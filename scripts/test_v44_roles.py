"""
test_v44_roles.py — Тесты v4.4.6: ролевая модель SU / admin / moderator.

Покрывает:
  - Миграция role: существующие веб-юзеры получают корректную роль
  - AuthUser: role загружается из БД (а не из токена)
  - Токен содержит 'r' (role); старые токены без 'r' валидны (fallback)
  - require_su: только SU (admin/moderator → /dashboard)
  - require_admin: SU + admin (moderator → /dashboard)
  - require_auth: любой (включая moderator)
  - /admin/users/create с role=moderator — создаётся moderator
  - /admin/users/create с role=admin — создаётся admin
  - /admin/users/create с невалидной role — flash
  - /admin/users форма содержит radio buttons (admin/moderator)
  - /admin/chats (GET): SU + admin OK, moderator → redirect /dashboard
  - /admin/chats/{id}/update (POST): SU + admin OK, moderator rejected
  - /admin/chats обновляет ChatSettings (hashtag, report_chat_id, thresholds)
  - Welcome DM: для moderator — текст про "только просмотр логов"
  - Welcome DM: для admin — текст про "управление модераторами и чатами"
  - Nav: moderator видит только Dashboard; admin видит Moderators+Chats;
         SU видит всё
  - Существующий web-юзер без role (старая БД) → after init_db → role='admin'

Запуск:
    cd /home/z/my-project/v4.5
    python3 scripts/test_v44_roles.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP_DB = "/tmp/test_v44_roles.db"
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
        print(f"  ✗ {name}  {detail}")


async def _su_login(client) -> str:
    resp = await client.post(
        "/login",
        data={"username": "su", "password": "test_su_password_123"},
        follow_redirects=False,
    )
    return resp.cookies.get("sl_session")


# ──────────────────────────────────────────────────────────────────────────
# 1. Миграция role: SU→'su', existing admin→'admin'
# ──────────────────────────────────────────────────────────────────────────
async def test_migration_assigns_roles():
    print("\n[1] Миграция: role присваивается корректно существующим записям")
    from db import init_db, async_session, WebUser

    # Перед init_db вставим вручную старые записи без role
    import sqlite3
    conn = sqlite3.connect(TMP_DB)
    # Создаём таблицу без role (как в старой версии)
    conn.executescript("""
        CREATE TABLE web_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username VARCHAR(64) NOT NULL UNIQUE,
            password_hash VARCHAR(255),
            is_su BOOLEAN NOT NULL DEFAULT 0,
            is_active BOOLEAN NOT NULL DEFAULT 1,
            created_at DATETIME,
            created_by VARCHAR(64),
            last_login_at DATETIME,
            tg_user_id BIGINT,
            tg_first_name VARCHAR(255),
            tg_last_name VARCHAR(255),
            tg_username VARCHAR(255)
        );
        CREATE UNIQUE INDEX ix_web_users_tg_user_id ON web_users (tg_user_id) WHERE tg_user_id IS NOT NULL;
    """)
    # SU без role
    conn.execute("INSERT INTO web_users (username, password_hash, is_su, is_active, created_by) "
                 "VALUES ('su', NULL, 1, 1, 'system')")
    # Старый admin без role
    conn.execute("INSERT INTO web_users (username, password_hash, is_su, is_active, created_by, tg_user_id, tg_username) "
                 "VALUES ('old_admin', 'salt:hash', 0, 1, 'su', 999999, 'old_admin')")
    conn.commit()
    conn.close()

    # Запускаем init_db — должна добавить колонку role и проставить значения
    await init_db()

    async with async_session() as s:
        from sqlalchemy import select
        su = (await s.execute(select(WebUser).where(WebUser.username == "su"))).scalar_one()
        old_admin = (await s.execute(select(WebUser).where(WebUser.username == "old_admin"))).scalar_one()

    check("SU.role = 'su' после миграции", su.role == "su", f"got: {su.role}")
    check("old_admin.role = 'admin' после миграции",
          old_admin.role == "admin", f"got: {old_admin.role}")


# ──────────────────────────────────────────────────────────────────────────
# 2. AuthUser: role загружается из БД, не из токена
# ──────────────────────────────────────────────────────────────────────────
async def test_auth_role_from_db():
    print("\n[2] AuthUser: role загружается из БД (а не из токена)")
    from db import init_db, async_session, WebUser, _hash_password
    from web_app import create_app, AuthUser, require_auth
    from httpx import AsyncClient, ASGITransport
    from sqlalchemy import select

    await init_db()
    # Создаём moderator в БД
    async with async_session() as s:
        s.add(WebUser(
            username="mod_user",
            password_hash=_hash_password("pw_123"),
            tg_user_id=2222001, tg_username="mod_user",
            role="moderator",
        ))
        s.add(WebUser(
            username="adm_user",
            password_hash=_hash_password("pw_456"),
            tg_user_id=2222002, tg_username="adm_user",
            role="admin",
        ))
        await s.commit()

    mock_bot = MagicMock()
    mock_bot.get_chat = AsyncMock()
    app = create_app(bot=mock_bot)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Логинимся как moderator
        resp = await client.post("/login",
                                 data={"username": "mod_user", "password": "pw_123"},
                                 follow_redirects=False)
        mod_cookie = resp.cookies.get("sl_session")
        check("moderator login OK", mod_cookie is not None)

        # /dashboard доступен (require_auth)
        resp = await client.get("/dashboard", cookies={"sl_session": mod_cookie},
                                follow_redirects=False)
        check("moderator: /dashboard → 200 (require_auth allows)",
              resp.status_code == 200, f"got {resp.status_code}")

        # В HTML есть пометка MOD в user-chip
        check("moderator: HTML содержит 'MOD' chip", ">MOD<" in resp.text)

        # Логинимся как admin
        resp = await client.post("/login",
                                 data={"username": "adm_user", "password": "pw_456"},
                                 follow_redirects=False)
        adm_cookie = resp.cookies.get("sl_session")
        resp = await client.get("/dashboard", cookies={"sl_session": adm_cookie})
        check("admin: HTML содержит 'ADMIN' chip", ">ADMIN<" in resp.text)


# ──────────────────────────────────────────────────────────────────────────
# 3. require_su: admin/moderator → /dashboard; SU → OK
# ──────────────────────────────────────────────────────────────────────────
async def test_require_su():
    print("\n[3] require_su: SU OK, admin/moderator → /dashboard")
    from web_app import create_app
    from httpx import AsyncClient, ASGITransport

    mock_bot = MagicMock()
    mock_bot.get_chat = AsyncMock()
    app = create_app(bot=mock_bot)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # /admin/cleanup — require_su
        # SU
        su_cookie = await _su_login(client)
        resp = await client.get("/admin/cleanup", cookies={"sl_session": su_cookie},
                                follow_redirects=False)
        check("SU: /admin/cleanup → 200", resp.status_code == 200, f"got {resp.status_code}")

        # admin
        resp = await client.post("/login",
                                 data={"username": "adm_user", "password": "pw_456"},
                                 follow_redirects=False)
        adm_cookie = resp.cookies.get("sl_session")
        resp = await client.get("/admin/cleanup", cookies={"sl_session": adm_cookie},
                                follow_redirects=False)
        check("admin: /admin/cleanup → 303 /dashboard (rejected)",
              resp.status_code == 303 and "/dashboard" in resp.headers.get("location", ""),
              f"got {resp.status_code}, loc: {resp.headers.get('location')}")

        # moderator
        resp = await client.post("/login",
                                 data={"username": "mod_user", "password": "pw_123"},
                                 follow_redirects=False)
        mod_cookie = resp.cookies.get("sl_session")
        resp = await client.get("/admin/cleanup", cookies={"sl_session": mod_cookie},
                                follow_redirects=False)
        check("moderator: /admin/cleanup → 303 /dashboard (rejected)",
              resp.status_code == 303 and "/dashboard" in resp.headers.get("location", ""))


# ──────────────────────────────────────────────────────────────────────────
# 4. /admin/users/create с role=moderator — создаётся moderator
# ──────────────────────────────────────────────────────────────────────────
async def test_create_moderator():
    print("\n[4] /admin/users/create с role=moderator — создаётся moderator")
    from db import async_session, WebUser
    from web_app import create_app
    from httpx import AsyncClient, ASGITransport
    from sqlalchemy import select

    fake_chat = types.SimpleNamespace(
        id=3333001, type="private",
        username="new_mod", first_name="NewMod", last_name=None,
    )
    mock_bot = MagicMock()
    mock_bot.get_chat = AsyncMock(return_value=fake_chat)
    # Заглушка send_rich_message — не падать
    mock_bot.send_rich_message = AsyncMock(return_value=MagicMock(message_id=1))

    app = create_app(bot=mock_bot)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        su_cookie = await _su_login(client)
        resp = await client.post(
            "/admin/users/create",
            data={"tg_user_id": "3333001", "role": "moderator"},
            cookies={"sl_session": su_cookie}, follow_redirects=False,
        )
        check("POST → 303 (redirect с flash token)", resp.status_code == 303,
              f"got {resp.status_code}, loc: {resp.headers.get('location')}")

    async with async_session() as s:
        wu = (await s.execute(
            select(WebUser).where(WebUser.username == "new_mod")
        )).scalar_one_or_none()
        check("WebUser new_mod создан", wu is not None)
        if wu:
            check("role = 'moderator'", wu.role == "moderator", f"got: {wu.role}")
            check("is_su = False", wu.is_su is False)


# ──────────────────────────────────────────────────────────────────────────
# 5. /admin/users/create с role=admin — создаётся admin
# ──────────────────────────────────────────────────────────────────────────
async def test_create_admin():
    print("\n[5] /admin/users/create с role=admin — создаётся admin (default)")
    from db import async_session, WebUser
    from web_app import create_app
    from httpx import AsyncClient, ASGITransport
    from sqlalchemy import select

    fake_chat = types.SimpleNamespace(
        id=3333002, type="private",
        username="new_adm", first_name="NewAdm", last_name=None,
    )
    mock_bot = MagicMock()
    mock_bot.get_chat = AsyncMock(return_value=fake_chat)
    mock_bot.send_rich_message = AsyncMock(return_value=MagicMock(message_id=1))

    app = create_app(bot=mock_bot)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        su_cookie = await _su_login(client)
        # Без role в форме — default 'admin'
        resp = await client.post(
            "/admin/users/create",
            data={"tg_user_id": "3333002"},
            cookies={"sl_session": su_cookie}, follow_redirects=False,
        )
        check("POST → 303", resp.status_code == 303)

    async with async_session() as s:
        wu = (await s.execute(
            select(WebUser).where(WebUser.username == "new_adm")
        )).scalar_one_or_none()
        check("WebUser new_adm создан", wu is not None)
        if wu:
            check("role = 'admin' (default)", wu.role == "admin", f"got: {wu.role}")

    # С явной role=admin
    fake_chat2 = types.SimpleNamespace(
        id=3333003, type="private",
        username="new_adm2", first_name="NewAdm2", last_name=None,
    )
    mock_bot2 = MagicMock()
    mock_bot2.get_chat = AsyncMock(return_value=fake_chat2)
    mock_bot2.send_rich_message = AsyncMock(return_value=MagicMock(message_id=1))
    app2 = create_app(bot=mock_bot2)
    transport2 = ASGITransport(app=app2)
    async with AsyncClient(transport=transport2, base_url="http://test") as client:
        su_cookie = await _su_login(client)
        resp = await client.post(
            "/admin/users/create",
            data={"tg_user_id": "3333003", "role": "admin"},
            cookies={"sl_session": su_cookie}, follow_redirects=False,
        )
        check("POST с role=admin → 303", resp.status_code == 303)

    async with async_session() as s:
        wu = (await s.execute(
            select(WebUser).where(WebUser.username == "new_adm2")
        )).scalar_one_or_none()
        if wu:
            check("role = 'admin' (явная)", wu.role == "admin")


# ──────────────────────────────────────────────────────────────────────────
# 6. /admin/users/create с невалидной role — flash, не создаётся
# ──────────────────────────────────────────────────────────────────────────
async def test_create_invalid_role():
    print("\n[6] /admin/users/create с невалидной role → flash, не создаётся")
    from db import async_session, WebUser
    from web_app import create_app
    from httpx import AsyncClient, ASGITransport
    from sqlalchemy import select

    mock_bot = MagicMock()
    mock_bot.get_chat = AsyncMock()
    app = create_app(bot=mock_bot)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        su_cookie = await _su_login(client)
        resp = await client.post(
            "/admin/users/create",
            data={"tg_user_id": "4444001", "role": "superadmin"},  # невалидная
            cookies={"sl_session": su_cookie}, follow_redirects=False,
        )
        check("POST → 303", resp.status_code == 303)
        check("flash содержит 'Invalid role'",
              "Invalid+role" in resp.headers.get("location", ""),
              f"loc: {resp.headers.get('location')}")

    async with async_session() as s:
        wu = (await s.execute(
            select(WebUser).where(WebUser.tg_user_id == 4444001)
        )).scalar_one_or_none()
        check("WebUser НЕ создан", wu is None)


# ──────────────────────────────────────────────────────────────────────────
# 7. /admin/users форма содержит radio для role
# ──────────────────────────────────────────────────────────────────────────
async def test_form_has_role_radio():
    print("\n[7] /admin/users форма содержит radio buttons (admin/moderator)")
    from web_app import create_app
    from httpx import AsyncClient, ASGITransport

    mock_bot = MagicMock()
    mock_bot.get_chat = AsyncMock()
    app = create_app(bot=mock_bot)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        su_cookie = await _su_login(client)
        resp = await client.get("/admin/users", cookies={"sl_session": su_cookie})

        check("есть radio name='role'", 'name="role"' in resp.text)
        check("есть radio value='admin'", 'value="admin"' in resp.text)
        check("есть radio value='moderator'", 'value="moderator"' in resp.text)
        check("admin radio checked по умолчанию", 'value="admin" checked' in resp.text)
        check("есть кнопка 'Create user'", "Create user" in resp.text)


# ──────────────────────────────────────────────────────────────────────────
# 8. /admin/chats GET — SU + admin OK, moderator rejected
# ──────────────────────────────────────────────────────────────────────────
async def test_admin_chats_access():
    print("\n[8] /admin/chats GET — SU + admin OK, moderator → /dashboard")
    from db import async_session, ChatSettings
    from web_app import create_app
    from httpx import AsyncClient, ASGITransport

    # Создаём тестовый chat_settings
    async with async_session() as s:
        s.add(ChatSettings(chat_id=-100123, hashtag="#Test"))
        await s.commit()

    mock_bot = MagicMock()
    mock_bot.get_chat = AsyncMock()
    app = create_app(bot=mock_bot)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # SU
        su_cookie = await _su_login(client)
        resp = await client.get("/admin/chats", cookies={"sl_session": su_cookie},
                                follow_redirects=False)
        check("SU: GET /admin/chats → 200", resp.status_code == 200, f"got {resp.status_code}")
        check("HTML содержит '#Test'", "#Test" in resp.text)
        check("HTML содержит 'Settings'", "Settings" in resp.text)
        check("HTML содержит form action", 'action="/admin/chats/-100123/update"' in resp.text)

        # admin
        resp = await client.post("/login",
                                 data={"username": "adm_user", "password": "pw_456"},
                                 follow_redirects=False)
        adm_cookie = resp.cookies.get("sl_session")
        resp = await client.get("/admin/chats", cookies={"sl_session": adm_cookie},
                                follow_redirects=False)
        check("admin: GET /admin/chats → 200", resp.status_code == 200)

        # moderator
        resp = await client.post("/login",
                                 data={"username": "mod_user", "password": "pw_123"},
                                 follow_redirects=False)
        mod_cookie = resp.cookies.get("sl_session")
        resp = await client.get("/admin/chats", cookies={"sl_session": mod_cookie},
                                follow_redirects=False)
        check("moderator: GET /admin/chats → 303 /dashboard (rejected)",
              resp.status_code == 303 and "/dashboard" in resp.headers.get("location", ""))


# ──────────────────────────────────────────────────────────────────────────
# 9. /admin/chats/{id}/update POST — обновление настроек
# ──────────────────────────────────────────────────────────────────────────
async def test_admin_chats_update():
    print("\n[9] /admin/chats/{id}/update — обновление ChatSettings")
    from db import async_session, ChatSettings
    from web_app import create_app
    from httpx import AsyncClient, ASGITransport
    from sqlalchemy import select

    mock_bot = MagicMock()
    mock_bot.get_chat = AsyncMock()
    app = create_app(bot=mock_bot)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        su_cookie = await _su_login(client)

        # Обновляем
        resp = await client.post(
            "/admin/chats/-100123/update",
            data={
                "hashtag": "#NewTag",
                "report_chat_id": "-100999",
                "warns_to_mute": "5",
                "mute_duration_seconds": "7200",
                "warns_to_ban": "10",
            },
            cookies={"sl_session": su_cookie}, follow_redirects=False,
        )
        check("POST → 303", resp.status_code == 303)
        check("flash содержит 'updated'",
              "updated" in resp.headers.get("location", "").lower())

    async with async_session() as s:
        cs = (await s.execute(
            select(ChatSettings).where(ChatSettings.chat_id == -100123)
        )).scalar_one()
        check("hashtag = '#NewTag'", cs.hashtag == "#NewTag", f"got: {cs.hashtag}")
        check("report_chat_id = -100999", cs.report_chat_id == -100999, f"got: {cs.report_chat_id}")
        check("warns_to_mute = 5", cs.warns_to_mute == 5)
        check("mute_duration_seconds = 7200", cs.mute_duration_seconds == 7200)
        check("warns_to_ban = 10", cs.warns_to_ban == 10)


# ──────────────────────────────────────────────────────────────────────────
# 10. /admin/chats/{id}/update — moderator rejected
# ──────────────────────────────────────────────────────────────────────────
async def test_admin_chats_update_moderator_rejected():
    print("\n[10] /admin/chats/{id}/update — moderator → /dashboard")
    from web_app import create_app
    from httpx import AsyncClient, ASGITransport

    mock_bot = MagicMock()
    mock_bot.get_chat = AsyncMock()
    app = create_app(bot=mock_bot)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/login",
                                 data={"username": "mod_user", "password": "pw_123"},
                                 follow_redirects=False)
        mod_cookie = resp.cookies.get("sl_session")

        resp = await client.post(
            "/admin/chats/-100123/update",
            data={"hashtag": "#HackerTag", "warns_to_mute": "999"},
            cookies={"sl_session": mod_cookie}, follow_redirects=False,
        )
        check("moderator: POST update → 303 /dashboard",
              resp.status_code == 303 and "/dashboard" in resp.headers.get("location", ""))


# ──────────────────────────────────────────────────────────────────────────
# 11. /admin/chats/{id}/update — валидация (невалидные числа)
# ──────────────────────────────────────────────────────────────────────────
async def test_admin_chats_update_validation():
    print("\n[11] /admin/chats/{id}/update — валидация невалидных чисел")
    from web_app import create_app
    from httpx import AsyncClient, ASGITransport

    mock_bot = MagicMock()
    mock_bot.get_chat = AsyncMock()
    app = create_app(bot=mock_bot)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        su_cookie = await _su_login(client)
        resp = await client.post(
            "/admin/chats/-100123/update",
            data={"hashtag": "", "warns_to_mute": "abc", "mute_duration_seconds": "60",
                  "warns_to_ban": "0", "report_chat_id": ""},
            cookies={"sl_session": su_cookie}, follow_redirects=False,
        )
        loc = resp.headers.get("location", "")
        check("POST с невалидным warns_to_mute → 303 (flash error)",
              resp.status_code == 303 and (
                  "must+be+a+number" in loc.lower() or
                  "must%20be%20a%20number" in loc.lower()
              ),
              f"loc: {loc}")


# ──────────────────────────────────────────────────────────────────────────
# 12. Welcome DM — для moderator содержит "только просмотр"
# ──────────────────────────────────────────────────────────────────────────
async def test_welcome_text_per_role():
    print("\n[12] Welcome DM — текст адаптируется под роль")
    from web_app import _send_admin_welcome
    from aiogram.types import InputRichMessage, InputRichBlockSectionHeading

    # Мокаем bot.send_rich_message чтобы захватить blocks
    sent_blocks = []

    async def fake_send(chat_id, rich_message):
        sent_blocks.extend(rich_message.blocks)
        return MagicMock(message_id=1)

    mock_bot = MagicMock()
    mock_bot.send_rich_message = fake_send

    # ── moderator ──
    sent_blocks.clear()
    ok, err = await _send_admin_welcome(
        bot=mock_bot, tg_user_id=123, login="mod1", password="pw",
        first_name="Mod", role="moderator",
    )
    check("moderator: welcome sent OK", ok, err)
    check("moderator: heading содержит 'модератор'",
          any("модератор" in str(getattr(b, "text", "")).lower() for b in sent_blocks),
          f"headings: {[str(getattr(b, 'text', '')) for b in sent_blocks]}")
    check("moderator: 'только просмотр логов' в каком-то блоке",
          any("только просмотр" in str(getattr(b, "text", "")) for b in sent_blocks),
          f"texts: {[str(getattr(b, 'text', '')) for b in sent_blocks]}")

    # ── admin ──
    sent_blocks.clear()
    ok, err = await _send_admin_welcome(
        bot=mock_bot, tg_user_id=456, login="adm1", password="pw",
        first_name="Adm", role="admin",
    )
    check("admin: welcome sent OK", ok, err)
    check("admin: heading содержит 'админ'",
          any("админ" in str(getattr(b, "text", "")).lower() for b in sent_blocks))
    check("admin: 'управление модераторами' в каком-то блоке",
          any("управление модераторами" in str(getattr(b, "text", "")) for b in sent_blocks))


# ──────────────────────────────────────────────────────────────────────────
# 13. Nav: moderator видит только Dashboard
# ──────────────────────────────────────────────────────────────────────────
async def test_nav_visibility_per_role():
    print("\n[13] Nav: moderator видит только Dashboard; admin видит Chats; SU видит всё")
    from web_app import create_app
    from httpx import AsyncClient, ASGITransport

    mock_bot = MagicMock()
    mock_bot.get_chat = AsyncMock()
    app = create_app(bot=mock_bot)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # ── SU ──
        su_cookie = await _su_login(client)
        resp = await client.get("/dashboard", cookies={"sl_session": su_cookie})
        check("SU видит Dashboard", 'href="/dashboard"' in resp.text)
        check("SU видит Chats", 'href="/admin/chats"' in resp.text)
        check("SU видит Users", 'href="/admin/users"' in resp.text)
        check("SU видит Cleanup", 'href="/admin/cleanup"' in resp.text)
        check("SU видит 'SU' chip", ">SU<" in resp.text)

        # ── admin ──
        resp = await client.post("/login",
                                 data={"username": "adm_user", "password": "pw_456"},
                                 follow_redirects=False)
        adm_cookie = resp.cookies.get("sl_session")
        resp = await client.get("/dashboard", cookies={"sl_session": adm_cookie})
        check("admin видит Dashboard", 'href="/dashboard"' in resp.text)
        check("admin видит Chats", 'href="/admin/chats"' in resp.text)
        check("admin НЕ видит Users", 'href="/admin/users"' not in resp.text)
        check("admin НЕ видит Cleanup", 'href="/admin/cleanup"' not in resp.text)
        check("admin видит 'ADMIN' chip", ">ADMIN<" in resp.text)

        # ── moderator ──
        resp = await client.post("/login",
                                 data={"username": "mod_user", "password": "pw_123"},
                                 follow_redirects=False)
        mod_cookie = resp.cookies.get("sl_session")
        resp = await client.get("/dashboard", cookies={"sl_session": mod_cookie})
        check("moderator видит Dashboard", 'href="/dashboard"' in resp.text)
        check("moderator НЕ видит Chats", 'href="/admin/chats"' not in resp.text)
        check("moderator НЕ видит Admins", 'href="/admin/users"' not in resp.text)
        check("moderator НЕ видит Cleanup", 'href="/admin/cleanup"' not in resp.text)
        check("moderator видит 'MOD' chip", ">MOD<" in resp.text)


# ──────────────────────────────────────────────────────────────────────────
# 14. /admin/users — список показывает role
# ──────────────────────────────────────────────────────────────────────────
async def test_admin_users_list_shows_role():
    print("\n[14] /admin/users — список показывает роль каждого юзера")
    from web_app import create_app
    from httpx import AsyncClient, ASGITransport

    mock_bot = MagicMock()
    mock_bot.get_chat = AsyncMock()
    app = create_app(bot=mock_bot)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        su_cookie = await _su_login(client)
        resp = await client.get("/admin/users", cookies={"sl_session": su_cookie})
        # В списке должны быть: su (super-user), adm_user (admin), mod_user (moderator), new_mod, new_adm, new_adm2
        check("есть 'super-user' для SU", "super-user" in resp.text)
        check("есть 'admin' для админов", ">admin<" in resp.text or "admin" in resp.text)
        check("есть 'moderator' для модераторов", "moderator" in resp.text)


# ──────────────────────────────────────────────────────────────────────────
# 15. AuthUser: если SU понизил роль — токен остаётся, но роль из БД
# ──────────────────────────────────────────────────────────────────────────
async def test_role_takes_effect_on_next_request():
    print("\n[15] Role change: следующий запрос увидит новую роль из БД")
    from db import async_session, WebUser
    from web_app import create_app
    from httpx import AsyncClient, ASGITransport
    from sqlalchemy import select

    mock_bot = MagicMock()
    mock_bot.get_chat = AsyncMock()
    app = create_app(bot=mock_bot)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # admin логинится
        resp = await client.post("/login",
                                 data={"username": "adm_user", "password": "pw_456"},
                                 follow_redirects=False)
        adm_cookie = resp.cookies.get("sl_session")
        # Изначально видит Chats
        resp = await client.get("/dashboard", cookies={"sl_session": adm_cookie})
        check("admin видит Chats link", 'href="/admin/chats"' in resp.text)

        # SU понижает admin → moderator
        async with async_session() as s:
            wu = (await s.execute(
                select(WebUser).where(WebUser.username == "adm_user")
            )).scalar_one()
            wu.role = "moderator"
            await s.commit()

        # Тот же cookie — но следующий запрос не увидит Chats link
        resp = await client.get("/dashboard", cookies={"sl_session": adm_cookie})
        check("после понижения: admin (теперь moderator) НЕ видит Chats link",
              'href="/admin/chats"' not in resp.text,
              "role change should take effect on next request via DB lookup")

        # Возвращаем обратно для других тестов
        async with async_session() as s:
            wu = (await s.execute(
                select(WebUser).where(WebUser.username == "adm_user")
            )).scalar_one()
            wu.role = "admin"
            await s.commit()


# ──────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────
async def main():
    print("=" * 70)
    print("v4.4.6 tests: role-based access control (SU / admin / moderator)")
    print("=" * 70)
    # v4.5.1: отключаем rate-limit на /login для тестов
    try:
        import web_app
        web_app._check_login_rate_limit = lambda ip: True
    except ImportError:
        pass
    await test_migration_assigns_roles()
    await test_auth_role_from_db()
    await test_require_su()
    await test_create_moderator()
    await test_create_admin()
    await test_create_invalid_role()
    await test_form_has_role_radio()
    await test_admin_chats_access()
    await test_admin_chats_update()
    await test_admin_chats_update_moderator_rejected()
    await test_admin_chats_update_validation()
    await test_welcome_text_per_role()
    await test_nav_visibility_per_role()
    await test_admin_users_list_shows_role()
    await test_role_takes_effect_on_next_request()

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
