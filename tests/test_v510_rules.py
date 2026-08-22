"""v5.1.0 — /rules: ссылка на правила, per-chat с дефолтом.

Запуск: uv run python tools/run_tests.py -k v510_rules
"""
from _paths import _P  # noqa: E402
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import TelegramAPIError

os.environ.setdefault("BOT_TOKEN", "test:test")
os.environ["ADMIN_IDS"] = "111"
os.environ["WEB_ALLOW_NO_SECRET"] = "1"
os.environ["DB_PATH"] = "/tmp/degramod_v510_rules.db"

sys.path.insert(0, _P())

import bot_handlers  # noqa: E402
from db import ChatSettings, init_db  # noqa: E402


class _FakeSettings:
    def __init__(self, rules_url):
        self.rules_url = rules_url


class TestRulesUrlResolution(unittest.TestCase):
    def test_default_when_none(self):
        self.assertEqual(
            bot_handlers._resolve_rules_url(_FakeSettings(None)),
            "https://rules.degradach.ru/",
        )

    def test_default_when_empty_string(self):
        self.assertEqual(
            bot_handlers._resolve_rules_url(_FakeSettings("   ")),
            "https://rules.degradach.ru/",
        )

    def test_default_when_settings_missing(self):
        self.assertEqual(
            bot_handlers._resolve_rules_url(None),
            "https://rules.degradach.ru/",
        )

    def test_per_chat_override(self):
        self.assertEqual(
            bot_handlers._resolve_rules_url(_FakeSettings("https://example.org/r")),
            "https://example.org/r",
        )

    def test_override_is_stripped(self):
        self.assertEqual(
            bot_handlers._resolve_rules_url(_FakeSettings("  https://example.org/r  ")),
            "https://example.org/r",
        )


class TestModelAndMigration(unittest.TestCase):
    def test_column_on_model(self):
        self.assertTrue(hasattr(ChatSettings, "rules_url"))

    def test_legacy_migration_block_present(self):
        with open(_P("db.py")) as f:
            src = f.read()
        self.assertIn('"rules_url" not in existing_cols', src)
        self.assertIn("ALTER TABLE chat_settings ADD COLUMN rules_url", src)

    def test_alembic_revision_present(self):
        import pathlib
        versions = pathlib.Path(_P("migrations/versions"))
        found = [p for p in versions.glob("*.py") if "rules_url" in p.name]
        self.assertTrue(found, "нет ревизии Alembic для rules_url")

    def test_default_constant(self):
        self.assertEqual(bot_handlers.RULES_URL_DEFAULT, "https://rules.degradach.ru/")


class TestFailedDeleteLoggedAsWarning(unittest.IsolatedAsyncioTestCase):
    """Фикс финального ревью: неудачное удаление команды /rules в группе
    логировалось как debug — запрос остаётся висеть в чате, а удаление
    команды ровно для этого и существует. Теперь warning.
    """

    async def asyncSetUp(self):
        await init_db()
        bot_handlers._rules_last_call.clear()

    async def asyncTearDown(self):
        bot_handlers._rules_last_call.clear()

    async def test_delete_failure_logs_warning_not_debug(self):
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=777, username="u", first_name="U", last_name=""),
            chat=SimpleNamespace(id=-100999),
            delete=AsyncMock(side_effect=TelegramAPIError(method=None, message="forbidden")),
            bot=AsyncMock(),
        )
        with patch.object(bot_handlers, "_send_ephemeral", AsyncMock()), \
             patch.object(bot_handlers.logger, "warning") as mock_warning, \
             patch.object(bot_handlers.logger, "debug") as mock_debug:
            await bot_handlers.handle_rules_group(message)

        self.assertTrue(mock_warning.called,
                        "неудачное удаление команды должно логироваться как warning")
        debug_calls = [c for c in mock_debug.call_args_list
                       if c.args and "cannot delete" in str(c.args[0])]
        self.assertEqual(debug_calls, [],
                         "неудачное удаление больше не должно логироваться как debug")


if __name__ == "__main__":
    unittest.main(verbosity=2)
