"""
test_v472_toggles.py — Smoke-тест v4.7.2: явные toggle для night mode / sanitary.

Проверяет:
  1. APP_VERSION = "v4.7.2".
  2. DB миграции: колонка sanitary_days_enabled в chat_settings.
  3. Миграция сброса: night_mode_enabled=False для всех чатов после init_db.
  4. /admin/chats содержит кнопку "Sanitary days" toggle.
  5. POST /admin/chats/<id>/toggle field=sanitary_days — переключает toggle.
  6. _sanitary_day_tick в bot.py пропускает чаты с sanitary_days_enabled=False.
  7. _night_mode_tick уже фильтрует по night_mode_enabled (не сломан).
  8. Команда /sanitary <chat_id> on включает функцию.
  9. Команда /sanitary <chat_id> add при выключенной функции — отказ.
 10. Команда /nightmode <chat_id> on включает функцию.
 11. Changelog содержит v4.7.2.
 12. Бейдж SAN не показывается если sanitary_days_enabled=False.
"""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch, AsyncMock, MagicMock

sys.path.insert(0, "/home/z/my-project/v4.5")
os.chdir("/home/z/my-project/v4.5")

_DB_PATH = tempfile.mktemp(suffix="_v472.db")
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
import bot as bot_module

from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader


async def _seed():
    """Init DB + seed SU + one regular chat with night_mode_enabled=True (проверим что миграция сбросит)."""
    await init_db()
    async with async_session() as s:
        await s.execute(text("DELETE FROM permission_presets"))
        await s.execute(text("DELETE FROM chat_settings"))
        await s.execute(text("DELETE FROM web_users WHERE username != 'su'"))
        await s.execute(text("DELETE FROM chat_admins"))
        await s.commit()
    await init_db()  # re-seed system presets + run migrations
    async with async_session() as s:
        existing_su = (await s.execute(
            select(WebUser).where(WebUser.username == "su")
        )).scalar_one_or_none()
        if existing_su is None:
            s.add(WebUser(username="su", is_su=True, is_active=True,
                          role="su", created_by="system"))
            await s.commit()
        # Regular chat with night_mode_enabled=True (проверим что миграция v4.7.2 сбросит)
        cs1 = ChatSettings(
            chat_id=-1001234567890,
            title="Test Chat",
            hashtag="#Test",
            is_enabled=True,
            warns_to_mute=3,
            warns_to_ban=5,
            mute_duration_seconds=3600,
            night_mode_enabled=True,  # ДОЛЖНО быть сброшено миграцией
            night_mode_start="23:00",
            night_mode_end="07:00",
        )
        s.add(cs1)
        await s.commit()
    # Re-run init_db to apply migration (сброс toggles)
    await init_db()


class TestV472Toggles(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await _seed()
        web_app._check_login_rate_limit = lambda ip: True
        self._mock_bot = MagicMock()
        async def _no_admins(chat_id):
            return []
        self._mock_bot.get_chat_administrators = _no_admins
        # Mock для _exit_night_mode / _exit_sanitary_day если понадобятся
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
    async def test_app_version_is_v472(self):
        self.assertEqual(web_app.APP_VERSION, "v4.7.2")

    # ── Test 2: DB колонка sanitary_days_enabled ─────────────────────
    async def test_db_column_sanitary_days_enabled(self):
        async with engine.connect() as conn:
            result = await conn.execute(text("PRAGMA table_info(chat_settings)"))
            columns = [row[1] for row in result.fetchall()]
        self.assertIn("sanitary_days_enabled", columns,
                      "Column sanitary_days_enabled missing in chat_settings")

    # ── Test 3: Миграция сбрасывает night_mode_enabled ────────────────
    async def test_migration_resets_night_mode_enabled(self):
        """После init_db (которая запускает миграцию), night_mode_enabled
        должно быть False для всех чатов — даже если мы его выставили в True."""
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001234567890)
            )).scalar_one()
            self.assertFalse(cs.night_mode_enabled,
                             "Migration should reset night_mode_enabled to False")
            self.assertFalse(cs.night_mode_currently_active,
                             "Migration should reset night_mode_currently_active to False")
            self.assertFalse(cs.sanitary_days_currently_active,
                             "Migration should reset sanitary_days_currently_active to False")

    # ── Test 4: кнопка Sanitary days toggle в /admin/chats ───────────
    async def test_admin_chats_has_sanitary_toggle(self):
        self._login_as_su()
        r = self.client.get("/admin/chats", follow_redirects=False)
        self.assertEqual(r.status_code, 200)
        html = r.text
        self.assertIn("Sanitary days", html,
                      "Missing 'Sanitary days' toggle button")
        self.assertIn('value="sanitary_days"', html,
                      "Missing field=sanitary_days in toggle form")

    # ── Test 5: POST toggle field=sanitary_days ──────────────────────
    async def test_toggle_sanitary_days(self):
        self._login_as_su()
        # Initially False → toggle to True
        r = self.client.post(
            "/admin/chats/-1001234567890/toggle",
            data={"field": "sanitary_days"},
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 303,
                         f"Expected 303, got {r.status_code}: {r.text[:200]}")
        loc = r.headers.get("location", "")
        self.assertIn("Sanitary+days+enabled", loc, f"Should enable: {loc}")

        # Verify DB
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001234567890)
            )).scalar_one()
            self.assertTrue(cs.sanitary_days_enabled,
                            "sanitary_days_enabled should be True after toggle")

        # Toggle back to False
        r = self.client.post(
            "/admin/chats/-1001234567890/toggle",
            data={"field": "sanitary_days"},
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 303)
        loc = r.headers.get("location", "")
        self.assertIn("Sanitary+days+disabled", loc, f"Should disable: {loc}")

        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001234567890)
            )).scalar_one()
            self.assertFalse(cs.sanitary_days_enabled,
                             "sanitary_days_enabled should be False after second toggle")

    # ── Test 6: _sanitary_day_tick пропускает disabled чаты ──────────
    async def test_sanitary_tick_skips_disabled_chats(self):
        """Чат с sanitary_days_enabled=False не должен обрабатываться tick."""
        # Setup: chat with sanitary_days set but enabled=False
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001234567890)
            )).scalar_one()
            cs.sanitary_days = '[["2026-08-01","2026-08-01"]]'
            cs.sanitary_days_enabled = False
            await s.commit()

        # Mock bot methods
        mock_bot_instance = MagicMock()
        mock_bot_instance.get_chat = AsyncMock(return_value=MagicMock(permissions=MagicMock()))
        mock_bot_instance.set_chat_permissions = AsyncMock()
        mock_bot_instance.send_message = AsyncMock()

        with patch.object(bot_module, 'bot', mock_bot_instance):
            await bot_module._sanitary_day_tick()

        # set_chat_permissions НЕ должен был вызываться (чат disabled)
        mock_bot_instance.set_chat_permissions.assert_not_called()

    # ── Test 7: _night_mode_tick фильтрует по night_mode_enabled ─────
    async def test_night_tick_filters_disabled(self):
        """Чат с night_mode_enabled=False не должен обрабатываться tick."""
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001234567890)
            )).scalar_one()
            cs.night_mode_enabled = False
            cs.night_mode_start = "23:00"
            cs.night_mode_end = "07:00"
            await s.commit()

        mock_bot_instance = MagicMock()
        mock_bot_instance.get_chat = AsyncMock(return_value=MagicMock(permissions=MagicMock()))
        mock_bot_instance.set_chat_permissions = AsyncMock()

        with patch.object(bot_module, 'bot', mock_bot_instance):
            await bot_module._night_mode_tick()

        mock_bot_instance.set_chat_permissions.assert_not_called()

    # ── Test 8: /sanitary <chat_id> on включает функцию ──────────────
    async def test_sanitary_command_on(self):
        """Проверяем что /sanitary chat_id on ставит sanitary_days_enabled=True."""
        from aiogram import types as tg_types
        # Создаём фейковый message
        message = MagicMock(spec=tg_types.Message)
        message.from_user = MagicMock(id=1)  # SU
        message.chat = MagicMock(type="private")
        message.text = "/sanitary -1001234567890 on"
        message.reply = AsyncMock()
        message.bot = self._mock_bot

        await bh.cmd_sanitary(message)

        message.reply.assert_awaited()
        # Проверяем что был вызов с "функция включена"
        args = message.reply.call_args[0][0]
        self.assertIn("функция включена", args,
                      f"Reply should mention enable: {args}")

        # Verify DB
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001234567890)
            )).scalar_one()
            self.assertTrue(cs.sanitary_days_enabled,
                            "sanitary_days_enabled should be True after /sanitary on")

    # ── Test 9: /sanitary <chat_id> add при выключенной функции ──────
    async def test_sanitary_add_rejected_when_disabled(self):
        """Если sanitary_days_enabled=False, /sanitary add должен отказать."""
        from aiogram import types as tg_types
        # Удостоверимся что функция выключена
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001234567890)
            )).scalar_one()
            cs.sanitary_days_enabled = False
            await s.commit()

        message = MagicMock(spec=tg_types.Message)
        message.from_user = MagicMock(id=1)
        message.chat = MagicMock(type="private")
        message.text = "/sanitary -1001234567890 add 2026-08-15"
        message.reply = AsyncMock()
        message.bot = self._mock_bot

        await bh.cmd_sanitary(message)

        message.reply.assert_awaited()
        args = message.reply.call_args[0][0]
        self.assertIn("выключена", args,
                      f"Reply should mention disabled: {args}")
        # Подсказка про on
        self.assertIn("/sanitary", args)

    # ── Test 10: /nightmode <chat_id> on ─────────────────────────────
    async def test_nightmode_command_on(self):
        """Проверяем что /nightmode chat_id on ставит night_mode_enabled=True."""
        from aiogram import types as tg_types
        message = MagicMock(spec=tg_types.Message)
        message.from_user = MagicMock(id=1)
        message.chat = MagicMock(type="private")
        message.text = "/nightmode -1001234567890 on"
        message.reply = AsyncMock()
        message.bot = self._mock_bot

        await bh.cmd_nightmode(message)

        message.reply.assert_awaited()
        args = message.reply.call_args[0][0]
        self.assertIn("функция включена", args,
                      f"Reply should mention enable: {args}")

        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001234567890)
            )).scalar_one()
            self.assertTrue(cs.night_mode_enabled,
                            "night_mode_enabled should be True after /nightmode on")

    # ── Test 11: Changelog содержит v4.7.2 ───────────────────────────
    async def test_changelog_mentions_v472(self):
        self._login_as_su()
        r = self.client.get("/dashboard", follow_redirects=False)
        html = r.text
        self.assertIn("v4.7.2", html, "Changelog should mention v4.7.2")
        self.assertIn("toggle", html.lower(), "Changelog should mention toggle")

    # ── Test 12: SAN бейдж не показывается при disabled ──────────────
    async def test_san_badge_hidden_when_disabled(self):
        """Если sanitary_days_enabled=False, бейдж SAN не должен показываться в карточке.
        Проверяем именно бейдж (class='badge'...>SAN...), а не упоминание SAN в changelog."""
        # Setup: chat with sanitary_days but enabled=False
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001234567890)
            )).scalar_one()
            cs.sanitary_days = '[["2026-08-01","2026-08-01"]]'
            cs.sanitary_days_enabled = False
            await s.commit()

        self._login_as_su()
        r = self.client.get("/admin/chats", follow_redirects=False)
        html = r.text
        # Ищем именно бейдж (span class="badge"...>SAN...)
        import re
        san_badges = re.findall(r'<span class="badge"[^>]*>SAN[^<]*</span>', html)
        self.assertEqual(len(san_badges), 0,
                         f"SAN badge should be hidden when sanitary_days_enabled=False, "
                         f"found: {san_badges}")

    # ── Test 13: SAN бейдж показывается при enabled + dates ──────────
    async def test_san_badge_shown_when_enabled(self):
        """Если sanitary_days_enabled=True и есть даты — бейдж SAN показывается."""
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001234567890)
            )).scalar_one()
            cs.sanitary_days = '[["2026-08-01","2026-08-01"]]'
            cs.sanitary_days_enabled = True
            await s.commit()

        self._login_as_su()
        r = self.client.get("/admin/chats", follow_redirects=False)
        html = r.text
        import re
        san_badges = re.findall(r'<span class="badge"[^>]*>SAN[^<]*</span>', html)
        self.assertGreaterEqual(len(san_badges), 1,
                                f"SAN badge should be visible when enabled + dates, "
                                f"found: {san_badges}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
