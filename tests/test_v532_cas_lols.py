"""v5.3.2 — CAS/LOLS ночные проверки: кэш вердиктов, seen, ignore, свип.

Покрывает:
  1. touch_member_seen: first/last_seen upsert (cas.py).
  2. _cached_verdict: свежий вердикт отдаётся, отсутствие — None.
  3. _sweep_chat: LOLS banned → бан без похода в CAS; чистый юзер →
     CAS-check через monkeypatch; свежий кэш / cas_ignore / админ — skip.
  4. Разбан CAS-бана → юзер в cas_ignore (revoke_user_ban hook).

Запуск: uv run python tools/run_tests.py -k v532_cas_lols
"""
from _paths import _P  # noqa: E402
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock

sys.path.insert(0, _P())
os.chdir(_P())

_DB_PATH = tempfile.mktemp(suffix="_v532_caslols.db")
os.environ["BOT_TOKEN"] = "123456…AAAA"
os.environ["ADMIN_IDS"] = "111"
os.environ["WEB_PASSWORD"] = "test_password_123"
os.environ["DB_PATH"] = _DB_PATH

from sqlalchemy import select  # noqa: E402

import bot_handlers as bh  # noqa: E402,F401 — router + хуки разбана
import cas  # noqa: E402
from db import (  # noqa: E402
    CasIgnore,
    CasVerdict,
    ChatAdmin,
    ChatMemberSeen,
    ChatSettings,
    Punishment,
    User,
    async_session,
    init_db,
)

_CHAT_ID = -100700


async def _seed():
    await init_db()
    async with async_session() as s:
        for tbl in (CasIgnore, CasVerdict, ChatMemberSeen, ChatAdmin,
                    ChatSettings, Punishment, User):
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
        cas._cas_enabled_chat_ids = {_CHAT_ID}
        cas._last_sanitary_sweep = {}
        cas._last_nightly_date = None
        cas._last_digest_date = None
        cas._day_stats = {"checked": 0, "banned": 0, "ids": []}  # noqa: W0201

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
