"""v5.2.0 — снятие наказаний по @username и TGID, а не только реплаем.

До v5.2.0 команды снятия (/unmute, /unban, /unwarn, /warns, /resetwarns)
работали ТОЛЬКО по reply: в handle_group_command для них стояла жёсткая
ветка «нет reply → отказ», а их паттерны в commands.py вообще не имели
группы target. Забанить можно было по @username или TGID (v4.8.3), а
разбанить — нет: забаненный юзер уже не в чате, реплаить не на что.

Проверяет:
  • Паттерны пяти команд снятия получили опциональную группу target.
  • commands.unwarn_args — дизамбигуация «/unwarn 3»: с reply число это
    количество варнов, без reply — TGID цели.
  • _resolve_punishment_target(require_membership=False) — для снятия
    цель резолвится, даже если юзера уже нет в чате (он забанен/вышел):
    fallback на БД, затем синтетический User с одним id.
  • handle_group_command диспатчит команды снятия без reply.
  • /help и меню больше не обещают «(reply)» как единственный способ.

Запуск: uv run python tools/run_tests.py -k v520_unremove_by_target
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
os.environ["DB_PATH"] = "/tmp/degramod_v520_unremove.db"

sys.path.insert(0, _P())

from aiogram.exceptions import TelegramAPIError  # noqa: E402

import bot_handlers  # noqa: E402
import commands  # noqa: E402
from db import async_session, init_db  # noqa: E402

_REMOVAL = ("unmute", "unban", "unwarn", "warns", "resetwarns")


def _resolve(text: str):
    found = commands.resolve(text, None)
    assert found is not None, f"команда не распознана: {text!r}"
    return found


class TestRemovalPatternsAcceptTarget(unittest.TestCase):
    """Пять команд снятия получили опциональную группу target."""

    def test_unban_accepts_username(self):
        spec, m = _resolve("/unban @vasya")
        self.assertEqual(spec.name, "unban")
        self.assertEqual(m.group("target"), "@vasya")

    def test_unban_accepts_tgid(self):
        spec, m = _resolve("/unban 123456789")
        self.assertEqual(spec.name, "unban")
        self.assertEqual(m.group("target"), "123456789")

    def test_unban_without_target_still_matches(self):
        """Обратная совместимость: голый /unban (по reply) обязан работать."""
        spec, m = _resolve("/unban")
        self.assertEqual(spec.name, "unban")
        self.assertIsNone(m.group("target"))

    def test_unmute_accepts_target(self):
        spec, m = _resolve("/unmute @vasya")
        self.assertEqual(spec.name, "unmute")
        self.assertEqual(m.group("target"), "@vasya")

    def test_warns_accepts_target(self):
        spec, m = _resolve("/warns 123456789")
        self.assertEqual(spec.name, "warns")
        self.assertEqual(m.group("target"), "123456789")

    def test_resetwarns_accepts_target(self):
        spec, m = _resolve("/resetwarns @vasya")
        self.assertEqual(spec.name, "resetwarns")
        self.assertEqual(m.group("target"), "@vasya")

    def test_bang_prefix_still_works(self):
        """«!» остаётся рабочим алиасом и для новой формы."""
        spec, m = _resolve("!unban @vasya")
        self.assertEqual(spec.name, "unban")
        self.assertEqual(m.group("target"), "@vasya")

    def test_unwarn_target_and_count(self):
        spec, m = _resolve("/unwarn @vasya 3")
        self.assertEqual(spec.name, "unwarn")
        self.assertEqual(m.group("target"), "@vasya")
        self.assertEqual(m.group("count"), "3")

    def test_removal_commands_are_listed_as_lenient(self):
        """Цель снятия резолвится мягко — юзера может уже не быть в чате."""
        self.assertEqual(set(commands.LENIENT_TARGET), set(_REMOVAL))


class TestUnwarnArgsDisambiguation(unittest.TestCase):
    """«/unwarn 3» двусмысленно: 3 варна или TGID 3? Решает наличие reply.

    Таблица утверждена при проектировании — это её исполняемая копия.
    """

    @staticmethod
    def _args(text: str, has_reply: bool):
        _spec, m = _resolve(text)
        return commands.unwarn_args(m, has_reply=has_reply)

    def test_reply_bare_removes_one_warn(self):
        self.assertEqual(self._args("/unwarn", True), (None, 1))

    def test_reply_with_number_is_count(self):
        self.assertEqual(self._args("/unwarn 3", True), (None, 3))

    def test_username_without_count_removes_one(self):
        self.assertEqual(self._args("/unwarn @vasya", False), ("@vasya", 1))

    def test_username_with_count(self):
        self.assertEqual(self._args("/unwarn @vasya 3", False), ("@vasya", 3))

    def test_tgid_without_count_removes_one(self):
        self.assertEqual(self._args("/unwarn 123456789", False), ("123456789", 1))

    def test_tgid_with_count(self):
        self.assertEqual(self._args("/unwarn 123456789 3", False), ("123456789", 3))

    def test_number_without_reply_is_target_not_count(self):
        """Без reply одинокое число — TGID цели, а не количество варнов.

        Иначе «/unwarn 3» без reply молча снимал бы три варна непонятно
        с кого. Резолв такого TGID закономерно провалится, и модератор
        получит ошибку вместо тихого нечего-не-произошло.
        """
        self.assertEqual(self._args("/unwarn 3", False), ("3", 1))

    def test_count_zero_is_clamped_to_one(self):
        self.assertEqual(self._args("/unwarn @vasya 0", False), ("@vasya", 1))

    def test_bare_unwarn_without_reply_has_no_target(self):
        self.assertEqual(self._args("/unwarn", False), (None, 1))


class TestLenientTargetResolution(unittest.IsolatedAsyncioTestCase):
    """require_membership=False: цель резолвится, когда её нет в чате.

    Забаненный или вышедший юзер — штатная ситуация для /unban. Строгая
    ветка (для /ban) при этом обязана остаться строгой.
    """

    async def asyncSetUp(self):
        await init_db()

    @staticmethod
    def _message(chat_id: int = -1001234567890) -> MagicMock:
        msg = MagicMock()
        msg.text = "/unban 987654321"
        msg.caption = None
        msg.entities = None
        msg.caption_entities = None
        msg.reply_to_message = None
        msg.chat = SimpleNamespace(id=chat_id)
        msg.bot = MagicMock()
        return msg

    async def test_strict_mode_rejects_user_absent_from_chat(self):
        msg = self._message()
        msg.bot.get_chat_member = AsyncMock(
            side_effect=TelegramAPIError(method=None, message="user not found"),
        )
        user, err = await bot_handlers._resolve_punishment_target(
            msg, "987654321", msg.chat.id,
        )
        self.assertIsNone(user)
        self.assertIsNotNone(err)

    async def test_lenient_mode_falls_back_to_synthetic_user(self):
        msg = self._message()
        msg.bot.get_chat_member = AsyncMock(
            side_effect=TelegramAPIError(method=None, message="user not found"),
        )
        user, err = await bot_handlers._resolve_punishment_target(
            msg, "987654321", msg.chat.id, require_membership=False,
        )
        self.assertIsNone(err)
        self.assertIsNotNone(user)
        self.assertEqual(user.id, 987654321)

    async def test_lenient_mode_prefers_db_record_over_synthetic(self):
        """Имя и @username из БД — иначе отчёт о разбане будет безымянным."""
        async with async_session() as session:
            await bot_handlers._upsert_user(
                session, 987654322, "vasyaban", "Вася", "Баненый",
            )
            await session.commit()
        msg = self._message()
        msg.bot.get_chat_member = AsyncMock(
            side_effect=TelegramAPIError(method=None, message="user not found"),
        )
        user, err = await bot_handlers._resolve_punishment_target(
            msg, "987654322", msg.chat.id, require_membership=False,
        )
        self.assertIsNone(err)
        self.assertEqual(user.id, 987654322)
        self.assertEqual(user.username, "vasyaban")
        self.assertEqual(user.first_name, "Вася")

    async def test_lenient_mode_still_uses_chat_member_when_available(self):
        """Если Telegram цель знает — берём живой User, а не заглушку."""
        live = SimpleNamespace(
            id=987654323, is_bot=False, username="live",
            first_name="Живой", last_name="",
        )
        msg = self._message()
        msg.bot.get_chat_member = AsyncMock(
            return_value=SimpleNamespace(user=live),
        )
        user, err = await bot_handlers._resolve_punishment_target(
            msg, "987654323", msg.chat.id, require_membership=False,
        )
        self.assertIsNone(err)
        self.assertIs(user, live)

    async def test_reply_still_wins_over_argument(self):
        msg = self._message()
        replied = SimpleNamespace(
            id=555, is_bot=False, username="replied",
            first_name="Реплай", last_name="",
        )
        msg.reply_to_message = SimpleNamespace(from_user=replied)
        user, err = await bot_handlers._resolve_punishment_target(
            msg, "987654321", msg.chat.id, require_membership=False,
        )
        self.assertIsNone(err)
        self.assertIs(user, replied)


def _make_group_message(text: str, *, reply_to=None) -> MagicMock:
    msg = MagicMock()
    msg.text = text
    msg.caption = None
    msg.entities = None
    msg.caption_entities = None
    msg.reply_to_message = reply_to
    msg.chat = SimpleNamespace(id=-1001234567890, type="supergroup")
    msg.from_user = SimpleNamespace(
        id=111, username="moder", first_name="Модер", last_name="", is_bot=False,
    )
    msg.delete = AsyncMock()
    msg.bot = MagicMock()
    msg.bot.send_message = AsyncMock()
    msg.bot.get_chat_member = AsyncMock(
        side_effect=TelegramAPIError(method=None, message="user not found"),
    )
    return msg


class TestDispatcherAcceptsRemovalWithoutReply(unittest.IsolatedAsyncioTestCase):
    """handle_group_command больше не требует reply для команд снятия.

    Мокается только сам обработчик команды (mod_commands.COMMANDS) и
    граница с Telegram — резолв цели и решение «пускать или отказать»
    выполняются по-настоящему.
    """

    async def asyncSetUp(self):
        await init_db()
        for name, mock in (
            ("_is_admin", AsyncMock(return_value=True)),
            ("_schedule_ephemeral_delete", AsyncMock()),
        ):
            p = patch.object(bot_handlers, name, mock)
            p.start()
            self.addCleanup(p.stop)

        import mod_commands
        self.handler = AsyncMock()
        self.captured: list = []

        async def _capture(message, ctx):
            self.captured.append(ctx)

        self.patched = {}
        for cmd in _REMOVAL:
            p = patch.dict(mod_commands.COMMANDS, {cmd: _capture})
            p.start()
            self.addCleanup(p.stop)

    async def test_unban_by_tgid_without_reply_dispatches(self):
        msg = _make_group_message("/unban 987654321")
        await bot_handlers.handle_group_command(msg)
        self.assertEqual(len(self.captured), 1, "cmd_unban не был вызван")
        self.assertEqual(self.captured[0].target.id, 987654321)

    async def test_unban_without_reply_and_without_target_is_rejected(self):
        """Совсем без цели команда по-прежнему отказывает, а не молчит."""
        msg = _make_group_message("/unban")
        await bot_handlers.handle_group_command(msg)
        self.assertEqual(self.captured, [])
        msg.bot.send_message.assert_awaited()

    async def test_unwarn_by_username_without_reply_dispatches(self):
        async with async_session() as session:
            await bot_handlers._upsert_user(
                session, 987654330, "vasyawarn", "Вася", "",
            )
            await session.commit()
        msg = _make_group_message("/unwarn @vasyawarn 3")
        await bot_handlers.handle_group_command(msg)
        self.assertEqual(len(self.captured), 1)
        self.assertEqual(self.captured[0].target.id, 987654330)

    async def test_unmute_by_reply_still_works(self):
        replied = SimpleNamespace(
            id=777, is_bot=False, username="naru", first_name="Нару", last_name="",
        )
        reply = SimpleNamespace(
            from_user=replied, text="привет", caption=None, photo=None,
            video=None, sticker=None, animation=None, audio=None, voice=None,
            document=None, video_note=None, poll=None, location=None,
            contact=None, message_id=42,
        )
        msg = _make_group_message("/unmute", reply_to=reply)
        await bot_handlers.handle_group_command(msg)
        self.assertEqual(len(self.captured), 1)
        self.assertEqual(self.captured[0].target.id, 777)


class TestHelpTextsMentionTarget(unittest.TestCase):
    """Справка обязана рассказать про новый способ — иначе им не пользуются."""

    def test_removal_specs_hint_target(self):
        for name in _REMOVAL:
            spec = commands.spec_by_name(name)
            self.assertIsNotNone(spec, name)
            hint = spec.args_hint
            self.assertIn("@user", hint,
                          f"{name}: args_hint не упоминает @user ({hint!r})")

    def test_removal_descriptions_do_not_promise_reply_only(self):
        for name in ("unmute", "unban"):
            spec = commands.spec_by_name(name)
            self.assertNotIn("(reply)", spec.description,
                             f"{name}: описание всё ещё обещает только reply")


if __name__ == "__main__":
    unittest.main(verbosity=2)
