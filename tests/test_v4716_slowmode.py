"""
v4.7.16 — тесты slow_mode в пресетах прав и night mode.

Что добавлено:
  • PermissionPreset.slow_mode_delay (nullable Integer) — None/0/>0
  • ChatSettings: day_slow_mode_delay, night_mode_slow_mode_delay,
    night_mode_saved_slow_mode_delay (копии из пресета + snapshot)
  • bot.py: _enter_night_mode применяет slow_mode + сохраняет snapshot;
    _restore_day_state восстанавливает slow_mode (preset > snapshot > skip)
  • bot_handlers.py: подкоманда /nightmode chat_id slowmode <day> <night> | off
  • web_app.py: admin_presets_create принимает slow_mode_delay;
    admin_chats_update копирует slow_mode из пресета в ChatSettings
  • templates/admin_presets.html: поле slow_mode_delay в форме создания
  • templates/admin_chats.html: 🐌 badge в dropdown night-пресетов

Тесты:
  1. APP_VERSION = "v4.7.16"
  2. APP_RELEASE_DATE = "2026-08-04"
  3. PermissionPreset.slow_mode_delay колонка существует (nullable)
  4. ChatSettings: 3 новые колонки существуют
  5. Миграция: идемпотентна (повторный init_db не ломает)
  6. SetChatSlowModeDelay класс: __api_method__ = 'setChatSlowModeDelay'
  7. SetChatSlowModeDelay: __returning__ = bool
  8. SetChatSlowModeDelay: инстанцируется с chat_id + slow_mode_delay
  9. _enter_night_mode: содержит вызов SetChatSlowModeDelay
 10. _enter_night_mode: сохраняет night_mode_saved_slow_mode_delay
 11. _enter_night_mode: читает slow_mode_delay из chat_info ПЕРЕД изменениями
 12. _restore_day_state: содержит вызов SetChatSlowModeDelay
 13. _restore_day_state: приоритет day_preset > snapshot > skip
 14. _exit_night_mode: чистит night_mode_saved_slow_mode_delay ПОСЛЕ restore
 15. /nightmode slowmode: подкоманда парсится (show / set / off)
 16. /nightmode slowmode: валидация 0..36400
 17. /nightmode slowmode: help-текст упоминает slowmode
 18. admin_presets_create: принимает slow_mode_delay Form field
 19. admin_presets_create: валидация int + 0..36400
 20. admin_presets_create: сохраняет slow_mode_delay в PermissionPreset
 21. admin_chats_update: _resolve_perms возвращает (perms, slow_mode)
 22. admin_chats_update: копирует slow_mode в ChatSettings
 23. admin_presets.html: содержит поле slow_mode_delay
 24. admin_presets.html: badge slow_mode в списке пресетов
 25. admin_chats.html: badge 🐌 в dropdown night-пресетов
 26. base.html: changelog v4.7.16 присутствует
 27. base.html: v4.7.16 упоминает slow_mode
 28. base.html: v4.7.15 сохранена (регрессия)
 29. base.html: v4.7.16 идёт ВЫШЕ v4.7.15
"""

import os
import re
import sys
import unittest
from _version import ver  # noqa: E402  (сравнение версий как кортежей, не строк)
import asyncio
import tempfile

# ── Пути ────────────────────────────────────────────────────────────────────
PROJECT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

import importlib.util

# Импортируем web_app для APP_VERSION
spec = importlib.util.spec_from_file_location(
    "web_app", os.path.join(PROJECT_DIR, "web_app.py")
)
web_app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(web_app)
APP_VERSION = web_app.APP_VERSION
APP_RELEASE_DATE = web_app.APP_RELEASE_DATE

# Импортируем db для проверки schema
spec_db = importlib.util.spec_from_file_location(
    "db", os.path.join(PROJECT_DIR, "db.py")
)
db_module = importlib.util.module_from_spec(spec_db)
spec_db.loader.exec_module(db_module)

# Импортируем bot_handlers для проверки подкоманды
import aiogram  # noqa: F401
spec_bh = importlib.util.spec_from_file_location(
    "bot_handlers", os.path.join(PROJECT_DIR, "bot_handlers.py")
)
bh = importlib.util.module_from_spec(spec_bh)
spec_bh.loader.exec_module(bh)

# Импортируем bot для проверки _enter_night_mode / _restore_day_state / SetChatSlowModeDelay
spec_bot = importlib.util.spec_from_file_location(
    "bot_module", os.path.join(PROJECT_DIR, "bot.py")
)
bot_module = importlib.util.module_from_spec(spec_bot)
spec_bot.loader.exec_module(bot_module)

BOT_PY = os.path.join(PROJECT_DIR, "bot.py")
BOT_HANDLERS_PY = os.path.join(PROJECT_DIR, "bot_handlers.py")
WEB_APP_PY = os.path.join(PROJECT_DIR, "web_app.py")
# v4.9.0 (Task 10): admin_presets_create переехал из web_app.py в
# web/admin_presets.py.
ADMIN_PRESETS_PY = os.path.join(PROJECT_DIR, "web", "admin_presets.py")
# v4.9.0 (Task 11): admin_chats_update (и вложенный хелпер _resolve_perms)
# переехал из web_app.py в web/admin_chats.py.
ADMIN_CHATS_PY = os.path.join(PROJECT_DIR, "web", "admin_chats.py")
DB_PY = os.path.join(PROJECT_DIR, "db.py")
BASE_HTML = os.path.join(PROJECT_DIR, "templates", "base.html")
ADMIN_PRESETS_HTML = os.path.join(PROJECT_DIR, "templates", "admin_presets.html")
ADMIN_CHATS_HTML = os.path.join(PROJECT_DIR, "templates", "admin_chats.html")


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# ─── Tests ──────────────────────────────────────────────────────────────────


class TestV4716SlowMode(unittest.TestCase):
    """v4.7.16: slow_mode в пресетах + night mode integration."""

    def setUp(self):
        self.bot_py = _read(BOT_PY)
        self.bot_handlers_py = _read(BOT_HANDLERS_PY)
        self.web_app_py = _read(WEB_APP_PY)
        self.admin_presets_py = _read(ADMIN_PRESETS_PY)
        self.admin_chats_py = _read(ADMIN_CHATS_PY)
        self.db_py = _read(DB_PY)
        self.base_html = _read(BASE_HTML)
        self.admin_presets_html = _read(ADMIN_PRESETS_HTML)
        self.admin_chats_html = _read(ADMIN_CHATS_HTML)

    # ─── 1-2. Version ──────────────────────────────────────────────────

    def test_01_app_version(self):
        # v4.10.0: FIX сравнение строк ломалось на двузначном minor
        # ("v4.10.0" < "v4.7.x" лексикографически) — сравниваем через ver().
        self.assertGreaterEqual(ver(APP_VERSION), ver("v4.7.16"),
            f"APP_VERSION={APP_VERSION} should be >= v4.7.16")

    def test_02_app_release_date(self):
        self.assertGreaterEqual(APP_RELEASE_DATE, "2026-08-04")

    # ─── 3-5. DB schema + migration ───────────────────────────────────

    def test_03_permission_preset_slow_mode_delay_column(self):
        """PermissionPreset.slow_mode_delay должен быть nullable Integer."""
        col = db_module.PermissionPreset.__table__.columns.get("slow_mode_delay")
        self.assertIsNotNone(col, "slow_mode_delay column missing from PermissionPreset")
        self.assertTrue(col.nullable, "slow_mode_delay should be nullable (None = не менять)")

    def test_04_chat_settings_slow_mode_columns(self):
        """ChatSettings: 3 новые колонки для slow_mode."""
        for col_name in (
            "day_slow_mode_delay",
            "night_mode_slow_mode_delay",
            "night_mode_saved_slow_mode_delay",
        ):
            col = db_module.ChatSettings.__table__.columns.get(col_name)
            self.assertIsNotNone(col, f"{col_name} missing from ChatSettings")

    def test_05_migration_idempotent(self):
        """Миграция должна быть идемпотентной — повторный init_db не падает."""
        # Use temp DB. Reload db module to pick up new DB_PATH.
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(path)
        old_db_path = os.environ.get("DB_PATH")
        os.environ["DB_PATH"] = path
        try:
            # Re-import db module fresh with new DB_PATH
            import importlib.util as _ilu
            spec = _ilu.spec_from_file_location("db_test", os.path.join(PROJECT_DIR, "db.py"))
            db_test = _ilu.module_from_spec(spec)
            spec.loader.exec_module(db_test)
            # First init — creates tables + adds columns
            asyncio.run(db_test.init_db())
            # Second init — should be no-op (idempotent)
            asyncio.run(db_test.init_db())
            # Verify columns exist
            from sqlalchemy import text
            async def check():
                async with db_test.engine.begin() as conn:
                    res = await conn.execute(text("PRAGMA table_info(chat_settings)"))
                    cs_cols = [row[1] for row in res.fetchall()]
                    res2 = await conn.execute(text("PRAGMA table_info(permission_presets)"))
                    pp_cols = [row[1] for row in res2.fetchall()]
                await db_test.engine.dispose()
                return cs_cols, pp_cols
            cs_cols, pp_cols = asyncio.run(check())
            for c in ("day_slow_mode_delay", "night_mode_slow_mode_delay",
                      "night_mode_saved_slow_mode_delay"):
                self.assertIn(c, cs_cols, f"{c} missing after re-init")
            self.assertIn("slow_mode_delay", pp_cols,
                          "slow_mode_delay missing from permission_presets after re-init")
        finally:
            if old_db_path is not None:
                os.environ["DB_PATH"] = old_db_path
            else:
                os.environ.pop("DB_PATH", None)
            if os.path.exists(path):
                os.unlink(path)

    # ─── 6-8. SetChatSlowModeDelay class ──────────────────────────────

    def test_06_set_chat_slow_mode_delay_api_method(self):
        """SetChatSlowModeDelay.__api_method__ = 'setChatSlowModeDelay'."""
        cls = bot_module.SetChatSlowModeDelay
        self.assertEqual(cls.__api_method__, "setChatSlowModeDelay")

    def test_07_set_chat_slow_mode_delay_returning(self):
        """SetChatSlowModeDelay.__returning__ = bool."""
        cls = bot_module.SetChatSlowModeDelay
        self.assertEqual(cls.__returning__, bool)

    def test_08_set_chat_slow_mode_delay_instantiation(self):
        """SetChatSlowModeDelay инстанцируется с chat_id + slow_mode_delay."""
        m = bot_module.SetChatSlowModeDelay(chat_id=-100123456789, slow_mode_delay=30)
        self.assertEqual(m.chat_id, -100123456789)
        self.assertEqual(m.slow_mode_delay, 30)

    # ─── 9-11. _enter_night_mode ──────────────────────────────────────

    def test_09_enter_night_mode_calls_set_chat_slow_mode_delay(self):
        """_enter_night_mode должен вызывать SetChatSlowModeDelay если night_slow > 0."""
        # Find _enter_night_mode function body
        src = self.bot_py
        # Look for SetChatSlowModeDelay call inside _enter_night_mode
        # Find the function start
        fn_start = src.find("async def _enter_night_mode(")
        self.assertGreater(fn_start, 0, "_enter_night_mode not found")
        # Find next async def (end of function)
        fn_end = src.find("async def ", fn_start + 10)
        fn_body = src[fn_start:fn_end] if fn_end > 0 else src[fn_start:]
        self.assertIn("SetChatSlowModeDelay", fn_body,
                      "_enter_night_mode должен вызывать SetChatSlowModeDelay")

    def test_10_enter_night_mode_saves_snapshot(self):
        """_enter_night_mode должен сохранять night_mode_saved_slow_mode_delay."""
        src = self.bot_py
        fn_start = src.find("async def _enter_night_mode(")
        fn_end = src.find("async def ", fn_start + 10)
        fn_body = src[fn_start:fn_end] if fn_end > 0 else src[fn_start:]
        self.assertIn("night_mode_saved_slow_mode_delay", fn_body,
                      "_enter_night_mode должен сохранять night_mode_saved_slow_mode_delay")

    def test_11_enter_night_mode_reads_slow_mode_before_changes(self):
        """_enter_night_mode должен читать current_slow_mode ДО set_chat_permissions."""
        src = self.bot_py
        fn_start = src.find("async def _enter_night_mode(")
        fn_end = src.find("async def ", fn_start + 10)
        fn_body = src[fn_start:fn_end] if fn_end > 0 else src[fn_start:]
        # current_slow_mode must be assigned BEFORE set_chat_permissions call
        # v4.8.0: прямой bot.set_chat_permissions заменён на обёртку
        # _apply_chat_permissions — она централизует
        # use_independent_chat_permissions=True (инвариант режимов чата).
        # Ищем оба варианта: важен порядок операций, а не имя вызова.
        idx_read = fn_body.find("current_slow_mode")
        idx_set_perms = fn_body.find("await _apply_chat_permissions")
        if idx_set_perms < 0:
            idx_set_perms = fn_body.find("await bot.set_chat_permissions")
        self.assertGreater(idx_read, 0, "current_slow_mode assignment not found")
        self.assertGreater(idx_set_perms, 0,
                           "chat permissions call not found "
                           "(_apply_chat_permissions / set_chat_permissions)")
        self.assertLess(idx_read, idx_set_perms,
                        "current_slow_mode должен читаться ДО set_chat_permissions")

    # ─── 12-13. _restore_day_state ────────────────────────────────────

    def test_12_restore_day_state_calls_set_chat_slow_mode_delay(self):
        """_restore_day_state должен вызывать SetChatSlowModeDelay."""
        src = self.bot_py
        fn_start = src.find("async def _restore_day_state(")
        self.assertGreater(fn_start, 0, "_restore_day_state not found")
        fn_end = src.find("async def ", fn_start + 10)
        fn_body = src[fn_start:fn_end] if fn_end > 0 else src[fn_start:]
        self.assertIn("SetChatSlowModeDelay", fn_body,
                      "_restore_day_state должен вызывать SetChatSlowModeDelay")

    def test_13_restore_day_state_priority_day_preset_over_snapshot(self):
        """_restore_day_state: day_slow > 0 имеет приоритет над saved_slow."""
        src = self.bot_py
        fn_start = src.find("async def _restore_day_state(")
        fn_end = src.find("async def ", fn_start + 10)
        fn_body = src[fn_start:fn_end] if fn_end > 0 else src[fn_start:]
        # Look for priority logic: day_slow checked first, then saved_slow
        idx_day = fn_body.find("day_slow = int(cs.day_slow_mode_delay")
        idx_saved = fn_body.find("saved_slow = cs.night_mode_saved_slow_mode_delay")
        idx_day_branch = fn_body.find("if day_slow > 0:", idx_day)
        idx_saved_branch = fn_body.find("elif saved_slow is not None:", idx_saved)
        self.assertGreater(idx_day, 0, "day_slow assignment not found")
        self.assertGreater(idx_saved, 0, "saved_slow assignment not found")
        self.assertGreater(idx_day_branch, 0, "if day_slow > 0 branch not found")
        self.assertGreater(idx_saved_branch, 0, "elif saved_slow branch not found")
        self.assertLess(idx_day_branch, idx_saved_branch,
                        "day_preset должен проверяться ПЕРВЫМ (приоритет)")

    # ─── 14. _exit_night_mode ─────────────────────────────────────────

    def test_14_exit_night_mode_clears_snapshot_after_restore(self):
        """_exit_night_mode: чистит night_mode_saved_slow_mode_delay ПОСЛЕ restore."""
        src = self.bot_py
        fn_start = src.find("async def _exit_night_mode(")
        fn_end = src.find("async def ", fn_start + 10)
        fn_body = src[fn_start:fn_end] if fn_end > 0 else src[fn_start:]
        # Find the cleanup of night_mode_saved_slow_mode_delay
        idx_clear = fn_body.find("db_cs.night_mode_saved_slow_mode_delay = None")
        self.assertGreater(idx_clear, 0,
                          "night_mode_saved_slow_mode_delay cleanup not found")
        # Find _restore_day_state call
        idx_restore = fn_body.find("await _restore_day_state(cs)")
        self.assertGreater(idx_restore, 0, "_restore_day_state call not found")
        self.assertGreater(idx_clear, idx_restore,
                           "snapshot должен чиститься ПОСЛЕ _restore_day_state (чтобы fallback работал)")

    # ─── 15-17. /nightmode slowmode subcommand ────────────────────────

    def test_15_nightmode_slowmode_subcommand_exists(self):
        """Подкоманда /nightmode slowmode должна существовать в bot_handlers.py."""
        # Find subcommand handler
        self.assertIn('arg2 == "slowmode"', self.bot_handlers_py,
                      "Подкоманда slowmode не найдена")

    def test_16_nightmode_slowmode_validation_0_to_36400(self):
        """Валидация: 0..36400 сек."""
        # Look for the validation in the slowmode subcommand handler
        idx = self.bot_handlers_py.find('arg2 == "slowmode"')
        self.assertGreater(idx, 0, "slowmode subcommand not found")
        # Get a chunk after the subcommand start
        chunk = self.bot_handlers_py[idx:idx+4000]
        self.assertIn("36400", chunk, "Telegram limit 36400 not enforced")
        # Should reject negative
        self.assertIn("v < 0", chunk, "negative check missing")

    def test_17_nightmode_help_mentions_slowmode(self):
        """Help-текст /nightmode должен упоминать slowmode."""
        # The help text is in the reply when parts < 3
        self.assertIn("slowmode", self.bot_handlers_py.lower())

    # ─── 18-20. admin_presets_create ──────────────────────────────────

    # v4.9.0 (Task 10): admin_presets_create переехал из web_app.py в
    # web/admin_presets.py — тесты 18-20 читают новый файл. Функция теперь
    # объявлена на верхнем уровне модуля (не внутри create_app()), поэтому
    # закрывающая скобка сигнатуры — "):" без отступа (была "    ):" при
    # 4-пробельном отступе вложенной функции).

    def test_18_admin_presets_create_accepts_slow_mode_delay(self):
        """admin_presets_create должен принимать Form field slow_mode_delay."""
        # Find the function
        idx = self.admin_presets_py.find("async def admin_presets_create(")
        self.assertGreater(idx, 0, "admin_presets_create not found")
        # Find the end of the function signature (next def or return)
        fn_end = self.admin_presets_py.find("):",
        idx) + len("):")
        fn_sig = self.admin_presets_py[idx:fn_end]
        self.assertIn("slow_mode_delay", fn_sig,
                      "slow_mode_delay Form field missing in admin_presets_create signature")

    def test_19_admin_presets_create_validates_int_and_range(self):
        """Валидация: int + 0..36400."""
        idx = self.admin_presets_py.find("async def admin_presets_create(")
        # Get a chunk of the function body
        chunk = self.admin_presets_py[idx:idx+5000]
        self.assertIn("int(slow_mode_raw)", chunk, "int parsing missing")
        self.assertIn("36400", chunk, "range check 36400 missing")

    def test_20_admin_presets_create_saves_slow_mode_delay(self):
        """admin_presets_create должен сохранять slow_mode_delay в PermissionPreset."""
        idx = self.admin_presets_py.find("async def admin_presets_create(")
        chunk = self.admin_presets_py[idx:idx+6000]
        # Look for PermissionPreset(...) constructor with slow_mode_delay
        self.assertIn("slow_mode_delay=slow_mode_value", chunk,
                      "slow_mode_delay не передаётся в PermissionPreset constructor")

    # ─── 21-22. admin_chats_update ────────────────────────────────────

    def test_21_resolve_perms_returns_tuple_with_slow_mode(self):
        """_resolve_perms должен возвращать (permissions_json, slow_mode_delay).

        v4.9.0 (Task 11): _resolve_perms живёт внутри admin_chats_update,
        которая переехала из web_app.py в web/admin_chats.py.
        """
        idx = self.admin_chats_py.find("def _resolve_perms(")
        self.assertGreater(idx, 0, "_resolve_perms not found")
        chunk = self.admin_chats_py[idx:idx+1500]
        # Return type annotation or return statements
        self.assertIn("slow_mode_delay", chunk,
                      "_resolve_perms должен возвращать slow_mode_delay")
        # Should return tuple
        self.assertTrue(
            "tuple[str | None, int | None]" in chunk or "return None, None" in chunk,
            "_resolve_perms должен возвращать tuple"
        )

    def test_22_admin_chats_update_copies_slow_mode_to_chat_settings(self):
        """admin_chats_update должен копировать slow_mode из пресета в ChatSettings.

        v4.9.0 (Task 11): роут переехал из web_app.py в web/admin_chats.py.
        """
        # Find the save block
        idx = self.admin_chats_py.find("cs.day_slow_mode_delay = ")
        self.assertGreater(idx, 0, "cs.day_slow_mode_delay assignment not found")
        idx2 = self.admin_chats_py.find("cs.night_mode_slow_mode_delay = ")
        self.assertGreater(idx2, 0, "cs.night_mode_slow_mode_delay assignment not found")

    # ─── 23-25. Templates ─────────────────────────────────────────────

    def test_23_admin_presets_html_has_slow_mode_field(self):
        """admin_presets.html должен содержать поле slow_mode_delay в форме."""
        self.assertIn("slow_mode_delay", self.admin_presets_html,
                      "Поле slow_mode_delay отсутствует в admin_presets.html")
        # Should be a number input
        self.assertIn('name="slow_mode_delay"', self.admin_presets_html,
                      "name=slow_mode_delay input не найден")

    def test_24_admin_presets_html_has_slow_mode_badge_in_list(self):
        """admin_presets.html должен показывать badge slow_mode в списке пресетов."""
        # Look for the badge in the existing presets list
        self.assertIn("slow_mode=", self.admin_presets_html,
                      "Badge slow_mode= не найден в списке пресетов")

    def test_25_admin_chats_html_has_slow_mode_badge_in_dropdown(self):
        """admin_chats.html: dropdown night-пресетов показывает 🐌 badge если slow_mode задан."""
        # Look for the slow_mode badge in the night preset dropdown
        self.assertIn("p.slow_mode_delay", self.admin_chats_html,
                      "slow_mode badge в dropdown night-пресетов не найден")

    # ─── 26-29. Changelog ─────────────────────────────────────────────

    def test_26_changelog_v4716_present(self):
        self.assertIn("v4.7.16", self.base_html)
        self.assertIn("4 августа 2026", self.base_html)

    def test_27_changelog_v4716_mentions_slow_mode(self):
        """Changelog v4.7.16 должен упоминать slow_mode."""
        # Find v4.7.16 section
        idx = self.base_html.find("<strong>v4.7.16</strong>")
        self.assertGreater(idx, 0, "v4.7.16 section not found")
        # Find next version section
        idx_next = self.base_html.find("<strong>v4.7.15</strong>", idx)
        self.assertGreater(idx_next, 0, "v4.7.15 section not found after v4.7.16")
        section = self.base_html[idx:idx_next]
        self.assertIn("slow_mode", section.lower(),
                      "v4.7.16 changelog должен упоминать slow_mode")

    def test_28_changelog_v4715_preserved(self):
        """Регрессия: v4.7.15 changelog сохранён."""
        self.assertIn("<strong>v4.7.15</strong>", self.base_html)

    def test_29_changelog_v4716_above_v4715(self):
        """v4.7.16 должен идти ВЫШЕ v4.7.15."""
        idx_16 = self.base_html.find("<strong>v4.7.16</strong>")
        idx_15 = self.base_html.find("<strong>v4.7.15</strong>")
        self.assertGreater(idx_16, 0)
        self.assertGreater(idx_15, 0)
        self.assertLess(idx_16, idx_15,
                        "v4.7.16 должен идти ВЫШЕ v4.7.15 в changelog")


if __name__ == "__main__":
    unittest.main(verbosity=2)
