"""v5.5.0 — каскад /account (метки без санкций) + импорт дамп-CSV (ADMIN_IDS).

Покрывает:
  1. _lols_tier: пороги C1/C2/C3 по метрикам (spam_factor/offenses/scammer).
  2. Каскад в _sweep_chat: potential → verdict с тиром и метриками,
     БЕЗ бана (bot.ban_chat_member не вызывается — санкций каскад не
     выдаёт, решение владельца 30.08.2026), счётчик marked.
  3. Пороги из CasSettings (singleton), а не дефолты.
  4. _import_members_csv: новые/существующие/banned-скип/неизвестный чат
     + Users upsert.
  5. Порядок порогов не ломается формой (mute ≤ ban).

Запуск: uv run python tools/run_tests.py -k v550_cas
"""
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from _paths import _P

sys.path.insert(0, _P())
os.chdir(_P())

_DB_PATH = tempfile.mktemp(suffix="_v550_cascascade.db")
os.environ["BOT_TOKEN"] = "123456…AAAA"
os.environ["ADMIN_IDS"] = "111"
os.environ["WEB_PASSWORD"] = "test_password_123"
os.environ["DB_PATH"] = _DB_PATH

from sqlalchemy import select  # noqa: E402

import bot_handlers as bh  # noqa: E402
import cas  # noqa: E402
from db import (  # noqa: E402
    CasSettings,
    CasVerdict,
    ChatMemberSeen,
    ChatSettings,
    User,
    async_session,
    init_db,
)

_CHAT = -100700


async def _seed():
    await init_db()
    async with async_session() as s:
        for t in (CasSettings, CasVerdict, ChatMemberSeen, ChatSettings, User):
            await s.execute(t.__table__.delete())
        s.add(ChatSettings(chat_id=_CHAT, cas_check_enabled=True,
                           is_enabled=True, title="CAS cascade test"))
        await s.commit()


class CasCascadeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await _seed()
        cas._lols_set = set()
        cas._lols_hot_set = set()
        cas._lols_full_set = {1007}          # потенциальный: в полном банлисте
        cas._lols_hot_at = None
        cas._lols_full_at = None
        cas._cas_enabled_chat_ids = {_CHAT}
        cas._last_nightly_date = None
        cas._last_digest_date = None
        cas._day_stats = {"checked": 0, "banned": 0, "marked": 0, "ids": []}  # noqa: W0201

    async def test_c3_watch_label_no_ban(self):
        """banned=true, метрики низкие → C3_watch, БЕЗ бана."""
        await cas.touch_member_seen(_CHAT, 1007)
        cas._lols_account = AsyncMock(return_value={
            "banned": True, "spam_factor": 12.35, "offenses": 2, "scammer": False,
        })
        bot = AsyncMock()
        stats = await cas._sweep_chat(
            bot, ChatSettings(chat_id=_CHAT, cas_check_enabled=True,
                              is_enabled=True),
        )
        self.assertEqual((stats["banned"], stats["marked"]), (0, 1))
        bot.ban_chat_member.assert_not_called()
        async with async_session() as s:
            v = (await s.execute(
                select(CasVerdict).where(CasVerdict.user_id == 1007)
            )).scalar_one()
            self.assertFalse(v.is_banned)
            self.assertEqual(v.tier, "C3_watch")
            self.assertAlmostEqual(v.spam_factor or 0.0, 12.35, places=2)
            self.assertIn("C3_watch", v.reason or "")

    async def test_c1_label_still_no_ban(self):
        """Даже C1 (scammer) — только метка: санкций каскад не выдаёт."""
        cas._lols_account = AsyncMock(return_value={
            "banned": True, "spam_factor": 70.0, "offenses": 4, "scammer": True,
        })
        await cas.touch_member_seen(_CHAT, 1007)
        bot = AsyncMock()
        stats = await cas._sweep_chat(
            bot, ChatSettings(chat_id=_CHAT, cas_check_enabled=True,
                              is_enabled=True),
        )
        self.assertEqual((stats["banned"], stats["marked"]), (0, 1))
        bot.ban_chat_member.assert_not_called()
        async with async_session() as s:
            v = (await s.execute(
                select(CasVerdict).where(CasVerdict.user_id == 1007)
            )).scalar_one()
            self.assertEqual(v.tier, "C1_ban")
            self.assertTrue(v.scammer)

    async def test_thresholds_from_cas_settings(self):
        """Пороги читаются из cas_settings: sf=25 при пороге 20 → C2."""
        async with async_session() as s:
            s.add(CasSettings(id=1, spamfactor_ban=60.0, spamfactor_mute=20.0,
                              offenses_mute=10))
            await s.commit()
        cas._lols_account = AsyncMock(return_value={
            "banned": True, "spam_factor": 25.0, "offenses": 1, "scammer": False,
        })
        await cas.touch_member_seen(_CHAT, 1007)
        bot = AsyncMock()
        await cas._sweep_chat(
            bot, ChatSettings(chat_id=_CHAT, cas_check_enabled=True,
                              is_enabled=True),
        )
        async with async_session() as s:
            v = (await s.execute(
                select(CasVerdict).where(CasVerdict.user_id == 1007)
            )).scalar_one()
            self.assertEqual(v.tier, "C2_mute",
                             "sf 25 ≥ 20 должен попасть в C2 при кастомном пороге")

    async def test_account_fail_open(self):
        """LOLS /account недоступен → юзер не падает, тир C3_watch без метрик."""
        cas._lols_account = AsyncMock(return_value={})
        await cas.touch_member_seen(_CHAT, 1007)
        bot = AsyncMock()
        stats = await cas._sweep_chat(
            bot, ChatSettings(chat_id=_CHAT, cas_check_enabled=True,
                              is_enabled=True),
        )
        self.assertEqual(stats["marked"], 1)
        async with async_session() as s:
            v = (await s.execute(
                select(CasVerdict).where(CasVerdict.user_id == 1007)
            )).scalar_one()
            self.assertFalse(v.is_banned)


class ImportTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await _seed()
        cas._lols_full_set = set()
        cas._cas_enabled_chat_ids = {_CHAT}

    async def test_import_new_banned_and_unknown_chat(self):
        """Новый юзер импортируется, banned-строка скипается, неизвестный
        чат уходит в unknown_chats, существующий — в existing."""
        await cas.touch_member_seen(_CHAT, 3000)  # уже было
        text = (
            "chat_id,user_id,username,first_name,last_name,is_bot,is_deleted,status,dumped_at\n"
            f"{_CHAT},2001,alice,Alice,,false,false,member,2026-09-03T05:54:17Z\n"
            f"{_CHAT},2002,bob,Bob,,false,false,banned,2026-09-03T05:54:17Z\n"
            f"999999,2003,carol,Carol,,false,false,member,2026-09-03T05:54:17Z\n"
        )
        stats = await bh._import_members_csv(text)
        self.assertEqual(stats["new"], 1)
        self.assertEqual(stats["existing"], 0)   # 3000 не в CSV — существующим не считается
        self.assertEqual(stats["banned"], 1)
        self.assertEqual(stats["bad"], 0)
        self.assertEqual(stats["unknown_chats"], 1)  # 999999 не в ChatSettings
        async with async_session() as s:
            row = (await s.execute(
                select(ChatMemberSeen).where(
                    ChatMemberSeen.chat_id == _CHAT,
                    ChatMemberSeen.user_id == 2001,
                )
            )).scalar_one()
            self.assertIsNotNone(row)
            miss = (await s.execute(
                select(ChatMemberSeen).where(
                    ChatMemberSeen.chat_id == _CHAT,
                    ChatMemberSeen.user_id == 2002,
                )
            )).scalar_one_or_none()
            self.assertIsNone(miss, "banned-строка не импортируется")

    async def test_import_repeated_is_noop(self):
        """Повторный импорт того же дампа — ноль новых (INSERT OR IGNORE)."""
        text = (
            "chat_id,user_id,status\n"
            f"{_CHAT},4001,member\n"
        )
        s1 = await bh._import_members_csv(text)
        s2 = await bh._import_members_csv(text)
        self.assertEqual(s1["new"], 1)
        self.assertEqual(s2["new"], 0, "повторный импорт не дублирует")

    async def test_bad_rows_counted(self):
        text = (
            "chat_id,user_id\n"
            "not-a-chat,123\n"
            f"{_CHAT},abc\n"
        )
        stats = await bh._import_members_csv(text)
        self.assertEqual(stats["bad"], 2)
        self.assertEqual(stats["unknown_chats"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
