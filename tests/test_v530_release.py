"""v5.3.0 — релизные артефакты: версия, changelog, roadmap, инвариант в CLAUDE.md.

Инвариант «своих не удаляем» держится на четырёх ветках guard'а, которые
легко «упростить» до «нет в списке — удалить». Поэтому он записан не только
в тесте, но и в CLAUDE.md — файле, который читает следующий, кто сюда
придёт.

Запуск: uv run python tools/run_tests.py -k v530_release
"""
from _paths import _P  # noqa: E402
import os
import sys
import unittest

os.environ.setdefault("BOT_TOKEN", "test:test")
os.environ["ADMIN_IDS"] = "111"
os.environ["WEB_PASSWORD"] = "testpass"
os.environ["WEB_ALLOW_NO_SECRET"] = "1"
os.environ["DB_PATH"] = "/tmp/degramod_v530_release.db"

sys.path.insert(0, _P())

_VERSION = "v5.3.0"


class TestVersion(unittest.TestCase):
    def test_app_version(self):
        import web_app
        self.assertEqual(web_app.APP_VERSION, _VERSION)


class TestChangelog(unittest.TestCase):
    def setUp(self):
        with open(_P("templates/base.html")) as f:
            self.html = f.read()

    def test_entry_exists_and_is_first(self):
        self.assertIn(f"<strong>{_VERSION}</strong>", self.html)
        self.assertLess(
            self.html.index(f"<strong>{_VERSION}</strong>"),
            self.html.index("<strong>v5.2.0</strong>"),
        )

    def test_entry_states_that_own_channel_is_never_deleted(self):
        """Пользователь обязан прочитать про защиту своих в changelog:
        иначе включит тумблер и будет ждать, что снесёт собственные посты."""
        head = self.html[self.html.index(f"<strong>{_VERSION}</strong>"):
                         self.html.index("<strong>v5.2.0</strong>")]
        self.assertIn("никогда", head.lower())
        self.assertIn("/channelallow", head)


class TestInvariantIsDocumented(unittest.TestCase):
    def test_claude_md_describes_the_guard(self):
        with open(_P("CLAUDE.md")) as f:
            md = f.read()
        self.assertIn("_channel_guard_reason", md)
        self.assertIn("channel_whitelist", md)


class TestRoadmap(unittest.TestCase):
    def test_release_recorded(self):
        with open(_P("roadmap.md")) as f:
            self.assertIn(_VERSION, f.read())


if __name__ == "__main__":
    unittest.main(verbosity=2)
