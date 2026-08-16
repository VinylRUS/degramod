#!/usr/bin/env python3
"""v4.8.8 functional test v2: с явной передачей cookie в headers.

TestClient/httpx плохо парсит cookie с JSON (запятые, фигурные скобки).
Обход: достаём set-cookie из response и отправляем обратно через Cookie header.
"""
from _paths import _P  # noqa: E402  (корень вычисляется от __file__)
import os
import sys
import tempfile
import asyncio
import re
from pathlib import Path

tmpdir = tempfile.mkdtemp()
db_path = str(Path(tmpdir) / "test.db")
os.environ.update({
    "DB_PATH": db_path, "BOT_TOKEN": "1:test",
    "WEB_PASSWORD": "test-su-password", "SESSION_SECRET": "a" * 64,
    "ADMIN_IDS": "", "GITHUB_PAT_ENC_KEY": "", "WEB_ALLOW_NO_SECRET": "1",
})
sys.path.insert(0, _P())

import db
import web_app

asyncio.run(db.init_db())
app = web_app.create_app(bot=None)
from fastapi.testclient import TestClient
client = TestClient(app)

# Логинимся и достаём set-cookie
r = client.post("/login", data={"username": "su", "password": "test-su-password"}, follow_redirects=False)
assert r.status_code == 303, f"login failed: {r.status_code}"
print(f"1. Login: {r.status_code} → {r.headers.get('location')}")

# Достаём cookie и отправляем вручную
set_cookie = r.headers.get("set-cookie", "")
# Берём всё до первой ';'
m = re.match(r"sl_session=([^;]+)", set_cookie)
assert m, f"sl_session cookie not found in set-cookie: {set_cookie[:100]}"
cookie_value = m.group(1)
print(f"   Cookie: sl_session={cookie_value[:60]}...")

# Хелпер: запрос с cookie
def get_with_cookie(path):
    return client.get(path, follow_redirects=False, headers={"Cookie": f"sl_session={cookie_value}"})

def post_with_cookie(path, data=None):
    return client.post(path, data=data or {}, follow_redirects=False, headers={"Cookie": f"sl_session={cookie_value}"})

# Проверка 1: GET /admin/settings с cookie
print("\n2. GET /admin/settings с cookie...")
r = get_with_cookie("/admin/settings")
assert r.status_code == 200, f"ожидался 200, получили {r.status_code}"
print(f"   OK: {r.status_code}")

# Достаём csrf_token из HTML
m = re.search(r'name="csrf_token" value="([^"]+)"', r.text)
assert m, "csrf_token не найден в HTML"
csrf_token = m.group(1)
print(f"   csrf_token: {csrf_token[:16]}... (len={len(csrf_token)})")

# Проверка 2: POST без csrf_token → 403
print("\n3. POST /admin/settings/vacuum БЕЗ csrf_token → ожидаем 403...")
r = post_with_cookie("/admin/settings/vacuum")
assert r.status_code == 403, f"ожидался 403, получили {r.status_code}"
print(f"   OK: {r.status_code} (CSRF защита работает)")

# Проверка 3: POST с правильным csrf_token → 303
print("\n4. POST /admin/settings/vacuum с правильным csrf_token → ожидаем 303...")
r = post_with_cookie("/admin/settings/vacuum", data={"csrf_token": csrf_token})
assert r.status_code == 303, f"ожидался 303, получили {r.status_code} {r.text[:200]}"
print(f"   OK: {r.status_code} → {r.headers.get('location')}")

# Проверка 4: POST с чужим csrf_token → 403
print("\n5. POST /admin/settings/vacuum с ЧУЖИМ csrf_token → ожидаем 403...")
r = post_with_cookie("/admin/settings/vacuum", data={"csrf_token": "wrong_token_aaaaaaaaaaaaaaaa"})
assert r.status_code == 403, f"ожидался 403, получили {r.status_code}"
print(f"   OK: {r.status_code}")

# Проверка 5: /login рендерится без csrf_token в форме (для незалогиненного)
print("\n6. GET /login без cookie → csrf_field рендерится как пустая строка...")
# Отдельный клиент без cookie jar: выше по скрипту client уже залогинился, и
# его кука сессии уходит вместе с запросом — тогда csrf_token в контексте есть
# и input рендерится штатно. Проверять анонимный рендер нужно анонимным клиентом.
_anon = TestClient(app)
r = _anon.get("/login", follow_redirects=False)
assert r.status_code == 200, f"/login: {r.status_code}"
# Для незалогиненного _csrf_token_from_request вернёт "", и csrf_field() вернёт ""
# Поэтому <input name="csrf_token"> не должен появиться в HTML.
# Упоминания "csrf_token" в changelog modal — это просто текст, не input.
import re as _re
input_match = _re.search(r'<input[^>]*name="csrf_token"', r.text)
assert input_match is None, \
    f"на /login не должно быть csrf input для незалогиненного, найдено: {input_match.group(0)[:80] if input_match else ''}"
print(f"   OK: /login отрендерился без csrf input для незалогиненного")

# Проверка 6: TRUSTED_PROXIES пустой → XFF игнорируется
print("\n7. TRUSTED_PROXIES пустой → XFF игнорируется...")
class FakeClient:
    host = "127.0.0.1"
class FakeRequest:
    client = FakeClient()
    headers = {"X-Forwarded-For": "1.2.3.4"}
ip = web_app._client_ip(FakeRequest())
assert ip == "127.0.0.1", f"ожидался 127.0.0.1, получили {ip}"
print(f"   OK: ip = {ip} (XFF проигнорирован)")

# Проверка 7: TRUSTED_PROXIES=127.0.0.1 → XFF учитывается
print("\n8. TRUSTED_PROXIES=127.0.0.1 → XFF учитывается...")
web_app._TRUSTED_PROXIES = frozenset({"127.0.0.1"})
ip = web_app._client_ip(FakeRequest())
assert ip == "1.2.3.4", f"ожидался 1.2.3.4, получили {ip}"
print(f"   OK: ip = {ip} (XFF учтён, т.к. peer доверенный)")
web_app._TRUSTED_PROXIES = frozenset()

# Проверка 8: /api/unban работает без CSRF (excluded, но требует auth)
print("\n9. /api/unban с auth, без CSRF → ожидаем не 403...")
# Этот роут excluded из CSRF, но требует require_auth.
# Если отправить без csrf_token, должно пройти auth (но возможно вернуть 400/404
# из-за неверных данных формы — это ок, главное не 403 CSRF).
r = post_with_cookie("/api/unban", data={"ban_id": "99999"})
# 403 от require_auth (если бан не найден) или от require_auth (если нет сессии) — не CSRF
# Главное: не "CSRF token missing"
csrf_blocked = r.status_code == 403 and "CSRF" in r.text
assert not csrf_blocked, f"/api/unban не должен блокироваться CSRF: {r.status_code} {r.text[:200]}"
print(f"   OK: {r.status_code} (CSRF не блокирует /api/unban)")

print("\n=== ALL TESTS PASSED ===")
