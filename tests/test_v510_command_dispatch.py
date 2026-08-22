"""v5.1.0 — диспетчер групповых команд: права, удаление, кулдаун отказа.

Проверяет, что обычный участник получает ephemeral-отказ вместо тишины,
что команда удаляется в любом случае и что повторный отказ гасится
кулдауном. Плюс два решения контроллера, не покрытые брифом:
  • /alarm переведён на реестр (commands.resolve), а не только 15 команд
    handle_group_command;
  • /alarm от юзера без прав ведёт себя как /ban — удаление + ephemeral-
    отказ, а не стелс-молчание.

Фикс ревью (находки 1 и 2, см. task-2-report.md):
  • TestKnownCommandFilterScope — _KnownCommandFilter матчит по
    spec.access, а не по факту регистрации хендлера в файле: Access.USER
    и alarm обязаны НЕ матчиться, MOD/ADMIN — обязаны.
  • TestAccessDeniedBehavior — поведенческий тест на сам механизм отказа:
    удаление сообщения, ровно один ephemeral за кулдаун, точный текст.
    Раньше в этом файле _send_access_denied не вызывался ни разу.

Запуск: uv run python tools/run_tests.py -k v510_command_dispatch
"""
from _paths import _P  # noqa: E402
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("BOT_TOKEN", "test:test")
os.environ["ADMIN_IDS"] = "111"
os.environ["WEB_ALLOW_NO_SECRET"] = "1"
os.environ["DB_PATH"] = "/tmp/degramod_v510_dispatch.db"

sys.path.insert(0, _P())

import re  # noqa: E402

import bot_handlers  # noqa: E402
from db import init_db  # noqa: E402


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


class TestKnownCommandFilterScope(unittest.IsolatedAsyncioTestCase):
    """Находка 1 ревью: область фильтра — по spec.access, не по регистрации.

    _KnownCommandFilter обязан матчить ровно то, что реально диспатчит
    handle_group_command: Access.MOD/ADMIN, кроме alarm (у него отдельный
    хендлер). Access.USER (mywarns, rules) и alarm обязаны НЕ матчиться —
    иначе их судьба снова зависит от того, выше или ниже
    handle_group_command в файле на 8.5k строк объявлен их хендлер. Именно
    так уже дважды ломались !ban и !alarm в проде.
    """

    @staticmethod
    async def _matches(text: str) -> bool:
        message = SimpleNamespace(text=text, caption=None)
        return await bot_handlers._KnownCommandFilter()(message)

    async def test_user_access_commands_do_not_match(self):
        self.assertFalse(await self._matches("/mywarns"))
        self.assertFalse(await self._matches("/rules"))

    async def test_alarm_does_not_match(self):
        # У alarm свой хендлер (_AlarmCommandFilter) — этот фильтр должен
        # пропустить propagation дальше, а не перехватить его первым.
        self.assertFalse(await self._matches("/alarm on"))
        self.assertFalse(await self._matches("!alarm off"))

    async def test_mod_and_admin_commands_match(self):
        self.assertTrue(await self._matches("/ban @vasya спам"))
        self.assertTrue(await self._matches("/mute 10m спам"))
        self.assertTrue(await self._matches("/resetwarns"))

    async def test_unknown_command_does_not_match(self):
        # /roll соседнего бота — не в реестре, должен провалиться дальше
        # в handle_content_filters, а не зависнуть здесь.
        self.assertFalse(await self._matches("/roll"))


def _make_denied_group_message(
    text: str = "/mute 10m спам",
    user_id: int = 555000111,
    chat_id: int = -1001234567890,
) -> MagicMock:
    """Сообщение мод-командой от участника без прав модерации."""
    msg = MagicMock()
    msg.text = text
    msg.caption = None
    msg.reply_to_message = None
    msg.chat = SimpleNamespace(id=chat_id)
    msg.from_user = SimpleNamespace(
        id=user_id, username="participant", first_name="Юзер",
        last_name="", is_bot=False,
    )
    msg.delete = AsyncMock()
    msg.bot = MagicMock()
    msg.bot.send_message = AsyncMock()
    return msg


class TestAccessDeniedBehavior(unittest.IsolatedAsyncioTestCase):
    """Находка 2 ревью: поведенческий тест на ephemeral-отказ и кулдаун.

    Мокается только граница с Telegram (message.bot.send_message,
    message.delete). _is_admin подменяется — вычисление прав не предмет
    этого теста (оно покрыто через реальную БД в других файлах), а сама
    проверяемая логика — кулдаун, удаление, текст отказа — выполняется
    по-настоящему, через handle_group_command и _send_access_denied.
    """

    async def asyncSetUp(self):
        await init_db()
        bot_handlers._denied_last_call.clear()
        # Реальный _schedule_ephemeral_delete спавнит фоновую таску со
        # sleep(30) — переживёт тест. Само авто-удаление ephemeral не
        # предмет этой находки, оно проверяется в test_v456_ephemeral_autodelete.py.
        sched_patcher = patch.object(
            bot_handlers, "_schedule_ephemeral_delete", AsyncMock(),
        )
        sched_patcher.start()
        self.addCleanup(sched_patcher.stop)
        is_admin_patcher = patch.object(
            bot_handlers, "_is_admin", AsyncMock(return_value=False),
        )
        is_admin_patcher.start()
        self.addCleanup(is_admin_patcher.stop)

    async def asyncTearDown(self):
        bot_handlers._denied_last_call.clear()

    async def test_denied_command_is_deleted(self):
        """Команда участника без прав удаляется из чата."""
        msg = _make_denied_group_message()
        await bot_handlers.handle_group_command(msg)
        msg.delete.assert_awaited_once()

    async def test_denied_command_sends_exactly_one_ephemeral(self):
        """За один отказ уходит ровно один ephemeral."""
        msg = _make_denied_group_message()
        await bot_handlers.handle_group_command(msg)
        msg.bot.send_message.assert_awaited_once()

    async def test_denied_text_matches_spec(self):
        """Текст отказа — тот, что задуман брифом, а не что-то произвольное."""
        msg = _make_denied_group_message()
        await bot_handlers.handle_group_command(msg)
        kwargs = msg.bot.send_message.await_args.kwargs
        self.assertEqual(kwargs["text"], "❌ У вас нет прав на эту команду.")

    async def test_second_denial_within_cooldown_sends_nothing(self):
        """Второй отказ в пределах 60с не шлёт повторный ephemeral.

        Команда при этом всё равно удаляется каждый раз — гасится только
        повторный ответ, не сама модерация сообщения.
        """
        first = _make_denied_group_message()
        await bot_handlers.handle_group_command(first)

        second = _make_denied_group_message()
        await bot_handlers.handle_group_command(second)

        second.bot.send_message.assert_not_awaited()
        second.delete.assert_awaited_once()

    async def test_send_access_denied_direct_cooldown(self):
        """_send_access_denied напрямую: второй вызов подряд ничего не шлёт."""
        msg = _make_denied_group_message()
        await bot_handlers._send_access_denied(msg, msg.chat.id, msg.from_user)
        await bot_handlers._send_access_denied(msg, msg.chat.id, msg.from_user)
        msg.bot.send_message.assert_awaited_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
