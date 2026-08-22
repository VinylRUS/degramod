"""v5.1.0 — релизные артефакты: /help из реестра, версия, changelog.

Запуск: uv run python tools/run_tests.py -k v510_release
"""
from _paths import _P  # noqa: E402
import os
import sys
import unittest

os.environ.setdefault("BOT_TOKEN", "test:test")
os.environ["ADMIN_IDS"] = "111"
os.environ["WEB_PASSWORD"] = "testpass"
os.environ["WEB_ALLOW_NO_SECRET"] = "1"
os.environ["DB_PATH"] = "/tmp/degramod_v510_release.db"

sys.path.insert(0, _P())


class TestHelpFromRegistry(unittest.TestCase):
    def test_help_rows_use_slash(self):
        import bot_handlers
        rows = bot_handlers._help_rows()
        self.assertTrue(rows)
        for label, description in rows:
            self.assertTrue(label.startswith("/"), f"{label} не на слэше")
            self.assertTrue(description.strip())

    def test_no_bang_commands_in_help_source(self):
        with open(_P("bot_handlers.py")) as f:
            src = f.read()
        for name in ("!mute", "!ban", "!warn", "!sban", "!alarm", "!unwarn"):
            self.assertNotIn(f'("{name}', src, f"{name} остался в /help")

    def test_help_covers_every_mod_command(self):
        import bot_handlers
        import commands
        labels = {label.split()[0].lstrip("/") for label, _ in bot_handlers._help_rows()}
        for spec in commands.GROUP_COMMANDS:
            self.assertIn(spec.name, labels, f"/{spec.name} отсутствует в /help")


class TestVersion(unittest.TestCase):
    def test_app_version(self):
        import web_app
        self.assertEqual(web_app.APP_VERSION, "v5.1.0")

    def test_changelog_entry(self):
        with open(_P("templates/base.html")) as f:
            html = f.read()
        self.assertIn("v5.1.0", html)


class TestRoadmap(unittest.TestCase):
    def test_bothost_moved_to_v520(self):
        with open(_P("roadmap.md")) as f:
            md = f.read()
        self.assertIn("v5.2.0", md,
                      "bothost-задачи должны быть перенесены на v5.2.0")


if __name__ == "__main__":
    unittest.main(verbosity=2)
