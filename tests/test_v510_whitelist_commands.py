"""v5.1.0 — DM-команды управления вайтлистом ботов.

Доступ по ADMIN_IDS — паритет с /linkallow.

Запуск: uv run python tools/run_tests.py -k v510_whitelist_commands
"""
from _paths import _P  # noqa: E402
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("BOT_TOKEN", "test:test")
os.environ["ADMIN_IDS"] = "111"
os.environ["WEB_ALLOW_NO_SECRET"] = "1"
os.environ["DB_PATH"] = "/tmp/degramod_v510_wlcmd.db"

sys.path.insert(0, _P())

import bot_handlers  # noqa: E402
from db import BotWhitelist, async_session, init_db  # noqa: E402
from sqlalchemy import select  # noqa: E402

ADMIN_ID = 111
NON_ADMIN_ID = 222


class TestScopeParsing(unittest.TestCase):
    def test_global_keyword(self):
        self.assertEqual(bot_handlers._parse_whitelist_scope("global"), 0)

    def test_global_case_insensitive(self):
        self.assertEqual(bot_handlers._parse_whitelist_scope("GLOBAL"), 0)

    def test_numeric_chat_id(self):
        self.assertEqual(
            bot_handlers._parse_whitelist_scope("-1001234567890"), -1001234567890,
        )

    def test_garbage_returns_none(self):
        self.assertIsNone(bot_handlers._parse_whitelist_scope("не-число"))
        self.assertIsNone(bot_handlers._parse_whitelist_scope(""))


def _make_dm_message(text, from_user_id=ADMIN_ID):
    """Mock DM-сообщения — паттерн из test_v452_features._make_dm_message."""
    msg = MagicMock()
    msg.text = text
    msg.reply = AsyncMock()

    chat = MagicMock()
    chat.type = "private"
    chat.id = from_user_id
    msg.chat = chat

    user = MagicMock()
    user.id = from_user_id
    user.username = "admin"
    msg.from_user = user
    return msg


async def _clear_whitelist():
    async with async_session() as s:
        for row in (await s.execute(select(BotWhitelist))).scalars().all():
            await s.delete(row)
        await s.commit()


class TestBotAllow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await init_db()
        await _clear_whitelist()

    async def test_adds_to_global_scope(self):
        msg = _make_dm_message("/botallow global @gif")
        await bot_handlers.cmd_botallow(msg)
        msg.reply.assert_called_once()
        async with async_session() as s:
            row = (await s.execute(
                select(BotWhitelist).where(
                    BotWhitelist.chat_id == 0, BotWhitelist.bot_username == "gif",
                )
            )).scalar_one_or_none()
            self.assertIsNotNone(row)

    async def test_adds_to_chat_scope_and_normalizes_username(self):
        msg = _make_dm_message("/botallow -1001234567890 @VidBot")
        await bot_handlers.cmd_botallow(msg)
        async with async_session() as s:
            row = (await s.execute(
                select(BotWhitelist).where(
                    BotWhitelist.chat_id == -1001234567890,
                    BotWhitelist.bot_username == "vidbot",
                )
            )).scalar_one_or_none()
            self.assertIsNotNone(row)

    async def test_dedup_reports_and_keeps_single_row(self):
        async with async_session() as s:
            s.add(BotWhitelist(chat_id=0, bot_username="gif"))
            await s.commit()
        msg = _make_dm_message("/botallow global @gif")
        await bot_handlers.cmd_botallow(msg)
        reply_text = msg.reply.call_args[0][0]
        self.assertIn("уже", reply_text)
        async with async_session() as s:
            rows = (await s.execute(
                select(BotWhitelist).where(BotWhitelist.bot_username == "gif")
            )).scalars().all()
            self.assertEqual(len(rows), 1)

    async def test_bad_scope_rejected_no_row_created(self):
        msg = _make_dm_message("/botallow не-число @gif")
        await bot_handlers.cmd_botallow(msg)
        async with async_session() as s:
            rows = (await s.execute(select(BotWhitelist))).scalars().all()
            self.assertEqual(rows, [])

    async def test_non_admin_ignored(self):
        msg = _make_dm_message("/botallow global @gif", from_user_id=NON_ADMIN_ID)
        await bot_handlers.cmd_botallow(msg)
        msg.reply.assert_not_called()
        async with async_session() as s:
            rows = (await s.execute(select(BotWhitelist))).scalars().all()
            self.assertEqual(rows, [])


class TestBotUnallow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await init_db()
        await _clear_whitelist()
        async with async_session() as s:
            s.add(BotWhitelist(chat_id=0, bot_username="gif"))
            await s.commit()

    async def test_removes_existing_row(self):
        msg = _make_dm_message("/botunallow global @gif")
        await bot_handlers.cmd_botunallow(msg)
        async with async_session() as s:
            row = (await s.execute(
                select(BotWhitelist).where(BotWhitelist.bot_username == "gif")
            )).scalar_one_or_none()
            self.assertIsNone(row)

    async def test_missing_row_reports_not_found_and_changes_nothing(self):
        msg = _make_dm_message("/botunallow global @notfound")
        await bot_handlers.cmd_botunallow(msg)
        reply_text = msg.reply.call_args[0][0]
        self.assertIn("не найден", reply_text)
        async with async_session() as s:
            rows = (await s.execute(select(BotWhitelist))).scalars().all()
            self.assertEqual(len(rows), 1)  # исходная запись @gif цела

    async def test_non_admin_ignored(self):
        msg = _make_dm_message("/botunallow global @gif", from_user_id=NON_ADMIN_ID)
        await bot_handlers.cmd_botunallow(msg)
        msg.reply.assert_not_called()
        async with async_session() as s:
            row = (await s.execute(
                select(BotWhitelist).where(BotWhitelist.bot_username == "gif")
            )).scalar_one_or_none()
            self.assertIsNotNone(row)

    async def test_bare_at_sign_rejected_with_format_error(self):
        """Фикс финального ревью: раньше «/botunallow global @» отвечал
        «⚠️ @ не найден в вайтлисте» вместо понятной ошибки формата —
        _normalize_bot_username("@") даёт пустую строку, и без guard'а
        (который есть у cmd_botallow, но отсутствовал у cmd_botunallow)
        код шёл прямиком к поиску по пустому username.
        """
        msg = _make_dm_message("/botunallow global @")
        await bot_handlers.cmd_botunallow(msg)
        reply_text = msg.reply.call_args[0][0]
        self.assertIn("Укажите", reply_text)
        self.assertNotIn("не найден", reply_text)
        # Существующая запись @gif не пострадала.
        async with async_session() as s:
            rows = (await s.execute(select(BotWhitelist))).scalars().all()
            self.assertEqual(len(rows), 1)


class TestBotAllowList(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await init_db()
        await _clear_whitelist()
        async with async_session() as s:
            s.add(BotWhitelist(chat_id=0, bot_username="gif"))
            s.add(BotWhitelist(chat_id=-1001234567890, bot_username="vid"))
            await s.commit()

    async def test_shows_all_rows_without_scope_arg(self):
        msg = _make_dm_message("/botallowlist")
        await bot_handlers.cmd_botallowlist(msg)
        reply_text = msg.reply.call_args[0][0]
        self.assertIn("gif", reply_text)
        self.assertIn("vid", reply_text)

    async def test_filters_by_scope_arg(self):
        msg = _make_dm_message("/botallowlist global")
        await bot_handlers.cmd_botallowlist(msg)
        reply_text = msg.reply.call_args[0][0]
        self.assertIn("gif", reply_text)
        self.assertNotIn("vid", reply_text)

    async def test_empty_list_reports_empty(self):
        await _clear_whitelist()
        msg = _make_dm_message("/botallowlist")
        await bot_handlers.cmd_botallowlist(msg)
        reply_text = msg.reply.call_args[0][0]
        self.assertIn("пуст", reply_text)

    async def test_non_admin_ignored(self):
        msg = _make_dm_message("/botallowlist", from_user_id=NON_ADMIN_ID)
        await bot_handlers.cmd_botallowlist(msg)
        msg.reply.assert_not_called()


class TestNotPublishedInMenu(unittest.TestCase):
    def test_not_published_in_menu(self):
        import commands
        menu = {name for name, _ in commands.DM_MENU_COMMANDS}
        self.assertNotIn("botallow", menu)
        self.assertNotIn("botunallow", menu)
        self.assertNotIn("botallowlist", menu)


if __name__ == "__main__":
    unittest.main(verbosity=2)
