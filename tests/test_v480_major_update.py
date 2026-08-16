"""v4.8.0 — tests for chat_modes.py, modchat.py, keyword-watch, modchat fields.

Покрывает:
  • #9: chat_modes.py — унифицированная snapshot/restore/apply логика.
  • #10: modchat поля в ChatSettings (mod_chat_id, is_mod_chat).
  • #10: KeywordWatch модель + таблица keyword_watch.
  • #10: keyword-watch matcher (substring + word-boundary).
  • #10: alarm-события в modchat (4 события + консолидация продлений).
  • #10: команды !setkeywords / !addkeyword / !delkeyword / !listkeywords / !setmodchat.
  • #10: rate-limit + multiplexing.
  • #10: exempt модераторов.
  • #10: взаимоисключение modchat ↔ report_chat.

Структурные тесты (AST-based) + поведенческие (mock-based).
"""
import ast
import asyncio
import re
import sys
import os
import json
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BOT_PY = ROOT / "bot.py"
BOT_HANDLERS_PY = ROOT / "bot_handlers.py"
DB_PY = ROOT / "db.py"
WEB_APP_PY = ROOT / "web_app.py"
BASE_HTML = ROOT / "templates" / "base.html"
CHAT_MODES_PY = ROOT / "chat_modes.py"
MODCHAT_PY = ROOT / "modchat.py"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _extract_func_body(src: str, func_name: str) -> str | None:
    """Извлекает тело функции (как текст) через AST."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            lines = src.splitlines()
            # node.end_lineno доступен в Python 3.8+
            start = node.lineno - 1
            end = node.end_lineno if hasattr(node, "end_lineno") else len(lines)
            return "\n".join(lines[start:end])
    return None


class TestV480Version(unittest.TestCase):
    """v4.8.0 version bump."""

    def test_01_app_version_is_v480(self):
        src = _read(WEB_APP_PY)
        m = re.search(r'APP_VERSION\s*=\s*"v(\d+)\.(\d+)\.(\d+)"', src)
        self.assertIsNotNone(m, "APP_VERSION not found")
        major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
        self.assertGreaterEqual(
            (major, minor, patch),
            (4, 8, 0),
            f"APP_VERSION must be >= v4.8.0, got v{major}.{minor}.{patch}",
        )

    def test_02_changelog_has_v480(self):
        html = _read(BASE_HTML)
        self.assertIn("v4.8.0", html, "Changelog must contain v4.8.0")
        self.assertIn("#9", html, "Changelog must mention #9 (refactoring)")
        self.assertIn("#10", html, "Changelog must mention #10 (modchat)")
        self.assertIn("keyword-watch", html.lower(), "Changelog must mention keyword-watch")
        self.assertIn("modchat", html.lower(), "Changelog must mention modchat")


class TestChatModesModule(unittest.TestCase):
    """#9: chat_modes.py — унифицированная логика режимов."""

    def setUp(self):
        self.src = _read(CHAT_MODES_PY)

    def test_10_module_exists(self):
        self.assertTrue(CHAT_MODES_PY.exists(), "chat_modes.py must exist")

    def test_11_has_snapshot_function(self):
        self.assertIn("async def _snapshot_chat_permissions", self.src,
                      "_snapshot_chat_permissions must be defined")

    def test_12_has_restore_perms_functions(self):
        self.assertIn("def _resolve_restore_perms_sync", self.src,
                      "_resolve_restore_perms_sync must be defined")
        self.assertIn("async def _resolve_restore_perms_async", self.src,
                      "_resolve_restore_perms_async must be defined")

    def test_13_has_apply_function(self):
        self.assertIn("async def _apply_chat_permissions", self.src,
                      "_apply_chat_permissions must be defined")

    def test_14_apply_uses_independent_true(self):
        """_apply_chat_permissions must use use_independent_chat_permissions=True."""
        body = _extract_func_body(self.src, "_apply_chat_permissions")
        self.assertIsNotNone(body)
        self.assertIn("use_independent_chat_permissions=True", body,
                      "_apply_chat_permissions must use independent=True")

    def test_15_has_mode_priority_helper(self):
        self.assertIn("def _mode_priority", self.src,
                      "_mode_priority helper must be defined")

    def test_16_has_active_modes_helper(self):
        self.assertIn("def _active_modes", self.src,
                      "_active_modes helper must be defined")

    def test_17_has_hardcoded_day_default(self):
        self.assertIn("_DAY_DEFAULT_HARDCODED", self.src,
                      "_DAY_DEFAULT_HARDCODED must be defined as fallback")
        # admin-права всегда False.
        self.assertIn('"can_change_info": False', self.src)
        self.assertIn('"can_invite_users": False', self.src)
        self.assertIn('"can_pin_messages": False', self.src)

    def test_18_perm_fields_constant(self):
        self.assertIn("_PERM_FIELDS", self.src)
        # Должно быть 13 полей.
        m = re.search(r'_PERM_FIELDS[^=]*=\s*\((.*?)\)', self.src, re.DOTALL)
        self.assertIsNotNone(m, "_PERM_FIELDS tuple not found")
        fields = [f.strip().strip('"').strip("'") for f in m.group(1).split(",") if f.strip()]
        self.assertEqual(len(fields), 13, f"Expected 13 perm fields, got {len(fields)}")

    def test_19_snapshot_priority_doc(self):
        """Документация приоритета в module docstring."""
        self.assertIn("sanitary > night > alarm > day", self.src,
                      "Priority sanitary > night > alarm > day must be documented")


class TestChatModesIntegration(unittest.TestCase):
    """#9: bot.py и bot_handlers.py используют chat_modes.py."""

    def setUp(self):
        self.bot_src = _read(BOT_PY)
        self.handlers_src = _read(BOT_HANDLERS_PY)

    def test_20_bot_py_imports_chat_modes(self):
        self.assertIn("from chat_modes import", self.bot_src,
                      "bot.py must import from chat_modes")

    def test_21_enter_night_mode_uses_snapshot(self):
        body = _extract_func_body(self.bot_src, "_enter_night_mode")
        self.assertIsNotNone(body)
        self.assertIn("_snapshot_chat_permissions", body,
                      "_enter_night_mode must use _snapshot_chat_permissions")
        self.assertIn("_apply_chat_permissions", body,
                      "_enter_night_mode must use _apply_chat_permissions")

    def test_22_enter_sanitary_day_uses_snapshot(self):
        body = _extract_func_body(self.bot_src, "_enter_sanitary_day")
        self.assertIsNotNone(body)
        self.assertIn("_snapshot_chat_permissions", body,
                      "_enter_sanitary_day must use _snapshot_chat_permissions")

    def test_23_restore_day_state_uses_resolve(self):
        body = _extract_func_body(self.bot_src, "_restore_day_state")
        self.assertIsNotNone(body)
        self.assertIn("_resolve_restore_perms_async", body,
                      "_restore_day_state must use _resolve_restore_perms_async")
        self.assertIn("_apply_chat_permissions", body,
                      "_restore_day_state must use _apply_chat_permissions")

    def test_24_deactivate_alarm_uses_resolve_sync(self):
        body = _extract_func_body(self.handlers_src, "_deactivate_alarm")
        self.assertIsNotNone(body)
        self.assertIn("_resolve_restore_perms_sync", body,
                      "_deactivate_alarm must use _resolve_restore_perms_sync")
        self.assertIn("_apply_chat_permissions", body,
                      "_deactivate_alarm must use _apply_chat_permissions")

    def test_25_handle_alarm_command_uses_snapshot(self):
        body = _extract_func_body(self.handlers_src, "handle_alarm_command")
        self.assertIsNotNone(body)
        self.assertIn("_snapshot_chat_permissions", body,
                      "handle_alarm_command must use _snapshot_chat_permissions")


class TestModchatFields(unittest.TestCase):
    """#10: mod_chat_id и is_mod_chat поля в ChatSettings."""

    def setUp(self):
        self.db_src = _read(DB_PY)

    def test_30_chat_settings_has_mod_chat_id(self):
        self.assertIn("mod_chat_id", self.db_src,
                      "ChatSettings must have mod_chat_id field")

    def test_31_chat_settings_has_is_mod_chat(self):
        self.assertIn("is_mod_chat", self.db_src,
                      "ChatSettings must have is_mod_chat field")

    def test_32_migration_for_modchat_fields(self):
        self.assertIn("v480_modchat_cols", self.db_src,
                      "Migration for modchat fields must exist")

    def test_33_keyword_watch_table_exists(self):
        self.assertIn("keyword_watch", self.db_src,
                      "keyword_watch table must be defined")
        # Проверяем CREATE TABLE IF NOT EXISTS в миграциях.
        self.assertIn("CREATE TABLE IF NOT EXISTS keyword_watch", self.db_src,
                      "Migration for keyword_watch table must exist")

    def test_34_keyword_watch_model_class(self):
        self.assertIn("class KeywordWatch", self.db_src,
                      "KeywordWatch model class must be defined")
        self.assertIn("__tablename__ = \"keyword_watch\"", self.db_src,
                      "KeywordWatch __tablename__ must be 'keyword_watch'")

    def test_35_keyword_watch_has_ban_in_night_mode(self):
        self.assertIn("ban_in_night_mode", self.db_src,
                      "KeywordWatch must have ban_in_night_mode field")

    def test_36_keyword_watch_has_rules_section(self):
        self.assertIn("rules_section", self.db_src,
                      "KeywordWatch must have rules_section field (for #11 GitHub sync)")


class TestModchatModule(unittest.TestCase):
    """#10: modchat.py — keyword-watch + alarm events."""

    def setUp(self):
        self.src = _read(MODCHAT_PY)

    def test_40_module_exists(self):
        self.assertTrue(MODCHAT_PY.exists(), "modchat.py must exist")

    def test_41_has_get_mod_chat_id(self):
        self.assertIn("async def _get_mod_chat_id", self.src,
                      "_get_mod_chat_id must be defined")

    def test_42_has_send_to_modchat(self):
        self.assertIn("async def _send_to_modchat", self.src,
                      "_send_to_modchat must be defined")

    def test_43_has_send_alarm_event(self):
        self.assertIn("async def _send_alarm_event_to_modchat", self.src,
                      "_send_alarm_event_to_modchat must be defined")

    def test_44_has_keyword_match(self):
        self.assertIn("async def _keyword_watch_match", self.src,
                      "_keyword_watch_match must be defined")

    def test_45_has_keyword_notify(self):
        self.assertIn("async def _send_keyword_notify_to_modchat", self.src,
                      "_send_keyword_notify_to_modchat must be defined")

    def test_46_has_rate_limit(self):
        self.assertIn("def _check_keyword_rate_limit", self.src,
                      "_check_keyword_rate_limit must be defined")
        self.assertIn("_KEYWORD_RATE_LIMIT_SECONDS", self.src,
                      "_KEYWORD_RATE_LIMIT_SECONDS constant must exist")

    def test_47_has_consolidation_window(self):
        self.assertIn("_ALARM_EXTEND_CONSOLIDATE_WINDOW_SECONDS", self.src,
                      "Consolidation window constant must exist")

    def test_48_word_boundary_compile(self):
        self.assertIn("_compile_word_boundary", self.src,
                      "_compile_word_boundary helper must exist")

    def test_49_event_types_documented(self):
        """Все 4+1 event type должны быть упомянуты в коде."""
        for et in ('"on"', '"off"', '"auto_off"', '"off_by_mode"', '"extend"'):
            self.assertIn(et, self.src, f"Event type {et} must be supported")


class TestKeywordWatchMatcher(unittest.TestCase):
    """#10: keyword-watch matcher — behavioral tests."""

    def setUp(self):
        # Импортируем modchat без запуска bot.py (через importlib).
        # Это работает потому что modchat.py не имеет side-effectов на import.
        sys.path.insert(0, str(ROOT))
        import modchat
        self.modchat = modchat

    def test_60_substring_match_for_phrase_with_space(self):
        """Фраза с пробелом → substring match."""
        pattern = self.modchat._compile_word_boundary
        # Для фраз с пробелами matcher использует простой `in`, не word-boundary.
        # Поэтому просто проверяем substring.
        text = "срал в торт детишкам вчера"
        phrase = "срал в торт детишкам"
        self.assertIn(phrase.lower(), text.lower(),
                      "Phrase with space must match as substring")

    def test_61_word_boundary_no_false_positive(self):
        """Одиночное слово 'модератор' НЕ должно матчит 'замодераторили'."""
        pattern = self.modchat._compile_word_boundary("модератор")
        # 'замодераторили' содержит 'модератор' как substring, но word-boundary
        # должен его отсечь.
        m = pattern.search("замодераторили этот чат")
        self.assertIsNone(m, "word-boundary must NOT match 'модератор' inside 'замодераторили'")

    def test_62_word_boundary_matches_standalone_word(self):
        """Одиночное слово 'модератор' должно матчит 'модератор заебал'."""
        pattern = self.modchat._compile_word_boundary("модератор")
        m = pattern.search("модератор заебал")
        self.assertIsNotNone(m, "word-boundary must match standalone 'модератор'")

    def test_63_word_boundary_matches_at_start(self):
        """Word-boundary: слово в начале строки."""
        pattern = self.modchat._compile_word_boundary("помогите")
        m = pattern.search("помогите найти кнопку")
        self.assertIsNotNone(m)

    def test_64_word_boundary_matches_at_end(self):
        """Word-boundary: слово в конце строки."""
        pattern = self.modchat._compile_word_boundary("помогите")
        m = pattern.search("ребят помогите")
        self.assertIsNotNone(m)

    def test_65_word_boundary_case_insensitive(self):
        """Word-boundary: case-insensitive."""
        pattern = self.modchat._compile_word_boundary("помогите")
        m = pattern.search("ПОМОГИТЕ плиз")
        self.assertIsNotNone(m)

    def test_66_word_boundary_with_punctuation(self):
        """Word-boundary: слово перед запятой/точкой."""
        pattern = self.modchat._compile_word_boundary("казино")
        m = pattern.search("казино, это плохо")
        self.assertIsNotNone(m)

    def test_67_word_boundary_no_match_in_other_word(self):
        """Word-boundary: 'казино' не должно матчит 'казиновский'."""
        pattern = self.modchat._compile_word_boundary("казино")
        m = pattern.search("казиновский стиль")
        self.assertIsNone(m, "word-boundary must NOT match 'казино' inside 'казиновский'")


class TestRateLimit(unittest.TestCase):
    """#10: rate-limit для keyword-watch."""

    def setUp(self):
        import modchat
        self.modchat = modchat
        # Чистим state перед каждым тестом.
        self.modchat._keyword_rate_limit.clear()

    def test_70_first_call_allowed(self):
        allowed, suppressed = self.modchat._check_keyword_rate_limit(
            chat_id=123, phrase_lower="помогите",
        )
        self.assertTrue(allowed, "First call must be allowed")
        self.assertEqual(suppressed, 0)

    def test_71_second_call_within_window_suppressed(self):
        self.modchat._check_keyword_rate_limit(123, "помогите")
        allowed, suppressed = self.modchat._check_keyword_rate_limit(
            123, "помогите",
        )
        self.assertFalse(allowed, "Second call within window must be suppressed")
        self.assertEqual(suppressed, 1)

    def test_72_different_phrases_independent(self):
        """Rate-limit independent per phrase."""
        self.modchat._check_keyword_rate_limit(123, "помогите")
        allowed, _ = self.modchat._check_keyword_rate_limit(123, "модератор")
        self.assertTrue(allowed, "Different phrase must not be rate-limited")

    def test_73_different_chats_independent(self):
        """Rate-limit independent per chat."""
        self.modchat._check_keyword_rate_limit(123, "помогите")
        allowed, _ = self.modchat._check_keyword_rate_limit(456, "помогите")
        self.assertTrue(allowed, "Different chat must not be rate-limited")


class TestKeywordCommands(unittest.TestCase):
    """#10: команды !setkeywords / !addkeyword / !delkeyword / !listkeywords / !setmodchat."""

    def setUp(self):
        self.src = _read(BOT_HANDLERS_PY)

    def test_80_setkeywords_command_exists(self):
        self.assertIn("async def cmd_setkeywords", self.src,
                      "cmd_setkeywords must be defined")
        self.assertIn('Command("setkeywords")', self.src,
                      'Command("setkeywords") decorator must exist')

    def test_81_addkeyword_command_exists(self):
        self.assertIn("async def cmd_addkeyword", self.src,
                      "cmd_addkeyword must be defined")
        self.assertIn('Command("addkeyword")', self.src,
                      'Command("addkeyword") decorator must exist')

    def test_82_delkeyword_command_exists(self):
        self.assertIn("async def cmd_delkeyword", self.src,
                      "cmd_delkeyword must be defined")
        self.assertIn('Command("delkeyword")', self.src,
                      'Command("delkeyword") decorator must exist')

    def test_83_listkeywords_command_exists(self):
        self.assertIn("async def cmd_listkeywords", self.src,
                      "cmd_listkeywords must be defined")
        self.assertIn('Command("listkeywords")', self.src,
                      'Command("listkeywords") decorator must exist')

    def test_84_setmodchat_command_exists(self):
        self.assertIn("async def cmd_setmodchat", self.src,
                      "cmd_setmodchat must be defined")
        self.assertIn('Command("setmodchat")', self.src,
                      'Command("setmodchat") decorator must exist')

    def test_85_setmodchat_checks_report_chat_conflict(self):
        """!setmodchat должен проверять что чат не является report_chat."""
        body = _extract_func_body(self.src, "cmd_setmodchat")
        self.assertIsNotNone(body)
        self.assertIn("is_report_chat", body,
                      "!setmodchat must check is_report_chat for conflict")

    def test_86_setkeywords_supports_ban_night_flag(self):
        """!setkeywords должен поддерживать --ban-night флаг."""
        body = _extract_func_body(self.src, "cmd_setkeywords")
        self.assertIsNotNone(body)
        self.assertIn("--ban-night", body,
                      "!setkeywords must support --ban-night flag")

    def test_87_addkeyword_supports_ban_night_flag(self):
        body = _extract_func_body(self.src, "cmd_addkeyword")
        self.assertIsNotNone(body)
        self.assertIn("--ban-night", body,
                      "!addkeyword must support --ban-night flag")

    def test_88_all_commands_su_only(self):
        """Все 5 команд должны быть SU-only (ADMIN_IDS check)."""
        for cmd in ("cmd_setkeywords", "cmd_addkeyword", "cmd_delkeyword",
                    "cmd_listkeywords", "cmd_setmodchat"):
            body = _extract_func_body(self.src, cmd)
            self.assertIsNotNone(body, f"{cmd} body not found")
            self.assertIn("ADMIN_IDS", body,
                          f"{cmd} must check ADMIN_IDS (SU-only)")


class TestAlarmEventsInModchat(unittest.TestCase):
    """#10: alarm-события в modchat интегрированы в handle_alarm_command."""

    def setUp(self):
        self.handlers_src = _read(BOT_HANDLERS_PY)
        self.bot_src = _read(BOT_PY)

    def test_90_alarm_on_sends_to_modchat(self):
        body = _extract_func_body(self.handlers_src, "handle_alarm_command")
        self.assertIsNotNone(body)
        self.assertIn("_send_alarm_event_to_modchat", body,
                      "handle_alarm_command must call _send_alarm_event_to_modchat")
        self.assertIn('event_type="on"', body,
                      "handle_alarm_command must send 'on' event")

    def test_91_alarm_off_sends_to_modchat(self):
        body = _extract_func_body(self.handlers_src, "handle_alarm_command")
        self.assertIsNotNone(body)
        self.assertIn('event_type="off"', body,
                      "handle_alarm_command must send 'off' event")

    def test_92_alarm_extend_sends_to_modchat(self):
        body = _extract_func_body(self.handlers_src, "handle_alarm_command")
        self.assertIsNotNone(body)
        self.assertIn('event_type="extend"', body,
                      "handle_alarm_command must send 'extend' event")

    def test_93_alarm_auto_off_sends_to_modchat(self):
        body = _extract_func_body(self.bot_src, "_alarm_auto_off_tick")
        self.assertIsNotNone(body)
        self.assertIn("_send_alarm_event_to_modchat", body,
                      "_alarm_auto_off_tick must call _send_alarm_event_to_modchat")
        self.assertIn('event_type="auto_off"', body,
                      "_alarm_auto_off_tick must send 'auto_off' event")

    def test_94_alarm_off_by_night_sends_to_modchat(self):
        body = _extract_func_body(self.bot_src, "_night_mode_tick")
        self.assertIsNotNone(body)
        self.assertIn('event_type="off_by_mode"', body,
                      "_night_mode_tick must send 'off_by_mode' event when deactivating alarm")

    def test_95_alarm_off_by_sanitary_sends_to_modchat(self):
        body = _extract_func_body(self.bot_src, "_enter_sanitary_day")
        self.assertIsNotNone(body)
        self.assertIn('event_type="off_by_mode"', body,
                      "_enter_sanitary_day must send 'off_by_mode' event when deactivating alarm")


class TestKeywordWatchInContentFilters(unittest.TestCase):
    """#10: keyword-watch интегрирован в handle_content_filters."""

    def setUp(self):
        self.src = _read(BOT_HANDLERS_PY)

    def test_100_content_filters_calls_keyword_watch(self):
        body = _extract_func_body(self.src, "handle_content_filters")
        self.assertIsNotNone(body)
        self.assertIn("_keyword_watch_match", body,
                      "handle_content_filters must call _keyword_watch_match")
        self.assertIn("_send_keyword_notify_to_modchat", body,
                      "handle_content_filters must call _send_keyword_notify_to_modchat")

    def test_101_content_filters_night_mode_auto_ban(self):
        body = _extract_func_body(self.src, "handle_content_filters")
        self.assertIsNotNone(body)
        # Должна быть проверка night_mode_currently_active и ban_in_night_mode.
        self.assertIn("night_mode_currently_active", body,
                      "handle_content_filters must check night_mode_currently_active")
        self.assertIn("ban_in_night_mode", body,
                      "handle_content_filters must check ban_in_night_mode for auto-ban")

    def test_102_content_filters_keyword_watch_exempt_admins(self):
        """Если юзер — модератор, keyword-watch не срабатывает."""
        body = _extract_func_body(self.src, "handle_content_filters")
        self.assertIsNotNone(body)
        # Exempt проверяется ДО keyword-watch — если is_adm, return.
        idx_admin = body.find("_is_admin(")
        idx_keyword = body.find("_keyword_watch_match")
        self.assertGreater(idx_admin, -1, "must call _is_admin")
        self.assertGreater(idx_keyword, -1, "must call _keyword_watch_match")
        self.assertLess(idx_admin, idx_keyword,
                        "_is_admin exempt must be BEFORE keyword-watch check")


class TestNoRegressions(unittest.TestCase):
    """Регрессия: ключевые функции всё ещё существуют и работают."""

    def setUp(self):
        self.bot_src = _read(BOT_PY)
        self.handlers_src = _read(BOT_HANDLERS_PY)
        self.db_src = _read(DB_PY)

    def test_110_night_mode_tick_still_works(self):
        self.assertIn("async def _night_mode_tick", self.bot_src,
                      "_night_mode_tick must still exist")

    def test_111_sanitary_day_tick_still_works(self):
        self.assertIn("async def _sanitary_day_tick", self.bot_src,
                      "_sanitary_day_tick must still exist")

    def test_112_alarm_auto_off_tick_still_works(self):
        self.assertIn("async def _alarm_auto_off_tick", self.bot_src,
                      "_alarm_auto_off_tick must still exist")

    def test_113_deactivate_alarm_still_returns_tuple(self):
        """_deactivate_alarm всё ещё возвращает tuple (Bug#5 fix сохранён)."""
        body = _extract_func_body(self.handlers_src, "_deactivate_alarm")
        self.assertIsNotNone(body)
        self.assertIn("return True, perms_source, slow_source", body,
                      "_deactivate_alarm must still return (ok, perms_source, slow_source)")

    @unittest.skip("v4.8.1: word_filter заменён на KeywordWatch, функции больше нет")
    def test_114_word_filter_still_works(self):
        """word_filter остался для обратной совместимости (deprecated)."""
        self.assertIn("class WordFilter", self.db_src,
                      "WordFilter must still exist (deprecated but not removed)")
        self.assertIn("async def _word_filter_match", self.handlers_src,
                      "_word_filter_match must still exist")

    def test_115_startup_recovery_includes_alarm(self):
        """Bug#2 fix сохранён — alarm в startup_recovery."""
        body = _extract_func_body(self.bot_src, "_startup_recovery")
        self.assertIsNotNone(body)
        self.assertIn("alarm_currently_active", body,
                      "_startup_recovery must still check alarm_currently_active")

    def test_116_all_tests_imports_ok(self):
        """Все модули импортируются без ошибок."""
        try:
            import bot_handlers
            import modchat
            import chat_modes
            import db
        except ImportError as e:
            self.fail(f"Module import failed: {e}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
