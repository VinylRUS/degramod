"""
test_v44_rich_report.py — Smoke-тест нового _send_report с RichTextUrl.

Проверяет:
  1. _send_report строит Rich-сообщение с RichTextUrl для нарушителя
  2. Блок "Модератор" добавляется если передан mod
  3. Блок "Веб-профиль" добавляется если задан WEB_PUBLIC_URL
  4. Plain-text fallback содержит те же данные в текстовом виде
  5. Если WEB_PUBLIC_URL пустой — веб-блока нет
  6. Если mod is None — блока модератора нет
  7. JSON-сериализация Rich-сообщения не падает (модель корректна)

Запуск:
    cd /home/z/my-project/v4.4
    python3 scripts/test_v44_rich_report.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Подкладываем путь к проекту
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Изолированная БД
TMP_DB = "/tmp/test_v44_rich_report.db"
if os.path.exists(TMP_DB):
    os.remove(TMP_DB)
os.environ["DB_PATH"] = TMP_DB
os.environ["WEB_PASSWORD"] = "test_su_password_123"
os.environ["SESSION_SECRET"] = "test_session_secret"
# WEB_PUBLIC_URL установим ниже через monkeypatch

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


def make_user(uid: int, first: str = "", last: str = "",
              username: str | None = None) -> MagicMock:
    """Мок aiogram User."""
    u = MagicMock()
    u.id = uid
    u.first_name = first or None
    u.last_name = last or None
    u.username = username
    return u


def make_message(text: str = "", caption: str = "") -> MagicMock:
    """Мок reply_to_message."""
    m = MagicMock()
    m.text = text or None
    m.caption = caption or None
    m.sticker = None
    m.photo = None
    m.video = None
    m.animation = None
    m.audio = None
    m.voice = None
    m.video_note = None
    m.document = None
    return m


async def main() -> None:
    # Импортируем модуль (читает env в момент импорта)
    import bot_handlers
    from db import init_db

    await init_db()

    # ── Тест 1: Rich-сообщение собирается и содержит RichTextUrl ────
    print("\n[1] Сборка Rich-сообщения с кликабельным именем нарушителя")

    target = make_user(123456789, "Иван", "Петров", "ivan_p")
    mod = make_user(987654321, "Админ", "Ботович", "admin_bot")

    with patch.object(bot_handlers, "WEB_PUBLIC_URL", "https://shadow-logs.example.com"):
        with patch.object(bot_handlers, "_get_report_chat_id",
                          AsyncMock(return_value=-1001234567890)):
            with patch.object(bot_handlers, "_get_chat_settings",
                              AsyncMock(return_value=MagicMock(hashtag="#test"))):
                with patch.object(bot_handlers, "_count_warns",
                                  AsyncMock(return_value=3)):
                    captured: dict = {}

                    async def fake_send_rich(*, chat_id, rich_message):
                        captured["chat_id"] = chat_id
                        captured["rich"] = rich_message

                    bot_mock = MagicMock()
                    bot_mock.send_rich_message = fake_send_rich

                    await bot_handlers._send_report(
                        bot=bot_mock,
                        chat_id=-1001234567890,
                        target=target,
                        action_type="mute",
                        reason="спам",
                        mod=mod,
                        duration_seconds=3600,
                        reply_to_message=make_message("Купить крипту!"),
                    )

    check("send_rich_message был вызван", "rich" in captured)
    check("chat_id передан верно",
          captured.get("chat_id") == -1001234567890)

    # Сериализуем модель в JSON (если упадёт — модель некорректна)
    try:
        rich_json = captured["rich"].model_dump_json()
        check("Rich-сообщение сериализуется в JSON", True)
    except Exception as e:
        check("Rich-сообщение сериализуется в JSON", False, str(e))
        rich_json = "{}"

    # Проверяем структуру через model_dump (nested dict)
    rich_dict = captured["rich"].model_dump(mode="python", by_alias=True)
    blocks = rich_dict.get("blocks", [])
    check("есть хотя бы 5 блоков (заголовок/нарушитель/модератор/веб/причина)",
          len(blocks) >= 5, f"blocks={len(blocks)}")

    # Блок 2 — нарушитель (Paragraph с RichTextUrl)
    if len(blocks) >= 2:
        para = blocks[1]
        text_field = para.get("text")
        # text может быть list или dict — сериализуем с ensure_ascii=False,
        # чтобы кириллица не уходила в \uXXXX
        para_str = json.dumps(para, default=str, ensure_ascii=False)
        check("блок нарушителя — paragraph",
              para.get("type") == "paragraph" or "paragraph" in str(para.get("type", "")))
        check("в блоке нарушителя есть tg://user?id=123456789",
              "tg://user?id=123456789" in para_str, para_str[:300])
        check("в блоке нарушителя есть имя Иван Петров",
              "Иван Петров" in para_str)
        check("в блоке нарушителя есть @ivan_p",
              "@ivan_p" in para_str)

    # Блок 3 — модератор
    if len(blocks) >= 3:
        mod_para = blocks[2]
        mod_str = json.dumps(mod_para, default=str, ensure_ascii=False)
        check("в блоке модератора есть tg://user?id=987654321",
              "tg://user?id=987654321" in mod_str)
        check("в блоке модератора есть 'Модератор'",
              "Модератор" in mod_str)
        check("в блоке модератора есть имя 'Админ Ботович'",
              "Админ Ботович" in mod_str)

    # Блок 4 — веб-профиль
    if len(blocks) >= 4:
        web_para = blocks[3]
        web_str = json.dumps(web_para, default=str, ensure_ascii=False)
        check("в блоке веб-профиля есть WEB_PUBLIC_URL",
              "shadow-logs.example.com/user/123456789" in web_str)

    # ── Тест 2: WEB_PUBLIC_URL пустой — веб-блока нет ────────────────
    print("\n[2] WEB_PUBLIC_URL пустой → веб-блок отсутствует")

    with patch.object(bot_handlers, "WEB_PUBLIC_URL", ""):
        with patch.object(bot_handlers, "_get_report_chat_id",
                          AsyncMock(return_value=-1001234567890)):
            with patch.object(bot_handlers, "_get_chat_settings",
                              AsyncMock(return_value=MagicMock(hashtag=None))):
                with patch.object(bot_handlers, "_count_warns",
                                  AsyncMock(return_value=0)):
                    captured2: dict = {}

                    async def fake_send_rich2(*, chat_id, rich_message):
                        captured2["rich"] = rich_message

                    bot_mock2 = MagicMock()
                    bot_mock2.send_rich_message = fake_send_rich2

                    await bot_handlers._send_report(
                        bot=bot_mock2,
                        chat_id=-1001234567890,
                        target=target,
                        action_type="ban",
                        reason="нарушение",
                        mod=mod,
                    )

    blocks2 = captured2["rich"].model_dump(mode="python", by_alias=True).get("blocks", [])
    blocks2_str = json.dumps(blocks2, default=str, ensure_ascii=False)
    check("веб-блок отсутствует (нет shadow-logs)",
          "shadow-logs" not in blocks2_str, f"blocks={len(blocks2)}")
    check("но модератор присутствует",
          "tg://user?id=987654321" in blocks2_str)

    # ── Тест 3: mod is None — блока модератора нет ───────────────────
    print("\n[3] mod is None → блока модератора нет")

    with patch.object(bot_handlers, "WEB_PUBLIC_URL", "https://shadow-logs.example.com"):
        with patch.object(bot_handlers, "_get_report_chat_id",
                          AsyncMock(return_value=-1001234567890)):
            with patch.object(bot_handlers, "_get_chat_settings",
                              AsyncMock(return_value=MagicMock(hashtag=None))):
                with patch.object(bot_handlers, "_count_warns",
                                  AsyncMock(return_value=0)):
                    captured3: dict = {}

                    async def fake_send_rich3(*, chat_id, rich_message):
                        captured3["rich"] = rich_message

                    bot_mock3 = MagicMock()
                    bot_mock3.send_rich_message = fake_send_rich3

                    await bot_handlers._send_report(
                        bot=bot_mock3,
                        chat_id=-1001234567890,
                        target=target,
                        action_type="ban",
                        reason="нарушение",
                        mod=None,
                    )

    blocks3 = captured3["rich"].model_dump(mode="python", by_alias=True).get("blocks", [])
    blocks3_str = json.dumps(blocks3, default=str, ensure_ascii=False)
    check("модератор-блок отсутствует (нет 'Модератор')",
          "Модератор" not in blocks3_str)
    check("нарушитель всё равно есть",
          "tg://user?id=123456789" in blocks3_str)

    # ── Тест 4: нарушитель без @username — кликабельное имя работает ─
    print("\n[4] Нарушитель без @username — кликабельное имя работает")

    target_no_un = make_user(555666777, "Безымянный", "")
    with patch.object(bot_handlers, "WEB_PUBLIC_URL", ""):
        with patch.object(bot_handlers, "_get_report_chat_id",
                          AsyncMock(return_value=-1001234567890)):
            with patch.object(bot_handlers, "_get_chat_settings",
                              AsyncMock(return_value=MagicMock(hashtag=None))):
                with patch.object(bot_handlers, "_count_warns",
                                  AsyncMock(return_value=0)):
                    captured4: dict = {}

                    async def fake_send_rich4(*, chat_id, rich_message):
                        captured4["rich"] = rich_message

                    bot_mock4 = MagicMock()
                    bot_mock4.send_rich_message = fake_send_rich4

                    await bot_handlers._send_report(
                        bot=bot_mock4,
                        chat_id=-1001234567890,
                        target=target_no_un,
                        action_type="warn",
                        reason="флуд",
                        mod=mod,
                    )

    blocks4 = captured4["rich"].model_dump(mode="python", by_alias=True).get("blocks", [])
    para4_str = json.dumps(blocks4[1], default=str, ensure_ascii=False) if len(blocks4) >= 2 else ""
    check("кликабельная ссылка tg://user?id=555666777 есть",
          "tg://user?id=555666777" in para4_str)
    check("имя 'Безымянный' присутствует",
          "Безымянный" in para4_str)
    # username=None — значит @-строки быть не должно (но @ может встречаться
    # в JSON-структуре типа "type":"url" — это норма, проверим только в text-части)
    check("@username-строки НЕТ (username=None)",
          "@" not in para4_str or "@" not in para4_str.replace('"type": "url"', ''))

    # ── Тест 5: нарушитель без имени совсем — fallback '(без имени)' ─
    print("\n[5] Нарушитель без имени — '(без имени)' в кликабельном тексте")

    target_no_name = make_user(999888777, "", "", None)
    with patch.object(bot_handlers, "WEB_PUBLIC_URL", ""):
        with patch.object(bot_handlers, "_get_report_chat_id",
                          AsyncMock(return_value=-1001234567890)):
            with patch.object(bot_handlers, "_get_chat_settings",
                              AsyncMock(return_value=MagicMock(hashtag=None))):
                with patch.object(bot_handlers, "_count_warns",
                                  AsyncMock(return_value=0)):
                    captured5: dict = {}

                    async def fake_send_rich5(*, chat_id, rich_message):
                        captured5["rich"] = rich_message

                    bot_mock5 = MagicMock()
                    bot_mock5.send_rich_message = fake_send_rich5

                    await bot_handlers._send_report(
                        bot=bot_mock5,
                        chat_id=-1001234567890,
                        target=target_no_name,
                        action_type="ban",
                        reason="троллинг",
                        mod=mod,
                    )

    blocks5 = captured5["rich"].model_dump(mode="python", by_alias=True).get("blocks", [])
    para5_str = json.dumps(blocks5[1], default=str, ensure_ascii=False) if len(blocks5) >= 2 else ""
    check("есть '(без имени)'",
          "(без имени)" in para5_str)
    check("кликабельная ссылка tg://user?id=999888777 есть",
          "tg://user?id=999888777" in para5_str)

    # ── Тест 6: Plain-text fallback вызывается при ошибке Rich ───────
    print("\n[6] Plain-text fallback: содержит mod, web_url, нарушителя")

    with patch.object(bot_handlers, "WEB_PUBLIC_URL", "https://shadow-logs.example.com"):
        with patch.object(bot_handlers, "_get_report_chat_id",
                          AsyncMock(return_value=-1001234567890)):
            with patch.object(bot_handlers, "_get_chat_settings",
                              AsyncMock(return_value=MagicMock(hashtag=None))):
                with patch.object(bot_handlers, "_count_warns",
                                  AsyncMock(return_value=0)):
                    from aiogram.exceptions import TelegramBadRequest

                    async def rich_boom(*, chat_id, rich_message):
                        raise TelegramBadRequest(method="sendRichMessage",
                                                 message="rich not supported")

                    sent_plain: dict = {}

                    async def send_message(*, chat_id, text):
                        sent_plain["chat_id"] = chat_id
                        sent_plain["text"] = text

                    bot_fb = MagicMock()
                    bot_fb.send_rich_message = rich_boom
                    bot_fb.send_message = send_message

                    await bot_handlers._send_report(
                        bot=bot_fb,
                        chat_id=-1001234567890,
                        target=target,
                        action_type="ban",
                        reason="спам",
                        mod=mod,
                    )

    check("plain-text fallback вызван", "text" in sent_plain)
    if "text" in sent_plain:
        t = sent_plain["text"]
        check("в plain есть имя нарушителя Иван Петров", "Иван Петров" in t)
        check("в plain есть @ivan_p", "@ivan_p" in t)
        check("в plain есть ID нарушителя", "ID: 123456789" in t)
        check("в plain есть модератор", "Модератор" in t and "Админ Ботович" in t)
        check("в plain есть веб-ссылка",
              "shadow-logs.example.com/user/123456789" in t)

    # ── Тест 7: дефолт WEB_PUBLIC_URL = degraban.bothost.tech ─────────
    # Проверяем, что без env и без патча в Rich-сообщении используется
    # production-URL по умолчанию.
    print("\n[7] WEB_PUBLIC_URL по умолчанию = https://degraban.bothost.tech")

    # Сохраняем оригинальный env, чистим
    saved_env = os.environ.pop("WEB_PUBLIC_URL", None)
    try:
        # Перечитываем значение из свежего импорта (не пересоздаём модуль,
        # просто проверяем текущее значение bot_handlers.WEB_PUBLIC_URL —
        # оно было зафиксировано при импорте модуля).
        # Если env был пустым при импорте — должен был подставиться дефолт.
        # Т.к. тесты запускаются с чистым env (кроме DB_PATH/WEB_PASSWORD/
        # SESSION_SECRET), ожидаем дефолт.
        current_default = bot_handlers.WEB_PUBLIC_URL
        check("bot_handlers.WEB_PUBLIC_URL = 'https://degraban.bothost.tech'",
              current_default == "https://degraban.bothost.tech",
              f"got: {current_default!r}")

        # Отправим отчёт БЕЗ патча WEB_PUBLIC_URL — должен использоваться дефолт
        with patch.object(bot_handlers, "_get_report_chat_id",
                          AsyncMock(return_value=-1001234567890)):
            with patch.object(bot_handlers, "_get_chat_settings",
                              AsyncMock(return_value=MagicMock(hashtag=None))):
                with patch.object(bot_handlers, "_count_warns",
                                  AsyncMock(return_value=0)):
                    captured7: dict = {}

                    async def fake_send_rich7(*, chat_id, rich_message):
                        captured7["rich"] = rich_message

                    bot_mock7 = MagicMock()
                    bot_mock7.send_rich_message = fake_send_rich7

                    await bot_handlers._send_report(
                        bot=bot_mock7,
                        chat_id=-1001234567890,
                        target=target,
                        action_type="warn",
                        reason="тест дефолта",
                        mod=mod,
                    )

        blocks7 = captured7["rich"].model_dump(mode="python", by_alias=True).get("blocks", [])
        blocks7_str = json.dumps(blocks7, default=str, ensure_ascii=False)
        check("в Rich-сообщении используется дефолт-URL degraban.bothost.tech",
              "degraban.bothost.tech/user/123456789" in blocks7_str,
              blocks7_str[:300])
    finally:
        # Возвращаем env как было (если был)
        if saved_env is not None:
            os.environ["WEB_PUBLIC_URL"] = saved_env

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
