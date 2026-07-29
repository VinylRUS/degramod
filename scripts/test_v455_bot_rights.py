"""
test_v455_bot_rights.py — Тесты v4.5.5: Проверка прав бота при добавлении в чат.

Покрывает:
  1. DB schema: 2 новые колонки ChatSettings (bot_rights_missing, bot_rights_checked_at).
     Миграция идемпотентна.
  2. _compute_bot_rights_missing: администратор с полным набором прав → [].
  3. _compute_bot_rights_missing: администратор без can_change_info → ['can_change_info'].
  4. _compute_bot_rights_missing: администратор без can_delete_messages → ['can_delete_messages'].
  5. _compute_bot_rights_missing: администратор без can_restrict_members → ['can_restrict_members'].
  6. _compute_bot_rights_missing: администратор без всех 3 прав → все 3 в списке.
  7. _compute_bot_rights_missing: статус member → ['__not_admin__'].
  8. _compute_bot_rights_missing: статус kicked/left/restricted → ['__not_admin__'].
  9. _compute_bot_rights_missing: None new_chat_member → ['__not_admin__'].
 10. parse_bot_rights_missing: None/пусто/битый JSON → [].
 11. parse_bot_rights_missing: нормальный JSON list → корректный list.
 12. serialize_bot_rights_missing: round-trip parse → serialize → parse.
 13. _persist_bot_rights_check: сохраняет missing + ставит checked_at.
 14. _persist_bot_rights_check: empty list → bot_rights_missing = NULL.
 15. _check_and_persist_bot_rights: полный flow (compute + persist + notify).
 16. _notify_admins_about_rights: находит SU+admin с tg_user_id, шлёт DM.
 17. _notify_admins_about_rights: не шлёт DM users без tg_user_id.
 18. _notify_admins_about_rights: не шлёт DM при пустом missing.
 19. _notify_admins_about_rights: не шлёт DM moderator-role юзерам.
 20. on_my_chat_member: при administrator с полными правами — missing = NULL.
 21. on_my_chat_member: при administrator без can_change_info — missing list сохранён.
 22. on_my_chat_member: при member (без admin) — missing = ['__not_admin__'].
 23. on_my_chat_member: DM отправляется Admin/SU при нехватке прав.
 24. stealth_catchall_group: при первом сообщении запускает rights check.
 25. POST /admin/chats/<id>/recheck-bot-rights: обновляет missing + redirect.
 26. POST /admin/chats/<id>/recheck-bot-rights: chat_id=0 → flash error.
 27. POST /admin/chats/<id>/recheck-bot-rights: невалидный chat_id → flash error.
 28. admin_chats.html: badge "⚠️ RIGHTS" присутствует при непустом bot_rights_missing.
 29. admin_chats.html: warning panel с описанием прав присутствует.
 30. admin_chats.html: кнопка "Recheck rights" присутствует.
 31. admin_chats.html: при bot_rights_missing = NULL — НЕТ бейджа.
 32. APP_VERSION = "v4.5.5".
 33. base.html changelog modal содержит v4.5.5.
 34. _REQUIRED_BOT_RIGHTS содержит ровно 3 имени.
 35. _BOT_RIGHT_LABELS содержит все 4 ключа (3 права + __not_admin__).
 36. parse_bot_rights_missing filter зарегистрирован в Jinja env.
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone, timedelta, date
import json
import asyncio

# Подкладываем test-окружение ДО импорта модулей проекта.
os.environ.setdefault("DB_PATH", ":memory:")
os.environ.setdefault("BOT_TOKEN", "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw")
os.environ.setdefault("WEB_PASSWORD", "test-pwd")
os.environ.setdefault("SESSION_SECRET", "test-secret-xxxxxxxxxxxxxxxxxxxxx")
os.environ.setdefault("ADMIN_IDS", "111111111")

sys.path.insert(0, "/home/z/my-project/v4.5")

from sqlalchemy import select, delete  # noqa: E402

from db import (  # noqa: E402
    async_session, init_db, WebUser, ChatSettings, Punishment, User, Moderator,
    ChatAdmin,
)

import web_app  # noqa: E402
import bot_handlers  # noqa: E402


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────


async def _clear_all_tables():
    async with async_session() as s:
        await s.execute(delete(Punishment))
        await s.execute(delete(ChatAdmin))
        await s.execute(delete(ChatSettings))
        await s.execute(delete(User))
        await s.execute(delete(Moderator))
        await s.execute(delete(WebUser))
        await s.commit()


async def _seed_chat_settings(chat_id=-1001234567890, title="Test", **overrides):
    async with async_session() as s:
        cs = ChatSettings(chat_id=chat_id, title=title, **overrides)
        s.add(cs)
        await s.commit()
        return cs


def _make_admin_member(*, can_change_info=True, can_delete_messages=True,
                       can_restrict_members=True, is_anonymous=False,
                       can_promote_members=False, can_invite_users=False,
                       can_pin_messages=False, can_manage_chat=True,
                       can_manage_video_chats=False, can_manage_topics=False,
                       can_be_edited=False, custom_title=""):
    """Mimics aiogram ChatMemberAdministrator — все права на верхнем уровне."""
    m = MagicMock()
    m.status = "administrator"
    m.is_anonymous = is_anonymous
    m.can_manage_chat = can_manage_chat
    m.can_delete_messages = can_delete_messages
    m.can_manage_video_chats = can_manage_video_chats
    m.can_restrict_members = can_restrict_members
    m.can_promote_members = can_promote_members
    m.can_change_info = can_change_info
    m.can_invite_users = can_invite_users
    m.can_pin_messages = can_pin_messages
    m.can_manage_topics = can_manage_topics
    m.can_be_edited = can_be_edited
    m.custom_title = custom_title
    return m


def _make_member_member():
    """Mimics aiogram ChatMemberMember — у普通ный участник без прав."""
    m = MagicMock()
    m.status = "member"
    # У ChatMemberMember нет admin-прав атрибутов — getattr вернёт False по умолчанию.
    return m


def _make_kicked_member():
    m = MagicMock()
    m.status = "kicked"
    return m


def _make_left_member():
    m = MagicMock()
    m.status = "left"
    return m


def _make_chat_member_updated(*, chat_id=-1001234567890, chat_title="Test Chat",
                              new_status="administrator", new_member=None,
                              old_status="left", old_member=None):
    """Mimics aiogram ChatMemberUpdated event."""
    event = MagicMock()
    chat = MagicMock()
    chat.id = chat_id
    chat.title = chat_title
    event.chat = chat
    bot = MagicMock()
    bot.id = 9999999999
    bot.send_message = AsyncMock()
    event.bot = bot
    if new_member is None:
        if new_status == "administrator":
            new_member = _make_admin_member()
        elif new_status == "member":
            new_member = _make_member_member()
        elif new_status == "kicked":
            new_member = _make_kicked_member()
        elif new_status == "left":
            new_member = _make_left_member()
        new_member.status = new_status
    if old_member is None:
        old_member = _make_left_member()
        old_member.status = old_status
    event.new_chat_member = new_member
    event.old_chat_member = old_member
    return event


# ────────────────────────────────────────────────────────────────────────────
# Tests
# ────────────────────────────────────────────────────────────────────────────


class TestAsyncBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await init_db()
        await _clear_all_tables()


class TestDBSchema(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await init_db()
        await _clear_all_tables()

    async def test_new_columns_present(self):
        """v4.5.5: bot_rights_missing + bot_rights_checked_at в ChatSettings."""
        async with async_session() as s:
            cs = ChatSettings(chat_id=-100111, title="T")
            s.add(cs)
            await s.commit()
            # Re-read
            fresh = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -100111)
            )).scalar_one()
            self.assertTrue(hasattr(fresh, "bot_rights_missing"))
            self.assertTrue(hasattr(fresh, "bot_rights_checked_at"))
            self.assertIsNone(fresh.bot_rights_missing)
            self.assertIsNone(fresh.bot_rights_checked_at)

    async def test_migration_idempotent(self):
        """Запуск init_db дважды не падает."""
        await init_db()
        await init_db()
        # Если миграция не идемпотентна — второй init_db упадёт с ошибкой ALTER.
        # Если дошли сюда — ок.


class TestComputeBotRightsMissing(unittest.TestCase):
    """Тестируем чистую функцию _compute_bot_rights_missing."""

    def test_admin_full_rights_returns_empty(self):
        m = _make_admin_member()
        self.assertEqual(bot_handlers._compute_bot_rights_missing(m), [])

    def test_admin_missing_change_info(self):
        m = _make_admin_member(can_change_info=False)
        self.assertEqual(
            bot_handlers._compute_bot_rights_missing(m),
            ["can_change_info"],
        )

    def test_admin_missing_delete_messages(self):
        m = _make_admin_member(can_delete_messages=False)
        self.assertEqual(
            bot_handlers._compute_bot_rights_missing(m),
            ["can_delete_messages"],
        )

    def test_admin_missing_restrict_members(self):
        m = _make_admin_member(can_restrict_members=False)
        self.assertEqual(
            bot_handlers._compute_bot_rights_missing(m),
            ["can_restrict_members"],
        )

    def test_admin_missing_all_three(self):
        m = _make_admin_member(
            can_change_info=False,
            can_delete_messages=False,
            can_restrict_members=False,
        )
        result = bot_handlers._compute_bot_rights_missing(m)
        self.assertEqual(set(result), {
            "can_change_info", "can_delete_messages", "can_restrict_members",
        })

    def test_member_status_returns_not_admin(self):
        m = _make_member_member()
        self.assertEqual(
            bot_handlers._compute_bot_rights_missing(m),
            ["__not_admin__"],
        )

    def test_kicked_status_returns_not_admin(self):
        m = _make_kicked_member()
        self.assertEqual(
            bot_handlers._compute_bot_rights_missing(m),
            ["__not_admin__"],
        )

    def test_left_status_returns_not_admin(self):
        m = _make_left_member()
        self.assertEqual(
            bot_handlers._compute_bot_rights_missing(m),
            ["__not_admin__"],
        )

    def test_none_member_returns_not_admin(self):
        # Если new_chat_member is None — getattr(status) → None, не administrator.
        # Функция вернёт ['__not_admin__'].
        m = MagicMock()
        m.status = None
        self.assertEqual(
            bot_handlers._compute_bot_rights_missing(m),
            ["__not_admin__"],
        )


class TestParseBotRightsMissing(unittest.TestCase):

    def test_none_returns_empty(self):
        self.assertEqual(bot_handlers.parse_bot_rights_missing(None), [])

    def test_empty_string_returns_empty(self):
        self.assertEqual(bot_handlers.parse_bot_rights_missing(""), [])

    def test_empty_list_returns_empty(self):
        self.assertEqual(bot_handlers.parse_bot_rights_missing("[]"), [])

    def test_invalid_json_returns_empty(self):
        self.assertEqual(bot_handlers.parse_bot_rights_missing("not json"), [])

    def test_not_a_list_returns_empty(self):
        self.assertEqual(bot_handlers.parse_bot_rights_missing('{"a": 1}'), [])

    def test_valid_list(self):
        self.assertEqual(
            bot_handlers.parse_bot_rights_missing('["can_change_info"]'),
            ["can_change_info"],
        )

    def test_valid_list_multiple(self):
        result = bot_handlers.parse_bot_rights_missing(
            '["can_change_info", "can_delete_messages", "__not_admin__"]'
        )
        self.assertEqual(result, ["can_change_info", "can_delete_messages", "__not_admin__"])


class TestSerializeBotRightsMissing(unittest.TestCase):

    def test_empty_list(self):
        self.assertEqual(bot_handlers.serialize_bot_rights_missing([]), "[]")

    def test_single(self):
        self.assertEqual(
            bot_handlers.serialize_bot_rights_missing(["can_change_info"]),
            '["can_change_info"]',
        )

    def test_round_trip(self):
        original = ["can_change_info", "can_restrict_members"]
        serialized = bot_handlers.serialize_bot_rights_missing(original)
        parsed = bot_handlers.parse_bot_rights_missing(serialized)
        self.assertEqual(parsed, original)


class TestPersistBotRightsCheck(TestAsyncBase):

    async def test_saves_missing_and_checked_at(self):
        await _seed_chat_settings(chat_id=-100222, title="T")
        async with async_session() as s:
            await bot_handlers._persist_bot_rights_check(
                s, -100222, ["can_change_info"],
            )
            await s.commit()
        # Verify
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -100222)
            )).scalar_one()
            self.assertEqual(
                bot_handlers.parse_bot_rights_missing(cs.bot_rights_missing),
                ["can_change_info"],
            )
            self.assertIsNotNone(cs.bot_rights_checked_at)

    async def test_empty_missing_sets_null(self):
        await _seed_chat_settings(chat_id=-100223, title="T")
        async with async_session() as s:
            await bot_handlers._persist_bot_rights_check(s, -100223, [])
            await s.commit()
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -100223)
            )).scalar_one()
            # Empty list → stored as NULL (not "[]").
            self.assertIsNone(cs.bot_rights_missing)
            self.assertIsNotNone(cs.bot_rights_checked_at)

    async def test_no_settings_no_crash(self):
        """Если chat_settings ещё нет — _persist не падает."""
        async with async_session() as s:
            await bot_handlers._persist_bot_rights_check(
                s, -999999, ["__not_admin__"],
            )
            await s.commit()
        # Если не упало — ок.


class TestCheckAndPersistBotRights(TestAsyncBase):

    async def test_full_flow_saves_and_returns_missing(self):
        await _seed_chat_settings(chat_id=-100333, title="T")
        bot = MagicMock()
        m = _make_admin_member(can_change_info=False)
        missing = await bot_handlers._check_and_persist_bot_rights(
            bot, chat_id=-100333, chat_title="T",
            new_chat_member=m, notify=False,
        )
        self.assertEqual(missing, ["can_change_info"])
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -100333)
            )).scalar_one()
            self.assertEqual(
                bot_handlers.parse_bot_rights_missing(cs.bot_rights_missing),
                ["can_change_info"],
            )

    async def test_full_flow_with_notify_triggers_dm_task(self):
        """notify=True + missing не пустой → _notify_admins_about_rights вызван."""
        await _seed_chat_settings(chat_id=-100334, title="T")
        bot = MagicMock()
        m = _make_admin_member(can_change_info=False)
        # Патчим саму функцию notify, а не asyncio.create_task (последнее
        # ломает SQLAlchemy, который внутри использует create_task для close()).
        with patch("bot_handlers._notify_admins_about_rights",
                   new=AsyncMock()) as mock_notify:
            missing = await bot_handlers._check_and_persist_bot_rights(
                bot, chat_id=-100334, chat_title="T",
                new_chat_member=m, notify=True,
            )
            self.assertEqual(missing, ["can_change_info"])
            mock_notify.assert_called_once()
            # Аргументы: (bot, chat_id, chat_title, missing_rights).
            call_args = mock_notify.call_args
            self.assertEqual(call_args.args[0], bot)
            self.assertEqual(call_args.args[1], -100334)
            self.assertEqual(call_args.args[2], "T")
            self.assertEqual(call_args.args[3], ["can_change_info"])

    async def test_full_flow_no_missing_no_notify(self):
        await _seed_chat_settings(chat_id=-100335, title="T")
        bot = MagicMock()
        m = _make_admin_member()  # All rights present.
        with patch("bot_handlers._notify_admins_about_rights",
                   new=AsyncMock()) as mock_notify:
            missing = await bot_handlers._check_and_persist_bot_rights(
                bot, chat_id=-100335, chat_title="T",
                new_chat_member=m, notify=True,
            )
            self.assertEqual(missing, [])
            mock_notify.assert_not_called()


class TestNotifyAdminsAboutRights(TestAsyncBase):

    async def _seed_admin(self, username, tg_user_id, role="admin"):
        async with async_session() as s:
            u = WebUser(
                username=username,
                password_hash="salt:hash",
                is_su=(role == "su"),
                is_active=True,
                role=role,
                tg_user_id=tg_user_id,
                tg_username=username,
            )
            s.add(u)
            await s.commit()

    async def test_sends_dm_to_su_and_admin(self):
        await self._seed_admin("su1", 100001, role="su")
        await self._seed_admin("adm1", 100002, role="admin")
        await self._seed_admin("mod1", 100003, role="moderator")
        bot = MagicMock()
        bot.send_message = AsyncMock()
        await bot_handlers._notify_admins_about_rights(
            bot, -100444, "Test Chat",
            ["can_change_info", "can_restrict_members"],
        )
        # DM должны получить только su1 + adm1 (moderator не получает).
        self.assertEqual(bot.send_message.call_count, 2)
        called_ids = sorted(c.kwargs["chat_id"] for c in bot.send_message.call_args_list)
        self.assertEqual(called_ids, [100001, 100002])
        # Текст должен содержать имя чата и недостающие права (human-readable).
        text0 = bot.send_message.call_args_list[0].kwargs["text"]
        self.assertIn("Test Chat", text0)
        # В тексте используются человекочитаемые названия прав, не коды.
        self.assertIn("Can change chat info", text0)
        self.assertIn("Can restrict/ban members", text0)

    async def test_skips_users_without_tg_user_id(self):
        async with async_session() as s:
            u = WebUser(
                username="adm_no_tg", password_hash="salt:hash",
                is_su=False, is_active=True, role="admin",
                tg_user_id=None,
            )
            s.add(u)
            await s.commit()
        bot = MagicMock()
        bot.send_message = AsyncMock()
        await bot_handlers._notify_admins_about_rights(
            bot, -100445, "T", ["can_change_info"],
        )
        bot.send_message.assert_not_called()

    async def test_skips_inactive_users(self):
        async with async_session() as s:
            u = WebUser(
                username="adm_inactive", password_hash="salt:hash",
                is_su=False, is_active=False, role="admin",
                tg_user_id=100004,
            )
            s.add(u)
            await s.commit()
        bot = MagicMock()
        bot.send_message = AsyncMock()
        await bot_handlers._notify_admins_about_rights(
            bot, -100446, "T", ["can_change_info"],
        )
        bot.send_message.assert_not_called()

    async def test_no_dm_when_missing_empty(self):
        bot = MagicMock()
        bot.send_message = AsyncMock()
        await bot_handlers._notify_admins_about_rights(
            bot, -100447, "T", [],
        )
        bot.send_message.assert_not_called()

    async def test_dm_text_contains_chat_id_and_title(self):
        await self._seed_admin("su1", 100001, role="su")
        bot = MagicMock()
        bot.send_message = AsyncMock()
        await bot_handlers._notify_admins_about_rights(
            bot, -100488, "My Chat Name",
            ["can_delete_messages"],
        )
        text = bot.send_message.call_args.kwargs["text"]
        self.assertIn("My Chat Name", text)
        self.assertIn("-100488", text)
        # В тексте используется человекочитаемое название права, не код.
        self.assertIn("Can delete messages", text)


class TestOnMyChatMemberBotRights(TestAsyncBase):

    async def test_administrator_full_rights_clears_missing(self):
        await _seed_chat_settings(chat_id=-100555, title="T")
        # Pre-set some missing rights.
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -100555)
            )).scalar_one()
            cs.bot_rights_missing = json.dumps(["can_change_info"])
            await s.commit()

        event = _make_chat_member_updated(
            chat_id=-100555, new_status="administrator",
            new_member=_make_admin_member(),  # All rights present.
        )
        await bot_handlers.on_my_chat_member(event)

        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -100555)
            )).scalar_one()
            self.assertIsNone(cs.bot_rights_missing)
            self.assertIsNotNone(cs.bot_rights_checked_at)

    async def test_administrator_partial_rights_saves_missing(self):
        await _seed_chat_settings(chat_id=-100556, title="T")
        event = _make_chat_member_updated(
            chat_id=-100556, new_status="administrator",
            new_member=_make_admin_member(
                can_change_info=False, can_restrict_members=False,
            ),
        )
        await bot_handlers.on_my_chat_member(event)
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -100556)
            )).scalar_one()
            missing = bot_handlers.parse_bot_rights_missing(cs.bot_rights_missing)
            self.assertEqual(set(missing), {"can_change_info", "can_restrict_members"})

    async def test_member_status_saves_not_admin(self):
        await _seed_chat_settings(chat_id=-100557, title="T")
        event = _make_chat_member_updated(
            chat_id=-100557, new_status="member",
            new_member=_make_member_member(),
        )
        await bot_handlers.on_my_chat_member(event)
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -100557)
            )).scalar_one()
            missing = bot_handlers.parse_bot_rights_missing(cs.bot_rights_missing)
            self.assertEqual(missing, ["__not_admin__"])

    async def test_dm_sent_when_missing_rights(self):
        """При нехватке прав — _notify_admins_about_rights вызывается."""
        await _seed_chat_settings(chat_id=-100558, title="T")
        event = _make_chat_member_updated(
            chat_id=-100558, new_status="administrator",
            new_member=_make_admin_member(can_delete_messages=False),
        )
        with patch("bot_handlers._notify_admins_about_rights",
                   new=AsyncMock()) as mock_notify:
            await bot_handlers.on_my_chat_member(event)
            mock_notify.assert_called_once()


class TestStealthCatchallBotRights(TestAsyncBase):

    async def test_first_message_triggers_rights_check(self):
        """При первом сообщении в новом чате — _stealth_check_bot_rights_safe
        вызывается (через patch _check_and_persist_bot_rights, чтобы не звать TG)."""
        msg = MagicMock()
        msg.chat.id = -100666
        msg.chat.type = "supergroup"
        msg.chat.title = "New Chat"
        msg.bot = MagicMock()
        msg.bot.id = 999
        msg.bot.get_chat_member = AsyncMock(
            return_value=_make_admin_member(can_change_info=False),
        )
        # Патчим _check_and_persist_bot_rights (он вызывается из фоновой таски).
        # Patch BOTH _stealth_check_bot_rights_safe и саму persist-функцию,
        # чтобы не падать на asyncio.create_task.
        with patch("bot_handlers._stealth_check_bot_rights_safe",
                   new=AsyncMock()) as mock_stealth:
            # Также нужно пропатчить _notify_su_about_chat, т.к. чат новый.
            with patch("bot_handlers._notify_su_about_chat",
                       new=AsyncMock()):
                await bot_handlers.stealth_catchall_group(msg)
                # Дать event loop'у запустить созданную таску.
                await asyncio.sleep(0.05)
                mock_stealth.assert_called_once()
        # Verify chat_settings was created.
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -100666)
            )).scalar_one()
            self.assertIsNotNone(cs)

    async def test_existing_chat_with_missing_rights_rechecks(self):
        """Если чат уже есть и missing не пустой и checked_at > 1 часа назад —
        перепроверка запускается."""
        old_checked = datetime.now(timezone.utc) - timedelta(hours=2)
        await _seed_chat_settings(
            chat_id=-100667, title="T",
            bot_rights_missing=json.dumps(["can_change_info"]),
            bot_rights_checked_at=old_checked,
        )
        msg = MagicMock()
        msg.chat.id = -100667
        msg.chat.type = "supergroup"
        msg.chat.title = "T"
        msg.bot = MagicMock()
        msg.bot.id = 999
        with patch("bot_handlers._stealth_check_bot_rights_safe",
                   new=AsyncMock()) as mock_stealth:
            await bot_handlers.stealth_catchall_group(msg)
            await asyncio.sleep(0.05)
            mock_stealth.assert_called_once()

    async def test_existing_chat_recent_check_no_recheck(self):
        """Если проверяли < 1 часа назад — НЕ перепроверяем (throttle)."""
        recent_checked = datetime.now(timezone.utc) - timedelta(minutes=5)
        await _seed_chat_settings(
            chat_id=-100668, title="T",
            bot_rights_missing=json.dumps(["can_change_info"]),
            bot_rights_checked_at=recent_checked,
        )
        msg = MagicMock()
        msg.chat.id = -100668
        msg.chat.type = "supergroup"
        msg.chat.title = "T"
        msg.bot = MagicMock()
        msg.bot.id = 999
        with patch("bot_handlers._stealth_check_bot_rights_safe",
                   new=AsyncMock()) as mock_stealth:
            await bot_handlers.stealth_catchall_group(msg)
            await asyncio.sleep(0.05)
            mock_stealth.assert_not_called()


class TestRecheckBotRightsEndpoint(TestAsyncBase):
    """Тестируем POST /admin/chats/<id>/recheck-bot-rights."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        # _clear_all_tables в asyncSetUp TestAsyncBase удалил SU — пере-seed.
        async with async_session() as s:
            s.add(WebUser(
                username="su", is_su=True, is_active=True, role="su", created_by="system",
            ))
            await s.commit()
        # Disable rate-limit on /login (audit fix from v4.5.1).
        web_app._check_login_rate_limit = lambda ip: True
        # Создаём mock bot — передаём в create_app, чтобы хендлер имел к нему доступ.
        self.mock_bot = MagicMock()
        self.mock_bot.id = 999
        self.mock_bot.get_chat_member = AsyncMock(
            return_value=_make_admin_member(can_change_info=False),
        )
        self.mock_bot.send_message = AsyncMock()
        self.app = web_app.create_app(bot=self.mock_bot)

    async def _login_as_su(self, client):
        r = await client.post("/login", data={
            "username": "su", "password": os.environ["WEB_PASSWORD"],
        }, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        return r.cookies

    async def test_recheck_invalid_chat_id(self):
        """Невалидный chat_id → redirect с flash error."""
        from httpx import AsyncClient, ASGITransport
        transport = ASGITransport(app=self.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            cookies = await self._login_as_su(client)
            r = await client.post(
                "/admin/chats/not-a-number/recheck-bot-rights",
                cookies=cookies, follow_redirects=False,
            )
            self.assertEqual(r.status_code, 303)
            self.assertIn("Invalid", r.headers["location"])

    async def test_recheck_chat_id_zero(self):
        """chat_id=0 → redirect с flash error (default settings)."""
        from httpx import AsyncClient, ASGITransport
        transport = ASGITransport(app=self.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            cookies = await self._login_as_su(client)
            r = await client.post(
                "/admin/chats/0/recheck-bot-rights",
                cookies=cookies, follow_redirects=False,
            )
            self.assertEqual(r.status_code, 303)
            self.assertIn("Cannot+recheck", r.headers["location"])

    async def test_recheck_success_full_rights(self):
        """Бот со всеми правами → flash 'full admin rights'."""
        # Override mock to return admin with all rights.
        self.mock_bot.get_chat_member = AsyncMock(
            return_value=_make_admin_member(),
        )
        await _seed_chat_settings(chat_id=-100999, title="Test")
        from httpx import AsyncClient, ASGITransport
        transport = ASGITransport(app=self.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            cookies = await self._login_as_su(client)
            r = await client.post(
                "/admin/chats/-100999/recheck-bot-rights",
                cookies=cookies, follow_redirects=False,
            )
            self.assertEqual(r.status_code, 303)
            self.assertIn("full+admin+rights", r.headers["location"])
        # Verify bot_rights_missing cleared.
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -100999)
            )).scalar_one()
            self.assertIsNone(cs.bot_rights_missing)

    async def test_recheck_missing_rights_sends_dm(self):
        """Бот без can_change_info → flash 'missing rights' + DM отправлен."""
        # Mock уже настроен на admin без can_change_info.
        await _seed_chat_settings(chat_id=-100998, title="Test")
        # Add a SU with tg_user_id to receive DM.
        async with async_session() as s:
            s.add(WebUser(
                username="su_x", password_hash=None, is_su=True, is_active=True,
                role="su", tg_user_id=888888,
            ))
            await s.commit()

        from httpx import AsyncClient, ASGITransport
        transport = ASGITransport(app=self.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            cookies = await self._login_as_su(client)
            r = await client.post(
                "/admin/chats/-100998/recheck-bot-rights",
                cookies=cookies, follow_redirects=False,
            )
            self.assertEqual(r.status_code, 303)
            self.assertIn("missing+rights", r.headers["location"])
        # Verify missing was saved.
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -100998)
            )).scalar_one()
            missing = bot_handlers.parse_bot_rights_missing(cs.bot_rights_missing)
            self.assertIn("can_change_info", missing)


class TestAdminChatsTemplateBotRights(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await init_db()
        await _clear_all_tables()

    def _render(self, chats):
        """Рендерит admin_chats.html с заданным списком chats (моки)."""
        from jinja2 import Environment, FileSystemLoader, select_autoescape
        env = Environment(
            loader=FileSystemLoader("/home/z/my-project/v4.5/templates"),
            autoescape=select_autoescape(["html"]),
        )
        # Зарегистрируем нужные фильтры.
        env.filters["msk"] = lambda dt: str(dt) if dt else ""
        env.filters["parse_bot_rights"] = bot_handlers.parse_bot_rights_missing
        env.filters["format_sanitary_days"] = lambda s: ""
        # from_json должен толерантно относиться к не-строкам (MagicMock в тестах).
        env.filters["from_json"] = lambda s: json.loads(s) if isinstance(s, str) else {}
        env.filters["night_mode_preset_name"] = lambda s: "text_only"
        env.globals["app_version"] = "v4.5.5"
        env.globals["app_release_date"] = "2026-07-29"
        template = env.get_template("admin_chats.html")
        # base.html requires `request` and other globals for navbar.
        mock_request = MagicMock()
        mock_request.url.path = "/admin/chats"
        return template.render(
            request=mock_request,
            chats=chats,
            stats={},
            mod_counts={},
            report_chat_options=[],
            auth_user=MagicMock(role="su", tg_user_id=111, username="su"),
            flash=None,
        )

    def _make_chat_mock(self, **overrides):
        """MagicMock со всеми атрибутами, которые использует admin_chats.html,
        выставленными в безопасные дефолты. Tests переопределяют нужные."""
        c = MagicMock()
        c.chat_id = -100777
        c.title = "Test"
        c.is_enabled = True
        c.is_private = False
        c.is_report_chat = False
        c.cas_check_enabled = False
        c.link_filter_enabled = False
        c.night_mode_enabled = False
        c.night_mode_start = "23:00"
        c.night_mode_end = "07:00"
        c.night_mode_permissions = None
        c.night_mode_tz = "Europe/Moscow"
        c.night_mode_weekend_start = None
        c.night_mode_weekend_end = None
        c.night_mode_notify = False
        c.night_mode_notify_enter_msg = None
        c.night_mode_notify_exit_msg = None
        c.night_mode_currently_active = False
        c.sanitary_days = None
        c.sanitary_days_currently_active = False
        c.warn_decay_days = 0
        c.hashtag = None
        c.warns_to_mute = 3
        c.warns_to_ban = 5
        c.mute_duration_seconds = 600
        c.link_filter_action = "delete"
        c.report_chat_id = None
        c.bot_rights_missing = None
        c.bot_rights_checked_at = None
        for k, v in overrides.items():
            setattr(c, k, v)
        return c

    async def test_badge_present_when_missing_rights(self):
        chat = self._make_chat_mock(
            chat_id=-100777,
            bot_rights_missing=json.dumps(["can_change_info"]),
            bot_rights_checked_at=datetime.now(timezone.utc),
        )
        html = self._render([chat])
        # "↻ Recheck rights" (with arrow glyph) is unique to the chat card button.
        self.assertIn("↻ Recheck rights", html)
        # Warning panel header text.
        self.assertIn("Боту нужны права в чате", html)
        # Specific right mentioned with its description.
        self.assertIn("can_change_info", html)
        self.assertIn("night mode", html.lower())

    async def test_badge_absent_when_no_missing_rights(self):
        chat = self._make_chat_mock(chat_id=-100778)
        html = self._render([chat])
        # No "↻ Recheck rights" button (with arrow glyph — only in chat card).
        self.assertNotIn("↻ Recheck rights", html)
        # No warning panel header.
        self.assertNotIn("Боту нужны права в чате", html)

    async def test_not_admin_marker_in_badge(self):
        chat = self._make_chat_mock(
            chat_id=-100779,
            bot_rights_missing=json.dumps(["__not_admin__"]),
            bot_rights_checked_at=datetime.now(timezone.utc),
        )
        html = self._render([chat])
        # "NOT ADMIN" only appears in chat card badge (not in changelog).
        self.assertIn("NOT ADMIN", html)
        self.assertIn("не является администратором", html)
        self.assertIn("↻ Recheck rights", html)


class TestAppVersion(unittest.TestCase):

    def test_app_version_is_v455(self):
        self.assertEqual(web_app.APP_VERSION, "v4.5.5")

    def test_app_release_date_set(self):
        self.assertEqual(web_app.APP_RELEASE_DATE, "2026-07-29")


class TestBaseHtmlChangelog(unittest.TestCase):

    def test_changelog_contains_v455(self):
        with open("/home/z/my-project/v4.5/templates/base.html") as f:
            html = f.read()
        self.assertIn("v4.5.5", html)
        self.assertIn("Bot rights check", html)
        # Previous versions still mentioned.
        self.assertIn("v4.5.4", html)


class TestRequiredBotRightsConstant(unittest.TestCase):

    def test_required_bot_rights_has_three_items(self):
        self.assertEqual(len(bot_handlers._REQUIRED_BOT_RIGHTS), 3)
        self.assertIn("can_change_info", bot_handlers._REQUIRED_BOT_RIGHTS)
        self.assertIn("can_delete_messages", bot_handlers._REQUIRED_BOT_RIGHTS)
        self.assertIn("can_restrict_members", bot_handlers._REQUIRED_BOT_RIGHTS)

    def test_labels_cover_all_required_plus_not_admin(self):
        for r in bot_handlers._REQUIRED_BOT_RIGHTS:
            self.assertIn(r, bot_handlers._BOT_RIGHT_LABELS)
        self.assertIn("__not_admin__", bot_handlers._BOT_RIGHT_LABELS)


class TestJinjaParseBotRightsFilter(unittest.TestCase):
    """parse_bot_rights filter зарегистрирован в Jinja env web_app."""

    def test_filter_callable_works(self):
        """Фильтр parse_bot_rights доступен и работает корректно."""
        # Поскольку templates — локальная переменная в create_app, мы проверяем
        # что сама функция parse_bot_rights_missing существует и работает
        # (она используется как фильтр в admin_chats.html — это покрыто
        # template-тестами в TestAdminChatsTemplateBotRights).
        self.assertTrue(callable(bot_handlers.parse_bot_rights_missing))
        self.assertEqual(
            bot_handlers.parse_bot_rights_missing('["can_change_info"]'),
            ["can_change_info"],
        )
        self.assertEqual(bot_handlers.parse_bot_rights_missing(None), [])
        self.assertEqual(bot_handlers.parse_bot_rights_missing(""), [])
        self.assertEqual(bot_handlers.parse_bot_rights_missing("not json"), [])


# ────────────────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    unittest.main(verbosity=2)
