"""Regression test: init_db() must not fail on old DB without
`project_status_option_name` column.

Reproducer for the v4.8.5.3 startup crash:
  sqlite3.OperationalError: no such column:
  github_settings.project_status_option_name

Root cause: in init_db() the ORM-SELECT of singleton row
`github_settings(id=1)` ran BEFORE the ALTER TABLE migration that
adds the new column. On an existing DB without that column, the SELECT
crashes the bot on startup.

Fix: move the ALTER TABLE migration block BEFORE the ORM-SELECT.
Also add the new column to the explicit `CREATE TABLE IF NOT EXISTS`
so the column exists even if the table was created manually from the
DDL string (not via Base.metadata.create_all).
"""
from __future__ import annotations

import asyncio
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Было `ROOT / "v485_work"` — отдельный рабочий каталог в песочнице, где писали
# тест. В репозитории модули лежат в корне.
V485_DIR = ROOT
sys.path.insert(0, str(V485_DIR))

failures: list[str] = []


def check(cond: bool, label: str) -> None:
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}")
        failures.append(label)


# ── T1: в db.py CREATE TABLE github_settings содержит колонку ────────────
db_src = (V485_DIR / "db.py").read_text(encoding="utf-8")

# Извлекаем блок CREATE TABLE IF NOT EXISTS github_settings целиком.
# Используем greedy `.*` чтобы дойти до последней `)` в блоке.
m = re.search(
    r"CREATE TABLE IF NOT EXISTS github_settings\s*\(.*?\)\s*\n\s*\"\"\"",
    db_src, re.DOTALL,
)
check(m is not None, "T1a: found CREATE TABLE IF NOT EXISTS github_settings")
if m:
    create_block = m.group(0)
    check(
        "project_status_option_name" in create_block,
        "T1b: CREATE TABLE statement includes project_status_option_name",
    )

# ── T2: ALTER TABLE-миграция идёт ДО ORM-SELECT singleton-строки ─────────
# Находим индекры двух блоков и проверяем порядок.
alter_idx = db_src.find(
    'ALTER TABLE github_settings ADD COLUMN "\n                "project_status_option_name'
)
# Альтернативный поиск, если выше не сработало.
if alter_idx == -1:
    alter_idx = db_src.find(
        "ALTER TABLE github_settings ADD COLUMN"
    )

select_idx = db_src.find("select(GithubSettings).where(GithubSettings.id == 1)")

check(alter_idx > 0, "T2a: found ALTER TABLE github_settings ADD COLUMN")
check(select_idx > 0, "T2b: found ORM SELECT github_settings id=1")
if alter_idx > 0 and select_idx > 0:
    check(
        alter_idx < select_idx,
        "T2c: ALTER TABLE migration runs BEFORE ORM SELECT (this was the bug)",
    )

# ── T3: реальный воспроизведённый запуск init_db() на старой БД ──────────
# Создаём временный sqlite-файл, вручную создаём старую версию таблицы
# github_settings БЕЗ новой колонки, потом вызываем init_db() и проверяем,
# что не падает и колонка появляется.


async def _run_init_db_against_old_schema() -> None:
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test_bot.db"

        # 1) Создаём старую схему вручную (без project_status_option_name).
        raw = sqlite3.connect(str(db_path))
        raw.executescript("""
            CREATE TABLE github_settings (
                id INTEGER PRIMARY KEY,
                pat_encrypted TEXT NULL,
                repo_owner VARCHAR(128) NULL,
                repo_name VARCHAR(128) NULL,
                project_node_id VARCHAR(64) NULL,
                project_number INTEGER NULL,
                project_owner_login VARCHAR(128) NULL,
                is_active BOOLEAN NOT NULL DEFAULT 0,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_by VARCHAR(64) NULL
            );
            INSERT INTO github_settings (id, is_active) VALUES (1, 0);
        """)
        raw.commit()
        raw.close()

        # 2) Подменяем env, чтобы db.py использовал наш temp-файл.
        #    db.py читает DB_PATH (не DATABASE_URL) и строит URL сам.
        os.environ["DB_PATH"] = str(db_path)
        os.environ.setdefault("FERNET_KEY", "")

        # 3) Перегружаем модуль db (чтобы подхватился новый DATABASE_URL).
        import importlib
        if "db" in sys.modules:
            del sys.modules["db"]
        import db as db_module
        importlib.reload(db_module)

        # Если FERNET_KEY не задан — _encrypt_pat упадёт, но нам он не нужен.
        # Просто вызываем init_db().
        await db_module.init_db()

        # 4) Проверяем, что колонка появилась и default заполнен.
        raw = sqlite3.connect(str(db_path))
        cols = [r[1] for r in raw.execute(
            "PRAGMA table_info(github_settings)"
        ).fetchall()]
        check(
            "project_status_option_name" in cols,
            "T3a: column project_status_option_name exists after init_db()",
        )
        val = raw.execute(
            "SELECT project_status_option_name FROM github_settings WHERE id=1"
        ).fetchone()
        check(
            val is not None and val[0] == "Предложено",
            f"T3b: existing singleton row has default 'Предложено' (got {val!r})",
        )
        raw.close()


asyncio.run(_run_init_db_against_old_schema())

# ── Итог ────────────────────────────────────────────────────────────────
# Было: sys.exit(0/1) прямо в теле модуля. Под pytest тело выполняется при
# импорте, и SystemExit обрывал весь прогон (INTERNALERROR). Теперь итог —
# обычный тест, а sys.exit остался только для запуска файла напрямую.


def test_all_checks_passed():
    """Итог всех проверок порядка миграций."""
    assert not failures, f"{len(failures)} проверок упало: " + ", ".join(failures)


if __name__ == "__main__":
    if failures:
        print(f"\nFAILED: {len(failures)} check(s)")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("\nALL CHECKS PASSED")
    sys.exit(0)
