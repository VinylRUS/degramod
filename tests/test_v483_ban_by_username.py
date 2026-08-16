"""v4.8.3: Тесты резолва цели наказания (@username, TGID, reply).

Проверяет:
  • Regex-паттерны для 6 команд (!ban, !sban, !warn, !swarn, !mute, !smute)
    с опциональным target-группой.
  • Хелпер _resolve_punishment_target:
    - reply приоритетнее @username/TGID.
    - @username → БД User.username.
    - @username через MessageEntityTextMention (inline-mention с user.id).
    - TGID → bot.get_chat_member.
    - Ошибки: @unknown, невалидный TGID, нет цели.

Запуск: python scripts/test_v483_ban_by_username.py
"""

import asyncio
import sys
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from types import SimpleNamespace

# Добавляем корень проекта в sys.path
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
sys.path.insert(0, _PROJECT_ROOT)
# v4.8.6: используем актуальную рабочую папку v485_work (раньше было v483_work).
# Если v485_work не существует (старый запуск) — fallback на v483_work.
_V483_WORK = os.path.join(_PROJECT_ROOT, "v485_work")
if not os.path.isdir(_V483_WORK):
    _V483_WORK = os.path.join(_PROJECT_ROOT, "v483_work")
sys.path.insert(0, _V483_WORK)


class TestRegexPatterns(unittest.TestCase):
    """Проверка regex-паттернов для 6 команд с target-группой."""

    def setUp(self):
        # Импортируем модуль bot_handlers — нужны только константы _CMD_*.
        # Если импорт падает из-за aiogram — используем прямое чтение regex'ов.
        try:
            import bot_handlers as bh  # type: ignore
            self._CMD_BAN = bh._CMD_BAN
            self._CMD_SBAN = bh._CMD_SBAN
            self._CMD_WARN = bh._CMD_WARN
            self._CMD_SWARN = bh._CMD_SWARN
            self._CMD_MUTE = bh._CMD_MUTE
            self._CMD_SMUTE = bh._CMD_SMUTE
        except ImportError:
            self.skipTest("aiogram not installed — skip regex import test")

    def test_ban_with_username_target(self):
        """!ban @username reason → target=@username, reason=reason."""
        m = self._CMD_BAN.match("!ban @narushitel Дурачок")
        self.assertIsNotNone(m)
        self.assertEqual(m.group("target"), "@narushitel")
        self.assertEqual(m.group("reason"), "Дурачок")

    def test_ban_with_tgid_target(self):
        """!ban 12345678 reason → target='12345678', reason=reason."""
        m = self._CMD_BAN.match("!ban 12345678 Дурачок")
        self.assertIsNotNone(m)
        self.assertEqual(m.group("target"), "12345678")
        self.assertEqual(m.group("reason"), "Дурачок")

    def test_ban_without_target_only_reason(self):
        """!ban reason (без target) → target=None, reason=reason (для reply)."""
        m = self._CMD_BAN.match("!ban Дурачок")
        self.assertIsNotNone(m)
        self.assertIsNone(m.group("target"))
        self.assertEqual(m.group("reason"), "Дурачок")

    def test_ban_no_args_no_match(self):
        """!ban без аргументов → regex не матчит (причина обязательна)."""
        m = self._CMD_BAN.match("!ban")
        self.assertIsNone(m)

    def test_ban_only_target_no_reason_no_match(self):
        """!ban @username без причины → regex не матчит (причина обязательна)."""
        m = self._CMD_BAN.match("!ban @username")
        self.assertIsNone(m)

    def test_sban_with_username_target(self):
        """!sban @username reason → target, reason."""
        m = self._CMD_SBAN.match("!sban @narushitel Дурка")
        self.assertIsNotNone(m)
        self.assertEqual(m.group("target"), "@narushitel")
        self.assertEqual(m.group("reason"), "Дурка")

    def test_sban_only_target_no_reason_ok(self):
        """!sban @username без причины → OK (тихая команда, причина опц.)."""
        m = self._CMD_SBAN.match("!sban @username")
        self.assertIsNotNone(m)
        self.assertEqual(m.group("target"), "@username")
        # reason может быть None
        self.assertFalse(m.group("reason"))

    def test_sban_no_args_ok(self):
        """!sban без аргументов → OK (если есть reply)."""
        m = self._CMD_SBAN.match("!sban")
        self.assertIsNotNone(m)
        self.assertIsNone(m.group("target"))
        self.assertFalse(m.group("reason"))

    def test_warn_with_tgid_target(self):
        """!warn 12345678 reason → target, reason."""
        m = self._CMD_WARN.match("!warn 12345678 Спам")
        self.assertIsNotNone(m)
        self.assertEqual(m.group("target"), "12345678")
        self.assertEqual(m.group("reason"), "Спам")

    def test_swarn_with_username_target(self):
        m = self._CMD_SWARN.match("!swarn @user Причина")
        self.assertIsNotNone(m)
        self.assertEqual(m.group("target"), "@user")
        self.assertEqual(m.group("reason"), "Причина")

    def test_swarn_no_args_ok(self):
        m = self._CMD_SWARN.match("!swarn")
        self.assertIsNotNone(m)

    def test_mute_with_target(self):
        """!mute @user 1h reason → target, dur, reason."""
        m = self._CMD_MUTE.match("!mute @user 1h Спам")
        self.assertIsNotNone(m)
        self.assertEqual(m.group("target"), "@user")
        self.assertEqual(m.group("dur"), "1h")
        self.assertEqual(m.group("reason"), "Спам")

    def test_mute_without_target(self):
        """!mute 1h reason (без target) → target=None, dur, reason."""
        m = self._CMD_MUTE.match("!mute 1h Спам")
        self.assertIsNotNone(m)
        self.assertIsNone(m.group("target"))
        self.assertEqual(m.group("dur"), "1h")
        self.assertEqual(m.group("reason"), "Спам")

    def test_mute_no_target_no_dur_no_match(self):
        m = self._CMD_MUTE.match("!mute")
        self.assertIsNone(m)

    def test_smute_with_target(self):
        m = self._CMD_SMUTE.match("!smute @user 1h Причина")
        self.assertIsNotNone(m)
        self.assertEqual(m.group("target"), "@user")
        self.assertEqual(m.group("dur"), "1h")
        self.assertEqual(m.group("reason"), "Причина")

    def test_smute_only_target_dur(self):
        """!smute @user 1h (без причины) → OK (тихая)."""
        m = self._CMD_SMUTE.match("!smute @user 1h")
        self.assertIsNotNone(m)
        self.assertEqual(m.group("target"), "@user")
        self.assertEqual(m.group("dur"), "1h")
        self.assertFalse(m.group("reason"))

    def test_smute_no_args_ok(self):
        m = self._CMD_SMUTE.match("!smute")
        self.assertIsNotNone(m)


class TestResolvePunishmentTarget(unittest.TestCase):
    """Проверка хелпера _resolve_punishment_target."""

    def setUp(self):
        try:
            import bot_handlers as bh  # type: ignore
            self.bh = bh
        except ImportError:
            self.skipTest("aiogram not installed")

    def _make_message(
        self,
        text: str = "",
        reply_to_message=None,
        entities=None,
        caption_entities=None,
        caption=None,
        bot=None,
    ):
        msg = MagicMock()
        msg.text = text or None
        msg.caption = caption
        msg.entities = entities or []
        msg.caption_entities = caption_entities or []
        msg.reply_to_message = reply_to_message
        msg.bot = bot or MagicMock()
        msg.chat = MagicMock(id=-1001234567890)
        return msg

    def _make_reply(self, user_id=12345, username="target_user", first_name="Target"):
        reply = MagicMock()
        reply.from_user = SimpleNamespace(
            id=user_id, username=username, first_name=first_name,
            last_name="", is_bot=False,
        )
        return reply

    def test_reply_priority_over_target_str(self):
        """Если есть reply — target_str игнорируется, цель = reply.from_user."""
        reply = self._make_reply(user_id=999, username="reply_user")
        msg = self._make_message(text="!ban @someone reason", reply_to_message=reply)

        target, err = asyncio.run(self.bh._resolve_punishment_target(msg, "@someone", -100))

        self.assertIsNone(err)
        self.assertEqual(target.id, 999)
        self.assertEqual(target.username, "reply_user")

    def test_no_reply_no_target_str_error(self):
        """Нет reply и нет target_str → ошибка."""
        msg = self._make_message(text="!ban reason")
        target, err = asyncio.run(self.bh._resolve_punishment_target(msg, None, -100))
        self.assertIsNone(target)
        self.assertIn("Не указана цель", err)

    def test_tgid_resolution_via_get_chat_member(self):
        """TGID → bot.get_chat_member → возвращает user."""
        # Мокаем bot.get_chat_member
        target_user = SimpleNamespace(
            id=12345678, username="found_user", first_name="Found",
            last_name="", is_bot=False,
        )
        member = MagicMock()
        member.user = target_user
        bot = MagicMock()
        bot.get_chat_member = AsyncMock(return_value=member)

        msg = self._make_message(text="!ban 12345678 reason", bot=bot)

        target, err = asyncio.run(self.bh._resolve_punishment_target(msg, "12345678", -100))

        self.assertIsNone(err)
        self.assertEqual(target.id, 12345678)
        self.assertEqual(target.username, "found_user")
        # Проверяем что get_chat_member был вызван с правильными аргументами.
        bot.get_chat_member.assert_called_once()

    def test_tgid_not_in_chat_error(self):
        """TGID не найден в чате → ошибка."""
        # v4.8.6: код ловит TelegramAPIError, не общий Exception.
        # Используем реальный класс ошибки aiogram 3.30+.
        try:
            from aiogram.exceptions import TelegramAPIError
            # Signature: TelegramAPIError(method, message). method не используется
            # при обработке — подойдёт любой объект.
            err_exc = TelegramAPIError(method=None, message="user not found")
        except (ImportError, TypeError):
            err_exc = Exception("user not found")
        bot = MagicMock()
        bot.get_chat_member = AsyncMock(side_effect=err_exc)

        # Используем TelegramAPIError если возможно, иначе общая Exception.
        msg = self._make_message(text="!ban 99999999 reason", bot=bot)

        target, err = asyncio.run(self.bh._resolve_punishment_target(msg, "99999999", -100))
        self.assertIsNone(target)
        self.assertIn("99999999", err)

    def test_tgid_zero_or_negative_error(self):
        """TGID <= 0 → ошибка."""
        msg = self._make_message(text="!ban 0 reason")

        target, err = asyncio.run(self.bh._resolve_punishment_target(msg, "0", -100))
        self.assertIsNone(target)
        self.assertIn("положительным", err)

        target, err = asyncio.run(self.bh._resolve_punishment_target(msg, "-5", -100))
        self.assertIsNone(target)
        # -5 не digit → не распознано.
        self.assertIn("распознать", err)

    def test_username_resolution_via_entities_text_mention(self):
        """@username через MessageEntityTextMention (inline-mention с user.id)."""
        target_user = SimpleNamespace(
            id=55555, username="inline_user", first_name="Inline",
            last_name="", is_bot=False,
        )
        ent = MagicMock()
        ent.type = "text_mention"
        ent.user = target_user
        ent.offset = 5  # после "!ban "
        ent.length = 9  # "@username" длина

        msg = self._make_message(
            text="!ban @username Дурачок",
            entities=[ent],
        )

        target, err = asyncio.run(self.bh._resolve_punishment_target(msg, "@username", -100))

        self.assertIsNone(err)
        self.assertEqual(target.id, 55555)

    def test_username_resolution_via_db(self):
        """@username → ищем в БД User.username (без entities)."""
        # Мокаем async_session и User
        db_user = MagicMock()
        db_user.user_id = 88888
        db_user.username = "dbuser"
        db_user.first_name = "DB"
        db_user.last_name = "User"

        # Патчим async_session
        session = MagicMock()
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=db_user)
        session.execute = AsyncMock(return_value=result)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=None)

        with patch.object(self.bh, "async_session", return_value=session):
            msg = self._make_message(text="!ban @dbuser reason")
            target, err = asyncio.run(self.bh._resolve_punishment_target(msg, "@dbuser", -100))

        self.assertIsNone(err)
        self.assertEqual(target.id, 88888)
        self.assertEqual(target.username, "dbuser")

    def test_username_not_in_db_error(self):
        """@username не найден в БД → ошибка."""
        session = MagicMock()
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=None)
        session.execute = AsyncMock(return_value=result)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=None)

        with patch.object(self.bh, "async_session", return_value=session):
            msg = self._make_message(text="!ban @unknown reason")
            target, err = asyncio.run(self.bh._resolve_punishment_target(msg, "@unknown", -100))

        self.assertIsNone(target)
        self.assertIn("@unknown", err)
        self.assertIn("не найден", err)

    def test_unrecognized_target_str_error(self):
        """target_str не @username и не digit → ошибка."""
        msg = self._make_message(text="!ban #weird reason")
        target, err = asyncio.run(self.bh._resolve_punishment_target(msg, "#weird", -100))
        self.assertIsNone(target)
        self.assertIn("распознать", err)


class TestModerationCommandFilterWithCaption(unittest.TestCase):
    """Проверка что _ModerationCommandFilter пропускает caption-команды."""

    def setUp(self):
        try:
            import bot_handlers as bh  # type: ignore
            self.bh = bh
        except ImportError:
            self.skipTest("aiogram not installed")

    def test_filter_text_command(self):
        """text содержит команду → filter возвращает True."""
        f = self.bh._ModerationCommandFilter()
        msg = MagicMock()
        msg.text = "!ban @user reason"
        msg.caption = None
        result = asyncio.run(f(msg))
        self.assertTrue(result)

    def test_filter_caption_command(self):
        """caption содержит команду, text=None → filter возвращает True (v4.8.3)."""
        f = self.bh._ModerationCommandFilter()
        msg = MagicMock()
        msg.text = None
        msg.caption = "!ban @user reason"
        result = asyncio.run(f(msg))
        self.assertTrue(result)

    def test_filter_no_text_no_caption(self):
        """Ни text, ни caption не содержат команду → False."""
        f = self.bh._ModerationCommandFilter()
        msg = MagicMock()
        msg.text = None
        msg.caption = None
        result = asyncio.run(f(msg))
        self.assertFalse(result)

    def test_filter_text_not_command(self):
        """text — обычное сообщение, не команда → False."""
        f = self.bh._ModerationCommandFilter()
        msg = MagicMock()
        msg.text = "Привет всем!"
        msg.caption = None
        result = asyncio.run(f(msg))
        self.assertFalse(result)


def run_all_tests():
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(TestRegexPatterns))
    suite.addTests(loader.loadTestsFromTestCase(TestResolvePunishmentTarget))
    suite.addTests(loader.loadTestsFromTestCase(TestModerationCommandFilterWithCaption))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return result.wasSuccessful()


if __name__ == "__main__":
    ok = run_all_tests()
    sys.exit(0 if ok else 1)
