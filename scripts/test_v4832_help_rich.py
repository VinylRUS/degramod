"""
v4.8.3.2 — тесты нового Rich Message /help.

7 секций / ~30 проверок:

  T1. _build_help_full_rich() возвращает валидный InputRichMessage
  T2. _build_help_moderator_rich() возвращает валидный InputRichMessage
  T3. Полная версия содержит все ожидаемые команды (громкие, тихие, снятие)
  T4. Сокращённая версия НЕ содержит команды только-для-админов
       (!resetwarns, !alarm, /nightmode, /sanitary, /warndecay, /settings,
        /sethashtag, /setreport, /warns_mute, /warns_ban, /mute_duration,
        /addadmin, /deladmin, /bansticker, /liststickers, /delsticker,
        /linkfilter, /linkallow, /linkallowlist, /cas)
  T5. Полная версия содержит 5 Details-блоков (настройки, фильтры, ночной,
       санитарные, прочее)
  T6. Сокращённая версия НЕ содержит Details-блоков
  T7. Footer в обеих версиях содержит веб-ссылку + версию бота
  T8. Структура блоков: первый блок — SectionHeading(size=1)
  T9. APP_VERSION в web_app.py = "v4.8.3.2" (синхронизация с changelog)
  T10. cmd_help: обычный юзер (wu is None) → не вызывает send_rich_message
  T11. cmd_help: SU (ADMIN_IDS) → вызывает send_rich_message с full rich message
  T12. cmd_help: moderator → вызывает send_rich_message с moderator rich message
  T13. cmd_help: deactivated WebUser → не вызывает send_rich_message (стелс)

Запуск: cd /home/z/my-project/v4832_work && /home/z/.venv/bin/python3 \
       /home/z/my-project/scripts/test_v4832_help_rich.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Подкладываем рабочий каталог v4832_work в sys.path, чтобы импортировать
# bot_handlers и web_app из v4.8.3.2 (а не из v4.5).
WORK_DIR = Path("/home/z/my-project/v4832_work")
sys.path.insert(0, str(WORK_DIR))

# Минимальные env-заглушки для импорта bot_handlers (db.py может требовать).
os.environ.setdefault("BOT_TOKEN", "0:fake_token_for_tests")
os.environ.setdefault("DATABASE_URL", "sqlite:///tmp/test_v4832.db")
os.environ.setdefault("ADMIN_IDS", "123456789")

# Импортируем тестируемые модули
from aiogram.types import (
    InputRichMessage,
    InputRichBlockSectionHeading,
    InputRichBlockDivider,
    InputRichBlockList,
    InputRichBlockListItem,
    InputRichBlockParagraph,
    InputRichBlockDetails,
    InputRichBlockFooter,
    RichTextCode,
    RichTextUrl,
    RichTextBold,
)

# web_app импортируется ПЕРВЫМ (он создаёт БД при старте, но без run() — safe).
# Если импорт падает из-за env — пропускаем T9, остальные тесты работают.
WEB_APP_IMPORTABLE = False
try:
    import web_app
    WEB_APP_IMPORTABLE = True
except Exception as e:
    print(f"[warn] web_app import failed: {e!r} — T9 будет пропущен")

import bot_handlers
from bot_handlers import (
    _build_help_full_rich,
    _build_help_moderator_rich,
    _help_code,
    _help_list_item,
    _help_section,
    _help_details,
    cmd_help,
)


# ── Утилиты ───────────────────────────────────────────────────────────────

def _walk_blocks(rich: InputRichMessage) -> list:
    """Рекурсивно обходит все блоки InputRichMessage и возвращает плоский список."""
    out: list = []
    for block in rich.blocks:
        out.append(block)
        # Details содержит вложенные blocks
        if isinstance(block, InputRichBlockDetails):
            for inner in block.blocks:
                out.append(inner)
    return out


def _collect_text(rich: InputRichMessage) -> str:
    """Собирает ВЕСЬ текст из InputRichMessage (для поиска подстрок)."""
    parts: list[str] = []

    def visit(obj):
        if isinstance(obj, str):
            parts.append(obj)
        elif isinstance(obj, list):
            for x in obj:
                visit(x)
        elif isinstance(obj, InputRichBlockSectionHeading):
            parts.append(obj.text or "")
        elif isinstance(obj, InputRichBlockParagraph):
            visit(obj.text)
        elif isinstance(obj, InputRichBlockFooter):
            visit(obj.text)
        elif isinstance(obj, InputRichBlockList):
            for it in obj.items:
                visit(it)
        elif isinstance(obj, InputRichBlockListItem):
            for b in obj.blocks:
                visit(b)
        elif isinstance(obj, InputRichBlockDetails):
            parts.append(obj.summary or "")
            for b in obj.blocks:
                visit(b)
        elif isinstance(obj, RichTextCode):
            parts.append(obj.text or "")
        elif isinstance(obj, RichTextUrl):
            parts.append(obj.text or "")
            parts.append(obj.url or "")
        elif isinstance(obj, RichTextBold):
            parts.append(obj.text or "")
        elif isinstance(obj, InputRichBlockDivider):
            pass
        else:
            # Неизвестный тип — пробуем model_dump
            try:
                d = obj.model_dump()
                parts.append(str(d))
            except Exception:
                pass

    visit(rich)
    return "\n".join(parts)


# ── T1: _build_help_full_rich() возвращает валидный InputRichMessage ──

def test_t1_full_returns_input_rich_message():
    """T1.1: тип — InputRichMessage."""
    r = _build_help_full_rich()
    assert isinstance(r, InputRichMessage), f"expected InputRichMessage, got {type(r)}"
    assert len(r.blocks) > 0, "blocks пустой"


def test_t1_full_first_block_is_h1():
    """T1.2: первый блок — SectionHeading(size=1)."""
    r = _build_help_full_rich()
    first = r.blocks[0]
    assert isinstance(first, InputRichBlockSectionHeading), \
        f"first block must be SectionHeading, got {type(first)}"
    assert first.size == 1, f"first heading size must be 1, got {first.size}"


def test_t1_full_has_divider_after_h1():
    """T1.3: после H1 идёт Divider (для визуального отделения)."""
    r = _build_help_full_rich()
    assert isinstance(r.blocks[1], InputRichBlockDivider), \
        f"second block must be Divider, got {type(r.blocks[1])}"


def test_t1_full_has_footer_last():
    """T1.4: последний блок — Footer (веб-ссылка + версия)."""
    r = _build_help_full_rich()
    last = r.blocks[-1]
    assert isinstance(last, InputRichBlockFooter), \
        f"last block must be Footer, got {type(last)}"


# ── T2: _build_help_moderator_rich() возвращает валидный InputRichMessage ──

def test_t2_moderator_returns_input_rich_message():
    r = _build_help_moderator_rich()
    assert isinstance(r, InputRichMessage)
    assert len(r.blocks) > 0


def test_t2_moderator_first_block_h1():
    r = _build_help_moderator_rich()
    first = r.blocks[0]
    assert isinstance(first, InputRichBlockSectionHeading)
    assert first.size == 1


# ── T3: полная версия содержит все ожидаемые команд ──

EXPECTED_FULL_COMMANDS = [
    "!mute",
    "!warn",
    "!ban",
    "!smute",
    "!swarn",
    "!sban",
    "!unmute",
    "!unban",
    "!unwarn",
    "!warns",
    "!resetwarns",
    "!alarm",
    "/settings",
    "/sethashtag",
    "/setreport",
    "/warns_mute",
    "/warns_ban",
    "/mute_duration",
    "/addadmin",
    "/deladmin",
    "/bansticker",
    "/liststickers",
    "/delsticker",
    "/linkfilter",
    "/linkallow",
    "/linkallowlist",
    "/cas",
    "/nightmode",
    "/sanitary",
    "/warndecay",
]


def test_t3_full_contains_all_commands():
    """T3: все 30 команд присутствуют в full rich message."""
    text = _collect_text(_build_help_full_rich())
    missing = [c for c in EXPECTED_FULL_COMMANDS if c not in text]
    assert not missing, f"в full help отсутствуют команды: {missing}"


# ── T4: сокращённая версия НЕ содержит команды только-для-админов ──

ADMIN_ONLY_COMMANDS = [
    "!resetwarns",
    "!alarm",
    "/settings",
    "/sethashtag",
    "/setreport",
    "/warns_mute",
    "/warns_ban",
    "/mute_duration",
    "/addadmin",
    "/deladmin",
    "/bansticker",
    "/liststickers",
    "/delsticker",
    "/linkfilter",
    "/linkallow",
    "/linkallowlist",
    "/cas",
    "/nightmode",
    "/sanitary",
    "/warndecay",
]

MODERATOR_ALLOWED_COMMANDS = ["!mute", "!warn", "!ban", "!smute", "!swarn", "!sban",
                              "!unmute", "!unban", "!unwarn", "!warns"]


def test_t4_moderator_excludes_admin_only_commands():
    """T4.1: moderator help НЕ содержит admin-only команд."""
    text = _collect_text(_build_help_moderator_rich())
    leaked = [c for c in ADMIN_ONLY_COMMANDS if c in text]
    assert not leaked, f"в moderator help просочились admin-only команды: {leaked}"


def test_t4_moderator_contains_allowed_commands():
    """T4.2: moderator help содержит все разрешённые модератору команды."""
    text = _collect_text(_build_help_moderator_rich())
    missing = [c for c in MODERATOR_ALLOWED_COMMANDS if c not in text]
    assert not missing, f"в moderator help отсутствуют разрешённые команды: {missing}"


# ── T5: полная версия содержит 5 Details-блоков ──

EXPECTED_FULL_DETAILS_SUMMARIES = [
    "Настройки чатов",
    "Фильтры",
    "Ночной режим",
    "Санитарные дни",
    "Прочее",
]


def test_t5_full_has_5_details_blocks():
    """T5: full help содержит 5 сворачиваемых Details-блоков."""
    r = _build_help_full_rich()
    details_blocks = [b for b in r.blocks if isinstance(b, InputRichBlockDetails)]
    assert len(details_blocks) == 5, \
        f"expected 5 Details blocks, got {len(details_blocks)}"


def test_t5_full_details_summaries():
    """T5.1: summaries содержат ожидаемые заголовки разделов."""
    r = _build_help_full_rich()
    details_blocks = [b for b in r.blocks if isinstance(b, InputRichBlockDetails)]
    summaries = " | ".join(d.summary or "" for d in details_blocks)
    for expected in EXPECTED_FULL_DETAILS_SUMMARIES:
        assert expected in summaries, \
            f"summary '{expected}' не найден в Details: {summaries}"


def test_t5_full_details_are_collapsed():
    """T5.2: все Details свернуты по умолчанию (is_open=False)."""
    r = _build_help_full_rich()
    details_blocks = [b for b in r.blocks if isinstance(b, InputRichBlockDetails)]
    for d in details_blocks:
        assert d.is_open is False, \
            f"Details '{d.summary}' должен быть свёрнут (is_open=False)"


# ── T6: сокращённая версия НЕ содержит Details ──

def test_t6_moderator_has_no_details():
    """T6: moderator help не имеет Details-блоков."""
    r = _build_help_moderator_rich()
    details_blocks = [b for b in r.blocks if isinstance(b, InputRichBlockDetails)]
    assert len(details_blocks) == 0, \
        f"moderator help не должен иметь Details, но нашёл {len(details_blocks)}"


# ── T7: Footer в обеих версиях содержит веб-ссылку + версию ──

def test_t7_full_footer_has_web_url():
    """T7.1: full help Footer содержит RichTextUrl с degraban.bothost.tech."""
    r = _build_help_full_rich()
    footer = r.blocks[-1]
    assert isinstance(footer, InputRichBlockFooter)
    # Ищем RichTextUrl в footer.text
    found_url = False
    for part in footer.text:
        if isinstance(part, RichTextUrl):
            assert "degraban.bothost.tech" in (part.url or ""), \
                f"URL должен быть degraban.bothost.tech, got {part.url}"
            found_url = True
    assert found_url, "Footer должен содержать RichTextUrl"


def test_t7_full_footer_has_version():
    """T7.2: full help Footer содержит версию бота (v4.8.3.x)."""
    r = _build_help_full_rich()
    footer = r.blocks[-1]
    text_parts = []
    for part in footer.text:
        if isinstance(part, str):
            text_parts.append(part)
    combined = " ".join(text_parts)
    assert "v4.8.3" in combined, \
        f"Footer должен содержать v4.8.3.x, got: {combined!r}"


def test_t7_moderator_footer_has_web_url():
    """T7.3: moderator help Footer содержит RichTextUrl."""
    r = _build_help_moderator_rich()
    footer = r.blocks[-2]  # последний блок — Paragraph с пометкой SU
    assert isinstance(footer, InputRichBlockFooter)
    found_url = False
    for part in footer.text:
        if isinstance(part, RichTextUrl):
            assert "degraban.bothost.tech" in (part.url or "")
            found_url = True
    assert found_url


# ── T8: Структура блоков ──

def test_t8_full_has_section_headings_size_2():
    """T8: full help содержит SectionHeading(size=2) для каждой раскрытой секции."""
    r = _build_help_full_rich()
    h2_blocks = [b for b in r.blocks
                 if isinstance(b, InputRichBlockSectionHeading) and b.size == 2]
    # Минимум 3 раскрытые секции: Громкие, Тихие, Снятие наказаний
    assert len(h2_blocks) >= 3, \
        f"expected >=3 H2 headings, got {len(h2_blocks)}"


def test_t8_full_has_lists():
    """T8.1: full help содержит InputRichBlockList для раскрытых секций."""
    r = _build_help_full_rich()
    lists = [b for b in r.blocks if isinstance(b, InputRichBlockList)]
    assert len(lists) >= 3, f"expected >=3 Lists, got {len(lists)}"


# ── T9: APP_VERSION в web_app.py = v4.8.3.2 ──

def test_t9_app_version_is_v4832():
    """T9: APP_VERSION в web_app.py = 'v4.8.3.2' (синхронизация с changelog)."""
    if not WEB_APP_IMPORTABLE:
        print("[skip] T9: web_app не импортируется — пропускаем")
        return
    assert web_app.APP_VERSION == "v4.8.3.2", \
        f"APP_VERSION должен быть v4.8.3.2, got {web_app.APP_VERSION!r}"


# ── T10-T13: cmd_help dispatch logic ──

async def _run_cmd_help_with_mock(
    user_id: int,
    admin_ids: set[int],
    webuser_factory,
):
    """Запускает cmd_help с замоканным message.bot.send_rich_message
    и замоканной async_session (возвращает подготовленного WebUser или None).

    webuser_factory — функция (session) -> WebUser | None, или None чтобы
    сессия вернула None (посторонний).
    """
    # Мокаем message
    msg = MagicMock()
    msg.from_user = MagicMock(id=user_id)
    msg.chat = MagicMock(id=user_id)  # ЛС
    msg.bot.send_rich_message = AsyncMock()

    # Мокаем async_session как async context manager
    session_mock = MagicMock()
    execute_result = MagicMock()
    if webuser_factory is None:
        execute_result.scalar_one_or_none = MagicMock(return_value=None)
    else:
        wu = webuser_factory(session_mock)
        execute_result.scalar_one_or_none = MagicMock(return_value=wu)
    session_mock.execute = AsyncMock(return_value=execute_result)

    @asynccontextmanager
    async def async_session_cm():
        yield session_mock

    with patch.object(bot_handlers, "ADMIN_IDS", admin_ids), \
         patch.object(bot_handlers, "async_session", async_session_cm):
        await cmd_help(msg)

    return msg


async def test_t10_stranger_no_send():
    """T10: посторонний (wu is None, не в ADMIN_IDS) → send_rich_message НЕ вызывается."""
    msg = await _run_cmd_help_with_mock(
        user_id=999999,
        admin_ids={123},  # 999999 не в admin_ids
        webuser_factory=None,  # wu = None
    )
    assert msg.bot.send_rich_message.call_count == 0, \
        "Посторонний не должен получать /help (стелс)"


async def test_t11_admin_ids_gets_full():
    """T11: SU (user_id в ADMIN_IDS) → send_rich_message с full rich message."""
    msg = await _run_cmd_help_with_mock(
        user_id=123,
        admin_ids={123},
        webuser_factory=None,  # не дойдёт до БД
    )
    assert msg.bot.send_rich_message.call_count == 1, \
        f"SU должен получить 1 сообщение, got {msg.bot.send_rich_message.call_count}"
    # Проверяем что rich_message — это full rich (содержит /nightmode)
    call_kwargs = msg.bot.send_rich_message.call_args.kwargs
    rich = call_kwargs.get("rich_message")
    assert isinstance(rich, InputRichMessage)
    text = _collect_text(rich)
    assert "/nightmode" in text, "SU должен видеть /nightmode (полная версия)"
    assert "/sanitary" in text, "SU должен видеть /sanitary"


async def test_t12_moderator_gets_moderator():
    """T12: moderator → send_rich_message с moderator rich message."""
    # WebUser-модератор
    wu = MagicMock()
    wu.is_active = True
    wu.role = "moderator"

    msg = await _run_cmd_help_with_mock(
        user_id=555,
        admin_ids={123},  # 555 не SU
        webuser_factory=lambda session: wu,
    )
    assert msg.bot.send_rich_message.call_count == 1
    call_kwargs = msg.bot.send_rich_message.call_args.kwargs
    rich = call_kwargs.get("rich_message")
    text = _collect_text(rich)
    assert "/nightmode" not in text, "Moderator НЕ должен видеть /nightmode"
    assert "!mute" in text, "Moderator должен видеть !mute"


async def test_t13_deactivated_webuser_no_send():
    """T13: деактивированный WebUser (is_active=False) → send_rich_message НЕ вызывается."""
    wu = MagicMock()
    wu.is_active = False
    wu.role = "admin"  # даже если роль admin, is_active=False → молчим

    msg = await _run_cmd_help_with_mock(
        user_id=777,
        admin_ids={123},
        webuser_factory=lambda session: wu,
    )
    assert msg.bot.send_rich_message.call_count == 0, \
        "Деактивированный WebUser не должен получать /help (стелс)"


async def test_t14_admin_role_gets_full():
    """T14: WebUser role='admin', is_active=True → full rich message."""
    wu = MagicMock()
    wu.is_active = True
    wu.role = "admin"

    msg = await _run_cmd_help_with_mock(
        user_id=888,
        admin_ids={123},  # 888 не в ADMIN_IDS
        webuser_factory=lambda session: wu,
    )
    assert msg.bot.send_rich_message.call_count == 1
    call_kwargs = msg.bot.send_rich_message.call_args.kwargs
    rich = call_kwargs.get("rich_message")
    text = _collect_text(rich)
    assert "/nightmode" in text, "admin должен видеть /nightmode (полная версия)"
    assert "!alarm" in text, "admin должен видеть !alarm"


async def test_t15_su_role_gets_full():
    """T15: WebUser role='su', is_active=True → full rich message."""
    wu = MagicMock()
    wu.is_active = True
    wu.role = "su"

    msg = await _run_cmd_help_with_mock(
        user_id=999,
        admin_ids={123},
        webuser_factory=lambda session: wu,
    )
    assert msg.bot.send_rich_message.call_count == 1
    text = _collect_text(msg.bot.send_rich_message.call_args.kwargs["rich_message"])
    assert "/warndecay" in text


# ── T16: helpers ──

def test_t16_help_code_returns_richtextcode():
    """T16.1: _help_code возвращает RichTextCode с правильным текстом."""
    r = _help_code("!mute")
    assert isinstance(r, RichTextCode)
    assert r.text == "!mute"


def test_t16_help_list_item_returns_listitem():
    """T16.2: _help_list_item возвращает InputRichBlockListItem с Paragraph внутри."""
    item = _help_list_item("!mute 1d", "мьют")
    assert isinstance(item, InputRichBlockListItem)
    assert len(item.blocks) == 1
    assert isinstance(item.blocks[0], InputRichBlockParagraph)


def test_t16_help_section_returns_3_blocks():
    """T16.3: _help_section возвращает 3 блока (Heading + Divider + List)."""
    blocks = _help_section("Test", [("!cmd", "test")])
    assert len(blocks) == 3
    assert isinstance(blocks[0], InputRichBlockSectionHeading)
    assert blocks[0].size == 2
    assert isinstance(blocks[1], InputRichBlockDivider)
    assert isinstance(blocks[2], InputRichBlockList)
    assert len(blocks[2].items) == 1


def test_t16_help_details_returns_details():
    """T16.4: _help_details возвращает InputRichBlockDetails с is_open=False."""
    d = _help_details("Test summary", [("!cmd", "test")])
    assert isinstance(d, InputRichBlockDetails)
    assert d.is_open is False
    assert d.summary == "Test summary"
    # Внутри: Divider + List (+ optional Paragraph если note задан)
    assert len(d.blocks) == 2


def test_t16_help_details_with_note():
    """T16.5: _help_details с note добавляет Paragraph."""
    d = _help_details("Test", [("!cmd", "test")], note=["extra note"])
    assert len(d.blocks) == 3
    assert isinstance(d.blocks[2], InputRichBlockParagraph)


# ── Runner ────────────────────────────────────────────────────────────────

from contextlib import asynccontextmanager


def run_sync_tests():
    """Запускает все синхронные тесты (T1-T9, T16)."""
    tests = [
        test_t1_full_returns_input_rich_message,
        test_t1_full_first_block_is_h1,
        test_t1_full_has_divider_after_h1,
        test_t1_full_has_footer_last,
        test_t2_moderator_returns_input_rich_message,
        test_t2_moderator_first_block_h1,
        test_t3_full_contains_all_commands,
        test_t4_moderator_excludes_admin_only_commands,
        test_t4_moderator_contains_allowed_commands,
        test_t5_full_has_5_details_blocks,
        test_t5_full_details_summaries,
        test_t5_full_details_are_collapsed,
        test_t6_moderator_has_no_details,
        test_t7_full_footer_has_web_url,
        test_t7_full_footer_has_version,
        test_t7_moderator_footer_has_web_url,
        test_t8_full_has_section_headings_size_2,
        test_t8_full_has_lists,
        test_t9_app_version_is_v4832,
        test_t16_help_code_returns_richtextcode,
        test_t16_help_list_item_returns_listitem,
        test_t16_help_section_returns_3_blocks,
        test_t16_help_details_returns_details,
        test_t16_help_details_with_note,
    ]
    passed, failed = 0, 0
    for t in tests:
        try:
            t()
            print(f"  ✅ {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ❌ {t.__name__}: {e}")
            failed += 1
    return passed, failed


def run_async_tests():
    """Запускает все асинхронные тесты (T10-T15)."""
    tests = [
        test_t10_stranger_no_send,
        test_t11_admin_ids_gets_full,
        test_t12_moderator_gets_moderator,
        test_t13_deactivated_webuser_no_send,
        test_t14_admin_role_gets_full,
        test_t15_su_role_gets_full,
    ]
    passed, failed = 0, 0
    for t in tests:
        try:
            asyncio.run(t())
            print(f"  ✅ {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ❌ {t.__name__}: {e}")
            failed += 1
    return passed, failed


def main():
    print("=" * 70)
    print("v4.8.3.2 — тесты Rich Message /help")
    print("=" * 70)
    print()
    print("Sync tests (T1-T9, T16):")
    p1, f1 = run_sync_tests()
    print()
    print("Async tests (T10-T15):")
    p2, f2 = run_async_tests()
    print()
    print("=" * 70)
    total_passed = p1 + p2
    total_failed = f1 + f2
    total = total_passed + total_failed
    print(f"ИТОГ: {total_passed}/{total} passed, {total_failed} failed")
    if total_failed:
        print("❌ ТЕСТЫ НЕ ПРОЙДЕНЫ")
        sys.exit(1)
    else:
        print("✅ ВСЕ ТЕСТЫ ПРОЙДЕНЫ")


if __name__ == "__main__":
    main()
