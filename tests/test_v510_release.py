"""v5.1.0 — релизные артефакты: /help из реестра, версия, changelog.

Запуск: uv run python tools/run_tests.py -k v510_release
"""
from _paths import _P  # noqa: E402
import json
import os
import re
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
        """v5.1.0 fix (ревью Task 10): раньше сравнивала _help_rows() саму
        с собой — _help_rows() это прямой цикл по commands.GROUP_COMMANDS,
        так что тест не мог упасть иначе как на внутренней ошибке разбора
        меток. Он не ловил реальный риск: забытый вызов _help_row() внутри
        секции _build_help_full_rich()/_build_help_moderator_rich() — эти
        секции курируются руками, а не генерируются циклом.

        Теперь проверяем ОТРЕНДЕРЕННЫЙ текст обоих Rich Message. mywarns и
        rules — Access.USER, публичные self-service команды, в мод-справке
        их не было и до реформы, поэтому они явно пропускаются.

        Поиск — через regex с границей слова после имени команды, не через
        `in`: подстрочный поиск даёт ложный "зелёный" при омонимах внутри
        текста справки — например "/mute" входит в "/mute_duration",
        "/warn" — в "/warns_mute" и "/resetwarns", "/ban" — в "/admin/bans".

        v5.1.0 (фикс round 2, ревью Task 10): раньше здесь было исключение
        для "alarm" — moderator-версия /help не показывала её, хотя она
        Access.MOD и handle_alarm_command пускает любого модератора (та же
        проверка, что у /ban и /mute). Расхождение было в справке, не в
        коде/реестре — починено добавлением /alarm в
        _build_help_moderator_rich(). Требование теперь единое для всех
        MOD-команд, без исключений.
        """
        import bot_handlers
        import commands

        full_json = json.dumps(
            bot_handlers._build_help_full_rich().model_dump(),
            default=str, ensure_ascii=False,
        )
        mod_json = json.dumps(
            bot_handlers._build_help_moderator_rich().model_dump(),
            default=str, ensure_ascii=False,
        )

        def _has_command(text: str, name: str) -> bool:
            return re.search(rf"/{re.escape(name)}\b", text) is not None

        for spec in commands.GROUP_COMMANDS:
            self.assertTrue(
                _has_command(full_json, spec.name),
                f"/{spec.name} отсутствует в отрендеренном full help",
            )
            # v5.1.0 (фикс финального ревью): /mywarns и /rules — Access.USER,
            # но теперь входят и в мод-справку тоже (модератор — тоже
            # участник чата), поэтому проверяем их наравне с MOD-командами.
            if spec.access in (commands.Access.MOD, commands.Access.USER):
                self.assertTrue(
                    _has_command(mod_json, spec.name),
                    f"/{spec.name} отсутствует в отрендеренном moderator help",
                )


class TestVersion(unittest.TestCase):
    # v5.2.0: проверка «APP_VERSION == v5.1.0» отсюда убрана. Она ломалась
    # на каждом следующем релизе, ничего при этом не защищая: текущую
    # версию пинует релизный тест текущей версии (test_v520_release.py).
    # Здесь остаётся то, что для v5.1.0 действительно инвариант — её
    # запись в changelog никуда не должна деться.

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
