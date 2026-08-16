"""
test_cleanup_test_data.py — Тесты для cleanup_test_data.py.

Создаёт временную SQLite-БД с реальной схемой бота, наполняет тестовыми данными
(модераторы + админы веб-панели + тестовые нарушители + наказания + chat_admins),
запускает скрипт через subprocess в dry-run и в apply, и проверяет что:
  • moderators, web_users, chat_settings НЕ тронуты
  • punishments полностью очищены
  • users очищены кроме модераторов
  • chat_admins по умолчанию не тронут, с --include-chat-admins — очищены
  • бэкап создаётся
  • VACUUM срабатывает
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

# cleanup_test_data.py — утилита обслуживания БД, а не тест. При заливке
# сюиты в main её не приложили (остался только этот тест к ней),
# поэтому она восстановлена из истории в tools/.
SCRIPT = Path(__file__).resolve().parent.parent / "tools" / "cleanup_test_data.py"
SCHEMA_SQL = """
CREATE TABLE users (
    user_id INTEGER PRIMARY KEY,
    username VARCHAR(255),
    first_name VARCHAR(255),
    last_name VARCHAR(255),
    last_seen DATETIME
);
CREATE TABLE moderators (
    mod_id INTEGER PRIMARY KEY,
    username VARCHAR(255),
    first_name VARCHAR(255)
);
CREATE TABLE punishments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(user_id),
    mod_id INTEGER NOT NULL REFERENCES moderators(mod_id),
    chat_id INTEGER NOT NULL,
    action_type VARCHAR(20) NOT NULL,
    duration_seconds INTEGER,
    reason TEXT,
    message_text TEXT,
    permissions_snapshot TEXT,
    report_message_id BIGINT,
    created_at DATETIME,
    is_revoked BOOLEAN NOT NULL DEFAULT 0,
    revoked_at DATETIME,
    revoked_by_mod_id BIGINT
);
CREATE TABLE chat_admins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    added_by INTEGER,
    created_at DATETIME
);
CREATE TABLE chat_settings (
    chat_id INTEGER PRIMARY KEY,
    hashtag VARCHAR(64),
    report_chat_id BIGINT,
    warns_to_mute INTEGER,
    mute_duration_seconds INTEGER,
    warns_to_ban INTEGER,
    updated_at DATETIME
);
CREATE TABLE web_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username VARCHAR(64) NOT NULL UNIQUE,
    password_hash VARCHAR(255),
    is_su BOOLEAN NOT NULL DEFAULT 0,
    is_active BOOLEAN NOT NULL DEFAULT 1,
    created_at DATETIME,
    created_by VARCHAR(64),
    last_login_at DATETIME,
    tg_user_id BIGINT,
    tg_first_name VARCHAR(255),
    tg_last_name VARCHAR(255),
    tg_username VARCHAR(255),
    role VARCHAR(16) NOT NULL DEFAULT 'admin'
);
CREATE UNIQUE INDEX ix_web_users_tg_user_id ON web_users (tg_user_id) WHERE tg_user_id IS NOT NULL;
"""


@pytest.fixture
def temp_db(tmp_path: Path) -> Path:
    """Создаёт временную БД с тестовыми данными. Возвращает путь."""
    db_path = tmp_path / "shadow_logs.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA_SQL)

    # ── Модераторы (НЕ ТРОГАТЬ) ──
    conn.execute("INSERT INTO moderators (mod_id, username, first_name) VALUES (100001, 'mod_one', 'Mod1')")
    conn.execute("INSERT INTO moderators (mod_id, username, first_name) VALUES (100002, 'mod_two', 'Mod2')")

    # ── Веб-юзеры (НЕ ТРОГАТЬ) ──
    conn.execute("INSERT INTO web_users (username, password_hash, is_su, is_active, created_by, role) "
                 "VALUES ('su', NULL, 1, 1, 'system', 'su')")
    conn.execute("INSERT INTO web_users (username, password_hash, is_su, is_active, created_by, tg_user_id, tg_username, role) "
                 "VALUES ('admin1', 'salt:hash', 0, 1, 'su', 100001, 'mod_one', 'admin')")

    # ── Users: тестовые нарушители + один модератор (для проверки исключения) ──
    # 200001, 200002 — чисто тестовые нарушители
    conn.execute("INSERT INTO users (user_id, username, first_name) VALUES (200001, 'baduser1', 'Bad1')")
    conn.execute("INSERT INTO users (user_id, username, first_name) VALUES (200002, 'baduser2', 'Bad2')")
    # 100001 — модератор, случайно попавший в users (его НЕ надо удалять)
    conn.execute("INSERT INTO users (user_id, username, first_name) VALUES (100001, 'mod_one', 'Mod1')")

    # ── Punishments (УДАЛИТЬ ВСЕ) ──
    for uid in (200001, 200002, 100001):
        conn.execute(
            "INSERT INTO punishments (user_id, mod_id, chat_id, action_type, created_at) "
            f"VALUES ({uid}, 100001, 999, 'warn', '2026-07-26 10:00:00')"
        )

    # ── Chat_admins (опционально удалить) ──
    conn.execute("INSERT INTO chat_admins (chat_id, user_id, added_by) VALUES (999, 200001, 100001)")
    conn.execute("INSERT INTO chat_admins (chat_id, user_id, added_by) VALUES (999, 200002, 100001)")

    # ── Chat_settings (НЕ ТРОГАТЬ) ──
    conn.execute("INSERT INTO chat_settings (chat_id, hashtag, warns_to_mute, warns_to_ban) "
                 "VALUES (999, '#Test', 3, 5)")

    conn.commit()
    conn.close()
    return db_path


def _run(db_path: Path, *args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["DB_PATH"] = str(db_path)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=env,
    )


def _counts(db_path: Path) -> dict[str, int]:
    conn = sqlite3.connect(db_path)
    out = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
           for t in ("users", "moderators", "punishments", "chat_admins", "chat_settings", "web_users")}
    out["users_mod_one"] = conn.execute(
        "SELECT COUNT(*) FROM users WHERE user_id = 100001"
    ).fetchone()[0]
    conn.close()
    return out


def test_dry_run_does_not_modify(temp_db: Path) -> None:
    """Dry-run не должен ничего менять."""
    before = _counts(temp_db)
    result = _run(temp_db)  # без --apply
    assert result.returncode == 0, result.stderr
    assert "DRY-RUN" in result.stdout
    after = _counts(temp_db)
    assert before == after, f"Dry-run изменил данные! before={before}, after={after}"


def test_apply_clears_punishments_and_test_users(temp_db: Path) -> None:
    """Apply должен удалить punishments + тестовых users, но НЕ модераторов и web_users."""
    result = _run(temp_db, "--apply")
    assert result.returncode == 0, result.stderr
    after = _counts(temp_db)
    assert after["punishments"] == 0, "punishments должны быть пустыми"
    # users: было 3 (200001, 200002, 100001), должно остаться 1 (100001 — модератор)
    assert after["users"] == 1, f"Должен остаться только модератор-в-users, got {after['users']}"
    assert after["users_mod_one"] == 1, "Модератор 100001 должен остаться в users"
    # moderators и web_users — не тронуты
    assert after["moderators"] == 2, "moderators не должны быть тронуты"
    assert after["web_users"] == 2, "web_users не должны быть тронуты"
    # chat_settings — не тронуты
    assert after["chat_settings"] == 1
    # chat_admins — по умолчанию НЕ тронуты
    assert after["chat_admins"] == 2, "chat_admins не должны быть тронуты без --include-chat-admins"


def test_apply_with_include_chat_admins(temp_db: Path) -> None:
    """С флагом --include-chat-admins должны очиститься и chat_admins."""
    result = _run(temp_db, "--apply", "--include-chat-admins")
    assert result.returncode == 0, result.stderr
    after = _counts(temp_db)
    assert after["chat_admins"] == 0, "chat_admins должны быть очищены"
    assert after["punishments"] == 0
    assert after["moderators"] == 2
    assert after["web_users"] == 2


def test_backup_is_created(temp_db: Path) -> None:
    """Apply должен создать .backup-*.db файл."""
    result = _run(temp_db, "--apply")
    assert result.returncode == 0, result.stderr
    parent = temp_db.parent
    backups = list(parent.glob(f"{temp_db.name}.backup-*.db"))
    assert len(backups) == 1, f"Должен быть один бэкап, found: {backups}"
    # В stdout должно быть упоминание бэкапа
    assert "backup" in result.stdout.lower() or "Бэкап" in result.stdout


def test_vacuum_runs(temp_db: Path) -> None:
    """После apply должен сработать VACUUM (проверяем по сообщению в stdout)."""
    result = _run(temp_db, "--apply")
    assert result.returncode == 0, result.stderr
    assert "VACUUM" in result.stdout


def test_refuses_on_empty_db(tmp_path: Path) -> None:
    """На пустой БД (без модераторов и веб-юзеров) скрипт должен отказаться."""
    db_path = tmp_path / "empty.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA_SQL)
    # Ничего не вставляем
    conn.commit()
    conn.close()
    result = _run(db_path, "--apply")
    assert result.returncode != 0, "Должен вернуть ненулевой exit code"
    assert "ОТКАЗ" in result.stderr or "отказывается" in result.stderr.lower()


def test_missing_db_file(tmp_path: Path) -> None:
    """Если файла БД нет — exit code 2."""
    result = _run(tmp_path / "nonexistent.db", "--apply")
    assert result.returncode == 2
    assert "не найден" in result.stderr


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
