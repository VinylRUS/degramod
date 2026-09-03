"""v5.5.0 — страница «На карандаше» (/admin/cas): рендер и ручные действия.

Главное, что здесь сторожится, — инвариант v4.8.11 (CLAUDE.md, «Разбан из
веб-панели требует привязанного Telegram»): mod_id действия берётся из
привязанного TG-аккаунта, у встроенного su это служебный _SU_WEB_MOD_ID
(-1), а веб-юзер без привязки получает отказ. Исходный код v5.5.0 писал
`tg_user_id or 0` — тот самый паттерн, из-за которого до v4.8.11
заводился несуществующий модератор и на него вешались все записи.

Запуск: uv run python tools/run_tests.py -k v550_cas_web
"""
from _paths import _P  # noqa: E402
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import unquote

sys.path.insert(0, _P())
os.chdir(_P())

_DB_PATH = tempfile.mktemp(suffix="_v550_casweb.db")
os.environ["BOT_TOKEN"] = "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
os.environ["ADMIN_IDS"] = "1"
os.environ["WEB_PASSWORD"] = "test_password_123"
os.environ["DB_PATH"] = _DB_PATH

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select, text  # noqa: E402

import bot_handlers as bh  # noqa: E402,F401 — чтобы aiogram router загрузился
import web_app  # noqa: E402
from db import (  # noqa: E402
    CasIgnore,
    CasSettings,
    CasVerdict,
    ChatMemberSeen,
    Moderator,
    Punishment,
    User,
    WebUser,
    async_session,
    engine,
    init_db,
)
from web.deps import require_csrf_admin  # noqa: E402

_CHAT_ID = -1005500000001
_WATCH_UID = 5501


async def _seed():
    await init_db()
    async with async_session() as s:
        for t in ("cas_verdicts", "cas_ignore", "chat_members_seen",
                  "punishments", "moderators", "users", "cas_settings"):
            await s.execute(text(f"DELETE FROM {t}"))
        await s.execute(text("DELETE FROM web_users WHERE username != 'su'"))
        await s.commit()
    async with async_session() as s:
        existing_su = (await s.execute(
            select(WebUser).where(WebUser.username == "su")
        )).scalar_one_or_none()
        if existing_su is None:
            s.add(WebUser(username="su", is_su=True, is_active=True,
                          role="su", created_by="system"))
        s.add(CasVerdict(user_id=_WATCH_UID, source="lols", is_banned=False,
                         reason="potential (C1_ban): spam_factor=88",
                         spam_factor=88.0, offenses=3, scammer=True,
                         tier="C1_ban"))
        s.add(User(user_id=_WATCH_UID, username="scam_guy"))
        s.add(ChatMemberSeen(chat_id=_CHAT_ID, user_id=_WATCH_UID))
        await s.commit()


class CasWebTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await _seed()
        web_app._check_login_rate_limit = lambda ip: True
        self._mock_bot = MagicMock()
        self._mock_bot.ban_chat_member = AsyncMock(return_value=True)
        self.app = web_app.create_app(bot=self._mock_bot)
        self.client = TestClient(self.app)
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

    async def test_page_renders_watch_row(self):
        r = self.client.get("/admin/cas", follow_redirects=False)
        self.assertEqual(r.status_code, 200)
        self.assertIn("На карандаше", r.text)
        self.assertIn("C1_ban", r.text)
        self.assertIn(str(_WATCH_UID), r.text)
        self.assertIn(str(_CHAT_ID), r.text, "чат присутствия должен быть виден")

    async def test_nav_link_is_not_duplicated(self):
        """v5.5.0 FIX: пункт CAS в шапке был вставлен трижды."""
        r = self.client.get("/admin/cas", follow_redirects=False)
        self.assertEqual(r.text.count('href="/admin/cas" class="nav-link'), 1)

    async def test_thresholds_saved_and_order_kept(self):
        r = self.client.post("/admin/cas/thresholds", data={
            "spamfactor_ban": "40", "spamfactor_mute": "80",
            "offenses_mute": "7",
        }, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        async with async_session() as s:
            cfg = (await s.execute(
                select(CasSettings).where(CasSettings.id == 1)
            )).scalar_one()
        self.assertEqual(cfg.spamfactor_ban, 40.0)
        self.assertEqual(cfg.spamfactor_mute, 40.0,
                         "mute-порог не должен превышать ban-порог")
        self.assertEqual(cfg.offenses_mute, 7)
        self.assertEqual(cfg.updated_by, "su")

    async def test_ban_writes_su_service_mod_id(self):
        """su без привязки → mod_id = -1 (_SU_WEB_MOD_ID), не 0."""
        r = self.client.post("/admin/cas/action", data={
            "action": "ban", "user_id": str(_WATCH_UID),
        }, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self._mock_bot.ban_chat_member.assert_awaited()
        async with async_session() as s:
            p = (await s.execute(select(Punishment))).scalars().all()
            mods = (await s.execute(select(Moderator))).scalars().all()
        self.assertEqual(len(p), 1)
        self.assertEqual(p[0].mod_id, web_app._SU_WEB_MOD_ID)
        self.assertEqual(p[0].chat_id, _CHAT_ID)
        self.assertIn("su", p[0].reason or "",
                      "автор действия обязан остаться в тексте причины")
        self.assertEqual([m.mod_id for m in mods], [web_app._SU_WEB_MOD_ID])
        self.assertNotIn(0, [m.mod_id for m in mods],
                         "модератор 0 — тот самый фантом из v4.8.11")

    async def test_ignore_adds_cas_ignore(self):
        r = self.client.post("/admin/cas/action", data={
            "action": "ignore", "user_id": str(_WATCH_UID),
        }, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        async with async_session() as s:
            row = (await s.execute(select(CasIgnore))).scalar_one()
        self.assertEqual(row.user_id, _WATCH_UID)
        # Юзер уходит со страницы: фильтр по cas_ignore.
        page = self.client.get("/admin/cas", follow_redirects=False)
        self.assertNotIn(f"<code>{_WATCH_UID}</code>", page.text)

    async def test_unlinked_web_user_is_refused(self):
        """Не-su без tg_user_id → отказ, а не бан от имени модератора 0."""
        class _FakeAuth:
            username = "moderator_without_tg"
            role = "admin"
            tg_user_id = None

        self.app.dependency_overrides[require_csrf_admin] = lambda: _FakeAuth()
        try:
            r = self.client.post("/admin/cas/action", data={
                "action": "ban", "user_id": str(_WATCH_UID),
            }, follow_redirects=False)
        finally:
            self.app.dependency_overrides.pop(require_csrf_admin, None)

        self.assertEqual(r.status_code, 303)
        self.assertIn("не привязана", unquote(r.headers["location"]),
                      "ожидался flash про непривязанную учётку")
        self._mock_bot.ban_chat_member.assert_not_awaited()
        async with async_session() as s:
            punishments = (await s.execute(select(Punishment))).scalars().all()
            mods = (await s.execute(select(Moderator))).scalars().all()
        self.assertEqual(punishments, [])
        self.assertEqual(mods, [], "фантомный модератор заводиться не должен")


if __name__ == "__main__":
    unittest.main(verbosity=2)
