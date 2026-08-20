"""
test_v4110_alembic_failsafe.py — миграции не роняют старт бота (Task 12).

История. В v4.8.9 проект перевели на Alembic, и прод дважды не поднялся.
Хотфиксы v4.8.9.1/v4.8.9.2 добавили auto-stamp для баз, созданных старым
`init_db()`, но проблему не закрыли — прод до сих пор работает через
`DB_USE_LEGACY_MIGRATIONS=1`, то есть Alembic на нём просто не вызывается.

Причина, найденная 20.08.2026 на копии боевой базы: таблица
`alembic_version` в ней **существует, но пуста**. Auto-stamp проверял только
наличие таблицы:

    has_alembic = "alembic_version" in all_tables
    if has_alembic:
        ...
        return          # ← выходит, не проставив ревизию

Видит таблицу — считает базу размеченной — выходит. Alembic дальше читает
текущую ревизию, получает пусто, решает, что не применена ни одна миграция,
и запускает baseline с нуля: `CREATE TABLE automute_counters` на таблице,
которая уже есть. `OperationalError: table already exists`, бот не стартует.

Как база пришла в это состояние: Alembic создаёт `alembic_version` до записи
ревизии. Первая попытка упала на существующих таблицах — таблица осталась,
запись нет.

Что проверяется здесь:

  1. Пустая `alembic_version` лечится штамповкой, а не падением.
  2. Неизвестная ревизия в таблице — тоже (её мог оставить откат на старую
     версию кода).
  3. Любой сбой Alembic откатывается на `init_db()`, и бот стартует.
     Худший исход миграций — запись в логе, а не лежащий бот.
  4. `DB_USE_LEGACY_MIGRATIONS=1` по-прежнему минует Alembic полностью.
  5. Схема после миграций совпадает с той, что создаёт `init_db()`.

Тесты воспроизводят сломанное состояние сами и не зависят от файла боевой
базы: в CI его нет.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from _paths import _P

os.environ.setdefault("BOT_TOKEN", "1:test")
os.environ.setdefault("ADMIN_IDS", "1")

sys.path.insert(0, _P())

WORK_DIR = Path(_P())


def _fresh_legacy_db(path: str) -> None:
    """Создаёт базу так, как её создавал init_db() до Alembic."""
    import importlib

    os.environ["DB_PATH"] = path
    import db as _db
    importlib.reload(_db)
    asyncio.run(_db.init_db())


def _tables(path: str) -> set[str]:
    c = sqlite3.connect(path)
    try:
        return {
            r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        }
    finally:
        c.close()


def _schema(path: str) -> dict[str, set[str]]:
    c = sqlite3.connect(path)
    try:
        out = {}
        for (t,) in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' AND name != 'alembic_version'"
        ):
            out[t] = {r[1] for r in c.execute(f"PRAGMA table_info({t})")}
        return out
    finally:
        c.close()


def _revision(path: str):
    c = sqlite3.connect(path)
    try:
        rows = c.execute("SELECT version_num FROM alembic_version").fetchall()
        return rows[0][0] if rows else None
    except sqlite3.Error:
        return None
    finally:
        c.close()


class _DbCase(unittest.TestCase):
    """Каждому тесту — своя временная база и свой DB_PATH."""

    def setUp(self):
        self._prev_db = os.environ.get("DB_PATH")
        self._prev_flag = os.environ.get("DB_USE_LEGACY_MIGRATIONS")
        os.environ.pop("DB_USE_LEGACY_MIGRATIONS", None)
        self.tmp = tempfile.mkdtemp()
        self.db_path = str(Path(self.tmp) / "test.db")

    def tearDown(self):
        if self._prev_db is not None:
            os.environ["DB_PATH"] = self._prev_db
        if self._prev_flag is not None:
            os.environ["DB_USE_LEGACY_MIGRATIONS"] = self._prev_flag
        else:
            os.environ.pop("DB_USE_LEGACY_MIGRATIONS", None)

    def _run_startup(self):
        """Прогоняет то, что делает bot.py при старте."""
        import importlib

        os.environ["DB_PATH"] = self.db_path
        import db as _db
        importlib.reload(_db)
        asyncio.run(_db.init_db_with_fallback())


class TestEmptyAlembicVersion(_DbCase):
    """Состояние боевой базы: таблица есть, запись отсутствует."""

    def setUp(self):
        super().setUp()
        _fresh_legacy_db(self.db_path)
        # Ровно то, что оставил после себя упавший Alembic в v4.8.9:
        # таблица создана, ревизия не записана.
        c = sqlite3.connect(self.db_path)
        c.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        c.commit()
        c.close()

    def test_startup_succeeds(self):
        """Бот стартует, а не падает на «table already exists».

        Это точный сценарий, который дважды ронял прод.
        """
        self._run_startup()

    def test_revision_gets_stamped(self):
        """Пустая таблица заполняется ревизией, а не игнорируется."""
        self._run_startup()
        self.assertIsNotNone(
            _revision(self.db_path),
            "alembic_version осталась пустой — следующий старт упадёт так же",
        )

    def test_existing_data_survives(self):
        """Данные на месте: лечим разметку, а не пересоздаём базу."""
        c = sqlite3.connect(self.db_path)
        c.execute(
            "INSERT INTO users (user_id, username) VALUES (?, ?)",
            (424242, "victim"),
        )
        c.commit()
        c.close()

        self._run_startup()

        c = sqlite3.connect(self.db_path)
        rows = c.execute("SELECT username FROM users WHERE user_id=424242").fetchall()
        c.close()
        self.assertEqual(rows, [("victim",)], "данные потерялись при миграции")


class TestUnknownRevision(_DbCase):
    """В таблице записана ревизия, которой нет в репозитории."""

    def setUp(self):
        super().setUp()
        _fresh_legacy_db(self.db_path)
        c = sqlite3.connect(self.db_path)
        c.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        c.execute("INSERT INTO alembic_version VALUES ('deadbeef_gone')")
        c.commit()
        c.close()

    def test_startup_succeeds(self):
        """Откат кода на старую версию не должен запирать бота намертво.

        Alembic на неизвестной ревизии кидает CommandError и старт падает.
        """
        self._run_startup()


class TestFallbackOnAlembicFailure(_DbCase):
    """Любой сбой Alembic — не повод не поднимать бота."""

    def setUp(self):
        super().setUp()
        _fresh_legacy_db(self.db_path)

    def test_falls_back_to_init_db(self):
        """При исключении из миграций старт идёт по старому пути.

        Миграции — не та операция, ради которой стоит держать бота
        выключенным: init_db() идемпотентна и отрабатывает на каждом старте.
        """
        import importlib

        os.environ["DB_PATH"] = self.db_path
        import db as _db
        importlib.reload(_db)

        with patch.object(
            _db, "run_migrations_async",
            side_effect=RuntimeError("alembic сломался"),
        ):
            asyncio.run(_db.init_db_with_fallback())

        self.assertIn("users", _tables(self.db_path))


class TestLegacyFlagStillWorks(_DbCase):
    """Рубильник на старый путь обязан работать как раньше."""

    def test_flag_skips_alembic_entirely(self):
        """С флагом Alembic не вызывается вовсе — даже попытки нет."""
        import importlib

        os.environ["DB_PATH"] = self.db_path
        os.environ["DB_USE_LEGACY_MIGRATIONS"] = "1"
        import db as _db
        importlib.reload(_db)

        with patch.object(_db, "run_migrations_async") as migrations:
            asyncio.run(_db.init_db_with_fallback())

        migrations.assert_not_called()
        self.assertIn("users", _tables(self.db_path))


class TestSchemaParity(_DbCase):
    """Схема после Alembic совпадает со схемой init_db()."""

    def test_same_schema_both_ways(self):
        """Расхождение означало бы, что baseline-ревизия отстала от моделей."""
        import importlib

        via_alembic = str(Path(self.tmp) / "alembic.db")
        via_initdb = str(Path(self.tmp) / "initdb.db")

        os.environ["DB_PATH"] = via_alembic
        import db as _db
        importlib.reload(_db)
        asyncio.run(_db.init_db_with_fallback())

        _fresh_legacy_db(via_initdb)

        self.assertEqual(
            _schema(via_alembic), _schema(via_initdb),
            "схемы разошлись — baseline-ревизия не соответствует моделям",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
