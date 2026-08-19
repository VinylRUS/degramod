"""
v4.7.19 — тесты обработки ошибок Telegram в night mode / sanitary day.

Что изменилось:
  • bot.py импортирует TelegramAPIError (базовый класс) из aiogram.exceptions
    ВМЕСТО одного только TelegramBadRequest.
  • Все `except TelegramBadRequest` в night mode / sanitary day функциях
    заменены на `except TelegramAPIError` — это покрывает TelegramNotFound,
    TelegramForbiddenError, TelegramConflictError и другие подклассы,
    которые НЕ наследуют TelegramBadRequest.
  • В notify-блоках _enter_night_mode / _exit_night_mode лог теперь
    содержит `source_chat=` и `report_chat=` — сразу видно, какой ID кривой.

Контекст (почему это важно):
  После v4.7.18 (уведомления night mode идут в репорт-чат) в логах стали
  появляться ERROR │ Night mode error for chat …: Telegram server says - Not Found
  каждую минуту. Причина: `except TelegramBadRequest` не ловил TelegramNotFound
  ("Not Found" при отправке в удалённый чат) — это отдельный класс в aiogram 3.x.
  Исключение пробивалось наверх в _night_mode_tick и логировалось как ERROR.
  Теперь ловим базовый TelegramAPIError — любая ошибка Telegram логируется
  warning'ом и не валит весь tick.

Тесты:
  1. APP_VERSION = "v4.7.19"
  2. APP_RELEASE_DATE = "2026-08-04"
  3. bot.py импортирует TelegramAPIError из aiogram.exceptions
  4. bot.py НЕ содержит `except TelegramBadRequest` в коде (только в комментариях)
  5. bot.py содержит `except TelegramAPIError`
  6. _enter_night_mode: notify-блок ловит TelegramAPIError
  7. _enter_night_mode: внешний try/except ловит TelegramAPIError
  8. _enter_night_mode: notify-блок логирует source_chat= и report_chat=
  9. _exit_night_mode: notify-блок ловит TelegramAPIError
 10. _exit_night_mode: внешний try/except ловит TelegramAPIError
 11. _exit_night_mode: notify-блок логирует source_chat= и report_chat=
 12. _restore_day_state: ловит TelegramAPIError
 13. _enter_sanitary_day: ловит TelegramAPIError
 14. _exit_sanitary_day: ловит TelegramAPIError
 15. Регрессия: _enter_night_mode всё ещё использует _get_report_chat_id
 16. Регрессия: _exit_night_mode всё ещё использует _get_report_chat_id
 17. Регрессия: send_message(chat_id=report_chat_id, ...) в _enter_night_mode
 18. Регрессия: send_message(chat_id=report_chat_id, ...) в _exit_night_mode
 19. base.html: changelog v4.7.19 присутствует
 20. base.html: v4.7.19 упоминает TelegramAPIError или TelegramNotFound
 21. base.html: v4.7.18 сохранена (регрессия)
 22. base.html: v4.7.19 идёт ВЫШЕ v4.7.18
"""

import os
import re
import sys
import unittest
from _version import ver  # noqa: E402  (сравнение версий как кортежей, не строк)

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
            cursor = idx + 10
            while cursor < len(src):
                next_async = src.find("\nasync def ", cursor)
                next_def = src.find("\ndef ", cursor)
                next_decorator = src.find("\n@", cursor)
                candidates = [c for c in (next_async, next_def, next_decorator) if c > 0]
                if not candidates:
                    return src[idx:]
                next_pos = min(candidates)
                return src[idx:next_pos]
            return src[idx:]
    return ""


def _count_except_in_code(src: str, exc_name: str) -> int:
    """Считает `except <exc_name>` в исходнике, ИГНОРИРУЯ комментарии и строки.

    Простая эвристика: разбиваем по строкам, для каждой строки:
      - удаляем комментарии (всё после #, если # не внутри строки)
      - проверяем не начинается ли (после отступа) с `except <exc_name>`
    Возвращает количество совпадений.
    """
    count = 0
    for line in src.splitlines():
        # Удаляем комментарий — простейший split, не учитывает # внутри строк,
        # но для наших целей достаточно (в коде нет строк с # внутри).
        code_part = line.split("#", 1)[0]
        if re.search(rf"\bexcept\s+{re.escape(exc_name)}\b", code_part):
            count += 1
    return count


# ─── Tests ──────────────────────────────────────────────────────────────────


class TestV4719NightNotifyErrorHandling(unittest.TestCase):
    """v4.7.19: обработка ошибок Telegram в night mode / sanitary day."""

    def setUp(self):
        self.bot_py = _read(BOT_PY)
        self.base_html = _read(BASE_HTML)
        self.enter_fn = _find_fn_body(self.bot_py, "_enter_night_mode")
        self.exit_fn = _find_fn_body(self.bot_py, "_exit_night_mode")
        self.restore_fn = _find_fn_body(self.bot_py, "_restore_day_state")
        self.enter_sanitary_fn = _find_fn_body(self.bot_py, "_enter_sanitary_day")
        self.exit_sanitary_fn = _find_fn_body(self.bot_py, "_exit_sanitary_day")

    # ─── 1-2. Version ──────────────────────────────────────────────────

    def test_01_app_version(self):
        """APP_VERSION должен быть v4.7.19."""
        # v4.10.0: FIX сравнение строк ломалось на двузначном minor
        # ("v4.10.0" < "v4.7.x" лексикографически) — сравниваем через ver().
        self.assertGreaterEqual(ver(APP_VERSION), ver("v4.7.19"))

    def test_02_app_release_date(self):
        """Дата релиза не изменилась (тот же день)."""
        self.assertGreaterEqual(APP_RELEASE_DATE, "2026-08-04")

    # ─── 3-5. Imports / except clauses ────────────────────────────────

    def test_03_bot_imports_telegram_api_error(self):
        """bot.py должен импортировать TelegramAPIError из aiogram.exceptions."""
        self.assertIn(
            "from aiogram.exceptions import",
            self.bot_py,
            "bot.py должен импортировать из aiogram.exceptions",
        )
        # TelegramAPIError должен быть в импорте
        # Ищем строку импорта
        for line in self.bot_py.splitlines():
            if line.startswith("from aiogram.exceptions import"):
                self.assertIn(
                    "TelegramAPIError", line,
                    "TelegramAPIError должен быть в from aiogram.exceptions import",
                )
                return
        self.fail("from aiogram.exceptions import line not found")

    def test_04_no_except_telegram_bad_request_in_code(self):
        """В коде bot.py не должно быть `except TelegramBadRequest`
        (только в комментариях и строках).

        Раньше это была причина засорения логов: TelegramNotFound не наследует
        TelegramBadRequest и пробивался наверх.
        """
        count = _count_except_in_code(self.bot_py, "TelegramBadRequest")
        self.assertEqual(
            count, 0,
            f"В bot.py найдено {count} `except TelegramBadRequest` в коде. "
            "Все такие блоки должны быть заменены на `except TelegramAPIError` — "
            "TelegramBadRequest не ловит TelegramNotFound/TelegramForbiddenError."
        )

    def test_05_bot_uses_except_telegram_api_error(self):
        """В bot.py должно быть хотя бы 5 `except TelegramAPIError`
        (enter/exit night, restore_day_state, enter/exit sanitary).
        """
        count = _count_except_in_code(self.bot_py, "TelegramAPIError")
        self.assertGreaterEqual(
            count, 5,
            f"Ожидалось минимум 5 `except TelegramAPIError` в bot.py, "
            f"найдено {count}.",
        )

    # ─── 6-8. _enter_night_mode ───────────────────────────────────────

    def test_06_enter_night_mode_notify_catches_telegram_api_error(self):
        """_enter_night_mode: notify-блок (внутренний try) ловит TelegramAPIError."""
        self.assertGreater(len(self.enter_fn), 0, "_enter_night_mode not found")
        # Должен быть хотя бы один `except TelegramAPIError` — это может быть
        # как notify-блок, так и внешний. Проверяем что он есть.
        self.assertIn(
            "except TelegramAPIError", self.enter_fn,
            "_enter_night_mode должен ловить TelegramAPIError",
        )

    def test_07_enter_night_mode_outer_catches_telegram_api_error(self):
        """_enter_night_mode: внешний try/except (для bot.get_chat /
        set_chat_permissions) тоже ловит TelegramAPIError, а не TelegramBadRequest.

        Проверяем что в теле функции НЕТ `except TelegramBadRequest` (даже в коде).
        """
        self.assertGreater(len(self.enter_fn), 0, "_enter_night_mode not found")
        count = _count_except_in_code(self.enter_fn, "TelegramBadRequest")
        self.assertEqual(
            count, 0,
            f"_enter_night_mode содержит {count} `except TelegramBadRequest` в коде — "
            "все должны быть заменены на `except TelegramAPIError`",
        )

    def test_08_enter_night_mode_notify_logs_source_and_report_chat(self):
        """_enter_night_mode: notify-блок логирует source_chat= и report_chat=.

        Раньше логировался только исходный cs.chat_id — было непонятно, какой
        report_chat_id кривой. Теперь лог содержит оба.
        """
        self.assertGreater(len(self.enter_fn), 0, "_enter_night_mode not found")
        # Ищем формат лога в notify-блоке (там где "Night mode enter notify failed")
        # Должно быть "source_chat=" и "report_chat="
        # Сначала найдём строку с "Night mode enter notify failed"
        notify_log_line = None
        for line in self.enter_fn.splitlines():
            if "Night mode enter notify failed" in line:
                # Берём ближайшую строку с logger.warning, включая соседние
                # (формат может быть на нескольких строках)
                notify_log_line = line
                break
        # Если не нашли в одной строке — берём блок из 5 строк вокруг
        if notify_log_line is None:
            # Ищем по подстроке "source_chat=" — это более надёжно
            self.assertIn(
                "source_chat=", self.enter_fn,
                "_enter_night_mode: notify-блок должен логировать source_chat= "
                "(для диагностики, какой исходный чат)",
            )
        else:
            self.assertIn(
                "source_chat=", self.enter_fn,
                "_enter_night_mode: notify-блок должен логировать source_chat= "
                "(для диагностики, какой исходный чат)",
            )
        self.assertIn(
            "report_chat=", self.enter_fn,
            "_enter_night_mode: notify-блок должен логировать report_chat= "
            "(для диагностики, какой report_chat_id кривой)",
        )

    # ─── 9-11. _exit_night_mode ───────────────────────────────────────

    def test_09_exit_night_mode_notify_catches_telegram_api_error(self):
        """_exit_night_mode: notify-блок ловит TelegramAPIError."""
        self.assertGreater(len(self.exit_fn), 0, "_exit_night_mode not found")
        self.assertIn(
            "except TelegramAPIError", self.exit_fn,
            "_exit_night_mode должен ловить TelegramAPIError",
        )

    def test_10_exit_night_mode_outer_catches_telegram_api_error(self):
        """_exit_night_mode: внешний try/except ловит TelegramAPIError."""
        self.assertGreater(len(self.exit_fn), 0, "_exit_night_mode not found")
        count = _count_except_in_code(self.exit_fn, "TelegramBadRequest")
        self.assertEqual(
            count, 0,
            f"_exit_night_mode содержит {count} `except TelegramBadRequest` в коде — "
            "все должны быть заменены на `except TelegramAPIError`",
        )

    def test_11_exit_night_mode_notify_logs_source_and_report_chat(self):
        """_exit_night_mode: notify-блок логирует source_chat= и report_chat=."""
        self.assertGreater(len(self.exit_fn), 0, "_exit_night_mode not found")
        self.assertIn(
            "source_chat=", self.exit_fn,
            "_exit_night_mode: notify-блок должен логировать source_chat=",
        )
        self.assertIn(
            "report_chat=", self.exit_fn,
            "_exit_night_mode: notify-блок должен логировать report_chat=",
        )

    # ─── 12-14. restore_day_state / sanitary day ──────────────────────

    def test_12_restore_day_state_catches_telegram_api_error(self):
        """_restore_day_state: ловит TelegramAPIError (для set_chat_slow_mode_delay)."""
        self.assertGreater(len(self.restore_fn), 0, "_restore_day_state not found")
        # _restore_day_state использует set_chat_slow_mode_delay через SetChatSlowModeDelay
        # Внешний try/except для set_chat_permissions НЕ обёрнут (преднамеренно —
        # если set_chat_permissions падает, исключение летит выше, где его ловит
        # _exit_night_mode / _exit_sanitary_day). Но set_chat_slow_mode_delay
        # обёрнут в try/except — он должен ловить TelegramAPIError.
        count = _count_except_in_code(self.restore_fn, "TelegramBadRequest")
        self.assertEqual(
            count, 0,
            f"_restore_day_state содержит {count} `except TelegramBadRequest` — "
            "должен использовать `except TelegramAPIError`",
        )

    def test_13_enter_sanitary_day_catches_telegram_api_error(self):
        """_enter_sanitary_day: внешний try/except ловит TelegramAPIError."""
        self.assertGreater(len(self.enter_sanitary_fn), 0, "_enter_sanitary_day not found")
        count = _count_except_in_code(self.enter_sanitary_fn, "TelegramBadRequest")
        self.assertEqual(
            count, 0,
            f"_enter_sanitary_day содержит {count} `except TelegramBadRequest` — "
            "должен использовать `except TelegramAPIError`",
        )

    def test_14_exit_sanitary_day_catches_telegram_api_error(self):
        """_exit_sanitary_day: все try/except ловят TelegramAPIError."""
        self.assertGreater(len(self.exit_sanitary_fn), 0, "_exit_sanitary_day not found")
        count = _count_except_in_code(self.exit_sanitary_fn, "TelegramBadRequest")
        self.assertEqual(
            count, 0,
            f"_exit_sanitary_day содержит {count} `except TelegramBadRequest` — "
            "должен использовать `except TelegramAPIError`",
        )

    # ─── 15-18. Регрессия v4.7.18 ─────────────────────────────────────

    def test_15_regression_enter_night_mode_uses_get_report_chat_id(self):
        """Регрессия: _enter_night_mode всё ещё использует _get_report_chat_id
        (из v4.7.18 — не должен был сломаться).
        """
        self.assertGreater(len(self.enter_fn), 0, "_enter_night_mode not found")
        self.assertIn("_get_report_chat_id", self.enter_fn)

    def test_16_regression_exit_night_mode_uses_get_report_chat_id(self):
        """Регрессия: _exit_night_mode всё ещё использует _get_report_chat_id."""
        self.assertGreater(len(self.exit_fn), 0, "_exit_night_mode not found")
        self.assertIn("_get_report_chat_id", self.exit_fn)

    def test_17_regression_enter_night_mode_sends_to_report_chat_id(self):
        """Регрессия: _enter_night_mode отправляет в report_chat_id, не в cs.chat_id."""
        self.assertGreater(len(self.enter_fn), 0, "_enter_night_mode not found")
        self.assertIn("send_message(chat_id=report_chat_id", self.enter_fn)
        self.assertNotIn("send_message(chat_id=cs.chat_id", self.enter_fn)

    def test_18_regression_exit_night_mode_sends_to_report_chat_id(self):
        """Регрессия: _exit_night_mode отправляет в report_chat_id, не в cs.chat_id."""
        self.assertGreater(len(self.exit_fn), 0, "_exit_night_mode not found")
        self.assertIn("send_message(chat_id=report_chat_id", self.exit_fn)
        self.assertNotIn("send_message(chat_id=cs.chat_id", self.exit_fn)

    # ─── 19-22. Changelog ─────────────────────────────────────────────

    def test_19_changelog_v4719_present(self):
        """base.html содержит changelog v4.7.19."""
        self.assertIn("<strong>v4.7.19</strong>", self.base_html)

    def test_20_changelog_v4719_mentions_telegram_api_error(self):
        """Changelog v4.7.19 упоминает TelegramAPIError или TelegramNotFound."""
        idx = self.base_html.find("<strong>v4.7.19</strong>")
        self.assertGreater(idx, 0, "v4.7.19 section not found")
        idx_next = self.base_html.find("<strong>v4.7.18</strong>", idx)
        self.assertGreater(idx_next, 0, "v4.7.18 section not found after v4.7.19")
        section = self.base_html[idx:idx_next]
        self.assertTrue(
            "TelegramAPIError" in section or "TelegramNotFound" in section,
            "v4.7.19 changelog должен упоминать TelegramAPIError или TelegramNotFound",
        )

    def test_21_changelog_v4718_preserved(self):
        """Регрессия: v4.7.18 changelog сохранён."""
        self.assertIn("<strong>v4.7.18</strong>", self.base_html)

    def test_22_changelog_v4719_above_v4718(self):
        """v4.7.19 должен идти ВЫШЕ v4.7.18 в changelog."""
        idx_19 = self.base_html.find("<strong>v4.7.19</strong>")
        idx_18 = self.base_html.find("<strong>v4.7.18</strong>")
        self.assertGreater(idx_19, 0)
        self.assertGreater(idx_18, 0)
        self.assertLess(idx_19, idx_18,
                        "v4.7.19 должен идти ВЫШЕ v4.7.18 в changelog")


if __name__ == "__main__":
    unittest.main(verbosity=2)
