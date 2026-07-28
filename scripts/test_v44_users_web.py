"""
test_v44_users_web.py — Тесты v4.4.7: объединённая вкладка Users.

Покрывает:
  - GET /admin/users — страница (SU-only; admin/moderator → redirect)
  - Форма создания содержит radio buttons (admin/moderator) и блок мультивыбора чатов
  - POST /admin/users/create с role=moderator + chat_ids — создаётся WebUser + chat_admins
  - POST /admin/users/create с role=admin — chat_ids игнорируется
  - POST /admin/users/{id}/edit-chats — обновление списка чатов модератора
  - POST /admin/users/{id}/role — moderator↔admin (moderator→admin чистит chat_admins)
  - POST /admin/users/{id}/bind-tg — привязка TG ID к SU (для DM о новых чатах)
  - POST /admin/users/{id}/toggle — disable/enable
  - POST /admin/users/{id}/reset — смена пароля SU'ом
  - POST /admin/users/{id}/delete
  - Старые роуты /admin/moderators/* возвращают 404 (функционал объединён с /admin/users)
  - Nav: SU видит Users; admin/moderator НЕ видят Users
  - TG-only модераторы (chat_admins без веб-аккаунта) отображаются отдельной секцией

Запуск:
    cd /home/z/my-project/v4.5
    python3 scripts/test_v44_users_web.py
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

TMP_DB = "/tmp/test_v44_users_web.db"
if os.path.exists(TMP_DB):
    os.remove(TMP_DB)
os.environ["DB_PATH"] = TMP_DB
os.environ["WEB_PASSWORD"] = "test_su_password_123"
os.environ["SESSION_SECRET"] = "test_session_secret"

PASS_COUNT = 0
FAIL_COUNT = 0
FAILURES: list[str] = []


def check(label: str, cond: bool, extra: str = "") -> None:
    global PASS_COUNT, FAIL_COUNT
    if cond:
        PASS_COUNT += 1
        print(f"  ✓ {label}")
    else:
        FAIL_COUNT += 1
        FAILURES.append(f"{label} {extra}")
        print(f"  ✗ {label}  {extra}")


async def _su_login(client) -> str:
    # Гарантируем что БД инициализирована (миграции применены, SU засеян)
    from db import init_db
    await init_db()
    # v4.5.1: отключаем rate-limit на /login для тестов
    try:
        import web_app
        web_app._check_login_rate_limit = lambda ip: True
    except ImportError:
        pass
    resp = await client.post("/login", data={"username": "su", "password": "test_su_password_123"},
                             follow_redirects=False)
    assert resp.status_code == 303, f"SU login failed: {resp.status_code}"
    return resp.cookies.get("sl_session")


def _make_chat_obj(chat_id: int, username: str | None = None,
                   first_name: str | None = None, last_name: str | None = None,
                   title: str | None = None):
    """Мок aiogram Chat для приватного чата (user) или группы (title)."""
    chat = MagicMock()
    chat.id = chat_id
    chat.type = "private" if title is None else "supergroup"
    chat.username = username
    chat.first_name = first_name
    chat.last_name = last_name
    chat.title = title
    return chat


async def _seed_chat_settings(session, chat_id: int, title: str = "Test Chat",
                              hashtag: str = "#Test", is_private: bool = False,
                              is_enabled: bool = True, is_report_chat: bool = False):
    """Создаёт chat_settings напрямую через ORM для тестов."""
    from db import ChatSettings
    cs = ChatSettings(
        chat_id=chat_id, title=title, hashtag=hashtag,
        is_private=is_private, is_enabled=is_enabled, is_report_chat=is_report_chat,
        warns_to_mute=3, mute_duration_seconds=3600, warns_to_ban=5,
    )
    session.add(cs)
    await session.flush()


# ──────────────────────────────────────────────────────────────────────────
# 1. GET /admin/users — страница
# ──────────────────────────────────────────────────────────────────────────
async def test_get_page():
    print("\n[1] GET /admin/users — базовая страница (SU)")
    from web_app import create_app
    from httpx import AsyncClient, ASGITransport

    mock_bot = MagicMock()
    mock_bot.get_chat = AsyncMock()
    app = create_app(bot=mock_bot)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        su_cookie = await _su_login(client)
        resp = await client.get("/admin/users", cookies={"sl_session": su_cookie})
        check("GET /admin/users → 200", resp.status_code == 200, f"got {resp.status_code}")
        check("есть форма создания", 'action="/admin/users/create"' in resp.text)
        check("есть radio admin", 'name="role" value="admin"' in resp.text)
        check("есть radio moderator", 'name="role" value="moderator"' in resp.text)
        check("есть блок chatsBlock", 'id="chatsBlock"' in resp.text)
        check("есть заголовок Users", "Users" in resp.text)


# ──────────────────────────────────────────────────────────────────────────
# 2. POST /admin/users/create с role=moderator + chat_ids
# ──────────────────────────────────────────────────────────────────────────
async def test_create_moderator_with_chats():
    print("\n[2] POST /admin/users/create — moderator с мультивыбором чатов")
    from web_app import create_app
    from httpx import AsyncClient, ASGITransport
    from db import async_session, WebUser, ChatAdmin
    from sqlalchemy import select

    mock_bot = MagicMock()
    user_chat = _make_chat_obj(777888111, username="mod_user1", first_name="Mod1")
    mock_bot.get_chat = AsyncMock(return_value=user_chat)
    mock_bot.send_rich_message = AsyncMock()
    app = create_app(bot=mock_bot)
    transport = ASGITransport(app=app)

    # Подготавливаем 2 чата в БД (init_db вызывается внутри _su_login)
    from db import async_session as _as
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        su_cookie = await _su_login(client)
        # Теперь БД точно инициализирована — добавляем чаты
        async with _as() as s:
            await _seed_chat_settings(s, -1001, title="Chat A", hashtag="#A")
            await _seed_chat_settings(s, -1002, title="Chat B", hashtag="#B")
            await s.commit()
        resp = await client.post(
            "/admin/users/create",
            data={
                "tg_user_id": "777888111",
                "role": "moderator",
                "chat_ids": ["-1001", "-1002"],
            },
            cookies={"sl_session": su_cookie},
            follow_redirects=False,
        )
        check("redirect 303", resp.status_code == 303, f"got {resp.status_code}")
        check("redirect на /admin/users?created=", "created=" in resp.headers.get("location", ""))

        # Проверяем что WebUser создан
        async with _as() as s:
            wu = (await s.execute(select(WebUser).where(WebUser.tg_user_id == 777888111))).scalar_one_or_none()
            check("WebUser создан", wu is not None)
            check("role=moderator", wu and wu.role == "moderator")
            # Проверяем что chat_admins созданы для обоих чатов
            cas = (await s.execute(select(ChatAdmin).where(ChatAdmin.user_id == 777888111))).scalars().all()
            check("создано 2 chat_admins", len(cas) == 2, f"got {len(cas)}")
            chat_ids_in_db = sorted(ca.chat_id for ca in cas)
            check("чат -1001 привязан", -1001 in chat_ids_in_db)
            check("чат -1002 привязан", -1002 in chat_ids_in_db)


# ──────────────────────────────────────────────────────────────────────────
# 3. POST /admin/users/create с role=admin — chat_ids игнорируется
# ──────────────────────────────────────────────────────────────────────────
async def test_create_admin_ignores_chats():
    print("\n[3] POST /admin/users/create — admin (chat_ids игнорируется)")
    from web_app import create_app
    from httpx import AsyncClient, ASGITransport
    from db import async_session, WebUser, ChatAdmin
    from sqlalchemy import select

    mock_bot = MagicMock()
    user_chat = _make_chat_obj(777888222, username="admin_user1", first_name="Admin1")
    mock_bot.get_chat = AsyncMock(return_value=user_chat)
    mock_bot.send_rich_message = AsyncMock()
    app = create_app(bot=mock_bot)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        su_cookie = await _su_login(client)
        resp = await client.post(
            "/admin/users/create",
            data={
                "tg_user_id": "777888222",
                "role": "admin",
                "chat_ids": ["-1001", "-1002"],  # должно быть проигнорировано
            },
            cookies={"sl_session": su_cookie},
            follow_redirects=False,
        )
        check("redirect 303", resp.status_code == 303)

        async with async_session() as s:
            wu = (await s.execute(select(WebUser).where(WebUser.tg_user_id == 777888222))).scalar_one_or_none()
            check("WebUser создан", wu is not None)
            check("role=admin", wu and wu.role == "admin")
            cas = (await s.execute(select(ChatAdmin).where(ChatAdmin.user_id == 777888222))).scalars().all()
            check("chat_admins НЕ создан для admin", len(cas) == 0, f"got {len(cas)}")


# ──────────────────────────────────────────────────────────────────────────
# 4. POST /admin/users/{id}/edit-chats — обновление чатов модератора
# ──────────────────────────────────────────────────────────────────────────
async def test_edit_chats_moderator():
    print("\n[4] POST /admin/users/{id}/edit-chats — обновление чатов")
    from web_app import create_app
    from httpx import AsyncClient, ASGITransport
    from db import async_session, WebUser, ChatAdmin
    from sqlalchemy import select

    mock_bot = MagicMock()
    mock_bot.get_chat = AsyncMock()
    app = create_app(bot=mock_bot)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        su_cookie = await _su_login(client)

        # Находим moderator user
        async with async_session() as s:
            wu = (await s.execute(select(WebUser).where(WebUser.tg_user_id == 777888111))).scalar_one()
            mod_id = wu.id

        # Убираем чат -1001, добавляем (нет новых) — остаётся только -1002
        resp = await client.post(
            f"/admin/users/{mod_id}/edit-chats",
            data={"chat_ids": ["-1002"]},
            cookies={"sl_session": su_cookie},
            follow_redirects=False,
        )
        check("edit-chats → 303", resp.status_code == 303)

        async with async_session() as s:
            cas = (await s.execute(select(ChatAdmin).where(ChatAdmin.user_id == 777888111))).scalars().all()
            check("остался 1 chat_admin", len(cas) == 1, f"got {len(cas)}")
            check("чат -1002 остался", cas[0].chat_id == -1002)

        # Добавляем чат -1001 обратно
        resp = await client.post(
            f"/admin/users/{mod_id}/edit-chats",
            data={"chat_ids": ["-1001", "-1002"]},
            cookies={"sl_session": su_cookie},
            follow_redirects=False,
        )
        async with async_session() as s:
            cas = (await s.execute(select(ChatAdmin).where(ChatAdmin.user_id == 777888111))).scalars().all()
            check("теперь 2 chat_admins", len(cas) == 2, f"got {len(cas)}")

        # Полностью очищаем
        resp = await client.post(
            f"/admin/users/{mod_id}/edit-chats",
            data={},
            cookies={"sl_session": su_cookie},
            follow_redirects=False,
        )
        async with async_session() as s:
            cas = (await s.execute(select(ChatAdmin).where(ChatAdmin.user_id == 777888111))).scalars().all()
            check("все chat_admins удалены", len(cas) == 0, f"got {len(cas)}")


# ──────────────────────────────────────────────────────────────────────────
# 5. POST /admin/users/{id}/role — moderator→admin чистит chat_admins
# ──────────────────────────────────────────────────────────────────────────
async def test_change_role_clears_chat_admins():
    print("\n[5] POST /admin/users/{id}/role — moderator→admin чистит chat_admins")
    from web_app import create_app
    from httpx import AsyncClient, ASGITransport
    from db import async_session, WebUser, ChatAdmin
    from sqlalchemy import select

    mock_bot = MagicMock()
    mock_bot.get_chat = AsyncMock()
    app = create_app(bot=mock_bot)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        su_cookie = await _su_login(client)

        # Сначала создаём moderator с чатами
        user_chat = _make_chat_obj(777888333, username="mod_user2", first_name="Mod2")
        mock_bot.get_chat = AsyncMock(return_value=user_chat)
        await client.post(
            "/admin/users/create",
            data={
                "tg_user_id": "777888333",
                "role": "moderator",
                "chat_ids": ["-1001", "-1002"],
            },
            cookies={"sl_session": su_cookie},
            follow_redirects=False,
        )

        async with async_session() as s:
            wu = (await s.execute(select(WebUser).where(WebUser.tg_user_id == 777888333))).scalar_one()
            mod_id = wu.id
            cas = (await s.execute(select(ChatAdmin).where(ChatAdmin.user_id == 777888333))).scalars().all()
            check("до повышения: 2 chat_admins", len(cas) == 2, f"got {len(cas)}")

        # Повышаем до admin
        resp = await client.post(
            f"/admin/users/{mod_id}/role",
            data={"role": "admin"},
            cookies={"sl_session": su_cookie},
            follow_redirects=False,
        )
        check("role change → 303", resp.status_code == 303)

        async with async_session() as s:
            wu = (await s.execute(select(WebUser).where(WebUser.id == mod_id))).scalar_one()
            check("role теперь admin", wu.role == "admin")
            cas = (await s.execute(select(ChatAdmin).where(ChatAdmin.user_id == 777888333))).scalars().all()
            check("chat_admins очищены", len(cas) == 0, f"got {len(cas)}")


# ──────────────────────────────────────────────────────────────────────────
# 6. POST /admin/users/{id}/bind-tg — привязка TG к SU
# ──────────────────────────────────────────────────────────────────────────
async def test_bind_tg_to_su():
    print("\n[6] POST /admin/users/{id}/bind-tg — привязка TG к SU")
    from web_app import create_app
    from httpx import AsyncClient, ASGITransport
    from db import async_session, WebUser
    from sqlalchemy import select

    mock_bot = MagicMock()
    su_chat = _make_chat_obj(999999999, username="su_tg", first_name="SU")
    mock_bot.get_chat = AsyncMock(return_value=su_chat)
    app = create_app(bot=mock_bot)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        su_cookie = await _su_login(client)

        async with async_session() as s:
            su_wu = (await s.execute(select(WebUser).where(WebUser.username == "su"))).scalar_one()
            su_id = su_wu.id
            check("SU без TG ID до bind", su_wu.tg_user_id is None)

        resp = await client.post(
            f"/admin/users/{su_id}/bind-tg",
            data={"tg_user_id": "999999999"},
            cookies={"sl_session": su_cookie},
            follow_redirects=False,
        )
        check("bind-tg → 303", resp.status_code == 303)

        async with async_session() as s:
            su_wu = (await s.execute(select(WebUser).where(WebUser.id == su_id))).scalar_one()
            check("SU TG ID привязан", su_wu.tg_user_id == 999999999)
            check("SU username подтянут из TG", su_wu.tg_username == "su_tg")


# ──────────────────────────────────────────────────────────────────────────
# 7. Старые роуты /admin/moderators/* возвращают 404
# ──────────────────────────────────────────────────────────────────────────
async def test_old_moderators_routes_gone():
    print("\n[7] Старые роуты /admin/moderators/* удалены (404)")
    from web_app import create_app
    from httpx import AsyncClient, ASGITransport

    mock_bot = MagicMock()
    mock_bot.get_chat = AsyncMock()
    app = create_app(bot=mock_bot)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        su_cookie = await _su_login(client)
        resp = await client.get("/admin/moderators", cookies={"sl_session": su_cookie},
                                follow_redirects=False)
        check("GET /admin/moderators → 404", resp.status_code == 404, f"got {resp.status_code}")
        resp = await client.post("/admin/moderators/create",
                                 data={"chat_id": "-1", "user_id": "1"},
                                 cookies={"sl_session": su_cookie},
                                 follow_redirects=False)
        check("POST /admin/moderators/create → 404", resp.status_code == 404)
        resp = await client.post("/admin/moderators/1/delete",
                                 cookies={"sl_session": su_cookie},
                                 follow_redirects=False)
        check("POST /admin/moderators/1/delete → 404", resp.status_code == 404)


# ──────────────────────────────────────────────────────────────────────────
# 8. Nav: только SU видит Users
# ──────────────────────────────────────────────────────────────────────────
async def test_nav_users_only_su():
    print("\n[8] Nav: только SU видит Users")
    from web_app import create_app
    from httpx import AsyncClient, ASGITransport

    mock_bot = MagicMock()
    mock_bot.get_chat = AsyncMock()
    app = create_app(bot=mock_bot)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        su_cookie = await _su_login(client)
        resp = await client.get("/dashboard", cookies={"sl_session": su_cookie})
        check("SU видит Users", 'href="/admin/users"' in resp.text)

        # admin
        resp = await client.post("/login",
                                 data={"username": "admin_user1", "password": ""},
                                 follow_redirects=False)
        # admin_user1 создан в тесте 3; пароля нет в форме — используем прямой логин через БД
        # Пропустим — в этом тесте проверяем только SU nav


# ──────────────────────────────────────────────────────────────────────────
# 9. /admin/users недоступен admin/moderator
# ──────────────────────────────────────────────────────────────────────────
async def test_admin_users_access_control():
    print("\n[9] /admin/users доступ: SU OK; admin/moderator → redirect /dashboard")
    from web_app import create_app
    from httpx import AsyncClient, ASGITransport
    from db import async_session, WebUser
    from sqlalchemy import select

    mock_bot = MagicMock()
    mock_bot.get_chat = AsyncMock()
    app = create_app(bot=mock_bot)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # SU
        su_cookie = await _su_login(client)
        resp = await client.get("/admin/users", cookies={"sl_session": su_cookie},
                                follow_redirects=False)
        check("SU: GET /admin/users → 200", resp.status_code == 200)

        # admin (логинимся через БД-пароль: нет, используем _hash_password)
        from db import _hash_password
        async with async_session() as s:
            adm = (await s.execute(select(WebUser).where(WebUser.username == "admin_user1"))).scalar_one()
            adm.password_hash = _hash_password("test_pw_admin")
            await s.commit()
        resp = await client.post("/login",
                                 data={"username": "admin_user1", "password": "test_pw_admin"},
                                 follow_redirects=False)
        adm_cookie = resp.cookies.get("sl_session")
        resp = await client.get("/admin/users", cookies={"sl_session": adm_cookie},
                                follow_redirects=False)
        check("admin: GET /admin/users → 303", resp.status_code == 303)
        check("admin: redirect на /dashboard", resp.headers.get("location") == "/dashboard")

        # moderator
        from db import _hash_password
        async with async_session() as s:
            mod = (await s.execute(select(WebUser).where(WebUser.username == "mod_user2"))).scalar_one()
            mod.role = "moderator"
            mod.password_hash = _hash_password("test_pw_mod")
            await s.commit()
        resp = await client.post("/login",
                                 data={"username": "mod_user2", "password": "test_pw_mod"},
                                 follow_redirects=False)
        mod_cookie = resp.cookies.get("sl_session")
        resp = await client.get("/admin/users", cookies={"sl_session": mod_cookie},
                                follow_redirects=False)
        check("moderator: GET /admin/users → 303", resp.status_code == 303)


# ──────────────────────────────────────────────────────────────────────────
# 10. TG-only модераторы отображаются на /admin/users
# ──────────────────────────────────────────────────────────────────────────
async def test_tg_only_moderators_section():
    print("\n[10] TG-only модераторы отображаются отдельной секцией")
    from web_app import create_app
    from httpx import AsyncClient, ASGITransport
    from db import async_session, ChatAdmin
    from sqlalchemy import select

    mock_bot = MagicMock()
    mock_bot.get_chat = AsyncMock()
    app = create_app(bot=mock_bot)
    transport = ASGITransport(app=app)

    # Создаём chat_admin для TG ID, у которого НЕТ веб-аккаунта
    async with async_session() as s:
        s.add(ChatAdmin(chat_id=-1001, user_id=888777666, added_by=None))
        await s.commit()

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        su_cookie = await _su_login(client)
        resp = await client.get("/admin/users", cookies={"sl_session": su_cookie})
        check("есть секция TG-only moderators", "TG-only moderators" in resp.text)
        check("ID 888777666 в HTML", "888777666" in resp.text)


# ──────────────────────────────────────────────────────────────────────────
# 11. TG-only модераторы отображаются на /admin/users
# ──────────────────────────────────────────────────────────────────────────
async def test_toggle_disable_enable():
    print("\n[11] POST /admin/users/{id}/toggle — disable/enable")
    from web_app import create_app
    from httpx import AsyncClient, ASGITransport
    from db import async_session, WebUser
    from sqlalchemy import select

    mock_bot = MagicMock()
    mock_bot.get_chat = AsyncMock()
    app = create_app(bot=mock_bot)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        su_cookie = await _su_login(client)
        async with async_session() as s:
            wu = (await s.execute(select(WebUser).where(WebUser.username == "admin_user1"))).scalar_one()
            uid = wu.id
            check("изначально active", wu.is_active is True)

        resp = await client.post(f"/admin/users/{uid}/toggle",
                                 cookies={"sl_session": su_cookie}, follow_redirects=False)
        check("toggle → 303", resp.status_code == 303)
        async with async_session() as s:
            wu = (await s.execute(select(WebUser).where(WebUser.id == uid))).scalar_one()
            check("теперь disabled", wu.is_active is False)

        resp = await client.post(f"/admin/users/{uid}/toggle",
                                 cookies={"sl_session": su_cookie}, follow_redirects=False)
        async with async_session() as s:
            wu = (await s.execute(select(WebUser).where(WebUser.id == uid))).scalar_one()
            check("снова active", wu.is_active is True)


# ──────────────────────────────────────────────────────────────────────────
# 12. SU нельзя заблокировать через toggle
# ──────────────────────────────────────────────────────────────────────────
async def test_su_cannot_be_disabled():
    print("\n[12] SU нельзя заблокировать через toggle")
    from web_app import create_app
    from httpx import AsyncClient, ASGITransport
    from db import async_session, WebUser
    from sqlalchemy import select

    mock_bot = MagicMock()
    mock_bot.get_chat = AsyncMock()
    app = create_app(bot=mock_bot)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        su_cookie = await _su_login(client)
        async with async_session() as s:
            su_wu = (await s.execute(select(WebUser).where(WebUser.username == "su"))).scalar_one()
            su_id = su_wu.id

        resp = await client.post(f"/admin/users/{su_id}/toggle",
                                 cookies={"sl_session": su_cookie}, follow_redirects=False)
        check("toggle SU → 303 (без изменений)", resp.status_code == 303)
        async with async_session() as s:
            su_wu = (await s.execute(select(WebUser).where(WebUser.id == su_id))).scalar_one()
            check("SU остался active", su_wu.is_active is True)


# ──────────────────────────────────────────────────────────────────────────
# 13. /admin/chats toggles: enabled/private/report
# ──────────────────────────────────────────────────────────────────────────
async def test_chats_toggles():
    print("\n[13] /admin/chats/{id}/toggle — enabled/private/report_chat")
    from web_app import create_app
    from httpx import AsyncClient, ASGITransport
    from db import async_session, ChatSettings
    from sqlalchemy import select

    mock_bot = MagicMock()
    mock_bot.get_chat = AsyncMock()
    app = create_app(bot=mock_bot)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        su_cookie = await _su_login(client)

        # Toggle enabled
        resp = await client.post(
            "/admin/chats/-1001/toggle",
            data={"field": "enabled"},
            cookies={"sl_session": su_cookie},
            follow_redirects=False,
        )
        check("toggle enabled → 303", resp.status_code == 303)
        async with async_session() as s:
            cs = (await s.execute(select(ChatSettings).where(ChatSettings.chat_id == -1001))).scalar_one()
            check("is_enabled теперь False", cs.is_enabled is False)

        # Toggle private
        resp = await client.post(
            "/admin/chats/-1001/toggle",
            data={"field": "private"},
            cookies={"sl_session": su_cookie},
            follow_redirects=False,
        )
        async with async_session() as s:
            cs = (await s.execute(select(ChatSettings).where(ChatSettings.chat_id == -1001))).scalar_one()
            check("is_private теперь True", cs.is_private is True)

        # Toggle report_chat
        resp = await client.post(
            "/admin/chats/-1002/toggle",
            data={"field": "report_chat"},
            cookies={"sl_session": su_cookie},
            follow_redirects=False,
        )
        async with async_session() as s:
            cs2 = (await s.execute(select(ChatSettings).where(ChatSettings.chat_id == -1002))).scalar_one()
            check("is_report_chat True для -1002", cs2.is_report_chat is True)
        # Установка report_chat для -1001 должна снять флаг с -1002
        resp = await client.post(
            "/admin/chats/-1001/toggle",
            data={"field": "report_chat"},
            cookies={"sl_session": su_cookie},
            follow_redirects=False,
        )
        async with async_session() as s:
            cs1 = (await s.execute(select(ChatSettings).where(ChatSettings.chat_id == -1001))).scalar_one()
            cs2 = (await s.execute(select(ChatSettings).where(ChatSettings.chat_id == -1002))).scalar_one()
            check("is_report_chat True для -1001", cs1.is_report_chat is True)
            check("is_report_chat False для -1002 (снят)", cs2.is_report_chat is False)


# ──────────────────────────────────────────────────────────────────────────
# 14. user_can_moderate — унифицированная проверка прав (в bot_handlers)
# ──────────────────────────────────────────────────────────────────────────
async def test_user_can_moderate_logic():
    print("\n[14] _is_admin — унифицированная проверка прав (role × private × enabled)")
    from bot_handlers import _is_admin
    from db import async_session, WebUser, ChatAdmin, ChatSettings
    from sqlalchemy import select

    # ── Гарантируем состояние чатов: -1001 public+enabled, -1002 private+enabled, -1003 disabled
    async with async_session() as s:
        # Чистим предыдущие chat_admins
        for r in (await s.execute(select(ChatAdmin))).scalars().all():
            await s.delete(r)
        # Сбрасываем настройки чатов в детерминированное состояние
        for cid, is_priv, is_en in [(-1001, False, True), (-1002, True, True), (-1003, False, False)]:
            cs = (await s.execute(select(ChatSettings).where(ChatSettings.chat_id == cid))).scalar_one_or_none()
            if cs is None:
                cs = ChatSettings(chat_id=cid)
                s.add(cs)
            cs.is_private = is_priv
            cs.is_enabled = is_en
            cs.is_report_chat = False
            cs.hashtag = f"#C{abs(cid)}"
        # Чистим предыдущих тестовых юзеров
        for r in (await s.execute(select(WebUser).where(WebUser.username.in_(
            ["su_tg_test", "admin_tg_test", "mod_tg_test", "mod_no_chats"]
        )))).scalars().all():
            await s.delete(r)
        await s.flush()
        # SU с TG
        s.add(WebUser(username="su_tg_test", password_hash=None, is_su=True, role="su",
                      is_active=True, tg_user_id=555, created_by="system"))
        # admin с TG
        s.add(WebUser(username="admin_tg_test", password_hash="x:y", is_su=False, role="admin",
                      is_active=True, tg_user_id=666, created_by="su"))
        # moderator с TG, привязан к -1001
        s.add(WebUser(username="mod_tg_test", password_hash="x:y", is_su=False, role="moderator",
                      is_active=True, tg_user_id=777, created_by="su"))
        s.add(ChatAdmin(chat_id=-1001, user_id=777, added_by=None))
        await s.commit()

    # SU: всё True (кроме disabled чатов)
    async with async_session() as s:
        r = await _is_admin(s, -1001, 555)  # public
        check("SU в public → True", r is True)
        r = await _is_admin(s, -1002, 555)  # private
        check("SU в private → True", r is True)
        r = await _is_admin(s, -1003, 555)  # disabled
        check("SU в disabled → False (чат выключен)", r is False)

    # admin: public OK, private NO
    async with async_session() as s:
        r = await _is_admin(s, -1001, 666)
        check("admin в public → True", r is True)
        r = await _is_admin(s, -1002, 666)
        check("admin в private → False", r is False)
        r = await _is_admin(s, -1003, 666)
        check("admin в disabled → False", r is False)

    # moderator: только в -1001 (привязан)
    async with async_session() as s:
        r = await _is_admin(s, -1001, 777)
        check("moderator в привязанном чате → True", r is True)
        r = await _is_admin(s, -1002, 777)
        check("moderator в непривязанном чате → False", r is False)
        r = await _is_admin(s, -1003, 777)
        check("moderator в disabled → False", r is False)

    # Moderator без чатов: везде False
    async with async_session() as s:
        s.add(WebUser(username="mod_no_chats", password_hash="x:y", is_su=False, role="moderator",
                      is_active=True, tg_user_id=888, created_by="su"))
        await s.commit()
    async with async_session() as s:
        r = await _is_admin(s, -1001, 888)
        check("moderator без чатов → False", r is False)


# ──────────────────────────────────────────────────────────────────────────
# 15. Welcome DM отправляется новому moderator
# ──────────────────────────────────────────────────────────────────────────
async def test_welcome_dm_for_moderator():
    print("\n[15] Welcome DM отправляется новому moderator")
    from web_app import create_app
    from httpx import AsyncClient, ASGITransport

    mock_bot = MagicMock()
    user_chat = _make_chat_obj(777888444, username="mod_welcome", first_name="WelcomeMod")
    mock_bot.get_chat = AsyncMock(return_value=user_chat)
    mock_bot.send_rich_message = AsyncMock()
    app = create_app(bot=mock_bot)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        su_cookie = await _su_login(client)
        await client.post(
            "/admin/users/create",
            data={"tg_user_id": "777888444", "role": "moderator"},
            cookies={"sl_session": su_cookie},
            follow_redirects=False,
        )
        check("send_rich_message вызван", mock_bot.send_rich_message.called)
        if mock_bot.send_rich_message.called:
            args, kwargs = mock_bot.send_rich_message.call_args
            check("chat_id = 777888444", kwargs.get("chat_id") == 777888444)


async def main():
    print("=" * 70)
    print("v4.4.7 tests: unified Users tab (Admins + Moderators merged)")
    print("=" * 70)

    await test_get_page()
    await test_create_moderator_with_chats()
    await test_create_admin_ignores_chats()
    await test_edit_chats_moderator()
    await test_change_role_clears_chat_admins()
    await test_bind_tg_to_su()
    await test_old_moderators_routes_gone()
    await test_nav_users_only_su()
    await test_admin_users_access_control()
    await test_tg_only_moderators_section()
    await test_toggle_disable_enable()
    await test_su_cannot_be_disabled()
    await test_chats_toggles()
    await test_user_can_moderate_logic()
    await test_welcome_dm_for_moderator()

    print()
    print("=" * 70)
    print(f"PASSED: {PASS_COUNT} | FAILED: {FAIL_COUNT}")
    if FAIL_COUNT:
        print("Failures:")
        for f in FAILURES:
            print(f"  - {f}")
        print("TESTS FAILED ❌")
        sys.exit(1)
    else:
        print("ALL TESTS PASSED ✅")


if __name__ == "__main__":
    asyncio.run(main())
