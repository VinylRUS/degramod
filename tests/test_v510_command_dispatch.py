"""v5.1.0 — диспетчер групповых команд: права, удаление, кулдаун отказа.

Проверяет, что обычный участник получает ephemeral-отказ вместо тишины,
что команда удаляется в любом случае и что повторный отказ гасится
кулдауном. Плюс два решения контроллера, не покрытые брифом:
  • /alarm переведён на реестр (commands.resolve), а не только 15 команд
    handle_group_command;
  • /alarm от юзера без прав ведёт себя как /ban — удаление + ephemeral-
    отказ, а не стелс-молчание.

Запуск: uv run python tools/run_tests.py -k v510_command_dispatch
"""
from _paths import _P  # noqa: E402
import os
import sys
import unittest

os.environ.setdefault("BOT_TOKEN", "test:test")
os.environ["ADMIN_IDS"] = "111"
os.environ["WEB_ALLOW_NO_SECRET"] = "1"
os.environ["DB_PATH"] = "/tmp/degramod_v510_dispatch.db"

sys.path.insert(0, _P())

import re  # noqa: E402


class TestPatternsLostBangAnchor(unittest.TestCase):
    """Паттерны переехали в commands.py и больше не якорятся на «!»."""

    def test_bot_handlers_has_no_bang_anchored_patterns(self):
        with open(_P("bot_handlers.py")) as f:
            src = f.read()
        leftovers = re.findall(r'r"\^!\w+', src)
        # v5.1.0 (решение 1 контроллера): _CMD_MYWARNS = re.compile(r"^!mywarns\s*$")
        # снимается не в этой задаче, а в Task 3 (миграция /mywarns на реестр).
        # Пока он жив в bot_handlers.py и матчится этим тестом — исключаем.
        leftovers = [x for x in leftovers if "mywarns" not in x]
        self.assertEqual(leftovers, [],
                         f"остались якоря на !: {leftovers}")

    def test_filter_renamed(self):
        with open(_P("bot_handlers.py")) as f:
            src = f.read()
        self.assertIn("class _KnownCommandFilter", src)
        self.assertNotIn("class _ModerationCommandFilter", src)
        # Декоратор handle_group_command должен ссылаться на новый фильтр.
        self.assertIn("_KnownCommandFilter()", src)

    def test_denied_cooldown_constant_present(self):
        with open(_P("bot_handlers.py")) as f:
            src = f.read()
        self.assertIn("_DENIED_COOLDOWN_SECONDS = 60", src)


class TestDeniedCooldown(unittest.TestCase):
    """Кулдаун отказа — чистая логика, проверяется без Telegram."""

    def setUp(self):
        import bot_handlers
        self.bh = bot_handlers
        self.bh._denied_last_call.clear()

    def test_prune_removes_stale_entries(self):
        self.bh._denied_last_call[(1, -100)] = 0.0
        self.bh._denied_prune_stale(now=self.bh._DENIED_COOLDOWN_SECONDS + 1)
        self.assertEqual(self.bh._denied_last_call, {})

    def test_prune_keeps_fresh_entries(self):
        self.bh._denied_last_call[(1, -100)] = 100.0
        self.bh._denied_prune_stale(now=101.0)
        self.assertIn((1, -100), self.bh._denied_last_call)

    def test_key_order_is_user_then_chat(self):
        # Совпадает с конвенцией _mywarns_last_call.
        self.bh._denied_last_call[(42, -100500)] = 1.0
        (user_id, chat_id), = self.bh._denied_last_call
        self.assertEqual(user_id, 42)
        self.assertEqual(chat_id, -100500)


class TestResolveDrivesDispatch(unittest.TestCase):
    """Диспетчер опирается на реестр, а не на каскад .match()."""

    def test_dispatcher_uses_commands_resolve(self):
        with open(_P("bot_handlers.py")) as f:
            src = f.read()
        self.assertIn("commands.resolve(", src)

    def test_punitive_check_uses_registry(self):
        with open(_P("bot_handlers.py")) as f:
            src = f.read()
        self.assertIn("commands.PUNITIVE", src)


class TestAlarmOnRegistry(unittest.TestCase):
    """Решение 2 контроллера: /alarm тоже переведён на commands.resolve.

    _CMD_ALARM удалён вместе с остальными паттернами (Step 3), поэтому
    _AlarmCommandFilter и handle_alarm_command обязаны резолвить команду
    через реестр, а не через свою локальную копию regex.
    """

    def setUp(self):
        with open(_P("bot_handlers.py")) as f:
            self.src = f.read()

    def test_cmd_alarm_removed(self):
        # Историческое упоминание в комментариях допустимо — важно, что
        # определения-паттерна (re.compile) больше нет.
        self.assertNotIn("_CMD_ALARM = re.compile", self.src)

    def test_alarm_filter_uses_registry(self):
        idx = self.src.find("class _AlarmCommandFilter")
        self.assertGreater(idx, 0, "_AlarmCommandFilter class not found")
        # Тело класса — от определения до следующего top-level class/def.
        tail = self.src[idx:]
        end = tail.find("\nclass ", 1)
        body = tail[:end] if end > 0 else tail
        self.assertIn("commands.resolve(", body,
                      "_AlarmCommandFilter должен резолвить команду через реестр")

    def test_handle_alarm_command_uses_registry(self):
        idx = self.src.find("async def handle_alarm_command(")
        self.assertGreater(idx, 0, "handle_alarm_command not found")
        # Именованные группы вместо позиционных — group("state")/group("amount")/group("unit").
        section = self.src[idx:idx + 4000]
        self.assertIn('.group("state")', section)
        self.assertIn('.group("amount")', section)
        self.assertIn('.group("unit")', section)
        # Позиционных group(1)/group(2)/group(3) из старого _CMD_ALARM.match быть не должно.
        self.assertNotIn("m.group(1)", section)
        self.assertNotIn("m.group(2)", section)
        self.assertNotIn("m.group(3)", section)


class TestAlarmDeniedIsNotStealth(unittest.TestCase):
    """Решение 3 контроллера: /alarm без прав ведёт себя как /ban, не молчит.

    Раньше в handle_alarm_command стоял стелс-возврат: юзер без прав
    получал полную тишину. Теперь — как у любой другой мод-команды:
    удаление сообщения + ephemeral через _send_access_denied.
    """

    def test_no_stealth_return_comment(self):
        with open(_P("bot_handlers.py")) as f:
            src = f.read()
        self.assertNotIn("Молча игнорируем — стелс", src,
                         "стелс-комментарий для /alarm без прав должен быть удалён")

    def test_handle_alarm_command_calls_send_access_denied(self):
        with open(_P("bot_handlers.py")) as f:
            src = f.read()
        idx = src.find("async def handle_alarm_command(")
        self.assertGreater(idx, 0)
        # Следующий top-level handler ("@router.message" -> "async def handle_")
        # после handle_alarm_command — берём тело до него.
        tail = src[idx:]
        end = tail.find("\n\n\n", 1)
        body = tail[:end] if end > 0 else tail
        self.assertIn("_send_access_denied(", body,
                      "handle_alarm_command должен звать _send_access_denied при отказе")


if __name__ == "__main__":
    unittest.main(verbosity=2)
