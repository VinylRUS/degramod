"""
test_v490_dashboard_pagination.py — регресс на падение дашборда при пагинации.

Прод-инцидент 19 августа 2026: GET /dashboard отдавал 500 с
`jinja2.exceptions.UndefinedError: 'abs' is undefined`.

Причина: `templates/dashboard.html` в блоке пагинации звал `abs(p - page)`.
`abs` — builtin Python, но Jinja2 builtins в шаблоны НЕ пробрасывает: в
окружении лежат только явно положенные в `env.globals` (`app_version`,
`app_release_date`, `csrf_field`). Правильная форма — фильтр `|abs`.

Почему не ловилось раньше — двойное условие:

  1. Блок стоит под `{% if total_pages > 1 %}` — нужна вторая страница,
     то есть больше PAGE_SIZE (50) записей. Существующие тесты дашборда
     сеют 1-3 наказания.
  2. Даже с пагинацией `abs()` вычисляется не всегда: условие
     `p <= 3 or p >= total_pages - 2 or abs(p - page) <= 1` короткозамыкающее.
     Пока страниц мало, каждая `p` попадает в первые два предиката, и до
     `abs` дело не доходит. Первая `p`, доходящая до третьего предиката,
     появляется только при **7 страницах** (это `p = 4`).

Итого падение начинается с 7 страниц, то есть с 301 записи. Баг пролежал
с 23 июля 2026 и «выстрелил», когда прод накопил столько наказаний.

Тест сеет 350 наказаний — 7 полных страниц.
"""

from __future__ import annotations
from _paths import _P  # noqa: E402

import os
import sys
import unittest
from unittest.mock import MagicMock

os.environ.setdefault("DB_PATH", ":memory:")
os.environ.setdefault("BOT_TOKEN", "test:token")
os.environ["WEB_PASSWORD"] = "test-pwd"
os.environ.setdefault("SESSION_SECRET", "test-secret-xxxxxxxxxxxxxxxxxxxxx")
os.environ["ADMIN_IDS"] = "111111111"

sys.path.insert(0, _P())

from sqlalchemy import delete  # noqa: E402

from db import (  # noqa: E402
    ChatSettings, Moderator, Punishment, User, WebUser, async_session, init_db,
)

# PAGE_SIZE в web_app.py = 50. Нужно 7 страниц, чтобы хоть одна `p` дошла до
# предиката с abs() — при меньшем числе страниц срабатывает короткое замыкание.
_ROWS = 350


class TestDashboardPagination(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await init_db()
        async with async_session() as s:
            await s.execute(delete(Punishment))
            await s.execute(delete(ChatSettings))
            await s.execute(delete(User))
            await s.execute(delete(Moderator))
            await s.execute(delete(WebUser))
            await s.commit()

        import web_app
        web_app._check_login_rate_limit = lambda ip: True

        async with async_session() as s:
            s.add(WebUser(username="su", is_su=True, is_active=True,
                          role="su", created_by="system"))
            s.add(User(user_id=1001, username="badguy"))
            s.add(Moderator(mod_id=999, username="admin"))
            for i in range(_ROWS):
                s.add(Punishment(
                    user_id=1001, mod_id=999, chat_id=-100,
                    action_type="warn", duration_seconds=1,
                    reason=f"нарушение {i}", is_revoked=False,
                ))
            await s.commit()

        from httpx import ASGITransport, AsyncClient
        self.app = web_app.create_app(bot=MagicMock())
        self.client = AsyncClient(
            transport=ASGITransport(app=self.app), base_url="http://test",
        )
        resp = await self.client.post(
            "/login", data={"username": "su", "password": "test-pwd"},
            follow_redirects=False,
        )
        assert resp.status_code == 303, f"login failed: {resp.status_code}"

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_dashboard_renders_with_pagination(self):
        """Дашборд отдаёт 200 при 7 страницах.

        До фикса падал 500: Jinja не знает функцию abs().
        """
        resp = await self.client.get("/dashboard", follow_redirects=False)
        self.assertEqual(
            resp.status_code, 200,
            "дашборд должен рендериться при включённой пагинации",
        )

    async def test_pagination_block_rendered(self):
        """Блок пагинации реально попал в вывод — иначе тест ничего не проверяет.

        Без этой проверки тест позеленел бы и при total_pages == 1, то есть
        не доказывал бы, что проблемная ветка шаблона выполнилась.
        """
        resp = await self.client.get("/dashboard", follow_redirects=False)
        self.assertIn('class="pagination"', resp.text)
        # Многоточие рендерится только когда страниц достаточно, чтобы часть
        # номеров схлопнулась — то есть ровно в том режиме, где вычисляется abs().
        self.assertIn("ellipsis", resp.text)

    async def test_middle_page_renders(self):
        """Средняя страница открывается: там abs(p - page) считается для нескольких p."""
        resp = await self.client.get("/dashboard?page=4", follow_redirects=False)
        self.assertEqual(resp.status_code, 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
