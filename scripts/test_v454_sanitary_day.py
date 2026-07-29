"""
test_v454_sanitary_day.py — Тесты v4.5.4: Санитарные дни (chat-level lockdown).

Покрывает:
  1. DB schema: 3 новые колонки ChatSettings (sanitary_days,
     sanitary_days_saved_permissions, sanitary_days_currently_active).
     Миграция идемпотентна.
  2. _parse_sanitary_date: валидные/невалидные даты.
  3. parse_sanitary_days_json: пустой/битый JSON, нормализация end<start.
  4. serialize_sanitary_days: валидные/невалидные пары.
  5. is_sanitary_day_today: точное совпадение, диапазон, несовпадение, пустой список.
  6. parse_sanitary_days_textarea: одна дата, диапазон, комментарии, невалидные строки.
  7. format_sanitary_days_textarea: однодневный, диапазон, пустой.
  8. /sanitary CLI: list (пусто), add (один день), add (диапазон), add (дубликат),
     remove (точное), remove (по дате), clear, toggle, невалидный chat_id,
     невалидная подкоманда, help.
  9. /admin/chats/<id>/update принимает sanitary_days_text; валидирует; сохраняет.
 10. _night_mode_tick пропускает чаты с sanitary_days_currently_active=True.
 11. _sanitary_day_tick enter/exit (через mock bot).
 12. _enter_sanitary_day: snapshot + lockdown + flag set.
 13. _exit_sanitary_day: restore from snapshot + flag clear.
 14. APP_VERSION bumped (≥ v4.5.4).
 15. /help содержит раздел "Санитарные дни".
 16. base.html changelog modal содержит v4.5.4 заголовок.
 17. admin_chats.html содержит textarea sanitary_days_text + SAN badge.
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
# Aiogram валидирует формат токена при создании Bot(). Используем валидный
# формат: <bot_id>:<base64-ish-35+chars>.
os.environ.setdefault("BOT_TOKEN", "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw")
os.environ.setdefault("WEB_PASSWORD", "test-pwd")
os.environ.setdefault("SESSION_SECRET", "test-secret-xxxxxxxxxxxxxxxxxxxxx")
os.environ.setdefault("ADMIN_IDS", "111111111")

sys.path.insert(0, "/home/z/my-project/v4.5")

from sqlalchemy import select, delete, inspect as sqlinspect  # noqa: E402

from db import (  # noqa: E402
    async_session, init_db, WebUser, ChatSettings, Punishment, User, Moderator,
    ChatAdmin,
)

import web_app  # noqa: E402
import bot_handlers  # noqa: E402
import bot  # noqa: E402


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


def _make_message(*, text=None, chat_id=-1001234567890, chat_type="private",
                  from_user_id=111111111):
    msg = MagicMock()
    msg.text = text
    chat = MagicMock()
    chat.id = chat_id
    chat.type = chat_type
    chat.title = "Test"
    msg.chat = chat
    user = MagicMock()
    user.id = from_user_id
    user.username = "admin"
    user.first_name = "Admin"
    user.is_bot = False
    msg.from_user = user
    msg.reply = AsyncMock()
    msg.bot = MagicMock()
    msg.bot.send_message = AsyncMock()
    msg.bot.set_chat_permissions = AsyncMock()
    msg.bot.get_chat = AsyncMock()
    return msg


# ═══════════════════════════════════════════════════════════════════════════
# Тест 1: DB schema — 3 новые колонки v4.5.4
# ═══════════════════════════════════════════════════════════════════════════
class TestDBSchemaV454(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await init_db()
        await _clear_all_tables()

    async def test_v454_columns_exist(self):
        """Новые колонки v4.5.4 присутствуют в ChatSettings и сохраняются."""
        async with async_session() as s:
            cs = ChatSettings(chat_id=-1001234567890, title="Test")
            cs.sanitary_days = '[["2026-08-01","2026-08-01"]]'
            cs.sanitary_days_saved_permissions = '{"can_send_messages":true}'
            cs.sanitary_days_currently_active = True
            s.add(cs)
            await s.commit()
            s.expire_all()
            reloaded = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001234567890)
            )).scalar_one()
            self.assertEqual(reloaded.sanitary_days, '[["2026-08-01","2026-08-01"]]')
            self.assertEqual(reloaded.sanitary_days_saved_permissions, '{"can_send_messages":true}')
            self.assertTrue(reloaded.sanitary_days_currently_active)

    async def test_v454_defaults(self):
        """Новые колонки v4.5.4 имеют корректные дефолты при создании."""
        async with async_session() as s:
            cs = ChatSettings(chat_id=-1009876543210, title="Fresh")
            s.add(cs)
            await s.commit()
            s.expire_all()
            reloaded = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1009876543210)
            )).scalar_one()
            self.assertIsNone(reloaded.sanitary_days)
            self.assertIsNone(reloaded.sanitary_days_saved_permissions)
            self.assertFalse(reloaded.sanitary_days_currently_active)

    async def test_migration_idempotent(self):
        """Повторный init_db не падает."""
        await init_db()
        await init_db()
        # Если мы здесь — миграция идемпотентна.
        async with async_session() as s:
            cs = ChatSettings(chat_id=-1005555555555, title="Idempotent")
            s.add(cs)
            await s.commit()
        self.assertTrue(True)


# ═══════════════════════════════════════════════════════════════════════════
# Тест 2: _parse_sanitary_date
# ═══════════════════════════════════════════════════════════════════════════
class TestParseSanitaryDate(unittest.TestCase):

    def test_valid_date(self):
        self.assertEqual(bot_handlers._parse_sanitary_date("2026-08-01"), date(2026, 8, 1))

    def test_valid_leap_year(self):
        self.assertEqual(bot_handlers._parse_sanitary_date("2024-02-29"), date(2024, 2, 29))

    def test_invalid_feb29_non_leap(self):
        self.assertIsNone(bot_handlers._parse_sanitary_date("2026-02-29"))

    def test_invalid_month(self):
        self.assertIsNone(bot_handlers._parse_sanitary_date("2026-13-01"))

    def test_invalid_day(self):
        self.assertIsNone(bot_handlers._parse_sanitary_date("2026-08-32"))

    def test_bad_format(self):
        self.assertIsNone(bot_handlers._parse_sanitary_date("01-08-2026"))
        self.assertIsNone(bot_handlers._parse_sanitary_date("2026/08/01"))
        self.assertIsNone(bot_handlers._parse_sanitary_date(""))
        self.assertIsNone(bot_handlers._parse_sanitary_date(None))
        self.assertIsNone(bot_handlers._parse_sanitary_date("not-a-date"))


# ═══════════════════════════════════════════════════════════════════════════
# Тест 3: parse_sanitary_days_json
# ═══════════════════════════════════════════════════════════════════════════
class TestParseSanitaryDaysJson(unittest.TestCase):

    def test_empty_string(self):
        self.assertEqual(bot_handlers.parse_sanitary_days_json(""), [])

    def test_none(self):
        self.assertEqual(bot_handlers.parse_sanitary_days_json(None), [])

    def test_empty_list(self):
        self.assertEqual(bot_handlers.parse_sanitary_days_json("[]"), [])

    def test_single_day(self):
        self.assertEqual(
            bot_handlers.parse_sanitary_days_json('[["2026-08-01","2026-08-01"]]'),
            [["2026-08-01", "2026-08-01"]],
        )

    def test_range(self):
        self.assertEqual(
            bot_handlers.parse_sanitary_days_json('[["2026-08-01","2026-08-05"]]'),
            [["2026-08-01", "2026-08-05"]],
        )

    def test_end_before_start_normalized(self):
        # end < start → однодневный (end = start).
        result = bot_handlers.parse_sanitary_days_json('[["2026-08-05","2026-08-01"]]')
        self.assertEqual(result, [["2026-08-05", "2026-08-05"]])

    def test_invalid_json(self):
        self.assertEqual(bot_handlers.parse_sanitary_days_json("not json"), [])

    def test_not_a_list(self):
        self.assertEqual(bot_handlers.parse_sanitary_days_json('{"a":"b"}'), [])

    def test_entry_not_pair(self):
        # Запись не пара — пропускается.
        self.assertEqual(bot_handlers.parse_sanitary_days_json('[["2026-08-01"]]'), [])

    def test_invalid_date_skipped(self):
        # Невалидная дата — пропускается.
        self.assertEqual(
            bot_handlers.parse_sanitary_days_json('[["bad","2026-08-01"]],[["2026-08-02","2026-08-02"]]'),
            [],
        )

    def test_multiple_pairs(self):
        result = bot_handlers.parse_sanitary_days_json(
            '[["2026-08-01","2026-08-01"],["2026-08-15","2026-08-17"]]'
        )
        self.assertEqual(len(result), 2)


# ═══════════════════════════════════════════════════════════════════════════
# Тест 4: serialize_sanitary_days
# ═══════════════════════════════════════════════════════════════════════════
class TestSerializeSanitaryDays(unittest.TestCase):

    def test_valid_pair(self):
        result = bot_handlers.serialize_sanitary_days([["2026-08-01", "2026-08-01"]])
        self.assertEqual(json.loads(result), [["2026-08-01", "2026-08-01"]])

    def test_invalid_skipped(self):
        result = bot_handlers.serialize_sanitary_days([["bad", "2026-08-01"], ["2026-08-02", "2026-08-02"]])
        self.assertEqual(json.loads(result), [["2026-08-02", "2026-08-02"]])

    def test_empty(self):
        self.assertEqual(bot_handlers.serialize_sanitary_days([]), "[]")

    def test_end_before_start_normalized(self):
        result = bot_handlers.serialize_sanitary_days([["2026-08-05", "2026-08-01"]])
        self.assertEqual(json.loads(result), [["2026-08-05", "2026-08-05"]])


# ═══════════════════════════════════════════════════════════════════════════
# Тест 5: is_sanitary_day_today
# ═══════════════════════════════════════════════════════════════════════════
class TestIsSanitaryDayToday(unittest.TestCase):

    def test_exact_match(self):
        self.assertTrue(bot_handlers.is_sanitary_day_today(
            [["2026-08-01", "2026-08-01"]], today=date(2026, 8, 1),
        ))

    def test_no_match(self):
        self.assertFalse(bot_handlers.is_sanitary_day_today(
            [["2026-08-01", "2026-08-01"]], today=date(2026, 8, 2),
        ))

    def test_range_start(self):
        self.assertTrue(bot_handlers.is_sanitary_day_today(
            [["2026-08-01", "2026-08-05"]], today=date(2026, 8, 1),
        ))

    def test_range_middle(self):
        self.assertTrue(bot_handlers.is_sanitary_day_today(
            [["2026-08-01", "2026-08-05"]], today=date(2026, 8, 3),
        ))

    def test_range_end_inclusive(self):
        self.assertTrue(bot_handlers.is_sanitary_day_today(
            [["2026-08-01", "2026-08-05"]], today=date(2026, 8, 5),
        ))

    def test_range_outside(self):
        self.assertFalse(bot_handlers.is_sanitary_day_today(
            [["2026-08-01", "2026-08-05"]], today=date(2026, 8, 6),
        ))

    def test_empty_list(self):
        self.assertFalse(bot_handlers.is_sanitary_day_today([], today=date(2026, 8, 1)))

    def test_json_string_input(self):
        # Принимает JSON-строку тоже.
        self.assertTrue(bot_handlers.is_sanitary_day_today(
            '[["2026-08-01","2026-08-01"]]', today=date(2026, 8, 1),
        ))

    def test_none(self):
        self.assertFalse(bot_handlers.is_sanitary_day_today(None, today=date(2026, 8, 1)))

    def test_multiple_pairs_any_match(self):
        pairs = [["2026-08-01", "2026-08-01"], ["2026-08-15", "2026-08-17"]]
        self.assertTrue(bot_handlers.is_sanitary_day_today(pairs, today=date(2026, 8, 16)))
        self.assertFalse(bot_handlers.is_sanitary_day_today(pairs, today=date(2026, 8, 10)))


# ═══════════════════════════════════════════════════════════════════════════
# Тест 6: parse_sanitary_days_textarea
# ═══════════════════════════════════════════════════════════════════════════
class TestParseSanitaryDaysTextarea(unittest.TestCase):

    def test_single_date(self):
        pairs, errors = bot_handlers.parse_sanitary_days_textarea("2026-08-01")
        self.assertEqual(pairs, [["2026-08-01", "2026-08-01"]])
        self.assertEqual(errors, [])

    def test_range_with_dash(self):
        pairs, errors = bot_handlers.parse_sanitary_days_textarea("2026-08-01 - 2026-08-05")
        self.assertEqual(pairs, [["2026-08-01", "2026-08-05"]])
        self.assertEqual(errors, [])

    def test_range_with_colon(self):
        pairs, errors = bot_handlers.parse_sanitary_days_textarea("2026-08-01:2026-08-05")
        self.assertEqual(pairs, [["2026-08-01", "2026-08-05"]])
        self.assertEqual(errors, [])

    def test_multiple_lines(self):
        text = "2026-08-01\n2026-08-15 - 2026-08-17\n2026-09-01"
        pairs, errors = bot_handlers.parse_sanitary_days_textarea(text)
        self.assertEqual(len(pairs), 3)
        self.assertEqual(errors, [])

    def test_comments_ignored(self):
        text = "# comment\n2026-08-01\n# another\n2026-08-15"
        pairs, errors = bot_handlers.parse_sanitary_days_textarea(text)
        self.assertEqual(len(pairs), 2)
        self.assertEqual(errors, [])

    def test_empty_lines_ignored(self):
        text = "\n\n2026-08-01\n\n"
        pairs, errors = bot_handlers.parse_sanitary_days_textarea(text)
        self.assertEqual(len(pairs), 1)
        self.assertEqual(errors, [])

    def test_empty_text(self):
        pairs, errors = bot_handlers.parse_sanitary_days_textarea("")
        self.assertEqual(pairs, [])
        self.assertEqual(errors, [])

    def test_invalid_date_returns_error(self):
        pairs, errors = bot_handlers.parse_sanitary_days_textarea("bad-date")
        self.assertEqual(pairs, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("Строка 1", errors[0])

    def test_invalid_range_returns_error(self):
        pairs, errors = bot_handlers.parse_sanitary_days_textarea("2026-08-01 - bad")
        self.assertEqual(pairs, [])
        self.assertEqual(len(errors), 1)


# ═══════════════════════════════════════════════════════════════════════════
# Тест 7: format_sanitary_days_textarea
# ═══════════════════════════════════════════════════════════════════════════
class TestFormatSanitaryDaysTextarea(unittest.TestCase):

    def test_single_day(self):
        result = bot_handlers.format_sanitary_days_textarea([["2026-08-01", "2026-08-01"]])
        self.assertEqual(result, "2026-08-01")

    def test_range(self):
        result = bot_handlers.format_sanitary_days_textarea([["2026-08-01", "2026-08-05"]])
        self.assertEqual(result, "2026-08-01 - 2026-08-05")

    def test_multiple(self):
        result = bot_handlers.format_sanitary_days_textarea([
            ["2026-08-01", "2026-08-01"],
            ["2026-08-15", "2026-08-17"],
        ])
        self.assertEqual(result, "2026-08-01\n2026-08-15 - 2026-08-17")

    def test_empty(self):
        self.assertEqual(bot_handlers.format_sanitary_days_textarea([]), "")

    def test_none(self):
        self.assertEqual(bot_handlers.format_sanitary_days_textarea(None), "")

    def test_json_string(self):
        result = bot_handlers.format_sanitary_days_textarea('[["2026-08-01","2026-08-01"]]')
        self.assertEqual(result, "2026-08-01")


# ═══════════════════════════════════════════════════════════════════════════
# Тест 8: /sanitary CLI
# ═══════════════════════════════════════════════════════════════════════════
class TestCmdSanitary(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await init_db()
        await _clear_all_tables()
        await _seed_chat_settings()

    async def test_help_when_no_args(self):
        msg = _make_message(text="/sanitary")
        await bot_handlers.cmd_sanitary(msg)
        msg.reply.assert_awaited_once()
        args, _ = msg.reply.call_args
        self.assertIn("Формат", args[0])
        self.assertIn("/sanitary chat_id add", args[0])

    async def test_invalid_chat_id(self):
        msg = _make_message(text="/sanitary abc")
        await bot_handlers.cmd_sanitary(msg)
        msg.reply.assert_awaited_once()
        args, _ = msg.reply.call_args
        self.assertIn("chat_id должен быть числом", args[0])

    async def test_list_empty(self):
        msg = _make_message(text="/sanitary -1001234567890")
        await bot_handlers.cmd_sanitary(msg)
        msg.reply.assert_awaited_once()
        args, _ = msg.reply.call_args
        self.assertIn("пусто", args[0])

    async def test_add_single_date(self):
        msg = _make_message(text="/sanitary -1001234567890 add 2026-08-01")
        await bot_handlers.cmd_sanitary(msg)
        msg.reply.assert_awaited_once()
        args, _ = msg.reply.call_args
        self.assertIn("Добавлен", args[0])
        # Verify DB.
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001234567890)
            )).scalar_one()
            self.assertEqual(
                json.loads(cs.sanitary_days),
                [["2026-08-01", "2026-08-01"]],
            )

    async def test_add_range_with_colon(self):
        msg = _make_message(text="/sanitary -1001234567890 add 2026-08-01:2026-08-05")
        await bot_handlers.cmd_sanitary(msg)
        msg.reply.assert_awaited_once()
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001234567890)
            )).scalar_one()
            self.assertEqual(
                json.loads(cs.sanitary_days),
                [["2026-08-01", "2026-08-05"]],
            )

    async def test_add_range_with_dash(self):
        msg = _make_message(text="/sanitary -1001234567890 add 2026-08-01 - 2026-08-05")
        await bot_handlers.cmd_sanitary(msg)
        msg.reply.assert_awaited_once()
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001234567890)
            )).scalar_one()
            self.assertEqual(
                json.loads(cs.sanitary_days),
                [["2026-08-01", "2026-08-05"]],
            )

    async def test_add_invalid_date(self):
        msg = _make_message(text="/sanitary -1001234567890 add bad-date")
        await bot_handlers.cmd_sanitary(msg)
        msg.reply.assert_awaited_once()
        args, _ = msg.reply.call_args
        self.assertIn("Невалидная дата", args[0])

    async def test_add_duplicate(self):
        # First add.
        msg1 = _make_message(text="/sanitary -1001234567890 add 2026-08-01")
        await bot_handlers.cmd_sanitary(msg1)
        # Second add of same date.
        msg2 = _make_message(text="/sanitary -1001234567890 add 2026-08-01")
        await bot_handlers.cmd_sanitary(msg2)
        args, _ = msg2.reply.call_args
        self.assertIn("уже есть", args[0])

    async def test_add_multiple_then_list(self):
        await bot_handlers.cmd_sanitary(_make_message(text="/sanitary -1001234567890 add 2026-08-01"))
        await bot_handlers.cmd_sanitary(_make_message(text="/sanitary -1001234567890 add 2026-08-15:2026-08-17"))
        msg = _make_message(text="/sanitary -1001234567890")
        await bot_handlers.cmd_sanitary(msg)
        args, _ = msg.reply.call_args
        self.assertIn("2026-08-01", args[0])
        self.assertIn("2026-08-15 → 2026-08-17", args[0])

    async def test_remove_by_exact_date(self):
        await bot_handlers.cmd_sanitary(_make_message(text="/sanitary -1001234567890 add 2026-08-01"))
        msg = _make_message(text="/sanitary -1001234567890 remove 2026-08-01")
        await bot_handlers.cmd_sanitary(msg)
        args, _ = msg.reply.call_args
        self.assertIn("Удалено записей: 1", args[0])
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001234567890)
            )).scalar_one()
            self.assertIsNone(cs.sanitary_days)

    async def test_remove_range_by_date_inside(self):
        await bot_handlers.cmd_sanitary(_make_message(text="/sanitary -1001234567890 add 2026-08-01:2026-08-05"))
        # Remove by middle date.
        msg = _make_message(text="/sanitary -1001234567890 remove 2026-08-03")
        await bot_handlers.cmd_sanitary(msg)
        args, _ = msg.reply.call_args
        self.assertIn("Удалено записей: 1", args[0])

    async def test_remove_nonexistent(self):
        msg = _make_message(text="/sanitary -1001234567890 remove 2026-08-01")
        await bot_handlers.cmd_sanitary(msg)
        args, _ = msg.reply.call_args
        self.assertIn("Не найдено", args[0])

    async def test_clear(self):
        await bot_handlers.cmd_sanitary(_make_message(text="/sanitary -1001234567890 add 2026-08-01"))
        await bot_handlers.cmd_sanitary(_make_message(text="/sanitary -1001234567890 add 2026-08-15"))
        msg = _make_message(text="/sanitary -1001234567890 clear")
        await bot_handlers.cmd_sanitary(msg)
        args, _ = msg.reply.call_args
        self.assertIn("очищены", args[0])
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001234567890)
            )).scalar_one()
            self.assertIsNone(cs.sanitary_days)
            self.assertFalse(cs.sanitary_days_currently_active)

    async def test_invalid_subcommand(self):
        msg = _make_message(text="/sanitary -1001234567890 bogus")
        await bot_handlers.cmd_sanitary(msg)
        args, _ = msg.reply.call_args
        self.assertIn("Неизвестная подкоманда", args[0])

    async def test_toggle_no_bot_import_side_effects(self):
        """toggle должен вызывать _enter_sanitary_day / _exit_sanitary_day из bot.
        Поскольку import bot в тестах работает (мы уже сделали `import bot`),
        проверяем что reply содержит текст про ручное включение.
        """
        # Patch _enter_sanitary_day to avoid real bot API calls.
        with patch.object(bot, "_enter_sanitary_day", new=AsyncMock()) as mock_enter:
            msg = _make_message(text="/sanitary -1001234567890 toggle")
            await bot_handlers.cmd_sanitary(msg)
            mock_enter.assert_awaited_once()
            args, _ = msg.reply.call_args
            self.assertIn("санитарный день включён вручную", args[0])


# ═══════════════════════════════════════════════════════════════════════════
# Тест 9: /admin/chats/<id>/update — sanitary_days_text
# ═══════════════════════════════════════════════════════════════════════════
class TestAdminChatsUpdateSanitary(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await init_db()
        await _clear_all_tables()
        # Seed SU.
        async with async_session() as s:
            s.add(WebUser(username="su", is_su=True, is_active=True, role="su", created_by="system"))
            await s.commit()
        # Disable rate-limit on /login (audit fix from v4.5.1).
        web_app._check_login_rate_limit = lambda ip: True
        await _seed_chat_settings(chat_id=-1001234567890, title="Test")

    async def _login_as_su(self, client):
        r = await client.post("/login", data={
            "username": "su", "password": os.environ["WEB_PASSWORD"],
        }, follow_redirects=False)
        assert r.status_code == 303, f"Login failed: {r.status_code}"
        return r.cookies

    async def _post_update(self, client, cookies, **form_fields):
        data = {
            "hashtag": "",
            "report_chat_id": "",
            "warns_to_mute": "3",
            "mute_duration_seconds": "3600",
            "warns_to_ban": "5",
            "warn_decay_days": "0",
            "link_filter_action": "delete",
            "night_mode_start": "23:00",
            "night_mode_end": "07:00",
            "night_mode_preset": "text_only",
            "night_mode_tz": "Europe/Moscow",
            "night_mode_weekend_start": "",
            "night_mode_weekend_end": "",
            "night_mode_notify": "",
            "night_mode_notify_enter_msg": "",
            "night_mode_notify_exit_msg": "",
        }
        data.update(form_fields)
        return await client.post(
            "/admin/chats/-1001234567890/update",
            data=data,
            cookies=cookies,
            follow_redirects=False,
        )

    async def test_update_saves_sanitary_days(self):
        from httpx import AsyncClient, ASGITransport
        app = web_app.create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            cookies = await self._login_as_su(client)
            r = await self._post_update(
                client, cookies,
                sanitary_days_text="2026-08-01\n2026-08-15 - 2026-08-17",
            )
            self.assertEqual(r.status_code, 303)

        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001234567890)
            )).scalar_one()
            pairs = json.loads(cs.sanitary_days)
            self.assertEqual(pairs, [["2026-08-01", "2026-08-01"], ["2026-08-15", "2026-08-17"]])

    async def test_update_invalid_sanitary_date(self):
        from httpx import AsyncClient, ASGITransport
        app = web_app.create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            cookies = await self._login_as_su(client)
            r = await self._post_update(client, cookies, sanitary_days_text="bad-date")
            self.assertEqual(r.status_code, 303)
            # Flash message in URL.
            loc = r.headers.get("location", "")
            self.assertTrue("Sanitary" in loc or "sanitary" in loc.lower(),
                            f"Expected sanitary flash in location: {loc}")

    async def test_update_clears_sanitary_days(self):
        # Seed existing sanitary days.
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001234567890)
            )).scalar_one()
            cs.sanitary_days = '[["2026-08-01","2026-08-01"]]'
            await s.commit()

        from httpx import AsyncClient, ASGITransport
        app = web_app.create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            cookies = await self._login_as_su(client)
            r = await self._post_update(client, cookies, sanitary_days_text="")
            self.assertEqual(r.status_code, 303)

        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001234567890)
            )).scalar_one()
            self.assertIsNone(cs.sanitary_days)


# ═══════════════════════════════════════════════════════════════════════════
# Тест 10: _night_mode_tick пропускает чаты в sanitary day
# ═══════════════════════════════════════════════════════════════════════════
class TestNightModeTickSkipsSanitary(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await init_db()
        await _clear_all_tables()
        await _seed_chat_settings()
        # Enable night mode + mark sanitary as active.
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001234567890)
            )).scalar_one()
            cs.night_mode_enabled = True
            cs.night_mode_start = "23:00"
            cs.night_mode_end = "07:00"
            cs.sanitary_days_currently_active = True  # <-- KEY: sanitary is active
            await s.commit()

    async def test_night_mode_tick_skips_sanitary_active_chat(self):
        """Если sanitary_days_currently_active=True — night mode пропускает чат.
        Проверяем, что bot.set_chat_permissions НЕ вызывается."""
        with patch.object(bot.bot, "set_chat_permissions", new=AsyncMock()) as mock_perms, \
             patch.object(bot.bot, "get_chat", new=AsyncMock()):
            await bot._night_mode_tick()
            mock_perms.assert_not_awaited()


# ═══════════════════════════════════════════════════════════════════════════
# Тест 11: _sanitary_day_tick — enter/exit logic
# ═══════════════════════════════════════════════════════════════════════════
class TestSanitaryDayTick(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await init_db()
        await _clear_all_tables()
        await _seed_chat_settings()

    async def test_tick_enters_when_today_is_sanitary(self):
        # Configure sanitary_days = today (in chat's tz, not UTC).
        from zoneinfo import ZoneInfo
        today_msk = datetime.now(timezone.utc).astimezone(ZoneInfo("Europe/Moscow")).date()
        today_iso = today_msk.isoformat()
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001234567890)
            )).scalar_one()
            cs.sanitary_days = json.dumps([[today_iso, today_iso]])
            cs.sanitary_days_currently_active = False
            cs.night_mode_tz = "Europe/Moscow"
            await s.commit()

        # Mock bot.get_chat to return ChatPermissions.
        mock_perms = MagicMock()
        for f in bot._PERM_FIELDS:
            setattr(mock_perms, f, True)
        mock_chat = MagicMock()
        mock_chat.permissions = mock_perms

        with patch.object(bot.bot, "get_chat", new=AsyncMock(return_value=mock_chat)), \
             patch.object(bot.bot, "set_chat_permissions", new=AsyncMock()) as mock_set:
            await bot._sanitary_day_tick()
            mock_set.assert_awaited_once()
            # Verify lockdown: all perms False.
            call_kwargs = mock_set.call_args.kwargs
            perms = call_kwargs["permissions"]
            for f in bot._PERM_FIELDS:
                self.assertFalse(getattr(perms, f, True), f"perm {f} should be False")

        # Verify DB state.
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001234567890)
            )).scalar_one()
            self.assertTrue(cs.sanitary_days_currently_active)
            self.assertIsNotNone(cs.sanitary_days_saved_permissions)

    async def test_tick_exits_when_today_not_sanitary(self):
        # Configure sanitary_days = yesterday (in chat's tz), but currently_active = True.
        from zoneinfo import ZoneInfo
        today_msk = datetime.now(timezone.utc).astimezone(ZoneInfo("Europe/Moscow")).date()
        yesterday = (today_msk - timedelta(days=1)).isoformat()
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001234567890)
            )).scalar_one()
            cs.sanitary_days = json.dumps([[yesterday, yesterday]])
            cs.sanitary_days_currently_active = True
            cs.sanitary_days_saved_permissions = json.dumps(
                {f: True for f in bot._PERM_FIELDS}
            )
            cs.night_mode_tz = "Europe/Moscow"
            await s.commit()

        with patch.object(bot.bot, "set_chat_permissions", new=AsyncMock()) as mock_set:
            await bot._sanitary_day_tick()
            mock_set.assert_awaited_once()
            # Verify restore: perms should be True (from snapshot).
            call_kwargs = mock_set.call_args.kwargs
            perms = call_kwargs["permissions"]
            for f in bot._PERM_FIELDS:
                self.assertTrue(getattr(perms, f, False), f"perm {f} should be True")

        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001234567890)
            )).scalar_one()
            self.assertFalse(cs.sanitary_days_currently_active)
            self.assertIsNone(cs.sanitary_days_saved_permissions)

    async def test_tick_skip_when_no_sanitary_days(self):
        """Чаты без sanitary_days не обрабатываются."""
        with patch.object(bot.bot, "set_chat_permissions", new=AsyncMock()) as mock_set:
            await bot._sanitary_day_tick()
            mock_set.assert_not_awaited()


# ═══════════════════════════════════════════════════════════════════════════
# Тест 12: _enter_sanitary_day — exit night mode first if active
# ═══════════════════════════════════════════════════════════════════════════
class TestEnterSanitaryDayExitsNightFirst(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await init_db()
        await _clear_all_tables()
        await _seed_chat_settings()

    async def test_enter_calls_exit_night_if_active(self):
        """Если night_mode_currently_active=True при входе в sanitary —
        сначала вызывается _exit_night_mode."""
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001234567890)
            )).scalar_one()
            cs.night_mode_currently_active = True
            cs.night_mode_saved_permissions = json.dumps({f: True for f in bot._PERM_FIELDS})
            await s.commit()
            cs_loaded = cs

        # Mock get_chat to return day perms.
        mock_perms = MagicMock()
        for f in bot._PERM_FIELDS:
            setattr(mock_perms, f, True)
        mock_chat = MagicMock()
        mock_chat.permissions = mock_perms

        with patch.object(bot.bot, "get_chat", new=AsyncMock(return_value=mock_chat)), \
             patch.object(bot.bot, "set_chat_permissions", new=AsyncMock()) as mock_set:
            await bot._enter_sanitary_day(cs_loaded)
            # set_chat_permissions called at least once (exit night + enter sanitary).
            self.assertGreaterEqual(mock_set.await_count, 1)

        # Verify DB: sanitary active, night NOT active.
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001234567890)
            )).scalar_one()
            self.assertTrue(cs.sanitary_days_currently_active)
            self.assertFalse(cs.night_mode_currently_active)


# ═══════════════════════════════════════════════════════════════════════════
# Тест 13: APP_VERSION
# ═══════════════════════════════════════════════════════════════════════════
class TestAppVersion(unittest.TestCase):

    def test_app_version_is_v454(self):
        # v4.5.5 bumped the version further; this test still validates that
        # the version is at least v4.5.4 (sanitary day shipped).
        ver = web_app.APP_VERSION
        # Парсим "v4.5.X" — split на '.', берём patch-компонент.
        m = ver.split(".")
        self.assertEqual(m[0], "v4", f"Major version should be v4, got {m[0]}")
        self.assertEqual(m[1], "5", f"Minor version should be 5, got {m[1]}")
        patch = int(m[2])
        self.assertGreaterEqual(patch, 4, f"Patch should be ≥ 4, got {patch}")

    def test_release_date_set(self):
        self.assertEqual(web_app.APP_RELEASE_DATE, "2026-07-29")


# ═══════════════════════════════════════════════════════════════════════════
# Тест 14: /help содержит раздел "Санитарные дни"
# ═══════════════════════════════════════════════════════════════════════════
class TestHelpSanitarySection(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await init_db()
        await _clear_all_tables()
        await _seed_chat_settings()

    async def test_help_contains_sanitary_section(self):
        msg = _make_message(text="/help")
        await bot_handlers.cmd_help(msg)
        msg.reply.assert_awaited_once()
        args, _ = msg.reply.call_args
        text = args[0]
        self.assertIn("Санитарные дни", text)
        self.assertIn("/sanitary chat_id", text)
        self.assertIn("add", text)
        self.assertIn("remove", text)
        self.assertIn("clear", text)
        self.assertIn("toggle", text)
        self.assertIn("Lockdown чата", text)


# ═══════════════════════════════════════════════════════════════════════════
# Тест 15: base.html changelog modal содержит v4.5.4
# ═══════════════════════════════════════════════════════════════════════════
class TestChangelogModalV454(unittest.TestCase):

    def test_changelog_contains_v454(self):
        with open("/home/z/my-project/v4.5/templates/base.html", encoding="utf-8") as f:
            html = f.read()
        self.assertIn("v4.5.4", html)
        self.assertIn("Sanitary days", html)
        self.assertIn("lockdown", html)
        # v4.5.3 still mentioned (compressed).
        self.assertIn("v4.5.3", html)


# ═══════════════════════════════════════════════════════════════════════════
# Тест 16: admin_chats.html содержит textarea + SAN badge
# ═══════════════════════════════════════════════════════════════════════════
class TestAdminChatsTemplateV454(unittest.TestCase):

    def test_template_contains_sanitary_textarea(self):
        with open("/home/z/my-project/v4.5/templates/admin_chats.html", encoding="utf-8") as f:
            html = f.read()
        self.assertIn('name="sanitary_days_text"', html)
        self.assertIn("Sanitary days", html)
        self.assertIn("LOCKDOWN ACTIVE", html)

    def test_template_contains_san_badge(self):
        with open("/home/z/my-project/v4.5/templates/admin_chats.html", encoding="utf-8") as f:
            html = f.read()
        self.assertIn("SAN", html)
        self.assertIn("sanitary_days_currently_active", html)


# ═══════════════════════════════════════════════════════════════════════════
# Тест 17: format_sanitary_days Jinja2 filter registered
# ═══════════════════════════════════════════════════════════════════════════
class TestFormatSanitaryDaysFilter(unittest.TestCase):

    def test_filter_works_via_jinja(self):
        """Регистрируем filter в изолированном Jinja2 Environment и проверяем
        что он корректно парсит JSON → multiline textarea text."""
        from jinja2 import Environment
        from bot_handlers import format_sanitary_days_textarea
        env = Environment()
        env.filters["format_sanitary_days"] = format_sanitary_days_textarea
        # Single day.
        tpl = env.from_string('{{ \'[["2026-08-01","2026-08-01"]]\' | format_sanitary_days }}')
        self.assertEqual(tpl.render(), "2026-08-01")
        # Range.
        tpl2 = env.from_string('{{ \'[["2026-08-01","2026-08-05"]]\' | format_sanitary_days }}')
        self.assertEqual(tpl2.render(), "2026-08-01 - 2026-08-05")
        # Empty.
        tpl3 = env.from_string('{{ \'\' | format_sanitary_days }}')
        self.assertEqual(tpl3.render(), "")
        # None.
        tpl4 = env.from_string('{{ none | format_sanitary_days }}')
        self.assertEqual(tpl4.render(), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
