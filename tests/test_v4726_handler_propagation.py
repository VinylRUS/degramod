"""v4.7.26 — regression tests for handler propagation fix.

CRITICAL BUG: via_bot / word / link filters were never called because
handle_alarm_command (L3588) and handle_group_command (L2909) had overly
broad filters and stopped propagation in aiogram 3.x.

In aiogram 3.x, the first matching handler in a router STOPS propagation
by default. So:
  - handle_alarm_command with filter F.chat.type.in_(["group","supergroup"])
    matched ALL group messages, returned early (no !alarm), and stopped
    propagation → handle_content_filters never ran.
  - handle_group_command with filter F.chat.type.in_(...) + F.reply_to_message
    matched ALL reply messages, returned early (no moderation command), and
    stopped propagation → handle_content_filters never ran for replies.

Fix: added _AlarmCommandFilter and _ModerationCommandFilter (BaseFilter
subclasses) that ONLY match when text is actually a command. Now non-command
messages fall through to handle_content_filters.

Tests:
  Structural (AST):
    1. APP_VERSION >= v4.7.26
    2. _AlarmCommandFilter class defined
    3. _ModerationCommandFilter class defined
    4. _AlarmCommandFilter uses _CMD_ALARM regex (case-insensitive)
    5. _ModerationCommandFilter uses _is_moderation_command
    6. handle_alarm_command decorated with _AlarmCommandFilter
    7. handle_group_command decorated with _ModerationCommandFilter
    8. handle_alarm_command filter does NOT have only F.chat.type.in_
    9. handle_group_command filter has _ModerationCommandFilter (not just reply)
   10. Changelog in base.html contains v4.7.26
   11. Changelog mentions propagation / "не работали"

  Behavioral (end-to-end with real aiogram Dispatcher):
   12. Plain text "hello" → handle_content_filters called
   13. Reply "just a reply" → handle_content_filters called (was BROKEN)
   14. Reply with "!mute 1h spam" → handle_group_command called
   15. "!alarm on" → handle_alarm_command called
   16. Message with via_bot → handle_content_filters called
   17. Photo with via_bot → handle_content_filters called
   18. "!ALARm on" (case-insensitive) → handle_alarm_command called
   19. "!alarmXYZ" (not a command) → handle_content_filters called
"""
import ast
import re
import sys
import os
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BOT_HANDLERS_PY = ROOT / "bot_handlers.py"
WEB_APP_PY = ROOT / "web_app.py"
BASE_HTML = ROOT / "templates" / "base.html"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


class TestV4726HandlerPropagation(unittest.TestCase):
    """v4.7.26: regression tests for handler propagation fix."""

    def setUp(self):
        self.handlers_src = _read(BOT_HANDLERS_PY)
        self.web_src = _read(WEB_APP_PY)
        self.html_src = _read(BASE_HTML)
        self.tree = ast.parse(self.handlers_src)

    # ── 1. Version ─────────────────────────────────────────────────────────
    def test_01_app_version_bumped(self):
        """APP_VERSION >= v4.7.26."""
        m = re.search(r'APP_VERSION\s*=\s*"v(\d+)\.(\d+)\.(\d+)"', self.web_src)
        self.assertIsNotNone(m, "APP_VERSION not found in web_app.py")
        major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
        self.assertGreaterEqual(
            (major, minor, patch),
            (4, 7, 26),
            f"APP_VERSION=v{major}.{minor}.{patch} should be >= v4.7.26",
        )

    # ── 2. _AlarmCommandFilter class defined ───────────────────────────────
    def test_02_alarm_command_filter_class_defined(self):
        """_AlarmCommandFilter class is defined in bot_handlers.py."""
        classes = [
            node for node in self.tree.body
            if isinstance(node, ast.ClassDef) and node.name == "_AlarmCommandFilter"
        ]
        self.assertEqual(len(classes), 1,
                         "_AlarmCommandFilter class should be defined exactly once")

    # ── 3. _ModerationCommandFilter class defined ──────────────────────────
    def test_03_moderation_command_filter_class_defined(self):
        """_ModerationCommandFilter class is defined in bot_handlers.py."""
        classes = [
            node for node in self.tree.body
            if isinstance(node, ast.ClassDef) and node.name == "_ModerationCommandFilter"
        ]
        self.assertEqual(len(classes), 1,
                         "_ModerationCommandFilter class should be defined exactly once")

    # ── 4. _AlarmCommandFilter uses _CMD_ALARM ─────────────────────────────
    def test_04_alarm_filter_uses_cmd_alarm_regex(self):
        """_AlarmCommandFilter.__call__ uses _CMD_ALARM.match()."""
        # Find the class and inspect its __call__ method.
        for node in ast.walk(self.tree):
            if (isinstance(node, ast.ClassDef) and node.name == "_AlarmCommandFilter"):
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == "__call__":
                        body_src = ast.unparse(item)
                        self.assertIn("_CMD_ALARM", body_src,
                                      "_AlarmCommandFilter should use _CMD_ALARM")
                        self.assertIn(".match(", body_src,
                                      "_AlarmCommandFilter should call .match()")
                        return
        self.fail("_AlarmCommandFilter.__call__ not found")

    # ── 5. _ModerationCommandFilter uses _is_moderation_command ────────────
    def test_05_moderation_filter_uses_helper(self):
        """_ModerationCommandFilter.__call__ uses _is_moderation_command()."""
        for node in ast.walk(self.tree):
            if (isinstance(node, ast.ClassDef) and node.name == "_ModerationCommandFilter"):
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == "__call__":
                        body_src = ast.unparse(item)
                        self.assertIn("_is_moderation_command", body_src,
                                      "_ModerationCommandFilter should use _is_moderation_command")
                        return
        self.fail("_ModerationCommandFilter.__call__ not found")

    # ── 6. handle_alarm_command decorated with _AlarmCommandFilter ─────────
    def test_06_alarm_handler_uses_filter(self):
        """handle_alarm_command has _AlarmCommandFilter in its decorator."""
        # Find handle_alarm_command function
        func = None
        for node in self.tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "handle_alarm_command":
                func = node
                break
        self.assertIsNotNone(func, "handle_alarm_command not found")
        # Check decorator arguments
        for dec in func.decorator_list:
            if isinstance(dec, ast.Call):
                for arg in dec.args:
                    if isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name):
                        if arg.func.id == "_AlarmCommandFilter":
                            return
                    if isinstance(arg, ast.Name) and arg.id == "_AlarmCommandFilter":
                        return
        self.fail("handle_alarm_command should have _AlarmCommandFilter() in decorator")

    # ── 7. handle_group_command decorated with _ModerationCommandFilter ────
    def test_07_group_handler_uses_filter(self):
        """handle_group_command has _ModerationCommandFilter in its decorator."""
        func = None
        for node in self.tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "handle_group_command":
                func = node
                break
        self.assertIsNotNone(func, "handle_group_command not found")
        for dec in func.decorator_list:
            if isinstance(dec, ast.Call):
                for arg in dec.args:
                    if isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name):
                        if arg.func.id == "_ModerationCommandFilter":
                            return
                    if isinstance(arg, ast.Name) and arg.id == "_ModerationCommandFilter":
                        return
        self.fail("handle_group_command should have _ModerationCommandFilter() in decorator")

    # ── 8. handle_alarm_command filter is NOT just F.chat.type ─────────────
    def test_08_alarm_handler_filter_is_specific(self):
        """handle_alarm_command has MORE than just F.chat.type.in_ in decorator.

        Specifically, it should NOT have a decorator that's ONLY
        F.chat.type.in_(...) — that was the bug.
        """
        func = None
        for node in self.tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "handle_alarm_command":
                func = node
                break
        self.assertIsNotNone(func)
        # Collect all decorator args
        all_args = []
        for dec in func.decorator_list:
            if isinstance(dec, ast.Call):
                all_args.extend(dec.args)
        # Must have at least 2 args (chat type filter + _AlarmCommandFilter)
        self.assertGreaterEqual(
            len(all_args), 2,
            f"handle_alarm_command should have >=2 filter args (chat type + "
            f"_AlarmCommandFilter), got {len(all_args)}",
        )

    # ── 9. handle_group_command filter has _ModerationCommandFilter ────────
    def test_09_group_handler_has_filter_beyond_reply(self):
        """handle_group_command has _ModerationCommandFilter (not just reply)."""
        func = None
        for node in self.tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "handle_group_command":
                func = node
                break
        self.assertIsNotNone(func)
        all_args = []
        for dec in func.decorator_list:
            if isinstance(dec, ast.Call):
                all_args.extend(dec.args)
        # Раньше фильтров было три: тип чата + F.reply_to_message +
        # _ModerationCommandFilter. В v4.8.3 требование reply убрали из
        # декоратора — цель команды можно указать через @username или TGID
        # (см. docstring handle_group_command). Осталось два, и это верно.
        #
        # Проверяем то, ради чего тест написан (v4.7.26): фильтр команд стоит
        # именно в декораторе, иначе handler перехватывает чужие сообщения и
        # обрывает propagation до handle_content_filters.
        import ast as _ast
        names = {
            d.func.id for d in all_args
            if isinstance(d, _ast.Call) and isinstance(d.func, _ast.Name)
        }
        self.assertIn(
            "_ModerationCommandFilter", names,
            f"handle_group_command must filter by _ModerationCommandFilter "
            f"in the decorator, got args: {names}",
        )
        self.assertGreaterEqual(
            len(all_args), 2,
            f"handle_group_command should keep the chat-type filter too, "
            f"got {len(all_args)}",
        )

    # ── 10. Changelog contains v4.7.26 ─────────────────────────────────────
    def test_10_changelog_contains_v4726(self):
        """base.html changelog has v4.7.26 entry."""
        self.assertIn("v4.7.26", self.html_src)
        # Find the v4.7.26 section
        m = re.search(
            r'v4\.7\.26</strong>(.*?)(?=v4\.7\.\d+</strong>)',
            self.html_src,
            re.DOTALL,
        )
        self.assertIsNotNone(m, "v4.7.26 changelog section not found")
        section = m.group(1)
        # Should mention key concepts
        self.assertIn("propagation", section.lower(),
                      "Changelog should mention 'propagation'")
        self.assertIn("aiogram", section.lower(),
                      "Changelog should mention 'aiogram'")

    # ── 11. Changelog mentions that filters didn't work ────────────────────
    def test_11_changelog_mentions_broken_filters(self):
        """Changelog mentions that filters were broken."""
        m = re.search(
            r'v4\.7\.26</strong>(.*?)(?=v4\.7\.\d+</strong>)',
            self.html_src,
            re.DOTALL,
        )
        self.assertIsNotNone(m)
        section = m.group(1).lower()
        # Should mention that filters didn't work
        self.assertTrue(
            "не работали" in section or "не срабатывал" in section,
            "Changelog should mention that filters were not working",
        )
        # Should mention via_bot
        self.assertIn("via_bot", section.lower(),
                      "Changelog should mention via_bot filter")

    # ── Behavioral tests (12-19) — end-to-end with real aiogram Dispatcher ─
    def _build_test_router(self):
        """Build a minimal router that mirrors the production handler chain.

        We can't import the real bot_handlers (it has heavy deps), so we
        recreate the same filter/handler structure here and verify the
        propagation behavior end-to-end with a real aiogram Dispatcher.
        """
        from aiogram import Router, Bot, Dispatcher, F
        from aiogram.filters import BaseFilter
        from aiogram.types import Message, Chat, User, Update

        # Replicate _CMD_ALARM (case-insensitive, with Russian aliases)
        _CMD_ALARM = re.compile(
            r"^!alarm\s+(on|off|вкл|выкл)"
            r"(?:\s+(\d+)\s*(ч|h|м|m|д|d))?"
            r"\s*$",
            re.IGNORECASE,
        )

        # Replicate _CMD_MUTE / _is_moderation_command (minimal)
        _CMD_MUTE = re.compile(r"^!mute\s+(\S+)(?:\s+(.+))?$", re.IGNORECASE)
        _ALL_MOD_COMMANDS = (_CMD_MUTE,)

        def _is_moderation_command(text):
            stripped = (text or "").lstrip()
            if not stripped.startswith("!"):
                return False
            return any(p.match(stripped) for p in _ALL_MOD_COMMANDS)

        # Replicate _AlarmCommandFilter and _ModerationCommandFilter
        class _AlarmCommandFilter(BaseFilter):
            async def __call__(self, message):
                text = message.text
                if not text:
                    return False
                return bool(_CMD_ALARM.match(text))

        class _ModerationCommandFilter(BaseFilter):
            async def __call__(self, message):
                text = message.text
                if not text:
                    return False
                return _is_moderation_command(text)

        router = Router()
        call_log = []

        @router.message(
            F.chat.type.in_(["group", "supergroup"]),
            F.reply_to_message,
            _ModerationCommandFilter(),
        )
        async def handle_group_command(message):
            call_log.append('group-command')

        @router.message(
            F.chat.type.in_(["group", "supergroup"]),
            _AlarmCommandFilter(),
        )
        async def handle_alarm_command(message):
            call_log.append('alarm')

        @router.message(F.chat.type.in_(["group", "supergroup"]))
        async def handle_content_filters(message):
            call_log.append(f'content(via_bot={message.via_bot is not None})')

        @router.message(F.chat.type.in_(["group", "supergroup"]))
        async def stealth_catchall_group(message):
            call_log.append('catchall')

        return router, call_log

    async def _feed(self, router, msg):
        """Feed a message through the dispatcher and return call log."""
        import asyncio
        from aiogram import Bot, Dispatcher
        from aiogram.types import Update

        bot = Bot(token='1234567890:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA')
        dp = Dispatcher()
        dp.include_router(router)
        await dp.feed_update(bot=bot, update=Update(update_id=1, message=msg))

    def _make_msg(self, **kwargs):
        """Build a Message with sensible defaults."""
        from aiogram.types import Message, Chat, User
        defaults = dict(
            message_id=1,
            date=1699999999,
            chat=Chat(id=-100123, type='supergroup', title='Test'),
            from_user=User(id=1, is_bot=False, first_name='Test'),
        )
        defaults.update(kwargs)
        return Message(**defaults)

    def test_12_plain_text_goes_to_content(self):
        """Plain text 'hello world' → handle_content_filters."""
        import asyncio
        router, call_log = self._build_test_router()
        msg = self._make_msg(text='hello world')
        asyncio.run(self._feed(router, msg))
        self.assertEqual(call_log, ['content(via_bot=False)'],
                         f"Plain text should reach content filters, got {call_log}")

    def test_13_reply_without_command_goes_to_content(self):
        """Reply 'just a reply' → handle_content_filters (was BROKEN before fix)."""
        import asyncio
        from aiogram.types import Message
        router, call_log = self._build_test_router()
        reply_to = self._make_msg(message_id=0, text='original')
        msg = self._make_msg(
            text='just a reply, not a command',
            reply_to_message=reply_to,
        )
        asyncio.run(self._feed(router, msg))
        self.assertEqual(call_log, ['content(via_bot=False)'],
                         f"Reply without command should reach content filters, "
                         f"got {call_log}")

    def test_14_reply_with_mute_goes_to_group_command(self):
        """Reply with '!mute 1h spam' → handle_group_command."""
        import asyncio
        router, call_log = self._build_test_router()
        reply_to = self._make_msg(message_id=0, text='spam message')
        msg = self._make_msg(
            text='!mute 1h spam',
            reply_to_message=reply_to,
        )
        asyncio.run(self._feed(router, msg))
        self.assertEqual(call_log, ['group-command'],
                         f"Reply with !mute should reach group-command, got {call_log}")

    def test_15_alarm_on_goes_to_alarm_handler(self):
        """'!alarm on' → handle_alarm_command."""
        import asyncio
        router, call_log = self._build_test_router()
        msg = self._make_msg(text='!alarm on')
        asyncio.run(self._feed(router, msg))
        self.assertEqual(call_log, ['alarm'],
                         f"'!alarm on' should reach alarm handler, got {call_log}")

    def test_16_via_bot_text_goes_to_content(self):
        """Text message with via_bot → handle_content_filters (where filter runs)."""
        import asyncio
        from aiogram.types import User
        router, call_log = self._build_test_router()
        msg = self._make_msg(
            text='poll result',
            via_bot=User(id=222, is_bot=True, first_name='VoteBot', username='vote'),
        )
        asyncio.run(self._feed(router, msg))
        self.assertEqual(call_log, ['content(via_bot=True)'],
                         f"via_bot text should reach content filters, got {call_log}")

    def test_17_via_bot_photo_goes_to_content(self):
        """Photo with via_bot → handle_content_filters."""
        import asyncio
        from aiogram.types import User
        router, call_log = self._build_test_router()
        msg = self._make_msg(
            photo=[],  # empty photo list (size variants)
            caption='via @vote',
            via_bot=User(id=222, is_bot=True, first_name='VoteBot', username='vote'),
        )
        asyncio.run(self._feed(router, msg))
        self.assertEqual(call_log, ['content(via_bot=True)'],
                         f"via_bot photo should reach content filters, got {call_log}")

    def test_18_case_insensitive_alarm(self):
        """'!ALARm on' (case-insensitive) → handle_alarm_command."""
        import asyncio
        router, call_log = self._build_test_router()
        msg = self._make_msg(text='!ALARm on')
        asyncio.run(self._feed(router, msg))
        self.assertEqual(call_log, ['alarm'],
                         f"'!ALARm on' (case-insensitive) should reach alarm, got {call_log}")

    def test_19_non_command_starting_with_alarm_goes_to_content(self):
        """'!alarmXYZ' (not a command) → handle_content_filters."""
        import asyncio
        router, call_log = self._build_test_router()
        msg = self._make_msg(text='!alarmXYZ')
        asyncio.run(self._feed(router, msg))
        self.assertEqual(call_log, ['content(via_bot=False)'],
                         f"'!alarmXYZ' should reach content, got {call_log}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
