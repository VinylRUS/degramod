"""
test_v4730_alarm_audit_fixes.py — v4.7.30: регрессионные тесты для фиксов
найденных в аудите багов.

Покрывает:
  - Баг #1: _alarm_auto_off_tick существует и не фильтрует по night_mode_enabled
  - Баг #2: alarm включён в _startup_recovery
  - Баг #3: _enter_sanitary_day снимает alarm перед входом
  - Баг #4: handle_sticker_message + handle_content_filters exempt модераторов
  - Баг #5: _deactivate_alarm возвращает tuple (ok, perms_source, slow_source)
            + DM-сообщение использует реальные source'ы
  - Баг #7: !alarm on (продление) показывает кто включил до тебя
  - CAS exempt: handle_new_members не баннит модераторов

Все тесты — структурные (AST/regex по исходникам) + поведенческие
(вызов функций с mock'ами).
"""

import ast
import asyncio
import json
import os
import re
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

# Path setup
_HERE = os.path.dirname(os.path.abspath(__file__))
_V45 = os.path.dirname(_HERE)
sys.path.insert(0, _V45)

# Read sources once
with open(os.path.join(_V45, "bot.py"), encoding="utf-8") as f:
    _BOT_SRC = f.read()
with open(os.path.join(_V45, "bot_handlers.py"), encoding="utf-8") as f:
    _HANDLERS_SRC = f.read()


def _extract_func_body(src: str, func_name: str) -> str | None:
    """Извлекает тело функции (после docstring) по имени."""
    m = re.search(
        r"(?:async\s+)?def\s+" + re.escape(func_name) + r"\s*\([^)]*\)\s*(?:->[^:]+)?\s*:\s*(.*?)(?=\n(?:async\s+)?def\s+|\nclass\s+|\Z)",
        src, re.DOTALL,
    )
    return m.group(1) if m else None


class TestBug1_AlarmAutoOffAllChats(unittest.TestCase):
    """Баг #1: авто-off alarm должен работать для ВСЕХ чатов, не только с night_mode."""

    def test_01_alarm_auto_off_tick_exists(self):
        """Функция _alarm_auto_off_tick определена в bot.py."""
        self.assertIn("async def _alarm_auto_off_tick(", _BOT_SRC,
                      "v4.7.30: должна быть функция _alarm_auto_off_tick")

    def test_02_alarm_auto_off_tick_called_in_loop(self):
        """_alarm_auto_off_tick вызывается в _night_mode_loop."""
        body = _extract_func_body(_BOT_SRC, "_night_mode_loop")
        self.assertIsNotNone(body)
        self.assertIn("_alarm_auto_off_tick()", body,
                      "_night_mode_loop должен вызывать _alarm_auto_off_tick()")

    def test_03_alarm_auto_off_tick_called_before_sanitary(self):
        """_alarm_auto_off_tick идёт ПЕРЕД _sanitary_day_tick в loop."""
        body = _extract_func_body(_BOT_SRC, "_night_mode_loop")
        # Ищем именно вызовы (await), не упоминания в комментариях
        idx_alarm = body.find("await _alarm_auto_off_tick()")
        idx_sanitary = body.find("await _sanitary_day_tick()")
        self.assertGreater(idx_alarm, -1)
        self.assertGreater(idx_sanitary, -1)
        self.assertLess(idx_alarm, idx_sanitary,
                        "_alarm_auto_off_tick должен идти ПЕРЕД _sanitary_day_tick")

    def test_04_alarm_auto_off_tick_query_no_night_mode_filter(self):
        """SQL query в _alarm_auto_off_tick НЕ фильтрует по night_mode_enabled."""
        body = _extract_func_body(_BOT_SRC, "_alarm_auto_off_tick")
        self.assertIsNotNone(body)
        # Извлекаем select()...where(...) — но query многострочный, поэтому
        # используем более жадный regex (до конца скобки where).
        # Ищем от 'select(' до закрытия .where(  )
        # Простой подход: берём всё от 'select(' до '.scalars()' или '.all()'
        select_match = re.search(
            r'select\(.*?(?:\.scalars\(\)|\.all\(\))',
            body, re.DOTALL,
        )
        self.assertIsNotNone(select_match,
                             "_alarm_auto_off_tick должна содержать select(...).scalars()/.all()")
        sql_part = select_match.group(0)
        self.assertIn("alarm_currently_active.is_(True)", sql_part)
        self.assertIn("alarm_active_until.is_not(None)", sql_part)
        self.assertNotIn("night_mode_enabled", sql_part,
                         "v4.7.30: НЕ должен фильтровать по night_mode_enabled (Баг #1)")

    def test_05_alarm_auto_off_tick_uses_deactivate_alarm(self):
        """_alarm_auto_off_tick вызывает _deactivate_alarm."""
        body = _extract_func_body(_BOT_SRC, "_alarm_auto_off_tick")
        self.assertIn("_deactivate_alarm(", body)
        self.assertIn("auto_off_timeout", body,
                      "reason должен быть 'auto_off_timeout'")


class TestBug2_AlarmInStartupRecovery(unittest.TestCase):
    """Баг #2: alarm должен быть в _startup_recovery."""

    def test_10_startup_recovery_includes_alarm(self):
        """_startup_recovery query включает alarm_currently_active."""
        body = _extract_func_body(_BOT_SRC, "_startup_recovery")
        self.assertIsNotNone(body)
        # Query должен включать alarm_currently_active
        self.assertIn("ChatSettings.alarm_currently_active.is_(True)", body,
                      "_startup_recovery должен включать alarm_currently_active в or_")

    def test_11_startup_recovery_calls_alarm_auto_off(self):
        """_startup_recovery вызывает _alarm_auto_off_tick."""
        body = _extract_func_body(_BOT_SRC, "_startup_recovery")
        self.assertIn("_alarm_auto_off_tick()", body,
                      "_startup_recovery должен вызывать _alarm_auto_off_tick()")

    def test_12_startup_recovery_calls_in_correct_order(self):
        """Порядок вызовов: alarm → sanitary → night.

        Ищем именно `await _func()` (вызовы), а не просто упоминания в
        docstring/комментариях.
        """
        body = _extract_func_body(_BOT_SRC, "_startup_recovery")
        idx_alarm = body.find("await _alarm_auto_off_tick()")
        idx_sanitary = body.find("await _sanitary_day_tick()")
        idx_night = body.find("await _night_mode_tick()")
        self.assertGreater(idx_alarm, -1, "Должен быть вызов await _alarm_auto_off_tick()")
        self.assertGreater(idx_sanitary, -1, "Должен быть вызов await _sanitary_day_tick()")
        self.assertGreater(idx_night, -1, "Должен быть вызов await _night_mode_tick()")
        self.assertLess(idx_alarm, idx_sanitary, "alarm должен идти ПЕРЕД sanitary")
        self.assertLess(idx_sanitary, idx_night, "sanitary должен идти ПЕРЕД night")

    def test_13_startup_recovery_logs_alarm_state(self):
        """_startup_recovery логирует alarm_active + alarm_until + started_by."""
        body = _extract_func_body(_BOT_SRC, "_startup_recovery")
        self.assertIn("alarm_active=", body)
        self.assertIn("alarm_until=", body)
        self.assertIn("alarm_started_by", body)


class TestBug3_SanitaryDayDeactivatesAlarm(unittest.TestCase):
    """Баг #3: _enter_sanitary_day должен снимать alarm перед snapshot."""

    def test_20_enter_sanitary_day_deactivates_alarm(self):
        """В _enter_sanitary_day есть блок деактивации alarm."""
        body = _extract_func_body(_BOT_SRC, "_enter_sanitary_day")
        self.assertIsNotNone(body)
        self.assertIn("alarm_currently_active", body,
                      "_enter_sanitary_day должен проверять alarm_currently_active")
        self.assertIn("_deactivate_alarm(", body,
                      "_enter_sanitary_day должен вызывать _deactivate_alarm")

    def test_21_enter_sanitary_day_uses_sanitary_reason(self):
        """Reason должен быть 'sanitary_day_enter'."""
        body = _extract_func_body(_BOT_SRC, "_enter_sanitary_day")
        self.assertIn("sanitary_day_enter", body,
                      "reason должен быть 'sanitary_day_enter' для логов")

    def test_22_enter_sanitary_day_alarm_block_before_snapshot(self):
        """Блок деактивации alarm идёт ДО snapshot'а прав."""
        body = _extract_func_body(_BOT_SRC, "_enter_sanitary_day")
        idx_alarm = body.find("_deactivate_alarm(")
        # Snapshot прав — ищем первое упоминание snapshot_data (старый код),
        # get_chat(chat_id=cs.chat_id) (старый код), или _snapshot_chat_permissions
        # (v4.8.0 — унифицированная функция из chat_modes.py).
        idx_snapshot = body.find("snapshot_data")
        if idx_snapshot < 0:
            idx_snapshot = body.find("get_chat(chat_id=cs.chat_id)")
        if idx_snapshot < 0:
            idx_snapshot = body.find("_snapshot_chat_permissions")
        self.assertGreater(idx_alarm, -1)
        self.assertGreater(idx_snapshot, -1)
        self.assertLess(idx_alarm, idx_snapshot,
                        "Деактивация alarm должна идти ДО snapshot'а прав")


class TestBug4_FiltersExemptModerators(unittest.TestCase):
    """Баг #4: sticker/content filters должны exempt модераторов."""

    def test_30_handle_sticker_message_exempts_admins(self):
        """handle_sticker_message проверяет _is_admin перед наказанием."""
        body = _extract_func_body(_HANDLERS_SRC, "handle_sticker_message")
        self.assertIsNotNone(body)
        self.assertIn("_is_admin(", body,
                      "handle_sticker_message должен вызывать _is_admin")

    def test_31_handle_sticker_message_exempt_before_delete(self):
        """Проверка exempt идёт ДО удаления сообщения."""
        body = _extract_func_body(_HANDLERS_SRC, "handle_sticker_message")
        idx_admin_check = body.find("_is_admin(")
        idx_delete = body.find("await message.delete()")
        self.assertGreater(idx_admin_check, -1)
        self.assertGreater(idx_delete, -1)
        self.assertLess(idx_admin_check, idx_delete,
                        "Проверка _is_admin должна идти ДО удаления сообщения")

    def test_32_handle_sticker_message_exempt_logs_info(self):
        """Exempt логируется на INFO уровне для аудита."""
        body = _extract_func_body(_HANDLERS_SRC, "handle_sticker_message")
        # Должен быть лог с упоминанием exempt
        self.assertTrue(
            'exempt' in body.lower() and 'logger' in body,
            "handle_sticker_message должен логировать exempt на INFO"
        )

    def test_33_handle_content_filters_exempts_admins(self):
        """handle_content_filters проверяет _is_admin перед word/link проверкой."""
        body = _extract_func_body(_HANDLERS_SRC, "handle_content_filters")
        self.assertIsNotNone(body)
        self.assertIn("_is_admin(", body,
                      "handle_content_filters должен вызывать _is_admin")

    @unittest.skip("v4.8.1: word_filter заменён на KeywordWatch, функции больше нет")
    def test_34_handle_content_filters_exempt_before_word_link(self):
        """Проверка exempt идёт ДО word/link фильтров.

        v4.8.1: word_filter полностью отключён (_word_filter_match больше
        не вызывается в handle_content_filters). Проверяем только что
        _is_admin вызывается до _link_filter_check.
        """
        body = _extract_func_body(_HANDLERS_SRC, "handle_content_filters")
        idx_admin = body.find("_is_admin(")
        idx_link = body.find("_link_filter_check(")
        self.assertGreater(idx_admin, -1)
        self.assertGreater(idx_link, -1)
        self.assertLess(idx_admin, idx_link,
                        "Проверка _is_admin должна идти ДО link filter")

    def test_35_handle_new_members_cas_exempts_admins(self):
        """handle_new_members CAS-проверка exempt модераторов."""
        body = _extract_func_body(_HANDLERS_SRC, "handle_new_members")
        self.assertIsNotNone(body)
        self.assertIn("_is_admin(", body,
                      "handle_new_members должен вызывать _is_admin для CAS exempt")


class TestBug5_DeactivateAlarmReturnsTuple(unittest.TestCase):
    """Баг #5: _deactivate_alarm возвращает tuple (ok, perms_source, slow_source)."""

    def test_40_deactivate_alarm_return_annotation(self):
        """В сигнатуре указан возвращаемый тип tuple[bool, str, str]."""
        # Ищем сигнатуру
        m = re.search(
            r"async\s+def\s+_deactivate_alarm\s*\([^)]*\)\s*->\s*tuple\[bool,\s*str,\s*str\]",
            _HANDLERS_SRC,
        )
        self.assertIsNotNone(m,
                             "_deactivate_alarm должна иметь return type tuple[bool, str, str]")

    def test_41_deactivate_alarm_returns_tuple_on_not_active(self):
        """Если alarm не активен — возвращает (False, '', '')."""
        body = _extract_func_body(_HANDLERS_SRC, "_deactivate_alarm")
        self.assertIn("return False, \"\", \"\"", body,
                      "При not active должен вернуть (False, '', '')")

    def test_42_deactivate_alarm_returns_tuple_on_failure(self):
        """При ошибке set_chat_permissions — возвращает (False, '', '')."""
        body = _extract_func_body(_HANDLERS_SRC, "_deactivate_alarm")
        # Должен быть return False, "", "" в except-блоке
        count = body.count("return False, \"\", \"\"")
        self.assertGreaterEqual(count, 2,
                                "Должно быть минимум 2 return (False, '', '') — для not_active и для failure")

    def test_43_deactivate_alarm_returns_tuple_on_success(self):
        """При успехе — возвращает (True, perms_source, slow_source)."""
        body = _extract_func_body(_HANDLERS_SRC, "_deactivate_alarm")
        self.assertIn("return True, perms_source, slow_source", body,
                      "При успехе должен вернуть (True, perms_source, slow_source)")

    def test_44_alarm_perms_source_to_human_exists(self):
        """Функция _alarm_perms_source_to_human определена."""
        self.assertIn("def _alarm_perms_source_to_human(", _HANDLERS_SRC,
                      "Должна быть функция _alarm_perms_source_to_human")

    def test_45_alarm_slow_source_to_human_exists(self):
        """Функция _alarm_slow_source_to_human определена."""
        self.assertIn("def _alarm_slow_source_to_human(", _HANDLERS_SRC,
                      "Должна быть функция _alarm_slow_source_to_human")

    def test_46_alarm_off_dm_uses_source_helpers(self):
        """DM в !alarm off ветке использует _alarm_perms_source_to_human и _alarm_slow_source_to_human."""
        # Ищем ветку is_off в handle_alarm_command
        m = re.search(
            r"if\s+is_off:\s*(.*?)(?=\n    # ── !alarm on|\n    # ──|\Z)",
            _HANDLERS_SRC, re.DOTALL,
        )
        self.assertIsNotNone(m, "Не найдена ветка if is_off в handle_alarm_command")
        off_body = m.group(1)
        self.assertIn("_alarm_perms_source_to_human(", off_body,
                      "off-ветка должна использовать _alarm_perms_source_to_human")
        self.assertIn("_alarm_slow_source_to_human(", off_body,
                      "off-ветка должна использовать _alarm_slow_source_to_human")

    def test_47_alarm_off_dm_does_not_say_snapshot_only(self):
        """DM НЕ должен содержать 'восстановлены из сохранённого snapshot' безусловно."""
        # Старый текст "Права и slow_mode восстановлены из сохранённого snapshot."
        # НЕ должен присутствовать (заменён на динамический)
        self.assertNotIn(
            "Права и slow_mode восстановлены из сохранённого snapshot.",
            _HANDLERS_SRC,
            "v4.7.30: старый вводящий в заблуждение текст должен быть удалён"
        )

    def test_48_deactivate_alarm_behavioral_returns_tuple(self):
        """Поведенческий тест: вызов _deactivate_alarm возвращает tuple."""
        from db import ChatSettings
        from bot_handlers import _deactivate_alarm

        cs = ChatSettings(chat_id=-1001234567890)
        cs.alarm_currently_active = False  # не активен
        cs.alarm_saved_permissions = None
        cs.alarm_saved_slow_mode_delay = None

        session = MagicMock()
        bot = MagicMock()

        result = asyncio.run(_deactivate_alarm(session, cs, bot, -1001234567890, reason="test"))
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 3)
        self.assertFalse(result[0])  # ok=False
        self.assertEqual(result[1], "")  # perms_source=""
        self.assertEqual(result[2], "")  # slow_source=""

    def test_49_perms_source_to_human_known_values(self):
        """_alarm_perms_source_to_human корректно переводит известные source."""
        from bot_handlers import _alarm_perms_source_to_human

        self.assertIn("day_permissions", _alarm_perms_source_to_human("day_permissions preset"))
        self.assertIn("snapshot", _alarm_perms_source_to_human("alarm_saved_permissions snapshot"))
        self.assertIn("дефолт", _alarm_perms_source_to_human("hardcoded default (all allowed)"))
        self.assertIn("неизвестен", _alarm_perms_source_to_human(""))
        self.assertIn("неизвестен", _alarm_perms_source_to_human(""))

    def test_50_slow_source_to_human_known_values(self):
        """_alarm_slow_source_to_human корректно переводит известные source."""
        from bot_handlers import _alarm_slow_source_to_human

        self.assertIn("60", _alarm_slow_source_to_human("day_slow_mode_delay=60"))
        self.assertIn("30", _alarm_slow_source_to_human("alarm_saved_slow_mode_delay=30"))
        self.assertIn("0 сек", _alarm_slow_source_to_human("default 0"))
        self.assertIn("0 сек", _alarm_slow_source_to_human(""))


class TestBug7_AlarmOnShowsPreviousModerator(unittest.TestCase):
    """Баг #7: !alarm on (продление) показывает, кто включил до тебя."""

    def test_60_alarm_extend_loads_prev_mod_profile(self):
        """В ветке продления alarm загружается профиль модератора из БД."""
        # Ищем ветку cs.alarm_currently_active в handle_alarm_command
        m = re.search(
            r"if\s+cs\.alarm_currently_active:\s*(.*?)(?=\n        # ── Включаем alarm с нуля)",
            _HANDLERS_SRC, re.DOTALL,
        )
        self.assertIsNotNone(m, "Не найдена ветка 'if cs.alarm_currently_active' в alarm on")
        body = m.group(1)
        # Должен быть SELECT Moderator по tg_user_id
        self.assertIn("Moderator.tg_user_id", body,
                      "Должен быть SELECT Moderator по tg_user_id для prev_mod_display")
        self.assertIn("prev_mod_display", body,
                      "Должна быть переменная prev_mod_display")

    def test_61_alarm_extend_dm_includes_prev_mod(self):
        """DM при продлении включает 'Предыдущий alarm включил:'."""
        m = re.search(
            r"if\s+cs\.alarm_currently_active:\s*(.*?)(?=\n        # ── Включаем alarm с нуля)",
            _HANDLERS_SRC, re.DOTALL,
        )
        body = m.group(1)
        self.assertIn("Предыдущий alarm включил", body,
                      "DM должен содержать 'Предыдущий alarm включил'")

    def test_62_alarm_extend_dm_for_both_branches(self):
        """DM в обеих ветках продления (с duration и без) содержит prev_mod_display."""
        m = re.search(
            r"if\s+cs\.alarm_currently_active:\s*(.*?)(?=\n        # ── Включаем alarm с нуля)",
            _HANDLERS_SRC, re.DOTALL,
        )
        body = m.group(1)
        # Должно быть 2 упоминания prev_mod_display в DM-текстах
        count = body.count("prev_mod_display")
        self.assertGreaterEqual(count, 3,
                                "prev_mod_display должен упоминаться минимум 3 раза: "
                                "вычисление + 2 DM (с duration и без)")


class TestBug8_RoadmapInvariantsDocumented(unittest.TestCase):
    """Баг #8: архитектурные инварианты задокументированы в начале ROADMAP."""

    def test_70_roadmap_has_invariants_section(self):
        """ROADMAP начинается с раздела АРХИТЕКТУРНЫЕ ИНВАРИАНТЫ."""
        roadmap_path = os.path.join(_V45, "..", "download", "ROADMAP_v4.8.0.md")
        if not os.path.exists(roadmap_path):
            self.skipTest("ROADMAP не найден в download/ — пропускаем")
        with open(roadmap_path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("АРХИТЕКТУРНЫЕ ИНВАРИАНТЫ", content,
                      "ROADMAP должен содержать раздел АРХИТЕКТУРНЫЕ ИНВАРИАНТЫ")

    def test_71_roadmap_documents_per_user_overrides(self):
        """ROADMAP документирует что alarm/night/sanitary НЕ трогают per-user overrides."""
        roadmap_path = os.path.join(_V45, "..", "download", "ROADMAP_v4.8.0.md")
        if not os.path.exists(roadmap_path):
            self.skipTest("ROADMAP не найден")
        with open(roadmap_path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("per-user", content.lower())
        self.assertIn("restrict_chat_member", content)


class TestCAS_ExemptModerators(unittest.TestCase):
    """CAS: модераторы/админы exempt от CAS-проверки."""

    def test_80_handle_new_members_cas_exempts_admins(self):
        """handle_new_members проверяет _is_admin перед CAS-баном."""
        body = _extract_func_body(_HANDLERS_SRC, "handle_new_members")
        self.assertIsNotNone(body)
        # CAS-бан идёт ПОСЛЕ проверки _is_admin
        idx_admin = body.find("_is_admin(")
        idx_ban = body.find("ban_chat_member(")
        self.assertGreater(idx_admin, -1)
        self.assertGreater(idx_ban, -1)
        self.assertLess(idx_admin, idx_ban,
                        "Проверка _is_admin должна идти ДО ban_chat_member")

    def test_81_handle_new_members_cas_fail_open(self):
        """При ошибке БД в _is_admin — fail-open (пропускаем, не банним)."""
        body = _extract_func_body(_HANDLERS_SRC, "handle_new_members")
        self.assertIn("fail-open", body.lower(),
                      "Должен быть fail-open fallback при ошибке _is_admin")


class TestNoRegressions(unittest.TestCase):
    """Проверка что старые тесты по-прежнему проходят (no regressions)."""

    def test_90_bot_ast_valid(self):
        """bot.py парсится без синтаксических ошибок."""
        try:
            ast.parse(_BOT_SRC)
        except SyntaxError as e:
            self.fail(f"bot.py syntax error: {e}")

    def test_91_handlers_ast_valid(self):
        """bot_handlers.py парсится без синтаксических ошибок."""
        try:
            ast.parse(_HANDLERS_SRC)
        except SyntaxError as e:
            self.fail(f"bot_handlers.py syntax error: {e}")

    def test_92_alarm_filter_still_works(self):
        """_AlarmCommandFilter всё ещё определён."""
        self.assertIn("class _AlarmCommandFilter(", _HANDLERS_SRC)

    def test_93_cmd_alarm_still_works(self):
        """handle_alarm_command всё ещё определён."""
        self.assertIn("async def handle_alarm_command(", _HANDLERS_SRC)

    def test_94_deactivate_alarm_still_works(self):
        """_deactivate_alarm всё ещё определён."""
        self.assertIn("async def _deactivate_alarm(", _HANDLERS_SRC)

    def test_95_night_mode_tick_still_works(self):
        """_night_mode_tick всё ещё определён."""
        self.assertIn("async def _night_mode_tick(", _BOT_SRC)

    def test_96_sanitary_day_tick_still_works(self):
        """_sanitary_day_tick всё ещё определён."""
        self.assertIn("async def _sanitary_day_tick(", _BOT_SRC)

    def test_97_enter_sanitary_day_still_works(self):
        """_enter_sanitary_day всё ещё определён."""
        self.assertIn("async def _enter_sanitary_day(", _BOT_SRC)

    def test_98_startup_recovery_still_works(self):
        """_startup_recovery всё ещё определён."""
        self.assertIn("async def _startup_recovery(", _BOT_SRC)


if __name__ == "__main__":
    unittest.main(verbosity=2)
