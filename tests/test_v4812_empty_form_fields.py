"""
test_v4812_empty_form_fields.py — пустое поле формы даёт понятную ошибку,
а не сырой 422.

Контекст. До обновления FastAPI (0.115.6 → 0.141.1, Starlette 0.41 → 1.6)
пустая строка в `Form(...)` считалась переданным значением: хендлер получал
`""`, сам его проверял и отвечал редиректом с flash-сообщением вроде
«Invalid preset name». В новом стеке пустое значение приравнено к
отсутствующему, поэтому запрос отсекается валидацией FastAPI ещё до
обработчика — пользователь вместо формы с подсказкой получает JSON
`{"detail":[{"type":"missing",...}]}`.

Затронуты все роуты, где пользователь заполняет текстовое поле руками.
Тесты фиксируют контракт: незаполненное поле возвращает пользователя на
страницу с объяснением.

Числовые поля (`punishment_id`, `user_id`, `chat_id`) сюда не входят: они
приходят из сгенерированных форм, руками не набираются, и 422 для них —
корректная реакция.
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest

from _paths import _P

sys.path.insert(0, _P())

os.environ.setdefault("BOT_TOKEN", "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw")
os.environ.setdefault("ADMIN_IDS", "111111111")
os.environ["WEB_PASSWORD"] = "test-su-password-12345"
os.environ.setdefault("SESSION_SECRET", "test-secret-xxxxxxxxxxxxxxxxxxxxx")
os.environ["WEB_ALLOW_NO_SECRET"] = "1"
os.environ["WEB_COOKIE_SECURE"] = "0"

from fastapi.testclient import TestClient  # noqa: E402

import web_app  # noqa: E402
from db import init_db  # noqa: E402


class TestEmptyFormFieldsGiveFriendlyError(unittest.TestCase):
    """Незаполненное текстовое поле → редирект с объяснением, не 422."""

    @classmethod
    def setUpClass(cls):
        asyncio.run(init_db())
        cls.app = web_app.create_app(bot=None)

    def setUp(self):
        self.client = TestClient(self.app)
        web_app._check_login_rate_limit = lambda ip: True
        r = self.client.post(
            "/login",
            data={"username": "su", "password": "test-su-password-12345"},
            follow_redirects=False,
        )
        self.assertIn(r.status_code, (200, 303), "не удалось залогиниться как SU")

    def _post(self, url: str, data: dict):
        return self.client.post(url, data=data, follow_redirects=False)

    def _assert_friendly(self, r, url: str):
        """Ответ объясняет проблему, а не отдаёт машинный 422."""
        self.assertNotEqual(
            r.status_code, 422,
            f"{url}: пустое поле должно обрабатываться хендлером, "
            f"а не отсекаться валидацией: {r.text[:200]}",
        )
        self.assertEqual(
            r.status_code, 303,
            f"{url}: ожидался редирект с flash, получили {r.status_code}",
        )

    def test_preset_create_empty_name(self):
        r = self._post("/admin/presets/create", {"name": "", "scope": "night"})
        self._assert_friendly(r, "/admin/presets/create")

    def test_preset_edit_empty_name(self):
        r = self._post("/admin/presets/1/edit", {"name": "", "scope": "night"})
        self._assert_friendly(r, "/admin/presets/1/edit")

    def test_words_add_empty_pattern(self):
        r = self._post("/admin/presets/words/add",
                       {"pattern": "", "chat_id": "-1001234", "action": "warn"})
        self._assert_friendly(r, "/admin/presets/words/add")

    def test_links_add_empty_domain(self):
        r = self._post("/admin/presets/links/add",
                       {"domain": "", "chat_id": "-1001234"})
        self._assert_friendly(r, "/admin/presets/links/add")

    def test_user_create_empty_tg_id(self):
        r = self._post("/admin/users/create", {"tg_user_id": ""})
        self._assert_friendly(r, "/admin/users/create")

    def test_password_reset_empty(self):
        """Пустой пароль отбивается проверкой длины, а не валидацией схемы."""
        r = self._post("/admin/users/1/reset", {"password": ""})
        self._assert_friendly(r, "/admin/users/1/reset")

    def test_change_password_empty(self):
        r = self._post("/me/password",
                       {"old_password": "", "new_password": "", "confirm": ""})
        self._assert_friendly(r, "/me/password")


if __name__ == "__main__":
    unittest.main(verbosity=2)
