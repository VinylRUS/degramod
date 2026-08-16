"""
test_v481_web_unban.py — behavioral-тесты для /admin/bans и /api/unban.

Покрывает:
  1. /admin/bans — GET: доступ moderator, redirect для unauth.
  2. /admin/bans — рендерит список активных банов (с фильтром и поиском).
  3. /api/unban — POST: разбан работает, создаёт unban-запись, помечает
     исходный бан как is_revoked=True.
  4. /api/unban — error cases: ban not found, ban already revoked.
  5. /api/unban — moderator без tg_user_id получает 400.
"""
import os
import sys
import unittest
import asyncio
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJ_ROOT = os.path.dirname(_HERE)
if _PROJ_ROOT not in sys.path:
    sys.path.insert(0, _PROJ_ROOT)

os.environ.setdefault("BOT_TOKEN", "123456789:AAEhBP0av28zZc-WSxGyJzkvJm5abc1234")
os.environ.setdefault("WEB_PASSWORD", "test-su-password")
os.environ.setdefault("SESSION_SECRET", "test-session-secret-for-unban-tests-v481")

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.pool import StaticPool
from sqlalchemy import text as sql_text

from fastapi.testclient import TestClient

from db import Base, User, Moderator, Punishment, ChatSettings, WebUser, _hash_password
import web_app
import db as _db


class _BaseWebUnbanTest(unittest.TestCase):
    """Базовый класс: поднимает in-memory SQLite + seed."""

    @classmethod
    def setUpClass(cls):
        cls._web_app = web_app
        cls._db = _db
        cls._app = web_app.create_app()

    def setUp(self):
        self.engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
            echo=False,
        )
        self.AsyncSessionLocal = async_sessionmaker(self.engine, expire_on_commit=False)

        # Патчим db.async_session
        patcher = patch.object(self._db, "async_session", self.AsyncSessionLocal)
        patcher.start()
        self.addCleanup(patcher.stop)

        # Патчим web_app.async_session (импортированный символ)
        web_app_patcher = patch.object(self._web_app, "async_session", self.AsyncSessionLocal)
        web_app_patcher.start()
        self.addCleanup(web_app_patcher.stop)

        # Создаём схему
        async def _init():
            async with self.engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
        asyncio.run(_init())

        # Seed: 2 чата, 1 нарушитель, 1 модератор, 3 бана (2 active + 1 revoked), 2 веб-юзера
        async def _seed():
            async with self.AsyncSessionLocal() as s:
                s.add(ChatSettings(chat_id=-1001234, hashtag="#test", title="Test chat"))
                s.add(ChatSettings(chat_id=-1005678, hashtag="#test2", title="Second chat"))
                s.add(User(user_id=999001, username="badguy", first_name="Bad", last_name="Guy"))
                s.add(Moderator(mod_id=999002, username="mod", first_name="Mod"))
                s.add(Punishment(
                    id=1, user_id=999001, mod_id=999002, chat_id=-1001234,
                    action_type="ban", duration_seconds=None,
                    reason="Test ban", message_text="bad message",
                    is_revoked=False,
                ))
                s.add(Punishment(
                    id=2, user_id=999001, mod_id=999002, chat_id=-1005678,
                    action_type="ban", duration_seconds=None,
                    reason="Another ban", message_text="another bad message",
                    is_revoked=False,
                ))
                s.add(Punishment(
                    id=3, user_id=999001, mod_id=999002, chat_id=-1001234,
                    action_type="ban", duration_seconds=None,
                    reason="Already unbanned", message_text="old",
                    is_revoked=True,
                ))
                s.add(WebUser(
                    username="moderator1",
                    password_hash=_hash_password("mod-pass"),
                    role="moderator", is_active=True,
                    tg_user_id=999099,
                ))
                s.add(WebUser(
                    username="moderator_no_tg",
                    password_hash=_hash_password("mod-pass"),
                    role="moderator", is_active=True,
                    tg_user_id=None,
                ))
                await s.commit()
        asyncio.run(_seed())

    def tearDown(self):
        asyncio.run(self.engine.dispose())

    def _make_token(self, username: str, role: str = "moderator") -> str:
        return self._web_app._make_token(username, is_su=False, role=role)

    def _count_bans(self) -> tuple[int, int, int]:
        """Возвращает (active, revoked, unbans) из БД (async)."""
        async def _q():
            async with self.AsyncSessionLocal() as s:
                active = (await s.execute(sql_text(
                    "SELECT COUNT(*) FROM punishments WHERE action_type='ban' AND is_revoked=0"
                ))).scalar()
                revoked = (await s.execute(sql_text(
                    "SELECT COUNT(*) FROM punishments WHERE action_type='ban' AND is_revoked=1"
                ))).scalar()
                unbans = (await s.execute(sql_text(
                    "SELECT COUNT(*) FROM punishments WHERE action_type='unban'"
                ))).scalar()
            return active, revoked, unbans
        return asyncio.run(_q())


class TestAdminBansPageAccess(_BaseWebUnbanTest):
    """Доступ к /admin/bans — moderator может, unauth redirect."""

    def test_01_unauth_redirects_to_login(self):
        """Без логина → redirect на /login."""
        client = TestClient(self._app)
        r = client.get("/admin/bans", follow_redirects=False)
        self.assertIn(r.status_code, (303, 302, 307))
        self.assertIn("/login", r.headers.get("location", ""))

    def test_02_moderator_can_access(self):
        """Moderator (role='moderator') может открыть /admin/bans."""
        client = TestClient(self._app)
        token = self._make_token("moderator1", role="moderator")
        r = client.get("/admin/bans",
                       cookies={self._web_app.COOKIE_NAME: token},
                       follow_redirects=False)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Active bans", r.text)


class TestAdminBansPageRendering(_BaseWebUnbanTest):
    """Рендеринг /admin/bans — список активных банов."""

    def _get_authed(self, path: str = "/admin/bans"):
        client = TestClient(self._app)
        token = self._make_token("moderator1", role="moderator")
        return client.get(path,
                          cookies={self._web_app.COOKIE_NAME: token},
                          follow_redirects=False)

    def test_10_page_shows_active_bans(self):
        """Страница показывает активные баны (id=1, id=2) — но не id=3 (revoked)."""
        r = self._get_authed()
        self.assertEqual(r.status_code, 200)
        self.assertIn("Test ban", r.text)
        self.assertIn("Another ban", r.text)
        self.assertNotIn("Already unbanned", r.text)

    def test_11_page_shows_usernames(self):
        """В таблице видны имена нарушителей."""
        r = self._get_authed()
        self.assertEqual(r.status_code, 200)
        self.assertIn("badguy", r.text.lower())
        self.assertIn("Bad", r.text)

    def test_12_page_shows_chat_titles(self):
        """В таблице видны названия чатов."""
        r = self._get_authed()
        self.assertEqual(r.status_code, 200)
        self.assertIn("Test chat", r.text)
        self.assertIn("Second chat", r.text)

    def test_13_page_has_unban_button(self):
        """На странице есть кнопка Unban."""
        r = self._get_authed()
        self.assertEqual(r.status_code, 200)
        self.assertIn("Unban", r.text)
        self.assertIn("/api/unban", r.text)

    def test_14_filter_by_chat(self):
        """Фильтр по чату работает — только баны выбранного чата."""
        r = self._get_authed("/admin/bans?chat_filter=-1001234")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Test ban", r.text)
        self.assertNotIn("Another ban", r.text)

    def test_15_search_by_username(self):
        """Поиск по никнейму работает."""
        r = self._get_authed("/admin/bans?search=badguy")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Test ban", r.text)
        self.assertIn("Another ban", r.text)

    def test_16_search_no_match(self):
        """Поиск без совпадений — пустой список."""
        r = self._get_authed("/admin/bans?search=nonexistent_user_xyz")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("Test ban", r.text)
        self.assertNotIn("Another ban", r.text)


class TestApiUnban(_BaseWebUnbanTest):
    """POST /api/unban — разбан через API."""

    def _post_authed(self, data: dict, username: str = "moderator1", role: str = "moderator"):
        client = TestClient(self._app)
        token = self._make_token(username, role=role)
        return client.post("/api/unban",
                           cookies={self._web_app.COOKIE_NAME: token},
                           data=data, follow_redirects=False)

    def test_20_unban_success(self):
        """Успешный разбан — исходный бан помечен is_revoked, создан unban."""
        active_before, revoked_before, unbans_before = self._count_bans()
        r = self._post_authed({"punishment_id": "1", "reason": "test unban"})
        self.assertEqual(r.status_code, 200, f"Got: {r.status_code} {r.text}")
        body = r.json()
        self.assertTrue(body["ok"], f"Expected ok=True, got: {body}")
        active_after, revoked_after, unbans_after = self._count_bans()
        self.assertEqual(active_after, active_before - 1)
        self.assertEqual(revoked_after, revoked_before + 1)
        self.assertEqual(unbans_after, unbans_before + 1)

    def test_21_unban_with_empty_reason(self):
        """Разбан с пустой reason — работает (reason optional)."""
        r = self._post_authed({"punishment_id": "2", "reason": ""})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])

    def test_22_unban_nonexistent_punishment(self):
        """Разбан несуществующего punishment_id → 404."""
        r = self._post_authed({"punishment_id": "99999", "reason": ""})
        self.assertEqual(r.status_code, 404)
        body = r.json()
        self.assertFalse(body["ok"])
        self.assertIn("не найден", body["error"].lower())

    def test_23_unban_already_revoked(self):
        """Разбан уже снятого бана (id=3, is_revoked=True) → 404."""
        r = self._post_authed({"punishment_id": "3", "reason": ""})
        self.assertEqual(r.status_code, 404)
        body = r.json()
        self.assertFalse(body["ok"])

    def test_24_unban_records_mod_id(self):
        """Разбан записывает mod_id (tg_user_id веб-юзера)."""
        r = self._post_authed({"punishment_id": "1", "reason": "test mod_id"})
        self.assertEqual(r.status_code, 200)

        async def _q():
            async with self.AsyncSessionLocal() as s:
                row = (await s.execute(sql_text(
                    "SELECT mod_id FROM punishments WHERE action_type='unban' ORDER BY id DESC LIMIT 1"
                ))).fetchone()
            return row
        row = asyncio.run(_q())
        self.assertIsNotNone(row)
        self.assertEqual(row[0], 999099, "mod_id должен быть tg_user_id веб-юзера")


class TestApiUnbanRequiresTgUserId(_BaseWebUnbanTest):
    """Разбан от веб-юзера без tg_user_id → 400."""

    def test_30_unban_without_tg_user_id_returns_400(self):
        """Веб-юзер без tg_user_id не может разбанивать."""
        client = TestClient(self._app)
        token = self._make_token("moderator_no_tg", role="moderator")
        r = client.post("/api/unban",
                        cookies={self._web_app.COOKIE_NAME: token},
                        data={"punishment_id": "1", "reason": ""},
                        follow_redirects=False)
        self.assertEqual(r.status_code, 400)
        body = r.json()
        self.assertFalse(body["ok"])
        self.assertIn("TG user ID", body["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
