"""
v4.7.7 — тесты хотфикса formnovalidate.

Проблема v4.7.6: toggle-кнопки (SAN/NIGHT/CAS/LINK/ENABLE/REPORT) жили в той
же /update форме, что и required-поля sanitary-периода (start_date, end_date).
При нажатии toggle через formaction браузер валидировал ВСЮ форму и блокировал
сабмит с сообщением «Вы пропустили это поле».

Решение: formnovalidate на всех toggle-кнопках и кнопках удаления периодов.

Тесты:
  1. APP_VERSION = "v4.7.7"
  2. В рендере /admin/chats КАЖДАЯ toggle-кнопка (formaction=.../toggle) содержит formnovalidate
  3. Кнопка "+ Add" (formaction=.../sanitary/add) содержит formnovalidate
  4. Кнопка ✕ (formaction=.../sanitary/{idx}/delete) содержит formnovalidate
  5. Кнопка "Сохранить" (без formaction, главный submit) НЕ содержит formnovalidate —
     валидация сохраняется для неё
  6. POST /admin/chats/{id}/toggle с field=sanitary_days РЕАЛЬНО работает (303 redirect)
  7. POST /admin/chats/{id}/sanitary/add с пустыми датами → flash с ошибкой
     (серверная валидация работает)
  8. Changelog содержит v4.7.7
  9-12. Статический анализ шаблона и base.html
"""
from _paths import _P  # noqa: E402  (корень вычисляется от __file__)

import os
import sys
import re
import json
import asyncio
import unittest
from html.parser import HTMLParser

sys.path.insert(0, _P())
sys.path.insert(0, _P("tests"))

_DB_PATH = "/tmp/test_v477_formnovalidate.db"
if os.path.exists(_DB_PATH):
    os.remove(_DB_PATH)

os.environ["BOT_TOKEN"] = "0:fake"
os.environ["ADMIN_IDS"] = "1"
os.environ["SU_IDS"] = "1"
os.environ["WEB_PASSWORD"] = "test_password_123"
os.environ["DB_PATH"] = _DB_PATH

from unittest.mock import MagicMock, AsyncMock
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
        s.add(ChatSettings(
            chat_id=-1004770000001,
            title="Test Chat v4.7.7",
            is_enabled=True,
        ))
        await s.commit()


class ToggleFormnovalidateParser(HTMLParser):
    """Парсер: ищет все <button> и категоризирует их."""

    def __init__(self):
        super().__init__()
        self.toggle_buttons = []
        self.sanitary_add_buttons = []
        self.sanitary_delete_buttons = []
        self.save_buttons = []     # buttons with type=submit but NO formaction

    def handle_starttag(self, tag, attrs):
        if tag != "button":
            return
        attr_d = dict(attrs)
        if attr_d.get("type") != "submit":
            return
        formaction = attr_d.get("formaction", "")
        if "/toggle" in formaction:
            self.toggle_buttons.append({
                "formaction": formaction,
                "has_formnovalidate": "formnovalidate" in attr_d,
                "name": attr_d.get("name"),
                "value": attr_d.get("value"),
            })
        elif "/sanitary/add" in formaction:
            self.sanitary_add_buttons.append({
                "has_formnovalidate": "formnovalidate" in attr_d,
            })
        elif "/sanitary/" in formaction and "/delete" in formaction:
            self.sanitary_delete_buttons.append({
                "has_formnovalidate": "formnovalidate" in attr_d,
                "formaction": formaction,
            })
        elif not formaction:
            # Plain submit button (likely "Сохранить")
            self.save_buttons.append({
                "has_formnovalidate": "formnovalidate" in attr_d,
            })


class TestV477Formnovalidate(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await _seed()
        web_app._check_login_rate_limit = lambda ip: True
        self._mock_bot = MagicMock()
        self._mock_bot.get_chat_administrators = AsyncMock(return_value=[])
        self.client = TestClient(web_app.create_app(bot=self._mock_bot))
        r = self.client.post("/login", data={
            "username": "su", "password": "test_password_123",
        }, follow_redirects=False)
        assert r.status_code in (303, 302), f"Login failed: {r.status_code}"

    async def asyncTearDown(self):
        try:
            self.client.close()
        except Exception:
            pass

    # ──────────── 1. Version ────────────

    def test_01_app_version_is_v477(self):
        # v4.7.10+: APP_VERSION bumped beyond v4.7.7. This test now verifies
        # that we're at least on v4.7.7 (when formnovalidate fix shipped),
        # so it doesn't break on every future version bump.
        v = web_app.APP_VERSION
        m = re.match(r"^v(\d+)\.(\d+)\.(\d+)$", v)
        self.assertIsNotNone(m, f"APP_VERSION format unexpected: {v!r}")
        major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
        self.assertTrue(
            (major, minor, patch) >= (4, 7, 7),
            f"APP_VERSION {v} should be >= v4.7.7 (formnovalidate fix)"
        )

    # ──────────── 2. Toggle buttons have formnovalidate ────────────

    def test_02_all_toggle_buttons_have_formnovalidate(self):
        r = self.client.get("/admin/chats")
        self.assertEqual(r.status_code, 200)
        parser = ToggleFormnovalidateParser()
        parser.feed(r.text)
        self.assertGreaterEqual(
            len(parser.toggle_buttons), 6,
            f"Expected ≥6 toggle buttons, got {len(parser.toggle_buttons)}",
        )
        for btn in parser.toggle_buttons:
            self.assertTrue(
                btn["has_formnovalidate"],
                f"Toggle button {btn['name']}={btn['value']} missing formnovalidate",
            )

    # ──────────── 3. Sanitary Add button has formnovalidate ────────────

    def test_03_sanitary_add_button_has_formnovalidate(self):
        r = self.client.get("/admin/chats")
        self.assertEqual(r.status_code, 200)
        parser = ToggleFormnovalidateParser()
        parser.feed(r.text)
        self.assertGreaterEqual(
            len(parser.sanitary_add_buttons), 1,
            "Expected ≥1 '+ Add' sanitary button",
        )
        for btn in parser.sanitary_add_buttons:
            self.assertTrue(
                btn["has_formnovalidate"],
                "+ Add button missing formnovalidate",
            )

    # ──────────── 4. Sanitary delete buttons have formnovalidate ────────────

    def test_04_sanitary_delete_buttons_have_formnovalidate_if_any(self):
        # Add a period first so ✕ button appears
        self.client.post(
            "/admin/chats/-1004770000001/sanitary/add",
            data={
                "start_date": "2026-07-31",
                "end_date": "2026-08-03",
                "start_time": "23:00",
                "end_time": "09:00",
            },
            follow_redirects=False,
        )
        r = self.client.get("/admin/chats")
        self.assertEqual(r.status_code, 200)
        parser = ToggleFormnovalidateParser()
        parser.feed(r.text)
        self.assertGreaterEqual(
            len(parser.sanitary_delete_buttons), 1,
            "Expected ≥1 sanitary delete button after adding a period",
        )
        for btn in parser.sanitary_delete_buttons:
            self.assertTrue(
                btn["has_formnovalidate"],
                f"Sanitary delete button {btn['formaction']} missing formnovalidate",
            )

    # ──────────── 5. Save button does NOT have formnovalidate ────────────

    def test_05_save_button_has_no_formnovalidate(self):
        r = self.client.get("/admin/chats")
        self.assertEqual(r.status_code, 200)
        parser = ToggleFormnovalidateParser()
        parser.feed(r.text)
        self.assertGreaterEqual(
            len(parser.save_buttons), 1,
            "Expected ≥1 plain submit button (Сохранить)",
        )
        for btn in parser.save_buttons:
            self.assertFalse(
                btn["has_formnovalidate"],
                "Save button should NOT have formnovalidate",
            )

    # ──────────── 6. Toggle sanitary_days works (303) ────────────

    def test_06_toggle_sanitary_days_works(self):
        r = self.client.post(
            "/admin/chats/-1004770000001/toggle",
            data={"field": "sanitary_days"},
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 303)
        self.assertIn("/admin/chats", r.headers.get("location", ""))

    # ──────────── 7. Server-side validation: empty dates → error flash ────────────

    def test_07_sanitary_add_empty_dates_returns_error_flash(self):
        r = self.client.post(
            "/admin/chats/-1004770000001/sanitary/add",
            data={
                "start_date": "",
                "end_date": "",
                "start_time": "",
                "end_time": "",
            },
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 303)
        loc = r.headers.get("location", "")
        self.assertTrue(
            "Sanitary+add+failed" in loc or "Invalid" in loc,
            f"Expected error flash in location, got: {loc}",
        )

    # ──────────── 8. Changelog mentions v4.7.7 ────────────

    def test_08_changelog_mentions_v477(self):
        r = self.client.get("/admin")
        if r.status_code != 200:
            r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("v4.7.7", r.text)
        self.assertIn("formnovalidate", r.text.lower())


class TestV477SourceCodePatterns(unittest.TestCase):

    def test_09_template_has_formnovalidate_on_toggle(self):
        with open(_P("templates/admin_chats.html")) as f:
            content = f.read()
        toggle_btn_re = re.compile(
            r'<button[^>]*formaction="[^"]*/toggle"[^>]*>',
            re.IGNORECASE,
        )
        matches = toggle_btn_re.findall(content)
        self.assertGreaterEqual(len(matches), 6)
        for m in matches:
            self.assertIn(
                "formnovalidate", m,
                f"Toggle button missing formnovalidate: {m[:120]}...",
            )

    def test_10_template_sanitary_add_has_formnovalidate(self):
        with open(_P("templates/admin_chats.html")) as f:
            content = f.read()
        add_btn_re = re.compile(
            r'<button[^>]*formaction="[^"]*/sanitary/add"[^>]*>',
            re.IGNORECASE,
        )
        matches = add_btn_re.findall(content)
        self.assertGreaterEqual(len(matches), 1)
        for m in matches:
            self.assertIn(
                "formnovalidate", m,
                f"Add button missing formnovalidate: {m[:120]}...",
            )

    def test_11_template_sanitary_delete_has_formnovalidate(self):
        with open(_P("templates/admin_chats.html")) as f:
            content = f.read()
        del_btn_re = re.compile(
            r'<button[^>]*formaction="[^"]*/sanitary/\{\{[^"]*\}\}/delete"[^>]*>',
            re.IGNORECASE,
        )
        matches = del_btn_re.findall(content)
        self.assertGreaterEqual(len(matches), 1)
        for m in matches:
            self.assertIn(
                "formnovalidate", m,
                f"Delete button missing formnovalidate: {m[:120]}...",
            )

    def test_12_changelog_v477_in_base_html(self):
        with open(_P("templates/base.html")) as f:
            content = f.read()
        self.assertIn("v4.7.7", content)
        self.assertIn("formnovalidate", content.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
