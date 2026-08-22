"""v5.1.0 — /mywarns показывает прогресс до ближайшего порога.

Пороги warns_to_mute и warns_to_ban независимы, 0 = выключен. В
_check_warn_threshold бан проверяется раньше мьюта, поэтому при равных
порогах показывается бан.

Запуск: uv run python tools/run_tests.py -k v510_mywarns
"""
from _paths import _P  # noqa: E402
import os
import sys
import unittest

os.environ.setdefault("BOT_TOKEN", "test:test")
os.environ["ADMIN_IDS"] = "111"
os.environ["WEB_ALLOW_NO_SECRET"] = "1"
os.environ["DB_PATH"] = "/tmp/degramod_v510_mywarns.db"

sys.path.insert(0, _P())

from bot_handlers import _format_warn_progress  # noqa: E402


class TestWarnProgress(unittest.TestCase):
    def test_only_mute_threshold(self):
        self.assertEqual(_format_warn_progress(2, 3, 0), "2/3 (до заглушения)")

    def test_only_ban_threshold(self):
        self.assertEqual(_format_warn_progress(2, 0, 5), "2/5 (до бана)")

    def test_both_thresholds_picks_nearest(self):
        self.assertEqual(_format_warn_progress(2, 3, 5), "2/3 (до заглушения)")

    def test_equal_thresholds_ban_wins(self):
        # _check_warn_threshold проверяет бан первым.
        self.assertEqual(_format_warn_progress(2, 3, 3), "2/3 (до бана)")

    def test_no_thresholds_plain_count(self):
        self.assertEqual(_format_warn_progress(2, 0, 0), "2")

    def test_thresholds_already_passed_plain_count(self):
        # Порог выключили задним числом или понизили — дробь соврала бы.
        self.assertEqual(_format_warn_progress(7, 3, 5), "7")

    def test_none_treated_as_disabled(self):
        self.assertEqual(_format_warn_progress(2, None, None), "2")

    def test_mute_nearer_than_ban_when_ban_passed(self):
        self.assertEqual(_format_warn_progress(4, 6, 5), "4/5 (до бана)")


class TestRegistryWiring(unittest.TestCase):
    def test_pattern_moved_to_registry(self):
        with open(_P("bot_handlers.py")) as f:
            src = f.read()
        self.assertNotIn("_CMD_MYWARNS", src,
                         "паттерн должен приехать из commands.py")

    def test_mywarns_filter_exists(self):
        with open(_P("bot_handlers.py")) as f:
            src = f.read()
        self.assertIn("class _MywarnsFilter", src)

    def test_both_prefixes_resolve(self):
        import commands
        for text in ("/mywarns", "!mywarns"):
            with self.subTest(text=text):
                found = commands.resolve(text, "degradach_bot")
                self.assertIsNotNone(found)
                self.assertEqual(found[0].name, "mywarns")


if __name__ == "__main__":
    unittest.main(verbosity=2)
