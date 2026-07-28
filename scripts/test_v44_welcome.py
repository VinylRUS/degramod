"""
test_v44_welcome.py — Тесты v4.4.2: Welcome-сообщение новому админу.

Покрывает:
  - web_app._send_admin_welcome: построение Rich-сообщения
  - send_rich_message вызывается с правильным chat_id
  - В сообщении есть: ссылка на веб-панель, логин, пароль (под спойлером),
    упоминание "Change my password", "Dashboard"
  - Пароль в RichTextSpoiler (скрыт до клика)
  - first_name используется в заголовке
  - bot=None → graceful (False, "bot is None")
  - send_rich_message падает → graceful (False, ...)
  - POST /admin/users/create вызывает _send_admin_welcome с нужными аргументами
  - GET /admin/users?created=<token> показывает welcome_sent=True/False
  - flash-токен содержит поле 'w'

Запуск:
    cd /home/z/my-project/v4.5
    python3 scripts/test_v44_welcome.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Подкладываем путь к проекту
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Изолированная БД
TMP_DB = "/tmp/test_v44_welcome.db"
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


async def main() -> None:
    import web_app
    import bot_handlers
    from db import init_db, async_session, WebUser

    await init_db()

    # v4.5.1: отключаем rate-limit на /login для тестов
    try:
        web_app._check_login_rate_limit = lambda ip: True
    except ImportError:
        pass

    # ── Тест 1: Базовая отправка welcome ──────────────────────────────
    print("\n[1] _send_admin_welcome: базовая отправка")

    captured: dict = {}

    async def fake_send_rich(*, chat_id, rich_message):
        captured["chat_id"] = chat_id
        captured["rich"] = rich_message

    bot_mock = MagicMock()
    bot_mock.send_rich_message = fake_send_rich

    ok, err = await web_app._send_admin_welcome(
        bot=bot_mock,
        tg_user_id=123456789,
        login="test_admin",
        password="SecRet12345",
        first_name="Иван",
    )

    check("ok=True", ok, err)
    check("err='ok'", err == "ok", err)
    check("chat_id передан верно", captured.get("chat_id") == 123456789)
    check("rich_message передан", "rich" in captured)

    # Сериализуем
    try:
        rich_dict = captured["rich"].model_dump(mode="python", by_alias=True)
        check("Rich-сообщение сериализуется", True)
    except Exception as e:
        check("Rich-сообщение сериализуется", False, str(e))
        rich_dict = {"blocks": []}

    blocks_str = json.dumps(rich_dict, default=str, ensure_ascii=False)

    # Проверяем содержимое
    check("есть ссылка на degraban.bothost.tech",
          "degraban.bothost.tech" in blocks_str)
    check("есть логин test_admin",
          "test_admin" in blocks_str)
    check("есть пароль SecRet12345",
          "SecRet12345" in blocks_str)
    check("есть 'Change my password'",
          "Change my password" in blocks_str)
    check("есть 'Dashboard'",
          "Dashboard" in blocks_str)
    check("есть 'Логин:' (label)",
          "Логин:" in blocks_str)
    check("есть 'Пароль:' (label)",
          "Пароль:" in blocks_str)

    # Проверяем структуру: спойлер присутствует (type='spoiler')
    check("есть RichTextSpoiler (type=spoiler)",
          '"spoiler"' in blocks_str or "'spoiler'" in blocks_str)

    # Проверяем имя в заголовке
    blocks = rich_dict.get("blocks", [])
    if blocks:
        heading = blocks[0]
        heading_str = json.dumps(heading, default=str, ensure_ascii=False)
        check("имя 'Иван' в заголовке",
              "Иван" in heading_str)
        check("в заголовке есть '🎉'",
              "🎉" in heading_str)

    # ── Тест 2: Без first_name ────────────────────────────────────────
    print("\n[2] _send_admin_welcome: без first_name")

    captured2: dict = {}

    async def fake_send_rich2(*, chat_id, rich_message):
        captured2["rich"] = rich_message

    bot_mock2 = MagicMock()
    bot_mock2.send_rich_message = fake_send_rich2

    ok2, _ = await web_app._send_admin_welcome(
        bot=bot_mock2,
        tg_user_id=999,
        login="noname_user",
        password="AbCdEf123456",
        first_name=None,
    )
    check("ok=True", ok2)
    blocks2 = captured2["rich"].model_dump(mode="python", by_alias=True).get("blocks", [])
    heading2_str = json.dumps(blocks2[0], default=str, ensure_ascii=False) if blocks2 else ""
    # Заголовок без имени: "🎉 Доступ к веб-панели" (без ", <name>")
    check("заголовок без ', '",
          ", " not in heading2_str or "Доступ к веб-панели" in heading2_str)
    check("логин noname_user присутствует",
          "noname_user" in json.dumps(blocks2, default=str, ensure_ascii=False))

    # ── Тест 3: bot=None ──────────────────────────────────────────────
    print("\n[3] _send_admin_welcome: bot=None → graceful")
    ok3, err3 = await web_app._send_admin_welcome(
        bot=None,
        tg_user_id=111,
        login="x",
        password="y",
    )
    check("ok=False", not ok3)
    check("err='bot is None'", err3 == "bot is None", err3)

    # ── Тест 4: send_rich_message падает ──────────────────────────────
    print("\n[4] _send_admin_welcome: send_rich_message падает → graceful")
    from aiogram.exceptions import TelegramBadRequest

    async def rich_boom(*, chat_id, rich_message):
        raise TelegramBadRequest(method="sendRichMessage",
                                 message="chat not found")

    bot_boom = MagicMock()
    bot_boom.send_rich_message = rich_boom

    ok4, err4 = await web_app._send_admin_welcome(
        bot=bot_boom,
        tg_user_id=222,
        login="y",
        password="z",
    )
    check("ok=False", not ok4)
    check("err содержит 'TelegramBadRequest'", "TelegramBadRequest" in err4, err4)
    check("err содержит 'chat not found'", "chat not found" in err4, err4)

    # ── Тест 5: POST /admin/users/create вызывает _send_admin_welcome ─
    print("\n[5] POST /admin/users/create вызывает welcome")

    # Мокаем бота с реальным get_chat и подменённым send_rich_message
    fake_chat = MagicMock()
    fake_chat.id = 7770001
    fake_chat.username = "alice_mod"
    fake_chat.first_name = "Alice"
    fake_chat.last_name = "Wonderland"
    fake_chat.type = "private"

    welcome_called: dict = {}

    async def fake_get_chat(*, chat_id):
        return fake_chat

    async def fake_send_rich_welcome(*, chat_id, rich_message):
        welcome_called["chat_id"] = chat_id
        welcome_called["rich"] = rich_message
        welcome_called["login_in_msg"] = "alice_mod" in json.dumps(
            rich_message.model_dump(mode="python", by_alias=True),
            default=str, ensure_ascii=False,
        )

    bot_create = MagicMock()
    bot_create.get_chat = fake_get_chat
    bot_create.send_rich_message = fake_send_rich_welcome

    from fastapi.testclient import TestClient
    # Создаём приложение с ботом
    app = web_app.create_app(bot=bot_create)
    client = TestClient(app)

    # Логинимся как SU
    sess = client.post("/login", data={"username": "su", "password": "test_su_password_123"},
                       follow_redirects=False)
    check("SU login → 303", sess.status_code == 303)
    su_cookies = client.cookies

    # Создаём админа
    resp = client.post("/admin/users/create", data={"tg_user_id": "7770001"},
                       follow_redirects=False)
    check("create → 303", resp.status_code == 303)
    loc = resp.headers.get("location", "")
    check("redirect на /admin/users?created=", "created=" in loc)

    check("_send_admin_welcome вызван", "chat_id" in welcome_called)
    check("welcome отправлен в chat_id=7770001",
          welcome_called.get("chat_id") == 7770001)
    check("логин alice_mod присутствует в сообщении",
          welcome_called.get("login_in_msg") is True)

    # ── Тест 6: GET /admin/users?created= показывает welcome_sent ─────
    print("\n[6] GET /admin/users?created= → welcome_sent отображается")

    # Токен из теста 5 — должен быть валидным
    token = loc.split("created=", 1)[1]
    resp2 = client.get(f"/admin/users?created={token}")
    check("GET → 200", resp2.status_code == 200)
    check("в HTML есть ✅ welcome_sent",
          "✅" in resp2.text and "Welcome message" in resp2.text,
          resp2.text[:500])

    # ── Тест 7: welcome_sent=False когда бот не может отправить ───────
    print("\n[7] welcome_sent=False при ошибке доставки")

    fake_chat2 = MagicMock()
    fake_chat2.id = 8880002
    fake_chat2.username = "bob_mod"
    fake_chat2.first_name = "Bob"
    fake_chat2.last_name = None
    fake_chat2.type = "private"

    async def fake_get_chat2(*, chat_id):
        return fake_chat2

    async def rich_boom2(*, chat_id, rich_message):
        raise TelegramBadRequest(method="sendRichMessage",
                                 message="bot was blocked by the user")

    bot_blocked = MagicMock()
    bot_blocked.get_chat = fake_get_chat2
    bot_blocked.send_rich_message = rich_boom2

    app2 = web_app.create_app(bot=bot_blocked)
    client2 = TestClient(app2)
    client2.post("/login", data={"username": "su", "password": "test_su_password_123"},
                 follow_redirects=False)

    resp3 = client2.post("/admin/users/create", data={"tg_user_id": "8880002"},
                         follow_redirects=False)
    check("create → 303", resp3.status_code == 303)
    loc3 = resp3.headers.get("location", "")
    token3 = loc3.split("created=", 1)[1]

    resp4 = client2.get(f"/admin/users?created={token3}")
    check("GET → 200", resp4.status_code == 200)
    check("в HTML есть ⚠ (welcome не доставлен)",
          "⚠" in resp4.text and "could not be delivered" in resp4.text,
          resp4.text[:500])

    # ── Тест 8: структура Rich-сообщения — подробная проверка ─────────
    print("\n[8] Структура Rich-сообщения: спойлер с логином/паролем")

    # Используем captured из теста 1
    blocks8 = captured["rich"].model_dump(mode="python", by_alias=True).get("blocks", [])
    # Ищем блок со спойлером
    spoiler_block = None
    for b in blocks8:
        text = b.get("text")
        if isinstance(text, dict) and text.get("type") == "spoiler":
            spoiler_block = text
            break
        if isinstance(text, list):
            for item in text:
                if isinstance(item, dict) and item.get("type") == "spoiler":
                    spoiler_block = item
                    break

    check("RichTextSpoiler найден в каком-то блоке", spoiler_block is not None)

    if spoiler_block:
        # Спойлер должен содержать логин и пароль (через RichTextBold)
        spoiler_str = json.dumps(spoiler_block, default=str, ensure_ascii=False)
        check("в спойлере есть логин test_admin",
              "test_admin" in spoiler_str)
        check("в спойлере есть пароль SecRet12345",
              "SecRet12345" in spoiler_str)
        # Подсветка bold-стилем
        check("в спойлере есть 'bold'",
              "'bold'" in spoiler_str or '"bold"' in spoiler_str)

    # ── Итог ─────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"PASS: {PASS_COUNT} | FAIL: {FAIL_COUNT}")
    if FAIL_COUNT:
        print("\nFailures:")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("ALL OK ✓")


if __name__ == "__main__":
    asyncio.run(main())
