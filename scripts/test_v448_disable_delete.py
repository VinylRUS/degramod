"""
test_v448_disable_delete.py — Тесты v4.4.8:
  1. _DisabledChatMiddleware: чат с is_enabled=False полностью игнорируется
     (ни один handler не вызывается), чат с is_enabled=True пропускается.
  2. POST /admin/chats/{id}/delete: бот ливает, chat_settings/chat_admins/
     punishments удаляются; chat_id=0 защищён от удаления.
  3. _is_moderation_command guard из v4.4.8 fix всё ещё работает корректно.

Все тесты используют общий in-memory SQLite (DB_PATH=:memory:). Между тестами
таблицы чистятся в asyncSetUp — это даёт изоляцию без пересоздания engine.
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# Подкладываем test-окружение ДО импорта модулей проекта.
os.environ.setdefault("DB_PATH", ":memory:")
os.environ.setdefault("BOT_TOKEN", "test:token")
os.environ.setdefault("WEB_PASSWORD", "test-pwd")
os.environ.setdefault("SESSION_SECRET", "test-secret-xxxxxxxxxxxxxxxxxxxxx")
os.environ.setdefault("ADMIN_IDS", "111111111")

sys.path.insert(0, "/home/z/my-project/v4.5")

import aiogram.types as _aiogram_types  # noqa: E402
from sqlalchemy import select, delete  # noqa: E402

from db import (  # noqa: E402
    async_session, init_db, engine, ChatSettings, ChatAdmin, Punishment,
    User, Moderator, WebUser,
)


async def _clear_all_tables():
    """Чистит все таблицы между тестами для изоляции."""
    async with async_session() as s:
        await s.execute(delete(Punishment))
        await s.execute(delete(ChatAdmin))
        await s.execute(delete(ChatSettings))
        await s.execute(delete(User))
        await s.execute(delete(Moderator))
        await s.execute(delete(WebUser))
        await s.commit()
    # v4.5.1: отключаем rate-limit на /login для тестов
    try:
        import web_app
        web_app._check_login_rate_limit = lambda ip: True
    except ImportError:
        pass


def _fake_message(chat_id: int, chat_type: str = "supergroup", text: str | None = "hello"):
    """Создаёт MagicMock aiogram.Message с минимально нужными полями."""
    msg = MagicMock(spec=_aiogram_types.Message)
    msg.chat = MagicMock()
    msg.chat.id = chat_id
    msg.chat.type = chat_type
    msg.chat.title = f"Test chat {chat_id}"
    msg.text = text
    msg.from_user = MagicMock()
    msg.from_user.id = 999999999
    msg.from_user.username = "tester"
    msg.from_user.first_name = "Tester"
    msg.reply_to_message = None
    msg.delete = AsyncMock()
    msg.bot = MagicMock()
    msg.bot.send_message = AsyncMock()
    return msg


# ═══════════════════════════════════════════════════════════════════════════
# Тест 1: _DisabledChatMiddleware — чат с is_enabled=False полностью игнорируется
# ═══════════════════════════════════════════════════════════════════════════
class TestDisabledChatMiddleware(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await init_db()
        await _clear_all_tables()
        async with async_session() as s:
            s.add(ChatSettings(chat_id=-1001, title="Enabled", is_enabled=True))
            s.add(ChatSettings(chat_id=-1002, title="Disabled", is_enabled=False))
            await s.commit()

    async def test_disabled_chat_blocks_handler(self):
        """Сообщения в disabled-чате НЕ доходят до handler."""
        from bot_handlers import _DisabledChatMiddleware
        mw = _DisabledChatMiddleware()
        called = False

        async def fake_handler(event, data):
            nonlocal called
            called = True

        msg = _fake_message(chat_id=-1002, text="!mute 1d test")
        await mw(fake_handler, msg, {})
        self.assertFalse(called, "Handler should NOT be called for disabled chat")

    async def test_enabled_chat_passes_through(self):
        """Сообщения в enabled-чате доходят до handler."""
        from bot_handlers import _DisabledChatMiddleware
        mw = _DisabledChatMiddleware()
        called = False

        async def fake_handler(event, data):
            nonlocal called
            called = True

        msg = _fake_message(chat_id=-1001, text="!mute 1d test")
        await mw(fake_handler, msg, {})
        self.assertTrue(called, "Handler SHOULD be called for enabled chat")

    async def test_unregistered_chat_passes_through(self):
        """Чат без settings пропускается (catchall создаст settings)."""
        from bot_handlers import _DisabledChatMiddleware
        mw = _DisabledChatMiddleware()
        called = False

        async def fake_handler(event, data):
            nonlocal called
            called = True

        msg = _fake_message(chat_id=-9999, text="hello")
        await mw(fake_handler, msg, {})
        self.assertTrue(called, "Handler SHOULD be called for unregistered chat")

    async def test_private_message_passes_through(self):
        """Личные сообщения не фильтруются middleware."""
        from bot_handlers import _DisabledChatMiddleware
        mw = _DisabledChatMiddleware()
        called = False

        async def fake_handler(event, data):
            nonlocal called
            called = True

        msg = _fake_message(chat_id=999999999, chat_type="private", text="/addadmin")
        await mw(fake_handler, msg, {})
        self.assertTrue(called, "Private messages SHOULD pass through")

    async def test_disabled_chat_blocks_plain_text_too(self):
        """В disabled-чате блокируется даже обычный текст (catchall тоже не сработает)."""
        from bot_handlers import _DisabledChatMiddleware
        mw = _DisabledChatMiddleware()
        called = False

        async def fake_handler(event, data):
            nonlocal called
            called = True

        msg = _fake_message(chat_id=-1002, text="Привет, как дела?")
        await mw(fake_handler, msg, {})
        self.assertFalse(called, "Plain text in disabled chat should be blocked too")


# ═══════════════════════════════════════════════════════════════════════════
# Тест 2: _is_moderation_command — guard из v4.4.8 fix
# ═══════════════════════════════════════════════════════════════════════════
class TestIsModerationCommand(unittest.TestCase):
    """Регрессионный тест: guard из v4.4.8 fix всё ещё работает."""

    def test_plain_text_returns_false(self):
        from bot_handlers import _is_moderation_command
        self.assertFalse(_is_moderation_command("Привет, как дела?"))
        self.assertFalse(_is_moderation_command("Согласен с тобой"))
        self.assertFalse(_is_moderation_command("12345"))

    def test_actual_commands_return_true(self):
        from bot_handlers import _is_moderation_command
        self.assertTrue(_is_moderation_command("!mute 1d спам"))
        self.assertTrue(_is_moderation_command("!MUTE 2h причина"))
        self.assertTrue(_is_moderation_command("!warn мат"))
        self.assertTrue(_is_moderation_command("!ban рекуррент"))
        self.assertTrue(_is_moderation_command("!unmute"))
        self.assertTrue(_is_moderation_command("!unban"))
        self.assertTrue(_is_moderation_command("!unwarn"))
        self.assertTrue(_is_moderation_command("!unwarn 3"))
        self.assertTrue(_is_moderation_command("!warns"))
        self.assertTrue(_is_moderation_command("!resetwarns"))

    def test_typos_return_false(self):
        from bot_handlers import _is_moderation_command
        self.assertFalse(_is_moderation_command("!mut 1d"))
        self.assertFalse(_is_moderation_command("!warning мат"))
        self.assertFalse(_is_moderation_command("!warns_extra"))

    def test_leading_whitespace_tolerated(self):
        from bot_handlers import _is_moderation_command
        self.assertTrue(_is_moderation_command("  !warn мат"))

    def test_missing_reason_returns_false(self):
        from bot_handlers import _is_moderation_command
        self.assertFalse(_is_moderation_command("!warn"))
        self.assertFalse(_is_moderation_command("!ban"))


# ═══════════════════════════════════════════════════════════════════════════
# Тест 3: POST /admin/chats/{id}/delete — bot leaves + DB cleaned
# ═══════════════════════════════════════════════════════════════════════════
class TestAdminChatsDelete(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await init_db()
        await _clear_all_tables()

        # SU-аккаунт (init_db сам его пересоздаст при следующем вызове, но
        # мы только что очистили таблицу — добавим обратно для /login).
        async with async_session() as s:
            s.add(WebUser(username="su", is_su=True, is_active=True,
                           role="su", created_by="system"))
            s.add(ChatSettings(chat_id=-2001, title="Test Chat", is_enabled=True))
            s.add(ChatAdmin(chat_id=-2001, user_id=1001, added_by=999))
            s.add(ChatAdmin(chat_id=-2001, user_id=1002, added_by=999))
            s.add(User(user_id=2001, username="badguy"))
            s.add(Moderator(mod_id=999, username="admin"))
            s.add(Punishment(user_id=2001, mod_id=999, chat_id=-2001,
                              action_type="warn", duration_seconds=1,
                              reason="spam"))
            s.add(Punishment(user_id=2001, mod_id=999, chat_id=-2001,
                              action_type="mute", duration_seconds=3600,
                              reason="more spam"))
            s.add(Punishment(user_id=2001, mod_id=999, chat_id=-2001,
                              action_type="ban", reason="bye"))
            s.add(ChatSettings(chat_id=0))
            await s.commit()

        # Создаём app с mock-bot
        from web_app import create_app
        self.mock_bot = MagicMock()
        self.mock_bot.leave_chat = AsyncMock()
        self.app = create_app(bot=self.mock_bot)

        # HTTP-клиент
        from httpx import AsyncClient, ASGITransport
        self.client = AsyncClient(transport=ASGITransport(app=self.app), base_url="http://test")

        # Логинимся как SU (WEB_PASSWORD=test-pwd), получаем реальный cookie.
        resp = await self.client.post(
            "/login",
            data={"username": "su", "password": "test-pwd"},
            follow_redirects=False,
        )
        assert resp.status_code == 303, f"Login failed: {resp.status_code} {resp.text}"
        # Cookie ставится в response; httpx сам хранит cookies между запросами.

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_delete_chat_calls_bot_leave(self):
        """DELETE /admin/chats/-2001/delete вызывает bot.leave_chat(-2001)."""
        resp = await self.client.post("/admin/chats/-2001/delete", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.mock_bot.leave_chat.assert_awaited_once_with(chat_id=-2001)

    async def test_delete_chat_removes_all_related_rows(self):
        """После удаления чата в БД не остаётся settings/admins/punishments."""
        await self.client.post("/admin/chats/-2001/delete", follow_redirects=False)
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -2001)
            )).scalar_one_or_none()
            self.assertIsNone(cs, "ChatSettings should be deleted")

            cas = (await s.execute(
                select(ChatAdmin).where(ChatAdmin.chat_id == -2001)
            )).scalars().all()
            self.assertEqual(len(cas), 0, "ChatAdmins should be deleted")

            ps = (await s.execute(
                select(Punishment).where(Punishment.chat_id == -2001)
            )).scalars().all()
            self.assertEqual(len(ps), 0, "Punishments should be deleted")

    async def test_delete_chat_preserves_chat_id_zero(self):
        """DELETE /admin/chats/0/delete НЕ удаляет дефолтные настройки."""
        resp = await self.client.post("/admin/chats/0/delete", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == 0)
            )).scalar_one_or_none()
            self.assertIsNotNone(cs, "Default settings (chat_id=0) must NOT be deleted")
        self.mock_bot.leave_chat.assert_not_awaited()

    async def test_delete_chat_nonexistent_returns_redirect(self):
        """DELETE для несуществующего chat_id — redirect с flash 'not found'."""
        resp = await self.client.post("/admin/chats/-7777777/delete", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertIn("not+found", resp.headers["location"].lower())

    async def test_delete_chat_invalid_id_returns_redirect(self):
        """DELETE с нечисловым chat_id — redirect с flash 'Invalid'."""
        resp = await self.client.post("/admin/chats/abc/delete", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertIn("Invalid", resp.headers["location"])

    async def test_delete_chat_when_bot_leave_fails_still_deletes_db(self):
        """Если bot.leave_chat падает — БД всё равно чистится."""
        self.mock_bot.leave_chat.side_effect = Exception("kicked from chat")
        resp = await self.client.post("/admin/chats/-2001/delete", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -2001)
            )).scalar_one_or_none()
            self.assertIsNone(cs, "ChatSettings should be deleted even if bot.leave failed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
