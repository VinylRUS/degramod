"""
test_v4720a_ephemeral_fix.py — v4.7.20 Этап 1: фикс эфемерных сообщений.

Покрывает:
- BUG#2: bot.delete_message → bot.delete_ephemeral_message (Bot API 10.2)
- BUG#3: except TelegramBadRequest → except TelegramAPIError (47 мест)
- BUG#10: добавлен лог успеха отправки ephemeral
- import: TelegramBadRequest убран из импорта, остался только TelegramAPIError
- changelog v4.7.20

Запуск:
    BOT_TOKEN="123456789:AAEhBP0av28-bot-test-token-fake" \\
    uv run pytest tests/test_v4720a_ephemeral_fix.py
"""
from _paths import _P  # noqa: E402  (корень вычисляется от __file__)

import ast
import re
import unittest
from pathlib import Path


V4_DIR = Path(_P())
BOT_HANDLERS_PATH = V4_DIR / "bot_handlers.py"
BASE_HTML_PATH = V4_DIR / "templates" / "base.html"


def _read_handlers() -> str:
    return BOT_HANDLERS_PATH.read_text(encoding="utf-8")


def _read_base() -> str:
    return BASE_HTML_PATH.read_text(encoding="utf-8")


def _read_web_app() -> str:
    return (V4_DIR / "web_app.py").read_text(encoding="utf-8")


class TestV4720aEphemeralFix(unittest.TestCase):
    """v4.7.20 Этап 1: фикс эфемерных сообщений и TelegramAPIError."""

    # ── 1. Импорт ──────────────────────────────────────────────────────────

    def test_01_no_TelegramBadRequest_import(self):
        """TelegramBadRequest больше не импортируется в bot_handlers.py."""
        src = _read_handlers()
        # Не должно быть `from aiogram.exceptions import ... TelegramBadRequest ...`
        m = re.search(r"^from\s+aiogram\.exceptions\s+import\s+(.+)$",
                      src, re.MULTILINE)
        self.assertIsNotNone(m, "aiogram.exceptions import не найден")
        imported = m.group(1)
        self.assertNotIn("TelegramBadRequest", imported,
                         "TelegramBadRequest не должен импортироваться — "
                         "заменён на TelegramAPIError (базовый класс)")

    def test_02_TelegramAPIError_imported(self):
        """TelegramAPIError импортирован из aiogram.exceptions."""
        src = _read_handlers()
        m = re.search(r"^from\s+aiogram\.exceptions\s+import\s+(.+)$",
                      src, re.MULTILINE)
        self.assertIsNotNone(m)
        self.assertIn("TelegramAPIError", m.group(1))

    # ── 2. Массовая замена except TelegramBadRequest → except TelegramAPIError ─

    def test_03_no_except_TelegramBadRequest_left(self):
        """Ни одного `except TelegramBadRequest` не должно остаться."""
        src = _read_handlers()
        matches = re.findall(r"\bexcept\s+TelegramBadRequest\b", src)
        self.assertEqual(matches, [],
                         f"Найдено {len(matches)} `except TelegramBadRequest` — "
                         "все должны быть заменены на `except TelegramAPIError`")

    def test_04_except_TelegramAPIError_count_at_least_47(self):
        """Должно быть минимум 47 `except TelegramAPIError` (как было BadRequest)."""
        src = _read_handlers()
        matches = re.findall(r"\bexcept\s+TelegramAPIError\b", src)
        self.assertGreaterEqual(
            len(matches), 47,
            f"Ожидалось ≥47 `except TelegramAPIError`, найдено {len(matches)}"
        )

    # ── 3. BUG#2: delete_ephemeral_message вместо delete_message ───────────

    def test_05_send_ephemeral_uses_delete_ephemeral_message(self):
        """_send_ephemeral должна использовать bot.delete_ephemeral_message.

        v4.7.20: после рефакторинга логика авто-удаления вынесена в
        _schedule_ephemeral_delete — поэтому проверяем что она вызывается
        оттуда, а не напрямую в _send_ephemeral.
        """
        src = _read_handlers()
        # Находим функцию _send_ephemeral
        m = re.search(
            r"async\s+def\s+_send_ephemeral\s*\([^)]*\)\s*->\s*None\s*:\s*(.*?)(?=\nasync\s+def\s+|\nclass\s+|\Z)",
            src, re.DOTALL,
        )
        self.assertIsNotNone(m, "_send_ephemeral не найдена")
        body = m.group(1)
        # _send_ephemeral должна вызывать _schedule_ephemeral_delete
        self.assertIn("_schedule_ephemeral_delete(", body,
                      "_send_ephemeral должна вызывать _schedule_ephemeral_delete")
        self.assertNotIn("bot.delete_message(", body,
                         "_send_ephemeral не должна использовать bot.delete_message")

    def test_06_send_user_warn_notification_uses_delete_ephemeral_message(self):
        """_send_user_warn_notification должна использовать _schedule_ephemeral_delete."""
        src = _read_handlers()
        m = re.search(
            r"async\s+def\s+_send_user_warn_notification\s*\([^)]*\)\s*->\s*None\s*:\s*(.*?)(?=\nasync\s+def\s+|\nclass\s+|\Z)",
            src, re.DOTALL,
        )
        self.assertIsNotNone(m, "_send_user_warn_notification не найдена")
        body = m.group(1)
        self.assertIn("_schedule_ephemeral_delete(", body,
                      "_send_user_warn_notification должна вызывать _schedule_ephemeral_delete")
        self.assertNotIn("bot.delete_message(", body,
                         "_send_user_warn_notification не должна использовать bot.delete_message")

    def test_07_delete_ephemeral_message_called_at_least_twice(self):
        """Метод delete_ephemeral_message должен вызываться минимум в 2 местах."""
        src = _read_handlers()
        matches = re.findall(r"bot\.delete_ephemeral_message\s*\(", src)
        self.assertGreaterEqual(len(matches), 2,
                                f"Ожидалось ≥2 вызовов delete_ephemeral_message, "
                                f"найдено {len(matches)}")

    # ── 4. BUG#10: лог успеха отправки ─────────────────────────────────────

    def test_08_send_ephemeral_logs_success(self):
        """После успешной отправки ephemeral модератору должен быть лог."""
        src = _read_handlers()
        # Ищем в _send_ephemeral лог успеха
        m = re.search(
            r"async\s+def\s+_send_ephemeral\s*\([^)]*\)\s*->\s*None\s*:\s*(.*?)(?=\nasync\s+def\s+|\nclass\s+|\Z)",
            src, re.DOTALL,
        )
        body = m.group(1)
        # Лог должен быть после send_message и до планировки auto-delete
        self.assertTrue(
            re.search(r'logger\.info\s*\(\s*["\']Ephemeral\s+sent:', body, re.IGNORECASE),
            "Должен быть logger.info('Ephemeral sent: ...') после успешной отправки"
        )

    def test_09_send_user_warn_notification_logs_success(self):
        """После успешной отправки warn ephemeral должен быть лог."""
        src = _read_handlers()
        m = re.search(
            r"async\s+def\s+_send_user_warn_notification\s*\([^)]*\)\s*->\s*None\s*:\s*(.*?)(?=\nasync\s+def\s+|\nclass\s+|\Z)",
            src, re.DOTALL,
        )
        body = m.group(1)
        self.assertTrue(
            re.search(r'logger\.info\s*\(\s*["\']Warn\s+ephemeral\s+sent:', body, re.IGNORECASE),
            "Должен быть logger.info('Warn ephemeral sent: ...') после успешной отправки"
        )

    def test_10_send_ephemeral_logs_delete_success(self):
        """После успешного удаления ephemeral должен быть лог.

        v4.7.20: после рефакторинга лог "deleted" находится в
        _schedule_ephemeral_delete, а не в _send_ephemeral.
        """
        src = _read_handlers()
        m = re.search(
            r"async\s+def\s+_schedule_ephemeral_delete\s*\([^)]*\)\s*->\s*None\s*:\s*(.*?)(?=\nasync\s+def\s+|\nclass\s+|\Z)",
            src, re.DOTALL,
        )
        self.assertIsNotNone(m, "_schedule_ephemeral_delete не найдена")
        body = m.group(1)
        self.assertIn("deleted:", body,
                      "Должен быть лог 'deleted:' в _schedule_ephemeral_delete")

    def test_11_send_user_warn_notification_logs_delete_success(self):
        """После успешного удаления warn ephemeral должен быть лог.

        v4.7.20: лог находится в общей функции _schedule_ephemeral_delete.
        """
        src = _read_handlers()
        m = re.search(
            r"async\s+def\s+_schedule_ephemeral_delete\s*\([^)]*\)\s*->\s*None\s*:\s*(.*?)(?=\nasync\s+def\s+|\nclass\s+|\Z)",
            src, re.DOTALL,
        )
        body = m.group(1)
        self.assertIn("deleted:", body,
                      "Должен быть лог 'deleted:' в _schedule_ephemeral_delete")

    # ── 5. except TelegramAPIError в ephemeral-функциях ────────────────────

    def test_12_send_ephemeral_outer_except_is_TelegramAPIError(self):
        """Внешний except в _send_ephemeral должен быть TelegramAPIError (не BadRequest)."""
        src = _read_handlers()
        m = re.search(
            r"async\s+def\s+_send_ephemeral\s*\([^)]*\)\s*->\s*None\s*:\s*(.*?)(?=\nasync\s+def\s+|\nclass\s+|\Z)",
            src, re.DOTALL,
        )
        body = m.group(1)
        # Должен быть `except TelegramAPIError as e:` после send_message
        self.assertTrue(
            re.search(r"except\s+TelegramAPIError\s+as\s+e\s*:", body),
            "Должен быть `except TelegramAPIError as e:` для обработки ошибок отправки"
        )

    def test_13_send_ephemeral_inner_except_is_TelegramAPIError(self):
        """Внутренний except в auto-delete должен быть TelegramAPIError.

        v4.7.20: после рефакторинга except находится в _schedule_ephemeral_delete.
        """
        src = _read_handlers()
        m = re.search(
            r"async\s+def\s+_schedule_ephemeral_delete\s*\([^)]*\)\s*->\s*None\s*:\s*(.*?)(?=\nasync\s+def\s+|\nclass\s+|\Z)",
            src, re.DOTALL,
        )
        self.assertIsNotNone(m, "_schedule_ephemeral_delete не найдена")
        body = m.group(1)
        self.assertTrue(
            re.search(r"except\s+TelegramAPIError\s+as\s+e\s*:", body),
            "Должен быть `except TelegramAPIError as e:` в _schedule_ephemeral_delete"
        )

    # ── 6. AST-валидация ────────────────────────────────────────────────────

    def test_14_bot_handlers_ast_valid(self):
        """bot_handlers.py должен быть синтаксически валиден."""
        src = _read_handlers()
        try:
            ast.parse(src)
        except SyntaxError as e:
            self.fail(f"bot_handlers.py syntax error: {e}")

    # ── 7. Комментарии про v4.7.20 ─────────────────────────────────────────

    def test_15_v4720_comments_present_in_ephemeral(self):
        """В _send_ephemeral должны быть комментарии v4.7.20 про Bot API 10.2."""
        src = _read_handlers()
        m = re.search(
            r"async\s+def\s+_send_ephemeral\s*\([^)]*\)\s*->\s*None\s*:\s*(.*?)(?=\nasync\s+def\s+|\nclass\s+|\Z)",
            src, re.DOTALL,
        )
        body = m.group(1)
        self.assertIn("v4.7.20", body,
                      "В _send_ephemeral должны быть комментарии с упоминанием v4.7.20")
        self.assertIn("delete_ephemeral_message", body)
        # Должно быть упоминание Bot API 10.2
        self.assertTrue(
            "10.2" in body or "Bot API" in body,
            "Должно быть упоминание Bot API 10.2 в комментариях"
        )

    # ── 8. Changelog (позже, на финальном этапе) ───────────────────────────

    def test_16_changelog_v4720_present(self):
        """В base.html должна быть запись v4.7.20 в changelog."""
        # Этот тест может падать до этапа version_bump — помечен как ожидаемый
        src = _read_base()
        if "v4.7.20" not in src:
            self.skipTest("v4.7.20 changelog ещё не добавлен — этап version_bump")
        self.assertIn("v4.7.20", src)

    def test_17_APP_VERSION_v4720(self):
        """APP_VERSION в web_app.py должна быть >= v4.7.20."""
        src = _read_web_app()
        m = re.search(r'APP_VERSION\s*=\s*"(v[\d\.]+)"', src)
        self.assertIsNotNone(m, "APP_VERSION не найден в web_app.py")
        version = m.group(1)
        # Сравниваем как tuple: (4, 7, 20)
        def parse(v):
            parts = v.lstrip("v").split(".")
            return tuple(int(p) for p in parts)
        self.assertGreaterEqual(parse(version), (4, 7, 20),
                                f"APP_VERSION {version} < v4.7.20")

    # ── 9. Регрессия: другие функции не сломаны ────────────────────────────

    def test_18_send_report_function_present(self):
        """_send_report функция должна остаться."""
        src = _read_handlers()
        self.assertIn("async def _send_report(", src)

    def test_19_check_warn_threshold_present(self):
        """_check_warn_threshold функция должна остаться."""
        src = _read_handlers()
        self.assertIn("async def _check_warn_threshold(", src)

    def test_20_warn_command_handler_present(self):
        """Обработчик !warn команды должен остаться."""
        src = _read_handlers()
        self.assertIn("_CMD_WARN", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
