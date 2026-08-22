"""v5.1.0 — реестр команд и нормализация префикса.

Модуль commands.py не зависит ни от aiogram, ни от БД, поэтому тестируется
напрямую, без поднятия бота.

Запуск: uv run python tools/run_tests.py -k v510_commands
"""
from _paths import _P  # noqa: E402
import sys
import unittest

sys.path.insert(0, _P())

import commands  # noqa: E402
import mod_commands  # noqa: E402


class TestStripPrefix(unittest.TestCase):
    def test_slash_prefix(self):
        self.assertEqual(commands.strip_prefix("/ban @vasya Скам", "degradach_bot"),
                         "ban @vasya Скам")

    def test_bang_prefix_still_works(self):
        self.assertEqual(commands.strip_prefix("!ban @vasya Скам", "degradach_bot"),
                         "ban @vasya Скам")

    def test_own_username_suffix_stripped(self):
        self.assertEqual(commands.strip_prefix("/ban@degradach_bot Скам", "degradach_bot"),
                         "ban Скам")

    def test_username_match_is_case_insensitive(self):
        self.assertEqual(commands.strip_prefix("/ban@DegraDach_Bot Скам", "degradach_bot"),
                         "ban Скам")

    def test_foreign_username_rejected(self):
        self.assertIsNone(commands.strip_prefix("/ban@other_bot Скам", "degradach_bot"))

    def test_unknown_own_username_rejects_explicit_mention(self):
        # Username ещё не известен (гонка на старте) — явно адресованную
        # команду не берём, чтобы не перехватить чужую.
        self.assertIsNone(commands.strip_prefix("/ban@degradach_bot Скам", None))

    def test_bare_command_works_without_known_username(self):
        self.assertEqual(commands.strip_prefix("/ban Скам", None), "ban Скам")

    def test_not_a_command(self):
        self.assertIsNone(commands.strip_prefix("просто текст", "degradach_bot"))
        self.assertIsNone(commands.strip_prefix("", "degradach_bot"))
        self.assertIsNone(commands.strip_prefix(None, "degradach_bot"))
        self.assertIsNone(commands.strip_prefix("/", "degradach_bot"))

    def test_leading_whitespace_tolerated(self):
        self.assertEqual(commands.strip_prefix("  /ban Скам", "degradach_bot"), "ban Скам")

    def test_multiline_command_keeps_tail(self):
        # Причина может быть многострочной — DOTALL обязателен.
        self.assertEqual(commands.strip_prefix("/ban Скам\nи флуд", "degradach_bot"),
                         "ban Скам\nи флуд")


class TestResolve(unittest.TestCase):
    def test_resolves_ban_with_named_groups(self):
        found = commands.resolve("/ban @vasya Скам", "degradach_bot")
        self.assertIsNotNone(found)
        spec, m = found
        self.assertEqual(spec.name, "ban")
        self.assertEqual(m.group("target"), "@vasya")
        self.assertEqual(m.group("reason"), "Скам")

    def test_resolves_mute_duration(self):
        spec, m = commands.resolve("/mute 2ч Флуд", "degradach_bot")
        self.assertEqual(spec.name, "mute")
        self.assertEqual(m.group("dur"), "2ч")
        self.assertEqual(m.group("reason"), "Флуд")

    def test_ban_without_reason_does_not_resolve(self):
        # v4.8.6: negative lookahead — «/ban @vasya» без причины не матчится.
        self.assertIsNone(commands.resolve("/ban @vasya", "degradach_bot"))

    def test_foreign_command_does_not_resolve(self):
        self.assertIsNone(commands.resolve("/roll 2d6", "degradach_bot"))

    def test_every_registry_entry_resolves_itself(self):
        # Защита от опечатки в паттерне: каждая команда обязана матчить
        # собственный минимальный пример.
        samples = {
            "ban": "/ban Скам",
            "sban": "/sban",
            "warn": "/warn Мат",
            "swarn": "/swarn",
            "mute": "/mute 2ч Флуд",
            "smute": "/smute 2ч",
            "unmute": "/unmute",
            "unban": "/unban",
            "unwarn": "/unwarn",
            "warns": "/warns",
            "resetwarns": "/resetwarns",
            "resetmc": "/resetmc",
            "alarm": "/alarm on",
            "mywarns": "/mywarns",
            "rules": "/rules",
        }
        for spec in commands.GROUP_COMMANDS:
            with self.subTest(command=spec.name):
                self.assertIn(spec.name, samples,
                              f"нет примера для команды {spec.name}")
                found = commands.resolve(samples[spec.name], "degradach_bot")
                self.assertIsNotNone(found, f"/{spec.name} не резолвится")
                self.assertEqual(found[0].name, spec.name)


class TestRegistryShape(unittest.TestCase):
    def test_names_unique(self):
        names = [s.name for s in commands.GROUP_COMMANDS]
        self.assertEqual(len(names), len(set(names)), "дубли имён в GROUP_COMMANDS")

    def test_only_user_commands_in_menu(self):
        # Мод-команды не публикуются в меню — см. спеку, раздел «Меню команд».
        for spec in commands.GROUP_COMMANDS:
            if spec.in_menu:
                self.assertEqual(spec.access, commands.Access.USER,
                                 f"/{spec.name} в меню, но не USER")

    def test_mywarns_and_rules_are_in_menu(self):
        in_menu = {s.name for s in commands.GROUP_COMMANDS if s.in_menu}
        self.assertEqual(in_menu, {"mywarns", "rules"})

    def test_punitive_set_matches_registry(self):
        for name in commands.PUNITIVE:
            self.assertIsNotNone(commands.spec_by_name(name),
                                 f"{name} в PUNITIVE, но не в реестре")

    def test_every_spec_has_description(self):
        for spec in commands.GROUP_COMMANDS:
            with self.subTest(command=spec.name):
                self.assertTrue(spec.description.strip())

    def test_dm_menu_commands_shape(self):
        # v5.1.0: состав меню личных чатов не должен разъезжаться
        dm_names = {name for name, _desc in commands.DM_MENU_COMMANDS}
        # 1. Состав ровно {start, help, mywarns, rules}
        self.assertEqual(dm_names, {"start", "help", "mywarns", "rules"},
                         "DM_MENU_COMMANDS должна содержать ровно start, help, mywarns, rules")
        # 2. Не должно быть административных команд
        forbidden = {"bansticker", "setkeywords", "linkallow", "setmodchat"}
        self.assertTrue(dm_names.isdisjoint(forbidden),
                        f"DM_MENU_COMMANDS содержит административные команды: {dm_names & forbidden}")
        # 3. Каждая запись — пара из двух непустых строк
        self.assertEqual(len(commands.DM_MENU_COMMANDS), len(dm_names),
                         "DM_MENU_COMMANDS содержит дубли имён")
        for name, desc in commands.DM_MENU_COMMANDS:
            self.assertIsInstance(name, str, f"{name} — не строка")
            self.assertIsInstance(desc, str, f"описание {name} — не строка")
            self.assertTrue(name.strip(), f"имя команды пусто: {name!r}")
            self.assertTrue(desc.strip(), f"описание команды {name} пусто: {desc!r}")


class TestDispatchTableMatchesRegistry(unittest.TestCase):
    """Находка 1 финального ревью (a24bae8): реестр и диспетчер связаны
    только фактом, не тестом. handle_group_command берёт обработчик из
    mod_commands.COMMANDS по имени команды, зарезолвленной из
    commands.GROUP_COMMANDS (bot_handlers.py, диспетч в конце
    handle_group_command). Команда, добавленная в реестр с Access.MOD/ADMIN
    и забытая в COMMANDS, пройдёт фильтр и проверку прав — но сообщение
    модератора к этому моменту уже удалено (auto_delete_commands), а
    наказание не выполнится: ни действия, ни лога, ни ответа. Это ровно тот
    баг прода (!ban/!alarm), ради которого затевался релиз v5.1.0.

    alarm легитимно исключён: у него отдельный хендлер (handle_alarm_command
    через _AlarmCommandFilter), _KnownCommandFilter его не матчит, и в
    mod_commands.COMMANDS ему не место.
    """

    def test_mod_admin_commands_have_dispatch_handler(self):
        registry_names = {
            s.name for s in commands.GROUP_COMMANDS
            if s.access != commands.Access.USER
        } - {"alarm"}
        self.assertEqual(registry_names, set(mod_commands.COMMANDS))


class TestBotUsernameGlobal(unittest.TestCase):
    def test_set_and_get(self):
        commands.set_bot_username("degradach_bot")
        self.assertEqual(commands.get_bot_username(), "degradach_bot")
        commands.set_bot_username(None)
        self.assertIsNone(commands.get_bot_username())


if __name__ == "__main__":
    unittest.main(verbosity=2)
