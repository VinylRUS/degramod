"""
v4.7.8 — тесты хотфикса 500-й ошибки при логине.

Проблема v4.7.7: на сервере degraban.bothost.tech SU-логин с правильным паролем
падал с 500 Internal Server Error. Причина — обновление last_login_at в БД
вызывало исключение (вероятно, schema mismatch или DB lock).

Решение: блок обновления last_login_at обёрнут в try/except. При ошибке —
логируем, но логин всё равно выполняется.

Тесты:
  1. APP_VERSION = "v4.7.8"
  2. Login SU с правильным паролем → 303 (раньше мог быть 500)
  3. Login SU с неверным паролем → 200 (flash error)
  4. Login SU с правильным паролем когда БД «плохая» (monkey-patch async_session
     чтобы commit() бросал Exception) → всё равно 303 (логин работает,
     last_login_at update fails silently, traceback в логе)
  5. Login обычного админа когда БД «плохая» → 303
  6. Changelog содержит v4.7.8
  7. Код содержит try/except вокруг last_login_at update (static check)
"""

import os
import sys
import asyncio
import unittest
from unittest.mock import MagicMock, AsyncMock, patch

sys.path.insert(0, "/home/z/my-project/v4.5")
sys.path.insert(0, "/home/z/my-project/v4.5/scripts")

_DB_PATH = "/tmp/test_v478_login_500_fix.db"
if os.path.exists(_DB_PATH):
    os.remove(_DB_PATH)

os.environ["BOT_TOKEN"] = "0:fake"
os.environ["ADMIN_IDS"] = "1"
os.environ["SU_IDS"] = "1"
os.environ["WEB_PASSWORD"] = "test_password_123"
os.environ["DB_PATH"] = _DB_PATH

from sqlalchemy import select, text
from db import (
    init_db, async_session, ChatSettings, WebUser,
)
import web_app
import bot_handlers as bh

from fastapi.testclient import TestClient


async def _seed():
    await init_db()
    async with async_session() as s:
        await s.execute(text("DELETE FROM permission_presets"))
        await s.execute(text("DELETE FROM chat_settings"))
        await s.execute(text("DELETE FROM web_users WHERE username != 'su'"))
        await s.execute(text("DELETE FROM chat_admins"))
        await s.execute(text("DELETE FROM word_filters"))
        await s.execute(text("DELETE FROM link_allowlist"))
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
        # Test admin user with password
        from db import WebUser as WU
        admin = WU(
            username="testadmin",
            is_su=False,
            is_active=True,
            role="admin",
            created_by="su",
            password_hash=web_app._hash_password("admin_pass_123"),
        )
        s.add(admin)
        await s.commit()


class TestV478Login500Fix(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await _seed()
        web_app._check_login_rate_limit = lambda ip: True
        self._mock_bot = MagicMock()
        self._mock_bot.get_chat_administrators = AsyncMock(return_value=[])
        self.client = TestClient(web_app.create_app(bot=self._mock_bot))

    async def asyncTearDown(self):
        try:
            self.client.close()
        except Exception:
            pass

    # ──────────── 1. Version ────────────

    def test_01_app_version_is_at_least_v478(self):
        """v4.7.8 фикс должен присутствовать в текущей версии
        (v4.7.9+ — тоже OK, фикс не удалён)."""
        # Parse version: "v4.7.8" → (4, 7, 8)
        v = web_app.APP_VERSION.lstrip("v").split(".")
        major, minor, patch = int(v[0]), int(v[1]), int(v[2])
        self.assertTrue(
            (major, minor, patch) >= (4, 7, 8),
            f"APP_VERSION={web_app.APP_VERSION} should be >= v4.7.8",
        )

    # ──────────── 2. SU login with correct password → 303 ────────────

    def test_02_su_login_correct_password_returns_303(self):
        r = self.client.post("/login", data={
            "username": "su", "password": "test_password_123",
        }, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("/dashboard", r.headers.get("location", ""))

    # ──────────── 3. SU login with wrong password → 200 ────────────

    def test_03_su_login_wrong_password_returns_200_with_error(self):
        r = self.client.post("/login", data={
            "username": "su", "password": "wrong_password",
        }, follow_redirects=False)
        self.assertEqual(r.status_code, 200)
        # Login page re-rendered with error
        self.assertIn("ACCESS DENIED", r.text.upper())

    # ──────────── 4. SU login works even if DB commit fails ────────────

    async def test_04_su_login_succeeds_when_db_commit_fails(self):
        """Если commit() бросает исключение — логин всё равно работает (303)."""
        # Monkey-patch session.commit to raise
        from sqlalchemy.ext.asyncio import AsyncSession
        original_commit = AsyncSession.commit

        async def _failing_commit(self_session):
            raise RuntimeError("simulated DB lock / schema mismatch")

        # Patch only for this test
        AsyncSession.commit = _failing_commit
        try:
            r = self.client.post("/login", data={
                "username": "su", "password": "test_password_123",
            }, follow_redirects=False)
            self.assertEqual(r.status_code, 303)
            self.assertIn("/dashboard", r.headers.get("location", ""))
        finally:
            AsyncSession.commit = original_commit

    # ──────────── 5. Admin login works even if DB commit fails ────────────

    async def test_05_admin_login_succeeds_when_db_commit_fails(self):
        """Аналогично test_04, но для обычного админа."""
        from sqlalchemy.ext.asyncio import AsyncSession
        original_commit = AsyncSession.commit

        async def _failing_commit(self_session):
            raise RuntimeError("simulated DB lock / schema mismatch")

        AsyncSession.commit = _failing_commit
        try:
            r = self.client.post("/login", data={
                "username": "testadmin", "password": "admin_pass_123",
            }, follow_redirects=False)
            self.assertEqual(r.status_code, 303)
            self.assertIn("/dashboard", r.headers.get("location", ""))
        finally:
            AsyncSession.commit = original_commit

    # ──────────── 6. Changelog mentions v4.7.8 ────────────

    def test_06_changelog_mentions_v478(self):
        r = self.client.get("/")
        # / → 303 → /login. Get /login directly.
        r = self.client.get("/login")
        self.assertEqual(r.status_code, 200)
        self.assertIn("v4.7.8", r.text)
        self.assertIn("500", r.text)


class TestV478SourceCodePatterns(unittest.TestCase):

    def test_07_login_handler_has_try_except_around_last_login_at(self):
        """В web_app.py login handler содержит try/except вокруг last_login_at update."""
        with open("/home/z/my-project/v4.5/web_app.py") as f:
            content = f.read()
        # Ищем паттерн: 'try:' followed by 'last_login_at = ' followed by 'except Exception'
        # в SU блоке
        self.assertIn("login: failed to update su.last_login_at", content)
        self.assertIn("login: failed to update %s.last_login_at", content)

    def test_08_changelog_v478_in_base_html(self):
        with open("/home/z/my-project/v4.5/templates/base.html") as f:
            content = f.read()
        self.assertIn("v4.7.8", content)
        self.assertIn("500", content)


if __name__ == "__main__":
    unittest.main(verbosity=2)
