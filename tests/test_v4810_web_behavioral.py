"""
test_v4810_web_behavioral.py — Behavioral-тесты для веб-панели v4.8.10.

Покрывает ключевые эндпоинты через TestClient:
  - Все 54 роута доступны (не 404) — проверка после декомпозиции create_app()
  - Роуты с require_auth → 303 redirect на /login без cookie
  - /admin/bans, /admin/keywords, /admin/chats, /admin/presets рендерятся с SU
  - POST роуты с require_csrf_* → 403 без CSRF токена
  - Перенесённые в web/ роуты работают (/health, /logout, /, /avatar, /api/presets, /api/automute-count)

Запуск:
    uv run pytest tests/test_v4810_web_behavioral.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

V488_WORK = Path(__file__).resolve().parent.parent / "v488_work"
sys.path.insert(0, str(V488_WORK))

_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False, prefix="v4810_test_")
_tmp_db.close()
os.environ["DB_PATH"] = _tmp_db.name
os.environ["WEB_ALLOW_NO_SECRET"] = "1"
os.environ["WEB_PASSWORD"] = "test-su-password-12345"
os.environ["WEB_COOKIE_SECURE"] = "0"
os.environ["TRUSTED_PROXIES"] = ""

import db  # noqa: E402
import web_app  # noqa: E402
from db import WebUser, async_session, init_db  # noqa: E402
from sqlalchemy import select  # noqa: E402
from starlette.routing import Route  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

_pass_count = 0
_fail_count = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global _pass_count, _fail_count
    if condition:
        print(f"  ✓ {name}")
        _pass_count += 1
    else:
        print(f"  ✗ {name} — {detail}")
        _fail_count += 1


def make_app_with_db() -> TestClient:
    async def _setup():
        await init_db()
        async with async_session() as session:
            existing = (await session.execute(
                select(WebUser).where(WebUser.username == "su")
            )).scalar_one_or_none()
            if existing is None:
                session.add(WebUser(username="su", is_su=True, role="su", is_active=True))
                await session.commit()

    asyncio.run(_setup())
    app = web_app.create_app(bot=None)
    return TestClient(app)


def login_as_su(client: TestClient) -> None:
    client.post("/login", data={"username": "su", "password": "test-su-password-12345"},
                follow_redirects=False)


# ── 1. Все роуты доступны (не 404) ───────────────────────────────────────────
# После декомпозиции create_app() нужно убедиться что все 54 роута зарегистрированы.

EXPECTED_ROUTES = {
    # v4.8.9/4.8.10: перенесённые в web/
    "/health", "/logout", "/", "/avatar/{tg_user_id:int}",
    "/api/presets", "/api/automute-count",
    # auth
    "/login",
    # me
    "/dashboard", "/user/{user_id:int}", "/me", "/me/password", "/me/avatar/refresh",
    # api
    "/api/dashboard", "/api/search", "/api/unban", "/api/reset-automute-count",
    # admin/users
    "/admin/users", "/admin/users/create", "/admin/users/{user_id:int}/toggle",
    "/admin/users/{user_id:int}/reset", "/admin/users/{user_id:int}/role",
    "/admin/users/{user_id:int}/edit-chats", "/admin/users/{user_id:int}/bind-tg",
    "/admin/users/{user_id:int}/delete",
    # admin/chats
    "/admin/chats", "/admin/chats/{chat_id_str}/update", "/admin/chats/{chat_id_str}/toggle",
    "/admin/chats/{chat_id_str}/delete", "/admin/chats/{chat_id_str}/sync-admins",
    "/admin/chats/{chat_id_str}/sanitary/add", "/admin/chats/{chat_id_str}/sanitary/{idx_str}/delete",
    # admin/keywords
    "/admin/keywords", "/admin/keywords/add", "/admin/keywords/{keyword_id:int}/delete",
    "/admin/keywords/{keyword_id:int}/toggle-ban-night",
    # admin/settings
    "/admin/settings", "/admin/settings/backup", "/admin/settings/vacuum",
    "/admin/settings/github", "/admin/settings/github/test",
    # admin/cleanup
    "/admin/cleanup",
    # admin/presets
    "/admin/presets", "/admin/presets/create", "/admin/presets/{preset_id:int}/edit",
    "/admin/presets/{preset_id:int}/delete", "/admin/presets/words/add",
    "/admin/presets/words/{word_id:int}/delete", "/admin/presets/links/add",
    "/admin/presets/links/{link_id:int}/delete",
    # admin/bans
    "/admin/bans",
}


def _walk(routes):
    """Разворачивает вложенные роутеры.

    FastAPI 0.141 кладёт в app.routes объект _IncludedRouter, а сами роуты
    прячет в его original_router.routes. У _IncludedRouter атрибут path
    существует и равен None, поэтому фильтр hasattr(r, "path") его не
    отсеивает и кладёт None в множество путей — без обхода вынесенные в
    web/ роуты не видны.
    """
    for r in routes:
        if isinstance(r, Route):
            yield r
        elif hasattr(r, "original_router"):
            yield from _walk(r.original_router.routes)


def t01_all_routes_registered() -> None:
    """1. Все ожидаемые роуты зарегистрированы в app.routes."""
    client = make_app_with_db()
    actual_routes = {r.path for r in _walk(client.app.routes)}
    missing = EXPECTED_ROUTES - actual_routes
    extra = actual_routes - EXPECTED_ROUTES - {"/openapi.json", "/docs", "/redoc"}
    check("1. Все ожидаемые роуты зарегистрированы",
          len(missing) == 0,
          f"missing: {missing}, extra: {extra}")
    check("1. Количество роутов >= 45",
          len(actual_routes) >= 45,
          f"actual: {len(actual_routes)}")


# ── 2. Роуты с require_auth → 303 redirect без cookie ──────────────────────


def t02_protected_routes_redirect_without_cookie() -> None:
    """2. Защищённые роуты → 303 /login без cookie."""
    client = make_app_with_db()
    protected_get = [
        "/dashboard", "/me", "/admin/users", "/admin/chats",
        "/admin/keywords", "/admin/settings", "/admin/cleanup",
        "/admin/presets", "/admin/bans",
    ]
    failed = []
    for path in protected_get:
        r = client.get(path, follow_redirects=False)
        if not (r.status_code == 303 and "/login" in r.headers.get("location", "")):
            failed.append(f"{path}→{r.status_code}")
    check("2. Защищённые GET → 303 /login без cookie",
          len(failed) == 0,
          f"failed: {failed}")


# ── 3. Admin pages рендерятся с SU cookie ───────────────────────────────────


def t03_admin_pages_render_with_su() -> None:
    """3. /admin/* страницы рендерятся (200) с SU cookie."""
    client = make_app_with_db()
    login_as_su(client)
    # /admin/cleanup — legacy редирект на /admin/settings#cleanup (303), не 200.
    pages = [
        "/dashboard", "/me", "/admin/users", "/admin/chats",
        "/admin/keywords", "/admin/settings",
        "/admin/presets", "/admin/bans",
    ]
    failed = []
    for path in pages:
        r = client.get(path, follow_redirects=False)
        if r.status_code != 200:
            failed.append(f"{path}→{r.status_code}")
    check("3. /admin/* страницы → 200 с SU cookie",
          len(failed) == 0,
          f"failed: {failed}")

    # /admin/cleanup — отдельная проверка: legacy редирект.
    r = client.get("/admin/cleanup", follow_redirects=False)
    check("3. /admin/cleanup → 303 /admin/settings#cleanup (legacy redirect)",
          r.status_code == 303 and "/admin/settings" in r.headers.get("location", ""),
          f"status={r.status_code}, location={r.headers.get('location')}")


# ── 4. POST роуты с require_csrf_* → 403 без CSRF ───────────────────────────


def t04_post_routes_403_without_csrf() -> None:
    """4. POST роуты с require_csrf_* → 403 без CSRF токена."""
    client = make_app_with_db()
    login_as_su(client)
    post_routes = [
        "/admin/settings/vacuum",
        "/admin/settings/backup",
        "/admin/cleanup",
        "/admin/keywords/add",
        "/admin/presets/create",
    ]
    failed = []
    for path in post_routes:
        r = client.post(path, follow_redirects=False)
        if r.status_code != 403:
            failed.append(f"{path}→{r.status_code}")
    check("4. POST роуты → 403 без CSRF токена",
          len(failed) == 0,
          f"failed: {failed}")


# ── 5. Перенесённые в web/ роуты работают ───────────────────────────────────


def t05_migrated_routes_work() -> None:
    """5. Роуты перенесённые в web/ package работают корректно."""
    client = make_app_with_db()
    # /health — без auth
    r = client.get("/health", follow_redirects=False)
    check("5. GET /health → 200 (перенесён в web/health.py)",
          r.status_code == 200 and r.json()["status"] == "ok",
          f"status={r.status_code}")

    # / → 302 /login (перенесён в web/me.py)
    r = client.get("/", follow_redirects=False)
    check("5. GET / → 302 /login (перенесён в web/me.py)",
          r.status_code == 302 and "/login" in r.headers.get("location", ""),
          f"status={r.status_code}")

    # POST /logout → 303 /login (перенесён в web/auth.py)
    r = client.post("/logout", follow_redirects=False)
    check("5. POST /logout → 303 /login (перенесён в web/auth.py)",
          r.status_code == 303 and "/login" in r.headers.get("location", ""),
          f"status={r.status_code}")

    # /api/presets без auth → 303 /login (перенесён в web/api.py)
    r = client.get("/api/presets", follow_redirects=False)
    check("5. GET /api/presets без auth → 303 /login (перенесён в web/api.py)",
          r.status_code == 303 and "/login" in r.headers.get("location", ""),
          f"status={r.status_code}")

    # /api/automute-count без auth → 303 /login (перенесён в web/api.py)
    r = client.get("/api/automute-count?chat_id=1&user_id=2", follow_redirects=False)
    check("5. GET /api/automute-count без auth → 303 /login (перенесён в web/api.py)",
          r.status_code == 303 and "/login" in r.headers.get("location", ""),
          f"status={r.status_code}")


# ── 6. /admin/bans рендерится с фильтрами ───────────────────────────────────


def t06_admin_bans_with_filters() -> None:
    """6. /admin/bans принимает query params (chat_id, q, limit, flash)."""
    client = make_app_with_db()
    login_as_su(client)
    r = client.get("/admin/bans?chat_id=123&q=test&limit=50&flash=ok", follow_redirects=False)
    check("6. GET /admin/bans?chat_id=123&q=test&limit=50 → 200",
          r.status_code == 200,
          f"status={r.status_code}")


# ── 7. /admin/keywords рендерится ───────────────────────────────────────────


def t07_admin_keywords_renders() -> None:
    """7. /admin/keywords рендерится с SU cookie."""
    client = make_app_with_db()
    login_as_su(client)
    r = client.get("/admin/keywords", follow_redirects=False)
    check("7. GET /admin/keywords → 200 с SU",
          r.status_code == 200,
          f"status={r.status_code}")


# ── 8. /api/unban без CSRF не блокируется (excluded) ─────────────────────────


def t08_api_unban_without_csrf_not_blocked() -> None:
    """8. /api/unban без CSRF → не 403 (excluded из CSRF, см. CHANGES_v4.8.8)."""
    client = make_app_with_db()
    login_as_su(client)
    r = client.post("/api/unban", json={}, follow_redirects=False)
    # Может быть 400/422/500 (нет payload), но НЕ 403 (CSRF не блокирует).
    check("8. POST /api/unban без CSRF → не 403 (excluded)",
          r.status_code != 403,
          f"status={r.status_code}")


# ── main ────────────────────────────────────────────────────────────────────


def main() -> int:
    print("=" * 70)
    print("test_v4810_web_behavioral.py — Behavioral-тесты веб-панели v4.8.10")
    print("=" * 70)
    print(f"  Test DB: {os.environ['DB_PATH']}")
    print()

    tests = [
        t01_all_routes_registered,
        t02_protected_routes_redirect_without_cookie,
        t03_admin_pages_render_with_su,
        t04_post_routes_403_without_csrf,
        t05_migrated_routes_work,
        t06_admin_bans_with_filters,
        t07_admin_keywords_renders,
        t08_api_unban_without_csrf_not_blocked,
    ]
    for fn in tests:
        try:
            fn()
        except Exception as e:
            check(fn.__name__, False, f"EXCEPTION: {type(e).__name__}: {e}")
        print()

    print("=" * 70)
    print(f"ИТОГ: {_pass_count}/{_pass_count + _fail_count} проверок прошли")
    print("=" * 70)

    try:
        for ext in ("", "-wal", "-shm"):
            p = os.environ["DB_PATH"] + ext
            if os.path.exists(p):
                os.unlink(p)
    except OSError:
        pass

    return 0 if _fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
