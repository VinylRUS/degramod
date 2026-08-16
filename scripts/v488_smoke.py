#!/usr/bin/env python3
"""v4.8.8 smoke test: проверить что web_app.py импортируется и create_app()
работает. Без этой проверки любой синтаксис/импорт-лаž в web_app.py не будет
обнаружен, пока не деплойнут в прод.
"""
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

sys.path.insert(0, "/home/z/my-project/v488_work")

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
print(f"   OK, роутов: {len(app.routes)}")

print("4. Поиск POST-роутов с CSRF...")
post_routes = []
for r in app.routes:
    if hasattr(r, "methods") and "POST" in r.methods:
        post_routes.append(r.path)
print(f"   POST-роутов: {len(post_routes)}")

print("5. Поиск CSRF зависимостей в исходнике...")
src = Path("/home/z/my-project/v488_work/web_app.py").read_text(encoding="utf-8")
csrf_count = src.count("Depends(require_csrf_")
print(f"   Depends(require_csrf_*): {csrf_count}")

print("6. Поиск старых зависимостей (только в POST-роутах должно быть 0)...")
# Простая проверка: count всех Depends(require_auth/su/admin) — это нормально,
# GET-роуты должны их использовать
old_count = src.count("Depends(require_auth)") + src.count("Depends(require_su)") + src.count("Depends(require_admin)")
old_csrf = src.count("Depends(require_csrf_auth)") + src.count("Depends(require_csrf_su)") + src.count("Depends(require_csrf_admin)")
print(f"   Старых require_*: {old_count} (включая GET-роуты — это нормально)")
print(f"   Новых require_csrf_*: {old_csrf}")

print("7. Проверка TRUSTED_PROXIES env...")
print(f"   _TRUSTED_PROXIES = {web_app._TRUSTED_PROXIES}")
print(f"   CSRF token for 'su': {web_app._csrf_token_for_username('su')[:16]}...")

print("\nALL OK")
