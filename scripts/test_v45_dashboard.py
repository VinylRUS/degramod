"""
test_v45_dashboard.py — Тесты v4.5.0:

  1. Дашборд урезан: 4 stat-карточки (Total/Mutes/Warns/Bans), нет top offenders/moderators,
     нет chat-settings, нет change-pw, нет anchor-nav.
  2. Action filter: 4 кнопки (All/Mute/Warn/Ban); URL ?action=unmute по-прежнему работает.
  3. Маршрут /me (Profile): рендерится, показывает логин/роль/TG ID, форму смены пароля (или
     предупреждение для SU, инструкцию для moderator).
  4. POST /me/password: смена пароля → редирект на /me?pw_msg=...
  5. POST /me/avatar/refresh: вызывает _fetch_and_save_avatar, обновляет tg_photo_updated_at.
  6. Маршрут /admin/settings (SU-only): рендерится с Bot info, Backup, Cleanup, VACUUM.
  7. GET /admin/cleanup → редирект на /admin/settings#cleanup.
  8. POST /admin/settings/backup: создаёт backup-файл.
  9. POST /admin/settings/vacuum: VACUUM выполняется.
 10. POST /admin/cleanup: реальное удаление, редирект на /admin/settings#cleanup.
 11. Аватарки: _avatar_url возвращает None если файла нет; _fetch_and_save_avatar
     обрабатывает пустой ответ от TG (no profile photos).
 12. Навбар: содержит Profile, Settings (для SU), не содержит Cleanup.
 13. /me требует авторизации.
 14. /admin/settings требует SU.
 15. /me/avatar/refresh: если нет TG ID — редирект с pw_msg.

Все тесты используют in-memory SQLite (DB_PATH=:memory:).
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from io import BytesIO

# Подкладываем test-окружение ДО импорта модулей проекта.
os.environ.setdefault("DB_PATH", ":memory:")
os.environ.setdefault("BOT_TOKEN", "test:token")
os.environ.setdefault("WEB_PASSWORD", "test-pwd")
os.environ.setdefault("SESSION_SECRET", "test-secret-xxxxxxxxxxxxxxxxxxxxx")
os.environ.setdefault("ADMIN_IDS", "111111111")

sys.path.insert(0, "/home/z/my-project/v4.5")

from sqlalchemy import select, delete  # noqa: E402

from db import (  # noqa: E402
    async_session, init_db, WebUser, ChatSettings, Punishment, User, Moderator,
)


async def _clear_all_tables():
    """Чистит все таблицы между тестами для изоляции."""
    async with async_session() as s:
        await s.execute(delete(Punishment))
        await s.execute(delete(ChatSettings))
        await s.execute(delete(User))
        await s.execute(delete(Moderator))
        await s.execute(delete(WebUser))
        await s.commit()
    # v4.5.1: отключаем rate-limit на /login для тестов
    try:
        import web_app
        web_app._check_login_rate_limit = lambda ip: True
    except ImportError:
        pass


async def _seed_su():
    """Создаёт SU-аккаунт в БД (нужно после _clear_all_tables)."""
    async with async_session() as s:
        s.add(WebUser(username="su", is_su=True, is_active=True,
                       role="su", created_by="system"))
        await s.commit()


# ═══════════════════════════════════════════════════════════════════════════
# Тест 1: Дашборд урезан
# ═══════════════════════════════════════════════════════════════════════════
class TestDashboardSlimmed(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await init_db()
        await _clear_all_tables()
        await _seed_su()
        # Подмешиваем немного данных
        async with async_session() as s:
            s.add(User(user_id=1001, username="badguy"))
            s.add(Moderator(mod_id=999, username="admin"))
            s.add(Punishment(user_id=1001, mod_id=999, chat_id=-100,
                              action_type="warn", duration_seconds=1, reason="spam",
                              is_revoked=False))
            s.add(Punishment(user_id=1001, mod_id=999, chat_id=-100,
                              action_type="mute", duration_seconds=3600, reason="more",
                              is_revoked=False))
            s.add(Punishment(user_id=1001, mod_id=999, chat_id=-100,
                              action_type="ban", reason="bye", is_revoked=False))
            await s.commit()

        from web_app import create_app
        self.mock_bot = MagicMock()
        self.app = create_app(bot=self.mock_bot)
        from httpx import AsyncClient, ASGITransport
        self.client = AsyncClient(transport=ASGITransport(app=self.app), base_url="http://test")
        resp = await self.client.post(
            "/login", data={"username": "su", "password": "test-pwd"},
            follow_redirects=False,
        )
        assert resp.status_code == 303, f"Login failed: {resp.status_code}"

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_dashboard_has_4_stat_cards(self):
        """На дашборде ровно 4 stat-карточки: Total, Mutes, Warns, Bans."""
        resp = await self.client.get("/dashboard", follow_redirects=False)
        self.assertEqual(resp.status_code, 200)
        body = resp.text
        # 4 карточки должны быть
        self.assertIn("Total", body)
        self.assertIn("Mutes", body)
        self.assertIn("Warns", body)
        self.assertIn("Bans", body)
        # Не должно быть unmute/unwarn/unban как отдельные карточки
        self.assertNotIn("Unmutes", body)
        self.assertNotIn("Unwarns", body)
        self.assertNotIn("Unbans", body)

    async def test_dashboard_no_top_offenders(self):
        """На дашборде нет секции Top offenders."""
        resp = await self.client.get("/dashboard", follow_redirects=False)
        self.assertNotIn("Top offenders", resp.text)
        self.assertNotIn("Top moderators", resp.text)

    async def test_dashboard_no_chat_settings(self):
        """На дашборде нет таблицы chat settings."""
        resp = await self.client.get("/dashboard", follow_redirects=False)
        self.assertNotIn("Chat settings", resp.text)
        self.assertNotIn("Warns → mute", resp.text)

    async def test_dashboard_no_change_password(self):
        """На дашборде нет формы смены пароля."""
        resp = await self.client.get("/dashboard", follow_redirects=False)
        self.assertNotIn("Change my password", resp.text)
        self.assertNotIn('name="new_password"', resp.text)

    async def test_dashboard_no_anchor_nav(self):
        """На дашборде нет anchor-nav (Jump: Search/Stats/...).
        Замечание: CSS-правило .anchor-nav определено в base.html (общее),
        поэтому проверяем не строку 'anchor-nav', а реальное использование —
        div с class="anchor-nav" и подпись "Jump:"."""
        resp = await self.client.get("/dashboard", follow_redirects=False)
        # Не должно быть div с class="anchor-nav" (CSS-класс в <style> не считается)
        self.assertNotIn('class="anchor-nav"', resp.text)
        self.assertNotIn("Jump:", resp.text)

    async def test_dashboard_action_filter_has_4_tabs(self):
        """Action filter: только All/Mute/Warn/Ban (нет unmute/unwarn/unban)."""
        resp = await self.client.get("/dashboard", follow_redirects=False)
        body = resp.text
        # Должны быть 4 кнопки
        self.assertIn(">All</a>", body)
        self.assertIn(">Mute</a>", body)
        self.assertIn(">Warn</a>", body)
        self.assertIn(">Ban</a>", body)
        # Не должно быть кнопок unmute/unwarn/unban в filter-tabs
        self.assertNotIn(">Unmute</a>", body)
        self.assertNotIn(">Unwarn</a>", body)
        self.assertNotIn(">Unban</a>", body)

    async def test_dashboard_url_action_unmute_still_works(self):
        """URL ?action=unmute по-прежнему работает (для прямых ссылок)."""
        resp = await self.client.get("/dashboard?action=unmute", follow_redirects=False)
        self.assertEqual(resp.status_code, 200)


# ═══════════════════════════════════════════════════════════════════════════
# Тест 2: Маршрут /me (Profile)
# ═══════════════════════════════════════════════════════════════════════════
class TestProfileRoute(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await init_db()
        await _clear_all_tables()
        await _seed_su()
        # Обычный админ с TG ID
        async with async_session() as s:
            s.add(WebUser(
                username="admin1", is_su=False, is_active=True,
                role="admin", created_by="su",
                password_hash="salt:hash",  # фейк
                tg_user_id=123456,
                tg_first_name="Alice",
                tg_last_name="Wonderland",
                tg_username="admin1",
            ))
            await s.commit()

        from web_app import create_app
        self.mock_bot = MagicMock()
        self.app = create_app(bot=self.mock_bot)
        from httpx import AsyncClient, ASGITransport
        self.client = AsyncClient(transport=ASGITransport(app=self.app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()

    async def _login_as(self, username: str, password: str):
        resp = await self.client.post(
            "/login", data={"username": username, "password": password},
            follow_redirects=False,
        )
        assert resp.status_code == 303

    async def _login_su(self):
        await self._login_as("su", "test-pwd")

    async def _login_admin1(self, password: str = "fake"):
        """Логинимся admin1 — но пароль-фейк, поэтому сделаем прямую куку."""
        # Подменим пароль на реальный, чтобы логин прошёл
        from db import _hash_password
        async with async_session() as s:
            wu = (await s.execute(
                select(WebUser).where(WebUser.username == "admin1")
            )).scalar_one()
            wu.password_hash = _hash_password(password)
            await s.commit()
        await self._login_as("admin1", password)

    async def test_me_requires_auth(self):
        """Без авторизации /me редиректит на /login."""
        resp = await self.client.get("/me", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertIn("/login", resp.headers["location"])

    async def test_me_su_shows_env_warning(self):
        """SU видит предупреждение про WEB_PASSWORD вместо формы."""
        await self._login_su()
        resp = await self.client.get("/me", follow_redirects=False)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("WEB_PASSWORD", resp.text)
        # SU не видит форму смены пароля (его пароль в env)
        self.assertIn("environment variable", resp.text.lower())

    async def test_me_admin_shows_password_form(self):
        """admin видит форму смены пароля."""
        await self._login_admin1("secret123")
        resp = await self.client.get("/me", follow_redirects=False)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Change my password", resp.text)
        self.assertIn('name="old_password"', resp.text)
        self.assertIn('name="new_password"', resp.text)
        self.assertIn('name="confirm"', resp.text)

    async def test_me_shows_tg_id(self):
        """Страница /me показывает TG ID пользователя."""
        await self._login_admin1("secret123")
        resp = await self.client.get("/me", follow_redirects=False)
        self.assertIn("123456", resp.text)

    async def test_me_shows_role_badge(self):
        """Страница /me показывает бейдж роли."""
        await self._login_admin1("secret123")
        resp = await self.client.get("/me", follow_redirects=False)
        self.assertIn("admin", resp.text)

    async def test_me_has_refresh_avatar_button(self):
        """Страница /me имеет кнопку Refresh для аватарки."""
        await self._login_admin1("secret123")
        resp = await self.client.get("/me", follow_redirects=False)
        self.assertIn("/me/avatar/refresh", resp.text)
        self.assertIn("Refresh", resp.text)

    async def test_me_has_placeholder_when_no_avatar(self):
        """Если аватарки нет — показывается placeholder с первой буквой логина."""
        await self._login_admin1("secret123")
        resp = await self.client.get("/me", follow_redirects=False)
        # Должна быть аватарка-placeholder или <div> с буквой A
        # И не должно быть <img src="/avatar/..."> (т.к. файла нет)
        self.assertNotIn("/avatar/123456", resp.text)


# ═══════════════════════════════════════════════════════════════════════════
# Тест 3: POST /me/password — смена пароля
# ═══════════════════════════════════════════════════════════════════════════
class TestChangePassword(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await init_db()
        await _clear_all_tables()
        await _seed_su()
        from db import _hash_password
        async with async_session() as s:
            s.add(WebUser(
                username="admin1", is_su=False, is_active=True,
                role="admin", created_by="su",
                password_hash=_hash_password("oldpass123"),
                tg_user_id=123456, tg_username="admin1",
            ))
            await s.commit()

        from web_app import create_app
        self.mock_bot = MagicMock()
        self.app = create_app(bot=self.mock_bot)
        from httpx import AsyncClient, ASGITransport
        self.client = AsyncClient(transport=ASGITransport(app=self.app), base_url="http://test")
        resp = await self.client.post(
            "/login", data={"username": "admin1", "password": "oldpass123"},
            follow_redirects=False,
        )
        assert resp.status_code == 303

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_change_password_success_redirects_to_me(self):
        """Успешная смена пароля → редирект на /me?pw_msg=..."""
        resp = await self.client.post(
            "/me/password",
            data={"old_password": "oldpass123", "new_password": "newpass456", "confirm": "newpass456"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        self.assertIn("/me", resp.headers["location"])
        self.assertIn("pw_msg=Password+changed+successfully", resp.headers["location"])

    async def test_change_password_wrong_old(self):
        """Неверный старый пароль → редирект с pw_msg=Current+password+is+incorrect."""
        resp = await self.client.post(
            "/me/password",
            data={"old_password": "WRONG", "new_password": "newpass456", "confirm": "newpass456"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        self.assertIn("Current+password+is+incorrect", resp.headers["location"])

    async def test_change_password_too_short(self):
        """Короткий новый пароль (<6) → редирект с pw_msg=at+least+6+chars."""
        resp = await self.client.post(
            "/me/password",
            data={"old_password": "oldpass123", "new_password": "abc", "confirm": "abc"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        self.assertIn("at+least+6+chars", resp.headers["location"])

    async def test_change_password_mismatch(self):
        """Несовпадение new/confirm → редирект с pw_msg=do+not+match."""
        resp = await self.client.post(
            "/me/password",
            data={"old_password": "oldpass123", "new_password": "newpass456", "confirm": "different"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        self.assertIn("do+not+match", resp.headers["location"])

    async def test_change_password_same_as_old(self):
        """Новый пароль совпадает со старым → редирект с pw_msg=must+differ."""
        resp = await self.client.post(
            "/me/password",
            data={"old_password": "oldpass123", "new_password": "oldpass123", "confirm": "oldpass123"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        self.assertIn("must+differ", resp.headers["location"])


# ═══════════════════════════════════════════════════════════════════════════
# Тест 4: Аватарки — хелперы
# ═══════════════════════════════════════════════════════════════════════════
class TestAvatarHelpers(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await init_db()
        await _clear_all_tables()
        await _seed_su()

    def test_avatar_url_returns_none_if_no_file(self):
        """_avatar_url возвращает None если файла нет."""
        from web_app import _avatar_url
        # Файла нет — None
        result = _avatar_url(999999, None)
        self.assertIsNone(result)

    def test_avatar_url_returns_none_if_no_tg_id(self):
        """_avatar_url возвращает None если tg_user_id is None."""
        from web_app import _avatar_url
        result = _avatar_url(None, None)
        self.assertIsNone(result)

    def test_avatar_url_with_file_returns_url_with_cache_buster(self):
        """Если файл есть — возвращается URL с ?v=<ts>."""
        import tempfile, os, time
        from web_app import _avatar_url, _avatar_path, AVATARS_DIR
        # Создаём временный файл аватарки
        os.makedirs(AVATARS_DIR, exist_ok=True)
        tg_id = 888888
        path = _avatar_path(tg_id)
        try:
            with open(path, "wb") as f:
                f.write(b"fake-jpeg")
            mtime = int(os.path.getmtime(path))
            result = _avatar_url(tg_id, None)
            self.assertIsNotNone(result)
            self.assertIn(f"/avatar/{tg_id}", result)
            self.assertIn(f"v={mtime}", result)
        finally:
            if os.path.exists(path):
                os.remove(path)

    async def test_fetch_and_save_avatar_returns_false_on_no_photos(self):
        """Если у юзера нет аватарки в TG — _fetch_and_save_avatar возвращает False."""
        from web_app import _fetch_and_save_avatar

        mock_bot = MagicMock()
        # TG возвращает пустой результат
        mock_photos = MagicMock()
        mock_photos.total_count = 0
        mock_photos.photos = []
        mock_bot.get_user_profile_photos = AsyncMock(return_value=mock_photos)

        result = await _fetch_and_save_avatar(mock_bot, 12345)
        self.assertFalse(result)

    async def test_fetch_and_save_avatar_returns_false_on_bot_none(self):
        """Если bot is None — _fetch_and_save_avatar возвращает False."""
        from web_app import _fetch_and_save_avatar
        result = await _fetch_and_save_avatar(None, 12345)
        self.assertFalse(result)

    async def test_fetch_and_save_avatar_saves_file_when_photos_exist(self):
        """Если TG возвращает фото — файл сохраняется, возвращается True."""
        import os
        from web_app import _fetch_and_save_avatar, _avatar_path, AVATARS_DIR
        os.makedirs(AVATARS_DIR, exist_ok=True)
        tg_id = 777777
        path = _avatar_path(tg_id)
        try:
            mock_bot = MagicMock()
            # PhotoSize с file_id
            mock_size = MagicMock()
            mock_size.file_id = "fake_file_id"
            mock_photos = MagicMock()
            mock_photos.total_count = 1
            mock_photos.photos = [[mock_size]]  # [[size1, size2, ...]]
            mock_bot.get_user_profile_photos = AsyncMock(return_value=mock_photos)
            # File с file_path
            mock_file = MagicMock()
            mock_file.file_path = "photos/123/file_1.jpg"
            mock_bot.get_file = AsyncMock(return_value=mock_file)
            # bot.download возвращает bytes
            mock_bot.download = AsyncMock(return_value=b"fake-jpeg-bytes")

            result = await _fetch_and_save_avatar(mock_bot, tg_id)
            self.assertTrue(result)
            self.assertTrue(os.path.exists(path))
            with open(path, "rb") as f:
                self.assertEqual(f.read(), b"fake-jpeg-bytes")
        finally:
            if os.path.exists(path):
                os.remove(path)

    async def test_fetch_and_save_avatar_handles_bytesio(self):
        """Если bot.download возвращает BytesIO — корректно читается."""
        import os
        from web_app import _fetch_and_save_avatar, _avatar_path, AVATARS_DIR
        os.makedirs(AVATARS_DIR, exist_ok=True)
        tg_id = 666666
        path = _avatar_path(tg_id)
        try:
            mock_bot = MagicMock()
            mock_size = MagicMock()
            mock_size.file_id = "fake_file_id"
            mock_photos = MagicMock()
            mock_photos.total_count = 1
            mock_photos.photos = [[mock_size]]
            mock_bot.get_user_profile_photos = AsyncMock(return_value=mock_photos)
            mock_file = MagicMock()
            mock_file.file_path = "photos/123/file_1.jpg"
            mock_bot.get_file = AsyncMock(return_value=mock_file)
            # BytesIO вместо bytes
            mock_bot.download = AsyncMock(return_value=BytesIO(b"bytesio-data"))

            result = await _fetch_and_save_avatar(mock_bot, tg_id)
            self.assertTrue(result)
            with open(path, "rb") as f:
                self.assertEqual(f.read(), b"bytesio-data")
        finally:
            if os.path.exists(path):
                os.remove(path)


# ═══════════════════════════════════════════════════════════════════════════
# Тест 5: Маршрут /me/avatar/refresh
# ═══════════════════════════════════════════════════════════════════════════
class TestAvatarRefresh(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await init_db()
        await _clear_all_tables()
        await _seed_su()
        from db import _hash_password
        async with async_session() as s:
            s.add(WebUser(
                username="admin1", is_su=False, is_active=True,
                role="admin", created_by="su",
                password_hash=_hash_password("pass123"),
                tg_user_id=123456, tg_username="admin1",
            ))
            await s.commit()

        from web_app import create_app
        self.mock_bot = MagicMock()
        self.app = create_app(bot=self.mock_bot)
        from httpx import AsyncClient, ASGITransport
        self.client = AsyncClient(transport=ASGITransport(app=self.app), base_url="http://test")
        resp = await self.client.post(
            "/login", data={"username": "admin1", "password": "pass123"},
            follow_redirects=False,
        )
        assert resp.status_code == 303

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_avatar_refresh_no_tg_id(self):
        """Если у юзера нет TG ID — редирект с pw_msg=No+TG+ID."""
        # Меняем юзеру tg_user_id на None
        async with async_session() as s:
            wu = (await s.execute(
                select(WebUser).where(WebUser.username == "admin1")
            )).scalar_one()
            wu.tg_user_id = None
            await s.commit()

        resp = await self.client.post("/me/avatar/refresh", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertIn("No+TG+ID", resp.headers["location"])

    async def test_avatar_refresh_success(self):
        """Успешный refresh — редирект с pw_msg=Avatar+updated."""
        import os
        from web_app import _avatar_path, AVATARS_DIR
        os.makedirs(AVATARS_DIR, exist_ok=True)
        path = _avatar_path(123456)
        try:
            # Mock для _fetch_and_save_avatar
            with patch("web_app._fetch_and_save_avatar", new=AsyncMock(return_value=True)):
                resp = await self.client.post("/me/avatar/refresh", follow_redirects=False)
            self.assertEqual(resp.status_code, 303)
            self.assertIn("Avatar+updated", resp.headers["location"])
            # tg_photo_updated_at должен быть проставлен
            async with async_session() as s:
                wu = (await s.execute(
                    select(WebUser).where(WebUser.username == "admin1")
                )).scalar_one()
                self.assertIsNotNone(wu.tg_photo_updated_at)
        finally:
            if os.path.exists(path):
                os.remove(path)

    async def test_avatar_refresh_failure(self):
        """Если _fetch_and_save_avatar вернул False — pw_msg=Could+not+fetch."""
        with patch("web_app._fetch_and_save_avatar", new=AsyncMock(return_value=False)):
            resp = await self.client.post("/me/avatar/refresh", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertIn("Could+not+fetch", resp.headers["location"])


# ═══════════════════════════════════════════════════════════════════════════
# Тест 6: Маршрут /admin/settings (SU-only)
# ═══════════════════════════════════════════════════════════════════════════
class TestAdminSettingsRoute(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await init_db()
        await _clear_all_tables()
        await _seed_su()
        from db import _hash_password
        async with async_session() as s:
            s.add(WebUser(
                username="mod1", is_su=False, is_active=True,
                role="moderator", created_by="su",
                password_hash=_hash_password("pass123"),
                tg_user_id=123456, tg_username="mod1",
            ))
            await s.commit()

        from web_app import create_app
        self.mock_bot = MagicMock()
        self.app = create_app(bot=self.mock_bot)
        from httpx import AsyncClient, ASGITransport
        self.client = AsyncClient(transport=ASGITransport(app=self.app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()

    async def _login_su(self):
        resp = await self.client.post(
            "/login", data={"username": "su", "password": "test-pwd"},
            follow_redirects=False,
        )
        assert resp.status_code == 303

    async def _login_mod(self):
        resp = await self.client.post(
            "/login", data={"username": "mod1", "password": "pass123"},
            follow_redirects=False,
        )
        assert resp.status_code == 303

    async def test_settings_requires_auth(self):
        """Без авторизации /admin/settings → /login."""
        resp = await self.client.get("/admin/settings", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertIn("/login", resp.headers["location"])

    async def test_settings_requires_su(self):
        """moderator → redirect на /dashboard."""
        await self._login_mod()
        resp = await self.client.get("/admin/settings", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertIn("/dashboard", resp.headers["location"])

    async def test_settings_su_can_access(self):
        """SU видит страницу Settings."""
        await self._login_su()
        resp = await self.client.get("/admin/settings", follow_redirects=False)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Bot info", resp.text)
        self.assertIn("Backup", resp.text)
        self.assertIn("Cleanup", resp.text)
        self.assertIn("VACUUM", resp.text)

    async def test_settings_shows_version(self):
        """Страница показывает версию приложения."""
        await self._login_su()
        resp = await self.client.get("/admin/settings", follow_redirects=False)
        self.assertIn("v4.5", resp.text)

    async def test_settings_has_backup_form(self):
        """Есть форма POST /admin/settings/backup."""
        await self._login_su()
        resp = await self.client.get("/admin/settings", follow_redirects=False)
        self.assertIn("/admin/settings/backup", resp.text)

    async def test_settings_has_vacuum_form(self):
        """Есть форма POST /admin/settings/vacuum."""
        await self._login_su()
        resp = await self.client.get("/admin/settings", follow_redirects=False)
        self.assertIn("/admin/settings/vacuum", resp.text)

    async def test_settings_has_cleanup_form(self):
        """Есть форма POST /admin/cleanup."""
        await self._login_su()
        resp = await self.client.get("/admin/settings", follow_redirects=False)
        self.assertIn("/admin/cleanup", resp.text)


# ═══════════════════════════════════════════════════════════════════════════
# Тест 7: GET /admin/cleanup → редирект на /admin/settings#cleanup
# ═══════════════════════════════════════════════════════════════════════════
class TestCleanupRedirect(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await init_db()
        await _clear_all_tables()
        await _seed_su()
        from web_app import create_app
        self.mock_bot = MagicMock()
        self.app = create_app(bot=self.mock_bot)
        from httpx import AsyncClient, ASGITransport
        self.client = AsyncClient(transport=ASGITransport(app=self.app), base_url="http://test")
        resp = await self.client.post(
            "/login", data={"username": "su", "password": "test-pwd"},
            follow_redirects=False,
        )
        assert resp.status_code == 303

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_get_admin_cleanup_redirects_to_settings(self):
        """GET /admin/cleanup → 303 на /admin/settings#cleanup."""
        resp = await self.client.get("/admin/cleanup", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertIn("/admin/settings", resp.headers["location"])
        self.assertIn("#cleanup", resp.headers["location"])

    async def test_get_admin_cleanup_with_flash(self):
        """GET /admin/cleanup?flash=... пробрасывает flash в redirect."""
        resp = await self.client.get("/admin/cleanup?flash=test_message", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertIn("flash=test_message", resp.headers["location"])


# ═══════════════════════════════════════════════════════════════════════════
# Тест 8: Навбар
# ═══════════════════════════════════════════════════════════════════════════
class TestNavbar(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await init_db()
        await _clear_all_tables()
        await _seed_su()
        from db import _hash_password
        async with async_session() as s:
            s.add(WebUser(
                username="mod1", is_su=False, is_active=True,
                role="moderator", created_by="su",
                password_hash=_hash_password("pass123"),
                tg_user_id=123456, tg_username="mod1",
            ))
            await s.commit()

        from web_app import create_app
        self.mock_bot = MagicMock()
        self.app = create_app(bot=self.mock_bot)
        from httpx import AsyncClient, ASGITransport
        self.client = AsyncClient(transport=ASGITransport(app=self.app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_navbar_su_has_settings_no_cleanup(self):
        """SU видит Settings, не видит Cleanup в навбаре."""
        resp = await self.client.post(
            "/login", data={"username": "su", "password": "test-pwd"},
            follow_redirects=False,
        )
        resp = await self.client.get("/dashboard", follow_redirects=False)
        # Settings есть
        self.assertIn('href="/admin/settings"', resp.text)
        self.assertIn(">Settings</a>", resp.text)
        # Cleanup нет как отдельного пункта
        self.assertNotIn('href="/admin/cleanup"', resp.text)

    async def test_navbar_su_has_profile_link(self):
        """В навбаре есть Profile (через /me)."""
        resp = await self.client.post(
            "/login", data={"username": "su", "password": "test-pwd"},
            follow_redirects=False,
        )
        resp = await self.client.get("/dashboard", follow_redirects=False)
        self.assertIn('href="/me"', resp.text)

    async def test_navbar_mod_does_not_see_settings(self):
        """moderator не видит Settings в навбаре."""
        resp = await self.client.post(
            "/login", data={"username": "mod1", "password": "pass123"},
            follow_redirects=False,
        )
        resp = await self.client.get("/dashboard", follow_redirects=False)
        self.assertNotIn('href="/admin/settings"', resp.text)

    async def test_navbar_mod_sees_profile(self):
        """moderator видит Profile в навбаре."""
        resp = await self.client.post(
            "/login", data={"username": "mod1", "password": "pass123"},
            follow_redirects=False,
        )
        resp = await self.client.get("/dashboard", follow_redirects=False)
        self.assertIn('href="/me"', resp.text)

    async def test_navbar_su_has_avatar_placeholder(self):
        """SU без аватарки видит placeholder с буквой 'S'."""
        resp = await self.client.post(
            "/login", data={"username": "su", "password": "test-pwd"},
            follow_redirects=False,
        )
        resp = await self.client.get("/dashboard", follow_redirects=False)
        self.assertIn("avatar-placeholder", resp.text)
        self.assertIn(">S</span>", resp.text)  # первая буква 'su' верхнего регистра

    async def test_navbar_shows_username(self):
        """В навбаре показывается логин пользователя."""
        resp = await self.client.post(
            "/login", data={"username": "su", "password": "test-pwd"},
            follow_redirects=False,
        )
        resp = await self.client.get("/dashboard", follow_redirects=False)
        self.assertIn(">su</span>", resp.text)


# ═══════════════════════════════════════════════════════════════════════════
# Тест 9: Health endpoint с версией
# ═══════════════════════════════════════════════════════════════════════════
class TestHealthEndpoint(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await init_db()
        await _clear_all_tables()
        await _seed_su()
        from web_app import create_app, APP_VERSION
        self.app_version = APP_VERSION
        self.mock_bot = MagicMock()
        self.app = create_app(bot=self.mock_bot)
        from httpx import AsyncClient, ASGITransport
        self.client = AsyncClient(transport=ASGITransport(app=self.app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_health_has_version(self):
        """Health endpoint возвращает версию приложения."""
        import json
        resp = await self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.text)
        self.assertEqual(data["version"], self.app_version)
        self.assertEqual(data["status"], "ok")


# ═══════════════════════════════════════════════════════════════════════════
# Тест 10: DB migration — tg_photo_updated_at добавляется
# ═══════════════════════════════════════════════════════════════════════════
class TestDBMigration(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await init_db()
        await _clear_all_tables()
        await _seed_su()

    async def test_tg_photo_updated_at_column_exists(self):
        """После init_db колонка tg_photo_updated_at существует в web_users."""
        from sqlalchemy import text
        from db import engine
        async with engine.connect() as conn:
            result = await conn.execute(text("PRAGMA table_info(web_users)"))
            columns = [row[1] for row in result.fetchall()]
        self.assertIn("tg_photo_updated_at", columns)

    async def test_tg_photo_updated_at_default_null(self):
        """У существующего юзера tg_photo_updated_at = NULL."""
        async with async_session() as s:
            wu = (await s.execute(
                select(WebUser).where(WebUser.username == "su")
            )).scalar_one()
            self.assertIsNone(wu.tg_photo_updated_at)


if __name__ == "__main__":
    unittest.main(verbosity=2)
