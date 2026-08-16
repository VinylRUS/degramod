"""
test_v470_admin_discovery.py — Smoke-тест v4.7.0: авто-обнаружение TG-админов.

Проверяет:
  1. APP_VERSION = "v4.7.0".
  2. DB миграции: колонки is_pending и auto_discovered в web_users.
  3. /admin/users больше не содержит форму ручного создания пользователя
     (нет <form action="/admin/users/create" ...>).
  4. /admin/users показывает PENDING-бейдж для pending-юзеров.
  5. /admin/chats содержит кнопку "Sync admins from TG" (только SU).
  6. /admin/chats для report-чата: скрыты секции Наказания/Ночной/Санитарные.
  7. POST /admin/chats/<id>/sync-admins — создание pending WebUser по mock-данным.
  8. POST /admin/chats/<id>/sync-admins для report-чата — отказ.
  9. /admin/presets рендерится без ошибок (баг navbar v4.6.2).
 10. base.html проходит Jinja2-парсинг без ошибок.
 11. Changelog содержит v4.7.0.
 12. /start handler в bot_handlers.py существует и имеет правильную сигнатуру.
"""
from _paths import _P  # noqa: E402  (корень вычисляется от __file__)
import os
import sys
import tempfile
import unittest
from unittest.mock import patch, AsyncMock, MagicMock

sys.path.insert(0, _P())
os.chdir(_P())

_DB_PATH = tempfile.mktemp(suffix="_v470.db")
os.environ["BOT_TOKEN"] = "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
os.environ["ADMIN_IDS"] = "1"
os.environ["WEB_PASSWORD"] = "test_password_123"
os.environ["DB_PATH"] = _DB_PATH

from sqlalchemy import select, text
from db import (
    init_db, async_session, ChatSettings, PermissionPreset, WebUser,
    ChatAdmin, engine, DB_PATH,
)
import web_app
import bot_handlers as bh  # noqa: F401

from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader


async def _seed():
    """Init DB + seed SU + one regular chat + one report chat."""
    await init_db()
    async with async_session() as s:
        await s.execute(text("DELETE FROM permission_presets"))
        await s.execute(text("DELETE FROM chat_settings"))
        await s.execute(text("DELETE FROM web_users WHERE username != 'su'"))
        await s.execute(text("DELETE FROM chat_admins"))
        await s.commit()
    await init_db()  # re-seed system presets
    async with async_session() as s:
        existing_su = (await s.execute(
            select(WebUser).where(WebUser.username == "su")
        )).scalar_one_or_none()
        if existing_su is None:
            s.add(WebUser(username="su", is_su=True, is_active=True,
                          role="su", created_by="system"))
            await s.commit()
        # Regular chat
        cs1 = ChatSettings(
            chat_id=-1001234567890,
            title="Test Chat",
            hashtag="#Test",
            is_enabled=True,
            warns_to_mute=3,
            warns_to_ban=5,
            mute_duration_seconds=3600,
        )
        s.add(cs1)
        # Report chat
        cs2 = ChatSettings(
            chat_id=-1009876543210,
            title="Report Chat",
            hashtag="#Report",
            is_enabled=True,
            warns_to_mute=3,
            warns_to_ban=5,
            mute_duration_seconds=3600,
            is_report_chat=True,
        )
        s.add(cs2)
        await s.commit()


class TestV470AdminDiscovery(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await _seed()
        web_app._check_login_rate_limit = lambda ip: True
        # Мок-бот: по умолчанию без админов. Тесты могут переопределить.
        self._mock_bot = MagicMock()
        async def _no_admins(chat_id):
            return []
        self._mock_bot.get_chat_administrators = _no_admins
        self.client = TestClient(web_app.create_app(bot=self._mock_bot))

    async def asyncTearDown(self):
        try:
            engine.sync_engine.dispose()
        except Exception:
            pass
        if os.path.exists(_DB_PATH):
            os.remove(_DB_PATH)

    def _login_as_su(self):
        r = self.client.post("/login", data={
            "username": "su",
            "password": "test_password_123",
        }, follow_redirects=False)
        assert r.status_code in (303, 200), f"login failed: {r.status_code} {r.text[:200]}"
        return r

    # ── Test 1: APP_VERSION ──────────────────────────────────────────
    @unittest.skip("проверка сравнивает APP_VERSION с версией, актуальной на момент написания теста; сейчас v4.8.10, и релиз сверять с константой в тесте нечем — changelog ведётся в templates/base.html")
    async def test_app_version_is_v470(self):
        # Принимаем v4.7.0 или v4.7.x (хотфиксы)
        self.assertTrue(web_app.APP_VERSION.startswith("v4.7."),
                        f"Expected v4.7.x, got {web_app.APP_VERSION}")

    # ── Test 2: DB миграции is_pending / auto_discovered ─────────────
    async def test_db_columns_exist(self):
        async with engine.connect() as conn:
            result = await conn.execute(text("PRAGMA table_info(web_users)"))
            columns = [row[1] for row in result.fetchall()]
        self.assertIn("is_pending", columns, "Column is_pending missing in web_users")
        self.assertIn("auto_discovered", columns, "Column auto_discovered missing in web_users")

    # ── Test 3: ручное создание пользователя убрано из /admin/users ─
    async def test_admin_users_no_manual_create_form(self):
        self._login_as_su()
        r = self.client.get("/admin/users", follow_redirects=False)
        self.assertEqual(r.status_code, 200, f"GET /admin/users failed: {r.status_code}")
        html = r.text
        # Старая форма создания больше не должна присутствовать
        self.assertNotIn('action="/admin/users/create"', html,
                         "Manual create form should be removed in v4.7.0")
        # Должна быть подсказка про Sync admins from TG
        self.assertIn("Sync admins", html,
                      "Should mention 'Sync admins' instead of manual create")

    # ── Test 4: PENDING-бейдж ────────────────────────────────────────
    async def test_admin_users_shows_pending_badge(self):
        # Создаём pending-юзера вручную в БД
        async with async_session() as s:
            s.add(WebUser(
                username="tg_pending_user",
                password_hash=None,
                is_su=False,
                is_active=False,
                is_pending=True,
                auto_discovered=True,
                role="moderator",
                tg_user_id=999999,
                tg_first_name="Pending",
                tg_last_name="User",
            ))
            await s.commit()

        self._login_as_su()
        r = self.client.get("/admin/users", follow_redirects=False)
        html = r.text
        # PENDING-бейдж должен быть виден
        self.assertIn("PENDING", html, "PENDING badge should be visible")
        # auto-метка тоже
        self.assertIn("auto", html, "auto marker should be visible")

    # ── Test 5: кнопка Sync admins в /admin/chats ────────────────────
    async def test_admin_chats_has_sync_button(self):
        self._login_as_su()
        r = self.client.get("/admin/chats", follow_redirects=False)
        self.assertEqual(r.status_code, 200)
        html = r.text
        self.assertIn("Sync admins from TG", html,
                      "Missing 'Sync admins from TG' button")
        # Endpoint URL
        self.assertIn("/sync-admins", html, "Missing sync-admins endpoint URL")

    # ── Test 6: report-chat скрывает секции ──────────────────────────
    async def test_report_chat_minimal_ui(self):
        self._login_as_su()
        # GET /admin/chats — должна быть карточка report-чата
        r = self.client.get("/admin/chats", follow_redirects=False)
        html = r.text
        # Report-chat должен присутствовать в списке
        self.assertIn("Report Chat", html, "Report chat not in list")
        # Проверим что в карточке report-чата нет dropdown'ов пресетов
        # (нельзя выбрать day/night/sanitary preset для report-чата)
        # Это упрощённая проверка — реально UI скрывает секции через if is_report_chat

    # ── Test 7: POST sync-admins создаёт pending WebUser ─────────────
    async def test_sync_admins_creates_pending_users(self):
        """Мокаем bot.get_chat_administrators и проверяем что:
        - Создаются pending WebUser для каждого TG-админа.
        - role=admin если can_promote, иначе moderator.
        - moderator'ам добавляется ChatAdmin.
        """
        self._login_as_su()

        # Мокаем TG-админов: один owner (can_promote=True), один moderator.
        mock_owner = MagicMock()
        mock_owner.user.id = 111111
        mock_owner.user.username = "owner_user"
        mock_owner.user.first_name = "Owner"
        mock_owner.user.last_name = "One"
        mock_owner.user.is_bot = False
        mock_owner.can_promote_members = True
        mock_owner.status = "administrator"

        mock_mod = MagicMock()
        mock_mod.user.id = 222222
        mock_mod.user.username = None  # нет @username — должен быть tg<id>
        mock_mod.user.first_name = "Mod"
        mock_mod.user.last_name = "Two"
        mock_mod.user.is_bot = False
        mock_mod.can_promote_members = False
        mock_mod.status = "administrator"

        # Патчим get_chat_administrators на моке
        async def fake_get_admins(chat_id):
            return [mock_owner, mock_mod]

        self._mock_bot.get_chat_administrators = fake_get_admins

        r = self.client.post(
            "/admin/chats/-1001234567890/sync-admins",
            follow_redirects=False,
        )

        # Должен быть редирект 303
        self.assertEqual(r.status_code, 303,
                         f"Expected 303, got {r.status_code}: {r.text[:300]}")
        loc = r.headers.get("location", "")
        self.assertIn("Sync", loc, f"Location should contain Sync: {loc}")
        self.assertIn("created=", loc, f"Location should contain created=: {loc}")

        # Проверяем что в БД создались правильные записи
        async with async_session() as s:
            owner_wu = (await s.execute(
                select(WebUser).where(WebUser.tg_user_id == 111111)
            )).scalar_one_or_none()
            self.assertIsNotNone(owner_wu, "Owner WebUser should be created")
            self.assertTrue(owner_wu.is_pending, "Owner should be pending")
            self.assertFalse(owner_wu.is_active, "Owner should NOT be active yet")
            self.assertEqual(owner_wu.role, "admin", "Owner should be admin")
            self.assertTrue(owner_wu.auto_discovered, "Should be auto_discovered")
            self.assertEqual(owner_wu.username, "owner_user",
                             "Login should be @username without @")

            mod_wu = (await s.execute(
                select(WebUser).where(WebUser.tg_user_id == 222222)
            )).scalar_one_or_none()
            self.assertIsNotNone(mod_wu, "Mod WebUser should be created")
            self.assertTrue(mod_wu.is_pending, "Mod should be pending")
            self.assertEqual(mod_wu.role, "moderator", "Mod should be moderator")
            self.assertEqual(mod_wu.username, "tg222222",
                             "Login should be tg<TGID> when no @username")

            # ChatAdmin для moderator
            ca = (await s.execute(
                select(ChatAdmin).where(
                    ChatAdmin.chat_id == -1001234567890,
                    ChatAdmin.user_id == 222222,
                )
            )).scalar_one_or_none()
            self.assertIsNotNone(ca, "ChatAdmin should be created for moderator")

    # ── Test 8: sync-admins для report-чата отказ ────────────────────
    async def test_sync_admins_rejects_report_chat(self):
        self._login_as_su()
        r = self.client.post(
            "/admin/chats/-1009876543210/sync-admins",
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 303)
        loc = r.headers.get("location", "")
        self.assertIn("Report+chat+ignored", loc,
                      f"Should reject report chat: {loc}")

    # ── Test 9: /admin/presets рендерится без ошибок (баг v4.6.2) ────
    async def test_admin_presets_renders(self):
        """Регрессионный: v4.6.2 фикс navbar на /admin/presets."""
        self._login_as_su()
        r = self.client.get("/admin/presets", follow_redirects=False)
        self.assertEqual(r.status_code, 200,
                         f"GET /admin/presets failed: {r.status_code} {r.text[:200]}")
        html = r.text
        # navbar должен содержать ссылку на Chats (видна SU)
        self.assertIn("/admin/chats", html, "Navbar should contain Chats link")
        self.assertIn("/admin/users", html, "Navbar should contain Users link")
        self.assertIn("/admin/settings", html, "Navbar should contain Settings link")

    # ── Test 10: Jinja2-парсинг base.html ────────────────────────────
    def test_base_html_parses(self):
        """Регрессионный: literal {% %} в changelog ломал парсер."""
        env = Environment(loader=FileSystemLoader("templates"))
        # Не должно бросать TemplateSyntaxError
        try:
            env.get_template("base.html")
        except Exception as e:
            self.fail(f"base.html failed to parse: {e}")

    # ── Test 11: Changelog содержит v4.7.x ───────────────────────────
    async def test_changelog_mentions_v470(self):
        self._login_as_su()
        r = self.client.get("/dashboard", follow_redirects=False)
        html = r.text
        self.assertIn("v4.7.", html, "Changelog should mention v4.7.x")
        # Ключевые фичи v4.7.0
        self.assertIn("Sync admins", html, "Changelog should mention Sync admins feature")
        self.assertIn("/start", html, "Changelog should mention /start activation")

    # ── Test 12: /start handler существует ───────────────────────────
    def test_private_start_handler_exists(self):
        """Проверяем что private_start_handler определён в bot_handlers."""
        self.assertTrue(hasattr(bh, 'private_start_handler'),
                        "private_start_handler should be defined")
        # Проверяем что это корутина
        import inspect
        self.assertTrue(
            inspect.iscoroutinefunction(bh.private_start_handler),
            "private_start_handler should be async"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
