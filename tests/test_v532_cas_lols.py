"""v5.3.2 — CAS/LOLS ночные проверки: кэш вердиктов, seen, ignore, свип.

Покрывает:
  1. touch_member_seen: first/last_seen upsert (cas.py).
  2. _cached_verdict: свежий вердикт отдаётся, отсутствие — None.
  3. _sweep_chat: LOLS banned → бан без похода в CAS; чистый юзер →
     CAS-check через monkeypatch; свежий кэш / cas_ignore / админ — skip.
  4. Разбан CAS-бана → юзер в cas_ignore (revoke_user_ban hook).
  5. Регресс-тесты на code-review фиксы (см. коммит): реальная
     регистрация MembersSeenMiddleware через Dispatcher, точный матч
     CAS-бана вместо подстроки "cas", exempt по WebUser.role, LOLS
     bulk-ответ неожиданной формы, переполнение id-списка в дайджесте.

Запуск: uv run python tools/run_tests.py -k v532_cas_lols
"""
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from _paths import _P

sys.path.insert(0, _P())
os.chdir(_P())

_DB_PATH = tempfile.mktemp(suffix="_v532_caslols.db")
os.environ["BOT_TOKEN"] = "123456…AAAA"
os.environ["ADMIN_IDS"] = "111"
os.environ["WEB_PASSWORD"] = "test_password_123"
os.environ["DB_PATH"] = _DB_PATH

from sqlalchemy import select  # noqa: E402

import bot_handlers as bh  # noqa: E402 — router + хуки разбана
import cas  # noqa: E402 — регистрирует MembersSeenMiddleware на bh.router
from db import (  # noqa: E402
    CasIgnore,
    CasVerdict,
    ChatAdmin,
    ChatMemberSeen,
    ChatSettings,
    Moderator,
    Punishment,
    User,
    WebUser,
    async_session,
    init_db,
)

_CHAT_ID = -100700


async def _seed():
    await init_db()
    async with async_session() as s:
        for tbl in (CasIgnore, CasVerdict, ChatMemberSeen, ChatAdmin,
                    ChatSettings, Punishment, User, WebUser, Moderator):
            await s.execute(tbl.__table__.delete())
        s.add(ChatSettings(chat_id=_CHAT_ID, cas_check_enabled=True,
                           is_enabled=True, title="CAS test chat"))
        # Пользователи для FK punishments.user_id.
        for uid in (1002, 777):
            s.add(User(user_id=uid))
        await s.commit()


class CasSweepTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await _seed()
        cas._lols_set = set()
        cas._lols_hot_set = set()
        cas._lols_full_set = set()
        cas._lols_hot_at = None
        cas._lols_full_at = None
        cas._cas_enabled_chat_ids = {_CHAT_ID}
        cas._last_sanitary_sweep = {}
        cas._last_nightly_date = None
        cas._last_digest_date = None
        cas._day_stats = {"checked": 0, "banned": 0, "marked": 0,
                          "ids": []}

    async def test_touch_member_seen_upsert(self):
        await cas.touch_member_seen(_CHAT_ID, 1001)
        await cas.touch_member_seen(_CHAT_ID, 1001)  # второй — обновляет
        async with async_session() as s:
            row = (await s.execute(
                select(ChatMemberSeen).where(
                    ChatMemberSeen.chat_id == _CHAT_ID,
                    ChatMemberSeen.user_id == 1001,
                )
            )).scalar_one()
            self.assertGreaterEqual(row.last_seen_at, row.first_seen_at)
            self.assertIsNotNone(row.first_seen_at)

    async def test_cached_verdict(self):
        self.assertIsNone(await cas._cached_verdict(222))
        await cas._store_verdict(222, "lols", True, "LOLS banlist")
        self.assertEqual(await cas._cached_verdict(222),
                         (True, "lols", "LOLS banlist"))

    async def test_sweep_lols_ban_without_cas_call(self):
        """1002 в LOLS Set → бан БЕЗ запроса к CAS; 1001 чистый → 1 CAS-запрос."""
        cas._lols_set = {1002}
        await cas.touch_member_seen(_CHAT_ID, 1001)
        await cas.touch_member_seen(_CHAT_ID, 1002)

        calls = {"n": 0}

        async def fake_cas(user_id):
            calls["n"] += 1
            return False, None

        orig = bh._cas_check_user
        bh._cas_check_user = fake_cas
        try:
            bot = AsyncMock()
            stats = await cas._sweep_chat(
                bot, ChatSettings(chat_id=_CHAT_ID, cas_check_enabled=True,
                                  is_enabled=True),
            )
        finally:
            bh._cas_check_user = orig

        self.assertEqual(stats["checked"], 1)          # только 1001 (1002 из LOLS)
        self.assertEqual(stats["banned"], 1)
        self.assertEqual(stats["users"], [1002])
        self.assertEqual(calls["n"], 1, "CAS дергается только для чистого юзера")
        bot.ban_chat_member.assert_called()

        async with async_session() as s:
            p = (await s.execute(
                select(Punishment).where(
                    Punishment.user_id == 1002,
                    Punishment.chat_id == _CHAT_ID,
                )
            )).scalars().all()
            self.assertTrue(
                any("CAS nightly sweep" in (x.reason or "") for x in p),
                "должна быть запись о бане с причиной CAS nightly sweep",
            )

    async def test_sweep_hot_ban_tier_b(self):
        """Tier B: юзер в banlist-1h (забанен LOLS час назад) → бан без CAS."""
        cas._lols_hot_set = {1008}
        await cas.touch_member_seen(_CHAT_ID, 1008)

        calls = {"n": 0}

        async def fake_cas(user_id):
            calls["n"] += 1
            return False, None

        orig = bh._cas_check_user
        bh._cas_check_user = fake_cas
        try:
            bot = AsyncMock()
            stats = await cas._sweep_chat(
                bot, ChatSettings(chat_id=_CHAT_ID, cas_check_enabled=True,
                                  is_enabled=True),
            )
        finally:
            bh._cas_check_user = orig

        self.assertEqual((stats["checked"], stats["banned"]), (0, 1))
        self.assertEqual(stats["users"], [1008])
        self.assertEqual(calls["n"], 0, "hot-тир банится без CAS-запроса")
        bot.ban_chat_member.assert_called()

    async def test_sweep_potential_marked_not_banned(self):
        """Tier C: юзер в полном банлисте LOLS, но не подтверждён → пометка,
        НЕ бан; CAS всё равно проверяется (CAS banned = подтверждение)."""
        cas._lols_full_set = {1007}
        await cas.touch_member_seen(_CHAT_ID, 1007)

        async def fake_cas(user_id):
            return False, None  # CAS чист

        orig = bh._cas_check_user
        bh._cas_check_user = fake_cas
        try:
            bot = AsyncMock()
            stats = await cas._sweep_chat(
                bot, ChatSettings(chat_id=_CHAT_ID, cas_check_enabled=True,
                                  is_enabled=True),
            )
        finally:
            bh._cas_check_user = orig

        self.assertEqual(stats["banned"], 0, "потенциального НЕ баним")
        self.assertEqual(stats["marked"], 1)
        bot.ban_chat_member.assert_not_called()

        async with async_session() as s:
            v = (await s.execute(
                select(CasVerdict).where(CasVerdict.user_id == 1007)
            )).scalar_one()
            self.assertFalse(v.is_banned)
            self.assertIn("potential", v.reason or "")

    async def test_hot_ban_beats_stale_clean_verdict(self):
        """v5.4.0 регресс: свежий вердикт «чист» не должен отменять Tier B.

        Со старым порядком (кэш-первый) сидящий с вердиктом «чист» с
        прошлого свипа пропускался целиком на 30 дней — то есть попасть
        под banlist-1h успевали только незнакомые боту юзеры.
        """
        await cas.touch_member_seen(_CHAT_ID, 1003)
        await cas._store_verdict(1003, "cas", False, None)  # свежий «чист»
        cas._lols_hot_set = {1003}

        async def fake_cas(user_id):
            raise AssertionError("CAS не должен дёргаться для hot-тира")

        orig = bh._cas_check_user
        bh._cas_check_user = fake_cas
        try:
            bot = AsyncMock()
            stats = await cas._sweep_chat(
                bot, ChatSettings(chat_id=_CHAT_ID, cas_check_enabled=True,
                                  is_enabled=True),
            )
        finally:
            bh._cas_check_user = orig

        self.assertEqual(stats["banned"], 1)
        self.assertEqual(stats["users"], [1003])

    async def test_fresh_ban_verdict_not_rebanned(self):
        """Обратная сторона: уже забаненного по вердикту не банимся снова,
        даже если он всё ещё лежит в verified-списке LOLS."""
        await cas.touch_member_seen(_CHAT_ID, 1009)
        await cas._store_verdict(1009, "lols", True,
                                 "verified scammer (LOLS)")
        cas._lols_set = {1009}

        bot = AsyncMock()
        stats = await cas._sweep_chat(
            bot, ChatSettings(chat_id=_CHAT_ID, cas_check_enabled=True,
                              is_enabled=True),
        )
        self.assertEqual(stats["banned"], 0)
        bot.ban_chat_member.assert_not_called()

    async def test_sweep_skips_fresh_verdict_ignore_and_admin(self):
        """Свежий кэш, cas_ignore и админ чата не проверяются вообще."""
        await cas.touch_member_seen(_CHAT_ID, 1003)   # свежий вердикт → skip
        await cas._store_verdict(1003, "cas", True, "old")
        await cas.touch_member_seen(_CHAT_ID, 1004)   # cas_ignore → skip
        async with async_session() as s:
            s.add(CasIgnore(user_id=1004, added_by=111, comment="fp"))
            s.add(ChatAdmin(chat_id=_CHAT_ID, user_id=1005))  # админ → exempt
            await s.commit()
        await cas.touch_member_seen(_CHAT_ID, 1005)

        calls = {"n": 0}

        async def fake_cas(user_id):
            calls["n"] += 1
            return False, None

        orig = bh._cas_check_user
        bh._cas_check_user = fake_cas
        try:
            stats = await cas._sweep_chat(
                AsyncMock(),
                ChatSettings(chat_id=_CHAT_ID, cas_check_enabled=True,
                             is_enabled=True),
            )
        finally:
            bh._cas_check_user = orig

        self.assertEqual((stats["checked"], stats["banned"]), (0, 0))
        self.assertEqual(calls["n"], 0, "ни один из трёх юзеров не проверялся")

    async def test_digest_text(self):
        cas._accumulate({"checked": 400, "banned": 2, "users": [900, 901]})
        text = cas._digest_text()
        self.assertIn("проверено 400", text)
        self.assertIn("забанено 2", text)
        self.assertIn("900", text)

    async def test_unban_cas_adds_ignore(self):
        """Разбан CAS-бана → юзер в cas_ignore (хук в revoke_user_ban)."""
        bot = AsyncMock()
        async with async_session() as s:
            s.add(Moderator(mod_id=0))
            s.add(Punishment(user_id=777, mod_id=0, chat_id=_CHAT_ID,
                             action_type="ban",
                             reason="CAS auto-ban: spam test",
                             permissions_snapshot=None))
            await s.commit()

        await bh.revoke_user_ban(bot=bot, chat_id=_CHAT_ID, user_id=777,
                                 mod_id=111, reason="ложняк",
                                 target_user=None)

        async with async_session() as s:
            row = (await s.execute(
                select(CasIgnore).where(CasIgnore.user_id == 777)
            )).scalar_one_or_none()
            self.assertIsNotNone(row, "юзер должен попасть в cas_ignore")
            self.assertIn("CAS", row.comment or "")

    async def test_unban_non_cas_reason_not_ignored(self):
        """code-review fix: обычный бан с 'cas' в тексте — не CAS-бан.

        Раньше матчилось по голой подстроке "cas" где угодно в причине —
        "casino spam" (обычный ручной бан) ложно улетал в cas_ignore.
        """
        bot = AsyncMock()
        async with async_session() as s:
            s.add(User(user_id=778))
            s.add(Moderator(mod_id=222))
            s.add(Punishment(user_id=778, mod_id=222, chat_id=_CHAT_ID,
                             action_type="ban",
                             reason="casino spam",
                             permissions_snapshot=None))
            await s.commit()

        await bh.revoke_user_ban(bot=bot, chat_id=_CHAT_ID, user_id=778,
                                 mod_id=111, reason="ошибка модератора",
                                 target_user=None)

        async with async_session() as s:
            row = (await s.execute(
                select(CasIgnore).where(CasIgnore.user_id == 778)
            )).scalar_one_or_none()
            self.assertIsNone(
                row, "бан с 'casino' в причине не должен считаться CAS-баном",
            )

    async def test_unban_other_auto_filter_not_ignored(self):
        """mod_id=0 сам по себе не значит CAS — его же используют
        Sticker/Via-bot/Content Filter."""
        bot = AsyncMock()
        async with async_session() as s:
            s.add(User(user_id=779))
            s.add(Moderator(mod_id=0))
            s.add(Punishment(user_id=779, mod_id=0, chat_id=_CHAT_ID,
                             action_type="ban",
                             reason="Banned sticker pack: evil_pack",
                             permissions_snapshot=None))
            await s.commit()

        await bh.revoke_user_ban(bot=bot, chat_id=_CHAT_ID, user_id=779,
                                 mod_id=111, reason="ложняк стикер-фильтра",
                                 target_user=None)

        async with async_session() as s:
            row = (await s.execute(
                select(CasIgnore).where(CasIgnore.user_id == 779)
            )).scalar_one_or_none()
            self.assertIsNone(
                row, "бан от Sticker Filter (mod_id=0) — не CAS-бан",
            )

    async def test_unban_cas_twice_does_not_raise(self):
        """Повторный разбан того же юзера — upsert, не IntegrityError по PK."""
        bot = AsyncMock()
        async with async_session() as s:
            s.add(Moderator(mod_id=0))
            s.add(Punishment(user_id=777, mod_id=0, chat_id=_CHAT_ID,
                             action_type="ban",
                             reason="CAS nightly sweep (cas): banned",
                             permissions_snapshot=None))
            await s.commit()

        for _ in range(2):
            result = await bh.revoke_user_ban(
                bot=bot, chat_id=_CHAT_ID, user_id=777,
                mod_id=111, reason="ложняк", target_user=None,
            )
            self.assertTrue(result["ok"], result)

    async def test_sweep_exempts_web_admin_without_chat_admin_row(self):
        """code-review fix: exempt по WebUser.role, не только chat_admins.

        su/admin веб-панели, не засинканный в chat_admins для конкретного
        чата, раньше не исключался из ночного свипа.
        """
        await cas.touch_member_seen(_CHAT_ID, 1006)
        async with async_session() as s:
            s.add(WebUser(username="webadmin1", role="admin",
                          is_active=True, tg_user_id=1006))
            await s.commit()

        calls = {"n": 0}

        async def fake_cas(user_id):
            calls["n"] += 1
            return False, None

        orig = bh._cas_check_user
        bh._cas_check_user = fake_cas
        try:
            stats = await cas._sweep_chat(
                AsyncMock(),
                ChatSettings(chat_id=_CHAT_ID, cas_check_enabled=True,
                             is_enabled=True),
            )
        finally:
            bh._cas_check_user = orig

        self.assertEqual(calls["n"], 0, "web-admin должен быть exempt")
        self.assertEqual(stats["checked"], 0)

    async def test_digest_text_overflow_indicator(self):
        """code-review fix: '+N ещё' должен появляться, если банов больше 5."""
        cas._accumulate({
            "checked": 10, "banned": 7,
            "users": [901, 902, 903, 904, 905, 906, 907],
        })
        text = cas._digest_text()
        self.assertIn("забанено 7", text)
        self.assertIn("(+2)", text)


class LolsRefreshTest(unittest.IsolatedAsyncioTestCase):
    """code-review fix: неожиданная форма ответа lols.bot не должна молча
    обнулять уже загруженный банлист."""

    async def asyncSetUp(self):
        cas._lols_set = {12345}
        cas._lols_loaded_at = None

    @staticmethod
    def _fake_session(body: bytes, status: int = 200):
        class _FakeResp:
            def __init__(self):
                self.status = status

            async def read(self):
                return body

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class _FakeSession:
            def get(self, url):
                return _FakeResp()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        return _FakeSession()

    async def test_non_list_response_keeps_old_set(self):
        body = b'{"users": [12345, 999]}'  # неожиданная форма
        with patch("cas.aiohttp.ClientSession",
                   return_value=self._fake_session(body)):
            size = await cas.refresh_lols_list()

        self.assertEqual(size, 1)
        self.assertEqual(cas._lols_set, {12345},
                         "старый набор должен сохраниться, а не обнулиться")

    async def test_scammers_json_shape_parsed(self):
        """scammers.json: список словарей с user_id (Tier A)."""
        body = (b'[{"user_id":8045177421,"names":["X"],"usernames":["@x"]},'
                b'{"user_id":777,"names":[]}]')
        with patch("cas.aiohttp.ClientSession",
                   return_value=self._fake_session(body)):
            await cas.refresh_lols_list()
        self.assertEqual(cas._lols_set, {8045177421, 777})

    async def test_flat_int_list_shape_parsed(self):
        """v5.4.0 регресс: banlist-1h.json/banlist.json — ПЛОСКИЙ список
        чисел, а не словарей. Старый парсер разбирал их в пустой набор,
        и Tier B с Tier C не работали в проде вообще."""
        cas._lols_hot_set = set()
        cas._lols_hot_at = None
        body = b"[350007112,674584862,-1004499466373,7006041058]"
        with patch("cas.aiohttp.ClientSession",
                   return_value=self._fake_session(body)):
            size = await cas.refresh_lols_hot()

        self.assertEqual(size, 4)
        self.assertIn(350007112, cas._lols_hot_set)
        self.assertIn(7006041058, cas._lols_hot_set)
        self.assertIsNotNone(cas._lols_hot_at)

    async def test_hot_refresh_failure_keeps_previous_set(self):
        """Сбой сети не обнуляет часовой набор: он максимум на час старее."""
        cas._lols_hot_set = {555}
        cas._lols_hot_at = None
        with patch("cas.aiohttp.ClientSession",
                   return_value=self._fake_session(b"", status=500)):
            size = await cas.refresh_lols_hot()

        self.assertEqual(size, 1)
        self.assertEqual(cas._lols_hot_set, {555})
        self.assertIsNone(cas._lols_hot_at,
                          "время загрузки не штампуется — тик повторит")

    async def test_release_lols_full_frees_memory(self):
        """Полный банлист живёт только внутри ночного окна."""
        cas._lols_full_set = {1, 2, 3}
        cas._lols_full_at = None
        cas._release_lols_full()
        self.assertEqual(cas._lols_full_set, set())


class CasSweepDispatcherTest(unittest.IsolatedAsyncioTestCase):
    """Настоящий aiogram-Dispatcher: MembersSeenMiddleware обязана
    сработать в реальной цепочке, а не только при прямом вызове.

    code-review fix: `_bh_router.message(MembersSeenMiddleware())`
    регистрировал хендлер, а не middleware — chat_members_seen никогда
    не наполнялся в проде, и ни один существующий тест (все вызывали
    touch_member_seen напрямую) этого не ловил.
    """

    _dp = None
    _bot = None

    async def asyncSetUp(self):
        await _seed()
        cas._cas_enabled_chat_ids = {_CHAT_ID}

    @classmethod
    def _dispatcher(cls):
        if cls._dp is None:
            from aiogram import Bot, Dispatcher
            cls._bot = Bot(token="1234567890:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
            cls._dp = Dispatcher()
            cls._dp.include_router(bh.router)
        return cls._dp, cls._bot

    async def _feed(self, message):
        from aiogram.types import Update
        dp, bot = self._dispatcher()
        await dp.feed_update(bot=bot, update=Update(update_id=1, message=message))

    async def test_message_in_cas_chat_populates_chat_members_seen(self):
        from aiogram.types import Chat, Message
        from aiogram.types import User as TgUser
        msg = Message(
            message_id=901,
            date=1699999999,
            chat=Chat(id=_CHAT_ID, type="supergroup", title="CAS test chat"),
            from_user=TgUser(id=42001, is_bot=False, first_name="Иван"),
            text="привет",
        )
        await self._feed(msg)

        async with async_session() as s:
            row = (await s.execute(
                select(ChatMemberSeen).where(
                    ChatMemberSeen.chat_id == _CHAT_ID,
                    ChatMemberSeen.user_id == 42001,
                )
            )).scalar_one_or_none()
        self.assertIsNotNone(
            row, "MembersSeenMiddleware должна была отработать через "
                 "реальный Dispatcher и записать last_seen",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
