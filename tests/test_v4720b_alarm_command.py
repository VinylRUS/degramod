"""
test_v4720b_alarm_command.py — v4.7.20 Этап 2: тесты !alarm команды.

Покрывает:
- _CMD_ALARM regex (on/off/вкл/выкл + duration)
- _alarm_permissions() — только текст, всё медиа False
- _parse_alarm_duration() — форматы 1ч/1h/30м/30m/2д/2d, ошибки
- _format_alarm_duration() — человекочитаемый вывод
- ChatSettings: новые поля alarm_*
- _deactivate_alarm() — логика восстановления прав
- handle_alarm_command handler существует
- _send_alarm_dm() — DM-уведомление
- Импорт _deactivate_alarm в bot.py для night mode integration
- _ALL_MOD_COMMANDS включает _CMD_ALARM
- Регрессия: другие команды не сломаны

Запуск:
    BOT_TOKEN="123456789:AAEhBP0av28-bot-test-token-fake" \\
    uv run pytest tests/test_v4720b_alarm_command.py
"""
from _paths import _P  # noqa: E402  (корень вычисляется от __file__)

import ast
import re
import unittest
from pathlib import Path
from datetime import timedelta


V4_DIR = Path(_P())
BOT_HANDLERS_PATH = V4_DIR / "bot_handlers.py"
BOT_PATH = V4_DIR / "bot.py"
DB_PATH = V4_DIR / "db.py"
BASE_HTML_PATH = V4_DIR / "templates" / "base.html"


def _read_handlers() -> str:
    return BOT_HANDLERS_PATH.read_text(encoding="utf-8")


def _read_bot() -> str:
    # v4.8.9: _alarm_auto_off_tick перенесена из bot.py в chat_modes.py — это
    # её домен (bot.py:198). Проверки ниже ищут определения и вызовы, поэтому
    # склеиваем оба модуля: важно наличие логики, а не файл, где она лежит.
    src = BOT_PATH.read_text(encoding="utf-8")
    _chat_modes = BOT_PATH.parent / "chat_modes.py"
    if _chat_modes.exists():
        src += "\n" + _chat_modes.read_text(encoding="utf-8")
    return src


def _read_db() -> str:
    return DB_PATH.read_text(encoding="utf-8")


def _read_base() -> str:
    return BASE_HTML_PATH.read_text(encoding="utf-8")


# Импортируем тестируемые функции из bot_handlers
import sys
sys.path.insert(0, str(V4_DIR))

# Установим dummy BOT_TOKEN чтобы bot.py импортировался
import os
os.environ.setdefault("BOT_TOKEN", "123456789:AAEhBP0av28-bot-test-token-fake")

from bot_handlers import (
    _alarm_permissions,
    _parse_alarm_duration,
    _format_alarm_duration,
    _ALARM_SLOW_MODE_DELAY,
)
from aiogram import types

# v5.1.0: _CMD_ALARM/_ALL_MOD_COMMANDS переехали в реестр commands.py —
# паттерн больше не якорится на "!" и матчит нормализованную строку
# (без префикса). commands.resolve делает нормализацию сама, поэтому тесты
# ниже гоняют через неё — так же, как это делает production-код.
import commands  # noqa: E402


def _resolve(text: str):
    return commands.resolve(text, None)


class TestV4720bAlarmRegex(unittest.TestCase):
    """Парсинг команды !alarm через commands.resolve (реестр v5.1.0)."""

    def test_01_alarm_on_simple(self):
        spec, m = _resolve("!alarm on")
        self.assertEqual(spec.name, "alarm")
        self.assertEqual(m.group("state").lower(), "on")
        self.assertIsNone(m.group("amount"))
        self.assertIsNone(m.group("unit"))

    def test_02_alarm_off_simple(self):
        spec, m = _resolve("!alarm off")
        self.assertEqual(spec.name, "alarm")
        self.assertEqual(m.group("state").lower(), "off")

    def test_03_alarm_on_with_duration_hours_ru(self):
        spec, m = _resolve("!alarm on 1ч")
        self.assertEqual(m.group("state").lower(), "on")
        self.assertEqual(m.group("amount"), "1")
        self.assertEqual(m.group("unit").lower(), "ч")

    def test_04_alarm_on_with_duration_hours_en(self):
        spec, m = _resolve("!alarm on 2h")
        self.assertEqual(m.group("amount"), "2")
        self.assertEqual(m.group("unit").lower(), "h")

    def test_05_alarm_on_with_duration_minutes_ru(self):
        spec, m = _resolve("!alarm on 30м")
        self.assertEqual(m.group("amount"), "30")
        self.assertEqual(m.group("unit").lower(), "м")

    def test_06_alarm_on_with_duration_minutes_en(self):
        spec, m = _resolve("!alarm on 45m")
        self.assertEqual(m.group("amount"), "45")
        self.assertEqual(m.group("unit").lower(), "m")

    def test_07_alarm_on_with_duration_days(self):
        spec, m = _resolve("!alarm on 2д")
        self.assertEqual(m.group("amount"), "2")
        self.assertEqual(m.group("unit").lower(), "д")

    def test_08_alarm_case_insensitive(self):
        spec, m = _resolve("!ALARM ON 1H")
        self.assertEqual(m.group("state").lower(), "on")

    def test_09_alarm_russian_aliases(self):
        spec, m = _resolve("!alarm вкл")
        self.assertEqual(m.group("state").lower(), "вкл")

        spec, m = _resolve("!alarm выкл")
        self.assertEqual(m.group("state").lower(), "выкл")

    def test_10_alarm_no_match_random_text(self):
        """Тексты, не резолвящиеся в /alarm, не должны матчиться."""
        for text in ["", "hello", "!warn reason", "!mute 1h", "alarm on", "!alarmon"]:
            found = _resolve(text)
            self.assertTrue(
                found is None or found[0].name != "alarm",
                f"Не должен резолвиться в alarm: {text!r}",
            )

    def test_11_alarm_no_duration_unit(self):
        """!alarm on 30 без единицы — не должно парситься."""
        found = _resolve("!alarm on 30")
        # По regex — number без unit не матчится (опциональная группа требует unit)
        self.assertIsNone(found)

    def test_12_ALARM_in_ALL_MOD_COMMANDS(self):
        """alarm должен быть зарегистрирован в commands.GROUP_COMMANDS."""
        self.assertIsNotNone(commands.spec_by_name("alarm"))


class TestV4720bAlarmPermissions(unittest.TestCase):
    """_alarm_permissions() возвращает ChatPermissions с текстом и без медиа."""

    def test_13_can_send_messages_true(self):
        perms = _alarm_permissions()
        self.assertTrue(perms.can_send_messages)

    def test_14_all_media_false(self):
        perms = _alarm_permissions()
        for field in [
            "can_send_audios", "can_send_documents", "can_send_photos",
            "can_send_videos", "can_send_video_notes", "can_send_voice_notes",
            "can_send_polls", "can_send_other_messages",
            "can_add_web_page_previews",
        ]:
            self.assertFalse(getattr(perms, field), f"{field} должен быть False")

    def test_15_no_admin_fields(self):
        """Админские права НЕ должны быть установлены (restrict_chat_member их не касается)."""
        perms = _alarm_permissions()
        # Админских полей вообще не должно быть в объекте (None = Telegram default)
        for field in ["can_change_info", "can_invite_users", "can_pin_messages"]:
            self.assertIsNone(getattr(perms, field, "NOT_SET"),
                              f"{field} должен быть None (не задан)")

    def test_16_ALARM_SLOW_MODE_DELAY_is_30(self):
        """Slow mode для alarm — 30 секунд (как специфицировано)."""
        self.assertEqual(_ALARM_SLOW_MODE_DELAY, 30)


class TestV4720bParseDuration(unittest.TestCase):
    """_parse_alarm_duration() — парсинг длительности."""

    def test_17_hours_ru(self):
        td = _parse_alarm_duration("1", "ч")
        self.assertEqual(td, timedelta(hours=1))

    def test_18_hours_en(self):
        td = _parse_alarm_duration("2", "H")
        self.assertEqual(td, timedelta(hours=2))

    def test_19_minutes_ru(self):
        td = _parse_alarm_duration("30", "м")
        self.assertEqual(td, timedelta(minutes=30))

    def test_20_minutes_en(self):
        td = _parse_alarm_duration("45", "M")
        self.assertEqual(td, timedelta(minutes=45))

    def test_21_days_ru(self):
        td = _parse_alarm_duration("2", "д")
        self.assertEqual(td, timedelta(days=2))

    def test_22_days_en(self):
        td = _parse_alarm_duration("1", "D")
        self.assertEqual(td, timedelta(days=1))

    def test_23_none_when_no_value(self):
        self.assertIsNone(_parse_alarm_duration(None, "ч"))
        self.assertIsNone(_parse_alarm_duration("1", None))
        self.assertIsNone(_parse_alarm_duration("", ""))

    def test_24_invalid_number_raises(self):
        with self.assertRaises(ValueError):
            _parse_alarm_duration("abc", "ч")

    def test_25_zero_or_negative_raises(self):
        with self.assertRaises(ValueError):
            _parse_alarm_duration("0", "ч")
        with self.assertRaises(ValueError):
            _parse_alarm_duration("-5", "ч")

    def test_26_unknown_unit_raises(self):
        with self.assertRaises(ValueError):
            _parse_alarm_duration("1", "s")  # seconds не поддерживаются


class TestV4720bFormatDuration(unittest.TestCase):
    """_format_alarm_duration() — человекочитаемый вывод."""

    def test_27_minutes_only(self):
        self.assertEqual(_format_alarm_duration(timedelta(minutes=30)), "30м")
        self.assertEqual(_format_alarm_duration(timedelta(minutes=45)), "45м")

    def test_28_hours_only(self):
        self.assertEqual(_format_alarm_duration(timedelta(hours=2)), "2ч")
        self.assertEqual(_format_alarm_duration(timedelta(hours=1)), "1ч")

    def test_29_hours_and_minutes(self):
        self.assertEqual(_format_alarm_duration(timedelta(hours=1, minutes=30)), "1ч 30м")

    def test_30_days_only(self):
        self.assertEqual(_format_alarm_duration(timedelta(days=2)), "2д")

    def test_31_days_and_hours(self):
        self.assertEqual(_format_alarm_duration(timedelta(days=1, hours=12)), "1д 12ч")


class TestV4720bDbSchema(unittest.TestCase):
    """Новые поля alarm_* в ChatSettings (db.py)."""

    def test_32_alarm_currently_active_field(self):
        src = _read_db()
        self.assertIn("alarm_currently_active", src)
        self.assertIn("Column(Boolean, default=False, nullable=False)", src)

    def test_33_alarm_saved_permissions_field(self):
        src = _read_db()
        self.assertIn("alarm_saved_permissions", src)
        self.assertIn("Column(Text, nullable=True)", src)

    def test_34_alarm_saved_slow_mode_delay_field(self):
        src = _read_db()
        self.assertIn("alarm_saved_slow_mode_delay", src)

    def test_35_alarm_active_until_field(self):
        src = _read_db()
        self.assertIn("alarm_active_until", src)
        self.assertIn("Column(DateTime, nullable=True)", src)

    def test_36_alarm_started_by_field(self):
        src = _read_db()
        self.assertIn("alarm_started_by", src)
        self.assertIn("Column(BigInteger, nullable=True)", src)

    def test_37_migration_block_present(self):
        """В init_db должна быть миграция для alarm колонок."""
        src = _read_db()
        self.assertIn("v4.7.20 !alarm columns", src)
        self.assertIn("v4720_alarm_cols", src)

    def test_38_db_ast_valid(self):
        src = _read_db()
        try:
            ast.parse(src)
        except SyntaxError as e:
            self.fail(f"db.py syntax error: {e}")


class TestV4720bHandlersCodeStructure(unittest.TestCase):
    """Структура кода handlers — функции присутствуют."""

    def test_39_deactivate_alarm_function(self):
        src = _read_handlers()
        self.assertIn("async def _deactivate_alarm(", src)

    def test_40_handle_alarm_command_handler(self):
        src = _read_handlers()
        self.assertIn("async def handle_alarm_command(", src)

    def test_41_send_alarm_dm_function(self):
        src = _read_handlers()
        self.assertIn("async def _send_alarm_dm(", src)

    def test_42_alarm_permissions_function(self):
        src = _read_handlers()
        self.assertIn("def _alarm_permissions(", src)

    def test_43_parse_alarm_duration_function(self):
        src = _read_handlers()
        self.assertIn("def _parse_alarm_duration(", src)

    def test_44_format_alarm_duration_function(self):
        src = _read_handlers()
        self.assertIn("def _format_alarm_duration(", src)

    def test_45_handlers_ast_valid(self):
        src = _read_handlers()
        try:
            ast.parse(src)
        except SyntaxError as e:
            self.fail(f"bot_handlers.py syntax error: {e}")


class TestV4720bBotIntegration(unittest.TestCase):
    """Интеграция alarm в bot.py (_night_mode_tick)."""

    def test_46_deactivate_alarm_imported_in_bot(self):
        src = _read_bot()
        self.assertIn("_deactivate_alarm", src)
        # Должен быть в импорте из bot_handlers
        self.assertTrue(
            re.search(r"from\s+bot_handlers\s+import\s+.*_deactivate_alarm", src, re.DOTALL),
            "_deactivate_alarm должен импортироваться из bot_handlers в bot.py"
        )

    def test_47_alarm_auto_off_in_night_tick(self):
        """В _night_mode_tick (или _alarm_auto_off_tick) должна быть проверка alarm_active_until.

        v4.7.30: логика auto-off вынесена из _night_mode_tick в отдельную
        функцию _alarm_auto_off_tick (Баг #1 аудита v4.7.30). Раньше auto-off
        работал только для чатов с night_mode_enabled=True — теперь для всех.
        Тест обновлён: ищет alarm_active_until и auto_off_timeout в любой из
        функций (предпочитает _alarm_auto_off_tick, fallback на _night_mode_tick
        для обратной совместимости со старыми версиями).
        """
        src = _read_bot()
        # Сначала ищем новую _alarm_auto_off_tick
        m_alarm = re.search(
            # `[^)]*\)\s*:` не переживает аннотацию возврата: сигнатура стала
            # `async def _alarm_auto_off_tick(bot: "Bot") -> None:`.
            r"async\s+def\s+_alarm_auto_off_tick\s*\([^)]*\)\s*(?:->[^:]+)?:\s*(.*?)"
            r"(?=\nasync\s+def\s+|\nclass\s+|\Z)",
            src, re.DOTALL,
        )
        if m_alarm:
            body = m_alarm.group(1)
            self.assertIn("alarm_active_until", body,
                          "_alarm_auto_off_tick должна проверять alarm_active_until")
            self.assertIn("auto_off_timeout", body,
                          "Должен быть reason='auto_off_timeout' при авто-выключении")
            # v4.7.30: SQL query не должен фильтровать по night_mode_enabled.
            # Извлекаем ТОЛЬКО тело select()...where(...) — пропуская docstring.
            # Docstring содержит "night_mode_enabled" в описании истории бага,
            # но SQL query — не должен.
            import re as _re
            # Берём всё после первого 'select(' (пропускаем docstring)
            select_match = _re.search(r'select\(.*?\.where\(.*?\)', body, _re.DOTALL)
            self.assertIsNotNone(select_match,
                                 "_alarm_auto_off_tick должна содержать select(...).where(...)")
            sql_part = select_match.group(0)
            self.assertIn("alarm_currently_active.is_(True)", sql_part,
                          "_alarm_auto_off_tick SQL должна фильтровать по alarm_currently_active=True")
            self.assertNotIn("night_mode_enabled", sql_part,
                             "v4.7.30: _alarm_auto_off_tick SQL не должен фильтровать по night_mode_enabled")
        else:
            # Fallback: старая структура (v4.7.20–v4.7.29) — логика в _night_mode_tick
            m = re.search(
                r"async\s+def\s+_night_mode_tick\s*\([^)]*\)\s*:\s*(.*?)(?=\nasync\s+def\s+|\nclass\s+|\Z)",
                src, re.DOTALL,
            )
            self.assertIsNotNone(m, "Ни _alarm_auto_off_tick, ни _night_mode_tick не найдены")
            body = m.group(1)
            self.assertIn("alarm_active_until", body,
                          "_night_mode_tick должна проверять alarm_active_until")
            self.assertIn("auto_off_timeout", body,
                          "Должен быть reason='auto_off_timeout' при авто-выключении")

    def test_48_alarm_auto_deactivate_on_night_enter(self):
        """При входе в night mode активный alarm должен сниматься."""
        src = _read_bot()
        m = re.search(
            r"async\s+def\s+_night_mode_tick\s*\([^)]*\)\s*:\s*(.*?)(?=\nasync\s+def\s+|\nclass\s+|\Z)",
            src, re.DOTALL,
        )
        body = m.group(1)
        self.assertIn("night_mode_enter", body,
                      "Должен быть reason='night_mode_enter' при снятии alarm перед night mode")
        self.assertIn("Alarm auto-deactivate on night mode enter", body)

    def test_49_bot_ast_valid(self):
        src = _read_bot()
        try:
            ast.parse(src)
        except SyntaxError as e:
            self.fail(f"bot.py syntax error: {e}")


class TestV4720bDeactivateAlarmLogic(unittest.TestCase):
    """Логика _deactivate_alarm — тесты через вызов функции."""

    def test_50_deactivate_returns_false_when_not_active(self):
        """Если alarm не активен — возвращает False, ничего не делает.

        v4.7.30: сигнатура изменена на tuple (ok, perms_source, slow_source).
        Старый тест проверял bool — теперь проверяем что ok=False.
        """
        # Создаём mock ChatSettings
        from db import ChatSettings
        from unittest.mock import AsyncMock, MagicMock

        cs = ChatSettings(chat_id=-1001234567890)
        cs.alarm_currently_active = False
        cs.alarm_saved_permissions = None
        cs.alarm_saved_slow_mode_delay = None

        session = MagicMock()
        bot = MagicMock()

        # Используем asyncio.run для вызова async функции
        import asyncio
        result = asyncio.run(_deactivate_alarm_inner(session, cs, bot, -1001234567890))
        # v4.7.30: теперь возвращается tuple (ok, perms_source, slow_source)
        self.assertIsInstance(result, tuple, "v4.7.30: должен вернуть tuple")
        self.assertEqual(len(result), 3, "v4.7.30: tuple должен быть длиной 3")
        ok = result[0]
        self.assertFalse(ok, "Если alarm не активен — ok=False")


async def _deactivate_alarm_inner(session, cs, bot, chat_id):
    """Обёртка для вызова _deactivate_alarm в тесте."""
    from bot_handlers import _deactivate_alarm
    return await _deactivate_alarm(session, cs, bot, chat_id, reason="test")


class TestV4720bChangelog(unittest.TestCase):
    """Changelog — проверяется в финальном этапе."""

    def test_51_changelog_v4720_alarm(self):
        src = _read_base()
        if "v4.7.20" not in src:
            self.skipTest("v4.7.20 changelog ещё не добавлен — этап version_bump")
        self.assertIn("v4.7.20", src)
        # Changelog должен упоминать alarm
        # (точная формулировка может варьироваться)
        self.assertTrue(
            "!alarm" in src or "alarm" in src.lower(),
            "Changelog должен упоминать !alarm"
        )


class TestV4720bSlowModeApiFix(unittest.TestCase):
    """Регрессия v4.7.21+22 hotfix: bot.set_chat_slow_mode_delay AttributeError.

    Корневая причина (найдена в v4.7.21): aiogram 3.30 НЕ экспортирует метод
    SetChatSlowModeDelay ни как bot.set_chat_slow_mode_delay(), ни как
    aiogram.methods.SetChatSlowModeDelay. В коде !alarm и _deactivate_alarm
    использовался bot.set_chat_slow_mode_delay(...) что вызывало AttributeError.

    v4.7.21 фикс (НЕВАРНЫЙ): late import `from bot import SetChatSlowModeDelay`.
    Вызывал НОВУЮ багу: при запуске bot.py как __main__ late import
    `from bot import X` вызывал повторный import bot.py как отдельный модуль
    (т.к. __main__ и bot — разные module names в sys.modules). Повторный import
    выполнял dp.include_router(mod_router) второй раз → RuntimeError: Router
    is already attached. (См. v4.7.22 changelog.)

    v4.7.22 фикс (ФИНАЛЬНЫЙ): класс SetChatSlowModeDelay перемещён из bot.py в
    bot_handlers.py (top-level определение). bot.py импортирует класс через
    существующий `from bot_handlers import ...` line. Late import больше не нужен.
    """

    def test_52_no_direct_bot_set_chat_slow_mode_delay_calls(self):
        """В bot_handlers.py не должно остаться вызовов bot.set_chat_slow_mode_delay()."""
        src = _read_handlers()
        # Исключаем комментарии и строки с упоминанием метода
        import re as _re
        # Находим строки вида `bot.set_chat_slow_mode_delay(` или `message.bot.set_chat_slow_mode_delay(`
        # где это ВЫЗОВ (не комментарий, не docstring).
        bad_lines = []
        for i, line in enumerate(src.splitlines(), 1):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            # Ищем паттерн вызова
            if _re.search(r"\.set_chat_slow_mode_delay\s*\(", line):
                # Проверяем что это реальный вызов, а не в комментарии или строке
                # (для упрощения — если строка содержит "=", "await" или в начале — это вызов)
                bad_lines.append((i, line.rstrip()))
        self.assertEqual(
            bad_lines, [],
            f"Найдены прямые вызовы bot.set_chat_slow_mode_delay() (не работают в aiogram 3.30): "
            f"{bad_lines}. Используйте `from bot import SetChatSlowModeDelay` + "
            f"`await bot(SetChatSlowModeDelay(chat_id=..., slow_mode_delay=...))`",
        )

    def test_53_set_chat_slow_mode_delay_wrapper_in_handlers(self):
        """v4.7.22: класс-обёртка SetChatSlowModeDelay живёт в bot_handlers.py (НЕ в bot.py)."""
        src = _read_handlers()
        self.assertIn("class SetChatSlowModeDelay", src,
                      "Класс SetChatSlowModeDelay должен быть определён в bot_handlers.py")
        self.assertIn("__api_method__ = \"setChatSlowModeDelay\"", src)
        self.assertIn("__returning__ = bool", src)
        # bot.py больше НЕ должен содержать определение класса — только import
        bot_src = _read_bot()
        self.assertNotIn("class SetChatSlowModeDelay", bot_src,
                          "bot.py НЕ должен содержать class SetChatSlowModeDelay (перенесён в bot_handlers.py в v4.7.22)")

    def test_54_handlers_uses_set_chat_slow_mode_delay_wrapper(self):
        """bot_handlers.py использует SetChatSlowModeDelay напрямую (класс локальный)."""
        src = _read_handlers()
        # Должно быть минимум 2 вызова SetChatSlowModeDelay(...) — в _deactivate_alarm
        # и handle_alarm_command. Late import НЕ нужен (класс определён в этом же модуле).
        self.assertGreaterEqual(
            src.count("SetChatSlowModeDelay("),
            2,
            "Должно быть минимум 2 вызова SetChatSlowModeDelay(...) — в _deactivate_alarm и handle_alarm_command",
        )

    def test_55_set_chat_slow_mode_delay_calls_inside_try_except(self):
        """Вызовы SetChatSlowModeDelay должны быть обёрнуты в try/except TelegramAPIError."""
        src = _read_handlers()
        # Простая эвристика: каждый вызов SetChatSlowModeDelay( должен быть внутри try:
        # Проверяем через AST.
        tree = ast.parse(src)
        wrapper_calls_in_try = 0
        wrapper_calls_total = 0
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                # Ищем вызов вида SetChatSlowModeDelay(...)
                func = node.func
                if isinstance(func, ast.Name) and func.id == "SetChatSlowModeDelay":
                    wrapper_calls_total += 1
                    # Проверяем что родительский узел — это await внутри Try
                    # AST не хранит ссылку на родителя, поэтому используем walk
                    # по всем Try узлам
        # Альтернативно: проверяем что в AST есть Try блоки, внутри которых есть Await с SetChatSlowModeDelay
        for node in ast.walk(tree):
            if isinstance(node, ast.Try):
                for child in ast.walk(node):
                    if isinstance(child, ast.Call):
                        func = child.func
                        if isinstance(func, ast.Name) and func.id == "SetChatSlowModeDelay":
                            wrapper_calls_in_try += 1
                            break  # один Try = один вызов (достаточно)
        self.assertGreaterEqual(
            wrapper_calls_in_try, 2,
            f"Ожидается минимум 2 вызова SetChatSlowModeDelay внутри try/except, "
            f"найдено {wrapper_calls_in_try} из {wrapper_calls_total} total",
        )

    def test_56_no_late_import_from_bot(self):
        """v4.7.22: в bot_handlers.py НЕ должно быть `from bot import SetChatSlowModeDelay`.

        v4.7.21 использовал late import — это вызывало RuntimeError: Router is
        already attached (см. test docstring класса).
        v4.7.22: класс определён локально в bot_handlers.py — late import не нужен.
        """
        src = _read_handlers()
        tree = ast.parse(src)
        # Находим все ImportFrom узлы на верхнем уровне модуля
        bad_top_level = []
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "bot":
                names = [a.name for a in node.names]
                bad_top_level.append((node.lineno, names))
        # Также ищем late imports внутри функций (через walk)
        late_imports = []
        top_level_nodes = set(id(n) for n in ast.iter_child_nodes(tree))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "bot":
                if id(node) not in top_level_nodes:
                    names = [a.name for a in node.names]
                    late_imports.append((node.lineno, names))
        self.assertEqual(
            bad_top_level, [],
            f"Top-level `from bot import ...` в bot_handlers.py вызовет circular import "
            f"или RuntimeError при повторном import bot.py. Найдено: {bad_top_level}",
        )
        # Late imports тоже запрещены — класс SetChatSlowModeDelay теперь локальный
        late_scsm_imports = [
            (line, names) for line, names in late_imports
            if "SetChatSlowModeDelay" in names
        ]
        self.assertEqual(
            late_scsm_imports, [],
            f"Late import `from bot import SetChatSlowModeDelay` в bot_handlers.py "
            f"вызовет RuntimeError: Router is already attached (bot.py запускается как "
            f"__main__, late import создаёт второй module instance). "
            f"Класс SetChatSlowModeDelay теперь определён локально в bot_handlers.py. "
            f"Найдено late imports: {late_scsm_imports}",
        )

    def test_57_bot_imports_set_chat_slow_mode_delay_from_handlers(self):
        """v4.7.22: bot.py должен импортировать SetChatSlowModeDelay из bot_handlers."""
        src = _read_bot()
        self.assertIn("SetChatSlowModeDelay", src,
                      "bot.py должен импортировать SetChatSlowModeDelay из bot_handlers")
        tree = ast.parse(src)
        found_import = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "bot_handlers":
                for alias in node.names:
                    if alias.name == "SetChatSlowModeDelay":
                        found_import = True
                        break
        self.assertTrue(
            found_import,
            "bot.py должен содержать `from bot_handlers import ... SetChatSlowModeDelay`",
        )

    def test_58_bot_uses_set_chat_slow_mode_delay_in_night_mode(self):
        """bot.py использует SetChatSlowModeDelay в _enter_night_mode и _restore_day_state."""
        src = _read_bot()
        fn_start = src.find("async def _enter_night_mode(")
        self.assertGreater(fn_start, 0, "_enter_night_mode not found in bot.py")
        fn_end = src.find("async def ", fn_start + 10)
        fn_body = src[fn_start:fn_end] if fn_end > 0 else src[fn_start:]
        self.assertIn("SetChatSlowModeDelay", fn_body,
                      "_enter_night_mode должен вызывать SetChatSlowModeDelay")
        fn_start = src.find("async def _restore_day_state(")
        self.assertGreater(fn_start, 0, "_restore_day_state not found in bot.py")
        fn_end = src.find("async def ", fn_start + 10)
        fn_body = src[fn_start:fn_end] if fn_end > 0 else src[fn_start:]
        self.assertIn("SetChatSlowModeDelay", fn_body,
                      "_restore_day_state должен вызывать SetChatSlowModeDelay")

    @unittest.skip(
        "v4.8.9: хак sys.modules.setdefault('bot', ...) удалён — вместо late import из bot.py введён service locator app_state.py (bot.py:12, app_state.py). Тест сторожил обходной путь, от которого осознанно избавились"
    )
    def test_59_bot_has_safety_net_for_late_imports(self):
        """v4.7.22: bot.py содержит safety net — sys.modules.setdefault('bot', ...).

        Без safety net late import `from bot import X` из bot_handlers.py
        вызывает повторный import bot.py → RuntimeError: Router is already attached.
        Safety net регистрирует текущий модуль под именем 'bot' в sys.modules,
        чтобы late import находил уже загруженный модуль.
        """
        src = _read_bot()
        # Проверяем наличие safety net строки
        self.assertIn(
            'sys.modules.setdefault("bot"', src,
            "bot.py должен содержать safety net: sys.modules.setdefault('bot', ...)",
        )
        # Проверяем что используется .get(__name__) для защиты от test-окружения
        # (где __name__ может быть 'bot_module' и модуля ещё нет в sys.modules)
        self.assertIn(
            "sys.modules.get(__name__)",
            src,
            "bot.py safety net должен использовать sys.modules.get(__name__) "
            "для защиты от test-окружения",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
