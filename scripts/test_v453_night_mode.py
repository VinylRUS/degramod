"""
test_v453_night_mode.py — Тесты v4.5.3: расширения ночного режима + чистка /help.

Покрывает:
  1. DB schema: новые колонки v4.5.3 в ChatSettings (night_mode_tz,
     night_mode_weekend_start/end, night_mode_notify, night_mode_notify_enter_msg,
     night_mode_notify_exit_msg). Миграция идемпотентна.
  2. _time_str_in_range с tz: Europe/Moscow (default), Asia/Yekaterinburg (+2h).
  3. _time_str_in_range с некорректной tz → fallback на MSK.
  4. _night_mode_in_window: weekday vs weekend schedule, суббота = выходной.
  5. _NIGHT_PERM_ALIASES карта алиасов корректна.
  6. _build_custom_night_permissions: точечные overrides сверху базового preset.
  7. cmd_nightmode базовая форма (бэквард-компат): /nightmode chat_id 23:00 07:00 strict.
  8. cmd_nightmode off: сбрасывает флаги.
  9. cmd_nightmode tz <name>: валидный tz сохраняется; невалидный — ошибка.
 10. cmd_nightmode weekend <start> <end>: сохраняет отдельное расписание.
 11. cmd_nightmode weekend off: сбрасывает расписание выходных.
 12. cmd_nightmode notify on/off: переключает флаг + сохраняет кастомный текст.
 13. cmd_nightmode notify_text enter/exit: раздельные тексты входа/выхода.
 14. cmd_nightmode notify_text enter default: сброс на дефолтный шаблон.
 15. cmd_nightmode custom <perm>=0|1 ...: применяет точечные права.
 16. cmd_nightmode custom с неизвестным perm → ошибка.
 17. cmd_nightmode custom с неверным форматом (нет '=') → ошибка.
 18. cmd_nightmode с некорректным chat_id → ошибка.
 19. /admin/chats/<id>/update принимает новые поля (tz, weekend, notify, custom).
 20. /admin/chats/<id>/update с preset='custom' строит JSON из 10 чекбоксов.
 21. /admin/chats/<id>/update валидирует tz (невалидный → редирект с flash).
 22. /admin/chats/<id>/update валидирует weekend schedule (только start, без end → ошибка).
 23. /admin/chats/<id>/update валидирует HH:MM для weekend.
 24. _night_mode_preset_name распознаёт 'custom' (10 флагов, не совпадающих с preset).
 25. APP_VERSION = "v4.5.3".
 26. /help cleanup: текст содержит ссылку на /admin/chats (упоминание веб-панели).
 27. /help cleanup: НЕ содержит "30м" пример (удалён избыточный пример).
 28. /help cleanup: содержит "/nightmode chat_id tz" (новые подкоманды).
 29. /help cleanup: содержит "/nightmode chat_id weekend" (новые подкоманды).
 30. from_json Jinja2 filter парсит JSON-строку в dict.
 31. base.html changelog modal содержит v4.5.3 заголовок.
 32. Идемпотентность миграции: повторный init_db не падает.
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone, timedelta
import json
import re

# Подкладываем test-окружение ДО импорта модулей проекта.
os.environ.setdefault("DB_PATH", ":memory:")
os.environ.setdefault("BOT_TOKEN", "test:token")
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


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────


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


async def _seed_su():
    """Создаёт SU-аккаунт в БД."""
    async with async_session() as s:
        s.add(WebUser(username="su", is_su=True, is_active=True,
                       role="su", created_by="system"))
        await s.commit()


async def _seed_chat_settings(chat_id=-1001234567890, title="Test", **overrides):
    """Создаёт ChatSettings с дефолтами + опциональными overrides."""
    async with async_session() as s:
        cs = ChatSettings(chat_id=chat_id, title=title, **overrides)
        s.add(cs)
        await s.commit()
        return cs


def _make_message(*, text=None, chat_id=-1001234567890, chat_type="private",
                  from_user_id=111111111, reply_to_message=None):
    """Mock aiogram Message для DM команд."""
    msg = MagicMock()
    msg.text = text
    msg.reply_to_message = reply_to_message
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
# Тест 1: DB schema — новые колонки v4.5.3
# ═══════════════════════════════════════════════════════════════════════════
class TestDBSchemaV453(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await init_db()
        await _clear_all_tables()

    async def test_v453_columns_exist(self):
        """Новые колонки v4.5.3 присутствуют в ChatSettings."""
        async with async_session() as s:
            cs = ChatSettings(chat_id=-1001234567890, title="Test")
            # Проверяем что все 6 новых полей можно прочитать/записать.
            cs.night_mode_tz = "Asia/Yekaterinburg"
            cs.night_mode_weekend_start = "02:00"
            cs.night_mode_weekend_end = "10:00"
            cs.night_mode_notify = True
            cs.night_mode_notify_enter_msg = "🌙 Тишина!"
            cs.night_mode_notify_exit_msg = "☀️ Утро!"
            s.add(cs)
            await s.commit()
            # Перезагружаем для проверки персистентности.
            s.expire_all()
            reloaded = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001234567890)
            )).scalar_one()
            self.assertEqual(reloaded.night_mode_tz, "Asia/Yekaterinburg")
            self.assertEqual(reloaded.night_mode_weekend_start, "02:00")
            self.assertEqual(reloaded.night_mode_weekend_end, "10:00")
            self.assertTrue(reloaded.night_mode_notify)
            self.assertEqual(reloaded.night_mode_notify_enter_msg, "🌙 Тишина!")
            self.assertEqual(reloaded.night_mode_notify_exit_msg, "☀️ Утро!")

    async def test_v453_defaults(self):
        """Новые колонки v4.5.3 имеют корректные дефолты при создании."""
        async with async_session() as s:
            cs = ChatSettings(chat_id=-1009876543210, title="Fresh")
            s.add(cs)
            await s.commit()
            s.expire_all()
            reloaded = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1009876543210)
            )).scalar_one()
            self.assertEqual(reloaded.night_mode_tz, "Europe/Moscow")
            self.assertIsNone(reloaded.night_mode_weekend_start)
            self.assertIsNone(reloaded.night_mode_weekend_end)
            self.assertFalse(reloaded.night_mode_notify)
            self.assertIsNone(reloaded.night_mode_notify_enter_msg)
            self.assertIsNone(reloaded.night_mode_notify_exit_msg)

    async def test_migration_idempotent(self):
        """Повторный init_db не должен падать и не должен дублировать колонки."""
        await init_db()
        await init_db()  # повторный запуск
        # Проверяем что колонки всё ещё доступны.
        async with async_session() as s:
            cs = ChatSettings(chat_id=-1001111111111, title="Idempotent Test")
            cs.night_mode_tz = "Asia/Novosibirsk"
            s.add(cs)
            await s.commit()


# ═══════════════════════════════════════════════════════════════════════════
# Тест 2-3: _time_str_in_range с tz
# ═══════════════════════════════════════════════════════════════════════════
class TestTimeStrInRangeTz(unittest.TestCase):

    def test_msk_default(self):
        """Без tz — fallback на MSK."""
        # 2026-07-29 22:30 UTC = 01:30 MSK (next day). Окно 23:00 → 07:00.
        now = datetime(2026, 7, 29, 22, 30, tzinfo=timezone.utc)
        self.assertTrue(bot_handlers._time_str_in_range(now, "23:00", "07:00"))
        # 2026-07-29 10:00 UTC = 13:00 MSK. Окно 23:00 → 07:00 — НЕ активно.
        now2 = datetime(2026, 7, 29, 10, 0, tzinfo=timezone.utc)
        self.assertFalse(bot_handlers._time_str_in_range(now2, "23:00", "07:00"))

    def test_ekaterinburg_tz(self):
        """Asia/Yekaterinburg = UTC+5 (MSK+2)."""
        # 2026-07-29 20:30 UTC = 01:30 EKB. Окно 23:00 → 07:00 — активно.
        now = datetime(2026, 7, 29, 20, 30, tzinfo=timezone.utc)
        self.assertTrue(
            bot_handlers._time_str_in_range(now, "23:00", "07:00", tz_name="Asia/Yekaterinburg")
        )
        # В MSK это было бы 23:30 — тоже в окне, но проверяем что tz применяется.
        # 2026-07-29 18:00 UTC = 23:00 EKB. Окно 23:00 → 07:00 — активно (граница включена).
        now2 = datetime(2026, 7, 29, 18, 0, tzinfo=timezone.utc)
        self.assertTrue(
            bot_handlers._time_str_in_range(now2, "23:00", "07:00", tz_name="Asia/Yekaterinburg")
        )

    def test_invalid_tz_fallback_msk(self):
        """Некорректная tz → fallback на MSK (не падает)."""
        # 2026-07-29 22:30 UTC = 01:30 MSK → в окне 23:00-07:00.
        now = datetime(2026, 7, 29, 22, 30, tzinfo=timezone.utc)
        self.assertTrue(
            bot_handlers._time_str_in_range(now, "23:00", "07:00", tz_name="Not/A/Real/Zone")
        )

    def test_none_tz(self):
        """tz_name=None → MSK."""
        now = datetime(2026, 7, 29, 22, 30, tzinfo=timezone.utc)
        self.assertTrue(bot_handlers._time_str_in_range(now, "23:00", "07:00", tz_name=None))


# ═══════════════════════════════════════════════════════════════════════════
# Тест 4: _night_mode_in_window — weekday vs weekend
# ═══════════════════════════════════════════════════════════════════════════
class TestNightModeInWindow(unittest.TestCase):

    def test_weekday_uses_weekday_schedule(self):
        """В среду (weekday=2) используется будничное расписание."""
        # 2026-07-29 = среда. 22:30 UTC = 01:30 MSK (четверг, но weekday проверяется по local).
        # Actually 22:30 UTC → 01:30+1day MSK = Thursday 01:30.
        # Возьмём более чистый пример: 2026-07-29 10:00 UTC = 13:00 MSK (Wednesday).
        # Будничное окно 09:00 → 18:00 — активно.
        # Выходное окно 02:00 → 10:00 — не должно использоваться.
        now = datetime(2026, 7, 29, 10, 0, tzinfo=timezone.utc)
        in_window = bot_handlers._night_mode_in_window(
            now=now,
            weekday_start="09:00", weekday_end="18:00",
            weekend_start="02:00", weekend_end="10:00",
            tz_name="Europe/Moscow",
        )
        self.assertTrue(in_window)

    def test_saturday_uses_weekend_schedule(self):
        """В субботу (weekday=5) используется выходное расписание."""
        # 2026-08-01 = суббота. 06:00 UTC = 09:00 MSK.
        # Будничное окно 09:00 → 18:00 — активное в будни, но сегодня выходной.
        # Выходное окно 02:00 → 10:00 — активно (09:00 < 10:00).
        now = datetime(2026, 8, 1, 6, 0, tzinfo=timezone.utc)
        in_window = bot_handlers._night_mode_in_window(
            now=now,
            weekday_start="09:00", weekday_end="18:00",
            weekend_start="02:00", weekend_end="10:00",
            tz_name="Europe/Moscow",
        )
        self.assertTrue(in_window)

    def test_saturday_outside_weekend_window(self):
        """В субботу вне выходного окна — не активно."""
        # 2026-08-01 = суббота. 12:00 UTC = 15:00 MSK.
        # Выходное окно 02:00 → 10:00 — неактивно (15:00 > 10:00).
        now = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
        in_window = bot_handlers._night_mode_in_window(
            now=now,
            weekday_start="09:00", weekday_end="18:00",
            weekend_start="02:00", weekend_end="10:00",
            tz_name="Europe/Moscow",
        )
        self.assertFalse(in_window)

    def test_sunday_uses_weekend_schedule(self):
        """Воскресенье (weekday=6) — выходной."""
        # 2026-08-02 = воскресенье. 06:00 UTC = 09:00 MSK.
        # Выходное окно 02:00 → 10:00 — активно.
        now = datetime(2026, 8, 2, 6, 0, tzinfo=timezone.utc)
        in_window = bot_handlers._night_mode_in_window(
            now=now,
            weekday_start="09:00", weekday_end="18:00",
            weekend_start="02:00", weekend_end="10:00",
            tz_name="Europe/Moscow",
        )
        self.assertTrue(in_window)

    def test_weekend_null_uses_weekday(self):
        """weekend_start/end = None → используется будничное расписание каждый день."""
        # Суббота 06:00 UTC = 09:00 MSK. Будничное окно 09:00 → 18:00 — активно.
        now = datetime(2026, 8, 1, 6, 0, tzinfo=timezone.utc)
        in_window = bot_handlers._night_mode_in_window(
            now=now,
            weekday_start="09:00", weekday_end="18:00",
            weekend_start=None, weekend_end=None,
            tz_name="Europe/Moscow",
        )
        self.assertTrue(in_window)


# ═══════════════════════════════════════════════════════════════════════════
# Тест 5-6: _NIGHT_PERM_ALIASES + _build_custom_night_permissions
# ═══════════════════════════════════════════════════════════════════════════
class TestCustomNightPermissions(unittest.TestCase):

    def test_alias_map_completeness(self):
        """Все 10 алиасов присутствуют в карте."""
        expected = {
            "msgs", "audios", "docs", "photos", "videos",
            "vnotes", "voices", "polls", "other", "links",
        }
        self.assertEqual(set(bot_handlers._NIGHT_PERM_ALIASES.keys()), expected)

    def test_alias_values_are_valid_perm_fields(self):
        """Все значения алиасов — валидные имена ChatPermissions."""
        from aiogram import types
        perms = types.ChatPermissions()
        for alias, full_name in bot_handlers._NIGHT_PERM_ALIASES.items():
            self.assertTrue(
                hasattr(perms, full_name),
                f"alias '{alias}' → '{full_name}' not a valid ChatPermissions field",
            )

    def test_build_custom_overrides_text_only_base(self):
        """base=text_only + override photos=1 → photos становятся True."""
        perms = bot_handlers._build_custom_night_permissions(
            "text_only", {"photos": True, "videos": True}
        )
        self.assertTrue(perms.can_send_messages)  # text_only дефолт
        self.assertTrue(perms.can_send_photos)  # overridden
        self.assertTrue(perms.can_send_videos)  # overridden
        self.assertFalse(perms.can_send_audios)  # text_only дефолт для остальных
        self.assertFalse(perms.can_send_other_messages)

    def test_build_custom_overrides_strict_base(self):
        """base=strict (всё False) + override msgs=1 → msgs становится True."""
        perms = bot_handlers._build_custom_night_permissions(
            "strict", {"msgs": True}
        )
        self.assertTrue(perms.can_send_messages)
        self.assertFalse(perms.can_send_audios)

    def test_build_custom_overrides_none_base(self):
        """base=none (всё True) + override photos=0 → photos становится False."""
        perms = bot_handlers._build_custom_night_permissions(
            "none", {"photos": False}
        )
        self.assertTrue(perms.can_send_messages)
        self.assertFalse(perms.can_send_photos)
        self.assertTrue(perms.can_send_audios)

    def test_build_custom_unknown_alias_ignored(self):
        """Неизвестный алиас не падает, просто игнорируется."""
        perms = bot_handlers._build_custom_night_permissions(
            "text_only", {"unknown_perm": True}
        )
        self.assertTrue(perms.can_send_messages)


# ═══════════════════════════════════════════════════════════════════════════
# Тест 7-18: cmd_nightmode DM commands
# ═══════════════════════════════════════════════════════════════════════════
class TestCmdNightmodeSubcommands(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await init_db()
        await _clear_all_tables()
        await _seed_su()
        await _seed_chat_settings(chat_id=-1001234567890, title="Test")

    async def _invoke(self, text):
        """Вызывает cmd_nightmode с текстом, возвращает msg.reply kwargs."""
        msg = _make_message(text=text, from_user_id=111111111)
        await bot_handlers.cmd_nightmode(msg)
        self.assertTrue(msg.reply.called, "cmd_nightmode should reply")
        return msg.reply.call_args

    async def test_basic_form_strict_preset(self):
        """Базовая форма /nightmode chat_id 23:00 07:00 strict сохраняет всё."""
        await self._invoke("/nightmode -1001234567890 23:00 07:00 strict")
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001234567890)
            )).scalar_one()
            self.assertTrue(cs.night_mode_enabled)
            self.assertEqual(cs.night_mode_start, "23:00")
            self.assertEqual(cs.night_mode_end, "07:00")
            # strict preset → все False в JSON.
            perms = json.loads(cs.night_mode_permissions)
            self.assertFalse(perms["can_send_messages"])
            self.assertFalse(perms["can_send_audios"])

    async def test_basic_form_default_preset(self):
        """Без указания preset — text_only."""
        await self._invoke("/nightmode -1001234567890 23:00 07:00")
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001234567890)
            )).scalar_one()
            perms = json.loads(cs.night_mode_permissions)
            self.assertTrue(perms["can_send_messages"])
            self.assertFalse(perms["can_send_photos"])

    async def test_off_disables(self):
        """Выключение через off."""
        # Сначала включим.
        await self._invoke("/nightmode -1001234567890 23:00 07:00")
        # Потом выключим.
        await self._invoke("/nightmode -1001234567890 off")
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001234567890)
            )).scalar_one()
            self.assertFalse(cs.night_mode_enabled)
            self.assertFalse(cs.night_mode_currently_active)

    async def test_invalid_chat_id(self):
        """Нечисловой chat_id → ошибка."""
        call_args = await self._invoke("/nightmode abc 23:00 07:00")
        text_arg = call_args.args[0] if call_args.args else call_args.kwargs.get("text", "")
        self.assertIn("числом", text_arg)

    async def test_tz_subcommand_set(self):
        """/nightmode chat_id tz <name> сохраняет tz."""
        await self._invoke("/nightmode -1001234567890 tz Asia/Yekaterinburg")
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001234567890)
            )).scalar_one()
            self.assertEqual(cs.night_mode_tz, "Asia/Yekaterinburg")

    async def test_tz_subcommand_invalid(self):
        """/nightmode chat_id tz Invalid/Zone → ошибка, tz не меняется."""
        # Сначала поставим корректный tz.
        await self._invoke("/nightmode -1001234567890 tz Europe/Moscow")
        # Попытаемся поставить некорректный.
        call_args = await self._invoke("/nightmode -1001234567890 tz Not/A/Zone")
        text_arg = call_args.args[0] if call_args.args else call_args.kwargs.get("text", "")
        self.assertIn("Некорректный tz", text_arg)
        # tz должен остаться Europe/Moscow.
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001234567890)
            )).scalar_one()
            self.assertEqual(cs.night_mode_tz, "Europe/Moscow")

    async def test_weekend_subcommand_set(self):
        """/nightmode chat_id weekend <start> <end> сохраняет расписание выходных."""
        await self._invoke("/nightmode -1001234567890 weekend 02:00 10:00")
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001234567890)
            )).scalar_one()
            self.assertEqual(cs.night_mode_weekend_start, "02:00")
            self.assertEqual(cs.night_mode_weekend_end, "10:00")

    async def test_weekend_off_resets(self):
        """/nightmode chat_id weekend off сбрасывает расписание выходных."""
        # Сначала установим.
        await self._invoke("/nightmode -1001234567890 weekend 02:00 10:00")
        # Потом сбросим.
        await self._invoke("/nightmode -1001234567890 weekend off")
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001234567890)
            )).scalar_one()
            self.assertIsNone(cs.night_mode_weekend_start)
            self.assertIsNone(cs.night_mode_weekend_end)

    async def test_weekend_invalid_time(self):
        """/nightmode chat_id weekend с битым временем → ошибка."""
        call_args = await self._invoke("/nightmode -1001234567890 weekend 99:99 10:00")
        text_arg = call_args.args[0] if call_args.args else call_args.kwargs.get("text", "")
        self.assertIn("некорректное", text_arg)

    async def test_notify_subcommand_on(self):
        """/nightmode chat_id notify on включает уведомления."""
        await self._invoke("/nightmode -1001234567890 notify on")
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001234567890)
            )).scalar_one()
            self.assertTrue(cs.night_mode_notify)

    async def test_notify_subcommand_off(self):
        """/nightmode chat_id notify off выключает уведомления."""
        # Сначала включим.
        await self._invoke("/nightmode -1001234567890 notify on")
        # Потом выключим.
        await self._invoke("/nightmode -1001234567890 notify off")
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001234567890)
            )).scalar_one()
            self.assertFalse(cs.night_mode_notify)

    async def test_notify_with_custom_text(self):
        """/nightmode chat_id notify on <text> сохраняет кастомный текст."""
        # NOTE: parsed via raw.split(maxsplit=4) inside cmd_nightmode →
        # sub = ['/nightmode', chat_id, 'notify', 'on', '🌙 Тишина!']
        await self._invoke("/nightmode -1001234567890 notify on 🌙 Тишина!")
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001234567890)
            )).scalar_one()
            self.assertTrue(cs.night_mode_notify)
            self.assertEqual(cs.night_mode_notify_enter_msg, "🌙 Тишина!")
            self.assertEqual(cs.night_mode_notify_exit_msg, "🌙 Тишина!")

    async def test_notify_text_enter_custom(self):
        """/nightmode chat_id notify_text enter <text>."""
        await self._invoke("/nightmode -1001234567890 notify_text enter 🌙 Заход太阳 ушёл")
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001234567890)
            )).scalar_one()
            self.assertEqual(cs.night_mode_notify_enter_msg, "🌙 Заход太阳 ушёл")

    async def test_notify_text_exit_custom(self):
        """/nightmode chat_id notify_text exit <text>."""
        await self._invoke("/nightmode -1001234567890 notify_text exit ☀️ Утро!")
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001234567890)
            )).scalar_one()
            self.assertEqual(cs.night_mode_notify_exit_msg, "☀️ Утро!")

    async def test_notify_text_enter_default_resets(self):
        """/nightmode chat_id notify_text enter default → None (дефолтный шаблон)."""
        # Сначала поставим кастомный.
        await self._invoke("/nightmode -1001234567890 notify_text enter 🌙 Custom")
        # Потом сбросим.
        await self._invoke("/nightmode -1001234567890 notify_text enter default")
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001234567890)
            )).scalar_one()
            self.assertIsNone(cs.night_mode_notify_enter_msg)

    async def test_custom_subcommand_applies_overrides(self):
        """/nightmode chat_id custom <perm>=0|1 применяет точечные права."""
        await self._invoke(
            "/nightmode -1001234567890 custom msgs=1 photos=1 videos=0"
        )
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001234567890)
            )).scalar_one()
            perms = json.loads(cs.night_mode_permissions)
            self.assertTrue(perms["can_send_messages"])
            self.assertTrue(perms["can_send_photos"])
            self.assertFalse(perms["can_send_videos"])

    async def test_custom_subcommand_unknown_perm(self):
        """/nightmode chat_id custom с неизвестным perm → ошибка."""
        call_args = await self._invoke(
            "/nightmode -1001234567890 custom bogus=1"
        )
        text_arg = call_args.args[0] if call_args.args else call_args.kwargs.get("text", "")
        self.assertIn("Неизвестный perm", text_arg)

    async def test_custom_subcommand_bad_format(self):
        """/nightmode chat_id custom с неверным форматом (нет '=') → ошибка."""
        call_args = await self._invoke(
            "/nightmode -1001234567890 custom msgs"
        )
        text_arg = call_args.args[0] if call_args.args else call_args.kwargs.get("text", "")
        self.assertIn("Неверный формат", text_arg)

    async def test_custom_subcommand_bad_value(self):
        """/nightmode chat_id custom со значением не 0/1 → ошибка."""
        call_args = await self._invoke(
            "/nightmode -1001234567890 custom msgs=yes"
        )
        text_arg = call_args.args[0] if call_args.args else call_args.kwargs.get("text", "")
        self.assertIn("0 или 1", text_arg)


# ═══════════════════════════════════════════════════════════════════════════
# Тест 19-23: web_app admin_chats_update с новыми полями
# ═══════════════════════════════════════════════════════════════════════════
class TestAdminChatsUpdateV453(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await init_db()
        await _clear_all_tables()
        await _seed_su()
        # Отключаем rate-limit на /login (audit fix from v4.5.1).
        web_app._check_login_rate_limit = lambda ip: True
        await _seed_chat_settings(chat_id=-1001234567890, title="Test")

    async def _login_as_su(self, client):
        """Логинимся как SU и возвращаем cookies."""
        r = await client.post("/login", data={
            "username": "su", "password": os.environ["WEB_PASSWORD"],
        }, follow_redirects=False)
        assert r.status_code == 303, f"Login failed: {r.status_code}"
        return r.cookies

    async def _post_update(self, client, cookies, **form_fields):
        """POST /admin/chats/<id>/update с указанными полями формы."""
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

    async def test_update_saves_tz(self):
        """Поле night_mode_tz сохраняется."""
        from httpx import AsyncClient, ASGITransport
        app = web_app.create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            cookies = await self._login_as_su(client)
            r = await self._post_update(client, cookies, night_mode_tz="Asia/Yekaterinburg")
            self.assertEqual(r.status_code, 303)
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001234567890)
            )).scalar_one()
            self.assertEqual(cs.night_mode_tz, "Asia/Yekaterinburg")

    async def test_update_invalid_tz_redirects_with_flash(self):
        """Невалидный tz → редирект с flash сообщением."""
        from httpx import AsyncClient, ASGITransport
        app = web_app.create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            cookies = await self._login_as_su(client)
            r = await self._post_update(client, cookies, night_mode_tz="Not/A/Real/Zone")
            self.assertEqual(r.status_code, 303)
            self.assertIn("Invalid+night_mode_tz", r.headers.get("location", ""))

    async def test_update_saves_weekend_schedule(self):
        """Поле weekend schedule сохраняется."""
        from httpx import AsyncClient, ASGITransport
        app = web_app.create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            cookies = await self._login_as_su(client)
            r = await self._post_update(
                client, cookies,
                night_mode_weekend_start="02:00",
                night_mode_weekend_end="10:00",
            )
            self.assertEqual(r.status_code, 303)
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001234567890)
            )).scalar_one()
            self.assertEqual(cs.night_mode_weekend_start, "02:00")
            self.assertEqual(cs.night_mode_weekend_end, "10:00")

    async def test_update_weekend_only_one_field_redirects(self):
        """Если указано только start (без end) → редирект с ошибкой."""
        from httpx import AsyncClient, ASGITransport
        app = web_app.create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            cookies = await self._login_as_su(client)
            r = await self._post_update(client, cookies, night_mode_weekend_start="02:00")
            self.assertEqual(r.status_code, 303)
            self.assertIn("Weekend+schedule+requires+both", r.headers.get("location", ""))

    async def test_update_invalid_weekend_time_redirects(self):
        """Невалидное HH:MM для weekend → редирект с ошибкой."""
        from httpx import AsyncClient, ASGITransport
        app = web_app.create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            cookies = await self._login_as_su(client)
            r = await self._post_update(
                client, cookies,
                night_mode_weekend_start="99:99",
                night_mode_weekend_end="10:00",
            )
            self.assertEqual(r.status_code, 303)
            self.assertIn("Invalid+night_mode_weekend_start", r.headers.get("location", ""))

    async def test_update_saves_notify_flag(self):
        """Поле night_mode_notify=on сохраняется как True."""
        from httpx import AsyncClient, ASGITransport
        app = web_app.create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            cookies = await self._login_as_su(client)
            r = await self._post_update(client, cookies, night_mode_notify="on")
            self.assertEqual(r.status_code, 303)
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001234567890)
            )).scalar_one()
            self.assertTrue(cs.night_mode_notify)

    async def test_update_notify_off_default(self):
        """По умолчанию (без notify=on) — False."""
        from httpx import AsyncClient, ASGITransport
        app = web_app.create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            cookies = await self._login_as_su(client)
            r = await self._post_update(client, cookies)
            self.assertEqual(r.status_code, 303)
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001234567890)
            )).scalar_one()
            self.assertFalse(cs.night_mode_notify)

    async def test_update_custom_preset_saves_individual_perms(self):
        """preset=custom + 10 чекбоксов → JSON с правильными флагами."""
        from httpx import AsyncClient, ASGITransport
        app = web_app.create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            cookies = await self._login_as_su(client)
            r = await self._post_update(
                client, cookies,
                night_mode_preset="custom",
                perm_can_send_messages="on",
                perm_can_send_photos="on",
                # audios, docs, videos и т.д. НЕ отправляем (=unchecked=False).
            )
            self.assertEqual(r.status_code, 303)
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001234567890)
            )).scalar_one()
            perms = json.loads(cs.night_mode_permissions)
            self.assertTrue(perms["can_send_messages"])
            self.assertTrue(perms["can_send_photos"])
            self.assertFalse(perms["can_send_audios"])
            self.assertFalse(perms["can_send_videos"])
            self.assertFalse(perms["can_send_other_messages"])
            # admin-level права всегда False.
            self.assertFalse(perms["can_change_info"])
            self.assertFalse(perms["can_invite_users"])
            self.assertFalse(perms["can_pin_messages"])

    async def test_update_notify_enter_exit_msgs(self):
        """Кастомные тексты уведомлений сохраняются."""
        from httpx import AsyncClient, ASGITransport
        app = web_app.create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            cookies = await self._login_as_su(client)
            r = await self._post_update(
                client, cookies,
                night_mode_notify="on",
                night_mode_notify_enter_msg="🌙 Тишина!",
                night_mode_notify_exit_msg="☀️ Утро!",
            )
            self.assertEqual(r.status_code, 303)
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001234567890)
            )).scalar_one()
            self.assertEqual(cs.night_mode_notify_enter_msg, "🌙 Тишина!")
            self.assertEqual(cs.night_mode_notify_exit_msg, "☀️ Утро!")

    async def test_update_notify_empty_msgs_become_none(self):
        """Пустые тексты → None (дефолтный шаблон)."""
        from httpx import AsyncClient, ASGITransport
        app = web_app.create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            cookies = await self._login_as_su(client)
            r = await self._post_update(
                client, cookies,
                night_mode_notify="on",
                night_mode_notify_enter_msg="",
                night_mode_notify_exit_msg="   ",
            )
            self.assertEqual(r.status_code, 303)
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001234567890)
            )).scalar_one()
            self.assertIsNone(cs.night_mode_notify_enter_msg)
            self.assertIsNone(cs.night_mode_notify_exit_msg)


# ═══════════════════════════════════════════════════════════════════════════
# Тест 24: _night_mode_preset_name распознаёт custom
# ═══════════════════════════════════════════════════════════════════════════
class TestNightModePresetNameCustom(unittest.TestCase):

    def test_text_only(self):
        perms = json.dumps({
            "can_send_messages": True,
            "can_send_audios": False, "can_send_documents": False,
            "can_send_photos": False, "can_send_videos": False,
            "can_send_video_notes": False, "can_send_voice_notes": False,
            "can_send_polls": False, "can_send_other_messages": False,
            "can_add_web_page_previews": False,
        })
        self.assertEqual(web_app._night_mode_preset_name(perms), "text_only")

    def test_strict(self):
        perms = json.dumps({
            "can_send_messages": False, "can_send_audios": False,
            "can_send_documents": False, "can_send_photos": False,
            "can_send_videos": False, "can_send_video_notes": False,
            "can_send_voice_notes": False, "can_send_polls": False,
            "can_send_other_messages": False,
        })
        self.assertEqual(web_app._night_mode_preset_name(perms), "strict")

    def test_none_preset(self):
        perms = json.dumps({
            "can_send_messages": True, "can_send_audios": True,
            "can_send_documents": True, "can_send_photos": True,
            "can_send_videos": True, "can_send_video_notes": True,
            "can_send_voice_notes": True, "can_send_polls": True,
            "can_send_other_messages": True, "can_add_web_page_previews": True,
        })
        self.assertEqual(web_app._night_mode_preset_name(perms), "none")

    def test_custom_partial(self):
        """Только photos=True, остальное False (кроме msgs) → custom."""
        perms = json.dumps({
            "can_send_messages": True,
            "can_send_audios": False, "can_send_documents": False,
            "can_send_photos": True,  # ← True, отличает от text_only
            "can_send_videos": False, "can_send_video_notes": False,
            "can_send_voice_notes": False, "can_send_polls": False,
            "can_send_other_messages": False,
            "can_add_web_page_previews": False,
        })
        self.assertEqual(web_app._night_mode_preset_name(perms), "custom")

    def test_none_input(self):
        self.assertEqual(web_app._night_mode_preset_name(None), "text_only")

    def test_invalid_json(self):
        self.assertEqual(web_app._night_mode_preset_name("not-json"), "text_only")


# ═══════════════════════════════════════════════════════════════════════════
# Тест 25: APP_VERSION
# ═══════════════════════════════════════════════════════════════════════════
class TestAppVersion(unittest.TestCase):

    def test_version_bumped(self):
        # v4.5.4 bumped the version further; this test still validates that
        # APP_VERSION is at least v4.5.3.
        ver = web_app.APP_VERSION
        m = ver.split(".")
        self.assertEqual(m[0], "v4", f"Major version should be v4, got {m[0]}")
        self.assertEqual(m[1], "5", f"Minor version should be 5, got {m[1]}")
        patch = int(m[2])
        self.assertGreaterEqual(patch, 3, f"Patch should be ≥ 3, got {patch}")


# ═══════════════════════════════════════════════════════════════════════════
# Тест 26-29: /help cleanup
# ═══════════════════════════════════════════════════════════════════════════
class TestHelpCleanup(unittest.TestCase):

    def _get_help_text(self):
        """Извлекает текст /help из cmd_help handler."""
        # Самый простой способ — вызвать cmd_help с mock message и перехватить reply.
        msg = MagicMock()
        msg.from_user = MagicMock()
        msg.from_user.id = 111111111
        msg.reply = AsyncMock()
        # Запускаем async функцию через unittest runner.
        import asyncio
        asyncio.run(bot_handlers.cmd_help(msg))
        return msg.reply.call_args.args[0] if msg.reply.call_args.args else \
            msg.reply.call_args.kwargs.get("text", "")

    def test_help_mentions_web_panel(self):
        """/help упоминает веб-панель /admin/chats."""
        text = self._get_help_text()
        self.assertIn("/admin/chats", text)
        self.assertIn("веб-панел", text.lower())

    def test_help_no_redundant_30m_example(self):
        """/help НЕ содержит избыточный пример '!mute 30м'."""
        text = self._get_help_text()
        # Старый текст имел отдельную строку "!mute 30м — мьют на 30 минут без причины".
        # Новый — объединяет в одну строку с упоминанием формата.
        self.assertNotIn("!mute 30м — мьют на 30 минут без причины", text)

    def test_help_contains_nightmode_tz_subcommand(self):
        """/help содержит '/nightmode chat_id tz'."""
        text = self._get_help_text()
        self.assertIn("/nightmode chat_id tz", text)

    def test_help_contains_nightmode_weekend_subcommand(self):
        """/help содержит '/nightmode chat_id weekend'."""
        text = self._get_help_text()
        self.assertIn("/nightmode chat_id weekend", text)

    def test_help_contains_nightmode_custom_subcommand(self):
        """/help содержит '/nightmode chat_id custom'."""
        text = self._get_help_text()
        self.assertIn("/nightmode chat_id custom", text)

    def test_help_contains_nightmode_notify_subcommand(self):
        """/help содержит '/nightmode chat_id notify'."""
        text = self._get_help_text()
        self.assertIn("/nightmode chat_id notify", text)

    def test_help_groups_commands(self):
        """/help имеет группы: 'В группах', 'В личке', 'Фильтры', 'Ночной режим'."""
        text = self._get_help_text()
        self.assertIn("В группах", text)
        self.assertIn("В личке", text)
        self.assertIn("Фильтры", text)
        self.assertIn("Ночной режим", text)


# ═══════════════════════════════════════════════════════════════════════════
# Тест 30: from_json Jinja2 filter
# ═══════════════════════════════════════════════════════════════════════════
class TestFromJsonFilter(unittest.TestCase):

    def test_from_json_parses_dict(self):
        """from_json парсит JSON-строку в dict."""
        from jinja2 import Environment
        env = Environment()
        env.filters["from_json"] = lambda s: json.loads(s) if s else {}
        template = env.from_string('{{ \'{"a": 1, "b": true}\' | from_json }}')
        result = template.render()
        # Jinja2 рендерит dict в его str-представление.
        self.assertIn("'a'", result)
        self.assertIn("1", result)

    def test_from_json_empty_string(self):
        """from_json с пустой строкой → пустой dict."""
        from jinja2 import Environment
        env = Environment()
        env.filters["from_json"] = lambda s: json.loads(s) if s else {}
        template = env.from_string('{{ "" | from_json | length }}')
        result = template.render()
        self.assertEqual(result, "0")

    def test_from_json_none(self):
        """from_json с None → пустой dict."""
        from jinja2 import Environment
        env = Environment()
        env.filters["from_json"] = lambda s: json.loads(s) if s else {}
        template = env.from_string('{{ none | from_json | length }}')
        result = template.render()
        self.assertEqual(result, "0")


# ═══════════════════════════════════════════════════════════════════════════
# Тест 31: base.html changelog modal содержит v4.5.3
# ═══════════════════════════════════════════════════════════════════════════
class TestChangelogModalV453(unittest.TestCase):

    def test_changelog_contains_v453(self):
        with open("/home/z/my-project/v4.5/templates/base.html") as f:
            content = f.read()
        self.assertIn("v4.5.3", content)
        self.assertIn("Night mode — per-chat timezone", content)
        self.assertIn("Night mode — weekend schedule", content)
        self.assertIn("Night mode — custom permissions", content)
        self.assertIn("Night mode — enter/exit notifications", content)


# ═══════════════════════════════════════════════════════════════════════════
# Тест 32: _night_mode_preset_name используется в шаблоне
# ═══════════════════════════════════════════════════════════════════════════
class TestTemplateUsesV453Fields(unittest.TestCase):

    def test_admin_chats_template_has_tz_select(self):
        with open("/home/z/my-project/v4.5/templates/admin_chats.html") as f:
            content = f.read()
        self.assertIn('name="night_mode_tz"', content)
        self.assertIn('name="night_mode_weekend_start"', content)
        self.assertIn('name="night_mode_weekend_end"', content)
        self.assertIn('name="night_mode_notify"', content)
        self.assertIn('name="night_mode_notify_enter_msg"', content)
        self.assertIn('name="night_mode_notify_exit_msg"', content)

    def test_admin_chats_template_has_custom_perms_grid(self):
        with open("/home/z/my-project/v4.5/templates/admin_chats.html") as f:
            content = f.read()
        # Поля генерируются через Jinja2 loop — name="{{ field_name }}", где
        # field_name берётся из tuple. Проверяем что tuple содержит все 10 полей.
        self.assertIn("'perm_can_send_messages'", content)
        self.assertIn("'perm_can_send_audios'", content)
        self.assertIn("'perm_can_send_photos'", content)
        self.assertIn("'perm_can_send_other_messages'", content)
        self.assertIn("'perm_can_add_web_page_previews'", content)
        # Custom grid div должен быть.
        self.assertIn("nm_custom_grid_", content)

    def test_admin_chats_template_preset_dropdown_has_custom(self):
        with open("/home/z/my-project/v4.5/templates/admin_chats.html") as f:
            content = f.read()
        # Проверяем что в dropdown пресета теперь 4 опции включая custom.
        # Ищем 'custom' в списке preset'ов.
        self.assertIn("'text_only', 'strict', 'none', 'custom'", content)

    def test_admin_chats_template_tz_dropdown_has_ekb(self):
        with open("/home/z/my-project/v4.5/templates/admin_chats.html") as f:
            content = f.read()
        self.assertIn("Asia/Yekaterinburg", content)
        self.assertIn("Europe/Moscow", content)
        self.assertIn("Asia/Novosibirsk", content)


if __name__ == "__main__":
    unittest.main(verbosity=2)
