"""v5.3.0 — веб-управление белым списком каналов и тумблером фильтра.

Секция «Вайтлист каналов» на /admin/presets — веб-эквивалент команд
/channelallow, /channelunallow, /channellist. Сделана по образцу вайтлиста
ботов (v5.1.0) на этой же странице: те же require_csrf_admin, hard delete
и flash-редиректы.

Тумблер CHAN живёт на /admin/chats рядом с LINK/VIA/CAS — фильтр
включается per-chat и по умолчанию выключен.

Запуск: uv run python tools/run_tests.py -k v530_channel_whitelist_web
"""
from _paths import _P  # noqa: E402
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, _P())
os.chdir(_P())

_DB_PATH = tempfile.mktemp(suffix="_v530_chanweb.db")
os.environ["BOT_TOKEN"] = "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
os.environ["ADMIN_IDS"] = "1"
os.environ["WEB_PASSWORD"] = "test_password_123"
os.environ["DB_PATH"] = _DB_PATH

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select, text  # noqa: E402

import bot_handlers as bh  # noqa: E402,F401 — чтобы aiogram router загрузился
import web_app  # noqa: E402
from db import (  # noqa: E402
    ChannelWhitelist,
    ChatSettings,
    WebUser,
    async_session,
    engine,
    init_db,
)

_CHAT_ID = -1005100000001
_FOREIGN_CHANNEL_ID = -1005555555555


async def _seed():
    await init_db()
    async with async_session() as s:
        await s.execute(text("DELETE FROM chat_settings"))
        await s.execute(text("DELETE FROM web_users WHERE username != 'su'"))
        await s.execute(text("DELETE FROM channel_whitelist"))
        await s.commit()
    await init_db()
    async with async_session() as s:
        existing_su = (await s.execute(
            select(WebUser).where(WebUser.username == "su")
        )).scalar_one_or_none()
        if existing_su is None:
            s.add(WebUser(username="su", is_su=True, is_active=True,
                          role="su", created_by="system"))
            await s.commit()
        s.add(ChatSettings(
            chat_id=_CHAT_ID, title="Test Chat v5.3.0", is_enabled=True,
        ))
        await s.commit()


class TestChannelWhitelistWeb(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await _seed()
        web_app._check_login_rate_limit = lambda ip: True
        self._mock_bot = MagicMock()
        self._mock_bot.get_chat_administrators = AsyncMock(return_value=[])
        self.client = TestClient(web_app.create_app(bot=self._mock_bot))
        r = self.client.post("/login", data={
            "username": "su", "password": "test_password_123",
        }, follow_redirects=False)
        assert r.status_code in (303, 200)

    async def asyncTearDown(self):
        try:
            engine.sync_engine.dispose()
        except Exception:
            pass
        if os.path.exists(_DB_PATH):
            os.remove(_DB_PATH)

    async def test_presets_page_renders_channel_section(self):
        r = self.client.get("/admin/presets", follow_redirects=False)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Вайтлист каналов", r.text)
        self.assertIn('action="/admin/presets/channels/add"', r.text)

    async def test_add_by_channel_id(self):
        r = self.client.post("/admin/presets/channels/add", data={
            "chat_id_str": "0",
            "channel_ref": str(_FOREIGN_CHANNEL_ID),
            "note": "партнёры",
        }, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        async with async_session() as s:
            row = (await s.execute(select(ChannelWhitelist))).scalar_one()
        self.assertEqual(row.chat_id, 0)
        self.assertEqual(row.channel_id, _FOREIGN_CHANNEL_ID)
        self.assertEqual(row.note, "партнёры")

    async def test_add_by_username_normalizes(self):
        self.client.post("/admin/presets/channels/add", data={
            "chat_id_str": str(_CHAT_ID),
            "channel_ref": "@SpamNews",
            "note": "",
        }, follow_redirects=False)
        async with async_session() as s:
            row = (await s.execute(select(ChannelWhitelist))).scalar_one()
        self.assertEqual(row.channel_username, "spamnews",
                         "username хранится в нижнем регистре без @")
        self.assertIsNone(row.channel_id)

    async def test_add_without_ref_is_rejected(self):
        r = self.client.post("/admin/presets/channels/add", data={
            "chat_id_str": "0", "channel_ref": "", "note": "",
        }, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        async with async_session() as s:
            rows = (await s.execute(select(ChannelWhitelist))).scalars().all()
        self.assertEqual(rows, [])

    async def test_duplicate_is_rejected(self):
        for _ in range(2):
            self.client.post("/admin/presets/channels/add", data={
                "chat_id_str": "0", "channel_ref": str(_FOREIGN_CHANNEL_ID),
                "note": "",
            }, follow_redirects=False)
        async with async_session() as s:
            rows = (await s.execute(select(ChannelWhitelist))).scalars().all()
        self.assertEqual(len(rows), 1)

    async def test_delete_removes_entry(self):
        self.client.post("/admin/presets/channels/add", data={
            "chat_id_str": "0", "channel_ref": str(_FOREIGN_CHANNEL_ID),
            "note": "",
        }, follow_redirects=False)
        async with async_session() as s:
            row = (await s.execute(select(ChannelWhitelist))).scalar_one()
            wl_id = row.id
        r = self.client.post(
            f"/admin/presets/channels/{wl_id}/delete", follow_redirects=False,
        )
        self.assertEqual(r.status_code, 303)
        async with async_session() as s:
            rows = (await s.execute(select(ChannelWhitelist))).scalars().all()
        self.assertEqual(rows, [])


class TestChannelFilterToggle(unittest.IsolatedAsyncioTestCase):
    """Тумблер CHAN на /admin/chats. По умолчанию выключен."""

    async def asyncSetUp(self):
        await _seed()
        web_app._check_login_rate_limit = lambda ip: True
        self._mock_bot = MagicMock()
        self._mock_bot.get_chat_administrators = AsyncMock(return_value=[])
        self.client = TestClient(web_app.create_app(bot=self._mock_bot))
        self.client.post("/login", data={
            "username": "su", "password": "test_password_123",
        }, follow_redirects=False)

    async def asyncTearDown(self):
        try:
            engine.sync_engine.dispose()
        except Exception:
            pass
        if os.path.exists(_DB_PATH):
            os.remove(_DB_PATH)

    @staticmethod
    async def _flag():
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == _CHAT_ID)
            )).scalar_one()
            return cs.delete_channel_messages

    async def test_defaults_to_off(self):
        self.assertFalse(await self._flag())

    async def test_toggle_turns_it_on_and_back_off(self):
        # Тумблеры ходят одним роутом /toggle с полем field — как CAS/LINK/VIA.
        url = f"/admin/chats/{_CHAT_ID}/toggle"
        r = self.client.post(url, data={"field": "channel_filter"},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertTrue(await self._flag())

        self.client.post(url, data={"field": "channel_filter"},
                         follow_redirects=False)
        self.assertFalse(await self._flag())

    async def test_chats_page_renders_toggle(self):
        r = self.client.get("/admin/chats", follow_redirects=False)
        self.assertEqual(r.status_code, 200)
        self.assertIn('value="channel_filter"', r.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
