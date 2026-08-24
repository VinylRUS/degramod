"""v5.3.0 — белый список каналов: удаление сообщений от имени чужих каналов.

Юзер может писать в группу от имени своего канала. Для спамера это способ
обойти персональные ограничения: замьютили человека — он продолжает от
имени канала. Фильтр удаляет такие сообщения, кроме внесённых в белый
список.

⚠️ Главный инвариант (см. CLAUDE.md): сообщения от имени САМОЙ группы
(анонимные админы) и от имени СВЯЗАННОГО канала не удаляются никогда —
безусловно, до всякой проверки белого списка. Это хардкод-guard, а не
запись в списке: запись можно случайно удалить кнопкой в веб-панели, и
тогда чат обсуждений потерял бы все посты вместе с ветками комментариев,
а группа — команды своих анонимных админов.

Проверяет:
  • _channel_guard_reason — четыре безусловных пропуска.
  • Инвариант в злом случае: тумблер включён, белый список ПУСТ — свои
    всё равно живы.
  • Матчинг белого списка: по channel_id, глобально, по @username с
    дописыванием id при первой встрече.
  • Тумблер chat_settings.delete_channel_messages (по умолчанию выкл).
  • _ChannelMessageMiddleware: удаляет чужих, пропускает своих, не роняет
    обработку при отказе в удалении.

Запуск: uv run python tools/run_tests.py -k v530_channel_whitelist
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
os.environ["DB_PATH"] = "/tmp/degramod_v530_channels.db"

sys.path.insert(0, _P())

from aiogram.exceptions import TelegramAPIError  # noqa: E402

import bot_handlers  # noqa: E402
from db import ChannelWhitelist, ChatSettings, async_session, init_db  # noqa: E402

_CHAT_ID = -1001234567890
_LINKED_CHANNEL_ID = -1009999999999
_FOREIGN_CHANNEL_ID = -1005555555555


def _msg(**kw):
    base = dict(
        message_id=100, text="реклама", caption=None,
        chat=SimpleNamespace(id=_CHAT_ID, type="supergroup", title="Чат"),
        sender_chat=None, from_user=None, is_automatic_forward=None,
        photo=None, video=None, sticker=None, animation=None, audio=None,
        voice=None, document=None, video_note=None, poll=None,
        location=None, contact=None, reply_to_message=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _channel(cid=_FOREIGN_CHANNEL_ID, title="Чужой канал", username="foreign"):
    return SimpleNamespace(id=cid, title=title, username=username, type="channel")


class TestUnconditionalGuards(unittest.TestCase):
    """Четыре пропуска, которые нельзя отменить ничем."""

    @staticmethod
    def _reason(message, linked=_LINKED_CHANNEL_ID):
        return bot_handlers._channel_guard_reason(message, linked_chat_id=linked)

    def test_message_without_sender_chat_is_not_a_channel_message(self):
        msg = _msg(from_user=SimpleNamespace(id=555, is_bot=False))
        self.assertIsNotNone(self._reason(msg))

    def test_group_itself_is_protected(self):
        """Анонимные админы пишут от имени самой группы."""
        msg = _msg(sender_chat=_channel(cid=_CHAT_ID, title="Чат"))
        self.assertIsNotNone(self._reason(msg))

    def test_linked_channel_is_protected(self):
        """Комментарии от имени канала-владельца чата обсуждений."""
        msg = _msg(sender_chat=_channel(cid=_LINKED_CHANNEL_ID, title="Свой"))
        self.assertIsNotNone(self._reason(msg))

    def test_automatic_forward_is_protected(self):
        """Автопост канала в чат обсуждений — вместе с ним ушла бы ветка."""
        msg = _msg(sender_chat=_channel(), is_automatic_forward=True)
        self.assertIsNotNone(self._reason(msg))

    def test_foreign_channel_is_a_candidate(self):
        msg = _msg(sender_chat=_channel())
        self.assertIsNone(self._reason(msg),
                          "чужой канал обязан доходить до проверки списка")

    def test_unknown_linked_chat_does_not_protect_everyone(self):
        """get_chat мог не ответить — это не повод пропускать всех подряд."""
        msg = _msg(sender_chat=_channel())
        self.assertIsNone(self._reason(msg, linked=None))

    def test_unknown_linked_chat_still_protects_the_group_itself(self):
        msg = _msg(sender_chat=_channel(cid=_CHAT_ID))
        self.assertIsNotNone(self._reason(msg, linked=None))


class TestWhitelistMatching(unittest.IsolatedAsyncioTestCase):
    """Матч по channel_id (он неизменен) и по @username как запасной путь."""

    async def asyncSetUp(self):
        await init_db()
        async with async_session() as session:
            await session.execute(ChannelWhitelist.__table__.delete())
            await session.commit()

    @staticmethod
    async def _add(**kw):
        async with async_session() as session:
            session.add(ChannelWhitelist(**kw))
            await session.commit()

    @staticmethod
    async def _check(sender_chat, chat_id=_CHAT_ID):
        async with async_session() as session:
            return await bot_handlers._is_channel_whitelisted(
                session, chat_id, sender_chat,
            )

    async def test_not_listed_is_false(self):
        self.assertFalse(await self._check(_channel()))

    async def test_match_by_channel_id_in_this_chat(self):
        await self._add(chat_id=_CHAT_ID, channel_id=_FOREIGN_CHANNEL_ID)
        self.assertTrue(await self._check(_channel()))

    async def test_entry_for_another_chat_does_not_match(self):
        await self._add(chat_id=-100777, channel_id=_FOREIGN_CHANNEL_ID)
        self.assertFalse(await self._check(_channel()))

    async def test_global_entry_matches_any_chat(self):
        await self._add(chat_id=0, channel_id=_FOREIGN_CHANNEL_ID)
        self.assertTrue(await self._check(_channel()))

    async def test_match_by_username_when_id_unknown(self):
        """Канал можно внести упреждающе, ещё не увидев его id."""
        await self._add(chat_id=_CHAT_ID, channel_username="foreign")
        self.assertTrue(await self._check(_channel()))

    async def test_username_match_backfills_channel_id(self):
        """id неизменен, username сменить можно — дописываем при встрече."""
        await self._add(chat_id=_CHAT_ID, channel_username="foreign")
        await self._check(_channel())
        async with async_session() as session:
            from sqlalchemy import select
            row = (await session.execute(
                select(ChannelWhitelist).where(
                    ChannelWhitelist.channel_username == "foreign")
            )).scalar_one()
            self.assertEqual(row.channel_id, _FOREIGN_CHANNEL_ID)

    async def test_username_match_is_case_insensitive(self):
        await self._add(chat_id=_CHAT_ID, channel_username="foreign")
        self.assertTrue(await self._check(_channel(username="FoReIgN")))

    async def test_channel_without_username_still_matches_by_id(self):
        await self._add(chat_id=_CHAT_ID, channel_id=_FOREIGN_CHANNEL_ID)
        self.assertTrue(await self._check(_channel(username=None)))


class TestToggleDefaultsOff(unittest.IsolatedAsyncioTestCase):
    """Фича не должна включиться сама: цена ошибки — массовое удаление."""

    async def asyncSetUp(self):
        await init_db()

    async def test_new_chat_has_filter_disabled(self):
        async with async_session() as session:
            settings, _created = await bot_handlers._ensure_chat_settings(
                session, chat_id=-100424242, title="Новый",
            )
            await session.commit()
            self.assertFalse(settings.delete_channel_messages)

    async def test_column_exists_on_model(self):
        self.assertTrue(hasattr(ChatSettings, "delete_channel_messages"))


def _make_event(sender_chat=None, **kw):
    event = MagicMock()
    event.message_id = kw.pop("message_id", 100)
    event.text = kw.pop("text", "реклама")
    event.caption = None
    event.chat = SimpleNamespace(id=_CHAT_ID, type="supergroup", title="Чат")
    event.sender_chat = sender_chat
    event.from_user = None
    event.is_automatic_forward = kw.pop("is_automatic_forward", None)
    event.reply_to_message = None
    for attr in ("photo", "video", "sticker", "animation", "audio", "voice",
                 "document", "video_note", "poll", "location", "contact"):
        setattr(event, attr, None)
    event.delete = AsyncMock()
    event.bot = MagicMock()
    for k, v in kw.items():
        setattr(event, k, v)
    return event


class TestChannelMessageMiddleware(unittest.IsolatedAsyncioTestCase):
    """Фильтр стоит middleware, а не хендлером — порядок хендлеров в файле
    на 9k строк уже дважды ронял прод (changelog v5.1.0)."""

    async def asyncSetUp(self):
        await init_db()
        async with async_session() as session:
            await session.execute(ChannelWhitelist.__table__.delete())
            settings, _ = await bot_handlers._ensure_chat_settings(
                session, chat_id=_CHAT_ID, title="Чат",
            )
            settings.delete_channel_messages = True
            await session.commit()

        bot_handlers._linked_chat_cache.clear()
        p = patch.object(
            bot_handlers, "_get_linked_chat_id",
            AsyncMock(return_value=_LINKED_CHANNEL_ID),
        )
        p.start()
        self.addCleanup(p.stop)

        self.notify = AsyncMock()
        p = patch.object(bot_handlers, "_notify_channel_message_deleted", self.notify)
        p.start()
        self.addCleanup(p.stop)

        self.mw = bot_handlers._ChannelMessageMiddleware()
        self.handler = AsyncMock(return_value="handled")

    async def test_foreign_channel_message_is_deleted(self):
        event = _make_event(sender_chat=_channel())
        result = await self.mw(self.handler, event, {})
        event.delete.assert_awaited_once()
        self.handler.assert_not_awaited()
        self.assertIsNone(result, "удалённое сообщение не идёт в обработку")

    async def test_deletion_notifies_modchat(self):
        event = _make_event(sender_chat=_channel())
        await self.mw(self.handler, event, {})
        self.notify.assert_awaited_once()

    async def test_whitelisted_channel_passes_through(self):
        async with async_session() as session:
            session.add(ChannelWhitelist(
                chat_id=_CHAT_ID, channel_id=_FOREIGN_CHANNEL_ID,
            ))
            await session.commit()
        event = _make_event(sender_chat=_channel())
        result = await self.mw(self.handler, event, {})
        event.delete.assert_not_awaited()
        self.assertEqual(result, "handled")

    async def test_ordinary_message_passes_through(self):
        event = _make_event(sender_chat=None)
        result = await self.mw(self.handler, event, {})
        event.delete.assert_not_awaited()
        self.assertEqual(result, "handled")

    async def test_failed_deletion_does_not_break_moderation(self):
        """Нет прав на удаление — чат не должен остаться без обработки."""
        event = _make_event(sender_chat=_channel())
        event.delete = AsyncMock(
            side_effect=TelegramAPIError(method=None, message="no rights"),
        )
        result = await self.mw(self.handler, event, {})
        self.assertEqual(result, "handled")

    async def test_private_chat_is_untouched(self):
        event = _make_event(sender_chat=_channel())
        event.chat = SimpleNamespace(id=555, type="private", title=None)
        result = await self.mw(self.handler, event, {})
        event.delete.assert_not_awaited()
        self.assertEqual(result, "handled")


class TestInvariantHoldsWithEmptyWhitelist(unittest.IsolatedAsyncioTestCase):
    """⚠️ Злой случай: тумблер ВКЛЮЧЁН, белый список ПУСТ.

    Именно так фича сломается, если кто-то потом будет «упрощать» условие
    до «нет в списке — удалить». Сообщения от самой группы и от связанного
    канала обязаны выжить без единой записи в списке.
    """

    async def asyncSetUp(self):
        await init_db()
        async with async_session() as session:
            await session.execute(ChannelWhitelist.__table__.delete())
            settings, _ = await bot_handlers._ensure_chat_settings(
                session, chat_id=_CHAT_ID, title="Чат",
            )
            settings.delete_channel_messages = True
            await session.commit()

        bot_handlers._linked_chat_cache.clear()
        p = patch.object(
            bot_handlers, "_get_linked_chat_id",
            AsyncMock(return_value=_LINKED_CHANNEL_ID),
        )
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(bot_handlers, "_notify_channel_message_deleted", AsyncMock())
        p.start()
        self.addCleanup(p.stop)

        self.mw = bot_handlers._ChannelMessageMiddleware()
        self.handler = AsyncMock(return_value="handled")

    async def test_whitelist_is_empty(self):
        from sqlalchemy import func, select
        async with async_session() as session:
            count = (await session.execute(
                select(func.count()).select_from(ChannelWhitelist)
            )).scalar_one()
        self.assertEqual(count, 0)

    async def test_anonymous_admin_of_this_group_survives(self):
        event = _make_event(sender_chat=_channel(cid=_CHAT_ID, title="Чат"))
        result = await self.mw(self.handler, event, {})
        event.delete.assert_not_awaited()
        self.assertEqual(result, "handled")

    async def test_linked_channel_survives(self):
        event = _make_event(
            sender_chat=_channel(cid=_LINKED_CHANNEL_ID, title="Свой канал"),
        )
        result = await self.mw(self.handler, event, {})
        event.delete.assert_not_awaited()
        self.assertEqual(result, "handled")

    async def test_automatic_forward_survives(self):
        event = _make_event(sender_chat=_channel(), is_automatic_forward=True)
        result = await self.mw(self.handler, event, {})
        event.delete.assert_not_awaited()
        self.assertEqual(result, "handled")

    async def test_foreign_channel_is_still_deleted(self):
        """Контроль: guard защищает своих, а не выключает фильтр целиком."""
        event = _make_event(sender_chat=_channel())
        await self.mw(self.handler, event, {})
        event.delete.assert_awaited_once()


class TestDisabledToggleStopsDeletion(unittest.IsolatedAsyncioTestCase):
    """Выключенный тумблер — чужие каналы тоже не трогаем."""

    async def asyncSetUp(self):
        await init_db()
        async with async_session() as session:
            settings, _ = await bot_handlers._ensure_chat_settings(
                session, chat_id=_CHAT_ID, title="Чат",
            )
            settings.delete_channel_messages = False
            await session.commit()
        bot_handlers._linked_chat_cache.clear()
        p = patch.object(
            bot_handlers, "_get_linked_chat_id", AsyncMock(return_value=None),
        )
        p.start()
        self.addCleanup(p.stop)
        self.mw = bot_handlers._ChannelMessageMiddleware()

    async def test_foreign_channel_survives_when_filter_is_off(self):
        handler = AsyncMock(return_value="handled")
        event = _make_event(sender_chat=_channel())
        result = await self.mw(handler, event, {})
        event.delete.assert_not_awaited()
        self.assertEqual(result, "handled")


class TestMiddlewareRegistration(unittest.TestCase):
    """Порядок outer-middleware: Disabled → Channel → ReplyContext."""

    def test_order(self):
        names = [type(m).__name__ for m in
                 bot_handlers.router.message.outer_middleware]
        for expected in ("_DisabledChatMiddleware", "_ChannelMessageMiddleware",
                         "_ReplyContextMiddleware"):
            self.assertIn(expected, names)
        self.assertLess(names.index("_DisabledChatMiddleware"),
                        names.index("_ChannelMessageMiddleware"))
        self.assertLess(
            names.index("_ChannelMessageMiddleware"),
            names.index("_ReplyContextMiddleware"),
            "контекст реплаев не пишется для сообщения, которое удаляется",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestCommandRegistry(unittest.TestCase):
    """Команды живут в реестре commands.py — иначе разойдутся со справкой."""

    @staticmethod
    def _resolve(text):
        import commands
        return commands.resolve(text, None)

    def test_channelallow_by_reply_has_no_arguments(self):
        spec, m = self._resolve("/channelallow")
        self.assertEqual(spec.name, "channelallow")
        self.assertIsNone(m.group("target"))

    def test_channelallow_accepts_username(self):
        spec, m = self._resolve("/channelallow @spamnews")
        self.assertEqual(spec.name, "channelallow")
        self.assertEqual(m.group("target"), "@spamnews")

    def test_channelallow_accepts_negative_channel_id(self):
        """ID каналов отрицательные — «-?\\d+», а не «\\d+»."""
        spec, m = self._resolve("/channelallow -1005555555555")
        self.assertEqual(spec.name, "channelallow")
        self.assertEqual(m.group("target"), "-1005555555555")

    def test_channelunallow_resolves(self):
        spec, _m = self._resolve("/channelunallow @spamnews")
        self.assertEqual(spec.name, "channelunallow")

    def test_channellist_resolves(self):
        spec, _m = self._resolve("/channellist")
        self.assertEqual(spec.name, "channellist")

    def test_commands_are_marked_as_not_targeting_users(self):
        """Цель этих команд — канал, а не участник: резолв через
        _resolve_punishment_target для них не должен запускаться вовсе."""
        import commands
        self.assertEqual(
            set(commands.NO_USER_TARGET),
            {"channelallow", "channelunallow", "channellist"},
        )


def _mod_message(text, *, sender_chat=None, reply_sender_chat=None):
    msg = MagicMock()
    msg.text = text
    msg.caption = None
    msg.entities = None
    msg.caption_entities = None
    msg.chat = SimpleNamespace(id=_CHAT_ID, type="supergroup", title="Чат")
    msg.from_user = SimpleNamespace(
        id=111, username="moder", first_name="Модер", last_name="", is_bot=False,
    )
    msg.sender_chat = sender_chat
    msg.message_id = 500
    msg.delete = AsyncMock()
    msg.bot = MagicMock()
    msg.bot.send_message = AsyncMock()
    if reply_sender_chat is not None:
        msg.reply_to_message = SimpleNamespace(
            sender_chat=reply_sender_chat, from_user=None,
            message_id=400, text="реклама", caption=None,
            photo=None, video=None, sticker=None, animation=None, audio=None,
            voice=None, document=None, video_note=None, poll=None,
            location=None, contact=None,
        )
    else:
        msg.reply_to_message = None
    return msg


class TestGroupCommands(unittest.IsolatedAsyncioTestCase):
    """Реплаем на сообщение канала — главный сценарий: id и название
    берутся из sender_chat, руками их добывать негде."""

    async def asyncSetUp(self):
        await init_db()
        async with async_session() as session:
            await session.execute(ChannelWhitelist.__table__.delete())
            await session.commit()
        for name, mock in (
            ("_is_admin", AsyncMock(return_value=True)),
            ("_schedule_ephemeral_delete", AsyncMock()),
        ):
            p = patch.object(bot_handlers, name, mock)
            p.start()
            self.addCleanup(p.stop)

    @staticmethod
    async def _rows():
        from sqlalchemy import select
        async with async_session() as session:
            return (await session.execute(select(ChannelWhitelist))).scalars().all()

    async def test_reply_adds_channel_with_id_and_title(self):
        msg = _mod_message("/channelallow", reply_sender_chat=_channel())
        await bot_handlers.handle_group_command(msg)
        rows = await self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].channel_id, _FOREIGN_CHANNEL_ID)
        self.assertEqual(rows[0].title, "Чужой канал")
        self.assertEqual(rows[0].chat_id, _CHAT_ID,
                         "команда в группе вносит канал в ЭТОТ чат")

    async def test_username_form_adds_channel_without_id(self):
        msg = _mod_message("/channelallow @spamnews")
        await bot_handlers.handle_group_command(msg)
        rows = await self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].channel_username, "spamnews")
        self.assertIsNone(rows[0].channel_id)

    async def test_adding_twice_does_not_duplicate(self):
        for _ in range(2):
            msg = _mod_message("/channelallow", reply_sender_chat=_channel())
            await bot_handlers.handle_group_command(msg)
        self.assertEqual(len(await self._rows()), 1)

    async def test_unallow_by_reply_removes_entry(self):
        await bot_handlers.handle_group_command(
            _mod_message("/channelallow", reply_sender_chat=_channel()))
        await bot_handlers.handle_group_command(
            _mod_message("/channelunallow", reply_sender_chat=_channel()))
        self.assertEqual(await self._rows(), [])

    async def test_channellist_does_not_require_a_target(self):
        """Регрессия: диспетчер не должен требовать цель-участника."""
        msg = _mod_message("/channellist")
        await bot_handlers.handle_group_command(msg)
        text = msg.bot.send_message.await_args.kwargs["text"]
        self.assertNotIn("Не указана цель", text)

    async def test_channelallow_without_target_and_reply_explains_itself(self):
        msg = _mod_message("/channelallow")
        await bot_handlers.handle_group_command(msg)
        text = msg.bot.send_message.await_args.kwargs["text"]
        self.assertIn("канал", text.lower())
        self.assertEqual(await self._rows(), [])


def _dm_message(text, from_user_id=111):
    msg = MagicMock()
    msg.text = text
    msg.reply = AsyncMock()
    msg.chat = SimpleNamespace(type="private", id=from_user_id)
    msg.from_user = SimpleNamespace(id=from_user_id, username="admin",
                                    first_name="Админ", last_name="")
    return msg


class TestDmCommands(unittest.IsolatedAsyncioTestCase):
    """Личка — калька с /botallow: область указывается явно, доступ по
    ADMIN_IDS. Нужна для упреждающего внесения и глобальной области,
    которой у групповой формы нет."""

    async def asyncSetUp(self):
        await init_db()
        async with async_session() as session:
            await session.execute(ChannelWhitelist.__table__.delete())
            await session.commit()

    @staticmethod
    async def _rows():
        from sqlalchemy import select
        async with async_session() as session:
            return (await session.execute(select(ChannelWhitelist))).scalars().all()

    async def test_global_scope_adds_entry(self):
        msg = _dm_message("/channelallow global -1005555555555")
        await bot_handlers.cmd_channelallow_dm(msg)
        rows = await self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].chat_id, 0)
        self.assertEqual(rows[0].channel_id, _FOREIGN_CHANNEL_ID)

    async def test_explicit_chat_scope(self):
        msg = _dm_message(f"/channelallow {_CHAT_ID} @spamnews")
        await bot_handlers.cmd_channelallow_dm(msg)
        rows = await self._rows()
        self.assertEqual(rows[0].chat_id, _CHAT_ID)
        self.assertEqual(rows[0].channel_username, "spamnews")

    async def test_non_admin_is_ignored(self):
        msg = _dm_message("/channelallow global -1005555555555", from_user_id=222)
        await bot_handlers.cmd_channelallow_dm(msg)
        self.assertEqual(await self._rows(), [])

    async def test_bad_scope_is_rejected(self):
        msg = _dm_message("/channelallow мусор -1005555555555")
        await bot_handlers.cmd_channelallow_dm(msg)
        self.assertEqual(await self._rows(), [])
        msg.reply.assert_awaited()

    async def test_missing_arguments_shows_format(self):
        msg = _dm_message("/channelallow")
        await bot_handlers.cmd_channelallow_dm(msg)
        self.assertEqual(await self._rows(), [])
        self.assertIn("/channelallow", msg.reply.await_args.args[0])

    async def test_unallow_removes_entry(self):
        await bot_handlers.cmd_channelallow_dm(
            _dm_message("/channelallow global -1005555555555"))
        await bot_handlers.cmd_channelunallow_dm(
            _dm_message("/channelunallow global -1005555555555"))
        self.assertEqual(await self._rows(), [])

    async def test_list_reports_entries(self):
        await bot_handlers.cmd_channelallow_dm(
            _dm_message("/channelallow global -1005555555555"))
        msg = _dm_message("/channelallowlist")
        await bot_handlers.cmd_channelallowlist_dm(msg)
        self.assertIn("1005555555555", msg.reply.await_args.args[0])

    async def test_list_on_empty_whitelist_does_not_crash(self):
        msg = _dm_message("/channelallowlist")
        await bot_handlers.cmd_channelallowlist_dm(msg)
        msg.reply.assert_awaited()


class TestEndToEndThroughDispatcher(unittest.IsolatedAsyncioTestCase):
    """Настоящий aiogram-Dispatcher: middleware обязан отработать в реальной
    цепочке, а не только при прямом вызове.

    Тот же пробел, который в v5.2.0 пришлось закрывать задним числом:
    прямой вызов middleware не доказывает, что aiogram вообще его позовёт.
    """

    _dp = None
    _bot = None

    async def asyncSetUp(self):
        await init_db()
        async with async_session() as session:
            await session.execute(ChannelWhitelist.__table__.delete())
            settings, _ = await bot_handlers._ensure_chat_settings(
                session, chat_id=_CHAT_ID, title="Чат",
            )
            settings.delete_channel_messages = True
            settings.is_enabled = True
            await session.commit()

        bot_handlers._linked_chat_cache.clear()
        for name, mock in (
            ("_get_linked_chat_id", AsyncMock(return_value=_LINKED_CHANNEL_ID)),
            ("_notify_channel_message_deleted", AsyncMock()),
        ):
            p = patch.object(bot_handlers, name, mock)
            p.start()
            self.addCleanup(p.stop)

    @classmethod
    def _dispatcher(cls):
        if cls._dp is None:
            from aiogram import Bot, Dispatcher
            cls._bot = Bot(token="1234567890:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
            cls._dp = Dispatcher()
            cls._dp.include_router(bot_handlers.router)
        return cls._dp, cls._bot

    async def _feed(self, message):
        from aiogram.types import Update
        dp, bot = self._dispatcher()
        await dp.feed_update(bot=bot, update=Update(update_id=1, message=message))

    @staticmethod
    def _real_message(sender_chat_id, title, **kw):
        from aiogram.types import Chat, Message
        return Message(
            message_id=kw.pop("message_id", 900),
            date=1699999999,
            chat=Chat(id=_CHAT_ID, type="supergroup", title="Чат"),
            sender_chat=Chat(id=sender_chat_id, type="channel", title=title),
            text=kw.pop("text", "реклама"),
            **kw,
        )

    async def test_foreign_channel_message_is_deleted_in_real_chain(self):
        msg = self._real_message(_FOREIGN_CHANNEL_ID, "Чужой канал")
        with patch.object(type(msg), "delete", AsyncMock()) as delete:
            await self._feed(msg)
        delete.assert_awaited_once()

    async def test_linked_channel_survives_in_real_chain(self):
        msg = self._real_message(_LINKED_CHANNEL_ID, "Свой канал")
        with patch.object(type(msg), "delete", AsyncMock()) as delete:
            await self._feed(msg)
        delete.assert_not_awaited()

    async def test_anonymous_admin_survives_in_real_chain(self):
        msg = self._real_message(_CHAT_ID, "Чат")
        with patch.object(type(msg), "delete", AsyncMock()) as delete:
            await self._feed(msg)
        delete.assert_not_awaited()


class TestDeletionSurvivesFloodControl(unittest.IsolatedAsyncioTestCase):
    """Спамящий канал шлёт пачками — удаления упираются в flood control.

    Без ретрая первый же 429 оставлял бы сообщение в чате: except
    TelegramAPIError ловит и TelegramRetryAfter тоже, то есть отказ был бы
    молчаливым и выглядел как «фильтр не работает».
    """

    async def asyncSetUp(self):
        await init_db()
        async with async_session() as session:
            await session.execute(ChannelWhitelist.__table__.delete())
            settings, _ = await bot_handlers._ensure_chat_settings(
                session, chat_id=_CHAT_ID, title="Чат",
            )
            settings.delete_channel_messages = True
            await session.commit()
        bot_handlers._linked_chat_cache.clear()
        for name, mock in (
            ("_get_linked_chat_id", AsyncMock(return_value=_LINKED_CHANNEL_ID)),
            ("_notify_channel_message_deleted", AsyncMock()),
        ):
            p = patch.object(bot_handlers, name, mock)
            p.start()
            self.addCleanup(p.stop)
        self.mw = bot_handlers._ChannelMessageMiddleware()

    async def test_retry_after_is_retried_and_message_is_deleted(self):
        from aiogram.exceptions import TelegramRetryAfter
        calls = []

        async def _delete():
            calls.append(1)
            if len(calls) == 1:
                raise TelegramRetryAfter(
                    method=None, message="flood", retry_after=0,
                )
            return True

        event = _make_event(sender_chat=_channel())
        event.delete = _delete
        handler = AsyncMock(return_value="handled")
        result = await self.mw(handler, event, {})

        self.assertEqual(len(calls), 2, "429 обязан быть отретраен")
        self.assertIsNone(result, "после удаления сообщение не идёт в обработку")
