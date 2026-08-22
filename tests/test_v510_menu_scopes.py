"""v5.1.0 — меню команд: что публикуется и в каких скоупах.

Мод-команды не публикуются нигде: скоуп AllChatAdministrators у Telegram
означает настоящих админов чата, а _is_admin про них не знает — он смотрит
ADMIN_IDS, WebUser и chat_admins. В этой инсталляции TG-админов больше,
и такой скоуп рекламировал бы /ban тем, кому он запрещён.

Запуск: uv run python tools/run_tests.py -k v510_menu
"""
from _paths import _P  # noqa: E402
import asyncio
import os
import sys
import unittest

os.environ.setdefault("BOT_TOKEN", "test:test")
os.environ["ADMIN_IDS"] = "111"
os.environ["WEB_ALLOW_NO_SECRET"] = "1"
os.environ["DB_PATH"] = "/tmp/degramod_v510_menu.db"

sys.path.insert(0, _P())

import commands  # noqa: E402


class _RecordingBot:
    """Фиксирует вызовы set_my_commands/delete_my_commands."""

    def __init__(self):
        self.calls = []
        self.deleted = []

    async def set_my_commands(self, commands_list, scope=None, **kw):
        self.calls.append((type(scope).__name__ if scope else None,
                           [c.command for c in commands_list]))

    async def delete_my_commands(self, scope=None, **kw):
        self.deleted.append(type(scope).__name__ if scope else None)


class TestMenuScopes(unittest.TestCase):
    def setUp(self):
        import bot as bot_module
        self.mod = bot_module
        self.bot = _RecordingBot()
        asyncio.run(self.mod._publish_bot_commands(self.bot))
        self.by_scope = dict(self.bot.calls)

    def test_group_scope_has_only_user_commands(self):
        self.assertEqual(sorted(self.by_scope["BotCommandScopeAllGroupChats"]),
                         ["mywarns", "rules"])

    def test_private_scope_matches_registry(self):
        expected = sorted(name for name, _ in commands.DM_MENU_COMMANDS)
        self.assertEqual(sorted(self.by_scope["BotCommandScopeAllPrivateChats"]),
                         expected)

    def test_no_mod_commands_anywhere(self):
        published = {c for _scope, names in self.bot.calls for c in names}
        mod_names = {
            s.name for s in commands.GROUP_COMMANDS
            if s.access != commands.Access.USER
        }
        self.assertEqual(published & mod_names, set(),
                         "мод-команды не должны публиковаться ни в одном скоупе")

    def test_chat_administrators_scope_not_set(self):
        # Скоупы Telegram не складываются: пустой админский скоуп отобрал бы
        # у админов /mywarns и /rules. Не задаём его вовсе — админы
        # наследуют AllGroupChats.
        self.assertNotIn("BotCommandScopeAllChatAdministrators", self.by_scope)

    def test_default_scope_cleared(self):
        self.assertIn(None, self.bot.deleted)


class TestFailureIsNonFatal(unittest.TestCase):
    def test_telegram_failure_does_not_raise(self):
        import bot as bot_module

        class _BrokenBot:
            async def set_my_commands(self, *a, **kw):
                raise RuntimeError("Telegram недоступен")

            async def delete_my_commands(self, *a, **kw):
                raise RuntimeError("Telegram недоступен")

        # Публикация меню не должна ронять старт бота.
        asyncio.run(bot_module._publish_bot_commands(_BrokenBot()))


class TestStepsAreIsolated(unittest.TestCase):
    """Отказ одного шага публикации не должен отменять остальные.

    До правки все три вызова (group scope, private scope, очистка
    default) жили под одним try/except: падение первого молча съедало
    два оставшихся. Ревью на Task 5 отметило это как Important-находку.
    """

    def test_first_step_failure_does_not_block_others(self):
        import bot as bot_module

        class _PartiallyBrokenBot:
            """Падает только на групповом скоупе, остальное фиксирует."""

            def __init__(self):
                self.set_my_commands_calls = []
                self.deleted = False

            async def set_my_commands(self, commands_list, scope=None, **kw):
                if type(scope).__name__ == "BotCommandScopeAllGroupChats":
                    raise RuntimeError("Telegram недоступен")
                self.set_my_commands_calls.append(type(scope).__name__)

            async def delete_my_commands(self, scope=None, **kw):
                self.deleted = True

        b = _PartiallyBrokenBot()
        asyncio.run(bot_module._publish_bot_commands(b))

        self.assertEqual(b.set_my_commands_calls, ["BotCommandScopeAllPrivateChats"],
                         "приватный скоуп должен опубликоваться, даже если групповой упал")
        self.assertTrue(b.deleted,
                        "очистка default-скоупа должна выполниться, даже если групповой упал")


class TestStealthRemovedDeliberately(unittest.TestCase):
    def test_blanket_delete_my_commands_gone(self):
        with open(_P("bot.py")) as f:
            src = f.read()
        self.assertNotIn("Bot commands cleared (stealth mode)", src,
                         "безусловная очистка меню заменена на _publish_bot_commands")


if __name__ == "__main__":
    unittest.main(verbosity=2)
