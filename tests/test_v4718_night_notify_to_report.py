"""
v4.7.18 — тесты перенаправления night mode уведомлений в репорт-чат.

Что изменилось:
  • bot.py импортирует _get_report_chat_id из bot_handlers
  • _enter_night_mode: уведомление «🌙 Ночной режим включён» теперь
    отправляется в репорт-чат (через _get_report_chat_id), а не в cs.chat_id
  • _exit_night_mode: уведомление «☀️ Ночной режим снят» — то же самое
  • Если репорт-чат не настроен (None) — лог warning, уведомление не отправляется
    (НЕ падает в общий чат как fallback)

Тесты:
  1. APP_VERSION = "v4.7.18"
  2. APP_RELEASE_DATE = "2026-08-04"
  3. bot.py импортирует _get_report_chat_id из bot_handlers
  4. _enter_night_mode: содержит вызов _get_report_chat_id
  5. _enter_night_mode: НЕ содержит send_message(chat_id=cs.chat_id, ...) для notify
  6. _enter_night_mode: содержит send_message(chat_id=report_chat_id, ...)
  7. _enter_night_mode: содержит fallback warning при report_chat_id is None
  8. _enter_night_mode: warning текст упоминает "no report chat"
  9. _exit_night_mode: содержит вызов _get_report_chat_id
 10. _exit_night_mode: НЕ содержит send_message(chat_id=cs.chat_id, ...) для notify
 11. _exit_night_mode: содержит send_message(chat_id=report_chat_id, ...)
 12. _exit_night_mode: содержит fallback warning при report_chat_id is None
 13. _exit_night_mode: warning текст упоминает "no report chat"
 14. Санитарные дни: _enter_sanitary_day НЕ отправляет send_message в общий чат
 15. Санитарные дни: _exit_sanitary_day НЕ отправляет send_message в общий чат
 16. Подсказка в warning: упоминает report_chat_id или is_report_chat
 17. Логирование успеха: "notify sent to report chat" в _enter_night_mode
 18. Логирование успеха: "notify sent to report chat" в _exit_night_mode
 19. base.html: changelog v4.7.18 присутствует
 20. base.html: v4.7.18 упоминает "репорт-чат" или "report chat"
 21. base.html: v4.7.17 сохранена (регрессия)
 22. base.html: v4.7.18 идёт ВЫШЕ v4.7.17
"""

import os
import sys
import unittest

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

BOT_PY = os.path.join(PROJECT_DIR, "bot.py")
BASE_HTML = os.path.join(PROJECT_DIR, "templates", "base.html")


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _find_fn_body(src: str, fn_name: str) -> str:
    """Найти тело функции async def fn_name (или def fn_name).

    Возвращает строку от начала определения до следующего top-level def/async def
    или декоратора.
    """
    for prefix in ("async def ", "def "):
        idx = src.find(f"{prefix}{fn_name}(")
        if idx > 0:
            # Ищем следующий def на том же уровне (4 пробела) или декоратор
            # Простейшая эвристика: ищем "\nasync def " или "\ndef " или "\n@"
            # на верхнем уровне (без отступа).
            cursor = idx + 10
            while cursor < len(src):
                # Ищем следующий кандидат
                next_async = src.find("\nasync def ", cursor)
                next_def = src.find("\ndef ", cursor)
                next_decorator = src.find("\n@", cursor)
                # Берём ближайший
                candidates = [c for c in (next_async, next_def, next_decorator) if c > 0]
                if not candidates:
                    return src[idx:]
                next_pos = min(candidates)
                # Проверяем, что это top-level (сразу после \n, без отступа)
                # async def / def — это всегда top-level если идут сразу после \n
                # @decorator — top-level если сразу после \n
                return src[idx:next_pos]
            return src[idx:]
    return ""


# ─── Tests ──────────────────────────────────────────────────────────────────


class TestV4718NightNotifyToReport(unittest.TestCase):
    """v4.7.18: night mode уведомления → репорт-чат."""

    def setUp(self):
        self.bot_py = _read(BOT_PY)
        self.base_html = _read(BASE_HTML)
        self.enter_fn = _find_fn_body(self.bot_py, "_enter_night_mode")
        self.exit_fn = _find_fn_body(self.bot_py, "_exit_night_mode")
        self.enter_sanitary_fn = _find_fn_body(self.bot_py, "_enter_sanitary_day")
        self.exit_sanitary_fn = _find_fn_body(self.bot_py, "_exit_sanitary_day")

    # ─── 1-2. Version ──────────────────────────────────────────────────

    def test_01_app_version(self):
        """APP_VERSION должен быть v4.7.18 или выше."""
        # v4.10.0: FIX сравнение строк ломалось на двузначном minor
        # ("v4.10.0" < "v4.7.x" лексикографически) — сравниваем как кортеж чисел.
        self.assertGreaterEqual(tuple(int(p) for p in APP_VERSION.lstrip("v").split(".")), tuple(int(p) for p in "v4.7.18".lstrip("v").split(".")))

    def test_02_app_release_date(self):
        self.assertGreaterEqual(APP_RELEASE_DATE, "2026-08-04")

    # ─── 3. Import ─────────────────────────────────────────────────────

    def test_03_bot_imports_get_report_chat_id(self):
        """bot.py должен импортировать _get_report_chat_id из bot_handlers."""
        # Ищем блок from bot_handlers import (...)
        idx = self.bot_py.find("from bot_handlers import (")
        self.assertGreater(idx, 0, "from bot_handlers import ( block not found")
        # Находим закрывающую скобку — это ) на отдельной строке без отступа
        # (top-level). Простая эвристика: ищем "\n)" после idx.
        end_idx = self.bot_py.find("\n)", idx)
        self.assertGreater(end_idx, 0, "closing ) not found")
        import_block = self.bot_py[idx:end_idx]
        # _get_report_chat_id должен быть в импорте как отдельная строка
        # (после запятой или в начале строки).
        self.assertIn("_get_report_chat_id,", import_block,
                      "_get_report_chat_id должен быть в from bot_handlers import (...)")

    # ─── 4-8. _enter_night_mode ───────────────────────────────────────

    def test_04_enter_night_mode_uses_get_report_chat_id(self):
        """_enter_night_mode должен вызывать _get_report_chat_id."""
        self.assertGreater(len(self.enter_fn), 0, "_enter_night_mode not found")
        self.assertIn("_get_report_chat_id", self.enter_fn,
                      "_enter_night_mode должен вызывать _get_report_chat_id")

    def test_05_enter_night_mode_no_send_to_cs_chat_id(self):
        """_enter_night_mode НЕ должен отправлять notify в cs.chat_id.

        Раньше было: await bot.send_message(chat_id=cs.chat_id, text=text)
        Теперь должно быть: await bot.send_message(chat_id=report_chat_id, text=text)
        """
        self.assertGreater(len(self.enter_fn), 0, "_enter_night_mode not found")
        self.assertNotIn("send_message(chat_id=cs.chat_id", self.enter_fn,
                          "_enter_night_mode не должен отправлять в cs.chat_id — "
                          "только в report_chat_id")

    def test_06_enter_night_mode_sends_to_report_chat_id(self):
        """_enter_night_mode должен отправлять в report_chat_id."""
        self.assertGreater(len(self.enter_fn), 0, "_enter_night_mode not found")
        self.assertIn("send_message(chat_id=report_chat_id", self.enter_fn,
                      "_enter_night_mode должен отправлять в report_chat_id")

    def test_07_enter_night_mode_has_none_fallback(self):
        """_enter_night_mode: если report_chat_id is None — skip с warning."""
        self.assertGreater(len(self.enter_fn), 0, "_enter_night_mode not found")
        self.assertIn("report_chat_id is None", self.enter_fn,
                      "Проверка report_chat_id is None отсутствует")

    def test_08_enter_night_mode_warning_mentions_no_report_chat(self):
        """Warning текст упоминает 'no report chat'."""
        self.assertGreater(len(self.enter_fn), 0, "_enter_night_mode not found")
        self.assertIn("no report chat", self.enter_fn.lower(),
                      "Warning должен упоминать 'no report chat'")

    # ─── 9-13. _exit_night_mode ───────────────────────────────────────

    def test_09_exit_night_mode_uses_get_report_chat_id(self):
        """_exit_night_mode должен вызывать _get_report_chat_id."""
        self.assertGreater(len(self.exit_fn), 0, "_exit_night_mode not found")
        self.assertIn("_get_report_chat_id", self.exit_fn,
                      "_exit_night_mode должен вызывать _get_report_chat_id")

    def test_10_exit_night_mode_no_send_to_cs_chat_id(self):
        """_exit_night_mode НЕ должен отправлять notify в cs.chat_id."""
        self.assertGreater(len(self.exit_fn), 0, "_exit_night_mode not found")
        self.assertNotIn("send_message(chat_id=cs.chat_id", self.exit_fn,
                          "_exit_night_mode не должен отправлять в cs.chat_id — "
                          "только в report_chat_id")

    def test_11_exit_night_mode_sends_to_report_chat_id(self):
        """_exit_night_mode должен отправлять в report_chat_id."""
        self.assertGreater(len(self.exit_fn), 0, "_exit_night_mode not found")
        self.assertIn("send_message(chat_id=report_chat_id", self.exit_fn,
                      "_exit_night_mode должен отправлять в report_chat_id")

    def test_12_exit_night_mode_has_none_fallback(self):
        """_exit_night_mode: если report_chat_id is None — skip с warning."""
        self.assertGreater(len(self.exit_fn), 0, "_exit_night_mode not found")
        self.assertIn("report_chat_id is None", self.exit_fn,
                      "Проверка report_chat_id is None отсутствует")

    def test_13_exit_night_mode_warning_mentions_no_report_chat(self):
        """Warning текст упоминает 'no report chat'."""
        self.assertGreater(len(self.exit_fn), 0, "_exit_night_mode not found")
        self.assertIn("no report chat", self.exit_fn.lower(),
                      "Warning должен упоминать 'no report chat'")

    # ─── 14-15. Санитарные дни не регрессировали ─────────────────────

    def test_14_enter_sanitary_day_no_send_to_public_chat(self):
        """_enter_sanitary_day НЕ должен отправлять send_message в cs.chat_id.

        Санитарные дни никогда не отправляли уведомления — проверяем что
        мы не добавили это случайно.
        """
        self.assertGreater(len(self.enter_sanitary_fn), 0,
                           "_enter_sanitary_day not found")
        self.assertNotIn("send_message(chat_id=cs.chat_id", self.enter_sanitary_fn,
                          "_enter_sanitary_day не должен отправлять в cs.chat_id")

    def test_15_exit_sanitary_day_no_send_to_public_chat(self):
        """_exit_sanitary_day НЕ должен отправлять send_message в cs.chat_id."""
        self.assertGreater(len(self.exit_sanitary_fn), 0,
                           "_exit_sanitary_day not found")
        self.assertNotIn("send_message(chat_id=cs.chat_id", self.exit_sanitary_fn,
                          "_exit_sanitary_day не должен отправлять в cs.chat_id")

    # ─── 16-18. Подсказка + логирование ──────────────────────────────

    def test_16_warning_mentions_how_to_configure(self):
        """Warning должен подсказывать как настроить репорт-чат
        (упоминать report_chat_id или is_report_chat).
        """
        # Проверяем в обеих функциях
        for fn_name, fn_body in (("_enter_night_mode", self.enter_fn),
                                  ("_exit_night_mode", self.exit_fn)):
            with self.subTest(fn=fn_name):
                self.assertTrue(
                    "report_chat_id" in fn_body or "is_report_chat" in fn_body,
                    f"{fn_name}: warning должен подсказывать как настроить "
                    f"репорт-чат (report_chat_id или is_report_chat)",
                )

    def test_17_enter_night_mode_logs_success_to_report_chat(self):
        """_enter_night_mode: при успехе логирует 'notify sent to report chat'."""
        self.assertGreater(len(self.enter_fn), 0, "_enter_night_mode not found")
        self.assertIn("notify sent to report chat", self.enter_fn.lower(),
                      "Лог успеха должен упоминать 'notify sent to report chat'")

    def test_18_exit_night_mode_logs_success_to_report_chat(self):
        """_exit_night_mode: при успехе логирует 'notify sent to report chat'."""
        self.assertGreater(len(self.exit_fn), 0, "_exit_night_mode not found")
        self.assertIn("notify sent to report chat", self.exit_fn.lower(),
                      "Лог успеха должен упоминать 'notify sent to report chat'")

    # ─── 19-22. Changelog ─────────────────────────────────────────────

    def test_19_changelog_v4718_present(self):
        self.assertIn("<strong>v4.7.18</strong>", self.base_html)

    def test_20_changelog_v4718_mentions_report_chat(self):
        """Changelog v4.7.18 должен упоминать 'репорт-чат' или 'report chat'."""
        idx = self.base_html.find("<strong>v4.7.18</strong>")
        self.assertGreater(idx, 0, "v4.7.18 section not found")
        idx_next = self.base_html.find("<strong>v4.7.17</strong>", idx)
        self.assertGreater(idx_next, 0, "v4.7.17 section not found after v4.7.18")
        section = self.base_html[idx:idx_next]
        self.assertTrue(
            "репорт-чат" in section.lower() or "report chat" in section.lower(),
            "v4.7.18 changelog должен упоминать репорт-чат/report chat",
        )

    def test_21_changelog_v4717_preserved(self):
        """Регрессия: v4.7.17 changelog сохранён."""
        self.assertIn("<strong>v4.7.17</strong>", self.base_html)

    def test_22_changelog_v4718_above_v4717(self):
        """v4.7.18 должен идти ВЫШЕ v4.7.17."""
        idx_18 = self.base_html.find("<strong>v4.7.18</strong>")
        idx_17 = self.base_html.find("<strong>v4.7.17</strong>")
        self.assertGreater(idx_18, 0)
        self.assertGreater(idx_17, 0)
        self.assertLess(idx_18, idx_17,
                        "v4.7.18 должен идти ВЫШЕ v4.7.17 в changelog")


if __name__ == "__main__":
    unittest.main(verbosity=2)
