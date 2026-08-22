"""v5.1.0 — формулировки публичных сообщений о наказаниях.

Единая схема «кто → что с ним сделали → по причине», русские «ёлочки».

Запуск: uv run python tools/run_tests.py -k v510_punishment_wording
"""
from _paths import _P  # noqa: E402
import os
import sys
import unittest

os.environ.setdefault("BOT_TOKEN", "test:test")
os.environ["ADMIN_IDS"] = "111"
os.environ["WEB_ALLOW_NO_SECRET"] = "1"
os.environ["DB_PATH"] = "/tmp/degramod_v510_wording.db"

sys.path.insert(0, _P())

from bot_handlers import _build_punishment_notice  # noqa: E402


class TestWording(unittest.TestCase):
    def test_mute(self):
        self.assertEqual(
            _build_punishment_notice("mute", "Vasya", "Флуд", 7200),
            "Пользователь «<b>Vasya</b>» был заглушён на <b>2ч</b> "
            "по причине: «<i>Флуд</i>»",
        )

    def test_ban(self):
        self.assertEqual(
            _build_punishment_notice("ban", "Vasya", "Скам", None),
            "Пользователь «<b>Vasya</b>» был забанен по причине: «<i>Скам</i>»",
        )

    def test_warn(self):
        self.assertEqual(
            _build_punishment_notice("warn", "Vasya", "Мат", None),
            "Пользователь «<b>Vasya</b>» получил предупреждение "
            "по причине: «<i>Мат</i>»",
        )

    def test_unknown_action_returns_none(self):
        self.assertIsNone(_build_punishment_notice("teleport", "Vasya", "x", None))

    def test_html_escaped(self):
        text = _build_punishment_notice("ban", "<script>", "a & b", None)
        self.assertIn("&lt;script&gt;", text)
        self.assertIn("a &amp; b", text)

    def test_no_latin_quotes_left(self):
        for action in ("mute", "ban", "warn"):
            text = _build_punishment_notice(action, "V", "R", 60)
            self.assertNotIn('"', text, f"{action}: остались латинские кавычки")


class TestViaBotWording(unittest.TestCase):
    def test_new_text_present(self):
        with open(_P("bot_handlers.py")) as f:
            src = f.read()
        self.assertIn("слишком много срал ботами и был заглушён на", src)

    def test_old_text_gone(self):
        with open(_P("bot_handlers.py")) as f:
            src = f.read()
        self.assertNotIn("задолбал срать в чат", src)

    def test_old_mute_wording_gone(self):
        with open(_P("bot_handlers.py")) as f:
            src = f.read()
        self.assertNotIn("замутан за", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
