"""v4.8.0 web UI tests — modchat toggle + keyword-watch admin page.

Покрывает:
  • /admin/chats/{id}/toggle с field=mod_chat (новое поле v4.8.0).
  • Взаимоисключение mod_chat ↔ report_chat (в обе стороны).
  • Маршруты /admin/keywords (GET, POST add/delete/toggle-ban-night).
  • Шаблон admin_keywords.html (формы, бейджи, секции).
  • Шаблон admin_chats.html — кнопка MOD + взаимоисключение через disabled.
  • base.html — пункт меню "Keywords" для SU.

Тесты структурные (AST/regex) + поведенческие (TestClient FastAPI + in-memory SQLite).
"""
import ast
import asyncio
import re
import sys
import os
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Setup env BEFORE imports (web_app reads env vars at import time)
os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ.setdefault("BOT_ID", "123")
os.environ.setdefault("USER_ID", "456")
os.environ["WEB_PASSWORD"] = "test-password"
os.environ.setdefault("SESSION_SECRET", "test-session-secret-1234567890")
os.environ.setdefault("DB_PATH", ":memory:")

WEB_APP_PY = ROOT / "web_app.py"
# v4.9.0 (Task 5): роуты /admin/keywords* переехали из web_app.py в
# web/admin_keywords.py. Структурные проверки, читавшие их из web_app.py,
# переадресованы на новый файл — смысл проверок сохранён дословно.
ADMIN_KEYWORDS_PY = ROOT / "web" / "admin_keywords.py"
# v4.9.0 (Task 9): /admin/users* переехали из web_app.py в web/admin_users.py.
ADMIN_USERS_PY = ROOT / "web" / "admin_users.py"
ADMIN_CHATS_HTML = ROOT / "templates" / "admin_chats.html"
ADMIN_KEYWORDS_HTML = ROOT / "templates" / "admin_keywords.html"
BASE_HTML = ROOT / "templates" / "base.html"
DB_PY = ROOT / "db.py"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ============================================================================
# 1. Структурные тесты web_app.py — toggle endpoint + keyword routes
# ============================================================================

class TestWebAppStructure(unittest.TestCase):
    """Проверка, что в web_app.py есть новые маршруты и toggle field."""

    def setUp(self):
        self.src = _read(WEB_APP_PY)

    def test_01_mod_chat_in_valid_fields(self):
        """'mod_chat' должен быть в valid_fields set у toggle endpoint."""
        # Найдём valid_fields set literal
        m = re.search(r'valid_fields\s*=\s*\{([^}]+)\}', self.src)
        self.assertIsNotNone(m, "valid_fields set not found in web_app.py")
        contents = m.group(1)
        self.assertIn("mod_chat", contents,
                      "'mod_chat' must be in valid_fields set for toggle endpoint")

    def test_02_mod_chat_branch_in_toggle(self):
        """Должна быть ветка `if field == "mod_chat":` в admin_chats_toggle."""
        self.assertIn('if field == "mod_chat":', self.src,
                      "Toggle handler must have explicit mod_chat branch")

    def test_03_mod_chat_mutual_exclusion_with_report(self):
        """Ветка mod_chat должна проверять is_report_chat и отказывать."""
        # Извлечём тело функции admin_chats_toggle
        try:
            tree = ast.parse(self.src)
        except SyntaxError as e:
            self.fail(f"web_app.py has syntax error: {e}")

        toggle_body = None
        for node in ast.walk(tree):
            if (isinstance(node, ast.AsyncFunctionDef)
                    and node.name == "admin_chats_toggle"):
                lines = self.src.splitlines()
                start = node.lineno - 1
                end = node.end_lineno or len(lines)
                toggle_body = "\n".join(lines[start:end])
                break

        self.assertIsNotNone(toggle_body, "admin_chats_toggle not found")
        # Проверим, что в теле есть упоминание is_report_chat в контексте mod_chat
        # Ищем фрагмент между `if field == "mod_chat":` и концом блока.
        m = re.search(
            r'if field == "mod_chat":(.*?)(?=\n            cs\.updated_at|\n            await session\.commit)',
            toggle_body,
            re.DOTALL,
        )
        self.assertIsNotNone(m, "mod_chat branch not found in toggle body")
        mod_chat_branch = m.group(1)
        self.assertIn("is_report_chat", mod_chat_branch,
                      "mod_chat branch must check is_report_chat for mutual exclusion")
        self.assertIn("is_mod_chat", mod_chat_branch,
                      "mod_chat branch must check is_mod_chat")

    def test_04_report_chat_branch_checks_is_mod_chat(self):
        """report_chat ветка тоже должна проверять is_mod_chat (обратное исключение)."""
        try:
            tree = ast.parse(self.src)
        except SyntaxError as e:
            self.fail(f"web_app.py has syntax error: {e}")

        toggle_body = None
        for node in ast.walk(tree):
            if (isinstance(node, ast.AsyncFunctionDef)
                    and node.name == "admin_chats_toggle"):
                lines = self.src.splitlines()
                start = node.lineno - 1
                end = node.end_lineno or len(lines)
                toggle_body = "\n".join(lines[start:end])
                break

        self.assertIsNotNone(toggle_body, "admin_chats_toggle not found")
        # Найдём ветку report_chat
        m = re.search(
            r'elif field == "report_chat":(.*?)(?=\n            # v4\.8\.0|\n            if field == "mod_chat"|\n            cs\.updated_at)',
            toggle_body,
            re.DOTALL,
        )
        self.assertIsNotNone(m, "report_chat branch not found")
        report_branch = m.group(1)
        self.assertIn("is_mod_chat", report_branch,
                      "report_chat branch must check is_mod_chat for mutual exclusion")

    def test_05_keyword_routes_exist(self):
        """Должны быть 4 маршрута: GET /admin/keywords, POST add/delete/toggle-ban-night.

        v4.9.0 (Task 5): роуты переехали в web/admin_keywords.py, декоратор
        сменился с @app.get/@app.post на @router.get/@router.post. Смысл
        проверки прежний — читаем из нового файла.
        """
        kw_src = _read(ADMIN_KEYWORDS_PY)
        try:
            tree = ast.parse(kw_src)
        except SyntaxError as e:
            self.fail(f"web/admin_keywords.py has syntax error: {e}")

        route_paths = []
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef):
                # Look up decorators for app.get/app.post
                for dec in node.decorator_list:
                    if isinstance(dec, ast.Call):
                        if isinstance(dec.func, ast.Attribute):
                            attr = dec.func.attr  # 'get' or 'post'
                            if attr in ("get", "post") and dec.args:
                                first_arg = dec.args[0]
                                if isinstance(first_arg, ast.Constant):
                                    route_paths.append((attr, first_arg.value, node.name))
        # Filter keyword routes
        kw_routes = [(m, p) for (m, p, _) in route_paths if "/admin/keywords" in p]
        # Expect: ('get', '/admin/keywords'), ('post', '/admin/keywords/add'),
        # ('post', '/admin/keywords/{keyword_id:int}/delete'),
        # ('post', '/admin/keywords/{keyword_id:int}/toggle-ban-night')
        paths = [p for _, p in kw_routes]
        self.assertIn("/admin/keywords", paths,
                      "GET /admin/keywords route missing")
        self.assertIn("/admin/keywords/add", paths,
                      "POST /admin/keywords/add route missing")
        self.assertIn("/admin/keywords/{keyword_id:int}/delete", paths,
                      "POST /admin/keywords/.../delete route missing")
        self.assertIn("/admin/keywords/{keyword_id:int}/toggle-ban-night", paths,
                      "POST /admin/keywords/.../toggle-ban-night route missing")

    def test_06_keyword_routes_use_require_su(self):
        """Все keyword маршруты должны использовать require_su, а не require_admin.

        v4.9.0 (Task 5): роуты переехали в web/admin_keywords.py — без
        переадресации этот тест молча не находил бы ни одной функции
        (0 итераций цикла) и проходил бы, ничего не проверяя.
        """
        kw_src = _read(ADMIN_KEYWORDS_PY)
        try:
            tree = ast.parse(kw_src)
        except SyntaxError as e:
            self.fail(f"web/admin_keywords.py has syntax error: {e}")

        # Найдём функции и их параметры Depends
        for node in ast.walk(tree):
            if (isinstance(node, ast.AsyncFunctionDef)
                    and node.name in (
                        "admin_keywords_page",
                        "admin_keywords_add",
                        "admin_keywords_delete",
                        "admin_keywords_toggle_ban_night",
                    )):
                # Найдём Depends(require_su) среди аргументов
                deps_found = False
                for arg in node.args.args:
                    if arg.annotation:
                        # Annotation может быть Depends(require_su) —
                        # сложно проверить точно, поэтому смотрим default.
                        pass
                # Проверим defaults — последние N аргументов имеют defaults
                # Defaults соответствуют последним n позиционным аргам.
                # v4.8.8: POST-роуты перешли с require_su на require_csrf_su —
                # та же проверка роли плюс CSRF, то есть строго сильнее.
                # Подстрокой одно в другом не содержится ("require_" + "csrf_su"),
                # поэтому принимаем оба имени.
                for default in node.args.defaults:
                    src_lines = kw_src.splitlines()
                    if hasattr(default, "lineno"):
                        line = src_lines[default.lineno - 1]
                        if "require_su" in line or "require_csrf_su" in line:
                            deps_found = True
                            break
                self.assertTrue(deps_found,
                    f"{node.name} must use require_su/require_csrf_su "
                    f"(found defaults: {node.args.defaults})")

    def test_07_keywordwatch_imported(self):
        """KeywordWatch должен быть импортирован из db.

        v4.9.0 (Task 5): импорт переехал в web/admin_keywords.py, где
        принят однострочный стиль (`from db import KeywordWatch,
        async_session`, без скобок) — как в web/admin_bans.py. Регекс
        подогнан под этот стиль, смысл проверки прежний.
        """
        kw_src = _read(ADMIN_KEYWORDS_PY)
        m = re.search(r'from db import ([^\n]+)', kw_src)
        self.assertIsNotNone(m, "from db import block not found")
        self.assertIn("KeywordWatch", m.group(1),
                      "KeywordWatch must be imported from db")


# ============================================================================
# 2. Структурные тесты шаблонов
# ============================================================================

class TestAdminChatsTemplate(unittest.TestCase):
    """admin_chats.html — кнопка MOD + взаимоисключение."""

    def setUp(self):
        self.html = _read(ADMIN_CHATS_HTML)

    def test_10_has_mod_toggle_button(self):
        """В шаблоне должна быть кнопка с value=mod_chat."""
        self.assertIn('value="mod_chat"', self.html,
                      "admin_chats.html must have MOD toggle button")

    def test_11_mod_button_disabled_for_report_chats(self):
        """Кнопка MOD должна быть disabled для чатов с is_report_chat=True."""
        # Jinja-условие может быть на следующей строке после value=, поэтому
        # re.DOTALL чтобы .* матчило переводы строк.
        m = re.search(
            r'value="mod_chat".*?\{% if c\.is_report_chat %\}disabled',
            self.html,
            re.DOTALL,
        )
        self.assertIsNotNone(m,
            "MOD button must be disabled when is_report_chat=True")

    def test_12_report_button_disabled_for_mod_chats(self):
        """Кнопка REPORT должна быть disabled для чатов с is_mod_chat=True."""
        m = re.search(
            r'value="report_chat".*?\{% if c\.is_mod_chat %\}disabled',
            self.html,
            re.DOTALL,
        )
        self.assertIsNotNone(m,
            "REPORT button must be disabled when is_mod_chat=True (mutual exclusion)")

    def test_13_has_mod_badge(self):
        """Должен быть бейдж MOD в badges-секции."""
        self.assertRegex(
            self.html,
            r'\{% if c\.is_mod_chat %\}.*?MOD',
            "MOD badge not found in chats template",
        )

    def test_14_grid_columns_count_updated(self):
        """Grid в toggles-секции должен быть 8 колонок (было 7, +1 MOD)."""
        # Ищем grid-template-columns: repeat(8, 1fr) — новая разметка
        self.assertIn("repeat(8, 1fr)", self.html,
                      "Toggle grid must be 8 columns (added MOD button)")

    def test_15_mod_button_states_visible(self):
        """MOD-кнопка должна показывать ★ MOD (активна) или ☆ MOD (не активна)."""
        self.assertIn("★ MOD", self.html,
                      "MOD button active state label missing")
        self.assertIn("☆ MOD", self.html,
                      "MOD button inactive state label missing")

    def test_16_summary_mentions_mod_state(self):
        """Сводка в <summary> Общее должна показывать MOD-статус."""
        # Проверим, что в summary есть упоминание MOD через is_mod_chat
        self.assertIn("is_mod_chat", self.html,
                      "Summary must reference is_mod_chat for MOD status display")


class TestAdminKeywordsTemplate(unittest.TestCase):
    """admin_keywords.html — структура страницы."""

    def setUp(self):
        self.html = _read(ADMIN_KEYWORDS_HTML)

    def test_20_template_exists(self):
        """Шаблон admin_keywords.html существует."""
        self.assertTrue(ADMIN_KEYWORDS_HTML.exists(),
                        "admin_keywords.html must exist")

    def test_21_extends_base(self):
        """Шаблон наследует base.html."""
        self.assertIn('{% extends "base.html" %}', self.html,
                      "admin_keywords.html must extend base.html")

    def test_22_has_add_form(self):
        """Есть форма добавления фразы."""
        self.assertIn('action="/admin/keywords/add"', self.html,
                      "Add phrase form must POST to /admin/keywords/add")
        self.assertIn('name="phrase"', self.html,
                      "Add phrase form must have phrase input")
        self.assertIn('name="ban_in_night_mode"', self.html,
                      "Add phrase form must have ban_in_night_mode checkbox")

    def test_23_has_delete_form(self):
        """Есть форма удаления фразы."""
        self.assertIn('action="/admin/keywords/{{ kw.id }}/delete"', self.html,
                      "Delete form must POST to /admin/keywords/{id}/delete")

    def test_24_has_toggle_ban_night_form(self):
        """Есть форма переключения ban_in_night_mode."""
        self.assertIn('action="/admin/keywords/{{ kw.id }}/toggle-ban-night"', self.html,
                      "Toggle ban-night form must exist")

    def test_25_has_active_phrases_section(self):
        """Есть секция активных фраз."""
        self.assertIn("Active phrases", self.html,
                      "Active phrases section must exist")
        self.assertIn("{% for kw in keywords %}", self.html,
                      "Template must iterate over keywords")

    def test_26_has_inactive_audit_section(self):
        """Есть секция удалённых фраз (audit log)."""
        self.assertIn("Deleted phrases", self.html,
                      "Audit log section for inactive keywords must exist")
        self.assertIn("{% for kw in inactive_keywords %}", self.html,
                      "Template must iterate over inactive_keywords")

    def test_27_word_phrase_badge_logic(self):
        """Шаблон различает WORD vs PHRASE (по наличию пробела)."""
        self.assertIn("' ' in kw.phrase", self.html,
                      "Template must distinguish WORD vs PHRASE badges by space in phrase")

    def test_28_explains_modchat_dependency(self):
        """Шаблон должен объяснить, что modchat должен быть назначен."""
        # Ищем текст про modchat в описании
        self.assertTrue(
            "modchat" in self.html.lower(),
            "Template must mention modchat (its dependency)",
        )

    def test_29_mentions_equivalent_tg_commands(self):
        """Шаблон должен упоминать эквивалентные TG-команды как бэкап."""
        self.assertIn("!addkeyword", self.html,
                      "Template should mention !addkeyword as backup")
        self.assertIn("!delkeyword", self.html,
                      "Template should mention !delkeyword as backup")
        self.assertIn("!listkeywords", self.html,
                      "Template should mention !listkeywords as backup")

    def test_30_maxlength_255_enforced(self):
        """HTML-форма должна ограничивать maxlength=255 для фразы."""
        self.assertIn('maxlength="255"', self.html,
                      "Phrase input must have maxlength=255 to match DB schema")


class TestBaseTemplateKeywordMenu(unittest.TestCase):
    """base.html — пункт меню Keywords."""

    def setUp(self):
        self.html = _read(BASE_HTML)

    def test_40_keywords_nav_link_exists(self):
        """В navbar должен быть пункт Keywords (SU-only)."""
        self.assertIn('href="/admin/keywords"', self.html,
                      "base.html must have nav link to /admin/keywords")

    def test_41_keywords_link_su_only(self):
        """Ссылка Keywords должна быть внутри SU-only блока nav."""
        # SU-only nav-блок содержит nested {% if %} (для аватарок), поэтому
        # простой `(.*?){% endif %}` обрывается раньше времени. Используем
        # подсчёт if/endif: между открывающим SU-IF и keywords-ссылкой
        # число `{% endif %}` не должно превышать число вложенных `{% if %}`.
        keywords_pos = self.html.find('href="/admin/keywords"')
        self.assertGreater(keywords_pos, -1, "Keywords nav link not found")
        # Ищем все SU-only nav IF\'ы перед keywords_pos
        su_if_pattern = r"\{% if auth_user and auth_user\.role == 'su' %\}"
        su_matches = list(re.finditer(su_if_pattern, self.html))
        self.assertTrue(su_matches, "No SU-only block found in base.html")
        # Найдём последний SU IF, который ПЕРЕД keywords_pos
        su_before = None
        for m in su_matches:
            if m.start() < keywords_pos:
                su_before = m
        self.assertIsNotNone(su_before,
            "Keywords link must come after a SU-only block opening")
        # Подсчёт if/endif между SU-IF и keywords-ссылкой:
        between = self.html[su_before.end():keywords_pos]
        if_count = len(re.findall(r'\{% if', between))
        endif_count = len(re.findall(r'\{% endif', between))
        # Если endif_count >= if_count + 1, то SU-блок уже закрылся.
        self.assertLess(
            endif_count, if_count + 1,
            "Keywords link must be inside SU-only nav block "
            "(block appears to be closed before the link)",
        )

    def test_42_keywords_active_highlight(self):
        """Ссылка Keywords должна иметь active-подсветку."""
        self.assertRegex(
            self.html,
            r'class="nav-link \{% if request\.url\.path\.startswith\([\'"]/admin/keywords[\'"]\) %\}active\{% endif %\}"',
            "Keywords nav link must have active state based on URL path",
        )

    def test_43_changelog_mentions_web_ui(self):
        """Changelog v4.8.0 должен упоминать веб-UI."""
        # Find v4.8.0 changelog block. Section ends at the start of v4.7.30
        # section (marked by `<p><strong>v4.7.30</strong>`). We use this marker
        # because the v4.8.0 section contains nested <ul>...</ul> for sublists
        # (e.g. inside <li>#9: Рефакторинг...), so stopping at the first </ul>
        # would cut the section short.
        v480_section = re.search(
            r"v4\.8\.0</strong>.*?(?=<p><strong>v4\.7\.30</strong>)",
            self.html,
            re.DOTALL,
        )
        self.assertIsNotNone(v480_section, "v4.8.0 changelog section not found")
        section_text = v480_section.group(0)
        # Should mention /admin/keywords or веб-панель
        self.assertTrue(
            "/admin/keywords" in section_text or "веб-панел" in section_text.lower(),
            "v4.8.0 changelog must mention /admin/keywords or веб-панель (web UI)",
        )


# ============================================================================
# 3. Поведенческие тесты — TestClient FastAPI + in-memory SQLite
# ============================================================================

class TestKeywordWebRoutesBehavior(unittest.TestCase):
    """Поведенческие тесты: GET /admin/keywords + POST add/delete/toggle.

    Использует FastAPI TestClient + in-memory SQLite (через DB_PATH=:memory:).
    Каждый тест создаёт чистую БД.
    """

    @classmethod
    def setUpClass(cls):
        # Импортируем модули один раз
        import web_app
        import db
        import web.admin_keywords
        cls._web_app = web_app
        cls._db = db
        cls._admin_keywords = web.admin_keywords
        # Создаём тестовое приложение
        cls._app = web_app.create_app()

    def setUp(self):
        """Чистая in-memory БД для каждого теста."""
        from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
        from sqlalchemy.pool import StaticPool
        # StaticPool — все сессии разделяют ОДНО соединение (in-memory SQLite
        # иначе создаёт новую БД для каждого соединения, что ломает тесты).
        self.engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
            echo=False,
        )
        self.AsyncSessionLocal = async_sessionmaker(self.engine, expire_on_commit=False)

        # Patch db.async_session to return our test session factory
        patcher = patch.object(self._db, "async_session", self.AsyncSessionLocal)
        patcher.start()
        self.addCleanup(patcher.stop)

        # Также patch web_app.async_session (импортированный символ)
        web_app_patcher = patch.object(self._web_app, "async_session", self.AsyncSessionLocal)
        web_app_patcher.start()
        self.addCleanup(web_app_patcher.stop)

        # v4.9.0 (Task 5): /admin/keywords* переехали в web/admin_keywords.py,
        # у которого async_session — свой импортированный символ, отдельный
        # от web_app.async_session. Без этого патча роуты читали бы боевую
        # БД мимо тестовой in-memory.
        admin_keywords_patcher = patch.object(
            self._admin_keywords, "async_session", self.AsyncSessionLocal
        )
        admin_keywords_patcher.start()
        self.addCleanup(admin_keywords_patcher.stop)

        # Создаём схему
        async def _init():
            async with self.engine.begin() as conn:
                await conn.run_sync(self._db.Base.metadata.create_all)
        asyncio.run(_init())

        # Создаём тестовых пользователей: SU и admin (для auth)
        async def _create_users():
            async with self.AsyncSessionLocal() as s:
                su = self._db.WebUser(
                    username="test_su",
                    is_su=True,
                    role="su",
                    is_active=True,
                    password_hash=self._db._hash_password("x"),
                )
                admin = self._db.WebUser(
                    username="test_admin",
                    is_su=False,
                    role="admin",
                    is_active=True,
                    password_hash=self._db._hash_password("x"),
                )
                s.add(su)
                s.add(admin)
                await s.commit()
        asyncio.run(_create_users())

    def tearDown(self):
        asyncio.run(self.engine.dispose())

    def _make_su_token(self) -> str:
        """Создаём валидный SU-токен для запросов."""
        return self._web_app._make_token("test_su", is_su=True, role="su")

    def _make_admin_token(self) -> str:
        """Создаём валидный admin-токен (не SU)."""
        return self._web_app._make_token("test_admin", is_su=False, role="admin")

    def _get_with_auth(self, client, url, token=None):
        cookies = {}
        if token:
            cookies[self._web_app.COOKIE_NAME] = token
        return client.get(url, cookies=cookies, follow_redirects=False)

    def _post_with_auth(self, client, url, token=None, data=None):
        cookies = {}
        if token:
            cookies[self._web_app.COOKIE_NAME] = token
        return client.post(url, cookies=cookies, data=data or {}, follow_redirects=False)

    def test_50_get_keywords_page_su(self):
        """SU может открыть /admin/keywords — 200 OK."""
        from fastapi.testclient import TestClient
        client = TestClient(self._app)
        token = self._make_su_token()
        resp = self._get_with_auth(client, "/admin/keywords", token)
        self.assertEqual(resp.status_code, 200,
                         f"SU GET /admin/keywords must return 200, got {resp.status_code}")
        # Контент должен содержать "Keyword watch list"
        self.assertIn("Keyword watch list", resp.text,
                      "Page must contain 'Keyword watch list' title")

    def test_51_get_keywords_page_admin_forbidden(self):
        """admin (не SU) не может открыть /admin/keywords — редирект."""
        from fastapi.testclient import TestClient
        client = TestClient(self._app)
        token = self._make_admin_token()
        resp = self._get_with_auth(client, "/admin/keywords", token)
        # require_su возвращает RedirectResponse на /dashboard (303)
        self.assertIn(resp.status_code, (303, 302, 307),
                      f"Non-SU must be redirected, got {resp.status_code}")

    def test_52_get_keywords_page_unauth_redirect(self):
        """Не залогиненный юзер — редирект на /login."""
        from fastapi.testclient import TestClient
        client = TestClient(self._app)
        resp = client.get("/admin/keywords", follow_redirects=False)
        self.assertIn(resp.status_code, (303, 302, 307),
                      "Unauthenticated GET must be redirected to login")

    def test_53_add_keyword_su_success(self):
        """SU может добавить фразу — она сохраняется в БД."""
        from fastapi.testclient import TestClient
        client = TestClient(self._app)
        token = self._make_su_token()
        resp = self._post_with_auth(
            client, "/admin/keywords/add", token,
            data={"phrase": "  тестовая фраза  ", "ban_in_night_mode": "on"},
        )
        self.assertEqual(resp.status_code, 303,
                         f"POST add must return 303 redirect, got {resp.status_code}")
        # Проверим, что фраза в БД (через patch-нутую сессию)
        async def _check():
            async with self.AsyncSessionLocal() as s:
                from sqlalchemy import select
                result = await s.execute(
                    select(self._db.KeywordWatch).where(
                        self._db.KeywordWatch.phrase == "тестовая фраза"
                    )
                )
                kw = result.scalar_one_or_none()
                return kw
        kw = asyncio.run(_check())
        self.assertIsNotNone(kw, "Keyword must be saved to DB after add")
        self.assertTrue(kw.is_active, "Added keyword must be active")
        self.assertTrue(kw.ban_in_night_mode,
                        "ban_in_night_mode must be True when checkbox is on")
        self.assertEqual(kw.chat_id, 0, "Keyword must be global (chat_id=0)")

    def test_54_add_keyword_normalizes_whitespace(self):
        """Фраза с лишними пробелами должна нормализоваться."""
        from fastapi.testclient import TestClient
        client = TestClient(self._app)
        token = self._make_su_token()
        resp = self._post_with_auth(
            client, "/admin/keywords/add", token,
            data={"phrase": "  слово1    слово2   "},
        )
        self.assertEqual(resp.status_code, 303)
        async def _check():
            async with self.AsyncSessionLocal() as s:
                from sqlalchemy import select
                result = await s.execute(
                    select(self._db.KeywordWatch).where(
                        self._db.KeywordWatch.phrase == "слово1 слово2"
                    )
                )
                return result.scalar_one_or_none()
        kw = asyncio.run(_check())
        self.assertIsNotNone(kw, "Whitespace must be collapsed + trimmed")

    def test_55_add_keyword_empty_phrase_rejected(self):
        """Пустая фраза (или только пробелы) — отказ."""
        from fastapi.testclient import TestClient
        client = TestClient(self._app)
        token = self._make_su_token()
        resp = self._post_with_auth(
            client, "/admin/keywords/add", token,
            data={"phrase": "   "},
        )
        # Должен быть redirect с flash сообщением
        self.assertEqual(resp.status_code, 303)
        self.assertIn("empty", resp.headers.get("location", "").lower(),
                      "Empty phrase must produce flash=empty in redirect URL")

    def test_56_add_keyword_too_long_rejected(self):
        """Фраза >255 символов — отказ (DB constraint)."""
        from fastapi.testclient import TestClient
        client = TestClient(self._app)
        token = self._make_su_token()
        long_phrase = "а" * 300
        resp = self._post_with_auth(
            client, "/admin/keywords/add", token,
            data={"phrase": long_phrase},
        )
        self.assertEqual(resp.status_code, 303)
        self.assertIn("long", resp.headers.get("location", "").lower(),
                      "Too long phrase must produce flash=long in redirect URL")

    def test_57_add_keyword_without_ban_flag(self):
        """Фраза без чекбокса ban_in_night_mode — флаг должен быть False."""
        from fastapi.testclient import TestClient
        client = TestClient(self._app)
        token = self._make_su_token()
        resp = self._post_with_auth(
            client, "/admin/keywords/add", token,
            data={"phrase": "мягкая фраза"},  # no ban_in_night_mode field
        )
        self.assertEqual(resp.status_code, 303)
        async def _check():
            async with self.AsyncSessionLocal() as s:
                from sqlalchemy import select
                result = await s.execute(
                    select(self._db.KeywordWatch).where(
                        self._db.KeywordWatch.phrase == "мягкая фраза"
                    )
                )
                return result.scalar_one_or_none()
        kw = asyncio.run(_check())
        self.assertIsNotNone(kw)
        self.assertFalse(kw.ban_in_night_mode,
                         "ban_in_night_mode must be False when checkbox absent")

    def test_58_add_duplicate_reactivates(self):
        """Добавление уже существующей (даже удалённой) фразы — реактивация."""
        from fastapi.testclient import TestClient
        client = TestClient(self._app)
        token = self._make_su_token()
        # First add
        self._post_with_auth(
            client, "/admin/keywords/add", token,
            data={"phrase": "reactivate-me"},
        )
        # Get id
        async def _get_id():
            async with self.AsyncSessionLocal() as s:
                from sqlalchemy import select
                result = await s.execute(
                    select(self._db.KeywordWatch).where(
                        self._db.KeywordWatch.phrase == "reactivate-me"
                    )
                )
                kw = result.scalar_one_or_none()
                return kw.id if kw else None
        kw_id = asyncio.run(_get_id())
        self.assertIsNotNone(kw_id)
        # Soft-delete
        self._post_with_auth(
            client, f"/admin/keywords/{kw_id}/delete", token,
        )
        # Verify deleted
        async def _check_active():
            async with self.AsyncSessionLocal() as s:
                from sqlalchemy import select
                result = await s.execute(
                    select(self._db.KeywordWatch).where(
                        self._db.KeywordWatch.id == kw_id
                    )
                )
                return result.scalar_one_or_none()
        kw = asyncio.run(_check_active())
        self.assertFalse(kw.is_active, "Soft-delete must set is_active=False")
        # Re-add — should reactivate
        self._post_with_auth(
            client, "/admin/keywords/add", token,
            data={"phrase": "reactivate-me", "ban_in_night_mode": "on"},
        )
        kw = asyncio.run(_check_active())
        self.assertTrue(kw.is_active, "Re-adding must reactivate the phrase")
        self.assertTrue(kw.ban_in_night_mode,
                        "Re-adding must update ban_in_night_mode flag")

    def test_59_delete_keyword_su_success(self):
        """SU может удалить фразу (soft-delete, is_active=False)."""
        from fastapi.testclient import TestClient
        client = TestClient(self._app)
        token = self._make_su_token()
        # Add
        self._post_with_auth(
            client, "/admin/keywords/add", token,
            data={"phrase": "to-be-deleted"},
        )
        async def _get_id():
            async with self.AsyncSessionLocal() as s:
                from sqlalchemy import select
                result = await s.execute(
                    select(self._db.KeywordWatch).where(
                        self._db.KeywordWatch.phrase == "to-be-deleted"
                    )
                )
                return result.scalar_one_or_none().id
        kw_id = asyncio.run(_get_id())
        # Delete
        resp = self._post_with_auth(
            client, f"/admin/keywords/{kw_id}/delete", token,
        )
        self.assertEqual(resp.status_code, 303)
        # Verify soft-deleted
        async def _check():
            async with self.AsyncSessionLocal() as s:
                from sqlalchemy import select
                result = await s.execute(
                    select(self._db.KeywordWatch).where(
                        self._db.KeywordWatch.id == kw_id
                    )
                )
                return result.scalar_one_or_none()
        kw = asyncio.run(_check())
        self.assertFalse(kw.is_active, "Delete must set is_active=False")
        # Audit history preserved (row still in DB)
        self.assertIsNotNone(kw, "Soft-delete must preserve the row for audit")

    def test_60_delete_nonexistent_keyword(self):
        """Удаление несуществующей фразы — flash not_found."""
        from fastapi.testclient import TestClient
        client = TestClient(self._app)
        token = self._make_su_token()
        resp = self._post_with_auth(
            client, "/admin/keywords/99999/delete", token,
        )
        self.assertEqual(resp.status_code, 303)
        self.assertIn("not+found", resp.headers.get("location", "").lower(),
                      "Delete nonexistent must produce flash=not+found")

    def test_61_toggle_ban_night(self):
        """Переключение ban_in_night_mode — флаг должен меняться."""
        from fastapi.testclient import TestClient
        client = TestClient(self._app)
        token = self._make_su_token()
        # Add without flag (False initially)
        self._post_with_auth(
            client, "/admin/keywords/add", token,
            data={"phrase": "toggle-test"},
        )
        async def _get():
            async with self.AsyncSessionLocal() as s:
                from sqlalchemy import select
                result = await s.execute(
                    select(self._db.KeywordWatch).where(
                        self._db.KeywordWatch.phrase == "toggle-test"
                    )
                )
                return result.scalar_one_or_none()
        kw = asyncio.run(_get())
        self.assertFalse(kw.ban_in_night_mode, "Initial state must be False")
        # Toggle ON
        resp = self._post_with_auth(
            client, f"/admin/keywords/{kw.id}/toggle-ban-night", token,
        )
        self.assertEqual(resp.status_code, 303)
        self.assertIn("ON", resp.headers.get("location", ""),
                      "Toggle ON must produce flash with ON")
        kw = asyncio.run(_get())
        self.assertTrue(kw.ban_in_night_mode, "After toggle, flag must be True")
        # Toggle OFF
        resp = self._post_with_auth(
            client, f"/admin/keywords/{kw.id}/toggle-ban-night", token,
        )
        self.assertIn("OFF", resp.headers.get("location", ""),
                      "Toggle OFF must produce flash with OFF")
        kw = asyncio.run(_get())
        self.assertFalse(kw.ban_in_night_mode, "After 2nd toggle, flag must be False")

    def test_62_add_keyword_admin_forbidden(self):
        """admin (не SU) не может добавить фразу."""
        from fastapi.testclient import TestClient
        client = TestClient(self._app)
        token = self._make_admin_token()
        resp = self._post_with_auth(
            client, "/admin/keywords/add", token,
            data={"phrase": "should-fail"},
        )
        self.assertIn(resp.status_code, (303, 302, 307),
                      "Non-SU POST add must be redirected")


# ============================================================================
# 4. Поведенческие тесты — toggle mod_chat endpoint
# ============================================================================

class TestModChatToggleBehavior(unittest.TestCase):
    """Поведенческие тесты toggle endpoint с field=mod_chat."""

    @classmethod
    def setUpClass(cls):
        import web_app
        import db
        cls._web_app = web_app
        cls._db = db
        cls._app = web_app.create_app()

    def setUp(self):
        from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
        from sqlalchemy.pool import StaticPool
        self.engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
            echo=False,
        )
        self.AsyncSessionLocal = async_sessionmaker(self.engine, expire_on_commit=False)
        patcher = patch.object(self._db, "async_session", self.AsyncSessionLocal)
        patcher.start()
        self.addCleanup(patcher.stop)
        web_app_patcher = patch.object(self._web_app, "async_session", self.AsyncSessionLocal)
        web_app_patcher.start()
        self.addCleanup(web_app_patcher.stop)

        async def _init():
            async with self.engine.begin() as conn:
                await conn.run_sync(self._db.Base.metadata.create_all)
        asyncio.run(_init())

        # Создаём SU + admin (для auth)
        async def _create_users():
            async with self.AsyncSessionLocal() as s:
                su = self._db.WebUser(
                    username="test_su",
                    is_su=True,
                    role="su",
                    is_active=True,
                    password_hash=self._db._hash_password("x"),
                )
                admin = self._db.WebUser(
                    username="test_admin",
                    is_su=False,
                    role="admin",
                    is_active=True,
                    password_hash=self._db._hash_password("x"),
                )
                s.add(su)
                s.add(admin)
                await s.commit()
        asyncio.run(_create_users())

        # Создаём тестовый чат
        async def _create_chat():
            async with self.AsyncSessionLocal() as s:
                cs = self._db.ChatSettings(
                    chat_id=-1001234567890,
                    title="Test Chat",
                    is_enabled=True,
                )
                s.add(cs)
                await s.commit()
        asyncio.run(_create_chat())

    def tearDown(self):
        asyncio.run(self.engine.dispose())

    def _make_su_token(self) -> str:
        return self._web_app._make_token("test_su", is_su=True, role="su")

    def _make_admin_token(self) -> str:
        return self._web_app._make_token("test_admin", is_su=False, role="admin")

    def _toggle(self, client, chat_id, field, token):
        return client.post(
            f"/admin/chats/{chat_id}/toggle",
            cookies={self._web_app.COOKIE_NAME: token},
            data={"field": field},
            follow_redirects=False,
        )

    def _get_chat(self, chat_id):
        async def _fetch():
            async with self.AsyncSessionLocal() as s:
                from sqlalchemy import select
                result = await s.execute(
                    select(self._db.ChatSettings).where(
                        self._db.ChatSettings.chat_id == chat_id
                    )
                )
                return result.scalar_one_or_none()
        return asyncio.run(_fetch())

    def test_70_toggle_mod_chat_on(self):
        """Включение mod_chat: is_mod_chat=True, mod_chat_id=chat_id."""
        from fastapi.testclient import TestClient
        client = TestClient(self._app)
        token = self._make_su_token()
        resp = self._toggle(client, -1001234567890, "mod_chat", token)
        self.assertEqual(resp.status_code, 303)
        cs = self._get_chat(-1001234567890)
        self.assertTrue(cs.is_mod_chat, "is_mod_chat must be True after toggle ON")
        self.assertEqual(cs.mod_chat_id, -1001234567890,
                         "mod_chat_id must be set to chat_id after toggle ON")

    def test_71_toggle_mod_chat_off(self):
        """Выключение mod_chat: is_mod_chat=False, mod_chat_id=None."""
        from fastapi.testclient import TestClient
        client = TestClient(self._app)
        token = self._make_su_token()
        # On
        self._toggle(client, -1001234567890, "mod_chat", token)
        # Off
        resp = self._toggle(client, -1001234567890, "mod_chat", token)
        self.assertEqual(resp.status_code, 303)
        cs = self._get_chat(-1001234567890)
        self.assertFalse(cs.is_mod_chat, "is_mod_chat must be False after toggle OFF")
        self.assertIsNone(cs.mod_chat_id, "mod_chat_id must be None after toggle OFF")

    def test_72_mod_chat_refuses_if_report_chat(self):
        """Если чат уже is_report_chat — toggle mod_chat отказывает."""
        from fastapi.testclient import TestClient
        client = TestClient(self._app)
        token = self._make_su_token()
        # Сначала сделаем report_chat
        self._toggle(client, -1001234567890, "report_chat", token)
        cs = self._get_chat(-1001234567890)
        self.assertTrue(cs.is_report_chat, "Pre-condition: chat must be report_chat")
        # Теперь попробуем mod_chat
        resp = self._toggle(client, -1001234567890, "mod_chat", token)
        self.assertEqual(resp.status_code, 303)
        cs = self._get_chat(-1001234567890)
        self.assertFalse(cs.is_mod_chat,
                         "mod_chat must NOT be set when chat is already report_chat")
        self.assertIn("cannot", resp.headers.get("location", "").lower(),
                      "Refusal must produce flash with 'cannot' in URL")

    def test_73_report_chat_refuses_if_mod_chat(self):
        """Если чат уже is_mod_chat — toggle report_chat отказывает."""
        from fastapi.testclient import TestClient
        client = TestClient(self._app)
        token = self._make_su_token()
        # Сначала сделаем mod_chat
        self._toggle(client, -1001234567890, "mod_chat", token)
        cs = self._get_chat(-1001234567890)
        self.assertTrue(cs.is_mod_chat, "Pre-condition: chat must be mod_chat")
        # Теперь попробуем report_chat
        resp = self._toggle(client, -1001234567890, "report_chat", token)
        self.assertEqual(resp.status_code, 303)
        cs = self._get_chat(-1001234567890)
        self.assertFalse(cs.is_report_chat,
                         "report_chat must NOT be set when chat is already mod_chat")
        self.assertIn("cannot", resp.headers.get("location", "").lower(),
                      "Refusal must produce flash with 'cannot' in URL")

    def test_74_mod_chat_only_one_at_time(self):
        """Modchat может быть только один — при назначении нового старый сбрасывается."""
        from fastapi.testclient import TestClient
        client = TestClient(self._app)
        token = self._make_su_token()
        # Создаём второй чат
        async def _create_chat2():
            async with self.AsyncSessionLocal() as s:
                cs = self._db.ChatSettings(
                    chat_id=-1009876543210,
                    title="Test Chat 2",
                    is_enabled=True,
                )
                s.add(cs)
                await s.commit()
        asyncio.run(_create_chat2())
        # Назначаем первый modchat
        self._toggle(client, -1001234567890, "mod_chat", token)
        cs1 = self._get_chat(-1001234567890)
        self.assertTrue(cs1.is_mod_chat)
        # Назначаем второй modchat
        self._toggle(client, -1009876543210, "mod_chat", token)
        cs1 = self._get_chat(-1001234567890)
        cs2 = self._get_chat(-1009876543210)
        self.assertFalse(cs1.is_mod_chat,
                         "Previous mod_chat must be cleared when new chat is assigned")
        self.assertTrue(cs2.is_mod_chat,
                        "New chat must become mod_chat")
        self.assertEqual(cs2.mod_chat_id, -1009876543210)

    def test_75_invalid_field_rejected(self):
        """Неверное поле field=invalid — redirect с flash=Invalid."""
        from fastapi.testclient import TestClient
        client = TestClient(self._app)
        token = self._make_su_token()
        resp = client.post(
            "/admin/chats/-1001234567890/toggle",
            cookies={self._web_app.COOKIE_NAME: token},
            data={"field": "invalid_field"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        self.assertIn("Invalid", resp.headers.get("location", ""),
                      "Invalid field must produce flash=Invalid in URL")

    def test_76_toggle_invalid_chat_id(self):
        """Неверный chat_id (строка) — redirect с flash=Invalid."""
        from fastapi.testclient import TestClient
        client = TestClient(self._app)
        token = self._make_su_token()
        resp = client.post(
            "/admin/chats/not-a-number/toggle",
            cookies={self._web_app.COOKIE_NAME: token},
            data={"field": "mod_chat"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        self.assertIn("Invalid", resp.headers.get("location", ""),
                      "Invalid chat_id must produce flash=Invalid in URL")

    def test_77_toggle_chat_not_found(self):
        """Несуществующий chat_id — redirect с flash=not+found."""
        from fastapi.testclient import TestClient
        client = TestClient(self._app)
        token = self._make_su_token()
        resp = client.post(
            "/admin/chats/-1005555555555/toggle",
            cookies={self._web_app.COOKIE_NAME: token},
            data={"field": "mod_chat"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        self.assertIn("not+found", resp.headers.get("location", "").lower(),
                      "Nonexistent chat must produce flash=not+found")


# ============================================================================
# 5. Регрессия: существующие toggles всё ещё работают
# ============================================================================

class TestNoRegression(unittest.TestCase):
    """Существующие поля toggle не сломаны после добавления mod_chat."""

    def test_80_existing_toggle_fields_preserved(self):
        """Все 7 существующих полей toggle должны остаться в valid_fields."""
        src = _read(WEB_APP_PY)
        m = re.search(r'valid_fields\s*=\s*\{([^}]+)\}', src)
        self.assertIsNotNone(m)
        contents = m.group(1)
        for field in ("enabled", "report_chat", "cas", "link_filter",
                      "night_mode", "sanitary_days", "via_bot_filter"):
            self.assertIn(f'"{field}"', contents,
                          f"Existing field '{field}' must still be in valid_fields")

    def test_81_all_old_routes_still_exist(self):
        """Все старые /admin/* маршруты на месте.

        v4.9.0 (Task 9): /admin/users и /admin/users/create переехали в
        web/admin_users.py (декоратор @router.get/@router.post вместо
        @app.get/@app.post). Собираем маршруты из обоих файлов — смысл
        проверки прежний, изменился только источник для двух путей.
        """
        route_paths = set()
        for path in (WEB_APP_PY, ADMIN_USERS_PY):
            try:
                tree = ast.parse(_read(path))
            except SyntaxError as e:
                self.fail(f"Syntax error in {path}: {e}")

            for node in ast.walk(tree):
                if isinstance(node, ast.AsyncFunctionDef):
                    for dec in node.decorator_list:
                        if isinstance(dec, ast.Call):
                            if isinstance(dec.func, ast.Attribute):
                                attr = dec.func.attr
                                if attr in ("get", "post") and dec.args:
                                    first_arg = dec.args[0]
                                    if isinstance(first_arg, ast.Constant):
                                        route_paths.add(first_arg.value)

        expected_routes = [
            "/admin/users",
            "/admin/users/create",
            "/admin/chats",
            "/admin/chats/{chat_id_str}/update",
            "/admin/chats/{chat_id_str}/toggle",
            "/admin/chats/{chat_id_str}/delete",
            "/admin/chats/{chat_id_str}/sync-admins",
            "/admin/presets",
            "/admin/presets/create",
        ]
        for r in expected_routes:
            self.assertIn(r, route_paths, f"Existing route {r} missing")

    def test_82_db_keywordwatch_model_unchanged(self):
        """Модель KeywordWatch в db.py не должна быть изменена регрессионно."""
        src = _read(DB_PY)
        # Core fields
        for field in ("phrase", "ban_in_night_mode", "is_active",
                      "rules_section", "chat_id", "created_at", "id"):
            self.assertIn(field, src,
                          f"KeywordWatch field '{field}' must still be in db.py")

    def test_83_mod_chat_fields_in_chat_settings(self):
        """ChatSettings.mod_chat_id и is_mod_chat на месте."""
        src = _read(DB_PY)
        self.assertIn("mod_chat_id", src)
        self.assertIn("is_mod_chat", src)

    def test_84_changelog_v480_section_exists(self):
        """Changelog v4.8.0 в base.html не удалён."""
        html = _read(BASE_HTML)
        self.assertIn("v4.8.0", html)
        self.assertIn("modchat", html.lower())
        self.assertIn("keyword-watch", html.lower())


# ============================================================================
# 6. Дымовой тест — приложение запускается без ошибок
# ============================================================================

class TestSmokeAppCreation(unittest.TestCase):
    """Приложение создаётся без ошибок после изменений."""

    def test_90_create_app_returns_fastapi(self):
        """create_app() возвращает FastAPI-приложение без ошибок."""
        import web_app
        from fastapi import FastAPI
        app = web_app.create_app()
        self.assertIsInstance(app, FastAPI, "create_app must return FastAPI instance")

    def test_91_all_keyword_routes_registered(self):
        """Все 4 keyword-маршрута зарегистрированы в приложении.

        v4.9.0 (Task 5): роуты подключаются через app.include_router(...),
        а Starlette 1.6 кладёт в app.routes не сами Route, а обёртку
        _IncludedRouter — плоский обход через hasattr(route, "path") их
        больше не видит. Разворачиваем так же, как test_v490_decomposition.py
        (_walk): смысл проверки прежний, изменился только способ дойти
        до реальных Route.
        """
        from starlette.routing import Route
        import web_app

        def _walk(routes):
            for r in routes:
                if isinstance(r, Route):
                    yield r
                elif hasattr(r, "original_router"):
                    yield from _walk(r.original_router.routes)

        app = web_app.create_app()
        paths = {r.path for r in _walk(app.routes)}
        self.assertIn("/admin/keywords", paths)
        self.assertIn("/admin/keywords/add", paths)
        self.assertIn("/admin/keywords/{keyword_id:int}/delete", paths)
        self.assertIn("/admin/keywords/{keyword_id:int}/toggle-ban-night", paths)


if __name__ == "__main__":
    unittest.main(verbosity=2)
