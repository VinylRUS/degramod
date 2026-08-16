#!/usr/bin/env python3
"""v4.7.27 — Manual ban reports: structural + behavioural tests.

Tests cover:
  1. bot.py — allowed_updates includes "chat_member" (webhook + polling).
  2. bot_handlers.py — `import time` is present (used by _mark_bot_ban).
  3. bot_handlers.py — `_recent_bot_bans` dict + `_BOT_BAN_DEDUP_TTL_SEC` constant.
  4. bot_handlers.py — `_mark_bot_ban(chat_id, user_id)` function defined.
  5. bot_handlers.py — `_consume_bot_ban(chat_id, user_id) -> bool` function defined.
  6. bot_handlers.py — all 5 `ban_chat_member` calls are followed by `_mark_bot_ban`.
  7. bot_handlers.py — `_send_manual_ban_report` async function defined.
  8. bot_handlers.py — `@router.chat_member()` handler `on_chat_member_updated` exists.
  9. web_app.py — APP_VERSION bumped to v4.7.27.
 10. base.html — changelog entry for v4.7.27 exists.
 11. Behavioural — `_consume_bot_ban` returns True immediately after `_mark_bot_ban`.
 12. Behavioural — `_consume_bot_ban` returns False when no mark was made.
 13. Behavioural — `_consume_bot_ban` pops the entry (second call returns False).
 14. Behavioural — TTL cleanup: stale entries are removed.
 15. Behavioural — handler ignores new_status != "kicked".
 16. Behavioural — handler ignores old_status == "kicked" (already-banned edge case).

Run:  python scripts/test_v4727_manual_ban_report.py
"""
from __future__ import annotations

import ast
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# Python 3.8+: IsolatedAsyncioTestCase for async test methods
from unittest import IsolatedAsyncioTestCase

# Path setup
HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)
sys.path.insert(0, PROJECT_ROOT)


def _read(rel: str) -> str:
    with open(os.path.join(PROJECT_ROOT, rel), "r", encoding="utf-8") as f:
        return f.read()


# ════════════════════════════════════════════════════════════════════════════
# 1. Structural tests — bot.py
# ════════════════════════════════════════════════════════════════════════════
class TestBotPyStructural(unittest.TestCase):

    def test_01_allowed_updates_includes_chat_member_webhook(self):
        """set_webhook call must include 'chat_member' in allowed_updates."""
        src = _read("bot.py")
        # Find set_webhook(...) call and check its allowed_updates kwarg
        # AST-based check: any set_webhook call with allowed_updates containing "chat_member"
        tree = ast.parse(src)
        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, 'attr', None) == 'set_webhook':
                for kw in node.keywords:
                    if kw.arg == 'allowed_updates' and isinstance(kw.value, ast.List):
                        vals = [e.value for e in kw.value.elts if isinstance(e, ast.Constant)]
                        if 'chat_member' in vals:
                            found = True
        self.assertTrue(found, "bot.py: set_webhook must include 'chat_member' in allowed_updates")

    def test_02_allowed_updates_includes_chat_member_polling(self):
        """dp.start_polling call must include 'chat_member' in allowed_updates."""
        src = _read("bot.py")
        tree = ast.parse(src)
        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, 'attr', None) == 'start_polling':
                for kw in node.keywords:
                    if kw.arg == 'allowed_updates' and isinstance(kw.value, ast.List):
                        vals = [e.value for e in kw.value.elts if isinstance(e, ast.Constant)]
                        if 'chat_member' in vals:
                            found = True
        self.assertTrue(found, "bot.py: dp.start_polling must include 'chat_member' in allowed_updates")

    def test_03_allowed_updates_still_includes_my_chat_member(self):
        """my_chat_member must still be present (regression: v4.5.1 feature)."""
        src = _read("bot.py")
        self.assertIn('"my_chat_member"', src, "bot.py: 'my_chat_member' must remain in allowed_updates")
        self.assertIn('"message"', src, "bot.py: 'message' must remain in allowed_updates")


# ════════════════════════════════════════════════════════════════════════════
# 2. Structural tests — bot_handlers.py
# ════════════════════════════════════════════════════════════════════════════
class TestBotHandlersStructural(unittest.TestCase):

    def test_10_import_time_present(self):
        """`import time` must be in bot_handlers.py (used by _mark_bot_ban)."""
        src = _read("bot_handlers.py")
        tree = ast.parse(src)
        import_time_found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == 'time':
                        import_time_found = True
            elif isinstance(node, ast.ImportFrom):
                if node.module == 'time':
                    import_time_found = True
        self.assertTrue(import_time_found, "bot_handlers.py: 'import time' must be present")

    def test_11_recent_bot_bans_dict_exists(self):
        """`_recent_bot_bans` global dict must be declared."""
        src = _read("bot_handlers.py")
        self.assertIn("_recent_bot_bans", src)
        # Must be a dict assignment at module level
        self.assertIn(
            "_recent_bot_bans: dict[tuple[int, int], float]",
            src,
            "_recent_bot_bans must be declared as dict[tuple[int,int], float]",
        )

    def test_12_dedup_ttl_constant_exists(self):
        """`_BOT_BAN_DEDUP_TTL_SEC` constant must be declared."""
        src = _read("bot_handlers.py")
        self.assertIn("_BOT_BAN_DEDUP_TTL_SEC", src)
        self.assertIn(
            "_BOT_BAN_DEDUP_TTL_SEC: float = 10.0",
            src,
            "_BOT_BAN_DEDUP_TTL_SEC must be declared as float = 10.0",
        )

    def test_13_mark_bot_ban_function_defined(self):
        """`_mark_bot_ban(chat_id, user_id)` function must exist."""
        src = _read("bot_handlers.py")
        tree = ast.parse(src)
        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == '_mark_bot_ban':
                # Check signature: (chat_id: int, user_id: int) -> None
                args = [a.arg for a in node.args.args]
                self.assertEqual(args, ['chat_id', 'user_id'],
                                 "_mark_bot_ban must accept (chat_id, user_id)")
                found = True
        self.assertTrue(found, "_mark_bot_ban function must be defined")

    def test_14_consume_bot_ban_function_defined(self):
        """`_consume_bot_ban(chat_id, user_id) -> bool` function must exist."""
        src = _read("bot_handlers.py")
        tree = ast.parse(src)
        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == '_consume_bot_ban':
                args = [a.arg for a in node.args.args]
                self.assertEqual(args, ['chat_id', 'user_id'],
                                 "_consume_bot_ban must accept (chat_id, user_id)")
                # Check return annotation: bool
                ret = node.returns
                self.assertIsNotNone(ret, "_consume_bot_ban must have return annotation")
                # ast for "bool" is ast.Name(id='bool')
                if isinstance(ret, ast.Name):
                    self.assertEqual(ret.id, 'bool',
                                     "_consume_bot_ban must return bool")
                found = True
        self.assertTrue(found, "_consume_bot_ban function must be defined")

    def test_15_all_ban_chat_member_calls_have_mark_bot_ban(self):
        """Every `await ...ban_chat_member(...)` must be followed by `_mark_bot_ban(...)`."""
        src = _read("bot_handlers.py")
        tree = ast.parse(src)
        violations = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Await):
                inner = node.value
                if (isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Attribute)
                    and inner.func.attr == 'ban_chat_member'):
                    # Find enclosing try/except or statement block
                    # Strategy: walk parents — but AST doesn't have parent links.
                    # Simpler: check the source lines around the await.
                    # Use 15-line window: enough to skip past try/except blocks
                    # (max realistic: 9 lines for except+inner try+return in !ban handler).
                    lineno = node.lineno
                    lines = src.splitlines()
                    following = "\n".join(lines[lineno: lineno + 15])
                    if '_mark_bot_ban' not in following:
                        violations.append(lineno)
        self.assertEqual(violations, [],
                         f"ban_chat_member calls at lines {violations} lack _mark_bot_ban within 15 lines")

    def test_16_count_ban_chat_member_calls(self):
        """There must be at least 5 ban_chat_member calls (regression sanity check)."""
        src = _read("bot_handlers.py")
        count = src.count("ban_chat_member(")
        # 5 ban calls + at least 5 _mark_bot_ban calls + maybe comments.
        # Just verify minimum number of ban calls:
        self.assertGreaterEqual(count, 5,
                               "Expected at least 5 ban_chat_member calls in bot_handlers.py")

    def test_17_send_manual_ban_report_function_exists(self):
        """`_send_manual_ban_report` async function must exist."""
        src = _read("bot_handlers.py")
        tree = ast.parse(src)
        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == '_send_manual_ban_report':
                # Check that it accepts the expected keyword-only args
                kw_only = [a.arg for a in node.args.kwonlyargs]
                expected = {'bot', 'chat_id', 'target', 'admin', 'report_dest', 'hashtag'}
                self.assertEqual(set(kw_only), expected,
                                 f"_send_manual_ban_report kw-only args must be {expected}")
                found = True
        self.assertTrue(found, "_send_manual_ban_report async function must be defined")

    def test_18_chat_member_handler_exists(self):
        """`@router.chat_member()` handler `on_chat_member_updated` must exist."""
        src = _read("bot_handlers.py")
        tree = ast.parse(src)
        handler_found = False
        decorator_found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == 'on_chat_member_updated':
                handler_found = True
                # Check decorators for @router.chat_member()
                for dec in node.decorator_list:
                    if (isinstance(dec, ast.Call)
                        and isinstance(dec.func, ast.Attribute)
                        and dec.func.attr == 'chat_member'):
                        decorator_found = True
        self.assertTrue(handler_found, "on_chat_member_updated handler must exist")
        self.assertTrue(decorator_found,
                        "on_chat_member_updated must have @router.chat_member() decorator")

    def test_19_handler_calls_consume_bot_ban(self):
        """on_chat_member_updated must call _consume_bot_ban for dedup."""
        src = _read("bot_handlers.py")
        tree = ast.parse(src)
        handler_calls_consume = False
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == 'on_chat_member_updated':
                # Walk the handler body looking for _consume_bot_ban call
                for sub in ast.walk(node):
                    if (isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Name)
                        and sub.func.id == '_consume_bot_ban'):
                        handler_calls_consume = True
                        break
        self.assertTrue(handler_calls_consume,
                        "on_chat_member_updated must call _consume_bot_ban")

    def test_20_handler_calls_send_manual_ban_report(self):
        """on_chat_member_updated must call _send_manual_ban_report for manual bans."""
        src = _read("bot_handlers.py")
        tree = ast.parse(src)
        handler_calls_send = False
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == 'on_chat_member_updated':
                for sub in ast.walk(node):
                    if (isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Name)
                        and sub.func.id == '_send_manual_ban_report'):
                        handler_calls_send = True
                        break
        self.assertTrue(handler_calls_send,
                        "on_chat_member_updated must call _send_manual_ban_report")

    def test_21_manual_ban_label_in_report(self):
        """_send_manual_ban_report must use '🚫 БАН (ручной)' label (not '🚫 БАН')."""
        src = _read("bot_handlers.py")
        self.assertIn("🚫 БАН (ручной)", src,
                      "_send_manual_ban_report must use '🚫 БАН (ручной)' label")


# ════════════════════════════════════════════════════════════════════════════
# 3. Structural tests — web_app.py
# ════════════════════════════════════════════════════════════════════════════
class TestWebAppStructural(unittest.TestCase):

    def test_30_app_version_bumped_to_v4727(self):
        """APP_VERSION in web_app.py must be >= v4.7.27 (v4.7.28+ OK too —
        later versions don't cancel the v4.7.27 manual-ban-report feature)."""
        src = _read("web_app.py")
        # Look for APP_VERSION = "vX.Y.Z" and parse it
        import re
        m = re.search(r'APP_VERSION\s*=\s*"v(\d+)\.(\d+)\.(\d+)"', src)
        self.assertIsNotNone(m, "APP_VERSION assignment not found in web_app.py")
        major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
        self.assertTrue(
            (major, minor, patch) >= (4, 7, 27),
            f"APP_VERSION must be >= v4.7.27, got v{major}.{minor}.{patch}",
        )


# ════════════════════════════════════════════════════════════════════════════
# 4. Structural tests — templates/base.html
# ════════════════════════════════════════════════════════════════════════════
class TestBaseHtmlStructural(unittest.TestCase):

    def test_40_changelog_has_v4727_entry(self):
        """base.html changelog must contain a v4.7.27 entry."""
        src = _read("templates/base.html")
        self.assertIn("v4.7.27", src, "templates/base.html: changelog must mention v4.7.27")

    def test_41_changelog_mentions_manual_ban(self):
        """Changelog must mention 'ручной бан' / 'manual ban'."""
        src = _read("templates/base.html")
        # Russian changelog — search for "ручн" (covers "ручной", "ручного", etc.)
        # Limit to the v4.7.27 section (after the first "v4.7.27" occurrence).
        idx = src.find("v4.7.27")
        self.assertGreater(idx, 0, "v4.7.27 entry must exist in changelog")
        section = src[idx: idx + 5000]
        self.assertIn("ручн", section, "Changelog v4.7.27 must mention 'ручной бан'")

    def test_42_changelog_mentions_dedup(self):
        """Changelog must explain deduplication."""
        src = _read("templates/base.html")
        idx = src.find("v4.7.27")
        section = src[idx: idx + 8000]
        # Should mention dedup mechanism
        self.assertTrue(
            "дублиров" in section or "dedup" in section.lower(),
            "Changelog v4.7.27 must explain deduplication mechanism",
        )


# ════════════════════════════════════════════════════════════════════════════
# 5. Behavioural tests — _mark_bot_ban / _consume_bot_ban
# ════════════════════════════════════════════════════════════════════════════
class TestDedupBehavioural(unittest.TestCase):
    """Tests that _mark_bot_ban / _consume_bot_ban work correctly.

    These tests import the actual module functions — they don't mock anything.
    They share state with the real `_recent_bot_bans` dict, so we clean up
    before/after each test.
    """

    def setUp(self):
        # Clean up the dict before each test
        from bot_handlers import _recent_bot_bans
        _recent_bot_bans.clear()

    def tearDown(self):
        from bot_handlers import _recent_bot_bans
        _recent_bot_bans.clear()

    def test_50_consume_returns_false_when_no_mark(self):
        """_consume_bot_ban returns False when no _mark_bot_ban was called."""
        from bot_handlers import _consume_bot_ban
        result = _consume_bot_ban(chat_id=-100123, user_id=99999)
        self.assertFalse(result, "consume must return False when no mark was made")

    def test_51_consume_returns_true_after_mark(self):
        """_consume_bot_ban returns True immediately after _mark_bot_ban."""
        from bot_handlers import _mark_bot_ban, _consume_bot_ban
        _mark_bot_ban(chat_id=-100123, user_id=88888)
        result = _consume_bot_ban(chat_id=-100123, user_id=88888)
        self.assertTrue(result, "consume must return True right after mark")

    def test_52_consume_pops_entry_after_call(self):
        """_consume_bot_ban must remove the entry — second call returns False."""
        from bot_handlers import _mark_bot_ban, _consume_bot_ban
        _mark_bot_ban(chat_id=-100123, user_id=77777)
        first = _consume_bot_ban(chat_id=-100123, user_id=77777)
        second = _consume_bot_ban(chat_id=-100123, user_id=77777)
        self.assertTrue(first, "first consume must return True")
        self.assertFalse(second, "second consume must return False (entry popped)")

    def test_53_mark_for_different_chat_doesnt_match(self):
        """_mark_bot_ban for chat A doesn't satisfy _consume_bot_ban for chat B."""
        from bot_handlers import _mark_bot_ban, _consume_bot_ban
        _mark_bot_ban(chat_id=-100111, user_id=55555)
        result = _consume_bot_ban(chat_id=-100222, user_id=55555)
        self.assertFalse(result, "mark for chat A must not match consume for chat B")

    def test_54_mark_for_different_user_doesnt_match(self):
        """_mark_bot_ban for user A doesn't satisfy _consume_bot_ban for user B."""
        from bot_handlers import _mark_bot_ban, _consume_bot_ban
        _mark_bot_ban(chat_id=-100123, user_id=44444)
        result = _consume_bot_ban(chat_id=-100123, user_id=33333)
        self.assertFalse(result, "mark for user A must not match consume for user B")

    def test_55_ttl_cleanup_removes_stale_entries(self):
        """_consume_bot_ban must clean up entries older than _BOT_BAN_DEDUP_TTL_SEC."""
        from bot_handlers import (
            _mark_bot_ban,
            _consume_bot_ban,
            _recent_bot_bans,
            _BOT_BAN_DEDUP_TTL_SEC,
        )
        # Manually insert a stale entry (timestamp far in the past)
        # _recent_bot_bans stores monotonic timestamps — use very negative value
        _recent_bot_bans[(-100123, 66666)] = -1_000_000.0  # very old
        # Now mark a fresh entry
        _mark_bot_ban(chat_id=-100123, user_id=22222)
        # Consume the fresh one — should also clean up the stale one
        result = _consume_bot_ban(chat_id=-100123, user_id=22222)
        self.assertTrue(result, "fresh mark must be consumed")
        # Stale entry must be cleaned up
        self.assertNotIn((-100123, 66666), _recent_bot_bans,
                         "stale entry must be removed by TTL cleanup")

    def test_56_dedup_ttl_is_10_seconds(self):
        """_BOT_BAN_DEDUP_TTL_SEC must be exactly 10.0 (per design)."""
        from bot_handlers import _BOT_BAN_DEDUP_TTL_SEC
        self.assertEqual(_BOT_BAN_DEDUP_TTL_SEC, 10.0,
                         "TTL must be 10.0 seconds per design")


# ════════════════════════════════════════════════════════════════════════════
# 6. Behavioural tests — on_chat_member_updated handler
# ════════════════════════════════════════════════════════════════════════════
class TestChatMemberHandlerBehavioural(IsolatedAsyncioTestCase):
    """Tests that on_chat_member_updated filters out non-ban updates."""

    def setUp(self):
        from bot_handlers import _recent_bot_bans
        _recent_bot_bans.clear()

    def tearDown(self):
        from bot_handlers import _recent_bot_bans
        _recent_bot_bans.clear()

    def _make_event(self, new_status: str, old_status: str = "member",
                    chat_id: int = -100123, user_id: int = 99999,
                    admin_id: int = 11111, admin_is_bot: bool = False):
        """Build a mock ChatMemberUpdated event for testing."""
        event = MagicMock()
        event.chat = MagicMock(id=chat_id)
        # new_chat_member.user
        new_member = MagicMock()
        new_member.status = new_status
        new_member.user = MagicMock(
            id=user_id,
            username=f"user_{user_id}",
            first_name=f"User{user_id}",
            last_name="Test",
            is_bot=False,
        )
        event.new_chat_member = new_member
        # old_chat_member.status
        old_member = MagicMock()
        old_member.status = old_status
        event.old_chat_member = old_member
        # from_user (admin who banned)
        event.from_user = MagicMock(
            id=admin_id,
            username=f"admin_{admin_id}",
            first_name=f"Admin{admin_id}",
            last_name="Bigboss",
            is_bot=admin_is_bot,
        )
        # bot
        event.bot = AsyncMock()
        return event

    @patch("bot_handlers._send_manual_ban_report", new_callable=AsyncMock)
    @patch("bot_handlers._get_report_chat_id", new_callable=AsyncMock)
    @patch("bot_handlers._get_chat_settings", new_callable=AsyncMock)
    @patch("bot_handlers._upsert_user", new_callable=AsyncMock)
    async def test_60_handler_ignores_member_status(
        self, mock_upsert, mock_settings, mock_report_dest, mock_send
    ):
        """Handler must ignore new_status='member' (not a ban)."""
        from bot_handlers import on_chat_member_updated
        event = self._make_event(new_status="member", old_status="left")
        await on_chat_member_updated(event)
        # _send_manual_ban_report must NOT be called
        mock_send.assert_not_called()

    @patch("bot_handlers._send_manual_ban_report", new_callable=AsyncMock)
    async def test_61_handler_ignores_administrator_status(self, mock_send):
        """Handler must ignore new_status='administrator' (promotion, not ban)."""
        from bot_handlers import on_chat_member_updated
        event = self._make_event(new_status="administrator", old_status="member")
        await on_chat_member_updated(event)
        mock_send.assert_not_called()

    @patch("bot_handlers._send_manual_ban_report", new_callable=AsyncMock)
    async def test_62_handler_ignores_left_status(self, mock_send):
        """Handler must ignore new_status='left' (user left voluntarily, not ban)."""
        from bot_handlers import on_chat_member_updated
        event = self._make_event(new_status="left", old_status="member")
        await on_chat_member_updated(event)
        mock_send.assert_not_called()

    @patch("bot_handlers._send_manual_ban_report", new_callable=AsyncMock)
    async def test_63_handler_ignores_restricted_status(self, mock_send):
        """Handler must ignore new_status='restricted' (mute, not ban)."""
        from bot_handlers import on_chat_member_updated
        event = self._make_event(new_status="restricted", old_status="member")
        await on_chat_member_updated(event)
        mock_send.assert_not_called()

    @patch("bot_handlers._send_manual_ban_report", new_callable=AsyncMock)
    async def test_64_handler_ignores_already_kicked(self, mock_send):
        """Handler must ignore when old_status='kicked' (already banned, not new ban)."""
        from bot_handlers import on_chat_member_updated
        event = self._make_event(new_status="kicked", old_status="kicked")
        await on_chat_member_updated(event)
        mock_send.assert_not_called()

    @patch("bot_handlers._send_manual_ban_report", new_callable=AsyncMock)
    @patch("bot_handlers._get_report_chat_id", new_callable=AsyncMock)
    @patch("bot_handlers._get_chat_settings", new_callable=AsyncMock)
    @patch("bot_handlers._upsert_user", new_callable=AsyncMock)
    async def test_65_handler_dedup_skips_bot_own_ban(
        self, mock_upsert, mock_settings, mock_report_dest, mock_send
    ):
        """Handler must NOT send manual-ban report when _mark_bot_ban was called."""
        from bot_handlers import on_chat_member_updated, _mark_bot_ban
        # Simulate: bot just banned this user via !ban
        _mark_bot_ban(chat_id=-100123, user_id=99999)
        # Now ChatMemberUpdated arrives
        event = self._make_event(
            new_status="kicked", old_status="member",
            chat_id=-100123, user_id=99999,
        )
        await on_chat_member_updated(event)
        # Manual ban report must NOT be sent (dedup'd)
        mock_send.assert_not_called()
        # And upsert must NOT be called either (early return)
        mock_upsert.assert_not_called()

    @patch("bot_handlers._send_manual_ban_report", new_callable=AsyncMock)
    @patch("bot_handlers._get_report_chat_id", new_callable=AsyncMock)
    @patch("bot_handlers._get_chat_settings", new_callable=AsyncMock)
    @patch("bot_handlers._upsert_user", new_callable=AsyncMock)
    async def test_66_handler_sends_report_for_manual_ban(
        self, mock_upsert, mock_settings, mock_report_dest, mock_send
    ):
        """Handler MUST send manual-ban report when no _mark_bot_ban was called."""
        from bot_handlers import on_chat_member_updated
        mock_report_dest.return_value = -100999  # reporting chat ID
        mock_settings.return_value = MagicMock(hashtag="#TestChat")
        event = self._make_event(
            new_status="kicked", old_status="member",
            chat_id=-100123, user_id=99999,
            admin_id=11111,
        )
        await on_chat_member_updated(event)
        # Manual ban report MUST be sent
        mock_send.assert_awaited_once()
        # Check args
        call_kwargs = mock_send.call_args.kwargs
        self.assertEqual(call_kwargs['chat_id'], -100123)
        self.assertEqual(call_kwargs['report_dest'], -100999)
        self.assertEqual(call_kwargs['hashtag'], "#TestChat")
        # target should be the user from event
        self.assertEqual(call_kwargs['target'].id, 99999)
        # admin should be the from_user
        self.assertEqual(call_kwargs['admin'].id, 11111)

    @patch("bot_handlers._send_manual_ban_report", new_callable=AsyncMock)
    @patch("bot_handlers._get_report_chat_id", new_callable=AsyncMock)
    @patch("bot_handlers._get_chat_settings", new_callable=AsyncMock)
    @patch("bot_handlers._upsert_user", new_callable=AsyncMock)
    async def test_67_handler_skips_when_no_report_chat(
        self, mock_upsert, mock_settings, mock_report_dest, mock_send
    ):
        """Handler must NOT send report when report_dest is None (no reporting chat set)."""
        from bot_handlers import on_chat_member_updated
        mock_report_dest.return_value = None  # no reporting chat
        event = self._make_event(
            new_status="kicked", old_status="member",
            chat_id=-100123, user_id=99999,
        )
        await on_chat_member_updated(event)
        mock_send.assert_not_called()

    @patch("bot_handlers._send_manual_ban_report", new_callable=AsyncMock)
    @patch("bot_handlers._get_report_chat_id", new_callable=AsyncMock)
    @patch("bot_handlers._get_chat_settings", new_callable=AsyncMock)
    @patch("bot_handlers._upsert_user", new_callable=AsyncMock)
    async def test_68_handler_upserts_user_before_report(
        self, mock_upsert, mock_settings, mock_report_dest, mock_send
    ):
        """Handler must upsert user before sending report (for web-panel profile)."""
        from bot_handlers import on_chat_member_updated
        mock_report_dest.return_value = -100999
        mock_settings.return_value = MagicMock(hashtag="")
        event = self._make_event(
            new_status="kicked", old_status="member",
            chat_id=-100123, user_id=99999,
        )
        await on_chat_member_updated(event)
        # _upsert_user must have been called (with the target user info)
        mock_upsert.assert_awaited_once()
        upsert_args = mock_upsert.call_args.args
        # Signature: _upsert_user(session, user_id, username, first_name, last_name)
        # session is first positional arg, then user_id
        self.assertEqual(upsert_args[1], 99999, "user_id must match event user")


# ════════════════════════════════════════════════════════════════════════════
# 7. Run all tests
# ════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    unittest.main(verbosity=2)
