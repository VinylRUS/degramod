"""
test_v44_tgid_create.py — Тесты v4.4: создание админов через TGID + смена пароля.

Покрывает:
  - db.py: миграция 4 новых колонок в web_users (tg_user_id/tg_first_name/tg_last_name/tg_username)
  - db.py: уникальный индекс на tg_user_id
  - web_app.py: _generate_password (длина, энтропия)
  - web_app.py: _sign_flash / _verify_flash (round-trip, tamper, expired)
  - web_app.py: POST /admin/users/create через TGID (мок bot.get_chat)
  - web_app.py: POST /me/password (смена своего пароля)
  - web_app.py: DELETE /admin/users/{id} (с привязкой TGID)

Запуск:
    cd /home/z/my-project/v4.4
    python3 scripts/test_v44_tgid_create.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Подкладываем путь к проекту
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Изолированная БД во временной папке
TMP_DB = "/tmp/test_v44_tgid_create.db"
if os.path.exists(TMP_DB):
    os.remove(TMP_DB)
os.environ["DB_PATH"] = TMP_DB
os.environ["WEB_PASSWORD"] = "test_su_password_123"
os.environ["SESSION_SECRET"] = "test_session_secret"

PASS_COUNT = 0
FAIL_COUNT = 0
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS_COUNT, FAIL_COUNT
    if cond:
        PASS_COUNT += 1
        print(f"  ✓ {name}")
    else:
        FAIL_COUNT += 1
        FAILURES.append(f"{name}: {detail}")
        print(f"  ✗ {name} — {detail}")


# ──────────────────────────────────────────────────────────────────────────
# 1. Миграция БД: 4 новые колонки в web_users + уникальный индекс
# ──────────────────────────────────────────────────────────────────────────
async def test_db_migration():
    print("\n[1] DB migration: 4 new columns in web_users + unique index")
    from db import init_db, engine, WebUser
    from sqlalchemy import text

    await init_db()

    async with engine.connect() as conn:
        result = await conn.execute(text("PRAGMA table_info(web_users)"))
        columns = {row[1]: row[2] for row in result.fetchall()}
    check("tg_user_id column exists", "tg_user_id" in columns, f"got: {list(columns.keys())}")
    check("tg_first_name column exists", "tg_first_name" in columns)
    check("tg_last_name column exists", "tg_last_name" in columns)
    check("tg_username column exists", "tg_username" in columns)

    # Проверяем уникальный индекс
    async with engine.connect() as conn:
        result = await conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='web_users'"
        ))
        index_names = [row[0] for row in result.fetchall()]
    check("unique index ix_web_users_tg_user_id exists",
          "ix_web_users_tg_user_id" in index_names,
          f"got: {index_names}")

    # Идемпотентность: повторный init_db не падает
    await init_db()
    check("init_db is idempotent (2nd call OK)", True)

    # SU-seed присутствует
    from db import async_session
    from sqlalchemy import select
    async with async_session() as session:
        su = (await session.execute(
            select(WebUser).where(WebUser.username == "su")
        )).scalar_one_or_none()
    check("SU seeded", su is not None)
    check("SU has no tg_user_id (it's env-based)", su.tg_user_id is None)


# ──────────────────────────────────────────────────────────────────────────
# 2. Уникальность tg_user_id
# ──────────────────────────────────────────────────────────────────────────
async def test_tg_user_id_unique():
    print("\n[2] tg_user_id uniqueness constraint")
    from db import async_session, WebUser, _hash_password, engine
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    async with async_session() as session:
        session.add(WebUser(
            username="alice",
            password_hash=_hash_password("password1"),
            tg_user_id=1111111,
            tg_username="alice",
        ))
        await session.commit()

    async with async_session() as session:
        session.add(WebUser(
            username="alice2",
            password_hash=_hash_password("password2"),
            tg_user_id=1111111,  # тот же TGID
            tg_username="alice2",
        ))
        try:
            await session.commit()
            check("duplicate tg_user_id rejected", False, "IntegrityError not raised")
        except IntegrityError:
            check("duplicate tg_user_id rejected", True)
            await session.rollback()

    # NULL tg_user_id — несколько записей могут (например SU)
    async with async_session() as session:
        session.add(WebUser(
            username="bob_without_tg",
            password_hash=_hash_password("pw"),
            tg_user_id=None,
        ))
        await session.commit()
    check("NULL tg_user_id allowed (multiple rows)", True)


# ──────────────────────────────────────────────────────────────────────────
# 3. _generate_password — длина, разные вызовы
# ──────────────────────────────────────────────────────────────────────────
def test_generate_password():
    print("\n[3] _generate_password")
    from web_app import _generate_password, _PASSWORD_LEN
    pw1 = _generate_password()
    pw2 = _generate_password()
    check(f"password length is {_PASSWORD_LEN}", len(pw1) == _PASSWORD_LEN, f"got {len(pw1)}: {pw1!r}")
    check("two calls produce different passwords", pw1 != pw2, f"both: {pw1!r}")
    check("password is base64url-safe chars",
          all(c.isalnum() or c in "-_" for c in pw1),
          f"got: {pw1!r}")
    check("no padding chars", "=" not in pw1)


# ──────────────────────────────────────────────────────────────────────────
# 4. _sign_flash / _verify_flash — round-trip, tamper, expired
# ──────────────────────────────────────────────────────────────────────────
def test_sign_flash():
    print("\n[4] _sign_flash / _verify_flash")
    from web_app import _sign_flash, _verify_flash

    payload = {"u": "alice", "p": "secret_pw", "tg": 12345, "t": int(time.time())}
    token = _sign_flash(payload)
    check("signed token is non-empty string", isinstance(token, str) and len(token) > 20)
    check("token contains separator dot", "." in token)

    verified = _verify_flash(token, max_age_seconds=60)
    check("verify returns payload dict", verified is not None)
    check("verified payload has u=alice", verified and verified.get("u") == "alice")
    check("verified payload has p=secret_pw", verified and verified.get("p") == "secret_pw")
    check("verified payload has tg=12345", verified and verified.get("tg") == 12345)

    # Tampered token (sig часть)
    tampered = token[:-4] + "XXXX"
    check("tampered token rejected", _verify_flash(tampered, max_age_seconds=60) is None)

    # Expired token
    old_payload = {"u": "old", "p": "pw", "t": int(time.time()) - 1000}
    old_token = _sign_flash(old_payload)
    check("expired token rejected (max_age=60)", _verify_flash(old_token, max_age_seconds=60) is None)

    # Garbage input
    check("garbage string rejected", _verify_flash("not_a_token", max_age_seconds=60) is None)
    check("empty string rejected", _verify_flash("", max_age_seconds=60) is None)
    check("token without dot rejected", _verify_flash("abc", max_age_seconds=60) is None)


# ──────────────────────────────────────────────────────────────────────────
# 5. POST /admin/users/create через TGID — полный сценарий
# ──────────────────────────────────────────────────────────────────────────
async def test_create_via_tgid():
    print("\n[5] POST /admin/users/create via TGID (mocked bot)")
    from db import async_session, WebUser, _hash_password, _verify_password, init_db
    from web_app import create_app
    from httpx import AsyncClient, ASGITransport

    # Мокаем бота
    mock_bot = MagicMock()
    mock_bot.get_chat = AsyncMock()

    app = create_app(bot=mock_bot)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # ── Логинимся как SU ────────────────────────────────────────────
        resp = await client.post("/login", data={"username": "su", "password": "test_su_password_123"},
                                  follow_redirects=False)
        check("SU login returns 303", resp.status_code == 303, f"got {resp.status_code}")
        su_cookie = resp.cookies.get("sl_session")
        check("SU session cookie set", su_cookie is not None)

        # ── 5a. Успешное создание ───────────────────────────────────────
        # Мокаем chat с username/first_name/last_name
        fake_chat = types.SimpleNamespace(
            id=5550001, type="private",
            username="Charlie",
            first_name="Charlie",
            last_name="Smith",
        )
        mock_bot.get_chat.return_value = fake_chat

        resp = await client.post(
            "/admin/users/create",
            data={"tg_user_id": "5550001"},
            cookies={"sl_session": su_cookie},
            follow_redirects=False,
        )
        check("create returns 303", resp.status_code == 303, f"got {resp.status_code}")
        check("redirects to /admin/users?created=...",
              resp.headers.get("location", "").startswith("/admin/users?created="),
              f"location: {resp.headers.get('location')}")
        check("bot.get_chat called with 5550001",
              mock_bot.get_chat.called and mock_bot.get_chat.call_args.kwargs.get("chat_id") == 5550001)

        # Извлекаем signed-flash token из location и проверяем что он валиден
        from urllib.parse import urlparse, parse_qs
        loc = resp.headers["location"]
        qs = parse_qs(urlparse(loc).query)
        created_token = qs.get("created", [""])[0]
        check("created token is non-empty", bool(created_token))

        from web_app import _verify_flash
        payload = _verify_flash(created_token, max_age_seconds=180)
        check("created token verifies", payload is not None)
        check("created token has u=charlie", payload and payload.get("u") == "charlie",
              f"got: {payload}")
        check("created token has tg=5550001", payload and payload.get("tg") == 5550001)
        check("created token has p (password)", payload and bool(payload.get("p")))

        # Проверяем что юзер реально сохранён в БД с правильными полями
        from sqlalchemy import select
        async with async_session() as session:
            wu = (await session.execute(
                select(WebUser).where(WebUser.tg_user_id == 5550001)
            )).scalar_one_or_none()
        check("WebUser saved with tg_user_id=5550001", wu is not None)
        check("WebUser.username = 'charlie' (lowercase, no @)", wu and wu.username == "charlie",
              f"got: {wu.username if wu else None}")
        check("WebUser.tg_first_name = 'Charlie'", wu and wu.tg_first_name == "Charlie")
        check("WebUser.tg_last_name = 'Smith'", wu and wu.tg_last_name == "Smith")
        check("WebUser.tg_username = 'charlie'", wu and wu.tg_username == "charlie")
        check("WebUser.is_su = False", wu and wu.is_su is False)
        check("WebUser.is_active = True", wu and wu.is_active is True)
        check("WebUser.password_hash is set (PBKDF2)",
              wu and wu.password_hash and ":" in wu.password_hash)
        # Пароль из flash-токена верно хэширован
        check("password from flash matches hash",
              wu and _verify_password(payload["p"], wu.password_hash))

        # ── 5b. Дубликат TGID ───────────────────────────────────────────
        mock_bot.get_chat.return_value = fake_chat  # тот же chat
        resp = await client.post(
            "/admin/users/create",
            data={"tg_user_id": "5550001"},
            cookies={"sl_session": su_cookie},
            follow_redirects=False,
        )
        loc = resp.headers.get("location", "")
        check("duplicate tg_user_id → flash about already bound",
              "already+bound" in loc, f"location: {loc}")

        # ── 5c. Юзер без @username ──────────────────────────────────────
        mock_bot.get_chat.return_value = types.SimpleNamespace(
            id=5550002, type="private",
            username=None, first_name="NoHandle", last_name="User",
        )
        resp = await client.post(
            "/admin/users/create",
            data={"tg_user_id": "5550002"},
            cookies={"sl_session": su_cookie},
            follow_redirects=False,
        )
        loc = resp.headers.get("location", "")
        check("user without @username → flash about no @username",
              "no+%40username" in loc or "no @username" in loc.replace("+", " "),
              f"location: {loc}")

        # ── 5d. Юзер не общался с ботом (get_chat падает) ────────────────
        mock_bot.get_chat.side_effect = Exception("Bad Request: chat not found")
        resp = await client.post(
            "/admin/users/create",
            data={"tg_user_id": "5550003"},
            cookies={"sl_session": su_cookie},
            follow_redirects=False,
        )
        loc = resp.headers.get("location", "")
        check("get_chat failure → flash about must have interacted",
              "interacted+with+the+bot" in loc, f"location: {loc}")

        # Сбрасываем side_effect для дальнейших тестов
        mock_bot.get_chat.side_effect = None

        # ── 5e. Нечисловой TGID ──────────────────────────────────────────
        resp = await client.post(
            "/admin/users/create",
            data={"tg_user_id": "abc"},
            cookies={"sl_session": su_cookie},
            follow_redirects=False,
        )
        loc = resp.headers.get("location", "")
        check("non-numeric TGID → flash 'must be a number'",
              "must+be+a+number" in loc, f"location: {loc}")

        # ── 5f. Отрицательный TGID ───────────────────────────────────────
        resp = await client.post(
            "/admin/users/create",
            data={"tg_user_id": "-5"},
            cookies={"sl_session": su_cookie},
            follow_redirects=False,
        )
        loc = resp.headers.get("location", "")
        check("negative TGID → flash 'must be positive'",
              "must+be+positive" in loc, f"location: {loc}")

        # ── 5g. SU пытается создать себя (зарезервированный 'su') ─────────
        # Создадим юзера с @username='su' — должно быть отказано
        mock_bot.get_chat.return_value = types.SimpleNamespace(
            id=5550004, type="private",
            username="su", first_name="Sneaky", last_name="User",
        )
        resp = await client.post(
            "/admin/users/create",
            data={"tg_user_id": "5550004"},
            cookies={"sl_session": su_cookie},
            follow_redirects=False,
        )
        loc = resp.headers.get("location", "")
        check("TG user with @username='su' → flash 'reserved'",
              "reserved" in loc, f"location: {loc}")

        # ── 5h. Создание второго админа (другой TGID, другой username) ───
        mock_bot.get_chat.return_value = types.SimpleNamespace(
            id=5550005, type="private",
            username="dave_jones", first_name="Dave", last_name="Jones",
        )
        resp = await client.post(
            "/admin/users/create",
            data={"tg_user_id": "5550005"},
            cookies={"sl_session": su_cookie},
            follow_redirects=False,
        )
        check("second admin create returns 303", resp.status_code == 303)
        async with async_session() as session:
            wu2 = (await session.execute(
                select(WebUser).where(WebUser.tg_user_id == 5550005)
            )).scalar_one_or_none()
        check("second WebUser saved", wu2 is not None)
        check("second WebUser.username = 'dave_jones'", wu2 and wu2.username == "dave_jones")

        # ── 5i. GET /admin/users?created=<token> показывает пароль ───────
        # Используем токен из 5a (если ещё не истёк)
        resp = await client.get(
            f"/admin/users?created={created_token}",
            cookies={"sl_session": su_cookie},
            follow_redirects=False,
        )
        check("GET /admin/users?created= returns 200", resp.status_code == 200)
        check("response contains the password (shown once)",
              payload and payload["p"] in resp.text,
              "password not in HTML")
        check("response contains 'only once' warning",
              "only once" in resp.text.lower() or "shown" in resp.text.lower(),
              "warning text not found")

        # ── 5j. GET с мусорным created-токеном не падает, не показывает пароль ──
        resp = await client.get(
            "/admin/users?created=garbage_token",
            cookies={"sl_session": su_cookie},
            follow_redirects=False,
        )
        check("garbage created token → 200 (no crash)", resp.status_code == 200)
        check("garbage created token → no password in HTML",
              "only once" not in resp.text.lower())

        # ── 5k. Non-SU не может создавать админов ────────────────────────
        # Логинимся как charlie
        resp = await client.post("/login",
                                  data={"username": "charlie", "password": payload["p"]},
                                  follow_redirects=False)
        check("charlie login returns 303", resp.status_code == 303)
        charlie_cookie = resp.cookies.get("sl_session")
        check("charlie session cookie set", charlie_cookie is not None)

        mock_bot.get_chat.return_value = types.SimpleNamespace(
            id=5550006, type="private",
            username="eve", first_name="Eve", last_name=None,
        )
        resp = await client.post(
            "/admin/users/create",
            data={"tg_user_id": "5550006"},
            cookies={"sl_session": charlie_cookie},
            follow_redirects=False,
        )
        # require_su редиректит не-SU на /dashboard
        check("non-SU create → redirect to /dashboard (303)",
              resp.status_code == 303 and "/dashboard" in resp.headers.get("location", ""),
              f"got {resp.status_code}, location={resp.headers.get('location')}")


# ──────────────────────────────────────────────────────────────────────────
# 6. POST /me/password — смена своего пароля
# ──────────────────────────────────────────────────────────────────────────
async def test_me_password():
    print("\n[6] POST /me/password")
    from db import async_session, WebUser, _hash_password, _verify_password
    from web_app import create_app
    from httpx import AsyncClient, ASGITransport
    from unittest.mock import MagicMock, AsyncMock

    # Подготавливаем БД с юзером carol
    async with async_session() as session:
        session.add(WebUser(
            username="carol",
            password_hash=_hash_password("old_pw_123"),
            tg_user_id=6660001,
            tg_username="carol",
        ))
        await session.commit()

    mock_bot = MagicMock()
    mock_bot.get_chat = AsyncMock()
    app = create_app(bot=mock_bot)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Логинимся как carol
        resp = await client.post("/login",
                                  data={"username": "carol", "password": "old_pw_123"},
                                  follow_redirects=False)
        check("carol login returns 303", resp.status_code == 303)
        cookie = resp.cookies.get("sl_session")
        check("carol cookie set", cookie is not None)

        # ── 6a. Неверный старый пароль ──────────────────────────────────
        resp = await client.post(
            "/me/password",
            data={"old_password": "WRONG", "new_password": "new_pw_456", "confirm": "new_pw_456"},
            cookies={"sl_session": cookie},
            follow_redirects=False,
        )
        loc = resp.headers.get("location", "")
        check("wrong old password → flash 'Current password is incorrect'",
              "Current+password+is+incorrect" in loc, f"loc: {loc}")

        # ── 6b. new != confirm ──────────────────────────────────────────
        resp = await client.post(
            "/me/password",
            data={"old_password": "old_pw_123", "new_password": "new_pw_456", "confirm": "different"},
            cookies={"sl_session": cookie},
            follow_redirects=False,
        )
        loc = resp.headers.get("location", "")
        check("mismatched confirm → flash 'do not match'",
              "do+not+match" in loc, f"loc: {loc}")

        # ── 6c. Слишком короткий новый пароль ───────────────────────────
        resp = await client.post(
            "/me/password",
            data={"old_password": "old_pw_123", "new_password": "abc", "confirm": "abc"},
            cookies={"sl_session": cookie},
            follow_redirects=False,
        )
        loc = resp.headers.get("location", "")
        check("short new password → flash 'at least 6 chars'",
              "at+least+6+chars" in loc, f"loc: {loc}")

        # ── 6d. Новый = старый ──────────────────────────────────────────
        resp = await client.post(
            "/me/password",
            data={"old_password": "old_pw_123", "new_password": "old_pw_123", "confirm": "old_pw_123"},
            cookies={"sl_session": cookie},
            follow_redirects=False,
        )
        loc = resp.headers.get("location", "")
        check("new = old → flash 'must differ'",
              "must+differ" in loc, f"loc: {loc}")

        # ── 6e. Успешная смена пароля ───────────────────────────────────
        resp = await client.post(
            "/me/password",
            data={"old_password": "old_pw_123", "new_password": "brand_new_pw_789", "confirm": "brand_new_pw_789"},
            cookies={"sl_session": cookie},
            follow_redirects=False,
        )
        loc = resp.headers.get("location", "")
        check("successful change → flash 'Password changed successfully'",
              "Password+changed+successfully" in loc, f"loc: {loc}")

        # Проверяем что в БД хэш обновился
        from sqlalchemy import select
        async with async_session() as session:
            wu = (await session.execute(
                select(WebUser).where(WebUser.username == "carol")
            )).scalar_one()
        check("new password verifies against updated hash",
              _verify_password("brand_new_pw_789", wu.password_hash))
        check("old password no longer verifies",
              not _verify_password("old_pw_123", wu.password_hash))

        # ── 6f. Старый пароль больше не работает для логина ─────────────
        resp = await client.post("/login",
                                  data={"username": "carol", "password": "old_pw_123"},
                                  follow_redirects=False)
        check("old password rejected at login", resp.status_code == 200)  # 200 = login form re-rendered

        # ── 6g. Новый пароль работает ───────────────────────────────────
        resp = await client.post("/login",
                                  data={"username": "carol", "password": "brand_new_pw_789"},
                                  follow_redirects=False)
        check("new password accepted at login (303)", resp.status_code == 303)

        # ── 6h. SU не может сменить пароль через /me/password ───────────
        resp = await client.post("/login",
                                  data={"username": "su", "password": "test_su_password_123"},
                                  follow_redirects=False)
        su_cookie = resp.cookies.get("sl_session")
        resp = await client.post(
            "/me/password",
            data={"old_password": "x", "new_password": "y", "confirm": "y"},
            cookies={"sl_session": su_cookie},
            follow_redirects=False,
        )
        loc = resp.headers.get("location", "")
        check("SU change attempt → flash 'WEB_PASSWORD env'",
              "WEB_PASSWORD" in loc, f"loc: {loc}")

        # ── 6i. GET /dashboard?pw_msg=... отображает сообщение ──────────
        resp = await client.get(
            "/dashboard?pw_msg=Hello+world",
            cookies={"sl_session": cookie},
            follow_redirects=False,
        )
        check("dashboard shows pw_msg", resp.status_code == 200 and "Hello world" in resp.text)

        # ── 6j. Блок Change my password виден не-SU на дашборде ─────────
        check("dashboard has 'Change my password' section for non-SU",
              "Change my password" in resp.text)

        # ── 6k. SU на дашборде видит предупреждение вместо формы ─────────
        resp = await client.get(
            "/dashboard",
            cookies={"sl_session": su_cookie},
            follow_redirects=False,
        )
        check("dashboard for SU mentions WEB_PASSWORD env",
              "WEB_PASSWORD" in resp.text and "env" in resp.text.lower())


# ──────────────────────────────────────────────────────────────────────────
# 7. DELETE /admin/users/{id} — удаление с привязкой TGID
# ──────────────────────────────────────────────────────────────────────────
async def test_delete_user():
    print("\n[7] DELETE /admin/users/{id}")
    from db import async_session, WebUser, _hash_password
    from web_app import create_app
    from httpx import AsyncClient, ASGITransport
    from unittest.mock import MagicMock, AsyncMock
    from sqlalchemy import select

    # Создаём юзера frank
    async with async_session() as session:
        frank = WebUser(
            username="frank",
            password_hash=_hash_password("frank_pw"),
            tg_user_id=7770001,
            tg_username="frank",
            tg_first_name="Frank",
        )
        session.add(frank)
        await session.commit()
        frank_id = frank.id

    mock_bot = MagicMock()
    mock_bot.get_chat = AsyncMock()
    app = create_app(bot=mock_bot)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Логинимся как SU
        resp = await client.post("/login",
                                  data={"username": "su", "password": "test_su_password_123"},
                                  follow_redirects=False)
        su_cookie = resp.cookies.get("sl_session")

        # ── 7a. Удаление frank ──────────────────────────────────────────
        resp = await client.post(
            f"/admin/users/{frank_id}/delete",
            cookies={"sl_session": su_cookie},
            follow_redirects=False,
        )
        check("delete returns 303", resp.status_code == 303)

        async with async_session() as session:
            frank_after = (await session.execute(
                select(WebUser).where(WebUser.id == frank_id)
            )).scalar_one_or_none()
        check("frank is gone from DB", frank_after is None)

        # ── 7b. SU нельзя удалить ───────────────────────────────────────
        async with async_session() as session:
            su_db = (await session.execute(
                select(WebUser).where(WebUser.username == "su")
            )).scalar_one()
        su_id = su_db.id
        resp = await client.post(
            f"/admin/users/{su_id}/delete",
            cookies={"sl_session": su_cookie},
            follow_redirects=False,
        )
        check("delete SU → 303 (silent refuse)", resp.status_code == 303)
        async with async_session() as session:
            su_after = (await session.execute(
                select(WebUser).where(WebUser.id == su_id)
            )).scalar_one_or_none()
        check("SU still in DB after delete attempt", su_after is not None)

        # ── 7c. После удаления frank — его TGID можно использовать снова ─
        mock_bot.get_chat.return_value = types.SimpleNamespace(
            id=7770001, type="private",
            username="frank_reborn", first_name="Frank", last_name="Reborn",
        )
        resp = await client.post(
            "/admin/users/create",
            data={"tg_user_id": "7770001"},
            cookies={"sl_session": su_cookie},
            follow_redirects=False,
        )
        loc = resp.headers.get("location", "")
        check("reusing TGID of deleted user → success (no 'already bound')",
              "already+bound" not in loc, f"loc: {loc}")


# ──────────────────────────────────────────────────────────────────────────
# 8. create_app(bot=None) — graceful degradation
# ──────────────────────────────────────────────────────────────────────────
async def test_create_app_no_bot():
    print("\n[8] create_app(bot=None) — graceful degradation")
    from web_app import create_app
    from httpx import AsyncClient, ASGITransport

    app = create_app(bot=None)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/login",
                                  data={"username": "su", "password": "test_su_password_123"},
                                  follow_redirects=False)
        su_cookie = resp.cookies.get("sl_session")

        resp = await client.post(
            "/admin/users/create",
            data={"tg_user_id": "12345"},
            cookies={"sl_session": su_cookie},
            follow_redirects=False,
        )
        loc = resp.headers.get("location", "")
        check("bot=None → flash 'Bot instance not available'",
              "Bot+instance+not+available" in loc, f"loc: {loc}")


# ──────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────
async def main():
    print("=" * 70)
    print("v4.4 tests: TGID creation + password change")
    print("=" * 70)
    await test_db_migration()
    await test_tg_user_id_unique()
    test_generate_password()
    test_sign_flash()
    await test_create_via_tgid()
    await test_me_password()
    await test_delete_user()
    await test_create_app_no_bot()

    print("\n" + "=" * 70)
    print(f"PASSED: {PASS_COUNT} | FAILED: {FAIL_COUNT}")
    if FAILURES:
        print("\nFailures:")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("ALL TESTS PASSED ✅")
    sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())
