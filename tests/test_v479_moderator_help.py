"""
v4.7.9 — тесты раздельного /help по ролям.

Проблема v4.7.8 и ранее: команда /help в ЛС боту работала только для
ADMIN_IDS (env). Модераторы, привязанные к чату через chat_admins,
могли использовать !mute/!warn в группах, но не видели /help — им было
неоткуда узнать синтаксис команд.

Решение v4.7.9:
  • ADMIN_IDS env → полный help (как раньше)
  • WebUser role='su' или 'admin', is_active=True → полный help
  • WebUser role='moderator', is_active=True → сокращённый help
    (только групповые команды + ссылка на веб-панель)
  • Все остальные — молчим (стелс сохраняется)

Тесты:
  1. APP_VERSION = "v4.7.9"
  2. /help от ADMIN_IDS env → полный текст (содержит /nightmode)
  3. /help от WebUser role='su' → полный текст
  4. /help от WebUser role='admin' → полный текст
  5. /help от WebUser role='moderator', is_active=True → сокращённый текст
     (содержит !mute, НЕ содержит /nightmode)
  6. /help от WebUser role='moderator', is_active=False → молчим (стелс)
  7. /help от постороннего (нет WebUser) → молчим (стелс)
  8. Сокращённый help содержит ссылку на веб-панель
  9. Сокращённый help НЕ содержит /sethashtag, /sanitary, /nightmode, /addadmin
  10. Сокращённый help содержит предупреждение про !resetwarns
  11. Changelog содержит v4.7.9
  12. Static check: код содержит _HELP_FULL_TEXT и _HELP_MODERATOR_TEXT
"""
from _paths import _P  # noqa: E402  (корень вычисляется от __file__)

import os
import sys
import re
import asyncio
import unittest
from unittest.mock import MagicMock, AsyncMock, patch

sys.path.insert(0, _P())
sys.path.insert(0, _P("tests"))

_DB_PATH = "/tmp/test_v479_moderator_help.db"
if os.path.exists(_DB_PATH):
    os.remove(_DB_PATH)

os.environ["BOT_TOKEN"] = "0:fake"
os.environ["ADMIN_IDS"] = "1"
os.environ["SU_IDS"] = "1"
os.environ["WEB_PASSWORD"] = "test_password_123"
os.environ["DB_PATH"] = _DB_PATH

from sqlalchemy import select, text
from db import (
    init_db, async_session, ChatSettings, WebUser, ChatAdmin,
)
import web_app
import bot_handlers as bh

from aiogram.fsm.context import FSMContext
from aiogram.types import (
    Chat, Message, User, MessageEntity, LinkPreviewOptions,
)


async def _seed():
    await init_db()
    async with async_session() as s:
        await s.execute(text("DELETE FROM permission_presets"))
        await s.execute(text("DELETE FROM chat_settings"))
        await s.execute(text("DELETE FROM web_users WHERE username != 'su'"))
        await s.execute(text("DELETE FROM chat_admins"))
        await s.execute(text("DELETE FROM word_filters"))
        await s.execute(text("DELETE FROM link_allowlist"))
        await s.commit()
    await init_db()
    async with async_session() as s:
        existing_su = (await s.execute(
            select(WebUser).where(WebUser.username == "su")
        )).scalar_one_or_none()
        if existing_su is None:
            s.add(WebUser(username="su", is_su=True, is_active=True,
                          role="su", created_by="system"))
            await s.commit()


def _make_private_message(from_user_id: int, text: str = "/help") -> Message:
    """Создаёт fake aiogram Message для ЛС боту."""
    user = User(
        id=from_user_id, is_bot=False, first_name=f"Test{from_user_id}",
    )
    chat = Chat(id=from_user_id, type="private", first_name=user.first_name)
    msg = MagicMock(spec=Message)
    msg.chat = chat
    msg.from_user = user
    msg.text = text
    msg.message_id = 1
    msg.date = None
    # reply — async method, captures the text the bot sends back
    msg.reply = AsyncMock()
    msg.answer = AsyncMock()
    msg.bot = MagicMock()
    msg.bot.send_message = AsyncMock()
    return msg


class TestV479ModeratorHelp(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await _seed()
        # Create test users with different roles
        async with async_session() as s:
            # Admin (role='admin', is_active=True)
            s.add(WebUser(
                username="admin_test", is_su=False, is_active=True,
                role="admin", created_by="su", tg_user_id=2001,
                password_hash=web_app._hash_password("x"),
            ))
            # Active moderator
            s.add(WebUser(
                username="mod_active", is_su=False, is_active=True,
                role="moderator", created_by="su", tg_user_id=2002,
                password_hash=web_app._hash_password("x"),
            ))
            # Deactivated moderator
            s.add(WebUser(
                username="mod_inactive", is_su=False, is_active=False,
                role="moderator", created_by="su", tg_user_id=2003,
                password_hash=web_app._hash_password("x"),
            ))
            # SU with tg_user_id (for testing role='su' path)
            su_wu = (await s.execute(
                select(WebUser).where(WebUser.username == "su")
            )).scalar_one()
            su_wu.tg_user_id = 2004
            await s.commit()

    async def asyncTearDown(self):
        pass

    # ──────────── 1. Version ────────────

    def test_01_app_version_is_v479(self):
        # v4.7.10+: APP_VERSION bumped beyond v4.7.9. This test now verifies
        # that we're at least on v4.7.9 (when moderator /help fix shipped),
        # so it doesn't break on every future version bump.
        v = web_app.APP_VERSION
        m = re.match(r"^v(\d+)\.(\d+)\.(\d+)$", v)
        self.assertIsNotNone(m, f"APP_VERSION format unexpected: {v!r}")
        major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
        self.assertTrue(
            (major, minor, patch) >= (4, 7, 9),
            f"APP_VERSION {v} should be >= v4.7.9 (moderator /help fix)"
        )

    # ──────────── 2. /help from ADMIN_IDS env → full text ────────────

    async def test_02_help_from_admin_ids_env_returns_full_text(self):
        """ADMIN_IDS env (user_id=1) → полный help с /nightmode."""
        msg = _make_private_message(from_user_id=1, text="/help")
        await bh.cmd_help(msg)
        msg.reply.assert_called_once()
        args, kwargs = msg.reply.call_args
        text_arg = args[0] if args else kwargs.get("text", "")
        self.assertIn("/nightmode", text_arg)
        self.assertIn("/sanitary", text_arg)
        self.assertIn("/sethashtag", text_arg)
        self.assertIn("!resetwarns", text_arg)

    # ──────────── 3. /help from WebUser role='su' → full text ────────────

    async def test_03_help_from_su_returns_full_text(self):
        """WebUser role='su' (tg_user_id=2004) → полный help."""
        msg = _make_private_message(from_user_id=2004, text="/help")
        await bh.cmd_help(msg)
        msg.reply.assert_called_once()
        text_arg = msg.reply.call_args[0][0]
        self.assertIn("/nightmode", text_arg)
        self.assertIn("/sanitary", text_arg)

    # ──────────── 4. /help from WebUser role='admin' → full text ────────────

    async def test_04_help_from_admin_returns_full_text(self):
        """WebUser role='admin' (tg_user_id=2001) → полный help."""
        msg = _make_private_message(from_user_id=2001, text="/help")
        await bh.cmd_help(msg)
        msg.reply.assert_called_once()
        text_arg = msg.reply.call_args[0][0]
        self.assertIn("/nightmode", text_arg)
        self.assertIn("/sanitary", text_arg)

    # ──────────── 5. /help from active moderator → shortened text ────────────

    async def test_05_help_from_active_moderator_returns_shortened_text(self):
        """WebUser role='moderator', is_active=True (tg_user_id=2002)
        → сокращённый help: !mute есть, !resetwarns упомянут как админ-only."""
        msg = _make_private_message(from_user_id=2002, text="/help")
        await bh.cmd_help(msg)
        msg.reply.assert_called_once()
        text_arg = msg.reply.call_args[0][0]
        # Has group commands (as actual usage docs, not just mentions)
        self.assertIn("!mute", text_arg)
        self.assertIn("!warn", text_arg)
        self.assertIn("!ban", text_arg)
        self.assertIn("!unmute", text_arg)
        self.assertIn("!unwarn", text_arg)
        self.assertIn("!warns", text_arg)
        # The moderator help should NOT include the full command syntax
        # for admin-only commands. Mentions of /nightmode etc. are allowed
        # only in the "Важно" warning section (where it says these are
        # admin-only). Verify by checking that no full syntax line exists
        # like "/nightmode chat_id" (which only appears in _HELP_FULL_TEXT).
        self.assertNotIn("/nightmode chat_id", text_arg)
        self.assertNotIn("/sanitary chat_id", text_arg)
        self.assertNotIn("/sethashtag chat_id", text_arg)
        self.assertNotIn("/addadmin chat_id", text_arg)
        self.assertNotIn("/cas chat_id", text_arg)

    # ──────────── 6. /help from deactivated moderator → silent ────────────

    async def test_06_help_from_deactivated_moderator_silent(self):
        """WebUser role='moderator', is_active=False (tg_user_id=2003)
        → молчим (стелс)."""
        msg = _make_private_message(from_user_id=2003, text="/help")
        await bh.cmd_help(msg)
        msg.reply.assert_not_called()

    # ──────────── 7. /help from stranger (no WebUser) → silent ────────────

    async def test_07_help_from_stranger_silent(self):
        """Посторонний (tg_user_id=99999, нет WebUser) → молчим."""
        msg = _make_private_message(from_user_id=99999, text="/help")
        await bh.cmd_help(msg)
        msg.reply.assert_not_called()

    # ──────────── 8. Shortened help contains web panel link ────────────

    async def test_08_shortened_help_contains_web_panel_link(self):
        """Сокращённый help содержит кликабельную ссылку на веб-панель."""
        msg = _make_private_message(from_user_id=2002, text="/help")
        await bh.cmd_help(msg)
        text_arg = msg.reply.call_args[0][0]
        self.assertIn("degraban.bothost.tech", text_arg)
        self.assertIn("<a href=", text_arg)  # clickable link

    # ──────────── 9. Shortened help does NOT contain admin command syntax ────────────

    async def test_09_shortened_help_excludes_admin_command_syntax(self):
        """Сокращённый help НЕ содержит полного синтаксиса команд настройки
        чатов (с аргументами). Имена команд могут упоминаться в секции
        "Важно" как admin-only, но без синтаксиса."""
        msg = _make_private_message(from_user_id=2002, text="/help")
        await bh.cmd_help(msg)
        text_arg = msg.reply.call_args[0][0]
        # Full syntax patterns that only appear in _HELP_FULL_TEXT:
        admin_syntax_patterns = [
            "/sethashtag chat_id",
            "/setreport chat_id",
            "/warns_mute chat_id",
            "/warns_ban chat_id",
            "/mute_duration chat_id",
            "/addadmin chat_id",
            "/deladmin chat_id",
            "/settings chat_id",
            "/bansticker",
            "/liststickers",
            "/delsticker",
            "/addword chat_id",
            "/delword chat_id",
            "/listwords",
            "/linkfilter chat_id",
            "/linkallow chat_id",
            "/linkallowlist",
            "/cas chat_id",
            "/nightmode chat_id",
            "/sanitary chat_id",
            "/warndecay chat_id",
        ]
        for pat in admin_syntax_patterns:
            self.assertNotIn(
                pat, text_arg,
                f"Shortened help should NOT contain full syntax '{pat}'",
            )

    # ──────────── 10. Shortened help warns about !resetwarns ────────────

    async def test_10_shortened_help_warns_about_resetwarns(self):
        """Сокращённый help явно говорит, что !resetwarns только для админов."""
        msg = _make_private_message(from_user_id=2002, text="/help")
        await bh.cmd_help(msg)
        text_arg = msg.reply.call_args[0][0]
        self.assertIn("!resetwarns", text_arg)
        # Should mention it's admin-only
        self.assertTrue(
            "только администраторам" in text_arg.lower()
            or "только для админов" in text_arg.lower(),
            "Shortened help should warn that !resetwarns is admin-only",
        )

    # ──────────── 11. Changelog contains v4.7.9 ────────────

    def test_11_changelog_contains_v479(self):
        with open(_P("templates/base.html")) as f:
            content = f.read()
        self.assertIn("v4.7.9", content)
        self.assertIn("модератор", content.lower())

    # ──────────── 12. Static check: code has _HELP_FULL_TEXT and _HELP_MODERATOR_TEXT ────────────

    def test_12_code_has_separate_help_text_constants(self):
        """В bot_handlers.py есть отдельные константы для полного и
        сокращённого help-текста."""
        self.assertTrue(hasattr(bh, "_HELP_FULL_TEXT"))
        self.assertTrue(hasattr(bh, "_HELP_MODERATOR_TEXT"))
        self.assertIsInstance(bh._HELP_FULL_TEXT, str)
        self.assertIsInstance(bh._HELP_MODERATOR_TEXT, str)
        # They should be different
        self.assertNotEqual(bh._HELP_FULL_TEXT, bh._HELP_MODERATOR_TEXT)
        # Full text is longer than moderator text
        self.assertGreater(
            len(bh._HELP_FULL_TEXT), len(bh._HELP_MODERATOR_TEXT),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
