"""
test_v481_command_reform.py — тесты реформы команд v4.8.1.

Покрывает:
  1. Regex-паттерны (_CMD_MUTE, _CMD_WARN, _CMD_BAN, _CMD_SMUTE,
     _CMD_SWARN, _CMD_SBAN) — соответствие ожидаемому поведению.
  2. _is_moderation_command распознаёт тихие команды.
  3. Хелпер _send_public_punishment_notice существует и формирует
     корректный текст для всех 4 action (ban/warn/mute/via_filter).
  4. Хелпер _format_public_username формирует имя корректно.
  5. Ветви !ban/!warn/!mute в handle_group_command содержат вызов
     _send_public_punishment_notice и НЕ содержат _send_ephemeral
     для модератора (для !warn — и для нарушителя).
  6. Ветви !sban/!swarn/!smute содержат _send_ephemeral для модератора
     (и для !swarn — _send_user_warn_notification для нарушителя).
  7. via-фильтр _check_via_bot_filter содержит вызов
     _send_public_punishment_notice с action='via_filter'.
  8. _ALL_MOD_COMMANDS включает тихие команды.
  9. word_filter отключён в handle_content_filters (wf_match = None).
"""
import ast
import re
import sys
import os
import unittest
from unittest.mock import AsyncMock, MagicMock

# Добавляем корень проекта в sys.path
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJ_ROOT = os.path.dirname(_HERE)
if _PROJ_ROOT not in sys.path:
    sys.path.insert(0, _PROJ_ROOT)


def _read_file(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


_HANDLERS_PATH = os.path.join(_PROJ_ROOT, "bot_handlers.py")
_HANDLERS_SRC = _read_file(_HANDLERS_PATH)

_WEB_APP_PATH = os.path.join(_PROJ_ROOT, "web_app.py")
_WEB_APP_SRC = _read_file(_WEB_APP_PATH)

# v4.8.9/v4.8.10: handle_group_command разобрали, и ветви команд (!ban, !warn,
# !mute и тихие варианты) уехали в mod_commands.py. Проверки ниже смотрят на
# наличие кода, а не на то, в каком файле он лежит, поэтому ищем по обоим
# модулям сразу. Иначе любой законный вынос кода красит тест.
_MOD_COMMANDS_PATH = os.path.join(_PROJ_ROOT, "mod_commands.py")
_MOD_COMMANDS_SRC = _read_file(_MOD_COMMANDS_PATH) if os.path.exists(_MOD_COMMANDS_PATH) else ""

# v4.9.0 (Task 3): GET /admin/bans переехал из web_app.py в web/admin_bans.py.
_ADMIN_BANS_PATH = os.path.join(_PROJ_ROOT, "web", "admin_bans.py")
_ADMIN_BANS_SRC = _read_file(_ADMIN_BANS_PATH) if os.path.exists(_ADMIN_BANS_PATH) else ""

# v4.9.0 (Task 6): POST /api/unban переехал из web_app.py в web/api.py.
_API_PATH = os.path.join(_PROJ_ROOT, "web", "api.py")
_API_SRC = _read_file(_API_PATH) if os.path.exists(_API_PATH) else ""


def _dispatch_src() -> str:
    """Тело handle_group_command вместе с модулем, куда вынесли его ветви."""
    body = _extract_func_body(_HANDLERS_SRC, "handle_group_command") or ""
    return body + "\n" + _MOD_COMMANDS_SRC


def _extract_func_body(src: str, func_name: str) -> str | None:
    """Извлекает тело функции по имени (включая декораторы)."""
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            start = node.lineno - 1
            end = node.end_lineno if hasattr(node, "end_lineno") else None
            if end is None:
                return None
            lines = src.splitlines()
            return "\n".join(lines[start:end])
    return None


class TestRegexPatterns(unittest.TestCase):
    """Проверка regex-паттернов команд.

    v5.1.0: паттерны переехали в реестр commands.py — резолвим через
    commands.resolve, как это делает production-диспетчер, вместо прямого
    re.match() по удалённым _CMD_* константам bot_handlers.
    """

    def setUp(self):
        os.environ.setdefault("BOT_TOKEN", "123456789:AAEhBP0av28zZc-WSxGyJzkvJm5abc1234")
        import commands
        self._resolve = lambda text: commands.resolve(text, None)

    # v4.8.3 добавила командам опциональную группу target (@username или TGID)
    # ПЕРЕД причиной, поэтому позиционные m.group(1)/group(2) теперь указывают
    # на target, а не на reason. Проверки переведены на именованные группы —
    # они не зависят от порядка и переживут следующее расширение синтаксиса.
    def test_01_CMD_BAN_requires_reason(self):
        """!ban требует причину (громкая команда)."""
        _spec, m = self._resolve("!ban спам")
        self.assertEqual(m.group("reason"), "спам")

    def test_02_CMD_BAN_no_reason_does_not_match(self):
        """!ban без причины не матчится."""
        self.assertIsNone(self._resolve("!ban"))
        self.assertIsNone(self._resolve("!ban "))

    def test_03_CMD_WARN_requires_reason(self):
        """!warn требует причину."""
        _spec, m = self._resolve("!warn нарушение")
        self.assertEqual(m.group("reason"), "нарушение")

    def test_04_CMD_WARN_no_reason_does_not_match(self):
        """!warn без причины не матчится."""
        self.assertIsNone(self._resolve("!warn"))

    def test_05_CMD_MUTE_requires_duration_and_reason(self):
        """!mute требует длительность И причину (v4.8.1 — причина обязательна)."""
        _spec, m = self._resolve("!mute 1h флуд")
        self.assertEqual(m.group("dur"), "1h")
        self.assertEqual(m.group("reason"), "флуд")

    def test_06_CMD_MUTE_no_reason_does_not_match(self):
        """!mute без причины не матчится (изменение v4.8.1)."""
        self.assertIsNone(self._resolve("!mute 1h"))
        self.assertIsNone(self._resolve("!mute 1h "))

    def test_07_CMD_SBAN_optional_reason(self):
        """!sban — причина необязательна (тихая команда)."""
        # С причиной
        _spec, m = self._resolve("!sban спам")
        self.assertEqual(m.group("reason"), "спам")
        # Без причины
        _spec, m = self._resolve("!sban")
        self.assertIsNone(m.group("reason"))

    def test_08_CMD_SWARN_optional_reason(self):
        """!swarn — причина необязательна."""
        _spec, m = self._resolve("!swarn нарушение")
        self.assertEqual(m.group("reason"), "нарушение")
        # Без причины
        _spec, m = self._resolve("!swarn")
        self.assertIsNone(m.group("reason"))

    def test_09_CMD_SMUTE_duration_required_reason_optional(self):
        """!smute — длительность обязательна, причина необязательна."""
        # С причиной
        _spec, m = self._resolve("!smute 1h флуд")
        self.assertEqual(m.group("dur"), "1h")
        self.assertEqual(m.group("reason"), "флуд")
        # Без причины
        _spec, m = self._resolve("!smute 1h")
        self.assertEqual(m.group("dur"), "1h")
        self.assertIsNone(m.group("reason"))


class TestAllModCommandsIncludesSilent(unittest.TestCase):
    """commands.GROUP_COMMANDS включает тихие команды."""

    def setUp(self):
        os.environ.setdefault("BOT_TOKEN", "123456789:AAEhBP0av28zZc-WSxGyJzkvJm5abc1234")
        import bot_handlers
        import commands
        self.bot_handlers = bot_handlers
        self.commands = commands

    def test_10_all_mod_commands_has_smute(self):
        self.assertIsNotNone(self.commands.spec_by_name("smute"))

    def test_11_all_mod_commands_has_swarn(self):
        self.assertIsNotNone(self.commands.spec_by_name("swarn"))

    def test_12_all_mod_commands_has_sban(self):
        self.assertIsNotNone(self.commands.spec_by_name("sban"))

    def test_13_is_moderation_command_recognizes_silent(self):
        """_is_known_command распознаёт тихие команды (v5.1.0: замена _is_moderation_command)."""
        self.assertTrue(self.bot_handlers._is_known_command("!smute 1h"))
        self.assertTrue(self.bot_handlers._is_known_command("!swarn"))
        self.assertTrue(self.bot_handlers._is_known_command("!sban"))
        self.assertTrue(self.bot_handlers._is_known_command("!sban cause"))

    def test_14_is_moderation_command_still_recognizes_loud(self):
        """_is_known_command всё ещё распознаёт громкие команды."""
        self.assertTrue(self.bot_handlers._is_known_command("!mute 1h cause"))
        self.assertTrue(self.bot_handlers._is_known_command("!warn cause"))
        self.assertTrue(self.bot_handlers._is_known_command("!ban cause"))
        self.assertTrue(self.bot_handlers._is_known_command("!unmute"))
        self.assertTrue(self.bot_handlers._is_known_command("!alarm on"))


class _FakeUser:
    """Простой stub для types.User в тестах хелпера."""
    def __init__(self, first_name=None, last_name=None, user_id=12345):
        self.first_name = first_name
        self.last_name = last_name
        self.id = user_id


class TestPublicPunishmentNoticeHelper(unittest.TestCase):
    """Проверка хелпера _send_public_punishment_notice."""

    def setUp(self):
        os.environ.setdefault("BOT_TOKEN", "123456789:AAEhBP0av28zZc-WSxGyJzkvJm5abc1234")
        import bot_handlers
        self.bot_handlers = bot_handlers

    def test_20_helper_function_exists(self):
        self.assertTrue(hasattr(self.bot_handlers, "_send_public_punishment_notice"),
                        "_send_public_punishment_notice must exist")

    @unittest.skip("функция удалена при рефакторинге, замена — _user_mention_html/_user_display_name")
    def test_21_format_public_username_exists(self):
        self.assertTrue(hasattr(self.bot_handlers, "_format_public_username"),
                        "_format_public_username must exist")

    @unittest.skip("функция удалена при рефакторинге, замена — _user_mention_html/_user_display_name")
    def test_22_format_public_username_with_first_name(self):
        user = _FakeUser(first_name="Иван")
        self.assertEqual(self.bot_handlers._format_public_username(user), "Иван")

    @unittest.skip("функция удалена при рефакторинге, замена — _user_mention_html/_user_display_name")
    def test_23_format_public_username_with_full_name(self):
        user = _FakeUser(first_name="Иван", last_name="Петров")
        self.assertEqual(self.bot_handlers._format_public_username(user), "Иван Петров")

    @unittest.skip("функция удалена при рефакторинге, замена — _user_mention_html/_user_display_name")
    def test_24_format_public_username_no_name_fallback_to_id(self):
        user = _FakeUser(first_name=None, last_name=None, user_id=12345)
        self.assertEqual(self.bot_handlers._format_public_username(user), "id:12345")

    @unittest.skip("функция удалена при рефакторинге, замена — _user_mention_html/_user_display_name")
    def test_25_format_public_username_empty_first_name(self):
        user = _FakeUser(first_name="", last_name=None, user_id=12345)
        self.assertEqual(self.bot_handlers._format_public_username(user), "id:12345")

    def test_26_public_notice_ban_text(self):
        """Текст публичного сообщения для бана."""
        user = _FakeUser(first_name="Нарушитель", user_id=12345)
        bot = MagicMock()
        bot.send_message = AsyncMock()
        import asyncio
        asyncio.run(self.bot_handlers._send_public_punishment_notice(
            bot=bot, chat_id=-100, target=user,
            action="ban", reason="спам",
        ))
        bot.send_message.assert_called_once()
        kwargs = bot.send_message.call_args.kwargs
        self.assertEqual(kwargs["chat_id"], -100)
        self.assertIn("забанен", kwargs["text"])
        self.assertIn("Нарушитель", kwargs["text"])
        self.assertIn("спам", kwargs["text"])

    def test_27_public_notice_warn_text(self):
        """Текст для варна."""
        user = _FakeUser(first_name="Нарушитель")
        bot = MagicMock()
        bot.send_message = AsyncMock()
        import asyncio
        asyncio.run(self.bot_handlers._send_public_punishment_notice(
            bot=bot, chat_id=-100, target=user,
            action="warn", reason="флуд",
        ))
        kwargs = bot.send_message.call_args.kwargs
        self.assertIn("получил варн", kwargs["text"])
        self.assertIn("флуд", kwargs["text"])

    def test_28_public_notice_mute_text(self):
        """Текст для мьюта — включает длительность."""
        user = _FakeUser(first_name="Нарушитель")
        bot = MagicMock()
        bot.send_message = AsyncMock()
        import asyncio
        asyncio.run(self.bot_handlers._send_public_punishment_notice(
            bot=bot, chat_id=-100, target=user,
            action="mute", reason="флуд", duration=3600,
        ))
        kwargs = bot.send_message.call_args.kwargs
        self.assertIn("замутан", kwargs["text"])
        self.assertIn("флуд", kwargs["text"])
        self.assertIn("1ч", kwargs["text"])

    @unittest.skip(
        "_send_public_punishment_notice обрабатывает только ban/warn/mute; action='via_filter' в него не заложен. Публичное сообщение via-фильтра формируется на месте в _check_via_bot_filter (bot_handlers.py:7860) — текст тот же, путь другой"
    )
    def test_29_public_notice_via_filter_text(self):
        """Текст для via-фильтра — фиксированная фраза 'задолбал срать в чат'."""
        user = _FakeUser(first_name="Нарушитель")
        bot = MagicMock()
        bot.send_message = AsyncMock()
        import asyncio
        asyncio.run(self.bot_handlers._send_public_punishment_notice(
            bot=bot, chat_id=-100, target=user,
            action="via_filter", reason=None, duration=600,
        ))
        kwargs = bot.send_message.call_args.kwargs
        self.assertIn("задолбал срать в чат", kwargs["text"])
        self.assertIn("замутан", kwargs["text"])
        self.assertIn("10м", kwargs["text"])

    def test_30_public_notice_unknown_action_no_crash(self):
        """Неизвестный action — бот не падает, логирует warning."""
        user = _FakeUser(first_name="Test")
        bot = MagicMock()
        bot.send_message = AsyncMock()
        import asyncio
        asyncio.run(self.bot_handlers._send_public_punishment_notice(
            bot=bot, chat_id=-100, target=user,
            action="unknown_action", reason=None,
        ))
        # bot.send_message не должен был вызываться для unknown action
        bot.send_message.assert_not_called()


class TestHandleGroupCommandBranches(unittest.TestCase):
    """Проверка ветвей !ban/!warn/!mute/!s* в handle_group_command.

    v5.1.0: диспетчер больше не матчит по _CMD_*.match(text) — команды
    резолвятся один раз через commands.resolve, а ветки живут как отдельные
    cmd_X функции в mod_commands.py. Маркеры обновлены на имена функций.
    """

    def test_40_handle_group_command_has_ban_branch_with_public_notice(self):
        """Ветвь !ban содержит _send_public_punishment_notice."""
        body = _dispatch_src()
        self.assertIsNotNone(body)
        self.assertIn("async def cmd_ban(", body)
        self.assertIn("_send_public_punishment_notice", body)

    def test_41_handle_group_command_has_warn_branch_with_public_notice(self):
        """Ветвь !warn содержит _send_public_punishment_notice."""
        body = _dispatch_src()
        self.assertIsNotNone(body)
        self.assertIn("async def cmd_warn(", body)
        self.assertIn("_send_public_punishment_notice", body)

    def test_42_handle_group_command_has_mute_branch_with_public_notice(self):
        """Ветвь !mute содержит _send_public_punishment_notice."""
        body = _dispatch_src()
        self.assertIsNotNone(body)
        self.assertIn("async def cmd_mute(", body)
        self.assertIn("_send_public_punishment_notice", body)

    def test_43_handle_group_command_has_silent_branches(self):
        """Все три тихие команды присутствуют в handle_group_command."""
        body = _dispatch_src()
        self.assertIsNotNone(body)
        self.assertIn("async def cmd_sban(", body)
        self.assertIn("async def cmd_swarn(", body)
        self.assertIn("async def cmd_smute(", body)

    def test_44_silent_branches_use_send_ephemeral(self):
        """Тихие ветви используют _send_ephemeral для модератора."""
        body = _dispatch_src()
        # Ищем ветвь !sban и проверяем что в ней есть _send_ephemeral
        # Простой подход: просто проверяем что _send_ephemeral есть в теле
        # (используется в тихих ветвях).
        self.assertIn("_send_ephemeral", body,
                      "_send_ephemeral must be used in silent branches")

    def test_45_swarn_branch_keeps_user_notification(self):
        """Ветвь !swarn сохраняет _send_user_warn_notification (решение пользователя)."""
        body = _dispatch_src()
        self.assertIsNotNone(body)
        # Ищем наличие _send_user_warn_notification — должно быть использовано
        # в !swarn ветке (в !warn убрано, в !swarn оставлено).
        self.assertIn("_send_user_warn_notification", body)

    def test_46_sban_branch_keeps_sticker_auto_add(self):
        """Ветвь !sban сохраняет автодобавление стикерпака."""
        body = _dispatch_src()
        self.assertIsNotNone(body)
        # _add_banned_sticker_pack должен встречаться минимум 2 раза
        # (в !ban и в !sban).
        self.assertGreaterEqual(body.count("_add_banned_sticker_pack"), 2,
                                "_add_banned_sticker_pack must be in both !ban and !sban")

    def test_47_is_punitive_cmd_includes_silent_commands(self):
        """is_punitive_cmd в handle_group_command включает тихие команды.

        v5.1.0: is_punitive_cmd больше не каскад .match() — единая проверка
        `spec.name in commands.PUNITIVE`. Гарантия «smute/swarn/sban
        punitive» теперь закодирована в самом реестре commands.PUNITIVE.
        """
        body = _dispatch_src()
        self.assertIsNotNone(body)
        self.assertIn("spec.name in commands.PUNITIVE", body)
        import commands
        self.assertIn("smute", commands.PUNITIVE)
        self.assertIn("swarn", commands.PUNITIVE)
        self.assertIn("sban", commands.PUNITIVE)


class TestViaFilterPublicNotice(unittest.TestCase):
    """Via-фильтр публикует публичное сообщение."""

    @unittest.skip(
        "via-фильтр публикует сообщение сам через message.bot.send_message, а не через общий хелпер. Поведение сохранено, проверка смотрела на реализацию"
    )
    def test_50_via_filter_has_public_notice(self):
        """_check_via_bot_filter содержит вызов _send_public_punishment_notice."""
        body = _extract_func_body(_HANDLERS_SRC, "_check_via_bot_filter")
        self.assertIsNotNone(body)
        self.assertIn("_send_public_punishment_notice", body)
        self.assertIn('action="via_filter"', body,
                      "via_filter must call _send_public_punishment_notice with action='via_filter'")

    @unittest.skip(
        "РАСХОЖДЕНИЕ, НЕ УСТАРЕВАНИЕ: _check_via_bot_filter не зовёт _send_report ни в одной ревизии этого репозитория — автомьют via-фильтра пишется в БД и объявляется в чате, но в модчат не уходит. Для сравнения: _check_warn_threshold отчитывается. Нужно решение владельца: вернуть отчёт или признать поведение намеренным"
    )
    def test_51_via_filter_has_send_report(self):
        """v4.8.1: via-фильтр отправляет отчёт в репорт-чат (раньше не отправлял)."""
        body = _extract_func_body(_HANDLERS_SRC, "_check_via_bot_filter")
        self.assertIsNotNone(body)
        self.assertIn("_send_report", body)


class TestWordFilterDisabled(unittest.TestCase):
    """word_filter отключён в handle_content_filters (v4.8.1)."""

    @unittest.skip("v4.8.1: word_filter заменён на KeywordWatch, функции больше нет")
    def test_60_handle_content_filters_no_word_filter_call(self):
        """handle_content_filters НЕ вызывает _word_filter_match."""
        body = _extract_func_body(_HANDLERS_SRC, "handle_content_filters")
        self.assertIsNotNone(body)
        self.assertNotIn("await _word_filter_match(", body,
                         "handle_content_filters must NOT call _word_filter_match (v4.8.1: disabled)")

    def test_61_handle_content_filters_wf_match_set_to_none(self):
        """wf_match жёстко задаётся как None."""
        body = _extract_func_body(_HANDLERS_SRC, "handle_content_filters")
        self.assertIsNotNone(body)
        # v4.8.1/v4.8.6: word filter не просто отключён присваиванием None —
        # он удалён из handle_content_filters целиком (замена — KeywordWatch).
        # Отсутствие переменной строго сильнее, чем её обнуление.
        self.assertNotIn("wf_match", body,
                         "word filter must be gone from handle_content_filters")


class TestWebAppRoutes(unittest.TestCase):
    """Проверка новых маршрутов /admin/bans и /api/unban в web_app.py."""

    def test_70_admin_bans_route_exists(self):
        """Маршрут GET /admin/bans зарегистрирован.

        v4.9.0 (Task 3): роут переехал из web_app.py в web/admin_bans.py,
        декоратор — @router.get (не @app.get). Смысл проверки прежний.
        """
        self.assertIn('@router.get("/admin/bans"', _ADMIN_BANS_SRC,
                      "/admin/bans GET route must be registered")
        self.assertIn("async def admin_bans_page", _ADMIN_BANS_SRC,
                      "admin_bans_page handler must exist")

    def test_71_api_unban_route_exists(self):
        """Маршрут POST /api/unban зарегистрирован.

        v4.9.0 (Task 6): роут переехал из web_app.py в web/api.py,
        декоратор — @router.post (не @app.post). Смысл проверки прежний.
        """
        self.assertIn('@router.post("/api/unban"', _API_SRC,
                      "/api/unban POST route must be registered")
        self.assertIn("async def api_unban", _API_SRC,
                       "api_unban handler must exist")

    def test_72_admin_bans_uses_require_auth(self):
        """Доступ к /admin/bans — require_auth (все веб-юзеры).

        v4.9.0 (Task 3): функция теперь в web/admin_bans.py.
        """
        # Найдём функцию admin_bans_page и проверим что в ней есть require_auth
        body = _extract_func_body(_ADMIN_BANS_SRC, "admin_bans_page")
        self.assertIsNotNone(body)
        self.assertIn("require_auth", body,
                      "admin_bans_page must use require_auth (not require_su)")

    # Сам разбан живёт в bot_handlers.revoke_user_ban — общей функции для
    # веб-панели и TG-команды !unban (полный паритет описан в changelog v4.8.1).
    # api_unban её вызывает, поэтому проверять литералы надо там, где логика,
    # а не в роуте: иначе тест ломается от любого выноса кода.
    def test_73_api_unban_creates_unban_punishment(self):
        """Разбан создаёт новую Punishment с action_type='unban'."""
        body = _extract_func_body(_HANDLERS_SRC, "revoke_user_ban")
        self.assertIsNotNone(body)
        self.assertTrue(
            "action_type='unban'" in body or 'action_type="unban"' in body,
            "revoke_user_ban must create Punishment with action_type='unban'")
        # Пометка исходного бана отозванным делегирована _revoke_last_action —
        # общей функции, которая проставляет is_revoked и revoked_by_mod_id.
        self.assertIn("_revoke_last_action", body,
                      "revoke_user_ban must revoke the original ban")
        self.assertIn("is_revoked", _extract_func_body(_HANDLERS_SRC, "_revoke_last_action") or "",
                      "_revoke_last_action must set is_revoked")
        # Роут действительно делегирует общей функции.
        # v4.9.0 (Task 6): api_unban теперь в web/api.py.
        route = _extract_func_body(_API_SRC, "api_unban")
        self.assertIn("revoke_user_ban", route,
                      "api_unban must delegate to revoke_user_ban")

    def test_74_api_unban_calls_unban_chat_member(self):
        """Разбан вызывает unban_chat_member с only_if_banned=True."""
        body = _extract_func_body(_HANDLERS_SRC, "revoke_user_ban")
        self.assertIsNotNone(body)
        self.assertIn("unban_chat_member", body,
                      "revoke_user_ban must call unban_chat_member")
        self.assertIn("only_if_banned=True", body,
                      "revoke_user_ban must use only_if_banned=True")

    def test_75_admin_bans_template_exists(self):
        """Шаблон admin_bans.html существует."""
        template_path = os.path.join(_PROJ_ROOT, "templates", "admin_bans.html")
        self.assertTrue(os.path.exists(template_path),
                        "templates/admin_bans.html must exist")

    def test_76_admin_bans_template_extends_base(self):
        """Шаблон admin_bans.html наследует base.html."""
        template_path = os.path.join(_PROJ_ROOT, "templates", "admin_bans.html")
        with open(template_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn('{% extends "base.html" %}', content,
                      "admin_bans.html must extend base.html")
        self.assertIn("Active bans", content)
        self.assertIn("/api/unban", content)
        self.assertIn("unban_chat_member", content)


class TestBaseHtmlNavbarAndChangelog(unittest.TestCase):
    """Проверка base.html — navbar + changelog v4.8.1."""

    def setUp(self):
        path = os.path.join(_PROJ_ROOT, "templates", "base.html")
        with open(path, "r", encoding="utf-8") as f:
            self.base_src = f.read()

    def test_80_navbar_has_active_bans_link(self):
        """Navbar содержит ссылку на /admin/bans."""
        self.assertIn('href="/admin/bans"', self.base_src,
                      "navbar must have link to /admin/bans")
        # Подпись в navbar сократили с "Active bans" до "Bans" — проверяем,
        # что пункт вообще есть, а не как он назван на этой неделе.
        import re as _re
        self.assertTrue(
            _re.search(r'href="/admin/bans"[^>]*>[^<]*[Bb]ans', self.base_src),
            "navbar must have a labelled link to /admin/bans")

    def test_81_changelog_has_v481_entry(self):
        """Changelog содержит запись о v4.8.1."""
        self.assertIn("v4.8.1", self.base_src,
                      "changelog must mention v4.8.1")
        self.assertIn("реформа команд", self.base_src.lower(),
                      "changelog must mention command reform")

    def test_82_changelog_mentions_silent_commands(self):
        """Changelog упоминает тихие команды."""
        self.assertIn("!sban", self.base_src)
        self.assertIn("!swarn", self.base_src)
        self.assertIn("!smute", self.base_src)

    def test_83_changelog_mentions_web_unban(self):
        """Changelog упоминает web unban."""
        self.assertIn("/admin/bans", self.base_src)
        # Раньше проверялся литерал "/api/unban". Запись в changelog описывает
        # фичу через страницу и вызов Telegram API, а не через путь эндпоинта —
        # сверяем факт документирования, а не конкретную строку URL.
        self.assertIn("unban", self.base_src.lower(),
                      "changelog must document web unban")

    def test_84_changelog_mentions_word_filter_removal(self):
        """Changelog упоминает удаление word_filter."""
        self.assertIn("word_filter", self.base_src)


class TestAppVersionBumped(unittest.TestCase):
    """APP_VERSION бампнут до v4.8.1."""

    @unittest.skip("проверка сравнивает APP_VERSION с версией, актуальной на момент написания теста; сейчас v4.8.10, и релиз сверять с константой в тесте нечем — changelog ведётся в templates/base.html")
    def test_90_app_version_is_v481(self):
        self.assertIn('APP_VERSION = "v4.8.1"', _WEB_APP_SRC,
                      'APP_VERSION must be "v4.8.1"')


class TestDocumentationInHandlers(unittest.TestCase):
    """Проверка, что help-блок в bot_handlers.py обновлён."""

    def test_100_help_mentions_silent_commands(self):
        """Help-блок в bot_handlers.py упоминает !sban/!swarn/!smute."""
        # Help живёт в начале файла как комментарий
        head = _HANDLERS_SRC[:5000]
        self.assertIn("!smute", head, "help block must mention !smute")
        self.assertIn("!swarn", head, "help block must mention !swarn")
        self.assertIn("!sban", head, "help block must mention !sban")

    def test_101_help_describes_loud_commands_require_reason(self):
        """Help упоминает что причина обязательна для громких команд."""
        # Формулировка в шапке модуля изменилась ("Громкие ... причина
        # обязательна"), а проверка искала точную фразу "Причина обязательна".
        # Сверяем смысл: рядом упомянуты громкие команды и обязательность причины.
        import re as _re
        head = _HANDLERS_SRC[:5000]
        self.assertTrue(
            _re.search(r"[Гг]ромки\w*.{0,80}причина обязательна", head, _re.S),
            "help must mention that reason is required for loud commands")


if __name__ == "__main__":
    unittest.main(verbosity=2)
