"""
test_v510_whitelist_web.py — веб-управление вайтлистом ботов на /admin/presets.

Контекст: v5.1.0 добавляет на страницу /admin/presets секцию «Вайтлист ботов»
— веб-эквивалент команд /botallow, /botunallow, /botallowlist (Task 7).
Модель `db.BotWhitelist` уже существует (Task 6): chat_id=0 — global,
bot_username хранится в нижнем регистре без «@», уникальность
(chat_id, bot_username).

Секция сделана по образцу Link Allowlist на этой же странице (v4.7.5):
те же require_csrf_admin, hard delete и flash-редиректы через
/admin/presets?flash=....

Запуск: uv run python tools/run_tests.py -k v510_whitelist_web
"""
from _paths import _P  # noqa: E402  (корень вычисляется от __file__)
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, AsyncMock

sys.path.insert(0, _P())
os.chdir(_P())

_DB_PATH = tempfile.mktemp(suffix="_v510_wlweb.db")
os.environ["BOT_TOKEN"] = "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
os.environ["ADMIN_IDS"] = "1"
os.environ["WEB_PASSWORD"] = "test_password_123"
os.environ["DB_PATH"] = _DB_PATH

from sqlalchemy import select, text
from db import (
    init_db, async_session, ChatSettings, WebUser, engine, BotWhitelist,
)
import web_app
import bot_handlers as bh  # noqa: F401 — чтобы aiogram router загрузился

from fastapi.testclient import TestClient


async def _seed():
    await init_db()
    async with async_session() as s:
        await s.execute(text("DELETE FROM chat_settings"))
        await s.execute(text("DELETE FROM web_users WHERE username != 'su'"))
        await s.execute(text("DELETE FROM chat_admins"))
        await s.execute(text("DELETE FROM bot_whitelist"))
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
            chat_id=-1005100000001,
            title="Test Chat v5.1.0",
            is_enabled=True,
        ))
        await s.commit()


class TestV510BotWhitelistWeb(unittest.IsolatedAsyncioTestCase):

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

    # ── GET /admin/presets рендерит секцию ────────────────────────────
    async def test_get_presets_page_renders_bot_whitelist_section(self):
        r = self.client.get("/admin/presets", follow_redirects=False)
        self.assertEqual(r.status_code, 200)
        body = r.text
        self.assertIn("Вайтлист ботов", body)
        self.assertIn('action="/admin/presets/bots/add"', body)

    # ── POST bots/add создаёт BotWhitelist ────────────────────────────
    async def test_bots_add_creates_whitelist_entry(self):
        r = self.client.post("/admin/presets/bots/add", data={
            "chat_id_str": "0",
            "bot_username": "@GifBot",
            "note": "gif-поиск",
        }, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("Bot+whitelisted", r.headers.get("location", ""))

        async with async_session() as s:
            row = (await s.execute(
                select(BotWhitelist).where(BotWhitelist.bot_username == "gifbot")
            )).scalar_one_or_none()
            self.assertIsNotNone(row, "BotWhitelist not created")
            self.assertEqual(row.chat_id, 0)
            self.assertEqual(row.bot_username, "gifbot",
                              "username должен нормализоваться: lower + без @")
            self.assertEqual(row.note, "gif-поиск")

    # ── POST bots/add с дубликатом → не создаёт вторую запись ─────────
    async def test_bots_add_duplicate_rejected(self):
        self.client.post("/admin/presets/bots/add", data={
            "chat_id_str": "0", "bot_username": "vid", "note": "",
        }, follow_redirects=False)

        r = self.client.post("/admin/presets/bots/add", data={
            "chat_id_str": "0", "bot_username": "@vid", "note": "",
        }, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("already+whitelisted", r.headers.get("location", ""))

        async with async_session() as s:
            rows = (await s.execute(
                select(BotWhitelist).where(BotWhitelist.bot_username == "vid")
            )).scalars().all()
            self.assertEqual(len(rows), 1, "Should not create duplicate row")

    # ── POST bots/add с пустым username → понятный ответ, не 422 ──────
    async def test_bots_add_empty_username_gives_friendly_error(self):
        r = self.client.post("/admin/presets/bots/add", data={
            "chat_id_str": "0", "bot_username": "", "note": "",
        }, follow_redirects=False)
        self.assertNotEqual(
            r.status_code, 422,
            f"пустой bot_username должен обрабатываться хендлером, "
            f"а не отсекаться валидацией: {r.text[:200]}",
        )
        self.assertEqual(r.status_code, 303)
        self.assertIn("required", r.headers.get("location", ""))

        async with async_session() as s:
            rows = (await s.execute(select(BotWhitelist))).scalars().all()
            self.assertEqual(len(rows), 0, "Пустой username не должен создавать запись")

    # ── POST bots/add с невалидным chat_id ─────────────────────────────
    async def test_bots_add_invalid_chat_id_rejected(self):
        r = self.client.post("/admin/presets/bots/add", data={
            "chat_id_str": "not-a-number", "bot_username": "somebot", "note": "",
        }, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("Invalid+chat_id", r.headers.get("location", ""))

        async with async_session() as s:
            rows = (await s.execute(
                select(BotWhitelist).where(BotWhitelist.bot_username == "somebot")
            )).scalars().all()
            self.assertEqual(len(rows), 0)

    # ── Per-chat запись (chat_id != 0) ─────────────────────────────────
    async def test_bots_add_per_chat_scope(self):
        r = self.client.post("/admin/presets/bots/add", data={
            "chat_id_str": "-1005100000001", "bot_username": "chatbot", "note": "",
        }, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("Bot+whitelisted", r.headers.get("location", ""))

        async with async_session() as s:
            row = (await s.execute(
                select(BotWhitelist).where(BotWhitelist.bot_username == "chatbot")
            )).scalar_one()
            self.assertEqual(row.chat_id, -1005100000001)

    # ── POST bots/{id}/delete → hard delete ────────────────────────────
    async def test_bots_delete_hard_delete(self):
        self.client.post("/admin/presets/bots/add", data={
            "chat_id_str": "0", "bot_username": "removeme", "note": "",
        }, follow_redirects=False)
        async with async_session() as s:
            row = (await s.execute(
                select(BotWhitelist).where(BotWhitelist.bot_username == "removeme")
            )).scalar_one()
            wl_id = row.id

        r = self.client.post(
            f"/admin/presets/bots/{wl_id}/delete", follow_redirects=False,
        )
        self.assertEqual(r.status_code, 303)
        self.assertIn("Bot+removed", r.headers.get("location", ""))

        async with async_session() as s:
            row = (await s.execute(
                select(BotWhitelist).where(BotWhitelist.id == wl_id)
            )).scalar_one_or_none()
            self.assertIsNone(row, "Should be hard-deleted (no row)")

    # ── POST bots/{id}/delete несуществующей записи ─────────────────────
    async def test_bots_delete_not_found(self):
        r = self.client.post(
            "/admin/presets/bots/999999/delete", follow_redirects=False,
        )
        self.assertEqual(r.status_code, 303)
        self.assertIn("Bot+not+found", r.headers.get("location", ""))

    # ── Модератор не может добавлять/удалять (require_admin) ────────────
    async def test_bots_add_requires_admin(self):
        self.client.post("/logout", follow_redirects=False)

        async with async_session() as s:
            mod = WebUser(
                username="mod1", is_su=False, is_active=True,
                role="moderator", password_hash="$fake$hash",
                created_by="system",
            )
            s.add(mod)
            await s.commit()
        from db import _hash_password
        async with async_session() as s:
            mod = (await s.execute(
                select(WebUser).where(WebUser.username == "mod1")
            )).scalar_one()
            mod.password_hash = _hash_password("mod_pass_123")
            await s.commit()

        r = self.client.post("/login", data={
            "username": "mod1", "password": "mod_pass_123",
        }, follow_redirects=False)
        self.assertEqual(r.status_code, 303)

        r = self.client.post("/admin/presets/bots/add", data={
            "chat_id_str": "0", "bot_username": "modbot", "note": "",
        }, follow_redirects=False)
        # require_csrf_admin/require_admin редиректит модератора на /dashboard
        self.assertEqual(r.status_code, 303)
        self.assertIn("/dashboard", r.headers.get("location", ""))

        async with async_session() as s:
            rows = (await s.execute(
                select(BotWhitelist).where(BotWhitelist.bot_username == "modbot")
            )).scalars().all()
            self.assertEqual(len(rows), 0, "Модератор не должен иметь доступ")


class TestV510SourceCodePatterns(unittest.TestCase):
    """Static source inspection — дополняет поведенческие тесты выше."""

    def test_routes_exist(self):
        with open(_P("web/admin_presets.py")) as f:
            src = f.read()
        self.assertIn('"/admin/presets/bots/add"', src)
        self.assertIn('"/admin/presets/bots/{wl_id:int}/delete"', src)

    def test_uses_csrf_admin_dependency(self):
        with open(_P("web/admin_presets.py")) as f:
            src = f.read()
        body = src[src.index("async def admin_presets_bots_add("):]
        body = body[:body.index("\n@router.post", 10)]
        self.assertIn("require_csrf_admin", body,
                      "паритет с link allowlist по защите")

    def test_username_field_uses_empty_default(self):
        # Form(...) отсекает пустую строку сырым 422 — контракт v4.8.12.
        with open(_P("web/admin_presets.py")) as f:
            src = f.read()
        self.assertIn('bot_username: str = Form("")', src)

    def test_helpers_called_through_module(self):
        # Модули web/ зовут хелперы как web_app._helper(...), иначе
        # патчи в тестах перестают действовать.
        with open(_P("web/admin_presets.py")) as f:
            src = f.read()
        self.assertNotIn("from web_app import _req_logger", src)


class TestV510Template(unittest.TestCase):
    def test_section_rendered(self):
        with open(_P("templates/admin_presets.html")) as f:
            html = f.read()
        self.assertIn("/admin/presets/bots/add", html)
        self.assertIn("bot_whitelist", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
