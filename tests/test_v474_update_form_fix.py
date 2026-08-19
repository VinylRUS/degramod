"""
test_v474_update_form_fix.py — E2E: POST /admin/chats/{id}/update реально
сохраняет настройки чата в БД.

Контекст: в v4.7.3 (и ранее) в шаблоне admin_chats.html главная форма
/admin/chats/{id}/update содержала ВНУТРИ себя несколько других <form>
для toggle-кнопок (Enabled/CAS/Link-filter/Private/Make report + sync-admins
+ night_mode toggle + sanitary_days toggle). HTML запрещает вложённые <form> —
браузер при парсинге автоматически закрывает внешнюю форму перед первой
вложенной. В итоге /update форма реально содержала только поля ДО первого
toggle (никаких), и при нажатии «Сохранить» сабмитилась пустая форма.

v4.7.4: inline toggle <form> заменены на <button formaction="/toggle" formmethod="post">
внутри главной /update формы. formaction переопределяет action родительской
формы при submit — кнопка делает POST на /toggle, а не на /update.
"""
from _paths import _P  # noqa: E402  (корень вычисляется от __file__)
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, AsyncMock, patch

sys.path.insert(0, _P())
os.chdir(_P())

_DB_PATH = tempfile.mktemp(suffix="_v474.db")
os.environ["BOT_TOKEN"] = "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
os.environ["ADMIN_IDS"] = "1"
os.environ["WEB_PASSWORD"] = "test_password_123"
os.environ["DB_PATH"] = _DB_PATH

from sqlalchemy import select, text
from db import (
    init_db, async_session, ChatSettings, WebUser, engine,
    PermissionPreset,
)
import web_app
import bot_handlers as bh  # noqa: F401

from fastapi.testclient import TestClient


async def _seed():
    await init_db()
    async with async_session() as s:
        await s.execute(text("DELETE FROM permission_presets"))
        await s.execute(text("DELETE FROM chat_settings"))
        await s.execute(text("DELETE FROM web_users WHERE username != 'su'"))
        await s.execute(text("DELETE FROM chat_admins"))
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
        # Chat to update
        cs = ChatSettings(
            chat_id=-1004740000001,
            title="Test Chat v4.7.4",
            is_enabled=True,
            warns_to_mute=3,
            mute_duration_seconds=3600,
            warns_to_ban=5,
            night_mode_start="23:00",
            night_mode_end="07:00",
        )
        s.add(cs)
        await s.commit()


class TestV474UpdateFormFix(unittest.IsolatedAsyncioTestCase):

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
        assert r.status_code in (303, 200)

    async def asyncTearDown(self):
        try:
            engine.sync_engine.dispose()
        except Exception:
            pass
        if os.path.exists(_DB_PATH):
            os.remove(_DB_PATH)

    # ── Test 1: APP_VERSION bumped ──────────────────────────────────
    async def test_app_version_is_v474(self):
        # v4.10.0: FIX сравнение строк ломалось на двузначном minor
        # ("v4.10.0" < "v4.7.x" лексикографически) — сравниваем как кортеж чисел.
        self.assertGreaterEqual(tuple(int(p) for p in web_app.APP_VERSION.lstrip("v").split(".")), tuple(int(p) for p in "v4.7.4".lstrip("v").split(".")),
            f"APP_VERSION={web_app.APP_VERSION} should be >= v4.7.4")

    # ── Test 2: POST /admin/chats/{id}/update сохраняет hashtag ─────
    async def test_update_saves_hashtag(self):
        """Главная форма /update должна реально сохранять hashtag в БД.

        До v4.7.4 это падало: форма обрывалась на первой вложенной toggle-форме,
        и POST уходил пустым (без полей hashtag/warns_to_mute/etc).
        """
        # Submit the update form with a new hashtag
        r = self.client.post(
            f"/admin/chats/-1004740000001/update",
            data={
                "hashtag": "#TestV474",
                "report_chat_id": "",
                "warns_to_mute": "5",
                "mute_duration_seconds": "7200",
                "warns_to_ban": "10",
                "warn_decay_days": "30",
                "link_filter_action": "warn",
                "night_mode_start": "22:00",
                "night_mode_end": "08:00",
                "night_mode_tz": "Europe/Moscow",
                "night_mode_weekend_start": "",
                "night_mode_weekend_end": "",
                "night_mode_notify": "",
                "night_mode_notify_enter_msg": "",
                "night_mode_notify_exit_msg": "",
                "sanitary_days_text": "",
                "day_preset_id": "__none__",
                "night_preset_id": "__none__",
                "sanitary_preset_id": "__lockdown__",
            },
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 303,
                         f"Expected 303 redirect, got {r.status_code}: {r.text[:300]}")
        loc = r.headers.get("location", "")
        self.assertIn("settings+updated", loc,
                      f"Flash should mention 'settings updated': {loc}")

        # Verify DB
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1004740000001)
            )).scalar_one()
            self.assertEqual(cs.hashtag, "#TestV474",
                             f"hashtag should be saved, got {cs.hashtag!r}")
            self.assertEqual(cs.warns_to_mute, 5,
                             f"warns_to_mute should be 5, got {cs.warns_to_mute}")
            self.assertEqual(cs.mute_duration_seconds, 7200,
                             f"mute_duration_seconds should be 7200, got {cs.mute_duration_seconds}")
            self.assertEqual(cs.warns_to_ban, 10,
                             f"warns_to_ban should be 10, got {cs.warns_to_ban}")
            self.assertEqual(cs.warn_decay_days, 30,
                             f"warn_decay_days should be 30, got {cs.warn_decay_days}")
            self.assertEqual(cs.link_filter_action, "warn",
                             f"link_filter_action should be 'warn', got {cs.link_filter_action}")
            self.assertEqual(cs.night_mode_start, "22:00",
                             f"night_mode_start should be 22:00, got {cs.night_mode_start}")

    # ── Test 3: POST /update с пустым hashtag сбрасывает его ─────────
    async def test_update_clears_hashtag_when_empty(self):
        # First set a hashtag
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1004740000001)
            )).scalar_one()
            cs.hashtag = "#OldTag"
            await s.commit()

        # Now submit empty hashtag
        r = self.client.post(
            f"/admin/chats/-1004740000001/update",
            data={
                "hashtag": "",
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
                "night_preset_id": "__none__",
                "sanitary_preset_id": "__lockdown__",
            },
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 303)

        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1004740000001)
            )).scalar_one()
            self.assertIsNone(cs.hashtag,
                              f"hashtag should be None after empty submit, got {cs.hashtag!r}")

    # ── Test 4: В шаблоне нет вложённых <form> ───────────────────────
    async def test_no_nested_forms_in_rendered_html(self):
        """Рендерим /admin/chats и проверяем что в HTML нет <form> внутри <form>."""
        r = self.client.get("/admin/chats", follow_redirects=False)
        self.assertEqual(r.status_code, 200)
        html = r.text

        # Уберём Jinja-комментарии (уже зарендеренные, но на всякий случай)
        import re
        # Симулируем браузерный парсинг
        form_opens = [(m.start(), re.search(r'action="([^"]+)"', m.group(0)).group(1) if re.search(r'action="([^"]+)"', m.group(0)) else "(none)")
                      for m in re.finditer(r'<form\b[^>]*>', html)]
        form_closes = [m.start() for m in re.finditer(r'</form>', html)]

        self.assertEqual(len(form_opens), len(form_closes),
                         f"Unbalanced forms: {len(form_opens)} opens vs {len(form_closes)} closes")

        stack = []
        nested = []
        events = [("open", p, a) for p, a in form_opens] + [("close", p, None) for p in form_closes]
        events.sort(key=lambda e: e[1])
        for kind, pos, action in events:
            if kind == "open":
                if stack:
                    parent_pos, parent_action = stack.pop()
                    nested.append((parent_action, action))
                stack.append((pos, action))
            else:
                if stack:
                    stack.pop()

        self.assertEqual(len(nested), 0,
                         f"Nested <form> detected in rendered HTML! "
                         f"Browser would auto-close outer form. Violations: {nested}")

    # ── Test 5: Все toggle-кнопки используют formaction (а не вложённые form) ─
    async def test_toggle_buttons_use_formaction(self):
        """В HTML рендере toggle-кнопки должны использовать formaction, не <form>."""
        r = self.client.get("/admin/chats", follow_redirects=False)
        html = r.text
        # formaction="/admin/chats/.../toggle" должен встречаться хотя бы 5 раз
        # (enabled, cas, link_filter, private, make_report, night_mode, sanitary_days)
        import re
        formaction_count = len(re.findall(r'formaction="/admin/chats/[^/]+/toggle"', html))
        self.assertGreaterEqual(formaction_count, 5,
                                f"Expected ≥5 toggle buttons with formaction, got {formaction_count}")

    # ── Test 6: Hashtag поле внутри update-формы в HTML рендере ──────
    async def test_hashtag_inside_update_form_in_rendered_html(self):
        """В HTML рендере поле hashtag должно быть между <form action=update>
        и </form> (а не после вложенной <form>, как было до v4.7.4)."""
        r = self.client.get("/admin/chats", follow_redirects=False)
        html = r.text
        import re

        # Найти все <form action="/admin/chats/.../update">
        # Для каждого — найти конец (ближайший </form>) и проверить что hashtag внутри
        update_forms = list(re.finditer(
            r'<form\b[^>]*action="/admin/chats/[^/]+/update"[^>]*>',
            html,
        ))
        self.assertGreater(len(update_forms), 0, "No /update form in rendered HTML")

        hashtag_inside_count = 0
        for uf in update_forms:
            start = uf.end()
            # Ближайший </form>
            close_m = re.search(r'</form>', html[start:])
            if close_m is None:
                continue
            end = start + close_m.start()
            # Hashtag field внутри?
            if 'name="hashtag"' in html[start:end]:
                hashtag_inside_count += 1

        self.assertEqual(hashtag_inside_count, len(update_forms),
                         f"Only {hashtag_inside_count}/{len(update_forms)} update forms "
                         f"contain hashtag field inside. Expected all.")

    # ── Test 7: Changelog содержит v4.7.4 ───────────────────────────
    async def test_changelog_mentions_v474(self):
        r = self.client.get("/dashboard", follow_redirects=False)
        html = r.text
        self.assertIn("v4.7.4", html, "Changelog must mention v4.7.4")
        self.assertIn("nested", html.lower(), "Changelog must mention nested forms bug")


if __name__ == "__main__":
    unittest.main(verbosity=2)
