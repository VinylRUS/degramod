"""
test_v4410_report_redesign.py — Тесты v4.4.10: редизайн Rich-отчёта в репорт-чате.

Проверяем (Вариант B — «Список с разделителями»):
  1. Структура блоков: [section_heading, divider, list, divider,
     (details media), divider, details доп.инфо, divider, footer].
  2. В List ровно 3 ListItem'а (нарушитель/причина/веб-профиль) если есть
     причина и WEB_PUBLIC_URL.
  3. ListItem нарушителя содержит RichTextUrl (кликабельное имя) и
     RichTextCode (моноширинный ID).
  4. ListItem веб-профиля содержит RichTextUrl с коротким текстом
     «Открыть профиль →» (НЕ полный URL).
  5. Details «📎 Показать медиа» имеет is_open=False (свёрнут по умолчанию).
  6. Внутри Details есть media_block (если было медиа) и BlockQuotation
     (если был text_content).
  7. Footer содержит RichTextUrl с tg://user?id=<mod_id> (кликабельное имя).
  8. Footer НЕ содержит приписку «Модератор:».
  9. Если mod=None — Footer без RichTextUrl (просто время + хэштег).
  10. Если reply_to_message=None — Details «📎 Показать медиа» отсутствует.
  11. Plain-text fallback: модератор идёт в самом конце, после времени, через |.
  12. Если нет причины — в List только 2 item'а (нарушитель + веб-профиль).
  13. Если нет WEB_PUBLIC_URL — в List нет пункта «🌐 Открыть профиль →».
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# Подкладываем test-окружение ДО импорта модулей проекта.
os.environ.setdefault("DB_PATH", ":memory:")
os.environ.setdefault("BOT_TOKEN", "test:token")
os.environ.setdefault("WEB_PASSWORD", "test-pwd")
os.environ.setdefault("SESSION_SECRET", "test-secret-xxxxxxxxxxxxxxxxxxxxx")
os.environ.setdefault("ADMIN_IDS", "111111111")

sys.path.insert(0, "/home/z/my-project/v4.5")

import aiogram.types as _aiogram_types  # noqa: E402
from sqlalchemy import delete  # noqa: E402

from db import (  # noqa: E402
    async_session, init_db, ChatSettings, ChatAdmin, Punishment,
    User, Moderator, WebUser,
)


# ═══════════════════════════════════════════════════════════════════════════
# Хелперы
# ═══════════════════════════════════════════════════════════════════════════

async def _clear_all_tables():
    async with async_session() as s:
        await s.execute(delete(Punishment))
        await s.execute(delete(ChatAdmin))
        await s.execute(delete(ChatSettings))
        await s.execute(delete(User))
        await s.execute(delete(Moderator))
        await s.execute(delete(WebUser))
        await s.commit()
    # v4.5.1: отключаем rate-limit на /login для тестов
    try:
        import web_app
        web_app._check_login_rate_limit = lambda ip: True
    except ImportError:
        pass


def _fake_user(uid: int, username: str | None = "user",
               first_name: str = "User") -> MagicMock:
    u = MagicMock(spec=_aiogram_types.User)
    u.id = uid
    u.username = username
    u.first_name = first_name
    u.last_name = None
    return u


def _make_photo_message(text: str | None = None) -> MagicMock:
    """Создаёт reply_to_message с фото (и опциональным caption)."""
    msg = MagicMock(spec=_aiogram_types.Message)
    msg.text = text
    msg.caption = "photo caption" if text is None else None
    msg.photo = [MagicMock(file_id="dummy_photo_file_id")]
    msg.video = None
    msg.animation = None
    msg.audio = None
    msg.voice = None
    msg.sticker = None
    msg.document = None
    msg.video_note = None
    return msg


def _make_text_message(text: str = "spam message") -> MagicMock:
    """Создаёт reply_to_message с текстом."""
    msg = MagicMock(spec=_aiogram_types.Message)
    msg.text = text
    msg.caption = None
    msg.photo = None
    msg.video = None
    msg.animation = None
    msg.audio = None
    msg.voice = None
    msg.sticker = None
    msg.document = None
    msg.video_note = None
    return msg


async def _setup_chat_settings(chat_id: int, report_chat_id: int,
                               hashtag: str = "#Бустерская"):
    """Создаёт ChatSettings с заданными параметрами."""
    async with async_session() as s:
        cs = ChatSettings(
            chat_id=chat_id,
            hashtag=hashtag,
            report_chat_id=report_chat_id,
            is_enabled=True,
        )
        s.add(cs)
        await s.commit()


# ═══════════════════════════════════════════════════════════════════════════
# Утилиты для распаковки rich message
# ═══════════════════════════════════════════════════════════════════════════

def _get_block_types(blocks: list) -> list[str]:
    """Возвращает список type-ов блоков."""
    types = []
    for b in blocks:
        # aiogram-объекты имеют .type, но иногда это enum — берём .value
        t = getattr(b, "type", None)
        if hasattr(t, "value"):
            t = t.value
        types.append(t)
    return types


def _find_block(blocks: list, block_type: str):
    """Возвращает первый блок заданного типа или None."""
    for b in blocks:
        t = getattr(b, "type", None)
        if hasattr(t, "value"):
            t = t.value
        if t == block_type:
            return b
    return None


def _find_all_blocks(blocks: list, block_type: str) -> list:
    """Возвращает все блоки заданного типа."""
    result = []
    for b in blocks:
        t = getattr(b, "type", None)
        if hasattr(t, "value"):
            t = t.value
        if t == block_type:
            result.append(b)
    return result


def _rich_text_parts(text_field) -> list[tuple[str, str | None]]:
    """Распаковывает RichText (str или list[str|RichText*]) в [(kind, value)].

    kind = 'str' для plain string, иначе имя типа RichText (e.g. 'url', 'code').
    value = сама строка (для url — 'text -> url').
    """
    if isinstance(text_field, str):
        return [("str", text_field)]
    parts = []
    for item in text_field:
        if isinstance(item, str):
            parts.append(("str", item))
        else:
            t = getattr(item, "type", None)
            if hasattr(t, "value"):
                t = t.value
            if t == "url":
                parts.append(("url", f"{item.text} -> {item.url}"))
            elif t == "code":
                parts.append(("code", item.text))
            else:
                parts.append((t or "unknown", str(getattr(item, "text", ""))))
    return parts


# ═══════════════════════════════════════════════════════════════════════════
# Тесты
# ═══════════════════════════════════════════════════════════════════════════

class TestReportStructure(unittest.IsolatedAsyncioTestCase):
    """Базовые тесты структуры rich message."""

    async def asyncSetUp(self):
        await init_db()
        await _clear_all_tables()
        await _setup_chat_settings(chat_id=-1001, report_chat_id=-2001,
                                   hashtag="#ТестЧат")

    async def _call_send_report(
        self,
        *,
        target: MagicMock | None = None,
        mod: MagicMock | None = ...,
        reason: str | None = "спам",
        action_type: str = "warn",
        reply_to_message: MagicMock | None = None,
        web_public_url: str | None = "https://degraban.bothost.tech",
    ) -> tuple[MagicMock, list]:
        """Вызывает _send_report и возвращает (mock_bot, blocks).

        mod=... (default, Ellipsis) → создаётся дефолтный модератор.
        mod=None                  → _send_report вызывается без модератора.
        """
        target = target or _fake_user(95354253, "VoronVan", "Ivan")
        if mod is ...:
            mod = _fake_user(12345678, "gleb_mod", "Gleb")
        # mod=None передаётся как есть

        bot = MagicMock()
        bot.send_rich_message = AsyncMock()
        bot.send_message = AsyncMock()
        bot.send_sticker = AsyncMock()

        with patch("bot_handlers.WEB_PUBLIC_URL", web_public_url):
            from bot_handlers import _send_report
            await _send_report(
                bot=bot,
                chat_id=-1001,
                target=target,
                action_type=action_type,
                reason=reason,
                mod=mod,
                warn_points=1,
                duration_seconds=None,
                reply_to_message=reply_to_message,
            )

        # Извлекаем blocks из rich_msg аргумента send_rich_message
        assert bot.send_rich_message.await_args is not None, \
            "send_rich_message was not called"
        rich_msg = bot.send_rich_message.await_args.kwargs.get("rich_message") \
            or bot.send_rich_message.await_args.args[0]
        # Убираем первый позиционный аргумент chat_id если он есть
        if rich_msg is None and bot.send_rich_message.await_args.args:
            # args[0]=chat_id, args[1]=rich_message
            rich_msg = bot.send_rich_message.await_args.args[1]
        blocks = list(rich_msg.blocks)
        return bot, blocks

    # ── Тест 1: базовая структура блоков ─────────────────────────────────
    async def test_block_structure_with_media(self):
        """Полная структура: 9 блоков в правильном порядке (с медиа)."""
        reply_msg = _make_photo_message(text=None)  # фото + caption
        _, blocks = await self._call_send_report(
            reply_to_message=reply_msg,
        )

        types = _get_block_types(blocks)
        # Ожидаем: heading, divider, list, divider, details(media),
        #          divider, details(extra), divider, footer
        expected = [
            "heading", "divider", "list", "divider",
            "details", "divider",
            "details", "divider",
            "footer",
        ]
        self.assertEqual(types, expected,
                         f"Block order mismatch.\n  got:      {types}\n"
                         f"  expected: {expected}")

    async def test_block_structure_without_media(self):
        """Без медиа: 7 блоков (media-details отсутствует)."""
        _, blocks = await self._call_send_report(
            reply_to_message=None,
        )
        types = _get_block_types(blocks)
        # Без медиа: heading, divider, list, divider, details(extra), divider, footer
        expected = [
            "heading", "divider", "list", "divider",
            "details", "divider",
            "footer",
        ]
        self.assertEqual(types, expected,
                         f"Block order (no media) mismatch.\n  got: {types}")

    # ── Тест 2: List содержит ровно 3 item'а ─────────────────────────────
    async def test_list_has_three_items_with_reason_and_web(self):
        """List содержит ровно 3 item'а: нарушитель, причина, веб-профиль."""
        _, blocks = await self._call_send_report(
            reason="спам",
            web_public_url="https://degraban.bothost.tech",
        )
        list_block = _find_block(blocks, "list")
        self.assertIsNotNone(list_block, "List block not found")
        self.assertEqual(len(list_block.items), 3,
                         f"Expected 3 list items, got {len(list_block.items)}")

    async def test_list_has_two_items_without_reason(self):
        """Без причины: List содержит 2 item'а (нарушитель + веб-профиль)."""
        _, blocks = await self._call_send_report(reason=None)
        list_block = _find_block(blocks, "list")
        self.assertEqual(len(list_block.items), 2,
                         "Without reason, list should have 2 items")

    async def test_list_has_two_items_without_web_url(self):
        """Без WEB_PUBLIC_URL: List содержит 2 item'а (нарушитель + причина)."""
        _, blocks = await self._call_send_report(
            web_public_url=None,
        )
        list_block = _find_block(blocks, "list")
        self.assertEqual(len(list_block.items), 2,
                         "Without WEB_PUBLIC_URL, list should have 2 items")

    async def test_list_has_one_item_only_offender(self):
        """Без причины и без WEB_PUBLIC_URL: List содержит 1 item (нарушитель)."""
        _, blocks = await self._call_send_report(
            reason=None, web_public_url=None,
        )
        list_block = _find_block(blocks, "list")
        self.assertEqual(len(list_block.items), 1,
                         "With only offender info, list should have 1 item")

    # ── Тест 3: offender item содержит RichTextUrl + RichTextCode ────────
    async def test_offender_item_has_url_and_code(self):
        """ListItem нарушителя содержит RichTextUrl (имя) + RichTextCode (ID)."""
        target = _fake_user(95354253, "VoronVan", "Ivan")
        _, blocks = await self._call_send_report(target=target)
        list_block = _find_block(blocks, "list")
        offender_item = list_block.items[0]
        # blocks[0] — это InputRichBlockParagraph
        para = offender_item.blocks[0]
        parts = _rich_text_parts(para.text)
        kinds = [k for k, _ in parts]

        self.assertIn("url", kinds,
                      f"Offender item should have RichTextUrl. Got: {kinds}")
        self.assertIn("code", kinds,
                      f"Offender item should have RichTextCode. Got: {kinds}")

        # Проверим что URL указывает на tg://user?id=<target_id>
        url_parts = [v for k, v in parts if k == "url"]
        self.assertTrue(any(f"tg://user?id={target.id}" in v for v in url_parts),
                        f"URL should point to tg://user?id={target.id}")

        # Проверим что code содержит ID
        code_parts = [v for k, v in parts if k == "code"]
        self.assertTrue(any(str(target.id) in v for v in code_parts),
                        f"Code should contain ID {target.id}")

    # ── Тест 4: веб-профиль item содержит RichTextUrl с коротким текстом ─
    async def test_web_profile_item_has_short_label(self):
        """ListItem веб-профиля содержит RichTextUrl с текстом
        «Открыть профиль →», НЕ полный URL."""
        _, blocks = await self._call_send_report(
            web_public_url="https://degraban.bothost.tech",
        )
        list_block = _find_block(blocks, "list")
        # items[-1] — последний, это веб-профиль (если причина есть)
        # Если причины нет — это items[1]
        web_item = list_block.items[-1]
        para = web_item.blocks[0]
        parts = _rich_text_parts(para.text)

        url_parts = [v for k, v in parts if k == "url"]
        self.assertTrue(len(url_parts) >= 1,
                        f"Web profile item should have RichTextUrl. parts: {parts}")

        # Текст ссылки должен быть коротким, не полный URL
        url_value = url_parts[0]  # format: "text -> url"
        label, _, actual_url = url_value.partition(" -> ")
        self.assertNotIn("degraban.bothost.tech", label,
                         f"Label should NOT contain full URL. Got: {label}")
        self.assertIn("профиль", label.lower(),
                      f"Label should mention 'профиль'. Got: {label}")
        # Сам URL должен быть полный
        self.assertIn("degraban.bothost.tech", actual_url)
        self.assertIn("/user/95354253", actual_url)

    # ── Тест 5: Details «📎 Показать медиа» свёрнут по умолчанию ─────────
    async def test_media_details_is_collapsed(self):
        """Details «📎 Показать медиа» имеет is_open=False."""
        reply_msg = _make_photo_message(text=None)
        _, blocks = await self._call_send_report(
            reply_to_message=reply_msg,
        )
        # Находим Details с summary "📎 Показать медиа"
        all_details = _find_all_blocks(blocks, "details")
        media_details = None
        for d in all_details:
            if "медиа" in (d.summary or "").lower():
                media_details = d
                break
        self.assertIsNotNone(media_details,
                             "Details with media not found")
        self.assertFalse(media_details.is_open,
                         "Media details should be collapsed (is_open=False)")

    # ── Тест 6: Внутри media Details есть media_block ────────────────────
    async def test_media_details_contains_photo_block(self):
        """Внутри Details есть InputRichBlockPhoto (если было фото)."""
        reply_msg = _make_photo_message(text=None)
        _, blocks = await self._call_send_report(
            reply_to_message=reply_msg,
        )
        all_details = _find_all_blocks(blocks, "details")
        media_details = None
        for d in all_details:
            if "медиа" in (d.summary or "").lower():
                media_details = d
                break
        self.assertIsNotNone(media_details)
        inner_types = _get_block_types(media_details.blocks)
        self.assertIn("photo", inner_types,
                      f"Media details should contain photo block. Got: {inner_types}")

    async def test_media_details_contains_blockquote_when_text(self):
        """Если есть text_content — внутри media Details есть BlockQuotation."""
        reply_msg = _make_text_message("spam message text")
        _, blocks = await self._call_send_report(
            reply_to_message=reply_msg,
        )
        all_details = _find_all_blocks(blocks, "details")
        media_details = None
        for d in all_details:
            if "медиа" in (d.summary or "").lower():
                media_details = d
                break
        self.assertIsNotNone(media_details)
        inner_types = _get_block_types(media_details.blocks)
        # aiogram использует 'blockquote' (Bot API type), не 'block_quotation'
        self.assertIn("blockquote", inner_types,
                      f"Media details should contain blockquote for text. "
                      f"Got: {inner_types}")

    # ── Тест 7: Footer содержит RichTextUrl с mod_id ─────────────────────
    async def test_footer_has_clickable_mod_name(self):
        """Footer содержит RichTextUrl с tg://user?id=<mod_id>."""
        mod = _fake_user(12345678, "gleb_mod", "Gleb")
        _, blocks = await self._call_send_report(mod=mod)
        footer = _find_block(blocks, "footer")
        self.assertIsNotNone(footer)
        parts = _rich_text_parts(footer.text)
        url_parts = [v for k, v in parts if k == "url"]
        self.assertTrue(any(f"tg://user?id={mod.id}" in v for v in url_parts),
                        f"Footer should have clickable URL for mod {mod.id}. "
                        f"parts: {parts}")

    # ── Тест 8: Footer НЕ содержит «Модератор:» ──────────────────────────
    async def test_footer_has_no_moderator_label(self):
        """Footer НЕ содержит приписку «Модератор:» или «👮»."""
        _, blocks = await self._call_send_report()
        footer = _find_block(blocks, "footer")
        parts = _rich_text_parts(footer.text)
        # Соберём все строки
        all_str = " ".join(v for _, v in parts)
        self.assertNotIn("Модератор", all_str,
                         f"Footer should NOT contain 'Модератор'. Got: {all_str}")
        self.assertNotIn("👮", all_str,
                         f"Footer should NOT contain 👮 emoji. Got: {all_str}")

    # ── Тест 9: mod=None — Footer без RichTextUrl ────────────────────────
    async def test_footer_without_mod_has_no_url(self):
        """Если mod=None — Footer без RichTextUrl (просто время + хэштег)."""
        _, blocks = await self._call_send_report(mod=None)
        footer = _find_block(blocks, "footer")
        self.assertIsNotNone(footer)
        parts = _rich_text_parts(footer.text)
        url_parts = [v for k, v in parts if k == "url"]
        self.assertEqual(url_parts, [],
                         f"Footer should have no URLs when mod=None. parts: {parts}")

    # ── Тест 10: reply_to_message=None — media Details отсутствует ───────
    async def test_no_media_details_without_reply(self):
        """Если reply_to_message=None — Details «📎 Показать медиа» отсутствует."""
        _, blocks = await self._call_send_report(reply_to_message=None)
        all_details = _find_all_blocks(blocks, "details")
        for d in all_details:
            self.assertNotIn("медиа", (d.summary or "").lower(),
                             "Should not have media Details when no reply_to_message")

    # ── Тест 11: Action label присутствует в SectionHeading ──────────────
    async def test_section_heading_has_action_label(self):
        """SectionHeading содержит правильный action_label."""
        for action_type, expected_emoji in [
            ("warn", "⚠️"),
            ("mute", "🔇"),
            ("ban", "🚫"),
            ("unmute", "🔊"),
            ("unban", "🎉"),
            ("unwarn", "↩️"),
        ]:
            _, blocks = await self._call_send_report(action_type=action_type)
            sh = _find_block(blocks, "heading")
            self.assertIsNotNone(sh)
            self.assertIn(expected_emoji, sh.text,
                          f"Action {action_type} should have emoji {expected_emoji}")


# ═══════════════════════════════════════════════════════════════════════════
# Тесты plain-text fallback
# ═══════════════════════════════════════════════════════════════════════════

class TestPlainFallback(unittest.IsolatedAsyncioTestCase):
    """Тесты что plain-text fallback тоже соответствует новой структуре."""

    async def asyncSetUp(self):
        await init_db()
        await _clear_all_tables()
        await _setup_chat_settings(chat_id=-1001, report_chat_id=-2001,
                                   hashtag="#ТестЧат")

    async def test_fallback_has_mod_at_end_after_time(self):
        """Если rich-отчёт падает — fallback отправляет модератора в конце."""
        from aiogram.exceptions import TelegramBadRequest
        from bot_handlers import _send_report

        target = _fake_user(95354253, "VoronVan", "Ivan")
        mod = _fake_user(12345678, "gleb_mod", "Gleb")

        bot = MagicMock()
        # send_rich_message падает → должен сработать fallback
        bot.send_rich_message = AsyncMock(
            side_effect=TelegramBadRequest(method="sendRichMessage",
                                           message="rich not supported"))
        bot.send_message = AsyncMock()
        bot.send_sticker = AsyncMock()

        with patch("bot_handlers.WEB_PUBLIC_URL",
                   "https://degraban.bothost.tech"):
            await _send_report(
                bot=bot, chat_id=-1001, target=target,
                action_type="warn", reason="спам", mod=mod,
                warn_points=1, duration_seconds=None,
                reply_to_message=None,
            )

        # send_message должен был вызваться (fallback)
        bot.send_message.assert_awaited()
        text = bot.send_message.await_args.kwargs.get("text") or \
            bot.send_message.await_args.args[1]

        # Модератор должен быть в самом конце, после времени
        time_idx = text.rfind("🕐")
        mod_idx = text.rfind(mod.first_name)
        self.assertGreater(mod_idx, time_idx,
                           f"Mod name should come AFTER time.\n"
                           f"  text: {text}\n  time_idx: {time_idx}, mod_idx: {mod_idx}")

        # Не должно быть «Модератор:»
        self.assertNotIn("Модератор:", text,
                         f"Plain fallback should NOT contain 'Модератор:'.\n"
                         f"  text: {text}")

        # Должен быть разделитель '|' перед модератором
        self.assertIn(f"| {mod.first_name}", text,
                      f"Mod should be prefixed with ' | '.\n  text: {text}")


# ═══════════════════════════════════════════════════════════════════════════
# Тесты: Divider'ы присутствуют
# ═══════════════════════════════════════════════════════════════════════════

class TestDividers(unittest.IsolatedAsyncioTestCase):
    """Проверяем что Divider'ы присутствуют в нужных местах."""

    async def asyncSetUp(self):
        await init_db()
        await _clear_all_tables()
        await _setup_chat_settings(chat_id=-1001, report_chat_id=-2001)

    async def test_at_least_three_dividers_with_media(self):
        """С медиа: минимум 4 divider'а (после heading, после list,
        после media details, после extra details)."""
        from bot_handlers import _send_report
        target = _fake_user(95354253, "VoronVan", "Ivan")
        mod = _fake_user(12345678, "gleb_mod", "Gleb")
        reply_msg = _make_photo_message(text=None)

        bot = MagicMock()
        bot.send_rich_message = AsyncMock()
        bot.send_message = AsyncMock()
        bot.send_sticker = AsyncMock()

        with patch("bot_handlers.WEB_PUBLIC_URL",
                   "https://degraban.bothost.tech"):
            await _send_report(
                bot=bot, chat_id=-1001, target=target,
                action_type="warn", reason="спам", mod=mod,
                warn_points=1, duration_seconds=None,
                reply_to_message=reply_msg,
            )

        rich_msg = bot.send_rich_message.await_args.kwargs.get("rich_message")
        blocks = list(rich_msg.blocks)
        dividers = _find_all_blocks(blocks, "divider")
        # Ожидаем 4 divider'а: после heading, после list, после media details,
        # после extra details
        self.assertGreaterEqual(len(dividers), 4,
                                f"Expected ≥4 dividers, got {len(dividers)}")

    async def test_at_least_three_dividers_without_media(self):
        """Без медиа: 3 divider'а (после heading, после list, после extra details)."""
        from bot_handlers import _send_report
        target = _fake_user(95354253, "VoronVan", "Ivan")
        mod = _fake_user(12345678, "gleb_mod", "Gleb")

        bot = MagicMock()
        bot.send_rich_message = AsyncMock()
        bot.send_message = AsyncMock()
        bot.send_sticker = AsyncMock()

        with patch("bot_handlers.WEB_PUBLIC_URL",
                   "https://degraban.bothost.tech"):
            await _send_report(
                bot=bot, chat_id=-1001, target=target,
                action_type="warn", reason="спам", mod=mod,
                warn_points=1, duration_seconds=None,
                reply_to_message=None,
            )

        rich_msg = bot.send_rich_message.await_args.kwargs.get("rich_message")
        blocks = list(rich_msg.blocks)
        dividers = _find_all_blocks(blocks, "divider")
        self.assertGreaterEqual(len(dividers), 3,
                                f"Expected ≥3 dividers (no media), got {len(dividers)}")


# ═══════════════════════════════════════════════════════════════════════════
# Тесты: стикер отправляется отдельным сообщением (без изменений от v4.4.9)
# ═══════════════════════════════════════════════════════════════════════════

class TestStickerHandling(unittest.IsolatedAsyncioTestCase):
    """Стикеры по-прежнему отправляются отдельным сообщением после rich-отчёта."""

    async def asyncSetUp(self):
        await init_db()
        await _clear_all_tables()
        await _setup_chat_settings(chat_id=-1001, report_chat_id=-2001)

    async def test_sticker_sent_separately_after_rich_report(self):
        """Если в reply_to_message есть стикер — bot.send_sticker вызывается."""
        from bot_handlers import _send_report
        target = _fake_user(95354253, "VoronVan", "Ivan")
        mod = _fake_user(12345678, "gleb_mod", "Gleb")

        # reply_to_message со стикером
        reply_msg = MagicMock(spec=_aiogram_types.Message)
        reply_msg.text = None
        reply_msg.caption = None
        reply_msg.photo = None
        reply_msg.video = None
        reply_msg.animation = None
        reply_msg.audio = None
        reply_msg.voice = None
        reply_msg.document = None
        reply_msg.video_note = None
        reply_msg.sticker = MagicMock(file_id="dummy_sticker_file_id")

        bot = MagicMock()
        bot.send_rich_message = AsyncMock()
        bot.send_message = AsyncMock()
        bot.send_sticker = AsyncMock()

        with patch("bot_handlers.WEB_PUBLIC_URL", None):
            await _send_report(
                bot=bot, chat_id=-1001, target=target,
                action_type="warn", reason="спам", mod=mod,
                warn_points=1, duration_seconds=None,
                reply_to_message=reply_msg,
            )

        bot.send_sticker.assert_awaited_once()
        # chat_id должен быть report_dest=-2001
        kwargs = bot.send_sticker.await_args.kwargs
        self.assertEqual(kwargs.get("chat_id"), -2001)
        self.assertEqual(kwargs.get("sticker"), "dummy_sticker_file_id")


if __name__ == "__main__":
    unittest.main(verbosity=2)
