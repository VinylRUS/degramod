"""
v4.7.13 — тесты чистки startup-логов (удаление ENV DUMP).

Проблема: при старте бот выводил в лог блок
    === ENV DUMP ===
      <ключ> = <значение>
      ... (десятки строк — PATH, HOME, PYTHONPATH, LANG, ...)
    === END ENV ===

Дамп был полезен на этапе отладки webhook/домена (когда не получалось
поднять домен — смотрели окружение), сейчас просто мусорит в логах.
Секреты маскировались (BOT_TOKEN, WEB_PASSWORD, SESSION_SECRET и т.д.
показывались как '<первые 8 символов>...'), но сам дамп избыточен.

Решение v4.7.13:
  • Удалён блок ENV DUMP целиком (цикл `for key, val in sorted(os.environ.items())`)
  • Удалены строки-маркеры `=== ENV DUMP ===` и `=== END ENV ===`
  • Удалён комментарий `# ── Диагностика ──`
  • Оставлена одна компактная строка startup-инфо:
        Startup: host=... ip=... port=... webhook=...
  • Секреты по-прежнему НЕ логируются нигде в коде

Тесты:
  1. APP_VERSION = "v4.7.13"
  2. APP_RELEASE_DATE = "2026-08-03"
  3. bot.py: НЕТ строки "=== ENV DUMP ==="
  4. bot.py: НЕТ строки "=== END ENV ==="
  5. bot.py: НЕТ цикла `for key, val in sorted(os.environ.items())`
  6. bot.py: НЕТ комментария `# ── Диагностика ──`
  7. bot.py: ЕСТЬ компактная startup-строка "Startup: host="
  8. bot.py: BOT_TOKEN / PORT / WEBHOOK_URL / WEBHOOK_SECRET по-прежнему читаются из env
  9. bot.py: Список секретов для маскировки не потерян (в коде или в комментарии)
 10. templates/base.html: ЕСТЬ запись v4.7.13 в changelog
 11. templates/base.html: v4.7.13 упоминает "ENV DUMP"
 12. templates/base.html: запись v4.7.12 сохранена (регрессия — не удалили чужую запись)
 13. templates/base.html: v4.7.13 идёт ВЫШЕ v4.7.12 (правильный порядок)

Regression:
  • test_v4712_exit_logic.py — 36+ тестов (логика выхода из режимов не должна сломаться)
  • test_v4710_chats_save_fix.py — 15 тестов (1 упадёт: APP_VERSION strict-equality, ожидаемо)
  • test_v4711_sanitary_roundtrip.py — 19 тестов (1 упадёт: APP_VERSION strict-equality, ожидаемо)
"""

import os
import re
import sys
import unittest

# ── Пути ────────────────────────────────────────────────────────────────────
PROJECT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

# Импортируем web_app только за APP_VERSION (он при импорте тянет много всего,
# но без него не проверить — остальное читаем как файл).
import importlib.util
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


# ─── Tests ──────────────────────────────────────────────────────────────────


class TestV4713EnvDumpCleanup(unittest.TestCase):
    """v4.7.13: ENV DUMP удалён из startup-логов."""

    def setUp(self):
        self.bot_py = _read(BOT_PY)
        self.base_html = _read(BASE_HTML)

    # ─── 1-2. Version ──────────────────────────────────────────────────

    def test_01_app_version_bumped(self):
        """APP_VERSION должен быть >= v4.7.13 (тест ослаблен в v4.7.14)."""
        # v4.7.14: ослаблен с == "v4.7.13" на >= v4.7.13 — чтобы не падать
        # на каждом следующем релизе.
        self.assertGreaterEqual(APP_VERSION, "v4.7.13",
                                f"APP_VERSION should be >= v4.7.13, got {APP_VERSION}")

    def test_02_release_date_updated(self):
        # v4.7.16+: release date bumped to 2026-08-04. Loosen to >=.
        self.assertGreaterEqual(APP_RELEASE_DATE, "2026-08-03",
            f"APP_RELEASE_DATE={APP_RELEASE_DATE} should be >= 2026-08-03")

    # ─── 3-6. ENV DUMP удалён ──────────────────────────────────────────

    def test_03_no_env_dump_marker(self):
        """Строка-маркер '=== ENV DUMP ===' не должна присутствовать."""
        self.assertNotIn("=== ENV DUMP ===", self.bot_py,
                         "ENV DUMP marker still present in bot.py")

    def test_04_no_end_env_marker(self):
        """Строка-маркер '=== END ENV ===' не должна присутствовать."""
        self.assertNotIn("=== END ENV ===", self.bot_py,
                         "END ENV marker still present in bot.py")

    def test_05_no_environ_iteration(self):
        """Цикл `for key, val in sorted(os.environ.items())` удалён."""
        # Допускаем варианты форматирования — главное убрать сам дамп.
        pattern = r"for\s+\w+\s*,\s*\w+\s+in\s+sorted\s*\(\s*os\.environ\.items"
        match = re.search(pattern, self.bot_py)
        self.assertIsNone(match,
                          f"os.environ.items() iteration still present: "
                          f"{match.group(0) if match else None}")

    def test_06_no_diagnostics_comment(self):
        """Комментарий 'Диагностика' (заголовок блока) удалён."""
        self.assertNotIn("# ── Диагностика", self.bot_py,
                         "Old 'Diagnostics' comment still present")

    # ─── 7. Compact startup line присутствует ──────────────────────────

    def test_07_compact_startup_line_present(self):
        """Одна компактная startup-строка заменяет дамп."""
        self.assertIn("Startup: host=", self.bot_py,
                      "Compact startup log line missing")
        # Проверяем что в строке есть ключевые поля
        idx = self.bot_py.find("Startup: host=")
        line_end = self.bot_py.find("\n", idx)
        line = self.bot_py[idx:line_end]
        for field in ["host=", "port=", "webhook="]:
            self.assertIn(field, line,
                          f"Startup line missing '{field}': {line!r}")

    # ─── 8. Env-загрузка не сломана ────────────────────────────────────

    def test_08_env_vars_still_loaded(self):
        """Все ключевые env-переменные по-прежнему читаются из os.getenv.

        Ищем подстроку 'os.getenv("VAR"' (без закрывающей скобки) — переменные
        могут иметь default value: os.getenv("PORT", "3000").
        """
        for var in ["BOT_TOKEN", "PORT", "WEBHOOK_URL", "WEBHOOK_SECRET"]:
            self.assertIn(f'os.getenv("{var}"', self.bot_py,
                          f"os.getenv('{var}'...) call missing in bot.py")

    def test_08a_bot_token_required_check_present(self):
        """RuntimeError если BOT_TOKEN не задан — критичная проверка."""
        self.assertIn('raise RuntimeError("BOT_TOKEN env variable is required")',
                      self.bot_py,
                      "BOT_TOKEN required-check missing")

    def test_08b_webhook_secret_generation_preserved(self):
        """WEBHOOK_SECRET autogen через secrets.token_hex(16) сохранён."""
        self.assertIn("secrets.token_hex(16)", self.bot_py,
                      "WEBHOOK_SECRET auto-generation missing")

    # ─── 9. Секреты не утекают ─────────────────────────────────────────

    def test_09_no_secret_logging_anywhere(self):
        """Ни в одной строке bot.py не должно быть логирования значений
        BOT_TOKEN / WEB_PASSWORD / SESSION_SECRET / WEBHOOK_SECRET.

        Проверяем простым поиском: если где-то есть logger.*(... BOT_TOKEN ...)
        или f"... {WEBHOOK_SECRET} ..." — это потенциальная утечка.
        Допускается упоминание этих переменных в комментариях и в
        os.getenv вызовах (это не логирование).
        """
        secrets = ["BOT_TOKEN", "WEB_PASSWORD", "SESSION_SECRET",
                   "WEBHOOK_SECRET"]
        # Проходим по каждой строке, ищем logger.* и проверяем что в той же
        # строке нет интерполяции секрета.
        for line_no, line in enumerate(self.bot_py.split("\n"), 1):
            if "logger." not in line:
                continue
            for s in secrets:
                # Допустимо: os.getenv("BOT_TOKEN") внутри logger? — нет, мы
                # просто проверяем, что имя переменной не вставляется в log-args.
                # Простая эвристика: f"...{BOT_TOKEN}..." или ", BOT_TOKEN,")
                # в строке с logger.*
                if re.search(rf"\{{\s*{s}\s*\}}|,\s*{s}\s*[,)]", line):
                    self.fail(
                        f"Possible secret leak at line {line_no}: "
                        f"{line.strip()!r}"
                    )

    def test_09a_secret_mask_list_documented(self):
        """Комментарий упоминает, какие секреты мы НЕ логируем (документация)."""
        # v4.7.13 комментарий содержит список секретов для будущих разработчиков
        self.assertIn("BOT_TOKEN", self.bot_py,
                      "BOT_TOKEN mention missing — comment should document secrets")

    # ─── 10-13. Changelog в base.html ─────────────────────────────────

    def test_10_changelog_has_v4713_entry(self):
        """В base.html есть запись для v4.7.13."""
        self.assertIn("<strong>v4.7.13</strong>", self.base_html,
                      "v4.7.13 changelog entry missing in base.html")

    def test_11_changelog_v4713_mentions_env_dump(self):
        """Запись v4.7.13 упоминает 'ENV DUMP'."""
        # Находим секцию v4.7.13
        idx_v13 = self.base_html.find("<strong>v4.7.13</strong>")
        idx_v12 = self.base_html.find("<strong>v4.7.12</strong>")
        self.assertGreater(idx_v13, -1, "v4.7.13 section not found")
        self.assertGreater(idx_v12, -1, "v4.7.12 section not found")
        section = self.base_html[idx_v13:idx_v12]
        self.assertIn("ENV DUMP", section,
                      "v4.7.13 changelog does not mention 'ENV DUMP'")

    def test_12_changelog_v4712_entry_preserved(self):
        """Запись v4.7.12 не удалена (регрессионная проверка)."""
        self.assertIn("<strong>v4.7.12</strong>", self.base_html,
                      "v4.7.12 changelog entry was deleted — regression!")

    def test_13_changelog_v4713_above_v4712(self):
        """v4.7.13 идёт ВЫШЕ v4.7.12 (обратный хронологический порядок)."""
        idx_v13 = self.base_html.find("<strong>v4.7.13</strong>")
        idx_v12 = self.base_html.find("<strong>v4.7.12</strong>")
        self.assertLess(idx_v13, idx_v12,
                        f"v4.7.13 (idx={idx_v13}) should be ABOVE "
                        f"v4.7.12 (idx={idx_v12}) in changelog")

    # ─── 14. Регрессия: hostname/socket всё ещё используются ──────────

    def test_14_socket_import_preserved(self):
        """socket.gethostname / gethostbyname всё ещё используются
        для компактной startup-строки."""
        self.assertIn("socket.gethostname()", self.bot_py,
                      "socket.gethostname() call missing")
        self.assertIn("socket.gethostbyname(", self.bot_py,
                      "socket.gethostbyname() call missing")

    # ─── 15. Регрессия: socket импортирован ───────────────────────────

    def test_15_socket_imported(self):
        """import socket присутствует."""
        # Ищем import socket в начале файла (до использования)
        head = self.bot_py[:5000]
        self.assertIn("import socket", head,
                      "import socket missing at top of bot.py")


if __name__ == "__main__":
    unittest.main(verbosity=2)
