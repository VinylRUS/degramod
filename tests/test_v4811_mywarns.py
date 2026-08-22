"""
test_v4811_mywarns.py — тесты команды !mywarns (v4.8.10) и её починок (v4.8.11).

Команда позволяет любому участнику чата посмотреть свои активные варны:
  • в группе — бот удаляет команду и отвечает ephemeral-сообщением в тот же
    чат: оно видно ТОЛЬКО автору команды (v4.9.0);
  • в личке — отвечает сводкой по всем чатам.

v4.9.0: в группе ответ больше не уходит в личку. Личка требовала, чтобы юзер
запустил бота, а /start открыт только модераторам — то есть обычный участник,
ради которого фича и делалась, не получал ничего. Ephemeral доставляется без
/start и при этом не показывает варны остальному чату.

Тесты закрывают то, что при добавлении команды осталось непокрытым:

  T1. Базовое поведение: сводка по чату, пустой случай, формат даты.
  T2. Приватность: команда удаляется из чата ВСЕГДА, включая срабатывание
      таймаута. Иначе смысл фичи теряется — чат видит, кто проверяет варны.
  T3. Таймаут: повторный вызов в окне 5 минут не шлёт второе сообщение.
  T4. Реестр таймаутов не растёт бесконечно — протухшие записи удаляются.
  T5. Отправка идёт через tg_safe_call: при 429 от Telegram сообщение
      доходит после ретрая, а не теряется.
  T6. Месяц в дате — по-русски независимо от локали окружения.
  T7. Адресация ephemeral: ответ уходит в чат с receiver_user_id автора,
      а не в личку и не публично.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from _paths import _P

sys.path.insert(0, _P())

os.environ.setdefault("BOT_TOKEN", "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw")
os.environ.setdefault("ADMIN_IDS", "111111111")

from aiogram.exceptions import TelegramRetryAfter  # noqa: E402
from sqlalchemy import delete  # noqa: E402

import bot_handlers  # noqa: E402
from db import (  # noqa: E402
    ChatSettings, Moderator, Punishment, User, async_session, init_db,
)

_CHAT_ID = -1001234567890
_OTHER_CHAT_ID = -1009876543210
_USER_ID = 555000111


_MOD_ID = 999


async def _reset_db() -> None:
    await init_db()
    async with async_session() as s:
        await s.execute(delete(Punishment))
        await s.execute(delete(ChatSettings))
        await s.execute(delete(User))
        await s.execute(delete(Moderator))
        await s.commit()
    # punishments.user_id / mod_id — внешние ключи, а foreign_keys=ON.
    async with async_session() as s:
        s.add(User(user_id=_USER_ID, username="tester", first_name="Тест"))
        s.add(Moderator(mod_id=_MOD_ID, username="mod", first_name="Мод"))
        await s.commit()


async def _add_warn(chat_id: int = _CHAT_ID, *, created_at: datetime | None = None) -> None:
    """Добавляет активный варн юзеру _USER_ID.

    duration_seconds для варна — это его «вес» в поинтах: _count_warns
    суммирует поле, а не считает строки. Без него варн весит ноль.
    """
    async with async_session() as s:
        s.add(Punishment(
            user_id=_USER_ID,
            mod_id=_MOD_ID,
            chat_id=chat_id,
            action_type="warn",
            duration_seconds=1,
            reason="тест",
            created_at=created_at or datetime.now(timezone.utc),
        ))
        await s.commit()


async def _add_chat(chat_id: int = _CHAT_ID, title: str = "Тестовый чат") -> None:
    async with async_session() as s:
        s.add(ChatSettings(chat_id=chat_id, title=title))
        await s.commit()


def _make_group_message() -> MagicMock:
    """Сообщение `!mywarns`, отправленное в группу."""
    msg = MagicMock()
    msg.text = "!mywarns"
    msg.chat = MagicMock()
    msg.chat.id = _CHAT_ID
    msg.chat.type = "supergroup"
    msg.from_user = MagicMock()
    msg.from_user.id = _USER_ID
    msg.from_user.is_bot = False
    msg.delete = AsyncMock()
    msg.reply = AsyncMock()
    msg.bot = AsyncMock()
    return msg


def _make_dm_message() -> MagicMock:
    msg = _make_group_message()
    msg.chat.id = _USER_ID
    msg.chat.type = "private"
    return msg


class _MywarnsTestCase(unittest.IsolatedAsyncioTestCase):
    """Общий сброс состояния: БД и реестр таймаутов.

    Планировщик авто-удаления ephemeral подменяется заглушкой: настоящий
    спавнит фоновую таску со sleep(30), которая переживёт тест и будет
    убита при закрытии event loop. Само авто-удаление проверяется в
    tests/test_v456_ephemeral_autodelete.py — здесь оно не предмет теста.
    """

    async def asyncSetUp(self):
        await _reset_db()
        bot_handlers._mywarns_last_call.clear()
        patcher = patch.object(
            bot_handlers, "_schedule_ephemeral_delete", AsyncMock(),
        )
        self._sched_mock = patcher.start()
        self.addCleanup(patcher.stop)

    async def asyncTearDown(self):
        bot_handlers._mywarns_last_call.clear()


# ── T1. Базовое поведение ───────────────────────────────────────────────────

class TestMywarnsFormatting(_MywarnsTestCase):

    async def test_no_warns_returns_none(self):
        """Без варнов форматтер возвращает None — вызывающий покажет «нет варнов»."""
        async with async_session() as s:
            result = await bot_handlers._format_user_warns_for_chat(s, _USER_ID, _CHAT_ID)
        self.assertIsNone(result)

    async def test_counts_active_warns(self):
        """Сводка содержит число активных варнов.

        v5.1.0: чат ещё не имеет явных настроек — _get_chat_settings создаёт
        ChatSettings с дефолтами модели (warns_to_mute=3, warns_to_ban=5,
        см. db.py). При total=2 ближайший порог — мьют.
        """
        await _add_warn()
        await _add_warn()
        async with async_session() as s:
            result = await bot_handlers._format_user_warns_for_chat(s, _USER_ID, _CHAT_ID)
        self.assertIsNotNone(result)
        self.assertEqual(result.split("\n")[0], "2/3 (до заглушения)")

    async def test_summary_groups_by_chat_title(self):
        """Сводка для лички группирует варны по названиям чатов."""
        await _add_chat(_CHAT_ID, "Первый чат")
        await _add_chat(_OTHER_CHAT_ID, "Второй чат")
        await _add_warn(_CHAT_ID)
        await _add_warn(_OTHER_CHAT_ID)
        async with async_session() as s:
            summary = await bot_handlers._format_user_warns_summary(s, _USER_ID)
        self.assertIn("Первый чат", summary)
        self.assertIn("Второй чат", summary)

    async def test_summary_empty_when_no_warns(self):
        async with async_session() as s:
            summary = await bot_handlers._format_user_warns_summary(s, _USER_ID)
        self.assertIn("нет активных варнов", summary.lower())


# ── T6. Русский месяц независимо от локали ──────────────────────────────────

class TestMywarnsDateLocale(_MywarnsTestCase):

    async def test_month_is_russian(self):
        """Дата последнего варна печатается русским месяцем.

        strftime("%b") зависит от локали, а в slim-образе её нет — юзер видел
        бы «12 Aug 2026» вместо «12 авг 2026».
        """
        await _add_warn(created_at=datetime(2026, 8, 12, 12, 30, tzinfo=timezone.utc))
        async with async_session() as s:
            result = await bot_handlers._format_user_warns_for_chat(s, _USER_ID, _CHAT_ID)
        self.assertIsNotNone(result)
        self.assertIn("авг", result)
        self.assertNotIn("Aug", result)


# ── T2/T3. Приватность и таймаут в группе ───────────────────────────────────

class TestMywarnsGroupPrivacy(_MywarnsTestCase):

    async def test_command_message_deleted(self):
        """Команда удаляется из чата — её не должны видеть другие участники."""
        await _add_warn()
        msg = _make_group_message()
        await bot_handlers.handle_mywarns_group(msg)
        msg.delete.assert_awaited_once()

    async def test_command_deleted_even_when_rate_limited(self):
        """Даже при срабатывании таймаута команда удаляется.

        Приватность — единственная причина, по которой команда вообще
        удаляется. Если при таймауте сообщение остаётся в чате, участники
        видят, кто проверяет свои варны — ровно то, чего фича избегает.
        """
        await _add_warn()
        first = _make_group_message()
        await bot_handlers.handle_mywarns_group(first)

        second = _make_group_message()
        await bot_handlers.handle_mywarns_group(second)

        second.delete.assert_awaited_once()

    async def test_rate_limited_call_sends_nothing(self):
        """Второй вызов в окне таймаута не шлёт повторное сообщение."""
        await _add_warn()
        first = _make_group_message()
        await bot_handlers.handle_mywarns_group(first)

        second = _make_group_message()
        await bot_handlers.handle_mywarns_group(second)

        second.bot.send_message.assert_not_awaited()


# ── T7. Адресация ephemeral ─────────────────────────────────────────────────

class TestMywarnsEphemeralDelivery(_MywarnsTestCase):
    """v4.9.0: ответ в группе — ephemeral в тот же чат, не личка.

    Личка требовала /start, а он открыт только модераторам: обычный юзер,
    ради которого фича делалась, не получал ничего.
    """

    async def test_answer_goes_to_group_chat_not_dm(self):
        """Сообщение уходит в чат, где введена команда, а не в личку юзеру."""
        await _add_warn()
        msg = _make_group_message()
        await bot_handlers.handle_mywarns_group(msg)

        msg.bot.send_message.assert_awaited_once()
        kwargs = msg.bot.send_message.await_args.kwargs
        self.assertEqual(
            kwargs["chat_id"], _CHAT_ID,
            "ответ должен уходить в групповой чат, а не в личку",
        )

    async def test_answer_is_addressed_to_command_author(self):
        """receiver_user_id — автор команды: остальные участники ответа не видят."""
        await _add_warn()
        msg = _make_group_message()
        await bot_handlers.handle_mywarns_group(msg)

        kwargs = msg.bot.send_message.await_args.kwargs
        self.assertEqual(
            kwargs.get("receiver_user_id"), _USER_ID,
            "без receiver_user_id сообщение станет публичным для всего чата",
        )

    async def test_answer_contains_warn_count(self):
        """В тексте — прогресс по количеству варнов до ближайшего дефолтного порога."""
        await _add_warn()
        msg = _make_group_message()
        await bot_handlers.handle_mywarns_group(msg)

        self.assertIn(
            "Ваши варны в этом чате: 1/3 (до заглушения)",
            msg.bot.send_message.await_args.kwargs["text"],
        )

    async def test_empty_case_is_ephemeral_too(self):
        """«Нет варнов» тоже адресное, а не публичное.

        Публичное «у вас нет варнов» раскрывает чату, что юзер проверялся —
        ровно то, ради чего удаляется сама команда.
        """
        msg = _make_group_message()
        await bot_handlers.handle_mywarns_group(msg)

        msg.bot.send_message.assert_awaited_once()
        kwargs = msg.bot.send_message.await_args.kwargs
        self.assertEqual(kwargs["chat_id"], _CHAT_ID)
        self.assertEqual(kwargs.get("receiver_user_id"), _USER_ID)
        self.assertIn("нет варнов", kwargs["text"])

    async def test_autodelete_is_scheduled(self):
        """Авто-удаление планируется: ephemeral в Telegram сам не исчезает."""
        await _add_warn()
        msg = _make_group_message()
        await bot_handlers.handle_mywarns_group(msg)

        self._sched_mock.assert_awaited_once()
        self.assertGreater(
            self._sched_mock.await_args.kwargs["delete_after"], 0,
            "без положительного delete_after сообщение останется в чате навсегда",
        )


# ── T4. Реестр таймаутов не растёт бесконечно ───────────────────────────────

class TestMywarnsTimeoutRegistry(_MywarnsTestCase):

    async def test_stale_entries_are_pruned(self):
        """Протухшие записи удаляются, иначе словарь растёт всё время работы.

        Ключ — (user_id, chat_id), запись на каждого юзера в каждом чате.
        Бот живёт месяцами без рестарта, чистки не было вовсе.
        """
        stale_key = (424242, -100999)
        bot_handlers._mywarns_last_call[stale_key] = (
            time.time() - bot_handlers._MYWARNS_TIMEOUT_SECONDS * 10
        )

        await _add_warn()
        msg = _make_group_message()
        await bot_handlers.handle_mywarns_group(msg)

        self.assertNotIn(stale_key, bot_handlers._mywarns_last_call)

    async def test_fresh_entries_are_kept(self):
        """Живые записи чистка не трогает — иначе таймаут перестанет работать."""
        fresh_key = (424242, -100999)
        bot_handlers._mywarns_last_call[fresh_key] = time.time()

        await _add_warn()
        msg = _make_group_message()
        await bot_handlers.handle_mywarns_group(msg)

        self.assertIn(fresh_key, bot_handlers._mywarns_last_call)


# ── T5. Отправка переживает flood control ───────────────────────────────────

class TestMywarnsFloodControl(_MywarnsTestCase):

    async def test_group_ephemeral_retries_after_flood_control(self):
        """При 429 сообщение доходит после ретрая, а не теряется.

        Правило проекта: Telegram-вызовы идут через tg_safe_call. Здесь это
        особенно заметно, потому что таймаут уже записан — без ретрая юзер
        не сможет повторить запрос ещё 5 минут.

        v4.9.0: отправка переехала в _send_ephemeral, и защита должна была
        переехать вместе с ней. Обёртка живёт внутри хелпера — снаружи её
        не навесить, он глотает TelegramAPIError сам.
        """
        await _add_warn()
        msg = _make_group_message()

        calls: list[int] = []

        async def _flaky(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise TelegramRetryAfter(
                    method=MagicMock(), message="flood", retry_after=0,
                )
            return MagicMock()

        msg.bot.send_message = AsyncMock(side_effect=_flaky)

        await bot_handlers.handle_mywarns_group(msg)

        self.assertEqual(len(calls), 2, "сообщение должно уйти со второй попытки")

    async def test_dm_reply_retries_after_flood_control(self):
        """То же для ответа в личке."""
        await _add_warn()
        msg = _make_dm_message()

        calls: list[int] = []

        async def _flaky(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise TelegramRetryAfter(
                    method=MagicMock(), message="flood", retry_after=0,
                )
            return MagicMock()

        msg.reply = AsyncMock(side_effect=_flaky)

        await bot_handlers.handle_mywarns_dm(msg)

        self.assertEqual(len(calls), 2, "ответ должен уйти со второй попытки")


if __name__ == "__main__":
    unittest.main(verbosity=2)
