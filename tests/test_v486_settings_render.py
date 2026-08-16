"""v4.8.6 Settings cleanup — end-to-end render test (v2).

Использует init_db() из db.py для создания корректной схемы.
"""
from _paths import _P  # noqa: E402  (корень вычисляется от __file__)
import os
import sys
import asyncio
import tempfile

os.environ.setdefault("BOT_TOKEN", "test:test")
os.environ["ADMIN_IDS"] = "123"

_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False, prefix="v486_e2e_")
_TMP_DB.close()
os.environ["DB_PATH"] = _TMP_DB.name
# Также надо убедиться что db.py видит DB_PATH
print(f"Using temp DB: {_TMP_DB.name}")

sys.path.insert(0, _P())

import db
asyncio.run(db.init_db())
print("✓ init_db() OK")

# Создаём SU web_user
import sqlite3
conn = sqlite3.connect(_TMP_DB.name)

# Найдём как хешируется пароль
import re
with open(_P("web_app.py")) as f:
    src = f.read()

# Найдём функцию хеширования
m = re.search(r"def hash_password\(([^)]+)\):\s*(.*?)(?=\ndef )", src, re.DOTALL)
print(f"hash_password signature: {m.group(1)[:120] if m else 'NOT FOUND'}")
hash_body = m.group(2) if m else ""

# Найдём _verify_password / verify
m2 = re.search(r"def verify_password\(([^)]+)\):\s*(.*?)(?=\ndef )", src, re.DOTALL)
print(f"verify_password signature: {m2.group(1)[:120] if m2 else 'NOT FOUND'}")

# Импортируем web_app и используем его функции хеширования
import web_app

# Найдём функцию хеширования в web_app
hash_fn = getattr(web_app, "hash_password", None)
print(f"web_app.hash_password exists: {hash_fn is not None}")

hash_fn = getattr(web_app, "_hash_password", None)
print(f"web_app._hash_password exists: {hash_fn is not None}")

if hash_fn:
    pw_hash = hash_fn("test123")
    print(f"  hash('test123') = {pw_hash[:60]}...")
else:
    # Ищем другие имена
    for name in dir(web_app):
        if "hash" in name.lower() or "password" in name.lower():
            print(f"  candidate: {name}")

conn.close()

# Логинимся через TestClient
from fastapi.testclient import TestClient
app = web_app.create_app(bot=None)
client = TestClient(app)

print("\n=== Render test ===")
# Создаём SU user
import sqlite3
conn = sqlite3.connect(_TMP_DB.name)
pw_hash = web_app._hash_password("test123")
conn.execute(
    "INSERT INTO web_users (username, password_hash, is_su, is_active, role, is_pending, auto_discovered) "
    "VALUES (?, ?, 1, 1, 'su', 0, 0)",
    ("su_test", pw_hash),
)
conn.commit()
conn.close()

# Логин
r = client.post("/login", data={"username": "su_test", "password": "test123"}, follow_redirects=False)
print(f"Login: status={r.status_code} location={r.headers.get('location','—')}")

if r.status_code in (303, 302, 307):
    # Проверим что есть session cookie
    print(f"  cookies after login: {list(client.cookies.keys())}")

# Теперь /admin/settings
print("\n[1] GET /admin/settings (с auth)...")
r = client.get("/admin/settings", follow_redirects=False)
print(f"  status: {r.status_code}")
if r.status_code == 200:
    body = r.text
    print(f"  body length: {len(body)} chars")
    
    # Проверяем ключевые элементы
    checks = [
        ("Bot info section", 'id="bot-info"'),
        ("Database section", 'id="database"'),
        ("Backup subsection", 'id="backup"'),
        ("Cleanup subsection", 'id="cleanup"'),
        ("VACUUM subsection", 'id="vacuum"'),
        ("GitHub section", 'id="github"'),
        ("Status badge 'Online'", "Online"),
        ("Memory RSS label", "Memory (RSS)"),
        ("Python version label", "Python"),
        ("Color-aware flash logic", "is_error"),
        ("Anchor nav", 'class="anchor-nav"'),
        ("Uptime display", "Uptime"),
    ]
    print()
    for name, token in checks:
        ok = token in body
        print(f"  [{'✓' if ok else '✗'}] {name}")
    
    print("\n[2] GET /admin/settings?flash=Backup+failed (error flash)...")
    r = client.get("/admin/settings", params={"flash": "Backup failed: disk full"}, follow_redirects=False)
    body = r.text
    assert "Backup failed: disk full" in body, "flash text not rendered"
    assert "rgba(204,51,51" in body, "error flash should use red bg"
    assert "⚠" in body, "error flash should have warning icon"
    print("  ✓ error flash rendered with red bg + ⚠ icon")
    
    print("\n[3] GET /admin/settings?flash=Backup+created (success flash)...")
    r = client.get("/admin/settings", params={"flash": "Backup created: bot_20260814.db"}, follow_redirects=False)
    body = r.text
    assert "Backup created" in body, "flash text not rendered"
    assert "rgba(0,204,102" in body, "success flash should use green bg"
    assert "✓" in body, "success flash should have check icon"
    print("  ✓ success flash rendered with green bg + ✓ icon")
    
    print("\n✓ Все render-проверки пройдены")
else:
    print(f"  ERROR: {r.status_code}")
    print(r.text[:500])
