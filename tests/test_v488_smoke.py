#!/usr/bin/env python3
"""v4.8.8 smoke test: web_app.py импортируется и create_app() работает.

Без этой проверки любая синтаксическая или импортная ошибка в web_app.py
доживёт до прода.

v4.10.1 (Task 18): шаги 1-3 работали и раньше — они падают при импорте, и
pytest это видит. А шаги 4-7 только печатали и после декомпозиции стали
врать: считали POST-роуты наивным обходом app.routes (не видит вынесенные —
в Starlette 1.6 там _IncludedRouter) и грепали CSRF-зависимости в web_app.py,
откуда роуты уехали в web/. Заменены на настоящие assert'ы; проверку
CSRF-инварианта ведёт tests/test_v488_verify_csrf.py, дублировать её здесь
нечего.
"""
from _paths import _P  # noqa: E402  (корень вычисляется от __file__)
import os
import sys
import tempfile
from pathlib import Path

# Изолированное окружение
tmpdir = tempfile.mkdtemp()
db_path = str(Path(tmpdir) / "test.db")

os.environ["DB_PATH"] = db_path
os.environ["BOT_TOKEN"] = "1:test"
os.environ["WEB_PASSWORD"] = "test-su-password"
os.environ["SESSION_SECRET"] = "a" * 64  # 64-char hex не нужен, но >=32
os.environ["ADMIN_IDS"] = ""
os.environ["GITHUB_PAT_ENC_KEY"] = ""
os.environ["WEB_ALLOW_NO_SECRET"] = "1"

sys.path.insert(0, _P())

print("1. Импорт модулей...")
import db
import web_app
print("   OK")

print("2. init_db()...")
import asyncio
asyncio.run(db.init_db())
print("   OK")

print("3. create_app(bot=None)...")
app = web_app.create_app(bot=None)
assert app is not None, "create_app(bot=None) вернул None"


def _walk(routes):
    """Разворачивает вложенные роутеры.

    FastAPI кладёт в app.routes объект _IncludedRouter, а сами роуты прячет
    в его original_router.routes. Без обхода счётчик видит только то, что
    объявлено в самом create_app — после декомпозиции это ноль.
    """
    from starlette.routing import Route
    for r in routes:
        if isinstance(r, Route):
            yield r
        elif hasattr(r, "original_router"):
            yield from _walk(r.original_router.routes)


_routes = list(_walk(app.routes))
print(f"   OK, роутов: {len(_routes)}")
assert len(_routes) >= 50, f"роутов подозрительно мало: {len(_routes)}"

print("4. POST-роуты на месте...")
_post = [r.path for r in _routes if r.methods and "POST" in r.methods]
print(f"   POST-роутов: {len(_post)}")
assert len(_post) >= 30, f"POST-роутов подозрительно мало: {len(_post)}"

print("5. Хелперы авторизации доступны...")
assert callable(web_app._csrf_token_for_username), "нет _csrf_token_for_username"
assert web_app._csrf_token_for_username("su"), "пустой CSRF-токен для su"
print(f"   CSRF token for 'su': {web_app._csrf_token_for_username('su')[:16]}...")

print("6. TRUSTED_PROXIES прочитан...")
assert hasattr(web_app, "_TRUSTED_PROXIES"), "нет _TRUSTED_PROXIES"
print(f"   _TRUSTED_PROXIES = {web_app._TRUSTED_PROXIES}")

print("\nALL OK")
