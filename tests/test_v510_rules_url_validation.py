"""
test_v510_rules_url_validation.py — E2E: /admin/chats/{id}/update валидирует
rules_url перед сохранением.

Контекст (финальное ревью v5.1.0, находка «Мелочи»): chat_settings.rules_url
не валидировался. Кривое значение (без схемы, случайный текст) сохранялось
молча — Telegram отвергал ссылку при отправке /rules, _send_ephemeral глушил
ошибку, и /rules тихо переставал работать без единого объяснения в
интерфейсе.

Фикс: web/admin_chats.py — startswith(("http://", "https://")) с редиректом
и понятным flash-сообщением при нарушении. Пусто по-прежнему разрешено
(означает «использовать RULES_URL_DEFAULT»).

Запуск: uv run python tools/run_tests.py -k v510_rules_url_validation
"""
from _paths import _P  # noqa: E402
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, _P())
os.chdir(_P())

_DB_PATH = tempfile.mktemp(suffix="_v510_rules_url.db")
os.environ["BOT_TOKEN"] = "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
os.environ["ADMIN_IDS"] = "1"
os.environ["WEB_PASSWORD"] = "test_password_123"
os.environ["DB_PATH"] = _DB_PATH

from sqlalchemy import select  # noqa: E402
from db import init_db, async_session, ChatSettings, WebUser, engine  # noqa: E402
import web_app  # noqa: E402
import bot_handlers as bh  # noqa: E402,F401

from fastapi.testclient import TestClient  # noqa: E402

_CHAT_ID = -1005100000001

# Набор полей формы /update, минимально достаточный чтобы пройти остальную
# валидацию (взято из test_v474_update_form_fix.py — рабочий baseline).
_BASE_FORM = {
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
}


async def _seed():
    await init_db()
    async with async_session() as s:
        existing_su = (await s.execute(
            select(WebUser).where(WebUser.username == "su")
        )).scalar_one_or_none()
        if existing_su is None:
            s.add(WebUser(username="su", is_su=True, is_active=True,
                          role="su", created_by="system"))
            await s.commit()
        cs = ChatSettings(
            chat_id=_CHAT_ID,
            title="Test Chat v5.1.0 rules_url",
            is_enabled=True,
            warns_to_mute=3,
            mute_duration_seconds=3600,
            warns_to_ban=5,
            night_mode_start="23:00",
            night_mode_end="07:00",
            rules_url="https://example.org/original-rules",
        )
        s.add(cs)
        await s.commit()


class TestRulesUrlValidation(unittest.IsolatedAsyncioTestCase):

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

    async def _get_rules_url(self) -> str | None:
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == _CHAT_ID)
            )).scalar_one()
            return cs.rules_url

    async def test_valid_https_url_saved(self):
        data = dict(_BASE_FORM, rules_url="https://example.org/rules")
        r = self.client.post(
            f"/admin/chats/{_CHAT_ID}/update", data=data, follow_redirects=False,
        )
        self.assertEqual(r.status_code, 303, r.text[:300])
        self.assertEqual(await self._get_rules_url(), "https://example.org/rules")

    async def test_valid_http_url_saved(self):
        data = dict(_BASE_FORM, rules_url="http://example.org/rules")
        r = self.client.post(
            f"/admin/chats/{_CHAT_ID}/update", data=data, follow_redirects=False,
        )
        self.assertEqual(r.status_code, 303, r.text[:300])
        self.assertEqual(await self._get_rules_url(), "http://example.org/rules")

    async def test_empty_url_clears_to_default(self):
        data = dict(_BASE_FORM, rules_url="")
        r = self.client.post(
            f"/admin/chats/{_CHAT_ID}/update", data=data, follow_redirects=False,
        )
        self.assertEqual(r.status_code, 303, r.text[:300])
        self.assertIsNone(await self._get_rules_url())

    async def test_schemeless_url_rejected_and_settings_unchanged(self):
        """Без http(s):// — запрос отклоняется, старое значение не тронуто."""
        data = dict(_BASE_FORM, rules_url="example.org/rules")
        r = self.client.post(
            f"/admin/chats/{_CHAT_ID}/update", data=data, follow_redirects=False,
        )
        self.assertEqual(r.status_code, 303)
        loc = r.headers.get("location", "")
        self.assertIn("rules_url", loc)
        self.assertEqual(
            await self._get_rules_url(), "https://example.org/original-rules",
            "невалидный rules_url не должен был перезаписать старое значение",
        )

    async def test_garbage_text_rejected_and_settings_unchanged(self):
        data = dict(_BASE_FORM, rules_url="не ссылка вообще")
        r = self.client.post(
            f"/admin/chats/{_CHAT_ID}/update", data=data, follow_redirects=False,
        )
        self.assertEqual(r.status_code, 303)
        self.assertEqual(
            await self._get_rules_url(), "https://example.org/original-rules",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
