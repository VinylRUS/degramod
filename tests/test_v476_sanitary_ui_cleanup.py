"""
test_v476_sanitary_ui_cleanup.py — v4.7.6: UI санитарных дней с временем +
упразднение private-системы + реорганизация action buttons.

Контекст: v4.7.6 делает несколько связанных изменений:
1. UI sanitary days в карточке чата — date/time picker (4 поля) + кнопка Add
   + список назначенных периодов с кнопкой Delete.
2. Поддержка времени в sanitary-периодах — формат JSON расширен до
   [start, end, start_time?, end_time?]. Tick-логика использует datetime для
   периодов со временем, date — для периодов без времени.
3. Упразднена система private/non-private чатов: toggle убран из UI,
   admin имеет доступ во все чаты.
4. Тогглеры NIGHT и SAN перенесены из <details> в зону action buttons.
5. Sync admins перенесён в зону action buttons (как «↻ Sync»).
6. Компактные лейблы: LINK-FILT → LINK, "make report" → REPORT.

Тесты (24):
  Unit-тесты bot_handlers (10):
    1. _parse_sanitary_time: валидные/невалидные значения, нормализация
    2. parse_sanitary_days_json: формат со временем (4 поля)
    3. parse_sanitary_days_json: backward compat (2 поля, без времени)
    4. parse_sanitary_days_json: dict-monthly со временем
    5. serialize_sanitary_days_monthly: round-trip с временем
    6. add_sanitary_period: добавляет период в правильный месяц
    7. add_sanitary_period: с временем и без
    8. delete_sanitary_period: по глобальному индексу
    9. is_sanitary_active_now_at: внутри/снаружи/на границах
    10. format_sanitary_period_human: с временем/без/однодневный

  E2E тесты web_app (10):
    11. APP_VERSION = "v4.7.6"
    12. GET /admin/chats рендерит date/time picker (input type=date/time)
    13. GET /admin/chats НЕ содержит "PRIVATE" badge / "PUBLIC" toggle
    14. GET /admin/chats содержит NIGHT toggle в action buttons (не в details)
    15. GET /admin/chats содержит SAN toggle в action buttons
    16. GET /admin/chats содержит "↻ Sync" в action buttons (SU only)
    17. POST /admin/chats/{id}/sanitary/add создаёт период с временем
    18. POST /admin/chats/{id}/sanitary/add без времени — full-day
    19. POST /admin/chats/{id}/sanitary/{idx}/delete удаляет период
    20. POST /admin/chats/{id}/toggle field=private → invalid (упразднено)

  Static-тесты (4):
    21. В web_app.py есть handlers /sanitary/add и /sanitary/{idx}/delete
    22. В web_app.py valid_fields не содержит "private"
    23. В bot_handlers.py admin return True (не зависит от is_private)
    24. В base.html changelog содержит v4.7.6
"""
from _paths import _P  # noqa: E402  (корень вычисляется от __file__)
import os
import sys
import tempfile
import json
import unittest
from unittest.mock import MagicMock, AsyncMock

sys.path.insert(0, _P())
os.chdir(_P())

_DB_PATH = tempfile.mktemp(suffix="_v476.db")
os.environ["BOT_TOKEN"] = "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
os.environ["ADMIN_IDS"] = "1"
os.environ["WEB_PASSWORD"] = "test_password_123"
os.environ["DB_PATH"] = _DB_PATH

from sqlalchemy import select, text
from db import (
    init_db, async_session, ChatSettings, WebUser,
)
import web_app
import bot_handlers as bh
from bot_handlers import (
    _parse_sanitary_time,
    parse_sanitary_days_json,
    parse_sanitary_days_monthly,
    serialize_sanitary_days_monthly,
    add_sanitary_period,
    delete_sanitary_period,
    get_sanitary_periods_flat,
    is_sanitary_active_now_at,
    is_sanitary_day_today,
    format_sanitary_period_human,
    format_sanitary_days_textarea,
)

from datetime import datetime, date, timezone
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
        # Test chat
        s.add(ChatSettings(
            chat_id=-1004760000001,
            title="Test Chat v4.7.6",
            is_enabled=True,
        ))
        await s.commit()


# ──────────────────────────────────────────────────────────────────────────
# Unit tests (bot_handlers)
# ──────────────────────────────────────────────────────────────────────────

class TestV476UnitBotHandlers(unittest.TestCase):
    """Юнит-тесты новых функций в bot_handlers.py."""

    def test_01_parse_sanitary_time(self):
        """_parse_sanitary_time: валидные/невалидные, нормализация 9:00 → 09:00."""
        self.assertEqual(_parse_sanitary_time("23:00"), "23:00")
        self.assertEqual(_parse_sanitary_time("9:00"), "09:00")  # normalize
        self.assertEqual(_parse_sanitary_time("00:00"), "00:00")
        self.assertEqual(_parse_sanitary_time("23:59"), "23:59")
        # Invalid
        self.assertIsNone(_parse_sanitary_time("25:00"))  # hour > 23
        self.assertIsNone(_parse_sanitary_time("12:60"))  # minute > 59
        self.assertIsNone(_parse_sanitary_time("abc"))
        self.assertIsNone(_parse_sanitary_time(""))
        self.assertIsNone(_parse_sanitary_time(None))

    def test_02_parse_json_with_time(self):
        """parse_sanitary_days_json: формат со временем (4 поля)."""
        js = json.dumps([["2026-07-31", "2026-08-03", "23:00", "09:00"]])
        result = parse_sanitary_days_json(js)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0], ["2026-07-31", "2026-08-03", "23:00", "09:00"])

    def test_03_parse_json_backward_compat(self):
        """parse_sanitary_days_json: старый формат (2 поля, без времени)."""
        js = json.dumps([["2026-08-01", "2026-08-01"]])
        result = parse_sanitary_days_json(js)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0], ["2026-08-01", "2026-08-01"])

    def test_04_parse_json_monthly_with_time(self):
        """parse_sanitary_days_json: dict-monthly со временем."""
        js = json.dumps({
            "2026-07": [["2026-07-31", "2026-08-03", "23:00", "09:00"]],
            "2026-09": [["2026-09-04", "2026-09-07", "23:00", "09:00"]],
        })
        result = parse_sanitary_days_json(js)
        self.assertEqual(len(result), 2)
        # Порядок: месяцы в порядке их появления в dict.
        self.assertEqual(result[0][:2], ["2026-07-31", "2026-08-03"])
        self.assertEqual(result[0][2:], ["23:00", "09:00"])
        self.assertEqual(result[1][:2], ["2026-09-04", "2026-09-07"])

    def test_05_serialize_monthly_roundtrip_with_time(self):
        """serialize_sanitary_days_monthly: round-trip с временем."""
        monthly = {
            "2026-07": [["2026-07-31", "2026-08-03", "23:00", "09:00"]],
        }
        js = serialize_sanitary_days_monthly(monthly)
        parsed = parse_sanitary_days_monthly(js)
        self.assertEqual(parsed["2026-07"][0], ["2026-07-31", "2026-08-03", "23:00", "09:00"])

    def test_06_add_period_to_correct_month(self):
        """add_sanitary_period: добавляет период в правильный месяц."""
        new_json, err = add_sanitary_period(None, "2026-07-31", "2026-08-03", "23:00", "09:00")
        self.assertIsNone(err)
        self.assertIsNotNone(new_json)
        monthly = parse_sanitary_days_monthly(new_json)
        self.assertIn("2026-07", monthly)
        self.assertEqual(len(monthly["2026-07"]), 1)
        self.assertEqual(monthly["2026-07"][0][:2], ["2026-07-31", "2026-08-03"])

    def test_07_add_period_with_and_without_time(self):
        """add_sanitary_period: с временем и без (full-day)."""
        # С временем
        j1, _ = add_sanitary_period(None, "2026-08-01", "2026-08-03", "23:00", "09:00")
        periods = get_sanitary_periods_flat(j1)
        self.assertEqual(periods[0], ["2026-08-01", "2026-08-03", "23:00", "09:00"])
        # Без времени (full-day)
        j2, _ = add_sanitary_period(None, "2026-10-15", "2026-10-15")
        periods2 = get_sanitary_periods_flat(j2)
        self.assertEqual(periods2[0], ["2026-10-15", "2026-10-15"])
        # Добавление к существующему
        j3, _ = add_sanitary_period(j1, "2026-10-15", "2026-10-15")
        periods3 = get_sanitary_periods_flat(j3)
        self.assertEqual(len(periods3), 2)

    def test_08_delete_period_by_global_index(self):
        """delete_sanitary_period: удаляет по глобальному индексу."""
        j, _ = add_sanitary_period(None, "2026-07-31", "2026-08-03", "23:00", "09:00")
        j, _ = add_sanitary_period(j, "2026-09-04", "2026-09-07", "23:00", "09:00")
        periods_before = get_sanitary_periods_flat(j)
        self.assertEqual(len(periods_before), 2)
        # Delete index 0
        j_after, err = delete_sanitary_period(j, 0)
        self.assertIsNone(err)
        periods_after = get_sanitary_periods_flat(j_after)
        self.assertEqual(len(periods_after), 1)
        self.assertEqual(periods_after[0][:2], ["2026-09-04", "2026-09-07"])
        # Invalid index
        _, err = delete_sanitary_period(j, 99)
        self.assertIsNotNone(err)

    def test_09_is_sanitary_active_now_at_boundaries(self):
        """is_sanitary_active_now_at: внутри/снаружи/на границах."""
        p = ["2026-07-31", "2026-08-03", "23:00", "09:00"]
        # Inside
        self.assertTrue(is_sanitary_active_now_at(p, datetime(2026, 8, 1, 12, 0)))
        self.assertTrue(is_sanitary_active_now_at(p, datetime(2026, 8, 2, 0, 0)))
        # At start boundary
        self.assertTrue(is_sanitary_active_now_at(p, datetime(2026, 7, 31, 23, 0)))
        # Just before start
        self.assertFalse(is_sanitary_active_now_at(p, datetime(2026, 7, 31, 22, 59)))
        # At end boundary
        self.assertTrue(is_sanitary_active_now_at(p, datetime(2026, 8, 3, 9, 0)))
        # Just after end
        self.assertFalse(is_sanitary_active_now_at(p, datetime(2026, 8, 3, 9, 1)))
        # Way after
        self.assertFalse(is_sanitary_active_now_at(p, datetime(2026, 9, 1, 12, 0)))
        # Aware datetime (timezone-aware — should still work)
        self.assertTrue(is_sanitary_active_now_at(p, datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)))

    def test_10_format_sanitary_period_human(self):
        """format_sanitary_period_human: разные форматы."""
        # С временем
        p1 = ["2026-07-31", "2026-08-03", "23:00", "09:00"]
        self.assertEqual(format_sanitary_period_human(p1), "31.07.2026 23:00 → 03.08.2026 09:00")
        # Однодневный без времени
        p2 = ["2026-10-15", "2026-10-15"]
        self.assertEqual(format_sanitary_period_human(p2), "2026-10-15")
        # Диапазон без времени
        p3 = ["2026-08-01", "2026-08-05"]
        self.assertEqual(format_sanitary_period_human(p3), "2026-08-01 - 2026-08-05")
        # Только start_time
        p4 = ["2026-08-01", "2026-08-01", "23:00"]
        self.assertEqual(format_sanitary_period_human(p4), "01.08.2026 23:00 → 01.08.2026")


# ──────────────────────────────────────────────────────────────────────────
# E2E tests (web_app + TestClient)
# ──────────────────────────────────────────────────────────────────────────

class TestV476E2EWebApp(unittest.IsolatedAsyncioTestCase):
    """E2E тесты через FastAPI TestClient."""

    async def asyncSetUp(self):
        await _seed()
        web_app._check_login_rate_limit = lambda ip: True
        self._mock_bot = MagicMock()
        self._mock_bot.get_chat_administrators = AsyncMock(return_value=[])
        self.client = TestClient(web_app.create_app(bot=self._mock_bot))
        # Login as SU
        r = self.client.post("/login", data={
            "username": "su", "password": "test_password_123",
        }, follow_redirects=False)
        assert r.status_code in (303, 302), f"Login failed: {r.status_code}"

    async def asyncTearDown(self):
        try:
            self.client.close()
        except Exception:
            pass

    def test_11_app_version(self):
        """APP_VERSION = 'v4.7.6'."""
        self.assertGreaterEqual(web_app.APP_VERSION, "v4.7.6",
            f"APP_VERSION={web_app.APP_VERSION} should be >= v4.7.6")

    def test_12_chats_page_has_datetime_picker(self):
        """GET /admin/chats рендерит input type=date и input type=time."""
        r = self.client.get("/admin/chats")
        self.assertEqual(r.status_code, 200)
        html = r.text
        self.assertIn('type="date"', html, "Date input not found in /admin/chats")
        self.assertIn('type="time"', html, "Time input not found in /admin/chats")
        self.assertIn("start_date", html)
        self.assertIn("end_date", html)
        self.assertIn("start_time", html)
        self.assertIn("end_time", html)

    def test_13_no_private_badge_or_toggle(self):
        """GET /admin/chats НЕ содержит PRIVATE badge / PUBLIC toggle в карточке чата.

        Замечание: 'PRIVATE' и 'PUBLIC' могут встречаться в changelog-модалке
        (историческая справка об упразднении системы). Проверяем только активные
        UI элементы — toggle value (которого точно не должно быть нигде, кроме
        histórical changelog-текста) и badge format.
        """
        r = self.client.get("/admin/chats")
        html = r.text
        # Private toggle value gone — this is the form field value, never in changelog.
        self.assertNotIn('value="private"', html, "Private toggle still present")
        # PRIVATE badge gone (badge format is <span class="badge"...>PRIVATE</span>)
        # We need to be careful — changelog mentions "PRIVATE" in plain text.
        # Look specifically for the badge span format.
        import re
        badge_match = re.search(r'<span class="badge"[^>]*>PRIVATE</span>', html)
        self.assertIsNone(badge_match, "PRIVATE badge span still present")

    def test_14_night_toggle_in_action_buttons(self):
        """NIGHT toggle есть в карточке чата (formaction /toggle field=night_mode)."""
        r = self.client.get("/admin/chats")
        html = r.text
        # Look for the NIGHT toggle button
        self.assertIn('value="night_mode"', html, "night_mode toggle not found")
        # And it should be in action buttons area (with label "NIGHT")
        self.assertIn("NIGHT", html)

    def test_15_san_toggle_in_action_buttons(self):
        """SAN toggle есть в карточке чата (formaction /toggle field=sanitary_days)."""
        r = self.client.get("/admin/chats")
        html = r.text
        self.assertIn('value="sanitary_days"', html, "sanitary_days toggle not found")
        self.assertIn("SAN", html)

    def test_16_sync_admins_in_action_buttons(self):
        """Sync admins кнопка есть в action buttons (SU only)."""
        r = self.client.get("/admin/chats")
        html = r.text
        # Path to sync-admins
        self.assertIn("/sync-admins", html, "sync-admins button not found")
        # Compact label "↻ Sync" (not "↻ Sync admins from TG")
        self.assertIn("↻ Sync", html)

    def test_17_sanitary_add_with_time(self):
        """POST /admin/chats/{id}/sanitary/add создаёт период с временем."""
        r = self.client.post(
            "/admin/chats/-1004760000001/sanitary/add",
            data={
                "start_date": "2026-07-31",
                "end_date": "2026-08-03",
                "start_time": "23:00",
                "end_time": "09:00",
            },
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 303)
        self.assertIn("Sanitary+period+added", r.headers.get("location", ""))
        # Verify in DB
        import asyncio
        async def _check():
            async with async_session() as s:
                cs = (await s.execute(
                    select(ChatSettings).where(ChatSettings.chat_id == -1004760000001)
                )).scalar_one()
                self.assertIsNotNone(cs.sanitary_days)
                periods = get_sanitary_periods_flat(cs.sanitary_days)
                self.assertEqual(len(periods), 1)
                self.assertEqual(periods[0], ["2026-07-31", "2026-08-03", "23:00", "09:00"])
        asyncio.run(_check())

    def test_18_sanitary_add_without_time(self):
        """POST /admin/chats/{id}/sanitary/add без времени — full-day."""
        r = self.client.post(
            "/admin/chats/-1004760000001/sanitary/add",
            data={
                "start_date": "2026-10-15",
                "end_date": "2026-10-15",
                "start_time": "",
                "end_time": "",
            },
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 303)
        import asyncio
        async def _check():
            async with async_session() as s:
                cs = (await s.execute(
                    select(ChatSettings).where(ChatSettings.chat_id == -1004760000001)
                )).scalar_one()
                periods = get_sanitary_periods_flat(cs.sanitary_days)
                self.assertEqual(len(periods), 1)
                # Full-day — no time fields
                self.assertEqual(periods[0], ["2026-10-15", "2026-10-15"])
        asyncio.run(_check())

    def test_19_sanitary_delete(self):
        """POST /admin/chats/{id}/sanitary/{idx}/delete удаляет период."""
        # Add two periods first
        self.client.post(
            "/admin/chats/-1004760000001/sanitary/add",
            data={
                "start_date": "2026-07-31", "end_date": "2026-08-03",
                "start_time": "23:00", "end_time": "09:00",
            },
            follow_redirects=False,
        )
        self.client.post(
            "/admin/chats/-1004760000001/sanitary/add",
            data={
                "start_date": "2026-09-04", "end_date": "2026-09-07",
                "start_time": "23:00", "end_time": "09:00",
            },
            follow_redirects=False,
        )
        # Delete index 0
        r = self.client.post(
            "/admin/chats/-1004760000001/sanitary/0/delete",
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 303)
        self.assertIn("Sanitary+period+deleted", r.headers.get("location", ""))
        import asyncio
        async def _check():
            async with async_session() as s:
                cs = (await s.execute(
                    select(ChatSettings).where(ChatSettings.chat_id == -1004760000001)
                )).scalar_one()
                periods = get_sanitary_periods_flat(cs.sanitary_days)
                self.assertEqual(len(periods), 1)
                # The remaining period should be the September one
                self.assertEqual(periods[0][:2], ["2026-09-04", "2026-09-07"])
        asyncio.run(_check())

    def test_20_toggle_private_invalid(self):
        """POST /admin/chats/{id}/toggle field=private → invalid (упразднено)."""
        r = self.client.post(
            "/admin/chats/-1004760000001/toggle",
            data={"field": "private"},
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 303)
        self.assertIn("Invalid+toggle+field", r.headers.get("location", ""))


# ──────────────────────────────────────────────────────────────────────────
# Static source-code tests
# ──────────────────────────────────────────────────────────────────────────

class TestV476SourceCodePatterns(unittest.TestCase):
    """Проверка исходников на наличие/отсутствие нужных паттернов."""

    def test_21_sanitary_handlers_in_web_app(self):
        """В web_app.py есть handlers /sanitary/add и /sanitary/{idx}/delete."""
        with open(_P("web_app.py")) as f:
            src = f.read()
        self.assertIn("/sanitary/add", src)
        self.assertIn("/sanitary/{idx_str}/delete", src)
        self.assertIn("admin_chats_sanitary_add", src)
        self.assertIn("admin_chats_sanitary_delete", src)

    def test_22_valid_fields_no_private(self):
        """В web_app.py valid_fields не содержит 'private'."""
        with open(_P("web_app.py")) as f:
            src = f.read()
        # Find the valid_fields line
        import re
        m = re.search(r'valid_fields\s*=\s*\{([^}]+)\}', src)
        self.assertIsNotNone(m, "valid_fields not found in web_app.py")
        fields_str = m.group(1)
        self.assertNotIn('"private"', fields_str, "'private' still in valid_fields")

    def test_23_admin_returns_true_not_is_private(self):
        """В bot_handlers.py admin return True (не зависит от is_private)."""
        with open(_P("bot_handlers.py")) as f:
            src = f.read()
        # The active code "return not settings.is_private" should be gone.
        # (Comments mentioning it are OK.)
        # Find the admin role block and verify it returns True.
        import re
        # Look for the admin role block — should contain "return True" not "return not settings.is_private"
        admin_block_match = re.search(
            r'if wu\.role == "admin".*?(?=\n        if wu\.role)',
            src, re.DOTALL,
        )
        self.assertIsNotNone(admin_block_match, "admin role block not found")
        admin_block = admin_block_match.group(0)
        # Active code "return not settings.is_private" should be gone
        # (i.e. only mentioned in comments if at all)
        # Strip comments to check active code only
        active_lines = []
        for line in admin_block.split("\n"):
            stripped = line.split("#")[0].rstrip()
            if stripped:
                active_lines.append(stripped)
        active_block = "\n".join(active_lines)
        self.assertNotIn(
            "return not settings.is_private",
            active_block,
            "Active code 'return not settings.is_private' still present",
        )
        # Should have the new comment + return True
        self.assertIn("v4.7.6", src)
        self.assertIn("return True", admin_block)

    def test_24_changelog_v476(self):
        """В base.html changelog содержит v4.7.6."""
        with open(_P("templates/base.html")) as f:
            src = f.read()
        self.assertIn("<strong>v4.7.6</strong>", src)
        self.assertIn("UI санитарных дней", src)
        # Either form is acceptable: "упразднена система private" (lowercase)
        # or "Упразднена система private" (capital — start of sentence).
        src_lower = src.lower()
        self.assertTrue(
            "упразднена система private" in src_lower,
            "private cleanup mention not found in changelog",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
