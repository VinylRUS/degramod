"""v5.5.0 — легаси-миграция cas_verdicts/cas_settings в init_db().

CLAUDE.md, «База данных»: колонка добавляется в модель И идемпотентным
блоком в init_db(). Блок этот не декоративный — init_db() это реальный
путь на проде: рубильник DB_USE_LEGACY_MIGRATIONS=1 и fallback
init_db_with_fallback() при сбое Alembic.

create_all() колонки в СУЩЕСТВУЮЩУЮ таблицу не добавляет, поэтому без
блока боевая БД (cas_verdicts заведена в v5.3.2) роняет любой ORM-запрос
к CasVerdict: «no such column: cas_verdicts.spam_factor» — а это ночной
свип и вся страница /admin/cas.

Запуск: uv run python tools/run_tests.py -k v550_legacy
"""
import os
import sqlite3
import sys
import tempfile
import unittest

from _paths import _P

sys.path.insert(0, _P())
os.chdir(_P())

_DB_PATH = tempfile.mktemp(suffix="_v550_legacy.db")
os.environ["BOT_TOKEN"] = "123456…AAAA"
os.environ["ADMIN_IDS"] = "111"
os.environ["WEB_PASSWORD"] = "test_password_123"
os.environ["DB_PATH"] = _DB_PATH

from sqlalchemy import select  # noqa: E402

from db import CasSettings, CasVerdict, async_session, init_db  # noqa: E402


def _seed_v540_schema() -> None:
    """cas_verdicts в том виде, в каком её оставила v5.4.0 (без метрик)."""
    con = sqlite3.connect(_DB_PATH)
    con.execute("""
        CREATE TABLE cas_verdicts (
            user_id INTEGER PRIMARY KEY,
            checked_at DATETIME,
            source VARCHAR(16) NOT NULL,
            is_banned BOOLEAN NOT NULL,
            reason TEXT
        )
    """)
    con.execute(
        "INSERT INTO cas_verdicts VALUES "
        "(777, '2026-09-01 00:00:00', 'lols', 0, 'potential')"
    )
    con.commit()
    con.close()


class LegacyMigrationTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        if os.path.exists(_DB_PATH):
            os.remove(_DB_PATH)
        _seed_v540_schema()

    async def test_init_db_upgrades_existing_cas_verdicts(self):
        """init_db() на БД v5.4.0 → ORM-запрос к CasVerdict не падает."""
        await init_db()
        async with async_session() as s:
            rows = (await s.execute(select(CasVerdict))).scalars().all()
        self.assertEqual(len(rows), 1, "старая строка вердикта должна выжить")
        self.assertIsNone(rows[0].spam_factor)
        self.assertIsNone(rows[0].tier)

    async def test_cas_settings_seeded(self):
        """cas_settings создана и засеяна singleton'ом с дефолтами."""
        await init_db()
        async with async_session() as s:
            cfg = (await s.execute(
                select(CasSettings).where(CasSettings.id == 1)
            )).scalar_one()
        self.assertEqual(cfg.spamfactor_ban, 60.0)
        self.assertEqual(cfg.spamfactor_mute, 30.0)
        self.assertEqual(cfg.offenses_mute, 10)

    async def test_init_db_is_idempotent(self):
        """Повторный init_db() не падает на уже добавленных колонках."""
        await init_db()
        await init_db()
        async with async_session() as s:
            cnt = len((await s.execute(select(CasSettings))).scalars().all())
        self.assertEqual(cnt, 1, "seed не должен дублировать singleton")


if __name__ == "__main__":
    unittest.main(verbosity=2)
