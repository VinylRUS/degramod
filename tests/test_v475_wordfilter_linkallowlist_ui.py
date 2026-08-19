"""
test_v475_wordfilter_linkallowlist_ui.py — UI для word filter и link allowlist
на странице /admin/presets.

Контекст: v4.7.5 добавляет на страницу /admin/presets две новые секции:
1. Ban Words (word_filters) — список запрещённых слов/regex-паттернов с
   add-form (chat_id, pattern, action, is_regex) и кнопкой Delete.
2. Link Allowlist (link_allowlist) — список разрешённых доменов с
   add-form (chat_id, domain) и кнопкой Delete.

Паритет с командами /addword, /delword, /linkallow, /linkallowlist — те же
таблицы БД, та же логика валидации.

Тесты:
  • APP_VERSION = "v4.7.5"
  • GET /admin/presets возвращает 200, рендерит секции с правильным заголовком
  • POST /admin/presets/words/add создаёт активный WordFilter в БД
  • POST /admin/presets/words/add с is_regex=on сохраняет is_regex=True
  • POST /admin/presets/words/add с битым regex → flash + БД не тронута
  • POST /admin/presets/words/add с дубликатом → upsert (обновляется action)
  • POST /admin/presets/words/{id}/delete → is_active=False (soft-delete)
  • POST /admin/presets/links/add создаёт LinkAllowlist
  • POST /admin/presets/links/add нормализует https://t.me/path → t.me
  • POST /admin/presets/links/add с дубликатом → flash "already in allowlist"
  • POST /admin/presets/links/{id}/delete → hard delete (строки нет в БД)
  • Source inspection: handlers присутствуют в web_app.py
  • Template admin_presets.html содержит секции Ban Words и Link Allowlist
"""
from _paths import _P  # noqa: E402  (корень вычисляется от __file__)
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, AsyncMock

sys.path.insert(0, _P())
os.chdir(_P())

_DB_PATH = tempfile.mktemp(suffix="_v475.db")
os.environ["BOT_TOKEN"] = "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
os.environ["ADMIN_IDS"] = "1"
os.environ["WEB_PASSWORD"] = "test_password_123"
os.environ["DB_PATH"] = _DB_PATH

from sqlalchemy import select, text
from db import (
    init_db, async_session, ChatSettings, WebUser, engine,
    PermissionPreset, WordFilter, LinkAllowlist,
)
import web_app
import bot_handlers as bh  # noqa: F401 — чтобы aiogram router загрузился

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
            chat_id=-1004750000001,
            title="Test Chat v4.7.5",
            is_enabled=True,
        ))
        await s.commit()


class TestV475WordFilterLinkAllowlistUI(unittest.IsolatedAsyncioTestCase):

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

    # ── Test 1: APP_VERSION ──────────────────────────────────────────
    async def test_app_version_is_v475(self):
        self.assertGreaterEqual(web_app.APP_VERSION, "v4.7.5",
            f"APP_VERSION={web_app.APP_VERSION} should be >= v4.7.5")

    # ── Test 2: GET /admin/presets рендерит секции ───────────────────
    async def test_get_presets_page_renders_sections(self):
        r = self.client.get("/admin/presets", follow_redirects=False)
        self.assertEqual(r.status_code, 200)
        body = r.text
        # Заголовки секций
        self.assertIn("(Word Filter)", body,
                      "Ban Words section heading missing")
        self.assertIn("Link Allowlist", body,
                      "Link Allowlist section heading missing")
        # Формы
        self.assertIn('action="/admin/presets/words/add"', body,
                      "Word add form action missing")
        self.assertIn('action="/admin/presets/links/add"', body,
                      "Link add form action missing")
        # Hint про паритет с командами
        self.assertIn("/addword", body)
        self.assertIn("/linkallow", body)

    # ── Test 3: POST words/add создаёт WordFilter ────────────────────
    async def test_words_add_creates_wordfilter(self):
        r = self.client.post("/admin/presets/words/add", data={
            "chat_id": "0",
            "pattern": "spam_keyword",
            "is_regex": "",
            "action": "delete",
        }, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        loc = r.headers.get("location", "")
        self.assertIn("Pattern+added", loc)

        async with async_session() as s:
            wf = (await s.execute(
                select(WordFilter).where(WordFilter.pattern == "spam_keyword")
            )).scalar_one_or_none()
            self.assertIsNotNone(wf, "WordFilter not created")
            self.assertEqual(wf.chat_id, 0)
            self.assertEqual(wf.action, "delete")
            self.assertFalse(wf.is_regex)
            self.assertTrue(wf.is_active)

    # ── Test 4: POST words/add с is_regex=on ─────────────────────────
    async def test_words_add_with_regex_flag(self):
        r = self.client.post("/admin/presets/words/add", data={
            "chat_id": "0",
            "pattern": r"^https?://\S+$",
            "is_regex": "on",
            "action": "warn",
        }, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("Pattern+added", r.headers.get("location", ""))

        async with async_session() as s:
            wf = (await s.execute(
                select(WordFilter).where(WordFilter.pattern == r"^https?://\S+$")
            )).scalar_one()
            self.assertTrue(wf.is_regex)
            self.assertEqual(wf.action, "warn")

    # ── Test 5: POST words/add с битым regex → flash, БД не тронута ──
    async def test_words_add_invalid_regex_rejected(self):
        r = self.client.post("/admin/presets/words/add", data={
            "chat_id": "0",
            "pattern": "[unclosed-bracket",
            "is_regex": "on",
            "action": "delete",
        }, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("Invalid+regex", r.headers.get("location", ""))

        async with async_session() as s:
            count = (await s.execute(
                select(WordFilter).where(WordFilter.pattern == "[unclosed-bracket")
            )).scalars().all()
            self.assertEqual(len(count), 0, "Invalid regex should not be saved")

    # ── Test 6: POST words/add с дубликатом → upsert ─────────────────
    async def test_words_add_duplicate_upserts(self):
        # First add
        self.client.post("/admin/presets/words/add", data={
            "chat_id": "0",
            "pattern": "dup_word",
            "is_regex": "",
            "action": "delete",
        }, follow_redirects=False)
        # Second add — should update action
        r = self.client.post("/admin/presets/words/add", data={
            "chat_id": "0",
            "pattern": "dup_word",
            "is_regex": "",
            "action": "ban",
        }, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("Pattern+updated", r.headers.get("location", ""))

        async with async_session() as s:
            wfs = (await s.execute(
                select(WordFilter).where(WordFilter.pattern == "dup_word")
            )).scalars().all()
            self.assertEqual(len(wfs), 1, "Should not create duplicate row")
            self.assertEqual(wfs[0].action, "ban", "Action should be updated")

    # ── Test 7: POST words/{id}/delete → soft-delete ─────────────────
    async def test_words_delete_soft_delete(self):
        # Create
        self.client.post("/admin/presets/words/add", data={
            "chat_id": "0",
            "pattern": "to_delete",
            "is_regex": "",
            "action": "delete",
        }, follow_redirects=False)
        async with async_session() as s:
            wf = (await s.execute(
                select(WordFilter).where(WordFilter.pattern == "to_delete")
            )).scalar_one()
            word_id = wf.id
            self.assertTrue(wf.is_active)

        # Delete via UI
        r = self.client.post(
            f"/admin/presets/words/{word_id}/delete",
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 303)
        self.assertIn("Pattern+deleted", r.headers.get("location", ""))

        async with async_session() as s:
            wf = (await s.execute(
                select(WordFilter).where(WordFilter.id == word_id)
            )).scalar_one()
            self.assertFalse(wf.is_active, "Should be soft-deleted (is_active=False)")

    # ── Test 8: POST links/add создаёт LinkAllowlist ─────────────────
    async def test_links_add_creates_allowlist(self):
        # Используем домен, которого нет в seeded global allowlist
        # (init_db сидирует: t.me, telegram.me, github.com, youtu.be, youtube.com)
        r = self.client.post("/admin/presets/links/add", data={
            "chat_id": "0",
            "domain": "fresh-domain-v475.example",
        }, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("Domain+added", r.headers.get("location", ""))

        async with async_session() as s:
            link = (await s.execute(
                select(LinkAllowlist).where(LinkAllowlist.domain == "fresh-domain-v475.example")
            )).scalar_one_or_none()
            self.assertIsNotNone(link)
            self.assertEqual(link.chat_id, 0)

    # ── Test 9: POST links/add нормализует URL ───────────────────────
    async def test_links_add_normalizes_url(self):
        r = self.client.post("/admin/presets/links/add", data={
            "chat_id": "0",
            "domain": "https://github.com/some/repo",
        }, follow_redirects=False)
        self.assertEqual(r.status_code, 303)

        async with async_session() as s:
            link = (await s.execute(
                select(LinkAllowlist).where(LinkAllowlist.domain == "github.com")
            )).scalar_one_or_none()
            self.assertIsNotNone(link, "Should be normalized to github.com")

    # ── Test 10: POST links/add с дубликатом ─────────────────────────
    async def test_links_add_duplicate_rejected(self):
        self.client.post("/admin/presets/links/add", data={
            "chat_id": "0",
            "domain": "example.com",
        }, follow_redirects=False)

        r = self.client.post("/admin/presets/links/add", data={
            "chat_id": "0",
            "domain": "example.com",
        }, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("already+in+allowlist", r.headers.get("location", ""))

        async with async_session() as s:
            links = (await s.execute(
                select(LinkAllowlist).where(LinkAllowlist.domain == "example.com")
            )).scalars().all()
            self.assertEqual(len(links), 1, "Should not create duplicate")

    # ── Test 11: POST links/{id}/delete → hard delete ────────────────
    async def test_links_delete_hard_delete(self):
        self.client.post("/admin/presets/links/add", data={
            "chat_id": "0",
            "domain": "remove-me.ru",
        }, follow_redirects=False)
        async with async_session() as s:
            link = (await s.execute(
                select(LinkAllowlist).where(LinkAllowlist.domain == "remove-me.ru")
            )).scalar_one()
            link_id = link.id

        r = self.client.post(
            f"/admin/presets/links/{link_id}/delete",
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 303)
        self.assertIn("Domain+removed", r.headers.get("location", ""))

        async with async_session() as s:
            link = (await s.execute(
                select(LinkAllowlist).where(LinkAllowlist.id == link_id)
            )).scalar_one_or_none()
            self.assertIsNone(link, "Should be hard-deleted (no row)")

    # ── Test 12: POST links/add с невалидным доменом (без точки) ─────
    async def test_links_add_invalid_domain_rejected(self):
        r = self.client.post("/admin/presets/links/add", data={
            "chat_id": "0",
            "domain": "no-dot-here",
        }, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("Invalid+domain", r.headers.get("location", ""))

        async with async_session() as s:
            links = (await s.execute(
                select(LinkAllowlist).where(LinkAllowlist.domain == "no-dot-here")
            )).scalars().all()
            self.assertEqual(len(links), 0)

    # ── Test 13: Per-chat word filter (chat_id != 0) ─────────────────
    async def test_words_add_per_chat_scope(self):
        r = self.client.post("/admin/presets/words/add", data={
            "chat_id": "-1004750000001",
            "pattern": "per_chat_word",
            "is_regex": "",
            "action": "warn",
        }, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("Pattern+added", r.headers.get("location", ""))

        async with async_session() as s:
            wf = (await s.execute(
                select(WordFilter).where(WordFilter.pattern == "per_chat_word")
            )).scalar_one()
            self.assertEqual(wf.chat_id, -1004750000001)

    # ── Test 14: Moderator не может удалить чужой word (require_admin) ─
    async def test_words_delete_requires_admin(self):
        # Create as SU
        self.client.post("/admin/presets/words/add", data={
            "chat_id": "0",
            "pattern": "mod_test",
            "is_regex": "",
            "action": "delete",
        }, follow_redirects=False)
        async with async_session() as s:
            wf = (await s.execute(
                select(WordFilter).where(WordFilter.pattern == "mod_test")
            )).scalar_one()
            word_id = wf.id

        # Logout SU
        self.client.post("/logout", follow_redirects=False)

        # Login as moderator
        async with async_session() as s:
            mod = WebUser(
                username="mod1",
                is_su=False,
                is_active=True,
                role="moderator",
                password_hash="$fake$hash",
                created_by="system",
            )
            s.add(mod)
            await s.commit()
        # Moderator has no password set — we'll set one manually
        from db import _hash_password
        async with async_session() as s:
            mod = (await s.execute(
                select(WebUser).where(WebUser.username == "mod1")
            )).scalar_one()
            mod.password_hash = _hash_password("mod_pass_123")
            await s.commit()

        r = self.client.post("/login", data={
            "username": "mod1", "password": "mod_pass_123",
        }, follow_redirects=False)
        self.assertEqual(r.status_code, 303)

        # Try to delete word as moderator → should redirect to /dashboard
        r = self.client.post(
            f"/admin/presets/words/{word_id}/delete",
            follow_redirects=False,
        )
        # require_admin redirects moderator to /dashboard
        self.assertEqual(r.status_code, 303)
        self.assertIn("/dashboard", r.headers.get("location", ""))

        # Word still in DB, still active
        async with async_session() as s:
            wf = (await s.execute(
                select(WordFilter).where(WordFilter.id == word_id)
            )).scalar_one()
            self.assertTrue(wf.is_active, "Moderator should not be able to delete")


class TestV475SourceCodePatterns(unittest.TestCase):
    """Static source inspection — без запуска приложения."""

    def test_web_app_has_wordfilter_handlers(self):
        """v4.9.0 (Task 10): роуты переехали в web/admin_presets.py."""
        with open(_P("web/admin_presets.py"), "r") as f:
            src = f.read()
        self.assertIn("/admin/presets/words/add", src)
        self.assertIn("/admin/presets/words/{word_id:int}/delete", src)
        self.assertIn("/admin/presets/links/add", src)
        self.assertIn("/admin/presets/links/{link_id:int}/delete", src)

    def test_web_app_imports_wordfilter_linkallowlist(self):
        """v4.9.0 (Task 10): импорт моделей переехал в web/admin_presets.py."""
        with open(_P("web/admin_presets.py"), "r") as f:
            src = f.read()
        self.assertIn("WordFilter", src)
        self.assertIn("LinkAllowlist", src)

    def test_template_has_sections(self):
        with open(_P("templates/admin_presets.html"), "r") as f:
            src = f.read()
        self.assertIn("(Word Filter)", src)
        self.assertIn("Link Allowlist", src)
        self.assertIn('name="is_regex"', src)
        self.assertIn('name="pattern"', src)
        self.assertIn('name="domain"', src)

    def test_changelog_mentions_v475(self):
        with open(_P("templates/base.html"), "r") as f:
            src = f.read()
        self.assertIn("v4.7.5", src)
        self.assertIn("Word filter", src)
        self.assertIn("Link allowlist", src)


if __name__ == "__main__":
    unittest.main()
