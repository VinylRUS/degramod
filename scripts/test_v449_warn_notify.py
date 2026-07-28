"""
test_v449_warn_notify.py — Тесты v4.4.9: уведомление нарушителю при !warn.

Проверяем:
  1. При !warn вызывается bot.send_message с receiver_user_id=target.id
     (нарушитель получает ephemeral-уведомление).
  2. Также вызывается bot.send_message с receiver_user_id=mod.id
     (модератор получает подтверждение, как и раньше).
  3. В тексте нарушителю присутствует причина и кол-во варнов.
  4. Если пороги warns_to_mute / warns_to_ban > 0 — в тексте есть «Лимиты».
  5. Если юзер подошёл к границе (total == warns_to_ban - 1) — есть «Следующий варн — бан».
  6. При !mute уведомление нарушителю НЕ отправляется (только модератору).
  7. При !ban уведомление нарушителю НЕ отправляется (только модератору).
  8. Если отправка уведомления падает (TelegramBadRequest) — варн всё равно сохраняется в БД.

Используем mock-bot + Direct вызов _send_user_warn_notification + интеграционный
тест через handle_group_command с patched _is_admin и DB.
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
    async_session, init_db, ChatSettings, ChatAdmin, Punishment,
    User, Moderator, WebUser,
)


async def _clear_all_tables():
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


def _fake_user(uid: int, username: str = "user", first_name: str = "User") -> MagicMock:
    u = MagicMock(spec=_aiogram_types.User)
    u.id = uid
    u.username = username
    u.first_name = first_name
    u.last_name = None
    return u


def _fake_message(
    text: str,
    chat_id: int,
    mod: MagicMock,
    target: MagicMock,
    target_msg_text: str = "spam message",
) -> MagicMock:
    """Создаёт MagicMock aiogram.Message с reply_to_message."""
    msg = MagicMock(spec=_aiogram_types.Message)
    msg.chat = MagicMock()
    msg.chat.id = chat_id
    msg.chat.type = "supergroup"
    msg.chat.title = f"Test chat {chat_id}"
    msg.text = text
    msg.from_user = mod
    msg.reply_to_message = MagicMock(spec=_aiogram_types.Message)
    msg.reply_to_message.from_user = target
    msg.reply_to_message.text = target_msg_text
    msg.reply_to_message.message_id = 99999
    msg.reply_to_message.delete = AsyncMock()
    msg.delete = AsyncMock()
    # bot.send_message — собираем вызовы в списки для проверок
    msg.bot = MagicMock()
    msg.bot.send_message = AsyncMock()
    msg.bot.restrict_chat_member = AsyncMock()
    msg.bot.ban_chat_member = AsyncMock()
    msg.bot.get_chat_member = AsyncMock()
    # get_chat_member возвращает ChatMember с пустым permissions
    member = MagicMock()
    member.permissions = MagicMock()
    for f in ("can_send_messages", "can_send_audios", "can_send_documents",
              "can_send_photos", "can_send_videos", "can_send_video_notes",
              "can_send_voice_notes", "can_send_polls", "can_send_other_messages",
              "can_add_web_page_previews", "can_change_info", "can_invite_users",
              "can_pin_messages"):
        setattr(member.permissions, f, True)
    msg.bot.get_chat_member.return_value = member
    return msg


# ═══════════════════════════════════════════════════════════════════════════
# Тест 1: Прямой вызов _send_user_warn_notification
# ═══════════════════════════════════════════════════════════════════════════
class TestSendUserWarnNotification(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await init_db()
        await _clear_all_tables()

    async def test_sends_message_with_receiver_user_id_target(self):
        """bot.send_message вызывается с receiver_user_id=target.id."""
        from bot_handlers import _send_user_warn_notification
        bot = MagicMock()
        bot.send_message = AsyncMock()
        target = _fake_user(2002, "badguy", "Bad")
        settings = ChatSettings(chat_id=-3001, warns_to_mute=0, warns_to_ban=0)

        await _send_user_warn_notification(
            bot=bot, chat_id=-3001, target=target,
            reason="спам", total_warns=1, settings=settings,
        )

        bot.send_message.assert_awaited_once()
        kwargs = bot.send_message.await_args.kwargs
        self.assertEqual(kwargs["chat_id"], -3001)
        self.assertEqual(kwargs["receiver_user_id"], 2002)
        self.assertIn("спам", kwargs["text"])
        self.assertIn("1", kwargs["text"])

    async def test_thresholds_included_when_configured(self):
        """Если warns_to_mute=3, warns_to_ban=5 — в тексте есть 'Лимиты' и оба порога."""
        from bot_handlers import _send_user_warn_notification
        bot = MagicMock()
        bot.send_message = AsyncMock()
        target = _fake_user(2002)
        settings = ChatSettings(chat_id=-3001, warns_to_mute=3, warns_to_ban=5)

        await _send_user_warn_notification(
            bot=bot, chat_id=-3001, target=target,
            reason="мат", total_warns=2, settings=settings,
        )

        text = bot.send_message.await_args.kwargs["text"]
        self.assertIn("Лимиты", text)
        self.assertIn("3", text)
        self.assertIn("5", text)

    async def test_warning_when_one_step_from_ban(self):
        """Если total == warns_to_ban - 1 — есть 'Следующий варн — бан'."""
        from bot_handlers import _send_user_warn_notification
        bot = MagicMock()
        bot.send_message = AsyncMock()
        target = _fake_user(2002)
        settings = ChatSettings(chat_id=-3001, warns_to_mute=3, warns_to_ban=5)

        await _send_user_warn_notification(
            bot=bot, chat_id=-3001, target=target,
            reason="мат", total_warns=4, settings=settings,
        )

        text = bot.send_message.await_args.kwargs["text"]
        self.assertIn("Следующий варн — бан", text)

    async def test_warning_when_one_step_from_mute(self):
        """Если total == warns_to_mute - 1 — есть 'Следующий варн — мьют'."""
        from bot_handlers import _send_user_warn_notification
        bot = MagicMock()
        bot.send_message = AsyncMock()
        target = _fake_user(2002)
        settings = ChatSettings(chat_id=-3001, warns_to_mute=3, warns_to_ban=5)

        await _send_user_warn_notification(
            bot=bot, chat_id=-3001, target=target,
            reason="мат", total_warns=2, settings=settings,
        )

        text = bot.send_message.await_args.kwargs["text"]
        self.assertIn("Следующий варн — мьют", text)

    async def test_no_thresholds_when_zero(self):
        """Если оба порога = 0 — секция 'Лимиты' не выводится."""
        from bot_handlers import _send_user_warn_notification
        bot = MagicMock()
        bot.send_message = AsyncMock()
        target = _fake_user(2002)
        settings = ChatSettings(chat_id=-3001, warns_to_mute=0, warns_to_ban=0)

        await _send_user_warn_notification(
            bot=bot, chat_id=-3001, target=target,
            reason="спам", total_warns=5, settings=settings,
        )

        text = bot.send_message.await_args.kwargs["text"]
        self.assertNotIn("Лимиты", text)
        self.assertNotIn("Следующий варн", text)

    async def test_failure_does_not_raise(self):
        """Если bot.send_message падает — функция не поднимает исключение."""
        from bot_handlers import _send_user_warn_notification
        from aiogram.exceptions import TelegramBadRequest
        bot = MagicMock()
        # TelegramBadRequest требует (method, message) — создаём с MagicMock method.
        exc = TelegramBadRequest(method=MagicMock(), message="Bad Request: chat not found")
        bot.send_message = AsyncMock(side_effect=exc)
        target = _fake_user(2002)
        settings = ChatSettings(chat_id=-3001)

        # Не должно поднять
        await _send_user_warn_notification(
            bot=bot, chat_id=-3001, target=target,
            reason="test", total_warns=1, settings=settings,
        )


# ═══════════════════════════════════════════════════════════════════════════
# Тест 2: Интеграционный — !warn через handle_group_command
# ═══════════════════════════════════════════════════════════════════════════
class TestWarnHandlerIntegration(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await init_db()
        await _clear_all_tables()
        async with async_session() as s:
            s.add(ChatSettings(chat_id=-3001, title="Test", is_enabled=True,
                                warns_to_mute=3, warns_to_ban=5))
            await s.commit()

    async def test_warn_notifies_target_and_mod(self):
        """!warn вызывает два send_message: для target (receiver_user_id=2002)
        и для mod (receiver_user_id=111111111)."""
        from bot_handlers import handle_group_command

        mod = _fake_user(111111111, "admin", "Admin")
        target = _fake_user(2002, "badguy", "Bad")
        msg = _fake_message("!warn спам в чате", chat_id=-3001, mod=mod, target=target)

        # v4.5.1: _is_admin должен вернуть True только для mod (111111111),
        # для target (2002) — False (иначе сработает friendly-fire проверка).
        async def _fake_is_admin(session, chat_id, user_id):
            return user_id == 111111111
        with patch("bot_handlers._is_admin", new=_fake_is_admin):
            await handle_group_command(msg)

        # send_message должен был вызваться как минимум 2 раза:
        #   1. Уведомление нарушителю (receiver_user_id=2002)
        #   2. Ephemeral модератору (receiver_user_id=111111111)
        # (плюс возможен вызов _send_report в репорт-чат, но там receiver_user_id не передаётся)
        calls = msg.bot.send_message.await_args_list
        receiver_ids = [c.kwargs.get("receiver_user_id") for c in calls
                         if c.kwargs.get("receiver_user_id") is not None]
        self.assertIn(2002, receiver_ids, "target should receive notification")
        self.assertIn(111111111, receiver_ids, "mod should receive confirmation")

    async def test_warn_text_contains_reason_and_count(self):
        """В тексте уведомления нарушителю есть причина и кол-во варнов."""
        from bot_handlers import handle_group_command

        mod = _fake_user(111111111, "admin", "Admin")
        target = _fake_user(2002, "badguy", "Bad")
        msg = _fake_message("!warn мат в чате", chat_id=-3001, mod=mod, target=target)

        # v4.5.1: _is_admin → True только для mod
        async def _fake_is_admin(session, chat_id, user_id):
            return user_id == 111111111
        with patch("bot_handlers._is_admin", new=_fake_is_admin):
            await handle_group_command(msg)

        # Находим вызов, адресованный нарушителю
        target_call = None
        for c in msg.bot.send_message.await_args_list:
            if c.kwargs.get("receiver_user_id") == 2002:
                target_call = c
                break
        self.assertIsNotNone(target_call, "No call to target user")
        text = target_call.kwargs["text"]
        self.assertIn("мат в чате", text)
        self.assertIn("1", text)  # total_warns after first warn
        self.assertIn("Лимиты", text)
        self.assertIn("3", text)  # warns_to_mute
        self.assertIn("5", text)  # warns_to_ban

    async def test_mute_does_not_notify_target(self):
        """!mute НЕ отправляет уведомление нарушителю (receiver_user_id != target.id)."""
        from bot_handlers import handle_group_command

        mod = _fake_user(111111111, "admin", "Admin")
        target = _fake_user(2002, "badguy", "Bad")
        msg = _fake_message("!mute 1d спам", chat_id=-3001, mod=mod, target=target)

        # v4.5.1: _is_admin → True только для mod
        async def _fake_is_admin(session, chat_id, user_id):
            return user_id == 111111111
        with patch("bot_handlers._is_admin", new=_fake_is_admin):
            await handle_group_command(msg)

        # Проверяем что НЕТ вызова с receiver_user_id=target.id (2002)
        target_calls = [c for c in msg.bot.send_message.await_args_list
                          if c.kwargs.get("receiver_user_id") == 2002]
        self.assertEqual(len(target_calls), 0,
                         "Target user should NOT receive notification on mute")

    async def test_ban_does_not_notify_target(self):
        """!ban НЕ отправляет уведомление нарушителю (только модератору)."""
        from bot_handlers import handle_group_command

        mod = _fake_user(111111111, "admin", "Admin")
        target = _fake_user(2002, "badguy", "Bad")
        msg = _fake_message("!ban спамер", chat_id=-3001, mod=mod, target=target)

        # v4.5.1: _is_admin → True только для mod
        async def _fake_is_admin(session, chat_id, user_id):
            return user_id == 111111111
        with patch("bot_handlers._is_admin", new=_fake_is_admin):
            await handle_group_command(msg)

        target_calls = [c for c in msg.bot.send_message.await_args_list
                          if c.kwargs.get("receiver_user_id") == 2002]
        self.assertEqual(len(target_calls), 0,
                         "Target user should NOT receive notification on ban")

    async def test_warn_saved_in_db_even_if_notification_fails(self):
        """Если bot.send_message падает при уведомлении нарушителю —
        варн всё равно сохраняется в БД (1 запись в punishments)."""
        from bot_handlers import handle_group_command
        from aiogram.exceptions import TelegramBadRequest

        mod = _fake_user(111111111, "admin", "Admin")
        target = _fake_user(2002, "badguy", "Bad")
        msg = _fake_message("!warn мат", chat_id=-3001, mod=mod, target=target)

        # Все send_message с receiver_user_id=2002 падают
        async def failing_send(*args, **kwargs):
            if kwargs.get("receiver_user_id") == 2002:
                raise TelegramBadRequest("user blocked the bot")
            return MagicMock()

        msg.bot.send_message.side_effect = failing_send

        # v4.5.1: _is_admin → True только для mod
        async def _fake_is_admin(session, chat_id, user_id):
            return user_id == 111111111
        with patch("bot_handlers._is_admin", new=_fake_is_admin):
            await handle_group_command(msg)

        # В БД должна быть 1 запись о варне
        async with async_session() as s:
            puns = (await s.execute(
                select(Punishment).where(
                    Punishment.chat_id == -3001,
                    Punishment.action_type == "warn",
                )
            )).scalars().all()
            self.assertEqual(len(puns), 1, "Warn should be saved in DB")
            self.assertEqual(puns[0].reason, "мат")


if __name__ == "__main__":
    unittest.main(verbosity=2)
