"""v5.2.0 — контекст «в ответ на» в отчётах репорт-чата.

Отчёт о наказании показывал сообщение нарушителя, но не то, на что
нарушитель отвечал. Модератор видел «пошёл ты» без адресата и без повода —
в отличие от скриншота, где вся ветка видна.

Починить это простым чтением нельзя: Bot API не вкладывает reply второго
уровня. Когда модератор отвечает «/ban» на сообщение нарушителя, бот
получает сообщение нарушителя с ПУСТЫМ reply_to_message, даже если оно
само было ответом. Поэтому родитель снимается в момент прихода сообщения
(outer-middleware) и кладётся в таблицу reply_contexts, а отчёт достаёт
его оттуда.

Проверяет:
  • _reply_snapshot_from_message — снимок родителя: автор, пост канала,
    обрезка текста, медиа, описание вместо пустого текста.
  • Round-trip через БД: _store_reply_context → _resolve_reply_context.
  • Приоритет живого reply над БД и отсутствие падений на None.
  • _purge_reply_contexts — TTL.
  • _ReplyContextMiddleware — пишет реплаи из групп, не трогает остальное
    и не роняет обработку при сбое БД.
  • _send_report — блок «↩️ В ответ на» с автором, цитатой и ссылкой.
  • Все вызовы _send_report прокидывают reply_context — иначе фича есть,
    но в отчёт не попадает.

Запуск: uv run python tools/run_tests.py -k v520_reply_context
"""
from _paths import _P  # noqa: E402
import ast
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("BOT_TOKEN", "test:test")
os.environ["ADMIN_IDS"] = "111"
os.environ["WEB_ALLOW_NO_SECRET"] = "1"
os.environ["DB_PATH"] = "/tmp/degramod_v520_replyctx.db"

sys.path.insert(0, _P())

import bot_handlers  # noqa: E402
from db import ReplyContext, async_session, init_db  # noqa: E402

_CHAT_ID = -1001234567890


def _msg(**kw):
    """Заглушка types.Message с полями, которые читает снимок."""
    base = dict(
        message_id=100, text=None, caption=None, from_user=None,
        sender_chat=None, photo=None, video=None, animation=None,
        audio=None, voice=None, sticker=None, document=None,
        video_note=None, poll=None, location=None, contact=None,
        reply_to_message=None,
        chat=SimpleNamespace(id=_CHAT_ID, type="supergroup"),
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _user(uid=555, username="vasya", first="Вася", last="Пупкин"):
    return SimpleNamespace(
        id=uid, is_bot=False, username=username,
        first_name=first, last_name=last,
    )


class TestReplySnapshot(unittest.TestCase):
    """Снимок родительского сообщения — чистая функция, без БД и Telegram."""

    def test_captures_author_and_text(self):
        parent = _msg(message_id=42, text="кто последний?", from_user=_user())
        snap = bot_handlers._reply_snapshot_from_message(parent)
        self.assertEqual(snap.parent_message_id, 42)
        self.assertEqual(snap.parent_user_id, 555)
        self.assertEqual(snap.parent_username, "vasya")
        self.assertEqual(snap.parent_first_name, "Вася")
        self.assertEqual(snap.parent_text, "кто последний?")

    def test_caption_is_used_when_there_is_no_text(self):
        parent = _msg(caption="подпись к фото", from_user=_user(),
                      photo=[SimpleNamespace(file_id="ph1")])
        snap = bot_handlers._reply_snapshot_from_message(parent)
        self.assertEqual(snap.parent_text, "подпись к фото")

    def test_long_text_is_truncated(self):
        parent = _msg(text="я" * 5000, from_user=_user())
        snap = bot_handlers._reply_snapshot_from_message(parent)
        self.assertLessEqual(
            len(snap.parent_text), bot_handlers._REPLY_CONTEXT_TEXT_LIMIT + 1,
        )

    def test_media_without_text_falls_back_to_description(self):
        """Пустой блок в отчёте бесполезен — кладём «🖼 [Фото]»."""
        parent = _msg(from_user=_user(), photo=[SimpleNamespace(file_id="ph1")])
        snap = bot_handlers._reply_snapshot_from_message(parent)
        self.assertEqual(snap.parent_text, "🖼 [Фото]")

    def test_photo_media_ref_is_captured(self):
        parent = _msg(from_user=_user(), photo=[
            SimpleNamespace(file_id="small"), SimpleNamespace(file_id="big"),
        ])
        snap = bot_handlers._reply_snapshot_from_message(parent)
        self.assertEqual(snap.parent_media_type, "photo")
        self.assertEqual(snap.parent_file_id, "big",
                         "берётся самый большой PhotoSize")

    def test_video_media_ref_is_captured(self):
        parent = _msg(from_user=_user(),
                      video=SimpleNamespace(file_id="vid1"))
        snap = bot_handlers._reply_snapshot_from_message(parent)
        self.assertEqual(snap.parent_media_type, "video")
        self.assertEqual(snap.parent_file_id, "vid1")

    def test_channel_post_is_captured_as_sender_chat(self):
        """Пост канала в чате обсуждений: автор — канал, не пользователь."""
        parent = _msg(
            message_id=7, text="новый пост",
            from_user=_user(uid=777, username="GroupAnonymousBot"),
            sender_chat=SimpleNamespace(id=-1009999, title="Мой Канал",
                                        username="mychannel"),
        )
        snap = bot_handlers._reply_snapshot_from_message(parent)
        self.assertEqual(snap.parent_sender_chat_id, -1009999)
        self.assertEqual(snap.parent_sender_chat_title, "Мой Канал")
        self.assertTrue(snap.is_channel_post)

    def test_user_message_is_not_a_channel_post(self):
        parent = _msg(text="привет", from_user=_user())
        self.assertFalse(
            bot_handlers._reply_snapshot_from_message(parent).is_channel_post,
        )

    def test_display_name_prefers_channel_title(self):
        parent = _msg(text="пост", from_user=_user(),
                      sender_chat=SimpleNamespace(id=-1, title="Канал",
                                                  username=None))
        snap = bot_handlers._reply_snapshot_from_message(parent)
        self.assertEqual(snap.display_name, "Канал")

    def test_display_name_joins_first_and_last(self):
        parent = _msg(text="привет", from_user=_user())
        snap = bot_handlers._reply_snapshot_from_message(parent)
        self.assertEqual(snap.display_name, "Вася Пупкин")


class TestMessageLink(unittest.TestCase):
    """Ссылка на исходное сообщение — чтобы модератор мог открыть ветку."""

    def test_supergroup_link_strips_minus_100(self):
        link = bot_handlers._message_link(-1001234567890, 42)
        self.assertEqual(link, "https://t.me/c/1234567890/42")

    def test_plain_group_has_no_link(self):
        """У обычных групп (id без -100) публичных ссылок не существует."""
        self.assertIsNone(bot_handlers._message_link(-4001234, 42))


class TestReplyContextStorage(unittest.IsolatedAsyncioTestCase):
    """Round-trip через SQLite: пишем снимок, читаем его в отчёте."""

    async def asyncSetUp(self):
        await init_db()
        async with async_session() as session:
            await session.execute(ReplyContext.__table__.delete())
            await session.commit()

    async def test_store_then_resolve_roundtrip(self):
        parent = _msg(message_id=42, text="исходное", from_user=_user())
        offender = _msg(message_id=100, text="ответ", from_user=_user(uid=666))
        await bot_handlers._store_reply_context(
            _CHAT_ID, offender.message_id,
            bot_handlers._reply_snapshot_from_message(parent),
        )

        # У пришедшего из мод-команды сообщения reply_to_message пустой —
        # именно эту дыру и закрывает таблица.
        got = await bot_handlers._resolve_reply_context(offender)
        self.assertIsNotNone(got)
        self.assertEqual(got.parent_message_id, 42)
        self.assertEqual(got.parent_text, "исходное")
        self.assertEqual(got.parent_user_id, 555)

    async def test_storing_same_message_twice_overwrites(self):
        """Повторный апдейт того же message_id не должен падать на PK."""
        offender = _msg(message_id=101)
        for text in ("первый", "второй"):
            await bot_handlers._store_reply_context(
                _CHAT_ID, 101,
                bot_handlers._reply_snapshot_from_message(
                    _msg(message_id=42, text=text, from_user=_user()),
                ),
            )
        got = await bot_handlers._resolve_reply_context(offender)
        self.assertEqual(got.parent_text, "второй")

    async def test_unknown_message_resolves_to_none(self):
        self.assertIsNone(
            await bot_handlers._resolve_reply_context(_msg(message_id=999999)),
        )

    async def test_none_message_resolves_to_none(self):
        """Наказание по @username: сообщения нарушителя вообще нет."""
        self.assertIsNone(await bot_handlers._resolve_reply_context(None))

    async def test_live_reply_wins_over_database(self):
        """Автоматические наказания видят родителя живьём — БД не нужна."""
        await bot_handlers._store_reply_context(
            _CHAT_ID, 102,
            bot_handlers._reply_snapshot_from_message(
                _msg(message_id=1, text="устаревшее", from_user=_user()),
            ),
        )
        live_parent = _msg(message_id=2, text="живое", from_user=_user())
        offender = _msg(message_id=102, reply_to_message=live_parent)
        got = await bot_handlers._resolve_reply_context(offender)
        self.assertEqual(got.parent_text, "живое")

    async def test_purge_drops_stale_rows_and_keeps_fresh(self):
        await bot_handlers._store_reply_context(
            _CHAT_ID, 200,
            bot_handlers._reply_snapshot_from_message(
                _msg(message_id=1, text="старое", from_user=_user()),
            ),
        )
        await bot_handlers._store_reply_context(
            _CHAT_ID, 201,
            bot_handlers._reply_snapshot_from_message(
                _msg(message_id=2, text="свежее", from_user=_user()),
            ),
        )
        stale = datetime.now(timezone.utc) - timedelta(
            days=bot_handlers._REPLY_CONTEXT_TTL_DAYS + 1,
        )
        async with async_session() as session:
            row = await session.get(ReplyContext, (_CHAT_ID, 200))
            row.created_at = stale
            await session.commit()

        deleted = await bot_handlers._purge_reply_contexts()
        self.assertEqual(deleted, 1)
        self.assertIsNone(await bot_handlers._resolve_reply_context(_msg(message_id=200)))
        self.assertIsNotNone(await bot_handlers._resolve_reply_context(_msg(message_id=201)))


class TestReplyContextMiddleware(unittest.IsolatedAsyncioTestCase):
    """Захват на входе. Middleware обязан быть прозрачным для обработки."""

    async def asyncSetUp(self):
        await init_db()
        self.mw = bot_handlers._ReplyContextMiddleware()
        self.handler = AsyncMock(return_value="handled")

    async def test_group_reply_is_stored_and_passed_through(self):
        parent = _msg(message_id=42, text="исходное", from_user=_user())
        event = _msg(message_id=300, text="ответ",
                     from_user=_user(uid=666), reply_to_message=parent)
        with patch.object(bot_handlers, "_store_reply_context",
                          AsyncMock()) as store:
            result = await self.mw(self.handler, event, {})
        store.assert_awaited_once()
        self.assertEqual(store.await_args.args[:2], (_CHAT_ID, 300))
        self.assertEqual(result, "handled")
        self.handler.assert_awaited_once()

    async def test_message_without_reply_is_not_stored(self):
        event = _msg(message_id=301, text="просто сообщение", from_user=_user())
        with patch.object(bot_handlers, "_store_reply_context",
                          AsyncMock()) as store:
            await self.mw(self.handler, event, {})
        store.assert_not_awaited()
        self.handler.assert_awaited_once()

    async def test_private_chat_is_not_stored(self):
        parent = _msg(message_id=1, text="p", from_user=_user())
        event = _msg(message_id=302, text="ответ в личке",
                     from_user=_user(), reply_to_message=parent,
                     chat=SimpleNamespace(id=555, type="private"))
        with patch.object(bot_handlers, "_store_reply_context",
                          AsyncMock()) as store:
            await self.mw(self.handler, event, {})
        store.assert_not_awaited()
        self.handler.assert_awaited_once()

    async def test_storage_failure_does_not_block_moderation(self):
        """Сбой записи контекста не должен стоить чату модерации."""
        parent = _msg(message_id=42, text="исходное", from_user=_user())
        event = _msg(message_id=303, text="ответ",
                     from_user=_user(), reply_to_message=parent)
        with patch.object(bot_handlers, "_store_reply_context",
                          AsyncMock(side_effect=RuntimeError("db down"))):
            result = await self.mw(self.handler, event, {})
        self.assertEqual(result, "handled")
        self.handler.assert_awaited_once()

    async def test_registered_as_outer_middleware(self):
        registered = [type(m).__name__ for m in
                      bot_handlers.router.message.outer_middleware]
        self.assertIn("_ReplyContextMiddleware", registered)
        self.assertLess(
            registered.index("_DisabledChatMiddleware"),
            registered.index("_ReplyContextMiddleware"),
            "контекст выключенного чата писать не надо — "
            "_DisabledChatMiddleware обязан идти первым",
        )


def _rich_texts(obj, acc=None):
    """Собирает все строки из дерева rich-блоков — для проверки рендера."""
    acc = [] if acc is None else acc
    if isinstance(obj, str):
        acc.append(obj)
        return acc
    if isinstance(obj, (list, tuple)):
        for item in obj:
            _rich_texts(item, acc)
        return acc
    for attr in ("blocks", "items", "text", "summary"):
        if hasattr(obj, attr):
            _rich_texts(getattr(obj, attr), acc)
    return acc


class TestReportRendersReplyContext(unittest.IsolatedAsyncioTestCase):
    """Отчёт в репорт-чат: новый свёрнутый блок «↩️ В ответ на»."""

    async def asyncSetUp(self):
        await init_db()
        self.bot = MagicMock()
        self.bot.send_rich_message = AsyncMock()
        p = patch.object(
            bot_handlers, "_get_report_chat_id", AsyncMock(return_value=-100777),
        )
        p.start()
        self.addCleanup(p.stop)

    async def _send(self, reply_context):
        await bot_handlers._send_report(
            bot=self.bot, chat_id=_CHAT_ID,
            target=_user(uid=666, username="naru", first="Нару", last=""),
            action_type="ban", reason="спам",
            mod=_user(uid=111, username="mod", first="Мод", last=""),
            reply_context=reply_context,
        )
        self.bot.send_rich_message.assert_awaited_once()
        rich = self.bot.send_rich_message.await_args.kwargs["rich_message"]
        return _rich_texts(rich.blocks)

    async def test_block_is_absent_without_context(self):
        texts = await self._send(None)
        self.assertNotIn("↩️ В ответ на", texts)

    async def test_block_shows_author_text_and_link(self):
        snap = bot_handlers._reply_snapshot_from_message(
            _msg(message_id=42, text="кто последний?", from_user=_user()),
        )
        texts = await self._send(snap)
        self.assertIn("↩️ В ответ на", texts)
        self.assertIn("кто последний?", texts)
        self.assertIn("Вася Пупкин", texts)
        self.assertTrue(any("@vasya" in t for t in texts))
        self.assertTrue(any("Открыть сообщение" in t for t in texts))

    async def test_block_labels_channel_post(self):
        snap = bot_handlers._reply_snapshot_from_message(
            _msg(message_id=7, text="новый пост", from_user=_user(),
                 sender_chat=SimpleNamespace(id=-1009999, title="Мой Канал",
                                             username="mychannel")),
        )
        texts = await self._send(snap)
        self.assertTrue(any("Мой Канал" in t for t in texts))
        self.assertTrue(any("📢" in t for t in texts),
                        "пост канала должен быть помечен, а не выглядеть юзером")
        self.assertFalse(any("@vasya" in t for t in texts),
                         "GroupAnonymousBot не должен выдаваться за автора поста")

    async def test_plain_fallback_mentions_reply_context(self):
        """Rich Message может не уйти — контекст обязан быть и в fallback'е."""
        from aiogram.exceptions import TelegramAPIError
        self.bot.send_rich_message = AsyncMock(
            side_effect=TelegramAPIError(method=None, message="nope"),
        )
        self.bot.send_message = AsyncMock()
        snap = bot_handlers._reply_snapshot_from_message(
            _msg(message_id=42, text="кто последний?", from_user=_user()),
        )
        await bot_handlers._send_report(
            bot=self.bot, chat_id=_CHAT_ID, target=_user(uid=666),
            action_type="ban", reason="спам", reply_context=snap,
        )
        sent = self.bot.send_message.await_args.kwargs["text"]
        self.assertIn("В ответ на", sent)
        self.assertIn("кто последний?", sent)


class TestPurgeLoopWiring(unittest.TestCase):
    """Фоновый цикл обязан быть и запущен, и отменяем при shutdown.

    В bot.py TaskGroup ждёт завершения всех задач, а `while True` сам не
    выйдет: цикл, забытый в списке отмены, подвешивает shutdown ровно до
    hard-timeout'а. Этот же тест уже был бы полезен, когда добавляли
    health_probe_loop — там про это написан отдельный комментарий.
    """

    def setUp(self):
        with open(_P("bot.py")) as f:
            self.src = f.read()

    def test_loop_is_started_in_taskgroup(self):
        self.assertIn("_reply_context_purge_loop()", self.src)
        self.assertIn('name="reply_context_purge_loop"', self.src)

    def test_loop_is_cancelled_on_shutdown(self):
        idx = self.src.find("bg_tasks = [")
        self.assertGreater(idx, 0, "список отменяемых задач не найден")
        line = self.src[idx:self.src.find("]", idx)]
        self.assertIn("purge_task", line,
                      "purge_task не в bg_tasks — shutdown будет висеть "
                      "до hard-timeout")


class TestAllReportCallsPassContext(unittest.TestCase):
    """Фича бесполезна, если хоть один вызов _send_report её не прокинул."""

    @staticmethod
    def _calls(path):
        tree = ast.parse(open(_P(path)).read())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if name == "_send_report":
                yield node

    def _assert_all_pass_context(self, path):
        missing = [
            node.lineno for node in self._calls(path)
            if not any(kw.arg == "reply_context" for kw in node.keywords)
        ]
        self.assertEqual(
            missing, [],
            f"{path}: вызовы _send_report без reply_context в строках {missing}",
        )

    def test_mod_commands_pass_context(self):
        self._assert_all_pass_context("mod_commands.py")

    def test_bot_handlers_pass_context(self):
        self._assert_all_pass_context("bot_handlers.py")


if __name__ == "__main__":
    unittest.main(verbosity=2)
