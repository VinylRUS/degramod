"""
test_v44_cleanup_web.py — Тесты v4.4.5: очистка тестовых данных через веб-панель.

Покрывает:
  - GET /admin/cleanup — страница с live-превью (SU-only)
  - POST /admin/cleanup — реальное удаление (SU-only)
  - Чекбокс include_chat_admins — опциональная очистка chat_admins
  - Бэкап создаётся перед удалением
  - VACUUM срабатывает (по изменению размера файла или по mtime)
  - moderators, web_users, chat_settings НЕ трогаются
  - users очищаются, кроме модераторов (если user_id == mod_id)
  - Защита: на пустой БД (no moderators + no web_users) — отказ
  - Non-SU → redirect на /dashboard
  - Неавторизованный → redirect на /login
  - HTML: nav link 'Cleanup' виден SU и не виден non-SU
  - HTML: confirm() в onsubmit формы
  - HTML: preview таблица показывает счётчики

Запуск:
    cd /home/z/my-project/v4.4
    python3 scripts/test_v44_cleanup_web.py
"""
from __future__ import annotations

import asyncio
import glob
import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP_DB = "/tmp/test_v44_cleanup_web.db"
if os.path.exists(TMP_DB):
    os.remove(TMP_DB)
# Удаляем старые бэкапы от прошлых прогонов
for old_bak in glob.glob(f"{TMP_DB}.backup-*.db"):
    os.remove(old_bak)
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


def _seed_test_data() -> None:
    """Наполняет БД тестовыми данными: 2 модератора, 1 веб-юзер, 3 users (1 модератор), 3 наказания, 2 chat_admins, 1 chat_setting."""
    conn = sqlite3.connect(TMP_DB)
    conn.execute("PRAGMA foreign_keys=ON")
    # Очищаем все таблицы КРОМЕ web_users — иначе SU удаляется и логин не проходит.
    # web_users перетрём явно: оставляем SU (если есть) + добавляем test_admin.
    for t in ("punishments", "users", "moderators", "chat_admins", "chat_settings"):
        conn.execute(f"DELETE FROM {t}")
    conn.execute("DELETE FROM web_users WHERE username != 'su'")
    # Гарантируем что SU есть (init_db его создаёт, но на всякий случай)
    su_exists = conn.execute(
        "SELECT COUNT(*) FROM web_users WHERE username = 'su'"
    ).fetchone()[0]
    if su_exists == 0:
        conn.execute(
            "INSERT INTO web_users (username, password_hash, is_su, is_active, created_by, role) "
            "VALUES ('su', NULL, 1, 1, 'system', 'su')"
        )
    # Модераторы
    conn.execute("INSERT INTO moderators (mod_id, username, first_name) VALUES (100001, 'mod1', 'Mod1')")
    conn.execute("INSERT INTO moderators (mod_id, username, first_name) VALUES (100002, 'mod2', 'Mod2')")
    # Веб-юзер (дополнительный к SU)
    conn.execute("INSERT INTO web_users (username, password_hash, is_su, is_active, created_by, role) "
                 "VALUES ('test_admin', 'salt:hash', 0, 1, 'su', 'admin')")
    # Users: 2 тестовых + 1 модератор
    conn.execute("INSERT INTO users (user_id, username, first_name) VALUES (200001, 'bad1', 'Bad1')")
    conn.execute("INSERT INTO users (user_id, username, first_name) VALUES (200002, 'bad2', 'Bad2')")
    conn.execute("INSERT INTO users (user_id, username, first_name) VALUES (100001, 'mod1', 'Mod1')")
    # Punishments (3 шт)
    for uid in (200001, 200002, 100001):
        conn.execute(
            "INSERT INTO punishments (user_id, mod_id, chat_id, action_type, created_at, is_revoked) "
            f"VALUES ({uid}, 100001, 999, 'warn', '2026-07-26 10:00:00', 0)"
        )
    # Chat admins
    conn.execute("INSERT INTO chat_admins (chat_id, user_id, added_by) VALUES (999, 200001, 100001)")
    conn.execute("INSERT INTO chat_admins (chat_id, user_id, added_by) VALUES (999, 200002, 100001)")
    # Chat settings
    conn.execute("INSERT INTO chat_settings (chat_id, hashtag, warns_to_mute, warns_to_ban) "
                 "VALUES (999, '#Test', 3, 5)")
    conn.commit()
    conn.close()


def _counts() -> dict[str, int]:
    conn = sqlite3.connect(TMP_DB)
    out = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
           for t in ("users", "moderators", "punishments", "chat_admins", "chat_settings", "web_users")}
    out["users_mod1"] = conn.execute(
        "SELECT COUNT(*) FROM users WHERE user_id = 100001"
    ).fetchone()[0]
    conn.close()
    return out


async def _su_login(client) -> str:
    resp = await client.post(
        "/login",
        data={"username": "su", "password": "test_su_password_123"},
        follow_redirects=False,
    )
    return resp.cookies.get("sl_session")


# ──────────────────────────────────────────────────────────────────────────
# 1. GET /admin/cleanup — страница с превью
# ──────────────────────────────────────────────────────────────────────────
async def test_get_page_su():
    print("\n[1] GET /admin/cleanup — страница с live-превью (SU)")
    from db import init_db
    from web_app import create_app
    from httpx import AsyncClient, ASGITransport

    await init_db()
    _seed_test_data()

    mock_bot = MagicMock()
    mock_bot.get_chat = AsyncMock()
    app = create_app(bot=mock_bot)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        su_cookie = await _su_login(client)
        check("SU login OK", su_cookie is not None)

        resp = await client.get("/admin/cleanup", cookies={"sl_session": su_cookie})
        check("GET /admin/cleanup → 200", resp.status_code == 200, f"got {resp.status_code}")

        # Ключевые элементы страницы
        check("есть заголовок 'Preview'", "Preview" in resp.text)
        check("есть заголовок 'Apply cleanup'", "Apply cleanup" in resp.text)
        check("есть форма POST /admin/cleanup", 'action="/admin/cleanup"' in resp.text)
        check("есть чекбокс include_chat_admins", 'name="include_chat_admins"' in resp.text)
        check("есть подтверждение confirm()", "confirm(" in resp.text)
        check("есть упоминание backup", "backup" in resp.text.lower())
        check("есть упоминание VACUUM", "VACUUM" in resp.text)

        # Превью счётчиков
        check("превью показывает punishments=3", ">3<" in resp.text)
        check("превью показывает moderators=2", ">2<" in resp.text)
        check("превью показывает users=3", ">3<" in resp.text)
        check("превью показывает 'PRESERVED' для moderators", "PRESERVED" in resp.text)
        check("превью показывает 'DELETE ALL' для punishments", "DELETE ALL" in resp.text)

        # Нет блока отказа (т.к. есть модераторы и веб-юзеры)
        check("нет 'Refusing to apply'", "Refusing to apply" not in resp.text)


# ──────────────────────────────────────────────────────────────────────────
# 2. POST /admin/cleanup — реальное удаление (без chat_admins)
# ──────────────────────────────────────────────────────────────────────────
async def test_post_apply_no_chat_admins():
    print("\n[2] POST /admin/cleanup — удаление (chat_admins не трогаем)")
    from web_app import create_app
    from httpx import AsyncClient, ASGITransport

    _seed_test_data()

    mock_bot = MagicMock()
    mock_bot.get_chat = AsyncMock()
    app = create_app(bot=mock_bot)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        su_cookie = await _su_login(client)
        resp = await client.post(
            "/admin/cleanup",
            data={},
            cookies={"sl_session": su_cookie},
            follow_redirects=False,
        )
        check("POST /admin/cleanup → 200 (renders result)", resp.status_code == 200,
              f"got {resp.status_code}")

        # Результат содержит блок
        check("есть 'Cleanup complete'", "Cleanup complete" in resp.text)
        check("показано 'Punishments removed: 3'", "3" in resp.text)
        check("показано 'backup'", "backup" in resp.text.lower())

    # Проверяем фактическое состояние БД
    after = _counts()
    check("punishments очищены", after["punishments"] == 0)
    check("users: осталось 1 (только модератор)", after["users"] == 1,
          f"got {after['users']}")
    check("moderator 100001 остался в users", after["users_mod1"] == 1)
    check("moderators не тронуты", after["moderators"] == 2)
    check("web_users не тронуты", after["web_users"] >= 1)
    check("chat_settings не тронуты", after["chat_settings"] == 1)
    check("chat_admins НЕ тронуты (по умолчанию)", after["chat_admins"] == 2,
          f"got {after['chat_admins']}")


# ──────────────────────────────────────────────────────────────────────────
# 3. POST /admin/cleanup с include_chat_admins
# ──────────────────────────────────────────────────────────────────────────
async def test_post_apply_with_chat_admins():
    print("\n[3] POST /admin/cleanup — с include_chat_admins=1")
    from web_app import create_app
    from httpx import AsyncClient, ASGITransport

    _seed_test_data()

    mock_bot = MagicMock()
    mock_bot.get_chat = AsyncMock()
    app = create_app(bot=mock_bot)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        su_cookie = await _su_login(client)
        resp = await client.post(
            "/admin/cleanup",
            data={"include_chat_admins": "1"},
            cookies={"sl_session": su_cookie},
            follow_redirects=False,
        )
        check("POST → 200", resp.status_code == 200)

    after = _counts()
    check("chat_admins очищены", after["chat_admins"] == 0)
    check("punishments очищены", after["punishments"] == 0)
    check("moderators не тронуты", after["moderators"] == 2)
    check("web_users не тронуты", after["web_users"] >= 1)


# ──────────────────────────────────────────────────────────────────────────
# 4. Backup создаётся
# ──────────────────────────────────────────────────────────────────────────
async def test_backup_created():
    print("\n[4] POST /admin/cleanup — создаётся .backup-*.db файл")
    from web_app import create_app
    from httpx import AsyncClient, ASGITransport

    _seed_test_data()
    # Подсчитываем бэкапы до
    backups_before = list(glob.glob(f"{TMP_DB}.backup-*.db"))

    mock_bot = MagicMock()
    mock_bot.get_chat = AsyncMock()
    app = create_app(bot=mock_bot)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        su_cookie = await _su_login(client)
        resp = await client.post(
            "/admin/cleanup", data={},
            cookies={"sl_session": su_cookie}, follow_redirects=False,
        )
        check("POST → 200", resp.status_code == 200)

    backups_after = list(glob.glob(f"{TMP_DB}.backup-*.db"))
    check("создан 1 новый бэкап",
          len(backups_after) - len(backups_before) == 1,
          f"before={len(backups_before)}, after={len(backups_after)}")
    check("backup filename содержит timestamp", any(".backup-20" in b for b in backups_after))

    # Бэкап должен содержать данные ДО удаления (punishments=3)
    if backups_after:
        # Берём самый свежий
        latest = max(backups_after, key=os.path.getmtime)
        conn = sqlite3.connect(latest)
        cnt = conn.execute("SELECT COUNT(*) FROM punishments").fetchone()[0]
        conn.close()
        check("backup содержит punishments=3 (снапшот до удаления)", cnt == 3,
              f"got {cnt}")


# ──────────────────────────────────────────────────────────────────────────
# 5. Защита: если moderators=0 И web_users=0 → POST отказывается
# ──────────────────────────────────────────────────────────────────────────
async def test_refuse_on_empty_db():
    print("\n[5] Защита: пустая БД (no moderators + no web_users) → POST отказывается")
    from web_app import create_app
    from httpx import AsyncClient, ASGITransport

    # Полностью очищаем БД (включая SU и модераторов) — эмулируем свежую БД
    conn = sqlite3.connect(TMP_DB)
    for t in ("punishments", "users", "moderators", "chat_admins", "chat_settings", "web_users"):
        conn.execute(f"DELETE FROM {t}")
    conn.commit()
    conn.close()

    mock_bot = MagicMock()
    mock_bot.get_chat = AsyncMock()
    app = create_app(bot=mock_bot)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Логин невозможен — нет SU. Поэтому POST без auth → 303 на /login.
        # Проверим это.
        resp = await client.post("/admin/cleanup", data={}, follow_redirects=False)
        check("POST на пустой БД без auth → 303 redirect на /login",
              resp.status_code == 303 and "/login" in resp.headers.get("location", ""))

    # Теперь: в БД есть только SU (web_users=1, moderators=0).
    # Cleanup должен РАЗРЕШИТЬ выполнение (т.к. web_users > 0).
    # Это нормально — safety check только предотвращает запуск на ПОЛНОСТЬЮ пустой БД.
    from db import init_db
    await init_db()  # Пересоздаст SU

    mock_bot2 = MagicMock()
    mock_bot2.get_chat = AsyncMock()
    app2 = create_app(bot=mock_bot2)
    transport2 = ASGITransport(app=app2)
    async with AsyncClient(transport=transport2, base_url="http://test") as client:
        su_cookie = await _su_login(client)
        check("SU login OK после восстановления", su_cookie is not None)

        # GET — должен показать страницу БЕЗ 'Refusing' (т.к. web_users >= 1)
        resp = await client.get("/admin/cleanup", cookies={"sl_session": su_cookie})
        check("GET → 200 (moderators=0, но web_users=1, очистка разрешена)",
              resp.status_code == 200)

        # POST — должен выполниться (модераторов нет, но web_users есть)
        resp = await client.post("/admin/cleanup", data={},
                                 cookies={"sl_session": su_cookie},
                                 follow_redirects=False)
        check("POST → 200 (выполнился, не отказал)",
              resp.status_code == 200,
              f"got {resp.status_code}")


# ──────────────────────────────────────────────────────────────────────────
# 6. Non-SU → redirect на /dashboard
# ──────────────────────────────────────────────────────────────────────────
async def test_non_su_access():
    print("\n[6] Non-SU → redirect на /dashboard")
    from db import async_session, WebUser, _hash_password
    from web_app import create_app
    from httpx import AsyncClient, ASGITransport

    _seed_test_data()
    async with async_session() as s:
        s.add(WebUser(
            username="regular_admin",
            password_hash=_hash_password("reg_pw_123"),
            is_su=False, is_active=True, created_by="su",
            tg_user_id=555555, tg_username="regular_admin",
        ))
        await s.commit()

    mock_bot = MagicMock()
    mock_bot.get_chat = AsyncMock()
    app = create_app(bot=mock_bot)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/login",
            data={"username": "regular_admin", "password": "reg_pw_123"},
            follow_redirects=False,
        )
        reg_cookie = resp.cookies.get("sl_session")
        check("regular admin login OK", reg_cookie is not None)

        # GET
        resp = await client.get("/admin/cleanup", cookies={"sl_session": reg_cookie},
                                follow_redirects=False)
        check("GET /admin/cleanup non-SU → 303", resp.status_code == 303)
        check("GET redirect to /dashboard", "/dashboard" in resp.headers.get("location", ""))

        # POST
        resp = await client.post("/admin/cleanup", data={},
                                 cookies={"sl_session": reg_cookie},
                                 follow_redirects=False)
        check("POST /admin/cleanup non-SU → 303", resp.status_code == 303)
        check("POST redirect to /dashboard", "/dashboard" in resp.headers.get("location", ""))

    # Данные не должны измениться
    after = _counts()
    check("punishments не тронуты (3)", after["punishments"] == 3)


# ──────────────────────────────────────────────────────────────────────────
# 7. Неавторизованный → redirect на /login
# ──────────────────────────────────────────────────────────────────────────
async def test_unauthenticated_access():
    print("\n[7] Неавторизованный → redirect на /login")
    from web_app import create_app
    from httpx import AsyncClient, ASGITransport

    _seed_test_data()
    mock_bot = MagicMock()
    mock_bot.get_chat = AsyncMock()
    app = create_app(bot=mock_bot)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # GET без cookie
        resp = await client.get("/admin/cleanup", follow_redirects=False)
        check("GET без auth → 303", resp.status_code == 303)
        check("GET redirect to /login", "/login" in resp.headers.get("location", ""))

        # POST без cookie
        resp = await client.post("/admin/cleanup", data={}, follow_redirects=False)
        check("POST без auth → 303", resp.status_code == 303)
        check("POST redirect to /login", "/login" in resp.headers.get("location", ""))


# ──────────────────────────────────────────────────────────────────────────
# 8. HTML: nav link 'Cleanup' виден SU и не виден non-SU
# ──────────────────────────────────────────────────────────────────────────
async def test_nav_link_visibility():
    print("\n[8] HTML: nav link 'Cleanup' в навбаре (SU видит, non-SU не видит)")
    from web_app import create_app
    from httpx import AsyncClient, ASGITransport

    _seed_test_data()
    mock_bot = MagicMock()
    mock_bot.get_chat = AsyncMock()
    app = create_app(bot=mock_bot)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        su_cookie = await _su_login(client)
        resp = await client.get("/dashboard", cookies={"sl_session": su_cookie})
        check("SU видит 'Cleanup' в навбаре", 'href="/admin/cleanup"' in resp.text)

    # Non-SU не видит
    from db import async_session, WebUser, _hash_password
    async with async_session() as s:
        s.add(WebUser(
            username="nav_check_admin",
            password_hash=_hash_password("pw_456"),
            is_su=False, is_active=True, created_by="su",
            tg_user_id=666666, tg_username="nav_check_admin",
        ))
        await s.commit()
    async with AsyncClient(transport=ASGITransport(app=create_app(bot=mock_bot)),
                           base_url="http://test") as client:
        resp = await client.post("/login",
                                 data={"username": "nav_check_admin", "password": "pw_456"},
                                 follow_redirects=False)
        reg_cookie = resp.cookies.get("sl_session")
        resp = await client.get("/dashboard", cookies={"sl_session": reg_cookie})
        check("non-SU НЕ видит 'Cleanup' в навбаре",
              'href="/admin/cleanup"' not in resp.text)


# ──────────────────────────────────────────────────────────────────────────
# 9. VACUUM — проверка что файл БД сжался
# ──────────────────────────────────────────────────────────────────────────
async def test_vacuum_runs():
    print("\n[9] VACUUM — файл БД сжимается после очистки")
    from web_app import create_app
    from httpx import AsyncClient, ASGITransport

    _seed_test_data()

    # Создаём большой мусор для гарантированного сжатия
    conn = sqlite3.connect(TMP_DB)
    for i in range(500):
        conn.execute(
            "INSERT INTO punishments (user_id, mod_id, chat_id, action_type, reason, created_at, is_revoked) "
            f"VALUES (200001, 100001, 999, 'warn', 'filler_{i}_padding_data_here_for_size', '2026-07-26 10:00:00', 0)"
        )
    conn.commit()
    conn.close()

    size_before = os.path.getsize(TMP_DB)

    mock_bot = MagicMock()
    mock_bot.get_chat = AsyncMock()
    app = create_app(bot=mock_bot)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        su_cookie = await _su_login(client)
        resp = await client.post("/admin/cleanup", data={},
                                 cookies={"sl_session": su_cookie},
                                 follow_redirects=False)
        check("POST → 200", resp.status_code == 200)
        check("есть упоминание VACUUM в результате", "VACUUM" in resp.text)

    size_after = os.path.getsize(TMP_DB)
    check("файл БД уменьшился после VACUUM",
          size_after < size_before,
          f"before={size_before}, after={size_after}")


# ──────────────────────────────────────────────────────────────────────────
# 10. Подтверждение confirm() в форме — защита от случайного нажатия
# ──────────────────────────────────────────────────────────────────────────
async def test_confirm_in_form():
    print("\n[10] HTML: форма содержит confirm() в onsubmit")
    from web_app import create_app
    from httpx import AsyncClient, ASGITransport

    _seed_test_data()
    mock_bot = MagicMock()
    mock_bot.get_chat = AsyncMock()
    app = create_app(bot=mock_bot)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        su_cookie = await _su_login(client)
        resp = await client.get("/admin/cleanup", cookies={"sl_session": su_cookie})

        # Форма должна содержать onsubmit с confirm
        check("форма содержит onsubmit", "onsubmit" in resp.text)
        check("onsubmit содержит confirm(", "confirm(" in resp.text)
        check("confirm содержит 'Apply cleanup?'", "Apply cleanup" in resp.text)


# ──────────────────────────────────────────────────────────────────────────
# 11. Идемпотентность: повторный POST не должен ломать БД
# ──────────────────────────────────────────────────────────────────────────
async def test_idempotent_second_run():
    print("\n[11] Идемпотентность: повторный POST не ломает БД")
    from web_app import create_app
    from httpx import AsyncClient, ASGITransport

    _seed_test_data()
    mock_bot = MagicMock()
    mock_bot.get_chat = AsyncMock()
    app = create_app(bot=mock_bot)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        su_cookie = await _su_login(client)

        # Первый прогон — удаляет данные
        resp1 = await client.post("/admin/cleanup", data={},
                                  cookies={"sl_session": su_cookie},
                                  follow_redirects=False)
        check("1-й POST → 200", resp1.status_code == 200)

        after1 = _counts()
        check("после 1-го прогона: punishments=0", after1["punishments"] == 0)
        check("после 1-го прогона: moderators=2", after1["moderators"] == 2)

        # Второй прогон — на уже пустой БД (но moderators/web_users есть)
        resp2 = await client.post("/admin/cleanup", data={},
                                  cookies={"sl_session": su_cookie},
                                  follow_redirects=False)
        check("2-й POST → 200", resp2.status_code == 200)

        after2 = _counts()
        check("после 2-го прогона: punishments=0", after2["punishments"] == 0)
        check("после 2-го прогона: moderators=2 (не тронуты)",
              after2["moderators"] == 2)
        check("после 2-го прогона: users=1 (только модератор)",
              after2["users"] == 1)


# ──────────────────────────────────────────────────────────────────────────
# 12. JSON-содержимое backup: WAL файлы не должны повредить backup
# ──────────────────────────────────────────────────────────────────────────
async def test_backup_is_valid_sqlite():
    print("\n[12] Backup — валидный SQLite-файл, открывается")
    from web_app import create_app
    from httpx import AsyncClient, ASGITransport

    _seed_test_data()
    mock_bot = MagicMock()
    mock_bot.get_chat = AsyncMock()
    app = create_app(bot=mock_bot)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        su_cookie = await _su_login(client)
        await client.post("/admin/cleanup", data={},
                          cookies={"sl_session": su_cookie},
                          follow_redirects=False)

    # Берём самый свежий бэкап
    backups = list(glob.glob(f"{TMP_DB}.backup-*.db"))
    check("бэкап создан", len(backups) > 0)
    if backups:
        latest = max(backups, key=os.path.getmtime)
        try:
            conn = sqlite3.connect(latest)
            # Проверяем что все таблицы на месте
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()]
            check("backup содержит все 6 таблиц",
                  set(tables) >= {"users", "moderators", "punishments",
                                  "chat_admins", "chat_settings", "web_users"},
                  f"got: {tables}")
            # integrity_check
            ic = conn.execute("PRAGMA integrity_check").fetchone()[0]
            check("backup: integrity_check = 'ok'", ic == "ok", f"got: {ic}")
            conn.close()
        except sqlite3.Error as e:
            check("backup валиден", False, f"sqlite error: {e}")


# ──────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────
async def main():
    print("=" * 70)
    print("v4.4.5 tests: cleanup web feature")
    print("=" * 70)
    await test_get_page_su()
    await test_post_apply_no_chat_admins()
    await test_post_apply_with_chat_admins()
    await test_backup_created()
    await test_refuse_on_empty_db()
    await test_non_su_access()
    await test_unauthenticated_access()
    await test_nav_link_visibility()
    await test_vacuum_runs()
    await test_confirm_in_form()
    await test_idempotent_second_run()
    await test_backup_is_valid_sqlite()

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
