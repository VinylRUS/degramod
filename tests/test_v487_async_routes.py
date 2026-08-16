"""v4.8.7 — end-to-end тесты всех 4 async роутов с блокирующим SQLite.

Проверяет что после refactoring:
  - /admin/settings рендерится (async _bot_info работает)
  - /admin/settings/backup создаёт файл (async shutil.copy2)
  - /admin/settings/vacuum работает (async VACUUM)
  - /admin/cleanup работает (3 blocking блока → async)
  - Логин как SU работает с hmac.compare_digest + secure cookie
  - Token expiry работает через HTTP (протухшая кука → /login redirect)

Запуск: python scripts/test_v487_async_routes.py
"""
from _paths import _P  # noqa: E402  (корень вычисляется от __file__)
import os
import sys
import asyncio
import tempfile
import time
import json
import hmac
import hashlib

os.environ.setdefault("BOT_TOKEN", "test:test")
os.environ["ADMIN_IDS"] = "123"
os.environ["WEB_ALLOW_NO_SECRET"] = "1"
os.environ["WEB_COOKIE_SECURE"] = "0"  # TestClient uses http://

# Set WEB_PASSWORD for SU login
os.environ["WEB_PASSWORD"] = "testpass123"

sys.path.insert(0, _P())
sys.path.insert(0, _P())

print("=== v4.8.7 end-to-end async routes test ===\n")

# Use temp DB
tmpdir = tempfile.mkdtemp()
db_path = os.path.join(tmpdir, "test.db")
os.environ["DB_PATH"] = db_path

import importlib
import db as _db
importlib.reload(_db)
import web_app
importlib.reload(web_app)
import bot_handlers
importlib.reload(bot_handlers)

# Init DB schema
asyncio.run(_db.init_db())
print(f"[setup] DB initialized at {db_path}")

# Create app
app = web_app.create_app(bot=None)
print(f"[setup] app created, APP_VERSION={web_app.APP_VERSION}")

from fastapi.testclient import TestClient

results = []

def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    results.append((name, cond))
    print(f"  [{status}] {name}" + (f" — {detail}" if detail and not cond else ""))

# === Test 1: Login as SU ===
print("\n[1] Login as SU (hmac.compare_digest + secure cookie path)...")
client = TestClient(app)
client.__enter__()
# Wrong password — single attempt to avoid rate-limit
r = client.post("/login", data={"username": "su", "password": "wrongpass"}, follow_redirects=False)
check("wrong password → login.html with error", r.status_code == 200 and "error" in r.text.lower(),
      f"got status={r.status_code}")
# Correct password
r = client.post("/login", data={"username": "su", "password": "testpass123"}, follow_redirects=False)
check("correct password → 303 redirect to /dashboard",
      r.status_code == 303 and "/dashboard" in r.headers.get("location", ""),
      f"got status={r.status_code} location={r.headers.get('location')}")

# === Test 2: /admin/settings renders ===
print("\n[2] /admin/settings renders with async _bot_info()...")
r = client.get("/admin/settings", follow_redirects=False)
check("GET /admin/settings → 200", r.status_code == 200, f"got {r.status_code}")
if r.status_code == 200:
    text = r.text
    check("page contains 'Bot info'", "Bot info" in text or "bot-info" in text.lower())
    check("page contains 'Online' badge", "Online" in text or "online" in text.lower())
    check("page contains 'Database maintenance'", "Database maintenance" in text or "Cleanup" in text or "Backup" in text)
    check("page contains v4.8.7", "v4.8.7" in text)
    check("page contains uptime", "uptime" in text.lower() or "Uptime" in text)

# === Test 3: /admin/settings/backup ===
print("\n[3] /admin/settings/backup creates backup file (async shutil.copy2)...")
r = client.post("/admin/settings/backup", follow_redirects=False)
check("POST /admin/settings/backup → 303", r.status_code == 303, f"got {r.status_code}")
if r.status_code == 303:
    loc = r.headers.get("location", "")
    check("redirect flash mentions 'Backup'", "Backup" in loc, f"location={loc}")
    # Check backup file actually created
    backup_files = [f for f in os.listdir(tmpdir) if ".backup-" in f]
    check("backup file created on disk", len(backup_files) >= 1,
          f"found {len(backup_files)} backup files in {tmpdir}")

# === Test 4: /admin/settings/vacuum ===
print("\n[4] /admin/settings/vacuum runs VACUUM (async)...")
r = client.post("/admin/settings/vacuum", follow_redirects=False)
check("POST /admin/settings/vacuum → 303", r.status_code == 303, f"got {r.status_code}")
if r.status_code == 303:
    loc = r.headers.get("location", "")
    check("redirect flash mentions 'VACUUM'", "VACUUM" in loc, f"location={loc}")

# === Test 5: /admin/cleanup (init_db seeds 1 moderator, so safety check passes) ===
print("\n[5] /admin/cleanup — full flow (backup + delete + VACUUM via async)...")
r = client.post("/admin/cleanup", data={"include_chat_admins": "on"}, follow_redirects=False)
check("POST /admin/cleanup → 303", r.status_code == 303, f"got {r.status_code}")
if r.status_code == 303:
    loc = r.headers.get("location", "")
    # init_db seeds default moderator — cleanup should run successfully (not refused)
    check("redirect flash mentions 'Cleanup complete' (not refused)",
          "Cleanup+complete" in loc, f"location={loc}")
    # Backup file from /cleanup should exist
    backup_files = [f for f in os.listdir(tmpdir) if ".backup-" in f]
    check("≥2 backup files exist (1 from /backup, 1 from /cleanup)",
          len(backup_files) >= 2, f"found {len(backup_files)}")

# === Test 6: Token expiry via HTTP ===
print("\n[6] Token expiry — expired cookie → /login redirect...")
with TestClient(app) as client:
    # Forge an expired token (issued 8 days ago, TTL=7 days)
    payload = {
        "u": "su", "s": 1, "r": "su",
        "t": int(time.time()) - (86400 * 8),  # 8 days ago
        "n": "expired",
    }
    raw = json.dumps(payload, separators=(",", ":"))
    sig = hmac.new(web_app._SESSION_SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()
    expired_token = f"{raw}:{sig}"

    r = client.get("/admin/settings", cookies={"sl_session": expired_token}, follow_redirects=False)
    check("expired token → 303 redirect to /login",
          r.status_code == 303 and "/login" in r.headers.get("location", ""),
          f"got status={r.status_code} location={r.headers.get('location')}")

    # Forge a future-dated token (clock skew attack)
    payload = {
        "u": "su", "s": 1, "r": "su",
        "t": int(time.time()) + 1000,  # 1000s in future
        "n": "future",
    }
    raw = json.dumps(payload, separators=(",", ":"))
    sig = hmac.new(web_app._SESSION_SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()
    future_token = f"{raw}:{sig}"

    r = client.get("/admin/settings", cookies={"sl_session": future_token}, follow_redirects=False)
    check("future-dated token → 303 redirect to /login",
          r.status_code == 303 and "/login" in r.headers.get("location", ""),
          f"got status={r.status_code} location={r.headers.get('location')}")

# === Test 7: SESSION_SECRET required (without WEB_ALLOW_NO_SECRET) ===
print("\n[7] SESSION_SECRET required — create_app fails without env...")
# Save and restore env
saved = os.environ.pop("WEB_ALLOW_NO_SECRET", None)
saved_secret = os.environ.pop("SESSION_SECRET", None)
try:
    importlib.reload(web_app)
    try:
        app2 = web_app.create_app(bot=None)
        check("create_app without SESSION_SECRET raises RuntimeError", False,
              "no exception raised")
    except RuntimeError as e:
        msg = str(e)
        check("create_app without SESSION_SECRET raises RuntimeError", True)
        check("error message mentions SESSION_SECRET", "SESSION_SECRET" in msg, f"msg={msg[:100]}")
        check("error message mentions Bothost", "Bothost" in msg or "python -c" in msg,
              f"msg={msg[:100]}")
finally:
    if saved is not None:
        os.environ["WEB_ALLOW_NO_SECRET"] = saved
    if saved_secret is not None:
        os.environ["SESSION_SECRET"] = saved_secret
    importlib.reload(web_app)

# === Summary ===
print()
print("=" * 60)
n_pass = sum(1 for _, ok in results if ok)
n_total = len(results)
print(f"RESULTS: {n_pass}/{n_total} PASS")
print("=" * 60)

# Было: sys.exit(0/1) в теле модуля. Под pytest тело выполняется при импорте,
# и SystemExit обрывал весь прогон (INTERNALERROR). Итог оформлен тестом,
# sys.exit оставлен для запуска файла напрямую.


def test_all_async_route_checks_passed():
    """Итог проверок async-роутов."""
    failed = [name for name, ok in results if not ok]
    assert not failed, f"{len(failed)} из {n_total} проверок упало: " + ", ".join(failed)


if __name__ == "__main__":
    if n_pass == n_total:
        print("ALL TESTS PASSED")
        sys.exit(0)
    print("FAILURES:")
    for name, ok in results:
        if not ok:
            print(f"  - {name}")
    sys.exit(1)
