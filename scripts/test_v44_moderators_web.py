"""
test_v44_moderators_web.py — Тесты v4.4.3: управление модераторами через веб-панель.

Покрывает:
  - GET /admin/moderators — страница с формой и списком (SU-only)
  - POST /admin/moderators/create — добавление модератора (SU-only)
  - POST /admin/moderators/{id}/delete — удаление модератора (SU-only)
  - Валидация: нечисловые/нулевые/отрицательные ID
  - Дубликаты (chat_id, user_id) отклоняются
  - Best-effort: bot.get_chat(user_id) для подтягивания профиля модератора
  - bot=None → запись создаётся, профиль не подтягивается (non-critical)
  - Non-SU → redirect на /dashboard
  - Неавторизованный → redirect на /login
  - parity с командой /addadmin: создаёт ту же запись в chat_admins
  - parity с командой /deladmin: удаляет ту же запись
  - HTML содержит ключевые элементы: nav link, form, fallback note
  - Удаление несуществующей записи → silent 303 (no crash)

Запуск:
    cd /home/z/my-project/v4.4
    python3 scripts/test_v44_moderators_web.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

# Подкладываем путь к проекту
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Изолированная БД
TMP_DB = "/tmp/test_v44_moderators_web.db"
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


# ──────────────────────────────────────────────────────────────────────────
# 1. GET /admin/moderators — базовая страница
# ──────────────────────────────────────────────────────────────────────────
async def test_get_page():
    print("\n[1] GET /admin/moderators — базовая страница (SU)")
    from db import init_db, async_session, ChatAdmin, ChatSettings, Moderator
    from web_app import create_app
    from httpx import AsyncClient, ASGITransport
    from sqlalchemy import select

    await init_db()

    # Подготавливаем данные: 1 чат с настройкой + 2 модератора (один с профилем, другой без)
    async with async_session() as s:
        s.add(ChatSettings(chat_id=-1001234567890, hashtag="#TestChat"))
        s.add(ChatAdmin(chat_id=-1001234567890, user_id=111222333, added_by=999999))
        s.add(ChatAdmin(chat_id=-1001234567890, user_id=444555666, added_by=None))  # via web
        s.add(ChatAdmin(chat_id=-1009876543210, user_id=111222333))  # другой чат
        # Профиль для первого модератора (как будто он уже использовал команду бота)
        s.add(Moderator(mod_id=111222333, username="alice_mod", first_name="Alice"))
        await s.commit()

    mock_bot = MagicMock()
    mock_bot.get_chat = AsyncMock()
    app = create_app(bot=mock_bot)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Логинимся как SU
        resp = await client.post("/login", data={"username": "su", "password": "test_su_password_123"},
                                  follow_redirects=False)
        check("SU login → 303", resp.status_code == 303)
        su_cookie = resp.cookies.get("sl_session")
        check("SU cookie set", su_cookie is not None)

        resp = await client.get("/admin/moderators", cookies={"sl_session": su_cookie})
        check("GET /admin/moderators → 200", resp.status_code == 200, f"got {resp.status_code}")

        # Ключевые элементы страницы
        check("есть 'Add moderator to chat'", "Add moderator to chat" in resp.text)
        check("есть 'Existing moderators'", "Existing moderators" in resp.text)
        check("есть форма создания", 'action="/admin/moderators/create"' in resp.text)
        check("есть input chat_id", 'name="chat_id"' in resp.text)
        check("есть input user_id", 'name="user_id"' in resp.text)
        check("есть упоминание /addadmin как fallback", "/addadmin" in resp.text)
        check("есть упоминание /deladmin как fallback", "/deladmin" in resp.text)

        # Отображение данных
        check("chat_id=-1001234567890 отображается", "-1001234567890" in resp.text)
        check("user_id=111222333 отображается", "111222333" in resp.text)
        check("user_id=444555666 отображается", "444555666" in resp.text)
        check("hashtag #TestChat отображается", "#TestChat" in resp.text)
        check("moderator Alice (first_name) отображается", "Alice" in resp.text)
        check("moderator @alice_mod отображается", "@alice_mod" in resp.text)
        check("'via bot' для added_by=999999", "via bot" in resp.text)
        check("'via web' для added_by=None", "via web" in resp.text)
        check("added_by 999999 отображается", "999999" in resp.text)

        # Known chats — кнопки быстрого заполнения
        check("есть блок 'Known chats'", "Known chats" in resp.text)


# ──────────────────────────────────────────────────────────────────────────
# 2. POST /admin/moderators/create — успешное создание
# ──────────────────────────────────────────────────────────────────────────
async def test_create_success():
    print("\n[2] POST /admin/moderators/create — успешное создание")
    from db import async_session, ChatAdmin, Moderator
    from web_app import create_app
    from httpx import AsyncClient, ASGITransport
    from sqlalchemy import select

    # Мокаем бота: get_chat возвращает профиль
    fake_chat = types.SimpleNamespace(
        id=777888999, type="private",
        username="new_mod_handle", first_name="Bob", last_name="Builder",
    )
    mock_bot = MagicMock()
    mock_bot.get_chat = AsyncMock(return_value=fake_chat)

    app = create_app(bot=mock_bot)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/login", data={"username": "su", "password": "test_su_password_123"},
                                  follow_redirects=False)
        su_cookie = resp.cookies.get("sl_session")

        resp = await client.post(
            "/admin/moderators/create",
            data={"chat_id": "-1001111222333", "user_id": "777888999"},
            cookies={"sl_session": su_cookie},
            follow_redirects=False,
        )
        check("create → 303", resp.status_code == 303, f"got {resp.status_code}")
        loc = resp.headers.get("location", "")
        check("redirect на /admin/moderators?flash=Added",
              "Added+moderator" in loc and "777888999" in loc,
              f"loc: {loc}")

        # Запись в БД
        async with async_session() as s:
            ca = (await s.execute(
                select(ChatAdmin).where(
                    ChatAdmin.chat_id == -1001111222333,
                    ChatAdmin.user_id == 777888999,
                )
            )).scalar_one_or_none()
        check("ChatAdmin saved", ca is not None)
        check("ChatAdmin.added_by is None (web-SU)", ca and ca.added_by is None)

        # Профиль модератора подтянут через bot.get_chat
        async with async_session() as s:
            mod = (await s.execute(
                select(Moderator).where(Moderator.mod_id == 777888999)
            )).scalar_one_or_none()
        check("Moderator profile saved (best-effort)", mod is not None)
        check("Moderator.username = 'new_mod_handle'", mod and mod.username == "new_mod_handle")
        check("Moderator.first_name = 'Bob'", mod and mod.first_name == "Bob")

        # bot.get_chat вызывался с правильным user_id
        mock_bot.get_chat.assert_called_with(chat_id=777888999)
        check("bot.get_chat called with user_id=777888999", True)


# ──────────────────────────────────────────────────────────────────────────
# 3. POST create — дубликат отклоняется
# ──────────────────────────────────────────────────────────────────────────
async def test_create_duplicate():
    print("\n[3] POST create — дубликат (chat_id, user_id) отклоняется")
    from db import async_session, ChatAdmin
    from web_app import create_app
    from httpx import AsyncClient, ASGITransport
    from sqlalchemy import select

    async with async_session() as s:
        s.add(ChatAdmin(chat_id=-1005556667777, user_id=1234567890))
        await s.commit()

    mock_bot = MagicMock()
    mock_bot.get_chat = AsyncMock()
    app = create_app(bot=mock_bot)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/login", data={"username": "su", "password": "test_su_password_123"},
                                  follow_redirects=False)
        su_cookie = resp.cookies.get("sl_session")

        resp = await client.post(
            "/admin/moderators/create",
            data={"chat_id": "-1005556667777", "user_id": "1234567890"},
            cookies={"sl_session": su_cookie},
            follow_redirects=False,
        )
        loc = resp.headers.get("location", "")
        check("duplicate → flash 'already a moderator'",
              "already+a+moderator" in loc, f"loc: {loc}")

        # bot.get_chat не должен был вызываться (валификация до него)
        mock_bot.get_chat.assert_not_called()
        check("bot.get_chat NOT called for duplicate", True)


# ──────────────────────────────────────────────────────────────────────────
# 4. POST create — валидация входных данных
# ──────────────────────────────────────────────────────────────────────────
async def test_create_validation():
    print("\n[4] POST create — валидация")
    from web_app import create_app
    from httpx import AsyncClient, ASGITransport

    mock_bot = MagicMock()
    mock_bot.get_chat = AsyncMock()
    app = create_app(bot=mock_bot)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/login", data={"username": "su", "password": "test_su_password_123"},
                                  follow_redirects=False)
        su_cookie = resp.cookies.get("sl_session")

        # 4a. Нечисловой chat_id
        resp = await client.post(
            "/admin/moderators/create",
            data={"chat_id": "abc", "user_id": "123"},
            cookies={"sl_session": su_cookie}, follow_redirects=False,
        )
        loc = resp.headers.get("location", "")
        check("non-numeric chat_id → flash 'must be numbers'",
              "must+be+numbers" in loc, f"loc: {loc}")

        # 4b. Нечисловой user_id
        resp = await client.post(
            "/admin/moderators/create",
            data={"chat_id": "-100123", "user_id": "xyz"},
            cookies={"sl_session": su_cookie}, follow_redirects=False,
        )
        loc = resp.headers.get("location", "")
        check("non-numeric user_id → flash 'must be numbers'",
              "must+be+numbers" in loc, f"loc: {loc}")

        # 4c. chat_id = 0 (default chat — не валидный для модераторов)
        resp = await client.post(
            "/admin/moderators/create",
            data={"chat_id": "0", "user_id": "123"},
            cookies={"sl_session": su_cookie}, follow_redirects=False,
        )
        loc = resp.headers.get("location", "")
        check("chat_id=0 → flash 'cannot be 0'",
              "cannot+be+0" in loc, f"loc: {loc}")

        # 4d. user_id отрицательный
        resp = await client.post(
            "/admin/moderators/create",
            data={"chat_id": "-100123", "user_id": "-5"},
            cookies={"sl_session": su_cookie}, follow_redirects=False,
        )
        loc = resp.headers.get("location", "")
        check("negative user_id → flash 'must be positive'",
              "must+be+positive" in loc, f"loc: {loc}")

        # 4e. user_id = 0
        resp = await client.post(
            "/admin/moderators/create",
            data={"chat_id": "-100123", "user_id": "0"},
            cookies={"sl_session": su_cookie}, follow_redirects=False,
        )
        loc = resp.headers.get("location", "")
        check("user_id=0 → flash 'must be positive'",
              "must+be+positive" in loc, f"loc: {loc}")

        # 4f. Пустые значения
        resp = await client.post(
            "/admin/moderators/create",
            data={"chat_id": "", "user_id": ""},
            cookies={"sl_session": su_cookie}, follow_redirects=False,
        )
        loc = resp.headers.get("location", "")
        check("empty values → flash 'must be numbers'",
              "must+be+numbers" in loc, f"loc: {loc}")

        # Ни один из невалидных запросов не должен был вызвать bot.get_chat
        mock_bot.get_chat.assert_not_called()
        check("bot.get_chat NOT called during validation tests", True)


# ──────────────────────────────────────────────────────────────────────────
# 5. POST create — bot=None: запись создаётся, профиль не подтягивается
# ──────────────────────────────────────────────────────────────────────────
async def test_create_no_bot():
    print("\n[5] POST create — bot=None (graceful)")
    from db import async_session, ChatAdmin, Moderator
    from web_app import create_app
    from httpx import AsyncClient, ASGITransport
    from sqlalchemy import select

    app = create_app(bot=None)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/login", data={"username": "su", "password": "test_su_password_123"},
                                  follow_redirects=False)
        su_cookie = resp.cookies.get("sl_session")

        resp = await client.post(
            "/admin/moderators/create",
            data={"chat_id": "-1009998887777", "user_id": "555444333"},
            cookies={"sl_session": su_cookie}, follow_redirects=False,
        )
        check("create with bot=None → 303", resp.status_code == 303)
        loc = resp.headers.get("location", "")
        check("flash 'Added moderator'", "Added+moderator" in loc, f"loc: {loc}")

        # Запись в chat_admins создана
        async with async_session() as s:
            ca = (await s.execute(
                select(ChatAdmin).where(
                    ChatAdmin.chat_id == -1009998887777,
                    ChatAdmin.user_id == 555444333,
                )
            )).scalar_one_or_none()
            mod = (await s.execute(
                select(Moderator).where(Moderator.mod_id == 555444333)
            )).scalar_one_or_none()
        check("ChatAdmin saved even with bot=None", ca is not None)
        check("Moderator profile NOT created (bot=None)", mod is None)


# ──────────────────────────────────────────────────────────────────────────
# 6. POST create — bot.get_chat падает: запись всё равно создаётся
# ──────────────────────────────────────────────────────────────────────────
async def test_create_get_chat_fails():
    print("\n[6] POST create — bot.get_chat падает (non-critical)")
    from db import async_session, ChatAdmin, Moderator
    from web_app import create_app
    from httpx import AsyncClient, ASGITransport
    from sqlalchemy import select

    mock_bot = MagicMock()
    mock_bot.get_chat = AsyncMock(side_effect=Exception("Bad Request: chat not found"))

    app = create_app(bot=mock_bot)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/login", data={"username": "su", "password": "test_su_password_123"},
                                  follow_redirects=False)
        su_cookie = resp.cookies.get("sl_session")

        resp = await client.post(
            "/admin/moderators/create",
            data={"chat_id": "-1001111222333", "user_id": "888777666"},
            cookies={"sl_session": su_cookie}, follow_redirects=False,
        )
        check("create despite get_chat fail → 303", resp.status_code == 303)
        loc = resp.headers.get("location", "")
        check("flash 'Added moderator' (success)", "Added+moderator" in loc, f"loc: {loc}")

        # Запись в chat_admins создана
        async with async_session() as s:
            ca = (await s.execute(
                select(ChatAdmin).where(
                    ChatAdmin.chat_id == -1001111222333,
                    ChatAdmin.user_id == 888777666,
                )
            )).scalar_one_or_none()
            mod = (await s.execute(
                select(Moderator).where(Moderator.mod_id == 888777666)
            )).scalar_one_or_none()
        check("ChatAdmin saved even though get_chat failed", ca is not None)
        check("Moderator profile NOT created (get_chat failed)", mod is None)


# ──────────────────────────────────────────────────────────────────────────
# 7. POST /admin/moderators/{id}/delete — успешное удаление
# ──────────────────────────────────────────────────────────────────────────
async def test_delete_success():
    print("\n[7] POST /admin/moderators/{id}/delete — успешное удаление")
    from db import async_session, ChatAdmin, Moderator
    from web_app import create_app
    from httpx import AsyncClient, ASGITransport
    from sqlalchemy import select

    # Создаём модератора с профилем
    async with async_session() as s:
        ca = ChatAdmin(chat_id=-1007776665555, user_id=222333444)
        s.add(ca)
        s.add(Moderator(mod_id=222333444, username="deleteme_mod", first_name="Delete"))
        await s.commit()
        ca_id = ca.id

    mock_bot = MagicMock()
    mock_bot.get_chat = AsyncMock()
    app = create_app(bot=mock_bot)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/login", data={"username": "su", "password": "test_su_password_123"},
                                  follow_redirects=False)
        su_cookie = resp.cookies.get("sl_session")

        resp = await client.post(
            f"/admin/moderators/{ca_id}/delete",
            cookies={"sl_session": su_cookie}, follow_redirects=False,
        )
        check("delete → 303", resp.status_code == 303)
        loc = resp.headers.get("location", "")
        check("redirect на /admin/moderators", loc == "/admin/moderators", f"loc: {loc}")

        # Запись в chat_admins удалена
        async with async_session() as s:
            ca_after = (await s.execute(
                select(ChatAdmin).where(ChatAdmin.id == ca_id)
            )).scalar_one_or_none()
            mod_after = (await s.execute(
                select(Moderator).where(Moderator.mod_id == 222333444)
            )).scalar_one_or_none()
        check("ChatAdmin removed", ca_after is None)
        # Профиль модератора ОСТАЁТСЯ — история санкций не должна потеряться
        check("Moderator profile preserved (history)", mod_after is not None)


# ──────────────────────────────────────────────────────────────────────────
# 8. POST delete — несуществующий ID (no crash)
# ──────────────────────────────────────────────────────────────────────────
async def test_delete_nonexistent():
    print("\n[8] POST /admin/moderators/{id}/delete — несуществующий ID")
    from web_app import create_app
    from httpx import AsyncClient, ASGITransport

    mock_bot = MagicMock()
    mock_bot.get_chat = AsyncMock()
    app = create_app(bot=mock_bot)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/login", data={"username": "su", "password": "test_su_password_123"},
                                  follow_redirects=False)
        su_cookie = resp.cookies.get("sl_session")

        resp = await client.post(
            "/admin/moderators/999999/delete",
            cookies={"sl_session": su_cookie}, follow_redirects=False,
        )
        check("delete nonexistent → 303 (silent)", resp.status_code == 303, f"got {resp.status_code}")


# ──────────────────────────────────────────────────────────────────────────
# 9. v4.4.6: role-based access — admin OK, moderator rejected
# ──────────────────────────────────────────────────────────────────────────
async def test_non_su_access():
    print("\n[9] Role-based: admin имеет доступ, moderator НЕ имеет")
    from db import async_session, WebUser, _hash_password
    from web_app import create_app
    from httpx import AsyncClient, ASGITransport

    # Создаём двух пользователей: admin и moderator
    async with async_session() as s:
        s.add(WebUser(
            username="regular_admin",
            password_hash=_hash_password("admin_pw_123"),
            tg_user_id=12345001,
            tg_username="regular_admin",
            role="admin",
        ))
        s.add(WebUser(
            username="just_moderator",
            password_hash=_hash_password("mod_pw_123"),
            tg_user_id=12345002,
            tg_username="just_moderator",
            role="moderator",
        ))
        await s.commit()

    mock_bot = MagicMock()
    mock_bot.get_chat = AsyncMock()
    app = create_app(bot=mock_bot)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # ── admin: имеет доступ (require_admin) ──────────────────────
        resp = await client.post("/login",
                                 data={"username": "regular_admin", "password": "admin_pw_123"},
                                 follow_redirects=False)
        admin_cookie = resp.cookies.get("sl_session")
        check("admin login → 303", resp.status_code == 303)

        resp = await client.get("/admin/moderators",
                                cookies={"sl_session": admin_cookie},
                                follow_redirects=False)
        check("admin: GET /admin/moderators → 200 (allowed)",
              resp.status_code == 200,
              f"got {resp.status_code}, loc: {resp.headers.get('location')}")

        resp = await client.post(
            "/admin/moderators/create",
            data={"chat_id": "-100999999", "user_id": "888777"},
            cookies={"sl_session": admin_cookie}, follow_redirects=False,
        )
        check("admin: POST /admin/moderators/create → 303 (allowed)",
              resp.status_code == 303,
              f"got {resp.status_code}, loc: {resp.headers.get('location')}")

        # ── moderator: НЕ имеет доступ (require_admin rejects) ───────
        resp = await client.post("/login",
                                 data={"username": "just_moderator", "password": "mod_pw_123"},
                                 follow_redirects=False)
        mod_cookie = resp.cookies.get("sl_session")
        check("moderator login → 303", resp.status_code == 303)

        resp = await client.get("/admin/moderators",
                                cookies={"sl_session": mod_cookie},
                                follow_redirects=False)
        check("moderator: GET /admin/moderators → 303 redirect /dashboard (rejected)",
              resp.status_code == 303 and "/dashboard" in resp.headers.get("location", ""),
              f"got {resp.status_code}, loc: {resp.headers.get('location')}")

        resp = await client.post(
            "/admin/moderators/create",
            data={"chat_id": "-100888", "user_id": "999"},
            cookies={"sl_session": mod_cookie}, follow_redirects=False,
        )
        check("moderator: POST /admin/moderators/create → 303 /dashboard (rejected)",
              resp.status_code == 303 and "/dashboard" in resp.headers.get("location", ""))

        resp = await client.post(
            "/admin/moderators/1/delete",
            cookies={"sl_session": mod_cookie}, follow_redirects=False,
        )
        check("moderator: POST /admin/moderators/{id}/delete → 303 /dashboard (rejected)",
              resp.status_code == 303 and "/dashboard" in resp.headers.get("location", ""))


# ──────────────────────────────────────────────────────────────────────────
# 10. Неавторизованный → redirect на /login
# ──────────────────────────────────────────────────────────────────────────
async def test_unauthenticated_access():
    print("\n[10] Неавторизованный → redirect на /login")
    from web_app import create_app
    from httpx import AsyncClient, ASGITransport

    app = create_app(bot=None)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/admin/moderators", follow_redirects=False)
        check("GET without auth → 303 /login",
              resp.status_code == 303 and "/login" in resp.headers.get("location", ""),
              f"got {resp.status_code}, loc: {resp.headers.get('location')}")

        resp = await client.post("/admin/moderators/create",
                                  data={"chat_id": "1", "user_id": "2"},
                                  follow_redirects=False)
        check("POST create without auth → 303 /login",
              resp.status_code == 303 and "/login" in resp.headers.get("location", ""))


# ──────────────────────────────────────────────────────────────────────────
# 11. Parity: web create и /addadmin пишут в одну и ту же таблицу
# ──────────────────────────────────────────────────────────────────────────
async def test_parity_with_bot_command():
    print("\n[11] Parity: web create и /addadmin — одна и та же таблица")
    from db import async_session, ChatAdmin
    from web_app import create_app
    from httpx import AsyncClient, ASGITransport
    from sqlalchemy import select, func

    mock_bot = MagicMock()
    mock_bot.get_chat = AsyncMock()
    app = create_app(bot=mock_bot)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/login", data={"username": "su", "password": "test_su_password_123"},
                                  follow_redirects=False)
        su_cookie = resp.cookies.get("sl_session")

        # Создаём через web
        await client.post(
            "/admin/moderators/create",
            data={"chat_id": "-1001112223334", "user_id": "9876543210"},
            cookies={"sl_session": su_cookie}, follow_redirects=False,
        )

    # Симулируем добавление через /addadmin (как делает bot_handlers.cmd_addadmin):
    # bot-команда пишет напрямую в ту же таблицу chat_admins.
    async with async_session() as s:
        s.add(ChatAdmin(chat_id=-1001112223334, user_id=9876543210, added_by=999))
        await s.commit()  # DB не имеет UNIQUE constraint — commit проходит

    # В БД теперь 2 записи (web + /addadmin). На уровне БД уникальности нет —
    # это намеренно: проверка дубликата делается на уровне приложения
    # (и в web_app.py, и в bot_handlers.cmd_addadmin).
    async with async_session() as s:
        rows = (await s.execute(
            select(ChatAdmin).where(
                ChatAdmin.chat_id == -1001112223334,
                ChatAdmin.user_id == 9876543210,
            )
        )).scalars().all()
    check("both web + /addadmin write to same chat_admins table",
          len(rows) == 2, f"got {len(rows)} rows")
    check("web row has added_by=None", any(r.added_by is None for r in rows))
    check("/addadmin row has added_by=999", any(r.added_by == 999 for r in rows))

    # При повторном web-create — приложение должно отвергнуть дубликат
    # (проверка existing в admin_moderators_create). Должна остаться 1 web-запись
    # (та, что уже есть) + 1 bot-запись.
    async with AsyncClient(transport=ASGITransport(app=create_app(bot=mock_bot)),
                            base_url="http://test") as client:
        resp = await client.post("/login", data={"username": "su", "password": "test_su_password_123"},
                                  follow_redirects=False)
        su_cookie = resp.cookies.get("sl_session")
        resp = await client.post(
            "/admin/moderators/create",
            data={"chat_id": "-1001112223334", "user_id": "9876543210"},
            cookies={"sl_session": su_cookie}, follow_redirects=False,
        )
        loc = resp.headers.get("location", "")
        check("web rejects duplicate (chat, user) even if /addadmin wrote it",
              "already+a+moderator" in loc, f"loc: {loc}")

    async with async_session() as s:
        cnt = (await s.execute(
            select(func.count(ChatAdmin.id)).where(
                ChatAdmin.chat_id == -1001112223334,
                ChatAdmin.user_id == 9876543210,
            )
        )).scalar()
    check("still 2 rows (web check prevented 3rd)", cnt == 2, f"got cnt={cnt}")


# ──────────────────────────────────────────────────────────────────────────
# 12. Best-effort: профиль модератора обновляется, если уже существует
# ──────────────────────────────────────────────────────────────────────────
async def test_create_updates_existing_moderator_profile():
    print("\n[12] POST create — обновление существующего профиля модератора")
    from db import async_session, ChatAdmin, Moderator
    from web_app import create_app
    from httpx import AsyncClient, ASGITransport
    from sqlalchemy import select

    # Модератор уже в БД с устаревшим именем
    async with async_session() as s:
        s.add(Moderator(mod_id=333222111, username="old_handle", first_name="OldName"))
        await s.commit()

    fake_chat = types.SimpleNamespace(
        id=333222111, type="private",
        username="new_handle", first_name="NewName", last_name=None,
    )
    mock_bot = MagicMock()
    mock_bot.get_chat = AsyncMock(return_value=fake_chat)

    app = create_app(bot=mock_bot)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/login", data={"username": "su", "password": "test_su_password_123"},
                                  follow_redirects=False)
        su_cookie = resp.cookies.get("sl_session")

        await client.post(
            "/admin/moderators/create",
            data={"chat_id": "-1004445556666", "user_id": "333222111"},
            cookies={"sl_session": su_cookie}, follow_redirects=False,
        )

    async with async_session() as s:
        mod = (await s.execute(
            select(Moderator).where(Moderator.mod_id == 333222111)
        )).scalar_one()
    check("Moderator.username updated to 'new_handle'", mod.username == "new_handle", f"got: {mod.username}")
    check("Moderator.first_name updated to 'NewName'", mod.first_name == "NewName", f"got: {mod.first_name}")


# ──────────────────────────────────────────────────────────────────────────
# 13. HTML: nav link "Moderators" присутствует в base.html для SU
# ──────────────────────────────────────────────────────────────────────────
async def test_nav_link_present():
    print("\n[13] HTML: nav link 'Moderators' в base.html для SU")
    from web_app import create_app
    from httpx import AsyncClient, ASGITransport

    mock_bot = MagicMock()
    mock_bot.get_chat = AsyncMock()
    app = create_app(bot=mock_bot)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Логинимся как SU
        resp = await client.post("/login", data={"username": "su", "password": "test_su_password_123"},
                                  follow_redirects=False)
        su_cookie = resp.cookies.get("sl_session")

        resp = await client.get("/dashboard", cookies={"sl_session": su_cookie})
        check("SU видит 'Moderators' в навбаре", 'href="/admin/moderators"' in resp.text)

        # ── admin: тоже видит Moderators (require_admin) ────────────
        from db import async_session, WebUser, _hash_password
        async with async_session() as s:
            s.add(WebUser(
                username="nav_test_admin",
                password_hash=_hash_password("pw_123"),
                tg_user_id=12345004,
                tg_username="nav_test_admin",
                role="admin",
            ))
            await s.commit()
        resp = await client.post("/login", data={"username": "nav_test_admin", "password": "pw_123"},
                                  follow_redirects=False)
        reg_cookie = resp.cookies.get("sl_session")
        resp = await client.get("/dashboard", cookies={"sl_session": reg_cookie})
        check("admin видит 'Moderators' в навбаре",
              'href="/admin/moderators"' in resp.text,
              "admin should see Moderators link")

        # ── moderator: НЕ видит Moderators (только Dashboard) ───────
        async with async_session() as s:
            s.add(WebUser(
                username="nav_test_moderator",
                password_hash=_hash_password("pw_456"),
                tg_user_id=12345003,
                tg_username="nav_test_moderator",
                role="moderator",
            ))
            await s.commit()
        resp = await client.post("/login", data={"username": "nav_test_moderator", "password": "pw_456"},
                                  follow_redirects=False)
        mod_cookie = resp.cookies.get("sl_session")
        resp = await client.get("/dashboard", cookies={"sl_session": mod_cookie})
        check("moderator НЕ видит 'Moderators' в навбаре",
              'href="/admin/moderators"' not in resp.text,
              "moderator should not see Moderators link")
        check("moderator НЕ видит 'Admins' в навбаре",
              'href="/admin/users"' not in resp.text)
        check("moderator НЕ видит 'Cleanup' в навбаре",
              'href="/admin/cleanup"' not in resp.text)


# ──────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────
async def main():
    print("=" * 70)
    print("v4.4.3 tests: moderator web management")
    print("=" * 70)
    await test_get_page()
    await test_create_success()
    await test_create_duplicate()
    await test_create_validation()
    await test_create_no_bot()
    await test_create_get_chat_fails()
    await test_delete_success()
    await test_delete_nonexistent()
    await test_non_su_access()
    await test_unauthenticated_access()
    await test_parity_with_bot_command()
    await test_create_updates_existing_moderator_profile()
    await test_nav_link_present()

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
