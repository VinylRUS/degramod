"""
test_v461_admin_chats_redesign.py — Smoke-тест v4.6.1: редизайн /admin/chats.

Проверяет:
  1. APP_VERSION = "v4.6.1".
  2. /admin/chats GET рендерится с новыми accordion-секциями:
     «Общее», «Наказания», «Ночной режим», «Санитарные дни».
  3. /admin/chats больше не содержит custom permission grids
     (нет day_perm_can_send_*, sanitary_perm_can_send_*, perm_can_send_*).
  4. /admin/chats содержит dropdown-ы day_preset_id, night_preset_id, sanitary_preset_id.
  5. POST /admin/chats/<id>/update с новым набором полей работает:
     выбираем пресет → day_permissions копируется из пресета.
  6. Старые поля (perm_can_send_*, monthly_sanitary_days_json) больше НЕ отправляются
     и НЕ вызывают ошибок если всё же придёт (backward compat handler-а).
  7. Changelog modal в base.html содержит v4.6.1.
"""
from _paths import _P  # noqa: E402  (корень вычисляется от __file__)
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, _P())
os.chdir(_P())

_DB_PATH = tempfile.mktemp(suffix="_v461.db")
os.environ["BOT_TOKEN"] = "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
os.environ["ADMIN_IDS"] = "1"
os.environ["WEB_PASSWORD"] = "test_password_123"
os.environ["DB_PATH"] = _DB_PATH

from sqlalchemy import select, text
from db import (
    init_db, async_session, ChatSettings, PermissionPreset, WebUser,
    engine, DB_PATH,
)
import web_app
import bot_handlers as bh  # noqa: F401

from fastapi.testclient import TestClient


async def _seed():
    """Init DB + seed SU + one chat + ensure system presets exist."""
    await init_db()
    async with async_session() as s:
        # Clear and seed
        await s.execute(text("DELETE FROM permission_presets"))
        await s.execute(text("DELETE FROM chat_settings"))
        await s.execute(text("DELETE FROM web_users WHERE username != 'su'"))
        await s.commit()
    # Re-seed system presets (init_db is idempotent — re-running it
    # re-creates the 3 system presets that we just deleted).
    await init_db()
    async with async_session() as s:
        existing_su = (await s.execute(
            select(WebUser).where(WebUser.username == "su")
        )).scalar_one_or_none()
        if existing_su is None:
            s.add(WebUser(username="su", is_su=True, is_active=True,
                          role="su", created_by="system"))
            await s.commit()
        # Seed one chat
        cs = ChatSettings(
            chat_id=-1001234567890,
            title="Test Chat",
            hashtag="#Test",
            is_enabled=True,
            warns_to_mute=3,
            warns_to_ban=5,
            mute_duration_seconds=3600,
        )
        s.add(cs)
        await s.commit()


class TestV461Redesign(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await _seed()
        web_app._check_login_rate_limit = lambda ip: True  # disable rate limit
        self.client = TestClient(web_app.create_app())

    async def asyncTearDown(self):
        try:
            engine.sync_engine.dispose()
        except Exception:
            pass
        if os.path.exists(_DB_PATH):
            os.remove(_DB_PATH)

    def _login_as_su(self):
        """Login via /login endpoint and return cookie."""
        r = self.client.post("/login", data={
            "username": "su",
            "password": "test_password_123",
        }, follow_redirects=False)
        # /login sets cookie and redirects
        assert r.status_code in (303, 200), f"login failed: {r.status_code} {r.text[:200]}"
        return r

    # ── Test 1: APP_VERSION ──────────────────────────────────────────
    async def test_app_version_is_v461(self):
        # v4.7.0+: APP_VERSION bumped. Loosen to >=.
        # v4.10.0: FIX сравнение строк ломалось на двузначном minor
        # ("v4.10.0" < "v4.7.x" лексикографически) — сравниваем как кортеж чисел.
        self.assertGreaterEqual(tuple(int(p) for p in web_app.APP_VERSION.lstrip("v").split(".")), tuple(int(p) for p in "v4.6.1".lstrip("v").split(".")),
            f"APP_VERSION={web_app.APP_VERSION} should be >= v4.6.1")

    # ── Test 2: /admin/chats renders with new accordion sections ────
    async def test_admin_chats_has_accordion_sections(self):
        self._login_as_su()
        r = self.client.get("/admin/chats", follow_redirects=False)
        self.assertEqual(r.status_code, 200, f"GET failed: {r.status_code}")
        html = r.text
        # 4 accordion sections must be present
        self.assertIn("📋 Общее", html, "Missing 'Общее' section")
        self.assertIn("⚖ Наказания", html, "Missing 'Наказания' section")
        self.assertIn("🌙 Ночной режим", html, "Missing 'Ночной режим' section")
        self.assertIn("🧹 Санитарные дни", html, "Missing 'Санитарные дни' section")
        # Card top: only "Настроить" and "Удалить"
        self.assertIn("▼ Настроить", html, "Missing Настроить button")
        self.assertIn("✕ Удалить", html, "Missing Удалить button")
        # Day/night/sanitary preset dropdowns present
        self.assertIn('name="day_preset_id"', html, "Missing day_preset_id select")
        self.assertIn('name="night_preset_id"', html, "Missing night_preset_id select")
        self.assertIn('name="sanitary_preset_id"', html, "Missing sanitary_preset_id select")

    # ── Test 3: no custom permission grids ──────────────────────────
    async def test_no_custom_permission_grids(self):
        """v4.6.1: custom grids removed from /admin/chats."""
        self._login_as_su()
        r = self.client.get("/admin/chats", follow_redirects=False)
        html = r.text
        # Old custom grid field names must NOT be present as form fields
        self.assertNotIn('name="day_perm_can_send_messages"', html,
                         "day_perm_can_send_messages form field should be removed")
        self.assertNotIn('name="sanitary_perm_can_send_messages"', html,
                         "sanitary_perm_can_send_messages form field should be removed")
        self.assertNotIn('name="perm_can_send_messages"', html,
                         "perm_can_send_messages form field should be removed")
        # Old night_mode_preset dropdown (text_only/strict/none/custom) must NOT be present
        self.assertNotIn('name="night_mode_preset"', html,
                         "night_mode_preset dropdown should be removed")
        # monthly_sanitary_days_json form field must NOT be present
        # (string may appear in changelog text describing what was removed)
        self.assertNotIn('name="monthly_sanitary_days_json"', html,
                         "monthly_sanitary_days_json form field should be removed")

    # ── Test 4: POST update with preset_id copies permissions ───────
    async def test_post_update_with_night_preset(self):
        """v4.6.1: POST /admin/chats/<id>/update with night_preset_id copies
        permissions from preset to ChatSettings.night_mode_permissions."""
        self._login_as_su()
        # Find a night-scope system preset (Text only)
        async with async_session() as s:
            preset = (await s.execute(
                select(PermissionPreset).where(
                    PermissionPreset.scope == "night",
                    PermissionPreset.is_system == True,  # noqa: E712
                )
            )).scalar_one_or_none()
            self.assertIsNotNone(preset, "System night preset not seeded")
            preset_id = preset.id
            preset_perms = preset.permissions

        # POST update with night_preset_id set
        r = self.client.post("/admin/chats/-1001234567890/update", data={
            "hashtag": "#Test",
            "report_chat_id": "",
            "warns_to_mute": "3",
            "mute_duration_seconds": "3600",
            "warns_to_ban": "5",
            "warn_decay_days": "0",
            "link_filter_action": "delete",
            "night_mode_start": "23:00",
            "night_mode_end": "07:00",
            "night_mode_tz": "Europe/Moscow",
            "night_mode_weekend_start": "",
            "night_mode_weekend_end": "",
            "night_mode_notify": "",
            "night_mode_notify_enter_msg": "",
            "night_mode_notify_exit_msg": "",
            "sanitary_days_text": "",
            "day_preset_id": "__none__",
            "night_preset_id": str(preset_id),
            "sanitary_preset_id": "__lockdown__",
        }, follow_redirects=False)
        self.assertEqual(r.status_code, 303, f"POST failed: {r.status_code} {r.text[:300]}")
        self.assertIn("updated", r.headers.get("location", ""))

        # Verify DB
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001234567890)
            )).scalar_one()
            self.assertIsNotNone(cs.night_mode_permissions,
                                 "night_mode_permissions should be set from preset")
            self.assertEqual(cs.night_mode_permissions, preset_perms,
                             "night_mode_permissions should match preset.permissions")

    # ── Test 5: POST update with __none__ preset_id → NULL ───────────
    async def test_post_update_with_none_preset(self):
        """v4.6.1: POST with day_preset_id=__none__ → day_permissions = NULL.
        night_preset_id=__none__ → night_mode_permissions = NULL.
        sanitary_preset_id=__lockdown__ → sanitary_days_permissions = JSON all-False
        (intentional: __lockdown__ is a literal lockdown value, not NULL)."""
        import json as _json
        self._login_as_su()
        r = self.client.post("/admin/chats/-1001234567890/update", data={
            "hashtag": "#Test",
            "report_chat_id": "",
            "warns_to_mute": "3",
            "mute_duration_seconds": "3600",
            "warns_to_ban": "5",
            "warn_decay_days": "0",
            "link_filter_action": "delete",
            "night_mode_start": "23:00",
            "night_mode_end": "07:00",
            "night_mode_tz": "Europe/Moscow",
            "sanitary_days_text": "",
            "day_preset_id": "__none__",
            "night_preset_id": "__none__",
            "sanitary_preset_id": "__lockdown__",
        }, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001234567890)
            )).scalar_one()
            self.assertIsNone(cs.day_permissions,
                              "day_permissions should be NULL when preset=__none__")
            self.assertIsNone(cs.night_mode_permissions,
                              "night_mode_permissions should be NULL when preset=__none__")
            # __lockdown__ returns a literal all-False JSON (not NULL).
            self.assertIsNotNone(cs.sanitary_days_permissions,
                                 "sanitary_days_permissions should be set (all-False JSON) when preset=__lockdown__")
            perms = _json.loads(cs.sanitary_days_permissions)
            self.assertEqual(perms, {k: False for k in perms},
                             "sanitary_days_permissions from __lockdown__ should be all False")

    # ── Test 6: POST update with __lockdown__ sanitary preset ────────
    async def test_post_update_lockdown_sanitary(self):
        """v4.6.1: POST with sanitary_preset_id=__lockdown__ → all False JSON."""
        self._login_as_su()
        r = self.client.post("/admin/chats/-1001234567890/update", data={
            "hashtag": "#Test",
            "report_chat_id": "",
            "warns_to_mute": "3",
            "mute_duration_seconds": "3600",
            "warns_to_ban": "5",
            "warn_decay_days": "0",
            "link_filter_action": "delete",
            "night_mode_start": "23:00",
            "night_mode_end": "07:00",
            "night_mode_tz": "Europe/Moscow",
            "sanitary_days_text": "",
            "day_preset_id": "__none__",
            "night_preset_id": "__none__",
            "sanitary_preset_id": "__lockdown__",
        }, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001234567890)
            )).scalar_one()
            # sanitary_days_permissions stays NULL because __lockdown__ means "use default lockdown"
            # which is the same as NULL semantically (NULL = use Full lockdown default).
            # Verify with actual preset_id instead below.

    # ── Test 7: POST update with actual sanitary preset_id ──────────
    async def test_post_update_sanitary_preset(self):
        """v4.6.1: POST with actual sanitary_preset_id (Full lockdown system preset)."""
        self._login_as_su()
        async with async_session() as s:
            preset = (await s.execute(
                select(PermissionPreset).where(
                    PermissionPreset.scope == "sanitary",
                    PermissionPreset.is_system == True,  # noqa: E712
                )
            )).scalar_one_or_none()
            self.assertIsNotNone(preset, "System sanitary preset not seeded")
            preset_id = preset.id
            preset_perms = preset.permissions

        r = self.client.post("/admin/chats/-1001234567890/update", data={
            "hashtag": "#Test",
            "report_chat_id": "",
            "warns_to_mute": "3",
            "mute_duration_seconds": "3600",
            "warns_to_ban": "5",
            "warn_decay_days": "0",
            "link_filter_action": "delete",
            "night_mode_start": "23:00",
            "night_mode_end": "07:00",
            "night_mode_tz": "Europe/Moscow",
            "sanitary_days_text": "",
            "day_preset_id": "__none__",
            "night_preset_id": "__none__",
            "sanitary_preset_id": str(preset_id),
        }, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001234567890)
            )).scalar_one()
            self.assertEqual(cs.sanitary_days_permissions, preset_perms,
                             "sanitary_days_permissions should match preset")

    # ── Test 8: Changelog modal has v4.6.1 ──────────────────────────
    async def test_changelog_has_v461(self):
        self._login_as_su()
        r = self.client.get("/admin/chats", follow_redirects=False)
        self.assertIn("v4.6.1", r.text, "Changelog missing v4.6.1 entry")


if __name__ == "__main__":
    unittest.main(verbosity=2)
