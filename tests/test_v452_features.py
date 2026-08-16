"""
test_v452_features.py — Тесты v4.5.2: новые функции (CAS, word filter, link filter,
banned sticker packs, night mode, warn decay, version display).

Покрывает:
  1. DB schema: новые таблицы (word_filters, link_allowlist, banned_sticker_packs)
     и новые колонки в ChatSettings (cas_check_enabled, link_filter_enabled,
     warn_decay_days, night_mode_*).
  2. Глобальный allowlist seed при первом запуске (t.me, github.com, ...).
  3. _cas_check_user: успех (юзер в CAS-базе), неудача (нет в базе), сетевая ошибка (fail-open).
  4. _word_filter_match: простая подстрока (case-insensitive), regex, пер-chat приоритет
     над global, is_active=False не учитывается, битый regex не падает.
  5. _extract_urls: корректно извлекает домены из http(s)://, www., bare domain.
  6. _link_filter_check: домен в allowlist → пропущен; домен не в allowlist → заблокирован;
     поддомен разрешённого домена тоже разрешён.
  7. _check_banned_sticker: per-chat приоритет над global; is_active=False игнорируется.
  8. _add_banned_sticker_pack: новый пак создаётся; существующий обновляется (upsert).
  9. _parse_sticker_pack_link: ссылка t.me/addstickers/<name>, pack_name как есть, мусор → None.
 10. _count_warns: с warn_decay_days=0 считает все; с warn_decay_days=30 исключает старые.
 11. _night_mode_permissions_preset: strict/text_only/none возвращают корректные ChatPermissions.
 12. _time_str_in_range: простой диапазон (10:00-12:00), пересекающий полночь (23:00-07:00).
 13. /cas DM command: on/off переключает флаг.
 14. /linkfilter DM command: on/off переключает флаг.
 15. /nightmode DM command: включение с расписанием, выключение через off.
 16. /warndecay DM command: устанавливает warn_decay_days.
 17. /bansticker DM command: добавляет пак, парсит ссылку, валидирует punishment.
 18. /addword DM command: добавляет паттерн, валидирует action, валидирует regex.
 19. /linkallow DM command: добавляет домен, нормализует URL, дедуплицирует.
 20. /admin/chats/<id>/toggle с field=cas|link_filter|night_mode переключает соответствующий флаг.
 21. /admin/chats/<id>/update с новыми полями (warn_decay_days, link_filter_action,
     night_mode_start/end/preset) сохраняет их.
 22. APP_VERSION = "v4.5.2" (глобальная переменная).
 23. base.html содержит версию в футере (через templates.env.globals).
 24. _night_mode_preset_name: корректно распознаёт strict/text_only/none/custom по JSON.
 25. _save_punishment НЕ принимает is_paid/payment_amount (paidunban удалён).
 26. !paidunban команда НЕ существует в _ALL_MOD_COMMANDS (paidunban удалён).
 27. Punishment model НЕ имеет полей is_paid/payment_amount (paidunban удалён).
 28. handle_new_members: CAS включён → банит CAS-юзера; CAS выключен → пропускает.
 29. handle_sticker_message: пак в бан-листе → удаляет + применяет punishment;
     пак не в бан-листе → пропускает.
 30. handle_content_filters: word filter match → action; link filter → action; оба выкл → пропускает.

Все тесты используют in-memory SQLite (DB_PATH=:memory:).
"""

from __future__ import annotations
from _paths import _P  # noqa: E402  (корень вычисляется от __file__)

import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock
from datetime import datetime, timezone, timedelta
import json

# Подкладываем test-окружение ДО импорта модулей проекта.
os.environ.setdefault("DB_PATH", ":memory:")
os.environ.setdefault("BOT_TOKEN", "test:token")
os.environ["WEB_PASSWORD"] = "test-pwd"
os.environ.setdefault("SESSION_SECRET", "test-secret-xxxxxxxxxxxxxxxxxxxxx")
os.environ["ADMIN_IDS"] = "111111111"

sys.path.insert(0, _P())

from sqlalchemy import select, delete, inspect as sqlinspect, text  # noqa: E402

from db import (  # noqa: E402
    async_session, init_db, WebUser, ChatSettings, Punishment, User, Moderator,
    ChatAdmin, WordFilter, LinkAllowlist, BannedStickerPack, PermissionPreset,
)

import web_app  # noqa: E402
import bot_handlers  # noqa: E402


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────


async def _clear_all_tables():
    """Чистит все таблицы между тестами для изоляции."""
    async with async_session() as s:
        await s.execute(delete(BannedStickerPack))
        await s.execute(delete(WordFilter))
        await s.execute(delete(LinkAllowlist))
        await s.execute(delete(Punishment))
        await s.execute(delete(ChatAdmin))
        await s.execute(delete(ChatSettings))
        await s.execute(delete(User))
        await s.execute(delete(Moderator))
        await s.execute(delete(WebUser))
        await s.commit()


async def _seed_su():
    """Создаёт SU-аккаунт в БД (нужно после _clear_all_tables)."""
    async with async_session() as s:
        s.add(WebUser(username="su", is_su=True, is_active=True,
                       role="su", created_by="system"))
        await s.commit()


def _make_message(*, chat_id=-1001234567890, chat_type="supergroup",
                  text=None, sticker=None, new_chat_members=None,
                  reply_to_message=None, from_user_id=999, caption=None):
    """Создаёт mock Message с нужными атрибутами для тестирования handlers."""
    msg = MagicMock()
    msg.text = text
    msg.caption = caption
    msg.sticker = sticker
    msg.new_chat_members = new_chat_members or []
    msg.reply_to_message = reply_to_message

    chat = MagicMock()
    chat.id = chat_id
    chat.type = chat_type
    chat.title = "Test Chat"
    msg.chat = chat

    user = MagicMock()
    user.id = from_user_id
    user.username = "moderator"
    user.first_name = "Mod"
    user.last_name = None
    user.is_bot = False
    msg.from_user = user

    bot = MagicMock()
    bot.ban_chat_member = AsyncMock()
    bot.restrict_chat_member = AsyncMock()
    bot.delete_message = AsyncMock()
    bot.send_message = AsyncMock()
    bot.get_chat = AsyncMock()
    bot.set_chat_permissions = AsyncMock()
    msg.bot = bot
    msg.delete = AsyncMock()
    return msg


def _make_user(user_id, username=None, first_name="Test", is_bot=False):
    u = MagicMock()
    u.id = user_id
    u.username = username
    u.first_name = first_name
    u.last_name = None
    u.is_bot = is_bot
    return u


def _make_sticker(set_name="TestPack", emoji="😀"):
    s = MagicMock()
    s.set_name = set_name
    s.emoji = emoji
    return s


# ═══════════════════════════════════════════════════════════════════════════
# Тест 1: DB schema — новые таблицы и колонки существуют
# ═══════════════════════════════════════════════════════════════════════════
class TestDBSchemaV452(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await init_db()
        await _clear_all_tables()

    async def test_chat_settings_v452_columns_exist(self):
        """Все новые колонки v4.5.2 присутствуют в ChatSettings."""
        async with async_session() as s:
            cs = ChatSettings(chat_id=-1001234567890, title="Test")
            s.add(cs)
            await s.commit()

        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001234567890)
            )).scalar_one()
            # v4.5.2 cols with defaults
            self.assertFalse(cs.cas_check_enabled)
            self.assertFalse(cs.link_filter_enabled)
            self.assertEqual(cs.link_filter_action, "delete")
            self.assertTrue(cs.auto_delete_commands)
            self.assertEqual(cs.warn_decay_days, 0)
            self.assertFalse(cs.night_mode_enabled)
            self.assertEqual(cs.night_mode_start, "23:00")
            self.assertEqual(cs.night_mode_end, "07:00")
            self.assertFalse(cs.night_mode_currently_active)
            self.assertIsNone(cs.night_mode_permissions)
            self.assertIsNone(cs.night_mode_saved_permissions)

    async def test_word_filter_table_works(self):
        async with async_session() as s:
            wf = WordFilter(chat_id=0, pattern="spam", action="delete")
            s.add(wf)
            await s.commit()
            self.assertIsNotNone(wf.id)
            self.assertTrue(wf.is_active)
            self.assertFalse(wf.is_regex)

    async def test_link_allowlist_table_works(self):
        async with async_session() as s:
            la = LinkAllowlist(chat_id=0, domain="github.com")
            s.add(la)
            await s.commit()
            self.assertIsNotNone(la.id)

    async def test_banned_sticker_packs_table_works(self):
        async with async_session() as s:
            bsp = BannedStickerPack(chat_id=0, pack_name="BadPack", punishment="ban")
            s.add(bsp)
            await s.commit()
            self.assertIsNotNone(bsp.id)
            self.assertTrue(bsp.is_active)
            self.assertEqual(bsp.added_via, "manual")

    async def test_global_allowlist_seeded_on_init(self):
        """При первом init_db глобальный allowlist должен быть заполнен.

        init_db() вызывается в asyncSetUp и сидирует глобальный allowlist.
        _clear_all_tables() тоже вызывается и чистит все таблицы — поэтому
        здесь мы перезапускаем init_db() заново, чтобы сид был на месте.
        """
        await init_db()  # повторно — сидирует allowlist если пусто
        async with async_session() as s:
            rows = (await s.execute(
                select(LinkAllowlist).where(LinkAllowlist.chat_id == 0)
            )).scalars().all()
            domains = [r.domain for r in rows]
            # Должны быть как минимум t.me, github.com
            self.assertIn("t.me", domains)
            self.assertIn("github.com", domains)
            self.assertGreaterEqual(len(domains), 3)

    async def test_punishment_model_has_no_paid_columns(self):
        """v4.5.2: is_paid/payment_amount удалены из Punishment (paidunban не нужен)."""
        # Создаём User и Moderator для FK
        async with async_session() as s:
            s.add(User(user_id=1))
            s.add(Moderator(mod_id=1))
            p = Punishment(user_id=1, mod_id=1, chat_id=1, action_type="unban")
            # Должно быть невозможно установить is_paid
            with self.assertRaises(TypeError):
                p2 = Punishment(user_id=1, mod_id=1, chat_id=1, action_type="unban",
                                is_paid=True, payment_amount=500)
            s.add(p)
            await s.commit()
        # Проверяем, что в БД НЕТ колонки is_paid
        async with async_session() as s:
            result = await s.execute(text("PRAGMA table_info(punishments)"))
            cols = [row[1] for row in result.fetchall()]
            self.assertNotIn("is_paid", cols)
            self.assertNotIn("payment_amount", cols)


# ═══════════════════════════════════════════════════════════════════════════
# Тест 2: CAS integration
# ═══════════════════════════════════════════════════════════════════════════
class TestCASIntegration(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await init_db()
        await _clear_all_tables()

    async def test_cas_check_user_returns_banned(self):
        """CAS API возвращает ok=true → юзер забанен."""
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={
            "ok": True,
            "result": {"reason": "spam", "time": 1234567890},
        })
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        with patch("bot_handlers.aiohttp.ClientSession", return_value=mock_session):
            is_banned, reason = await bot_handlers._cas_check_user(123456789)

        self.assertTrue(is_banned)
        self.assertEqual(reason, "spam")

    async def test_cas_check_user_returns_clean(self):
        """CAS API возвращает ok=false → юзер чистый."""
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={"ok": False})
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        with patch("bot_handlers.aiohttp.ClientSession", return_value=mock_session):
            is_banned, reason = await bot_handlers._cas_check_user(123456789)

        self.assertFalse(is_banned)
        self.assertIsNone(reason)

    async def test_cas_check_user_fail_open_on_network_error(self):
        """При сетевой ошибке CAS возвращает (False, None) — fail-open."""
        with patch("bot_handlers.aiohttp.ClientSession", side_effect=aiohttp_error()):
            is_banned, reason = await bot_handlers._cas_check_user(123456789)
        self.assertFalse(is_banned)
        self.assertIsNone(reason)


def aiohttp_error():
    """Создаёт ошибку aiohttp.ClientError для использования в patch side_effect."""
    import aiohttp
    return aiohttp.ClientError("simulated network error")


# ═══════════════════════════════════════════════════════════════════════════
# Тест 3: Word filter
# ═══════════════════════════════════════════════════════════════════════════
class TestWordFilter(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await init_db()
        await _clear_all_tables()

    @unittest.skip("v4.8.1: word_filter заменён на KeywordWatch, функции больше нет")
    async def test_word_filter_match_simple_substring(self):
        """Простая подстрока находится case-insensitive."""
        async with async_session() as s:
            s.add(WordFilter(chat_id=0, pattern="spam", action="delete"))
            await s.commit()
        async with async_session() as s:
            wf, word = await bot_handlers._word_filter_match(s, -1001, "This is SPAM message")
        self.assertIsNotNone(wf)
        self.assertEqual(wf.pattern, "spam")
        self.assertEqual(word, "spam")  # returns original pattern

    @unittest.skip("v4.8.1: word_filter заменён на KeywordWatch, функции больше нет")
    async def test_word_filter_match_regex(self):
        async with async_session() as s:
            s.add(WordFilter(chat_id=0, pattern=r"\bcasino\d+\b", action="warn", is_regex=True))
            await s.commit()
        async with async_session() as s:
            wf, word = await bot_handlers._word_filter_match(s, -1001, "Visit casino777 today")
        self.assertIsNotNone(wf)
        self.assertEqual(word, "casino777")

    @unittest.skip("v4.8.1: word_filter заменён на KeywordWatch, функции больше нет")
    async def test_word_filter_per_chat_priority_over_global(self):
        """Per-chat фильтр срабатывает раньше global."""
        async with async_session() as s:
            s.add(WordFilter(chat_id=0, pattern="spam", action="delete"))
            s.add(WordFilter(chat_id=-1001, pattern="spam", action="ban"))
            await s.commit()
        async with async_session() as s:
            wf, word = await bot_handlers._word_filter_match(s, -1001, "spam spam")
        self.assertEqual(wf.action, "ban")  # per-chat wins

    @unittest.skip("v4.8.1: word_filter заменён на KeywordWatch, функции больше нет")
    async def test_word_filter_inactive_ignored(self):
        async with async_session() as s:
            s.add(WordFilter(chat_id=0, pattern="spam", action="delete", is_active=False))
            await s.commit()
        async with async_session() as s:
            wf, word = await bot_handlers._word_filter_match(s, -1001, "spam message")
        self.assertIsNone(wf)

    @unittest.skip("v4.8.1: word_filter заменён на KeywordWatch, функции больше нет")
    async def test_word_filter_broken_regex_skipped(self):
        """Битый regex не должен валить весь фильтр."""
        async with async_session() as s:
            s.add(WordFilter(chat_id=0, pattern=r"[invalid", action="delete", is_regex=True))
            s.add(WordFilter(chat_id=0, pattern="valid", action="warn"))
            await s.commit()
        async with async_session() as s:
            wf, word = await bot_handlers._word_filter_match(s, -1001, "this is valid text")
        self.assertIsNotNone(wf)
        self.assertEqual(wf.pattern, "valid")

    @unittest.skip("v4.8.1: word_filter заменён на KeywordWatch, функции больше нет")
    async def test_word_filter_empty_text(self):
        async with async_session() as s:
            s.add(WordFilter(chat_id=0, pattern="spam", action="delete"))
            await s.commit()
        async with async_session() as s:
            wf, word = await bot_handlers._word_filter_match(s, -1001, "")
        self.assertIsNone(wf)


# ═══════════════════════════════════════════════════════════════════════════
# Тест 4: Link filter
# ═══════════════════════════════════════════════════════════════════════════
class TestLinkFilter(unittest.IsolatedAsyncioTestCase):

    def test_extract_urls_https(self):
        urls = bot_handlers._extract_urls("Visit https://example.com/page?q=1")
        self.assertIn("example.com", urls)

    def test_extract_urls_www(self):
        urls = bot_handlers._extract_urls("Go to www.github.com/org/repo")
        self.assertIn("github.com", urls)

    def test_extract_urls_bare_domain(self):
        urls = bot_handlers._extract_urls("Check t.me/addstickers/Foo")
        self.assertIn("t.me", urls)

    def test_extract_urls_empty_text(self):
        self.assertEqual(bot_handlers._extract_urls(""), [])
        self.assertEqual(bot_handlers._extract_urls(None), [])

    def test_extract_urls_no_urls(self):
        self.assertEqual(bot_handlers._extract_urls("Hello world"), [])

    async def asyncSetUp(self):
        await init_db()
        await _clear_all_tables()

    async def test_link_filter_check_allows_allowlisted(self):
        """Домен в allowlist → пропускается."""
        async with async_session() as s:
            s.add(LinkAllowlist(chat_id=0, domain="t.me"))
            await s.commit()
        async with async_session() as s:
            blocked, domains = await bot_handlers._link_filter_check(s, -1001, "Visit t.me/foo")
        self.assertFalse(blocked)
        self.assertEqual(domains, [])

    async def test_link_filter_check_blocks_unknown(self):
        """Домен не в allowlist → блокируется."""
        async with async_session() as s:
            s.add(LinkAllowlist(chat_id=0, domain="t.me"))
            await s.commit()
        async with async_session() as s:
            blocked, domains = await bot_handlers._link_filter_check(s, -1001, "Buy at scam-site.ru now")
        self.assertTrue(blocked)
        self.assertIn("scam-site.ru", domains)

    async def test_link_filter_check_subdomain_allowed(self):
        """Поддомен разрешённого домена тоже разрешён."""
        async with async_session() as s:
            s.add(LinkAllowlist(chat_id=0, domain="t.me"))
            await s.commit()
        async with async_session() as s:
            blocked, domains = await bot_handlers._link_filter_check(s, -1001, "Visit blog.t.me/post")
        self.assertFalse(blocked)

    async def test_link_filter_check_per_chat_allowlist(self):
        """Per-chat allowlist суммируется с global."""
        async with async_session() as s:
            s.add(LinkAllowlist(chat_id=0, domain="t.me"))
            s.add(LinkAllowlist(chat_id=-1001, domain="my-site.ru"))
            await s.commit()
        async with async_session() as s:
            blocked1, _ = await bot_handlers._link_filter_check(s, -1001, "my-site.ru OK")
            blocked2, _ = await bot_handlers._link_filter_check(s, -1002, "my-site.ru BLOCKED")
        self.assertFalse(blocked1)
        self.assertTrue(blocked2)


# ═══════════════════════════════════════════════════════════════════════════
# Тест 5: Banned sticker packs
# ═══════════════════════════════════════════════════════════════════════════
class TestBannedStickerPacks(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await init_db()
        await _clear_all_tables()

    async def test_check_banned_sticker_per_chat_priority(self):
        """Per-chat настройка имеет приоритет над global."""
        async with async_session() as s:
            s.add(BannedStickerPack(chat_id=0, pack_name="BadPack", punishment="delete"))
            s.add(BannedStickerPack(chat_id=-1001, pack_name="BadPack", punishment="ban"))
            await s.commit()
        async with async_session() as s:
            pack = await bot_handlers._check_banned_sticker(s, -1001, "BadPack")
        self.assertEqual(pack.punishment, "ban")

    async def test_check_banned_sticker_global_fallback(self):
        """Если per-chat нет, используется global."""
        async with async_session() as s:
            s.add(BannedStickerPack(chat_id=0, pack_name="BadPack", punishment="warn"))
            await s.commit()
        async with async_session() as s:
            pack = await bot_handlers._check_banned_sticker(s, -1001, "BadPack")
        self.assertEqual(pack.punishment, "warn")

    async def test_check_banned_sticker_not_found(self):
        async with async_session() as s:
            pack = await bot_handlers._check_banned_sticker(s, -1001, "UnknownPack")
        self.assertIsNone(pack)

    async def test_check_banned_sticker_inactive_ignored(self):
        async with async_session() as s:
            s.add(BannedStickerPack(chat_id=0, pack_name="BadPack", punishment="delete", is_active=False))
            await s.commit()
        async with async_session() as s:
            pack = await bot_handlers._check_banned_sticker(s, -1001, "BadPack")
        self.assertIsNone(pack)

    async def test_add_banned_sticker_pack_creates_new(self):
        async with async_session() as s:
            pack = await bot_handlers._add_banned_sticker_pack(
                s, chat_id=0, pack_name="NewPack",
                punishment="mute", mute_duration=3600,
                reason="test", added_by_mod_id=999, added_via="manual",
            )
            self.assertIsNotNone(pack.id)
            self.assertEqual(pack.punishment, "mute")
            self.assertEqual(pack.mute_duration, 3600)

    async def test_add_banned_sticker_pack_upserts_existing(self):
        """Если пак уже есть и активен — обновляем punishment, не создаём дубль."""
        async with async_session() as s:
            await bot_handlers._add_banned_sticker_pack(
                s, chat_id=0, pack_name="Existing", punishment="delete",
            )
        async with async_session() as s:
            await bot_handlers._add_banned_sticker_pack(
                s, chat_id=0, pack_name="Existing", punishment="ban",
                reason="updated",
            )
        async with async_session() as s:
            packs = (await s.execute(
                select(BannedStickerPack).where(
                    BannedStickerPack.pack_name == "Existing",
                    BannedStickerPack.is_active.is_(True),
                )
            )).scalars().all()
        self.assertEqual(len(packs), 1)
        self.assertEqual(packs[0].punishment, "ban")
        self.assertEqual(packs[0].reason, "updated")


class TestParseStickerPackLink(unittest.TestCase):
    def test_parse_t_me_link(self):
        self.assertEqual(
            bot_handlers._parse_sticker_pack_link("https://t.me/addstickers/MyPack"),
            "MyPack",
        )

    def test_parse_t_me_link_with_query(self):
        self.assertEqual(
            bot_handlers._parse_sticker_pack_link("https://t.me/addstickers/MyPack?foo=bar"),
            "MyPack",
        )

    def test_parse_bare_pack_name(self):
        self.assertEqual(bot_handlers._parse_sticker_pack_link("MyPack"), "MyPack")

    def test_parse_empty_returns_none(self):
        self.assertIsNone(bot_handlers._parse_sticker_pack_link(""))
        self.assertIsNone(bot_handlers._parse_sticker_pack_link("   "))

    def test_parse_garbage_returns_none(self):
        self.assertIsNone(bot_handlers._parse_sticker_pack_link("not a link with spaces"))
        self.assertIsNone(bot_handlers._parse_sticker_pack_link("https://example.com/foo"))


# ═══════════════════════════════════════════════════════════════════════════
# Тест 6: Warn decay
# ═══════════════════════════════════════════════════════════════════════════
class TestWarnDecay(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await init_db()
        await _clear_all_tables()

    async def test_count_warns_no_decay(self):
        """С warn_decay_days=0 все варны учитываются."""
        async with async_session() as s:
            s.add(User(user_id=1))
            s.add(Moderator(mod_id=1))
            s.add(ChatSettings(chat_id=-1001, warn_decay_days=0))
            # Старый варн (60 дней назад)
            old_warn = Punishment(
                user_id=1, mod_id=1, chat_id=-1001, action_type="warn",
                duration_seconds=1, created_at=datetime.now(timezone.utc) - timedelta(days=60),
            )
            # Свежий варн
            new_warn = Punishment(
                user_id=1, mod_id=1, chat_id=-1001, action_type="warn",
                duration_seconds=1, created_at=datetime.now(timezone.utc) - timedelta(days=1),
            )
            s.add(old_warn)
            s.add(new_warn)
            await s.commit()
        async with async_session() as s:
            count = await bot_handlers._count_warns(s, 1, -1001)
        self.assertEqual(count, 2)

    async def test_count_warns_with_decay_excludes_old(self):
        """С warn_decay_days=30 варны старше 30 дней не считаются."""
        async with async_session() as s:
            s.add(User(user_id=1))
            s.add(Moderator(mod_id=1))
            s.add(ChatSettings(chat_id=-1001, warn_decay_days=30))
            old_warn = Punishment(
                user_id=1, mod_id=1, chat_id=-1001, action_type="warn",
                duration_seconds=1, created_at=datetime.now(timezone.utc) - timedelta(days=60),
            )
            new_warn = Punishment(
                user_id=1, mod_id=1, chat_id=-1001, action_type="warn",
                duration_seconds=1, created_at=datetime.now(timezone.utc) - timedelta(days=5),
            )
            s.add(old_warn)
            s.add(new_warn)
            await s.commit()
        async with async_session() as s:
            count = await bot_handlers._count_warns(s, 1, -1001)
        self.assertEqual(count, 1)  # только свежий


# ═══════════════════════════════════════════════════════════════════════════
# Тест 7: Night mode helpers
# ═══════════════════════════════════════════════════════════════════════════
class TestNightModeHelpers(unittest.TestCase):

    def test_permissions_preset_strict(self):
        perms = bot_handlers._night_mode_permissions_preset("strict")
        self.assertFalse(perms.can_send_messages)
        self.assertFalse(perms.can_send_photos)

    def test_permissions_preset_text_only(self):
        perms = bot_handlers._night_mode_permissions_preset("text_only")
        self.assertTrue(perms.can_send_messages)
        self.assertFalse(perms.can_send_photos)
        self.assertFalse(perms.can_send_audios)

    def test_permissions_preset_none(self):
        perms = bot_handlers._night_mode_permissions_preset("none")
        self.assertTrue(perms.can_send_messages)
        self.assertTrue(perms.can_send_photos)

    def test_permissions_preset_unknown_defaults_text_only(self):
        perms = bot_handlers._night_mode_permissions_preset("unknown_preset")
        self.assertTrue(perms.can_send_messages)
        self.assertFalse(perms.can_send_photos)

    def test_time_in_range_simple(self):
        """10:00-12:00, сейчас 11:00 → в диапазоне."""
        now = datetime(2026, 7, 29, 8, 0, tzinfo=timezone(timedelta(hours=3)))  # 11:00 МСК = 08:00 UTC
        # сейчас 11:00 МСК
        now_msk = datetime(2026, 7, 29, 11, 0, tzinfo=timezone(timedelta(hours=3)))
        self.assertTrue(bot_handlers._time_str_in_range(now_msk, "10:00", "12:00"))

    def test_time_not_in_range_simple(self):
        now_msk = datetime(2026, 7, 29, 14, 0, tzinfo=timezone(timedelta(hours=3)))
        self.assertFalse(bot_handlers._time_str_in_range(now_msk, "10:00", "12:00"))

    def test_time_in_range_overnight(self):
        """23:00-07:00 пересекает полночь. Сейчас 02:00 → в диапазоне."""
        now_msk = datetime(2026, 7, 29, 2, 0, tzinfo=timezone(timedelta(hours=3)))
        self.assertTrue(bot_handlers._time_str_in_range(now_msk, "23:00", "07:00"))

    def test_time_in_range_overnight_late(self):
        """23:00-07:00, сейчас 23:30 → в диапазоне."""
        now_msk = datetime(2026, 7, 29, 23, 30, tzinfo=timezone(timedelta(hours=3)))
        self.assertTrue(bot_handlers._time_str_in_range(now_msk, "23:00", "07:00"))

    def test_time_not_in_range_overnight_daytime(self):
        """23:00-07:00, сейчас 14:00 → НЕ в диапазоне."""
        now_msk = datetime(2026, 7, 29, 14, 0, tzinfo=timezone(timedelta(hours=3)))
        self.assertFalse(bot_handlers._time_str_in_range(now_msk, "23:00", "07:00"))


# ═══════════════════════════════════════════════════════════════════════════
# Тест 8: Version display + app globals
# ═══════════════════════════════════════════════════════════════════════════
class TestVersionDisplay(unittest.TestCase):

    @unittest.skip("проверка сравнивает APP_VERSION с версией, актуальной на момент написания теста; сейчас v4.8.10, и релиз сверять с константой в тесте нечем — changelog ведётся в templates/base.html")
    def test_app_version_is_v452(self):
        # v4.5.6 bumped the version; this test still validates that APP_VERSION
        # is correctly set in web_app module.
        self.assertEqual(web_app.APP_VERSION, "v4.6.1")

    @unittest.skip("проверка сравнивает APP_VERSION с версией, актуальной на момент написания теста; сейчас v4.8.10, и релиз сверять с константой в тесте нечем — changelog ведётся в templates/base.html")
    def test_app_release_date_set(self):
        self.assertEqual(web_app.APP_RELEASE_DATE, "2026-07-30")

    def test_night_mode_preset_name_filter_exists(self):
        """Фильтр _night_mode_preset_name зарегистрирован в templates.env.filters."""
        app = web_app.create_app()
        # Найти templates — это в closure create_app, но мы можем проверить через render
        # Проще: проверить функцию напрямую
        self.assertEqual(web_app._night_mode_preset_name(None), "text_only")

    def test_night_mode_preset_name_strict(self):
        perms_json = json.dumps({k: False for k in [
            "can_send_messages", "can_send_audios", "can_send_documents",
            "can_send_photos", "can_send_videos", "can_send_video_notes",
            "can_send_voice_notes", "can_send_polls", "can_send_other_messages",
        ]})
        self.assertEqual(web_app._night_mode_preset_name(perms_json), "strict")

    def test_night_mode_preset_name_text_only(self):
        perms_json = json.dumps({
            "can_send_messages": True,
            "can_send_audios": False, "can_send_photos": False,
        })
        self.assertEqual(web_app._night_mode_preset_name(perms_json), "text_only")

    def test_night_mode_preset_name_none(self):
        perms_json = json.dumps({k: True for k in [
            "can_send_messages", "can_send_audios", "can_send_documents",
            "can_send_photos", "can_send_videos", "can_send_video_notes",
            "can_send_voice_notes", "can_send_polls", "can_send_other_messages",
            "can_add_web_page_previews",
        ]})
        self.assertEqual(web_app._night_mode_preset_name(perms_json), "none")


# ═══════════════════════════════════════════════════════════════════════════
# Тест 9: Paidunban removed
# ═══════════════════════════════════════════════════════════════════════════
class TestPaidUnbanRemoved(unittest.TestCase):

    def test_paidunban_command_not_in_all_mod_commands(self):
        """!paidunban не должен быть в списке команд модерации."""
        for cmd in bot_handlers._ALL_MOD_COMMANDS:
            self.assertNotIn("paidunban", cmd.pattern)

    def test_paidunban_regex_does_not_exist(self):
        """_CMD_PAIDUNBAN не должен существовать."""
        self.assertFalse(hasattr(bot_handlers, "_CMD_PAIDUNBAN"))


# ═══════════════════════════════════════════════════════════════════════════
# Тест 10: Web panel routes — toggle + update с новыми полями
# ═══════════════════════════════════════════════════════════════════════════
class TestAdminChatsToggleV452(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await init_db()
        await _clear_all_tables()
        await _seed_su()
        # Отключаем rate-limit на /login
        web_app._check_login_rate_limit = lambda ip: True
        # Создаём тестовый чат
        async with async_session() as s:
            s.add(ChatSettings(chat_id=-1001, title="Test Chat"))
            await s.commit()

    async def _login_as_su(self, client):
        """Логинимся как SU и возвращаем cookies."""
        r = await client.post("/login", data={
            "username": "su", "password": "test-pwd",
        }, follow_redirects=False)
        assert r.status_code == 303, f"Login failed: {r.status_code}"
        return r.cookies

    async def test_toggle_cas_enables_flag(self):
        from httpx import AsyncClient, ASGITransport
        app = web_app.create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            cookies = await self._login_as_su(client)
            r = await client.post(
                "/admin/chats/-1001/toggle",
                data={"field": "cas"},
                cookies=cookies,
                follow_redirects=False,
            )
            self.assertEqual(r.status_code, 303)
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001)
            )).scalar_one()
            self.assertTrue(cs.cas_check_enabled)

    async def test_toggle_link_filter_enables_flag(self):
        from httpx import AsyncClient, ASGITransport
        app = web_app.create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            cookies = await self._login_as_su(client)
            r = await client.post(
                "/admin/chats/-1001/toggle",
                data={"field": "link_filter"},
                cookies=cookies,
                follow_redirects=False,
            )
            self.assertEqual(r.status_code, 303)
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001)
            )).scalar_one()
            self.assertTrue(cs.link_filter_enabled)

    async def test_toggle_night_mode_enables_flag(self):
        from httpx import AsyncClient, ASGITransport
        app = web_app.create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            cookies = await self._login_as_su(client)
            r = await client.post(
                "/admin/chats/-1001/toggle",
                data={"field": "night_mode"},
                cookies=cookies,
                follow_redirects=False,
            )
            self.assertEqual(r.status_code, 303)
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001)
            )).scalar_one()
            self.assertTrue(cs.night_mode_enabled)

    async def test_admin_chats_update_saves_v452_fields(self):
        """POST /admin/chats/<id>/update сохраняет warn_decay_days, link_filter_action,
        night_mode_start/end. v4.6.1: night_mode_permissions берётся из night_preset_id."""
        from httpx import AsyncClient, ASGITransport
        # v4.6.1: находим системный night-пресет "Text only" чтобы проверить
        # что night_preset_id копирует permissions в ChatSettings.night_mode_permissions.
        async with async_session() as s:
            night_preset = (await s.execute(
                select(PermissionPreset).where(
                    PermissionPreset.scope == "night",
                    PermissionPreset.is_system == True,  # noqa: E712
                )
            )).scalar_one_or_none()
            self.assertIsNotNone(night_preset, "System night preset must be seeded")
            night_preset_id = str(night_preset.id)
            night_preset_perms = night_preset.permissions

        app = web_app.create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            cookies = await self._login_as_su(client)
            r = await client.post(
                "/admin/chats/-1001/update",
                data={
                    "hashtag": "#Test",
                    "report_chat_id": "",
                    "warns_to_mute": "3",
                    "mute_duration_seconds": "3600",
                    "warns_to_ban": "5",
                    "warn_decay_days": "30",
                    "link_filter_action": "mute",
                    "night_mode_start": "22:00",
                    "night_mode_end": "06:00",
                    "night_mode_tz": "Europe/Moscow",
                    "night_preset_id": night_preset_id,
                    "sanitary_preset_id": "__lockdown__",
                    "day_preset_id": "__none__",
                    "sanitary_days_text": "",
                },
                cookies=cookies,
                follow_redirects=False,
            )
            self.assertEqual(r.status_code, 303)
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001)
            )).scalar_one()
            self.assertEqual(cs.warn_decay_days, 30)
            self.assertEqual(cs.link_filter_action, "mute")
            self.assertEqual(cs.night_mode_start, "22:00")
            self.assertEqual(cs.night_mode_end, "06:00")
            self.assertIsNotNone(cs.night_mode_permissions)
            self.assertEqual(cs.night_mode_permissions, night_preset_perms,
                             "night_mode_permissions should match the chosen preset")
            perms = json.loads(cs.night_mode_permissions)
            # "Text only" system preset: can_send_messages=True, all others False.
            self.assertTrue(perms["can_send_messages"])
            self.assertFalse(perms["can_send_audios"])

    async def test_admin_chats_update_rejects_invalid_night_mode_time(self):
        from httpx import AsyncClient, ASGITransport
        app = web_app.create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            cookies = await self._login_as_su(client)
            r = await client.post(
                "/admin/chats/-1001/update",
                data={
                    "hashtag": "",
                    "report_chat_id": "",
                    "warns_to_mute": "3",
                    "mute_duration_seconds": "3600",
                    "warns_to_ban": "5",
                    "warn_decay_days": "0",
                    "link_filter_action": "delete",
                    "night_mode_start": "25:99",  # invalid
                    "night_mode_end": "07:00",
                    "night_mode_preset": "text_only",
                },
                cookies=cookies,
                follow_redirects=False,
            )
            self.assertEqual(r.status_code, 303)
            self.assertIn("Invalid", r.headers["location"])


# ═══════════════════════════════════════════════════════════════════════════
# Тест 11: DM commands (CAS, linkfilter, nightmode, warndecay, bansticker, addword, linkallow)
# ═══════════════════════════════════════════════════════════════════════════
class TestDMCommandsV452(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await init_db()
        await _clear_all_tables()
        # Создаём тестовый чат
        async with async_session() as s:
            s.add(ChatSettings(chat_id=-1001, title="Test Chat"))
            await s.commit()

    async def _make_dm_message(self, text, from_user_id=111111111):
        """Создаёт mock DM message для тестирования команд."""
        msg = MagicMock()
        msg.text = text
        msg.reply = AsyncMock()

        chat = MagicMock()
        chat.type = "private"
        chat.id = from_user_id
        msg.chat = chat

        user = MagicMock()
        user.id = from_user_id
        user.username = "admin"
        user.first_name = "Admin"
        msg.from_user = user

        bot = MagicMock()
        bot.send_message = AsyncMock()
        msg.bot = bot
        return msg

    async def test_cmd_cas_enables(self):
        msg = await self._make_dm_message("/cas -1001 on")
        await bot_handlers.cmd_cas(msg)
        msg.reply.assert_called_once()
        # Проверяем, что флаг установлен
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001)
            )).scalar_one()
            self.assertTrue(cs.cas_check_enabled)

    async def test_cmd_cas_disables(self):
        # Сначала включаем
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001)
            )).scalar_one()
            cs.cas_check_enabled = True
            await s.commit()
        # Потом выключаем
        msg = await self._make_dm_message("/cas -1001 off")
        await bot_handlers.cmd_cas(msg)
        msg.reply.assert_called_once()
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001)
            )).scalar_one()
            self.assertFalse(cs.cas_check_enabled)

    async def test_cmd_cas_invalid_chat_id(self):
        msg = await self._make_dm_message("/cas abc on")
        await bot_handlers.cmd_cas(msg)
        msg.reply.assert_called_once()
        reply_text = msg.reply.call_args[0][0]
        self.assertIn("числом", reply_text)

    async def test_cmd_cas_non_admin_ignored(self):
        """Команда от не-ADMIN_IDS молча игнорируется (стелс)."""
        msg = await self._make_dm_message("/cas -1001 on", from_user_id=999999999)
        await bot_handlers.cmd_cas(msg)
        msg.reply.assert_not_called()

    async def test_cmd_linkfilter_enables(self):
        msg = await self._make_dm_message("/linkfilter -1001 on")
        await bot_handlers.cmd_linkfilter(msg)
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001)
            )).scalar_one()
            self.assertTrue(cs.link_filter_enabled)

    async def test_cmd_nightmode_enables_with_schedule(self):
        msg = await self._make_dm_message("/nightmode -1001 23:00 07:00 strict")
        await bot_handlers.cmd_nightmode(msg)
        msg.reply.assert_called_once()
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001)
            )).scalar_one()
            self.assertTrue(cs.night_mode_enabled)
            self.assertEqual(cs.night_mode_start, "23:00")
            self.assertEqual(cs.night_mode_end, "07:00")
            perms = json.loads(cs.night_mode_permissions)
            self.assertFalse(perms["can_send_messages"])  # strict

    async def test_cmd_nightmode_off(self):
        # Сначала включаем
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001)
            )).scalar_one()
            cs.night_mode_enabled = True
            cs.night_mode_currently_active = True
            await s.commit()
        # Выключаем
        msg = await self._make_dm_message("/nightmode -1001 off")
        await bot_handlers.cmd_nightmode(msg)
        msg.reply.assert_called_once()
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001)
            )).scalar_one()
            self.assertFalse(cs.night_mode_enabled)
            self.assertFalse(cs.night_mode_currently_active)

    async def test_cmd_nightmode_invalid_time(self):
        msg = await self._make_dm_message("/nightmode -1001 99:99 07:00")
        await bot_handlers.cmd_nightmode(msg)
        msg.reply.assert_called_once()
        reply_text = msg.reply.call_args[0][0]
        self.assertIn("некорректное", reply_text)

    async def test_cmd_warndecay_sets_days(self):
        msg = await self._make_dm_message("/warndecay -1001 30")
        await bot_handlers.cmd_warndecay(msg)
        msg.reply.assert_called_once()
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001)
            )).scalar_one()
            self.assertEqual(cs.warn_decay_days, 30)

    async def test_cmd_warndecay_zero_disables(self):
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001)
            )).scalar_one()
            cs.warn_decay_days = 30
            await s.commit()
        msg = await self._make_dm_message("/warndecay -1001 0")
        await bot_handlers.cmd_warndecay(msg)
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001)
            )).scalar_one()
            self.assertEqual(cs.warn_decay_days, 0)

    async def test_cmd_bansticker_adds_pack(self):
        msg = await self._make_dm_message("/bansticker TestPack ban")
        await bot_handlers.cmd_bansticker(msg)
        msg.reply.assert_called_once()
        async with async_session() as s:
            pack = (await s.execute(
                select(BannedStickerPack).where(BannedStickerPack.pack_name == "TestPack")
            )).scalar_one_or_none()
            self.assertIsNotNone(pack)
            self.assertEqual(pack.punishment, "ban")
            self.assertEqual(pack.chat_id, 0)  # global

    async def test_cmd_bansticker_parses_t_me_link(self):
        msg = await self._make_dm_message("/bansticker https://t.me/addstickers/MyPack delete")
        await bot_handlers.cmd_bansticker(msg)
        async with async_session() as s:
            pack = (await s.execute(
                select(BannedStickerPack).where(BannedStickerPack.pack_name == "MyPack")
            )).scalar_one_or_none()
            self.assertIsNotNone(pack)
            self.assertEqual(pack.punishment, "delete")

    async def test_cmd_bansticker_invalid_punishment(self):
        msg = await self._make_dm_message("/bansticker TestPack kick")
        await bot_handlers.cmd_bansticker(msg)
        msg.reply.assert_called_once()
        reply_text = msg.reply.call_args[0][0]
        self.assertIn("delete/warn/mute/ban", reply_text)
        # Пак не должен быть добавлен
        async with async_session() as s:
            pack = (await s.execute(
                select(BannedStickerPack).where(BannedStickerPack.pack_name == "TestPack")
            )).scalar_one_or_none()
            self.assertIsNone(pack)

    async def test_cmd_liststickers_shows_packs(self):
        async with async_session() as s:
            s.add(BannedStickerPack(chat_id=0, pack_name="Pack1", punishment="delete"))
            s.add(BannedStickerPack(chat_id=0, pack_name="Pack2", punishment="ban"))
            await s.commit()
        msg = await self._make_dm_message("/liststickers")
        await bot_handlers.cmd_liststickers(msg)
        msg.reply.assert_called_once()
        reply_text = msg.reply.call_args[0][0]
        self.assertIn("Pack1", reply_text)
        self.assertIn("Pack2", reply_text)

    async def test_cmd_liststickers_empty(self):
        msg = await self._make_dm_message("/liststickers")
        await bot_handlers.cmd_liststickers(msg)
        msg.reply.assert_called_once()
        reply_text = msg.reply.call_args[0][0]
        self.assertIn("Нет", reply_text)

    async def test_cmd_delsticker_removes_pack(self):
        async with async_session() as s:
            s.add(BannedStickerPack(chat_id=0, pack_name="Removable", punishment="delete"))
            await s.commit()
        msg = await self._make_dm_message("/delsticker Removable")
        await bot_handlers.cmd_delsticker(msg)
        msg.reply.assert_called_once()
        async with async_session() as s:
            pack = (await s.execute(
                select(BannedStickerPack).where(
                    BannedStickerPack.pack_name == "Removable",
                    BannedStickerPack.is_active.is_(True),
                )
            )).scalar_one_or_none()
            self.assertIsNone(pack)

    @unittest.skip("v4.8.6: bot-команда /addword удалена, слова ведутся через веб-панель")
    async def test_cmd_addword_creates_pattern(self):
        msg = await self._make_dm_message("/addword 0 spam delete")
        await bot_handlers.cmd_addword(msg)
        msg.reply.assert_called_once()
        async with async_session() as s:
            wf = (await s.execute(
                select(WordFilter).where(WordFilter.pattern == "spam")
            )).scalar_one_or_none()
            self.assertIsNotNone(wf)
            self.assertEqual(wf.action, "delete")
            self.assertFalse(wf.is_regex)

    @unittest.skip("v4.8.6: bot-команда /addword удалена, слова ведутся через веб-панель")
    async def test_cmd_addword_regex(self):
        msg = await self._make_dm_message("/addword 0 \\\\bcasino\\\\d+\\\\b warn 1")
        await bot_handlers.cmd_addword(msg)
        msg.reply.assert_called_once()
        async with async_session() as s:
            wf = (await s.execute(
                select(WordFilter).where(WordFilter.is_regex.is_(True))
            )).scalar_one_or_none()
            self.assertIsNotNone(wf)
            self.assertEqual(wf.action, "warn")

    @unittest.skip("v4.8.6: bot-команда /addword удалена, слова ведутся через веб-панель")
    async def test_cmd_addword_invalid_action(self):
        msg = await self._make_dm_message("/addword 0 spam kick")
        await bot_handlers.cmd_addword(msg)
        msg.reply.assert_called_once()
        reply_text = msg.reply.call_args[0][0]
        self.assertIn("delete/warn/mute/ban", reply_text)

    @unittest.skip("v4.8.6: bot-команда /delword удалена, слова ведутся через веб-панель")
    async def test_cmd_delword_removes_pattern(self):
        async with async_session() as s:
            s.add(WordFilter(chat_id=0, pattern="spam", action="delete"))
            await s.commit()
        msg = await self._make_dm_message("/delword 0 spam")
        await bot_handlers.cmd_delword(msg)
        msg.reply.assert_called_once()
        async with async_session() as s:
            wf = (await s.execute(
                select(WordFilter).where(
                    WordFilter.pattern == "spam",
                    WordFilter.is_active.is_(True),
                )
            )).scalar_one_or_none()
            self.assertIsNone(wf)

    @unittest.skip("v4.8.6: bot-команда /listwords удалена, слова ведутся через веб-панель")
    async def test_cmd_listwords_shows_patterns(self):
        async with async_session() as s:
            s.add(WordFilter(chat_id=0, pattern="spam", action="delete"))
            s.add(WordFilter(chat_id=0, pattern="scam", action="warn", is_regex=True))
            await s.commit()
        msg = await self._make_dm_message("/listwords")
        await bot_handlers.cmd_listwords(msg)
        reply_text = msg.reply.call_args[0][0]
        self.assertIn("spam", reply_text)
        self.assertIn("scam", reply_text)
        self.assertIn("regex", reply_text)

    async def test_cmd_linkallow_adds_domain(self):
        msg = await self._make_dm_message("/linkallow global my-site.ru")
        await bot_handlers.cmd_linkallow(msg)
        msg.reply.assert_called_once()
        async with async_session() as s:
            la = (await s.execute(
                select(LinkAllowlist).where(
                    LinkAllowlist.chat_id == 0,
                    LinkAllowlist.domain == "my-site.ru",
                )
            )).scalar_one_or_none()
            self.assertIsNotNone(la)

    async def test_cmd_linkallow_normalizes_url(self):
        """Если передать https://example.com/path, сохранится только домен."""
        msg = await self._make_dm_message("/linkallow global https://example.com/path")
        await bot_handlers.cmd_linkallow(msg)
        async with async_session() as s:
            la = (await s.execute(
                select(LinkAllowlist).where(LinkAllowlist.domain == "example.com")
            )).scalar_one_or_none()
            self.assertIsNotNone(la)

    async def test_cmd_linkallow_dedup(self):
        async with async_session() as s:
            s.add(LinkAllowlist(chat_id=0, domain="t.me"))
            await s.commit()
        msg = await self._make_dm_message("/linkallow global t.me")
        await bot_handlers.cmd_linkallow(msg)
        reply_text = msg.reply.call_args[0][0]
        self.assertIn("уже", reply_text)
        # Должна быть только одна запись
        async with async_session() as s:
            rows = (await s.execute(
                select(LinkAllowlist).where(
                    LinkAllowlist.chat_id == 0,
                    LinkAllowlist.domain == "t.me",
                )
            )).scalars().all()
            self.assertEqual(len(rows), 1)

    async def test_cmd_linkallowlist_shows_domains(self):
        # Перезапускаем init_db чтобы сидированный allowlist был на месте
        await init_db()
        async with async_session() as s:
            s.add(LinkAllowlist(chat_id=0, domain="custom.ru"))
            await s.commit()
        msg = await self._make_dm_message("/linkallowlist")
        await bot_handlers.cmd_linkallowlist(msg)
        reply_text = msg.reply.call_args[0][0]
        self.assertIn("custom.ru", reply_text)
        self.assertIn("t.me", reply_text)  # from seed


# ═══════════════════════════════════════════════════════════════════════════
# Тест 12: Group message handlers
# ═══════════════════════════════════════════════════════════════════════════
class TestGroupHandlersV452(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await init_db()
        await _clear_all_tables()
        async with async_session() as s:
            s.add(ChatSettings(chat_id=-1001, title="Test Chat"))
            await s.commit()

    async def test_handle_new_members_cas_disabled_passes(self):
        """CAS выключен → handle_new_members не банит."""
        msg = _make_message(
            chat_id=-1001, chat_type="supergroup",
            new_chat_members=[_make_user(12345)],
            from_user_id=999,
        )
        await bot_handlers.handle_new_members(msg)
        msg.bot.ban_chat_member.assert_not_called()

    async def test_handle_new_members_cas_enabled_bans_cas_user(self):
        """CAS включён, юзер в CAS-базе → банится."""
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001)
            )).scalar_one()
            cs.cas_check_enabled = True
            await s.commit()
        msg = _make_message(
            chat_id=-1001, chat_type="supergroup",
            new_chat_members=[_make_user(12345)],
            from_user_id=999,
        )
        with patch("bot_handlers._cas_check_user", new=AsyncMock(return_value=(True, "spam"))):
            await bot_handlers.handle_new_members(msg)
        msg.bot.ban_chat_member.assert_called_once_with(
            chat_id=-1001, user_id=12345,
        )

    async def test_handle_new_members_cas_enabled_clean_user_passes(self):
        """CAS включён, юзер чистый → не банится."""
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001)
            )).scalar_one()
            cs.cas_check_enabled = True
            await s.commit()
        msg = _make_message(
            chat_id=-1001, chat_type="supergroup",
            new_chat_members=[_make_user(12345)],
            from_user_id=999,
        )
        with patch("bot_handlers._cas_check_user", new=AsyncMock(return_value=(False, None))):
            await bot_handlers.handle_new_members(msg)
        msg.bot.ban_chat_member.assert_not_called()

    async def test_handle_sticker_message_no_pack_in_banlist_passes(self):
        """Стикер из пака, которого нет в бан-листе → пропускается."""
        msg = _make_message(
            chat_id=-1001, chat_type="supergroup",
            sticker=_make_sticker("UnknownPack"),
            from_user_id=999,
        )
        await bot_handlers.handle_sticker_message(msg)
        msg.delete.assert_not_called()
        msg.bot.ban_chat_member.assert_not_called()

    async def test_handle_sticker_message_banned_pack_deletes(self):
        """Стикер из забаненного пака → удаляется."""
        async with async_session() as s:
            s.add(BannedStickerPack(chat_id=0, pack_name="BadPack", punishment="delete"))
            await s.commit()
        msg = _make_message(
            chat_id=-1001, chat_type="supergroup",
            sticker=_make_sticker("BadPack"),
            from_user_id=999,
        )
        await bot_handlers.handle_sticker_message(msg)
        msg.delete.assert_called_once()
        msg.bot.ban_chat_member.assert_not_called()  # punishment=delete, not ban

    async def test_handle_sticker_message_banned_pack_ban_punishment(self):
        """Стикер из забаненного пака с punishment=ban → бан."""
        async with async_session() as s:
            s.add(BannedStickerPack(chat_id=0, pack_name="BadPack", punishment="ban"))
            await s.commit()
        msg = _make_message(
            chat_id=-1001, chat_type="supergroup",
            sticker=_make_sticker("BadPack"),
            from_user_id=999,
        )
        await bot_handlers.handle_sticker_message(msg)
        msg.delete.assert_called_once()
        msg.bot.ban_chat_member.assert_called_once_with(
            chat_id=-1001, user_id=999,
        )

    async def test_handle_sticker_message_anonymous_sticker_passes(self):
        """Анонимный стикер (без set_name) → пропускается."""
        sticker = MagicMock()
        sticker.set_name = None
        sticker.emoji = "😀"
        msg = _make_message(
            chat_id=-1001, chat_type="supergroup",
            sticker=sticker, from_user_id=999,
        )
        await bot_handlers.handle_sticker_message(msg)
        msg.delete.assert_not_called()

    async def test_handle_content_filters_no_filters_passes(self):
        """Нет word filter и link filter выкл → пропускается."""
        msg = _make_message(
            chat_id=-1001, chat_type="supergroup",
            text="Hello world", from_user_id=999,
        )
        await bot_handlers.handle_content_filters(msg)
        msg.delete.assert_not_called()

    @unittest.skip("v4.8.1: word_filter заменён на KeywordWatch, функции больше нет")
    async def test_handle_content_filters_word_filter_match_deletes(self):
        """Word filter match → удаляет сообщение."""
        async with async_session() as s:
            s.add(WordFilter(chat_id=0, pattern="spam", action="delete"))
            await s.commit()
        msg = _make_message(
            chat_id=-1001, chat_type="supergroup",
            text="This is spam message", from_user_id=999,
        )
        await bot_handlers.handle_content_filters(msg)
        msg.delete.assert_called_once()

    async def test_handle_content_filters_link_filter_match_deletes(self):
        """Link filter on + ссылка не из allowlist → удаляет."""
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001)
            )).scalar_one()
            cs.link_filter_enabled = True
            cs.link_filter_action = "delete"
            await s.commit()
        msg = _make_message(
            chat_id=-1001, chat_type="supergroup",
            text="Buy at scam-site.ru now", from_user_id=999,
        )
        await bot_handlers.handle_content_filters(msg)
        msg.delete.assert_called_once()

    async def test_handle_content_filters_link_filter_allowlist_passes(self):
        """Link filter on, но ссылка из allowlist → пропускается."""
        await init_db()  # перезапускаем для сида allowlist
        async with async_session() as s:
            cs = (await s.execute(
                select(ChatSettings).where(ChatSettings.chat_id == -1001)
            )).scalar_one()
            cs.link_filter_enabled = True
            await s.commit()
        msg = _make_message(
            chat_id=-1001, chat_type="supergroup",
            text="Visit t.me/foo", from_user_id=999,
        )
        await bot_handlers.handle_content_filters(msg)
        msg.delete.assert_not_called()

    async def test_handle_content_filters_word_filter_ban_action(self):
        """Word filter с action=ban → банит юзера."""
        async with async_session() as s:
            s.add(WordFilter(chat_id=0, pattern="banned_word", action="ban"))
            await s.commit()
        msg = _make_message(
            chat_id=-1001, chat_type="supergroup",
            text="this is banned_word text", from_user_id=999,
        )
        await bot_handlers.handle_content_filters(msg)
        msg.delete.assert_called_once()
        msg.bot.ban_chat_member.assert_called_once_with(
            chat_id=-1001, user_id=999,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
