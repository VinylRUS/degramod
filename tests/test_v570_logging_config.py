"""
test_v570_logging_config.py — тесты v5.7.0 (конфигурация логирования: stdout + файл + Loki).

Что проверяет:

  T1:  configure_logging без LOKI_URL → 2 handler'а (Stream + File), без Loki.
  T2:  configure_logging с LOKI_URL + LOKI_USER + LOKI_PASSWORD → 3 handler'а.
  T3:  LOKI_URL задан, logging-loki не установлен → warning, 2 handler'а.
  T4:  LOG_DIR в недоступном каталоге → warning, 1 handler (Stream).
  T5:  Идемпотентность: повторный configure_logging не дублирует handler'ы.
  T6:  Loki handler с auth tuple (user, password) — передаётся в LokiHandler.
  T7:  Loki handler без auth (только password) — auth=("", password).
  T8:  Loki handler без auth вообще — auth=None, handler всё равно добавляется.
  T9:  APP_VERSION_TAG берётся из env, default "unknown".
  T10: LOKI_ENVIRONMENT берётся из env, default "production".
  T11: APP_VERSION в web_app.py = "v5.7.0".
  T12: version в pyproject.toml = "5.7.0".
  T13: logging_config.py существует и экспортирует configure_logging.
  T14: python-logging-loki в pyproject.toml dependencies.
  T15: Changelog-запись для v5.7.0 в templates/base.html.

Запуск:
    uv run python tools/run_tests.py -k v570_logging
    uv run python tests/test_v570_logging_config.py
"""
from __future__ import annotations

import logging
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

from _paths import ROOT

sys.path.insert(0, str(ROOT))


def _clear_loki_env() -> None:
    """Чистит все LOKI_* и APP_VERSION_TAG переменные."""
    for key in ("LOKI_URL", "LOKI_USER", "LOKI_PASSWORD",
                "LOKI_ENVIRONMENT", "APP_VERSION_TAG", "LOG_DIR"):
        os.environ.pop(key, None)


def _reset_root_logger() -> None:
    """Сброс root logger — закрыть и убрать все handler'ы."""
    root = logging.getLogger()
    for h in list(root.handlers):
        try:
            h.close()
        except Exception:
            pass
        root.removeHandler(h)


def _invalidate_logging_modules() -> None:
    """Удаляет кеш импорта logging_config и logging_loki из sys.modules.

    Нужно, чтобы patch.dict(sys.modules, ...) подействовал — иначе
    повторный `import logging_config` возьмёт кеш и не переимпортирует
    `logging_loki` внутри.
    """
    sys.modules.pop("logging_config", None)
    sys.modules.pop("logging_loki", None)


class TestConfigureLoggingNoLoki(unittest.TestCase):
    """Без LOKI_URL бот стартует без удалённого логирования."""

    def setUp(self):
        _clear_loki_env()
        self._tmpdir = tempfile.mkdtemp(prefix="test_v570_logs_")
        os.environ["LOG_DIR"] = self._tmpdir
        _invalidate_logging_modules()

    def tearDown(self):
        _clear_loki_env()
        _reset_root_logger()
        _invalidate_logging_modules()

    def test_t1_no_loki_url_two_handlers(self):
        """T1: без LOKI_URL — 2 handler'а (Stream + File), без Loki."""
        import logging_config
        logging_config.configure_logging()

        root = logging.getLogger()
        types = [type(h).__name__ for h in root.handlers]
        self.assertEqual(len(root.handlers), 2,
                         f"Expected 2 handlers, got {len(types)}: {types}")
        self.assertIn("StreamHandler", types)
        self.assertIn("TimedRotatingFileHandler", types)
        self.assertFalse(any("Loki" in t for t in types),
                         f"Loki handler не должен добавляться: {types}")

    def test_t5_idempotent_no_duplicate_handlers(self):
        """T5: повторный configure_logging не дублирует handler'ы."""
        import logging_config
        logging_config.configure_logging()
        logging_config.configure_logging()
        logging_config.configure_logging()

        root = logging.getLogger()
        self.assertEqual(len(root.handlers), 2,
                         "Повторные вызовы не должны дублировать handler'ы")


class TestConfigureLoggingWithLoki(unittest.TestCase):
    """С LOKI_URL — добавляется Loki handler.

    Использует FakeLokiHandler (mock) — тесты проверяют, с какими аргументами
    вызывается LokiHandler, а не сетевое взаимодействие. Это держит тесты
    детерминированными и не зависит от того, установлен ли python-logging-loki
    в окружении (CI vs local).
    """

    def setUp(self):
        _clear_loki_env()
        self._tmpdir = tempfile.mkdtemp(prefix="test_v570_logs_")
        os.environ["LOG_DIR"] = self._tmpdir
        os.environ["LOKI_URL"] = "https://example.com/loki/api/v1/push"
        os.environ["LOKI_USER"] = "12345"
        os.environ["LOKI_PASSWORD"] = "secret-key"
        os.environ["APP_VERSION_TAG"] = "v5.7.0"
        _invalidate_logging_modules()

    def tearDown(self):
        _clear_loki_env()
        _reset_root_logger()
        _invalidate_logging_modules()

    def _make_fake_loki_module(self, captured: dict):
        """Создаёт fake модуль logging_loki с захватывающим LokiHandler."""

        class FakeLokiHandler(logging.Handler):
            def __init__(self, url=None, tags=None, auth=None, **kwargs):
                super().__init__()
                captured["url"] = url
                captured["tags"] = tags
                captured["auth"] = auth

        fake_module = type("FakeLokiModule", (), {"LokiHandler": FakeLokiHandler})
        return fake_module

    def test_t2_with_loki_url_three_handlers(self):
        """T2: с LOKI_URL + USER + PASSWORD → 3 handler'а."""
        captured = {}
        fake_module = self._make_fake_loki_module(captured)

        with patch.dict(sys.modules, {"logging_loki": fake_module}):
            import logging_config
            logging_config.configure_logging()

        root = logging.getLogger()
        types = [type(h).__name__ for h in root.handlers]
        self.assertEqual(len(root.handlers), 3,
                         f"Expected 3 handlers, got {len(types)}: {types}")
        self.assertIn("StreamHandler", types)
        self.assertIn("TimedRotatingFileHandler", types)
        self.assertTrue(any("Loki" in t for t in types),
                        f"Expected Loki handler in {types}")

    def test_t6_loki_with_user_and_password(self):
        """T6: LOKI_USER + LOKI_PASSWORD → auth=(user, password)."""
        captured = {}
        fake_module = self._make_fake_loki_module(captured)

        with patch.dict(sys.modules, {"logging_loki": fake_module}):
            import logging_config
            logging_config.configure_logging()

        self.assertEqual(captured["auth"], ("12345", "secret-key"))

    def test_t7_loki_with_only_password(self):
        """T7: только LOKI_PASSWORD (без USER) → auth=("", password)."""
        os.environ.pop("LOKI_USER", None)
        os.environ["LOKI_PASSWORD"] = "just-password"

        captured = {}
        fake_module = self._make_fake_loki_module(captured)

        with patch.dict(sys.modules, {"logging_loki": fake_module}):
            import logging_config
            logging_config.configure_logging()

        self.assertEqual(captured["auth"], ("", "just-password"))

    def test_t8_loki_without_any_auth(self):
        """T8: без USER и PASSWORD → auth=None, handler всё равно добавляется."""
        os.environ.pop("LOKI_USER", None)
        os.environ.pop("LOKI_PASSWORD", None)

        captured = {}
        fake_module = self._make_fake_loki_module(captured)

        with patch.dict(sys.modules, {"logging_loki": fake_module}):
            import logging_config
            logging_config.configure_logging()

        self.assertIsNone(captured["auth"])

    def test_t9_app_version_tag_used_as_label(self):
        """T9: APP_VERSION_TAG берётся из env и попадает в Loki tags."""
        os.environ["APP_VERSION_TAG"] = "v9.9.9-test"

        captured = {}
        fake_module = self._make_fake_loki_module(captured)

        with patch.dict(sys.modules, {"logging_loki": fake_module}):
            import logging_config
            logging_config.configure_logging()

        self.assertEqual(captured["tags"].get("version"), "v9.9.9-test")

    def test_t10_loki_environment_label(self):
        """T10: LOKI_ENVIRONMENT → env label в Loki tags."""
        os.environ["LOKI_ENVIRONMENT"] = "staging"

        captured = {}
        fake_module = self._make_fake_loki_module(captured)

        with patch.dict(sys.modules, {"logging_loki": fake_module}):
            import logging_config
            logging_config.configure_logging()

        self.assertEqual(captured["tags"].get("env"), "staging")
        self.assertEqual(captured["tags"].get("app"), "degramod")


class TestConfigureLoggingFailures(unittest.TestCase):
    """Сбои в логировании не валят старт бота."""

    def setUp(self):
        _clear_loki_env()
        _invalidate_logging_modules()

    def tearDown(self):
        _clear_loki_env()
        _reset_root_logger()
        _invalidate_logging_modules()

    def test_t3_loki_url_but_no_library(self):
        """T3: LOKI_URL задан, logging-loki не установлен → warning, 2 handler'а."""
        self._tmpdir = tempfile.mkdtemp(prefix="test_v570_logs_")
        os.environ["LOG_DIR"] = self._tmpdir
        os.environ["LOKI_URL"] = "https://example.com/loki/api/v1/push"

        # Подменяем __import__ чтобы он выбрасывал ImportError для logging_loki.
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "logging_loki":
                raise ImportError("No module named 'logging_loki'")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fake_import):
            import logging_config
            # Не должно выбросить исключение — только warning в лог.
            try:
                logging_config.configure_logging()
            except ImportError:
                self.fail(
                    "configure_logging не должен падать при отсутствии logging_loki"
                )

        root = logging.getLogger()
        types = [type(h).__name__ for h in root.handlers]
        self.assertEqual(len(root.handlers), 2,
                         f"Expected Stream+File only, got {types}")
        self.assertIn("StreamHandler", types)
        self.assertIn("TimedRotatingFileHandler", types)
        self.assertFalse(any("Loki" in t for t in types))

    def test_t4_log_dir_not_writable(self):
        """T4: LOG_DIR в недоступном каталоге → 1 handler (Stream), без File."""
        # Mock Path.mkdir чтобы он поднял PermissionError (наследник OSError).
        # Реально найти unwritable path на всех платформах сложно — root в
        # Docker может писать куда угодно. Mock — детерминированный способ.

        def fake_mkdir(self, *args, **kwargs):
            raise PermissionError(
                "[Errno 13] Permission denied (mock from test_v570)"
            )

        with patch("pathlib.Path.mkdir", fake_mkdir):
            import logging_config
            try:
                logging_config.configure_logging()
            except OSError:
                self.fail(
                    "configure_logging не должен валить старт при OSError в LOG_DIR"
                )

        root = logging.getLogger()
        types = [type(h).__name__ for h in root.handlers]
        self.assertEqual(len(root.handlers), 1,
                         f"Expected only Stream, got {types}")
        self.assertIn("StreamHandler", types)
        self.assertNotIn("TimedRotatingFileHandler", types)


class TestModuleExists(unittest.TestCase):
    """Санитарные проверки: файлы на месте, version в синхроне."""

    def test_t13_logging_config_module_exists(self):
        """T13: logging_config.py существует и экспортирует configure_logging."""
        self.assertTrue((ROOT / "logging_config.py").exists(),
                        "logging_config.py должен быть в корне репо")
        # Не импортируем (могут быть side-effects), проверяем исходник.
        content = (ROOT / "logging_config.py").read_text(encoding="utf-8")
        self.assertIn("def configure_logging", content,
                      "configure_logging должна быть определена")

    def test_t11_app_version_v570(self):
        """T11: APP_VERSION в web_app.py = 'v5.7.0'."""
        # Читаем исходник, без импорта (web_app требует heavy deps).
        content = (ROOT / "web_app.py").read_text(encoding="utf-8")
        self.assertIn('APP_VERSION = "v5.7.0"', content)

    def test_t12_pyproject_version(self):
        """T12: version в pyproject.toml = '5.7.0'."""
        content = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('version = "5.7.0"', content)

    def test_t14_logging_loki_in_deps(self):
        """T14: python-logging-loki в pyproject.toml dependencies."""
        content = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn("python-logging-loki", content,
                      "python-logging-loki должен быть в dependencies")

    def test_t15_changelog_v570_in_base_html(self):
        """T15: changelog-запись для v5.7.0 в templates/base.html."""
        content = (ROOT / "templates" / "base.html").read_text(encoding="utf-8")
        self.assertIn("v5.7.0", content)
        self.assertIn("Loki", content)


if __name__ == "__main__":
    unittest.main(verbosity=2)
