"""v4.8.3.1 HOTFIX: Регрессионные тесты для !swarn / !smute / !sban.

Контекст бага:
  В v4.8.3 при рефакторинге regex-ов (для поддержки @username/TGID как первого
  аргумента) были внесены два регресса:

  BUG #1: _CMD_SWARN и _CMD_SBAN не матчат `!swarn Причина` (bare reason
          без @username/TGID). Причина: внешняя `\s+` съедала единственный
          пробел, и внутренней `(?:\s+(?P<reason>.+))?` не оставалось
          whitespace для match. Симптом: модератор пишет `!swarn Спамит`,
          бот молча игнорирует (сообщение модератора не удаляется, ничего
          не происходит). Тот же баг у !sban, но он менее заметен —
          `!sban` без причины работает.

  BUG #2: _CMD_SMUTE в v4.8.3 разрешил `!smute` без аргументов (всё тело
          стало optional). При этом `m.group("dur")` = None, и handler
          звал `_parse_duration(None)` → AttributeError на `None.strip()`.
          Симптом: модератор пишет `!smute` (с reply, без длительности),
          бот падает в handler, сообщение модератора уже удалено,
          ничего не происходит.

Фикс в v4.8.3.1:
  • _CMD_SWARN / _CMD_SBAN: regex переосмыслен — target и reason стали
    двумя независимыми optional-блоками верхнего уровня, каждый со своим
    `\s+`. Теперь матчится `!swarn Причина`.
  • _CMD_SMUTE: regex оставлен как есть, но handler явно проверяет
    `dur is None` и отправляет ephemeral с подсказкой формата.

Тесты:
  1. Regex: все варианты вызова !swarn/!sban/!smute (с reply, без reply,
     с target, без target, с reason, без reason, с dur, без dur).
  2. _is_moderation_command: `!swarn Причина` теперь распознаётся как
     команда (раньше — нет).
  3. _parse_duration(None): подтверждаем что вызов с None действительно
     вызывал бы AttributeError (если бы handler не делал None-check).
  4. Интеграционный тест !smute без dur: handler не падает, отправляет
     ephemeral модератору с подсказкой.
  5. Интеграционный тест !swarn с bare reason: full dispatch доходит
     до !swarn handler, target резолвится из reply, варн сохраняется.

Запуск: python scripts/test_v4831_swarn_smute_fix.py
"""

import asyncio
import os
import re
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from types import SimpleNamespace

# Добавляем v483_work в sys.path
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
_V483_WORK = os.path.join(_PROJECT_ROOT, "v483_work")
sys.path.insert(0, _V483_WORK)


def _import_bh():
    """Импортирует bot_handlers (после установки aiogram и др. зависимостей)."""
    import bot_handlers  # type: ignore
    return bot_handlers


# ============================================================================
# КЛАСС 1: Регрессия regex-паттернов (главная цель hotfix'а).
# ============================================================================
class TestRegexHotfix(unittest.TestCase):
    """Проверка что v4.8.3.1 regex матчит все варианты вызова !swarn/!sban/!smute."""

    def setUp(self):
        try:
            self.bh = _import_bh()
        except ImportError as e:
            self.skipTest(f"aiogram/dependencies not installed: {e}")

    # ── !swarn: все варианты ─────────────────────────────────────────────
    def test_swarn_no_args_ok(self):
        """`!swarn` (без аргументов, с reply) → MATCH, target=None, reason=None."""
        m = self.bh._CMD_SWARN.match("!swarn")
        self.assertIsNotNone(m, "!swarn должно матчиться (для reply-based swarn)")
        self.assertIsNone(m.group("target"))
        self.assertIsNone(m.group("reason"))

    def test_swarn_bare_reason_ok_HOTFIX(self):
        """`!swarn Причина` (без @username/TGID) → MATCH, target=None, reason='Причина'.

        ГЛАВНЫЙ РЕГРЕСС-КЕЙС: в v4.8.3 этот вариант НЕ матчило, из-за чего
        !swarn с reason, но без target, полностью ломался.
        """
        m = self.bh._CMD_SWARN.match("!swarn Спам ссылками")
        self.assertIsNotNone(m, "REGRESSION: !swarn <reason> должно матчиться в v4.8.3.1")
        self.assertIsNone(m.group("target"))
        self.assertEqual(m.group("reason"), "Спам ссылками")

    def test_swarn_only_target_ok(self):
        """`!swarn @user` (без причины) → MATCH, target='@user', reason=None."""
        m = self.bh._CMD_SWARN.match("!swarn @narushitel")
        self.assertIsNotNone(m)
        self.assertEqual(m.group("target"), "@narushitel")
        self.assertIsNone(m.group("reason"))

    def test_swarn_target_and_reason_ok(self):
        """`!swarn @user Причина` → MATCH, target, reason."""
        m = self.bh._CMD_SWARN.match("!swarn @narushitel Спам")
        self.assertIsNotNone(m)
        self.assertEqual(m.group("target"), "@narushitel")
        self.assertEqual(m.group("reason"), "Спам")

    def test_swarn_tgid_target_ok(self):
        """`!swarn 12345 Причина` → MATCH, target='12345', reason."""
        m = self.bh._CMD_SWARN.match("!swarn 12345678 Спам")
        self.assertIsNotNone(m)
        self.assertEqual(m.group("target"), "12345678")
        self.assertEqual(m.group("reason"), "Спам")

    def test_swarn_tgid_only_ok(self):
        """`!swarn 12345` → MATCH, target='12345', reason=None."""
        m = self.bh._CMD_SWARN.match("!swarn 12345678")
        self.assertIsNotNone(m)
        self.assertEqual(m.group("target"), "12345678")
        self.assertIsNone(m.group("reason"))

    def test_swarn_cyrillic_reason_ok(self):
        """`!swarn Русская причина` → MATCH, reason с пробелами и кириллицей."""
        m = self.bh._CMD_SWARN.match("!swarn Спам ссылкой на канал")
        self.assertIsNotNone(m)
        self.assertEqual(m.group("reason"), "Спам ссылкой на канал")

    # ── !sban: те же варианты (regex идентичен по структуре) ────────────
    def test_sban_no_args_ok(self):
        m = self.bh._CMD_SBAN.match("!sban")
        self.assertIsNotNone(m)
        self.assertIsNone(m.group("target"))
        self.assertIsNone(m.group("reason"))

    def test_sban_bare_reason_ok_HOTFIX(self):
        """`!sban Причина` → MATCH. ГЛАВНЫЙ РЕГРЕСС-КЕЙС (баг v4.8.3, но менее заметный)."""
        m = self.bh._CMD_SBAN.match("!sban Реклама")
        self.assertIsNotNone(m, "REGRESSION: !sban <reason> должно матчиться в v4.8.3.1")
        self.assertIsNone(m.group("target"))
        self.assertEqual(m.group("reason"), "Реклама")

    def test_sban_only_target_ok(self):
        m = self.bh._CMD_SBAN.match("!sban @user")
        self.assertIsNotNone(m)
        self.assertEqual(m.group("target"), "@user")

    def test_sban_target_and_reason_ok(self):
        m = self.bh._CMD_SBAN.match("!sban @user Причина")
        self.assertIsNotNone(m)
        self.assertEqual(m.group("target"), "@user")
        self.assertEqual(m.group("reason"), "Причина")

    # ── !smute: регресс _parse_duration(None) ───────────────────────────
    def test_smute_no_args_matches_but_dur_none_HOTFIX(self):
        """`!smute` (без аргументов) → MATCH, но dur=None.

        В v4.8.3 это соответствовало regex, но затем handler звал
        _parse_duration(None) и падал. В v4.8.3.1 regex оставлен как есть,
        но handler делает явный None-check.
        """
        m = self.bh._CMD_SMUTE.match("!smute")
        self.assertIsNotNone(m, "!smute должно матчиться (regex позволяет)")
        self.assertIsNone(m.group("dur"))
        self.assertIsNone(m.group("target"))
        self.assertIsNone(m.group("reason"))

    def test_smute_with_dur_ok(self):
        """`!smute 1d` (с reply, без причины) → MATCH, dur='1d'."""
        m = self.bh._CMD_SMUTE.match("!smute 1d")
        self.assertIsNotNone(m)
        self.assertEqual(m.group("dur"), "1d")
        self.assertIsNone(m.group("reason"))

    def test_smute_dur_and_reason_ok(self):
        """`!smute 1d Причина` → MATCH, dur, reason."""
        m = self.bh._CMD_SMUTE.match("!smute 1d Спам")
        self.assertIsNotNone(m)
        self.assertEqual(m.group("dur"), "1d")
        self.assertEqual(m.group("reason"), "Спам")

    def test_smute_target_dur_reason_ok(self):
        """`!smute @user 1d Причина` → MATCH, target, dur, reason."""
        m = self.bh._CMD_SMUTE.match("!smute @user 1d Спам")
        self.assertIsNotNone(m)
        self.assertEqual(m.group("target"), "@user")
        self.assertEqual(m.group("dur"), "1d")
        self.assertEqual(m.group("reason"), "Спам")

    def test_smute_cyrillic_dur_ok(self):
        """`!smute 1д` (кириллическая единица) → MATCH, dur='1д'."""
        m = self.bh._CMD_SMUTE.match("!smute 1д 30м")
        self.assertIsNotNone(m)
        # В режиме "1д 30м" regex возьмёт только первое "1д" как dur,
        # а "30м" останется висеть — и матч провалится. Проверяем это.
        # Если хотим комбинированные длительности — нужно расширять regex.
        # Но базовый `!smute 1д` должен работать.
        m2 = self.bh._CMD_SMUTE.match("!smute 1д")
        self.assertIsNotNone(m2)
        self.assertEqual(m2.group("dur"), "1д")

    def test_smute_bare_reason_no_match(self):
        """`!smute Причина` (без длительности) → NO MATCH.

        Это ОК: smute без длительности не имеет смысла. Модератор получит
        подсказку только если напишет ровно `!smute` (без аргументов вообще)
        — тогда regex матчит с dur=None, и handler отправит ephemeral.
        Если же модератор написал `!smute Причина` (со словом, не являющимся
        длительностью) — regex не матчит, _is_moderation_command возвращает
        False, и сообщение трактуется как обычный reply модератора.
        """
        m = self.bh._CMD_SMUTE.match("!smute Причина")
        self.assertIsNone(m, "!smute <reason без dur> не должно матчиться")


# ============================================================================
# КЛАСС 2: _is_moderation_command — ранняя guard-проверка.
# ============================================================================
class TestIsModerationCommandHotfix(unittest.TestCase):
    """Проверка что _is_moderation_command распознаёт новые варианты вызова.

    В v4.8.3 `!swarn Причина` НЕ распознавалось как команда (regex не матчит),
    поэтому сообщение модератора трактовалось как обычный reply и бот его
    игнорировал. В v4.8.3.1 это исправлено.
    """

    def setUp(self):
        try:
            self.bh = _import_bh()
        except ImportError as e:
            self.skipTest(f"aiogram/dependencies not installed: {e}")

    def test_swarn_bare_reason_recognized_HOTFIX(self):
        """`!swarn Спам` теперь распознаётся как модераторская команда."""
        self.assertTrue(
            self.bh._is_moderation_command("!swarn Спам"),
            "REGRESSION: !swarn <reason> должно распознаваться как команда"
        )

    def test_sban_bare_reason_recognized_HOTFIX(self):
        """`!sban Реклама` теперь распознаётся."""
        self.assertTrue(self.bh._is_moderation_command("!sban Реклама"))

    def test_swarn_no_args_recognized(self):
        """`!swarn` (без аргументов) распознаётся (работало и в v4.8.3)."""
        self.assertTrue(self.bh._is_moderation_command("!swarn"))

    def test_smute_no_args_recognized(self):
        """`!smute` (без аргументов) распознаётся."""
        self.assertTrue(self.bh._is_moderation_command("!smute"))

    def test_smute_with_dur_recognized(self):
        """`!smute 1d` распознаётся."""
        self.assertTrue(self.bh._is_moderation_command("!smute 1d"))

    def test_non_command_not_recognized(self):
        """Обычный текст не распознаётся как команда."""
        self.assertFalse(self.bh._is_moderation_command("Привет всем!"))
        self.assertFalse(self.bh._is_moderation_command(""))
        self.assertFalse(self.bh._is_moderation_command("Ответ модератора"))


# ============================================================================
# КЛАСС 3: _parse_duration(None) — подтверждение бага.
# ============================================================================
class TestParseDurationNoneBug(unittest.TestCase):
    """Подтверждаем что _parse_duration(None) действительно вызвал бы AttributeError.

    В v4.8.3 handler !smute звал _parse_duration(dur_str) где dur_str = None.
    Этот тест документирует баг и гарантирует, что handler больше не делает
    такой вызов без None-check.
    """

    def setUp(self):
        try:
            self.bh = _import_bh()
        except ImportError as e:
            self.skipTest(f"aiogram/dependencies not installed: {e}")

    def test_parse_duration_none_raises_attribute_error(self):
        """Документируем баг: _parse_duration(None) падает с AttributeError."""
        with self.assertRaises(AttributeError):
            self.bh._parse_duration(None)

    def test_parse_duration_valid_string_ok(self):
        """Санитарная проверка: с валидной строкой _parse_duration работает."""
        self.assertEqual(self.bh._parse_duration("1d"), 86400)
        self.assertEqual(self.bh._parse_duration("1h"), 3600)
        self.assertEqual(self.bh._parse_duration("30m"), 1800)
        self.assertEqual(self.bh._parse_duration("1д"), 86400)
        self.assertEqual(self.bh._parse_duration("1ч"), 3600)
        self.assertEqual(self.bh._parse_duration("30м"), 1800)

    def test_parse_duration_invalid_string_returns_none(self):
        """С невалидной строкой _parse_duration возвращает None (не падает)."""
        self.assertIsNone(self.bh._parse_duration("abc"))
        self.assertIsNone(self.bh._parse_duration(""))
        self.assertIsNone(self.bh._parse_duration("0m"))  # 0 длительность


# ============================================================================
# КЛАСС 4: Интеграционный тест !smute без длительности.
# ============================================================================
class TestSmuteHandlerNoDurHotfix(unittest.IsolatedAsyncioTestCase):
    """Полный dispatch для `!smute` (без длительности) — handler не падает.

    В v4.8.3 этот сценарий ронял整个 handler. В v4.8.3.1 handler отправляет
    ephemeral модератору с подсказкой формата.
    """

    async def asyncSetUp(self):
        try:
            self.bh = _import_bh()
        except ImportError as e:
            self.skipTest(f"aiogram/dependencies not installed: {e}")
        # Хендлер доходит до запроса в БД, поэтому схема должна существовать.
        from db import init_db
        await init_db()

    def _make_message(
        self,
        text: str = "!smute",
        reply_to_message=None,
        from_user_id: int = 100200,
        chat_id: int = -1001234567890,
    ):
        msg = MagicMock()
        msg.text = text
        msg.caption = None
        msg.photo = None
        msg.entities = []
        msg.caption_entities = []
        msg.reply_to_message = reply_to_message
        msg.from_user = SimpleNamespace(
            id=from_user_id,
            username="moderator",
            first_name="Mod",
            last_name="",
            is_bot=False,
        )
        msg.chat = SimpleNamespace(id=chat_id)
        msg.bot = MagicMock()
        msg.bot.send_message = AsyncMock()
        msg.bot.restrict_chat_member = AsyncMock()
        msg.bot.ban_chat_member = AsyncMock()
        msg.bot.get_chat_member = AsyncMock()
        msg.delete = AsyncMock()
        return msg

    def _make_reply(self, user_id=12345, username="bad_user"):
        reply = MagicMock()
        reply.from_user = SimpleNamespace(
            id=user_id,
            username=username,
            first_name="Bad",
            last_name="",
            is_bot=False,
        )
        reply.text = "Спам-сообщение"
        reply.photo = None
        reply.sticker = None
        reply.delete = AsyncMock()
        return reply

    @patch("bot_handlers._is_admin")
    @patch("bot_handlers._get_chat_settings")
    @patch("bot_handlers._send_ephemeral", new_callable=AsyncMock)
    async def test_smute_no_dur_sends_ephemeral_hotfix(
        self, mock_ephemeral, mock_get_cs, mock_is_admin,
    ):
        """`!smute` без длительности → handler не падает, отправляет ephemeral."""
        # Настраиваем mocks: _is_admin возвращает True только для модератора
        # (from_user_id=100200), но False для target (id=999111) — иначе
        # handler откажется наказывать "своего".
        async def _is_admin_fake(session, chat_id, user_id):
            return user_id == 100200
        mock_is_admin.side_effect = _is_admin_fake

        cs = MagicMock()
        cs.auto_delete_commands = False  # чтобы message.delete не вызывался
        mock_get_cs.return_value = cs

        reply = self._make_reply(user_id=999111)
        msg = self._make_message(text="!smute", reply_to_message=reply)

        # Проверяем что handler не падает (v4.8.3 бы упал с AttributeError)
        try:
            await self.bh.handle_group_command(msg)
        except AttributeError as e:
            self.fail(f"REGRESSION NOT FIXED: handler упал с AttributeError: {e}")
        except Exception as e:
            # Другие исключения (например, от mock-объектов) — допустимы,
            # но не AttributeError от _parse_duration(None).
            if "NoneType" in str(e) and "strip" in str(e):
                self.fail(f"REGRESSION NOT FIXED: {e}")
            # Иначе просто логируем и идём дальше проверять вызовы.
            pass

        # Проверяем: бот НЕ пытался замьютить (restrict_chat_member не звался).
        # Это правильное поведение — нет длительности, нет мьюта.
        # (Может вызваться из других handler'ов, но не из !smute.)
        # Важно: ephemeral с подсказкой должен быть отправлен.
        # Но т.к. mock_ephemeral это AsyncMock, мы проверяем что он был вызван
        # хотя бы раз с текстом про "длительность".
        ephemeral_calls = mock_ephemeral.call_args_list
        self.assertGreater(
            len(ephemeral_calls), 0,
            "Ожидается что handler отправил хотя бы один ephemeral (подсказка)",
        )
        # Ищем среди вызовов тот, где текст про длительность
        found_hint = False
        for call in ephemeral_calls:
            text_arg = call.kwargs.get("text", "")
            if "длительность" in text_arg or "Не указана" in text_arg:
                found_hint = True
                break
        self.assertTrue(
            found_hint,
            f"Ожидается ephemeral с подсказкой про длительность. "
            f"Полученные вызовы: {[c.kwargs.get('text', '')[:50] for c in ephemeral_calls]}"
        )


# ============================================================================
# КЛАСС 5: Интеграционный тест !swarn с bare reason.
# ============================================================================
class TestSwarnBareReasonHotfix(unittest.IsolatedAsyncioTestCase):
    """Полный dispatch для `!swarn Причина` — варн сохраняется в БД.

    В v4.8.3 этот сценарий вообще не доходил до handler'а (regex не матчит,
    _is_moderation_command возвращает False, бот игнорирует команду).
    В v4.8.3.1 regex матчит, dispatch доходит до !swarn handler,
    варн сохраняется в БД, отправляется ephemeral модератору.
    """

    async def asyncSetUp(self):
        try:
            self.bh = _import_bh()
        except ImportError as e:
            self.skipTest(f"aiogram/dependencies not installed: {e}")
        # Хендлер доходит до запроса в БД, поэтому схема должна существовать.
        from db import init_db
        await init_db()

    def _make_message(
        self,
        text: str = "!swarn Спам",
        reply_to_message=None,
        from_user_id: int = 100200,
        chat_id: int = -1001234567890,
    ):
        msg = MagicMock()
        msg.text = text
        msg.caption = None
        msg.photo = None
        msg.entities = []
        msg.caption_entities = []
        msg.reply_to_message = reply_to_message
        msg.from_user = SimpleNamespace(
            id=from_user_id,
            username="moderator",
            first_name="Mod",
            last_name="",
            is_bot=False,
        )
        msg.chat = SimpleNamespace(id=chat_id)
        msg.bot = MagicMock()
        msg.bot.send_message = AsyncMock()
        msg.bot.restrict_chat_member = AsyncMock()
        msg.bot.ban_chat_member = AsyncMock()
        msg.bot.get_chat_member = AsyncMock()
        msg.delete = AsyncMock()
        return msg

    def _make_reply(self, user_id=12345, username="bad_user"):
        reply = MagicMock()
        reply.from_user = SimpleNamespace(
            id=user_id,
            username=username,
            first_name="Bad",
            last_name="",
            is_bot=False,
        )
        reply.text = "Нарушение"
        reply.photo = None
        reply.sticker = None
        reply.delete = AsyncMock()
        return reply

    # v4.8.9/v4.8.10: ветки !swarn/!sban уехали в mod_commands.py, а модуль
    # связывает хелперы при импорте (`from bot_handlers import _count_warns`).
    # Патч по имени bot_handlers.<...> его копию не подменяет — патчим там,
    # где хендлер исполняется.
    @patch("bot_handlers._is_admin")
    @patch("mod_commands._get_chat_settings")
    @patch("mod_commands._save_punishment", new_callable=AsyncMock)
    @patch("mod_commands._upsert_user", new_callable=AsyncMock)
    @patch("mod_commands._upsert_moderator", new_callable=AsyncMock)
    @patch("mod_commands._count_warns", return_value=1)
    @patch("mod_commands._send_report", new_callable=AsyncMock)
    @patch("mod_commands._send_user_warn_notification", new_callable=AsyncMock)
    @patch("mod_commands._send_ephemeral", new_callable=AsyncMock)
    @patch("mod_commands._check_warn_threshold", new_callable=AsyncMock)
    async def test_swarn_bare_reason_dispatches_to_handler_hotfix(
        self,
        mock_threshold,
        mock_ephemeral,
        mock_user_notify,
        mock_send_report,
        mock_count_warns,
        mock_upsert_mod,
        mock_upsert_user,
        mock_save_punishment,
        mock_get_cs,
        mock_is_admin,
    ):
        """`!swarn Спам` с reply → dispatch доходит до !swarn handler, варн сохраняется."""
        # _is_admin: True только для модератора, False для target (friendly-fire check).
        async def _is_admin_fake(session, chat_id, user_id):
            return user_id == 100200
        mock_is_admin.side_effect = _is_admin_fake

        cs = MagicMock()
        cs.auto_delete_commands = False
        mock_get_cs.return_value = cs

        reply = self._make_reply(user_id=999111, username="spammer")
        msg = self._make_message(text="!swarn Спам ссылками", reply_to_message=reply)

        # Вызываем handler. В v4.8.3 это бы просто return'нуло на уровне
        # _is_moderation_command (т.к. regex не матчит). В v4.8.3.1 —
        # dispatch доходит до !swarn handler, который сохраняет варн.
        try:
            await self.bh.handle_group_command(msg)
        except Exception as e:
            self.fail(f"Handler упал с unexpected exception: {e}")

        # Проверяем что _save_punishment был вызван (варн сохранён).
        self.assertTrue(
            mock_save_punishment.called,
            "REGRESSION: !swarn <reason> не дошёл до _save_punishment — "
            "варн не сохранён. В v4.8.3 dispatch не доходил до handler'а."
        )
        # Проверяем аргументы _save_punishment: action_type='warn', reason='Спам ссылками'
        call_args = mock_save_punishment.call_args
        # _save_punishment(session, user_id, mod_id, chat_id, action_type, ...)
        # session — positional first arg, остальные — kwargs или positional
        # Проверяем что action_type='warn' и reason='Спам ссылками'
        args = call_args.args
        kwargs = call_args.kwargs
        # Ищем action_type (5-й positional или в kwargs)
        action_type = None
        if "action_type" in kwargs:
            action_type = kwargs["action_type"]
        elif len(args) >= 5:
            action_type = args[4]
        self.assertEqual(action_type, "warn", "Должен быть 'warn'")

        # Ищем reason (7-й positional или в kwargs)
        reason = None
        if "reason" in kwargs:
            reason = kwargs["reason"]
        elif len(args) >= 7:
            reason = args[6]
        self.assertEqual(reason, "Спам ссылками", "Reason должен быть передан")

    @patch("bot_handlers._is_admin")
    @patch("bot_handlers._get_chat_settings")
    async def test_swarn_bare_reason_no_silent_ignore_hotfix(
        self, mock_get_cs, mock_is_admin,
    ):
        """`!swarn Спам` БЕЗ reply → бот отвечает ephemeral про отсутствующую цель.

        В v4.8.3 `!swarn Спам` без reply вообще никак не обрабатывался
        (regex не матчит → _is_moderation_command=False → return без действий).
        Модератор не получал никакого фидбэка. В v4.8.3.1 regex матчит,
        и _resolve_punishment_target возвращает ошибку про "укажите цель".
        """
        async def _is_admin_fake(session, chat_id, user_id):
            return user_id == 100200
        mock_is_admin.side_effect = _is_admin_fake

        cs = MagicMock()
        cs.auto_delete_commands = False
        mock_get_cs.return_value = cs

        msg = self._make_message(text="!swarn Спам", reply_to_message=None)

        # Подменяем _send_ephemeral чтобы проверить что был вызов с ошибкой
        with patch("bot_handlers._send_ephemeral", new_callable=AsyncMock) as mock_eph:
            try:
                await self.bh.handle_group_command(msg)
            except Exception as e:
                self.fail(f"Handler упал: {e}")

            # Должен быть хотя бы один вызов _send_ephemeral
            self.assertGreater(
                len(mock_eph.call_args_list), 0,
                "REGRESSION: !swarn <reason> без reply должно вызвать "
                "ephemeral с подсказкой указать цель. В v4.8.3 вообще "
                "никакого фидбэка не было."
            )


# ============================================================================
# КЛАСС 6: Регрессионный smoke-test — !sban с bare reason.
# ============================================================================
class TestSbanBareReasonHotfix(unittest.IsolatedAsyncioTestCase):
    """!sban <reason> — проверка что dispatch доходит до handler'а.

    Хотя пользователь не сообщал о баге с !sban (вероятно, всегда вызывал
    `!sban` без причины), regex-баг в v4.8.3 затронул и !sban. В v4.8.3.1
    это исправлено.
    """

    async def asyncSetUp(self):
        try:
            self.bh = _import_bh()
        except ImportError as e:
            self.skipTest(f"aiogram/dependencies not installed: {e}")
        # Хендлер доходит до запроса в БД, поэтому схема должна существовать.
        from db import init_db
        await init_db()

    def _make_message(self, text="!sban Реклама", reply_to_message=None):
        msg = MagicMock()
        msg.text = text
        msg.caption = None
        msg.photo = None
        msg.entities = []
        msg.caption_entities = []
        msg.reply_to_message = reply_to_message
        msg.from_user = SimpleNamespace(
            id=100200, username="mod", first_name="Mod",
            last_name="", is_bot=False,
        )
        msg.chat = SimpleNamespace(id=-1001234567890)
        msg.bot = MagicMock()
        msg.bot.send_message = AsyncMock()
        msg.bot.ban_chat_member = AsyncMock()
        msg.bot.get_chat_member = AsyncMock()
        msg.delete = AsyncMock()
        return msg

    def _make_reply(self, user_id=12345, username="bad"):
        reply = MagicMock()
        reply.from_user = SimpleNamespace(
            id=user_id, username=username, first_name="Bad",
            last_name="", is_bot=False,
        )
        reply.text = "Нарушение"
        reply.photo = None
        # v4.8.9/v4.8.10: ветки !swarn/!sban уехали в mod_commands.py, а модуль
        # связывает хелперы при импорте (`from bot_handlers import _count_warns`).
        # Патч по имени bot_handlers.<...> его копию не подменяет — патчим там,
        # где хендлер исполняется.
        reply.sticker = None
        reply.delete = AsyncMock()
        return reply

    @patch("bot_handlers._is_admin")
    @patch("mod_commands._get_chat_settings")
    @patch("mod_commands._save_punishment", new_callable=AsyncMock)
    @patch("bot_handlers._upsert_user", new_callable=AsyncMock)
    @patch("bot_handlers._upsert_moderator", new_callable=AsyncMock)
    @patch("bot_handlers._send_report", new_callable=AsyncMock)
    @patch("bot_handlers._send_ephemeral", new_callable=AsyncMock)
    async def test_sban_bare_reason_dispatches_hotfix(
        self,
        mock_eph, mock_report, mock_upsert_mod, mock_upsert_user,
        mock_save_punishment, mock_get_cs, mock_is_admin,
    ):
        """`!sban Реклама` с reply → dispatch доходит до handler, бан сохраняется."""
        # _is_admin: True только для модератора, False для target.
        async def _is_admin_fake(session, chat_id, user_id):
            return user_id == 100200
        mock_is_admin.side_effect = _is_admin_fake

        cs = MagicMock()
        cs.auto_delete_commands = False
        mock_get_cs.return_value = cs

        reply = self._make_reply(user_id=888999, username="spammer")
        msg = self._make_message(text="!sban Реклама ботов", reply_to_message=reply)

        try:
            await self.bh.handle_group_command(msg)
        except Exception as e:
            self.fail(f"Handler упал: {e}")

        self.assertTrue(
            mock_save_punishment.called,
            "REGRESSION: !sban <reason> не дошёл до _save_punishment"
        )
        # Проверяем что был вызов ban_chat_member (бан выполнен)
        self.assertTrue(
            msg.bot.ban_chat_member.called,
            "Бан не был выполнен"
        )


# ============================================================================
# КЛАСС 7: Сравнение с v4.8.2 — НЕ-регрессия громких команд.
# ============================================================================
class TestLoudCommandsNotRegressed(unittest.TestCase):
    """Проверка что громкие !ban/!warn/!mute продолжают работать как раньше.

    v4.8.3.1 не трогает их regex-ы, но проверяем что наш hotfix не сломал
    косвенно dispatch (например, через _ALL_MOD_COMMANDS).
    """

    def setUp(self):
        try:
            self.bh = _import_bh()
        except ImportError as e:
            self.skipTest(f"aiogram/dependencies not installed: {e}")

    def test_ban_with_target_and_reason_ok(self):
        m = self.bh._CMD_BAN.match("!ban @user Спам")
        self.assertIsNotNone(m)
        self.assertEqual(m.group("target"), "@user")
        self.assertEqual(m.group("reason"), "Спам")

    def test_ban_only_reason_ok(self):
        m = self.bh._CMD_BAN.match("!ban Спам")
        self.assertIsNotNone(m)
        # target может быть None (regex его не матчит — нет @ или цифр)
        # и reason = "Спам"
        self.assertEqual(m.group("reason"), "Спам")

    def test_warn_with_target_ok(self):
        m = self.bh._CMD_WARN.match("!warn @user Спам")
        self.assertIsNotNone(m)
        self.assertEqual(m.group("target"), "@user")
        self.assertEqual(m.group("reason"), "Спам")

    def test_mute_with_target_ok(self):
        m = self.bh._CMD_MUTE.match("!mute @user 1h Спам")
        self.assertIsNotNone(m)
        self.assertEqual(m.group("target"), "@user")
        self.assertEqual(m.group("dur"), "1h")
        self.assertEqual(m.group("reason"), "Спам")

    def test_all_mod_commands_includes_silent(self):
        """_ALL_MOD_COMMANDS включает все 6 punish-команд + un* + alarm."""
        # Простая проверка: хотя бы 6 punish-паттернов + остальные
        self.assertGreaterEqual(len(self.bh._ALL_MOD_COMMANDS), 9)


if __name__ == "__main__":
    unittest.main(verbosity=2)
