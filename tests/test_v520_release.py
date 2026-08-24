"""v5.2.0 — релизные артефакты: версия, дата, changelog, roadmap.

Changelog в templates/base.html — фактический источник истины по релизам
(он подробнее roadmap.md и обычно свежее). Версия без записи в нём
означает, что владелец бота не узнает, что изменилось.

Запуск: uv run python tools/run_tests.py -k v520_release
"""
from _paths import _P  # noqa: E402
import os
import sys
import unittest

os.environ.setdefault("BOT_TOKEN", "test:test")
os.environ["ADMIN_IDS"] = "111"
os.environ["WEB_PASSWORD"] = "testpass"
os.environ["WEB_ALLOW_NO_SECRET"] = "1"
os.environ["DB_PATH"] = "/tmp/degramod_v520_release.db"

sys.path.insert(0, _P())

_VERSION = "v5.2.0"


class TestVersion(unittest.TestCase):
    # v5.3.0: проверка «APP_VERSION == v5.2.0» убрана — она ломается на
    # каждом следующем релизе, ничего при этом не защищая. Текущую версию
    # пинует релизный тест текущей версии (см. test_v530_release.py).
    def test_release_date_moved(self):
        """Дата сборки должна быть новой — иначе пилюля в футере врёт."""
        import web_app
        self.assertEqual(web_app.APP_RELEASE_DATE, "2026-08-24")


class TestChangelog(unittest.TestCase):
    def setUp(self):
        with open(_P("templates/base.html")) as f:
            self.html = f.read()

    def test_entry_exists(self):
        self.assertIn(f"<strong>{_VERSION}</strong>", self.html)

    def test_entry_is_first(self):
        """Свежий релиз идёт первым — модалка листается сверху вниз."""
        self.assertLess(
            self.html.index(f"<strong>{_VERSION}</strong>"),
            self.html.index("<strong>v5.1.0</strong>"),
        )

    def test_entry_covers_both_features(self):
        head = self.html[self.html.index(f"<strong>{_VERSION}</strong>"):
                         self.html.index("<strong>v5.1.0</strong>")]
        self.assertIn("в ответ на", head.lower())
        self.assertIn("/unban", head)


class TestRoadmap(unittest.TestCase):
    def test_release_recorded(self):
        with open(_P("roadmap.md")) as f:
            md = f.read()
        self.assertIn(_VERSION, md)


if __name__ == "__main__":
    unittest.main(verbosity=2)
