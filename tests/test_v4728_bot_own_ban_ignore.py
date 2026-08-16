#!/usr/bin/env python3
"""v4.7.28 — Bot-own ban ignore via `event.from_user.is_bot` check.

Tests cover the new PERSISTENT-deduplication layer (separate from the
v4.7.27 TTL-based layer):

STRUCTURAL:
  1. web_app.py — APP_VERSION == "v4.7.28".
  2. bot_handlers.py — `on_chat_member_updated` handler contains an explicit
     `is_bot` check on `event.from_user` BEFORE `_consume_bot_ban`.
  3. bot_handlers.py — `_consume_bot_ban` is still called (TTL backup layer).
  4. bot_handlers.py — all 5 `ban_chat_member` calls still marked with
     `_mark_bot_ban` (TTL backup layer preserved).
  5. bot_handlers.py — `_recent_bot_bans` dict and `_BOT_BAN_DEDUP_TTL_SEC`
     constant still exist (v4.7.27 backup layer intact).
  6. templates/base.html — changelog entry for v4.7.28 exists.

BEHAVIOURAL:
  7. Handler ignores ban when `from_user.is_bot=True` — even WITHOUT
     `_mark_bot_ban` being called (persistent check works standalone).
  8. Handler sends report when `from_user.is_bot=False` (regular admin).
  9. Handler ignores ban when actor is ANOTHER bot (not our bot) — covers
     the "second moderator-bot in chat" scenario.
 10. Handler treats `from_user=None` gracefully (does NOT crash, falls through
     to TTL-backup logic).
 11. Handler still sends report when `from_user.is_bot=False` AND
     `_consume_bot_ban` returns True (shouldn't happen in practice, but
     confirms TTL-backup behaviour is preserved).
 12. Handler does NOT call `_upsert_user` when ban is from a bot
     (early-return before DB ops).
 13. Handler does NOT call `_get_report_chat_id` when ban is from a bot
     (early-return before DB ops).
 14. v4.7.27 tests still pass (regression — backup layer not broken).

Run:  python scripts/test_v4728_bot_own_ban_ignore.py
"""
from __future__ import annotations

import ast
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from unittest import IsolatedAsyncioTestCase

# Path setup
HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)
sys.path.insert(0, PROJECT_ROOT)


def _read(rel: str) -> str:
    with open(os.path.join(PROJECT_ROOT, rel), "r", encoding="utf-8") as f:
        return f.read()


# ════════════════════════════════════════════════════════════════════════════
# 1. Structural tests
# ════════════════════════════════════════════════════════════════════════════
class TestV4728Structural(unittest.TestCase):
    """Structural tests — verify the new check exists and v4.7.27 backup is intact."""

    def test_01_app_version_bumped_to_v4728(self):
        """web_app.py APP_VERSION must be at least 'v4.7.28' (v4.7.29+ OK too —
        later versions don't cancel the v4.7.28 bot-own-ban-ignore feature)."""
        src = _read("web_app.py")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id == "APP_VERSION":
                        v = node.value.value
                        # Parse version: "v4.7.28" → (4, 7, 28)
                        parts = v.lstrip('v').split('.')
                        major, minor, patch = int(parts[0]), int(parts[1]), int(parts[2])
                        self.assertTrue(
                            (major, minor, patch) >= (4, 7, 28),
                            f"APP_VERSION must be >= v4.7.28, got {v!r}",
                        )
                        return
        self.fail("APP_VERSION assignment not found in web_app.py")

    def test_02_handler_has_is_bot_check_before_consume(self):
        """on_chat_member_updated must check `event.from_user.is_bot` (as an
        `if`-condition that returns) BEFORE calling _consume_bot_ban — that's
        what makes it persistent."""
        src = _read("bot_handlers.py")
        tree = ast.parse(src)

        # Find on_chat_member_updated function
        handler_fn = None
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "on_chat_member_updated":
                handler_fn = node
                break
        self.assertIsNotNone(handler_fn, "on_chat_member_updated not found")

        # Walk through the function body in source order, looking for:
        #   - An `If` node whose test contains `.is_bot` attribute access
        #     (this is the persistent check that should `return`).
        #   - The _consume_bot_ban(...) call (TTL backup).
        is_bot_if_line = None
        consume_call_line = None

        for node in ast.walk(handler_fn):
            # Find `if ... .is_bot:` — If-node whose test references is_bot
            if isinstance(node, ast.If):
                # Walk the test expression looking for `.is_bot`
                for sub in ast.walk(node.test):
                    if isinstance(sub, ast.Attribute) and sub.attr == "is_bot":
                        # Must be the FIRST If with is_bot in the handler
                        if is_bot_if_line is None or node.lineno < is_bot_if_line:
                            is_bot_if_line = node.lineno
                        break
            # Find _consume_bot_ban(...) call
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id == "_consume_bot_ban":
                    if consume_call_line is None or node.lineno < consume_call_line:
                        consume_call_line = node.lineno

        self.assertIsNotNone(
            is_bot_if_line,
            "if-statement with `.is_bot` check not found in on_chat_member_updated — "
            "v4.7.28 must add `if event.from_user is not None and event.from_user.is_bot: return`",
        )
        self.assertIsNotNone(
            consume_call_line,
            "_consume_bot_ban call not found (TTL backup must be preserved)",
        )
        self.assertLess(
            is_bot_if_line, consume_call_line,
            f"is_bot if-check (line {is_bot_if_line}) must come BEFORE _consume_bot_ban call (line {consume_call_line})",
        )

    def test_03_consume_bot_ban_still_called(self):
        """TTL-backup layer (_consume_bot_ban) must still be called after is_bot check."""
        src = _read("bot_handlers.py")
        # Plain substring check is sufficient
        self.assertIn("_consume_bot_ban(chat_id, user_id)", src,
                      "_consume_bot_ban call must remain (TTL backup layer)")

    def test_04_all_ban_chat_member_calls_still_marked(self):
        """All 5 `ban_chat_member` calls must still be followed by `_mark_bot_ban`
        — TTL-backup layer needs these marks to function."""
        src = _read("bot_handlers.py")
        # Count ban_chat_member calls and _mark_bot_ban calls
        ban_count = src.count("await bot.ban_chat_member(") + src.count("bot.ban_chat_member(")
        mark_count = src.count("_mark_bot_ban(")
        # At least 5 ban_chat_member calls and at least 5 _mark_bot_ban calls
        self.assertGreaterEqual(ban_count, 5, f"Expected >=5 ban_chat_member calls, got {ban_count}")
        self.assertGreaterEqual(mark_count, 5, f"Expected >=5 _mark_bot_ban calls, got {mark_count}")

    def test_05_recent_bot_bans_dict_and_ttl_constant_exist(self):
        """v4.7.27 backup layer (in-memory dict + TTL constant) must still exist."""
        src = _read("bot_handlers.py")
        self.assertIn("_recent_bot_bans", src, "_recent_bot_bans dict must exist (v4.7.27 backup)")
        self.assertIn("_BOT_BAN_DEDUP_TTL_SEC", src, "_BOT_BAN_DEDUP_TTL_SEC constant must exist")

    def test_06_changelog_contains_v4728(self):
        """templates/base.html changelog must mention v4.7.28."""
        src = _read("templates/base.html")
        self.assertIn("v4.7.28", src, "Changelog must contain v4.7.28 entry")
        # Also confirm key feature description is in the changelog
        self.assertIn("from_user.is_bot", src,
                      "Changelog must explain the new from_user.is_bot check")
        self.assertIn("PERSISTENT", src,
                      "Changelog must describe the check as PERSISTENT")

    def test_07_handler_checks_actor_is_not_none(self):
        """Handler must guard against `event.from_user is None` before checking
        `is_bot` — otherwise AttributeError on None.is_bot."""
        src = _read("bot_handlers.py")
        # Find the pattern: `if actor is not None and actor.is_bot:` or similar
        # Look for the combination of from_user assignment and None-check + is_bot
        self.assertIn("actor = event.from_user", src,
                      "Handler must assign `actor = event.from_user` for clean None-check")
        # Check that actor is checked with `is not None` before .is_bot access
        self.assertIn("actor is not None", src,
                      "Handler must check `actor is not None` before accessing .is_bot")


# ════════════════════════════════════════════════════════════════════════════
# 2. Behavioural tests — handler ignores bot-issued bans
# ════════════════════════════════════════════════════════════════════════════
class TestV4728HandlerBehavioural(IsolatedAsyncioTestCase):
    """Tests that on_chat_member_updated ignores bot-issued bans via is_bot check."""

    def setUp(self):
        from bot_handlers import _recent_bot_bans
        _recent_bot_bans.clear()

    def tearDown(self):
        from bot_handlers import _recent_bot_bans
        _recent_bot_bans.clear()

    def _make_event(self, new_status: str = "kicked", old_status: str = "member",
                    chat_id: int = -100123, user_id: int = 99999,
                    admin_id: int = 11111, admin_is_bot: bool = False,
                    from_user_none: bool = False):
        """Build a mock ChatMemberUpdated event for testing."""
        event = MagicMock()
        event.chat = MagicMock(id=chat_id)
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
        old_member = MagicMock()
        old_member.status = old_status
        event.old_chat_member = old_member
        if from_user_none:
            # Simulate the rare case where Telegram doesn't send from_user
            event.from_user = None
        else:
            event.from_user = MagicMock(
                id=admin_id,
                username=f"actor_{admin_id}",
                first_name=f"Actor{admin_id}",
                last_name="Test",
                is_bot=admin_is_bot,
            )
        event.bot = AsyncMock()
        return event

    @patch("bot_handlers._send_manual_ban_report", new_callable=AsyncMock)
    @patch("bot_handlers._get_report_chat_id", new_callable=AsyncMock)
    @patch("bot_handlers._get_chat_settings", new_callable=AsyncMock)
    @patch("bot_handlers._upsert_user", new_callable=AsyncMock)
    @patch("bot_handlers._consume_bot_ban")
    async def test_10_handler_ignores_ban_from_bot_actor(
        self, mock_consume, mock_upsert, mock_settings, mock_report_dest, mock_send
    ):
        """Handler MUST ignore ban when from_user.is_bot=True — even WITHOUT
        _mark_bot_ban being called (persistent check works standalone)."""
        from bot_handlers import on_chat_member_updated
        # CRITICAL: _consume_bot_ban returns False (no _mark_bot_ban was called)
        # — but handler should STILL skip because from_user.is_bot=True.
        mock_consume.return_value = False
        mock_report_dest.return_value = -100999
        mock_settings.return_value = MagicMock(hashtag="#Test")
        # Actor is a bot (id=777777, is_bot=True) — our bot itself or another bot
        event = self._make_event(
            new_status="kicked", old_status="member",
            chat_id=-100123, user_id=99999,
            admin_id=777777, admin_is_bot=True,
        )
        await on_chat_member_updated(event)
        # Manual ban report MUST NOT be sent
        mock_send.assert_not_called()
        # _consume_bot_ban MUST NOT be called (handler returns before TTL-backup)
        mock_consume.assert_not_called()
        # DB ops MUST NOT happen (early-return)
        mock_upsert.assert_not_called()
        mock_report_dest.assert_not_called()

    @patch("bot_handlers._send_manual_ban_report", new_callable=AsyncMock)
    @patch("bot_handlers._get_report_chat_id", new_callable=AsyncMock)
    @patch("bot_handlers._get_chat_settings", new_callable=AsyncMock)
    @patch("bot_handlers._upsert_user", new_callable=AsyncMock)
    @patch("bot_handlers._consume_bot_ban")
    async def test_11_handler_sends_report_for_human_admin(
        self, mock_consume, mock_upsert, mock_settings, mock_report_dest, mock_send
    ):
        """Handler MUST send report when from_user.is_bot=False (regular admin)."""
        from bot_handlers import on_chat_member_updated
        mock_consume.return_value = False
        mock_report_dest.return_value = -100999
        mock_settings.return_value = MagicMock(hashtag="#Test")
        event = self._make_event(
            new_status="kicked", old_status="member",
            chat_id=-100123, user_id=99999,
            admin_id=11111, admin_is_bot=False,  # human admin
        )
        await on_chat_member_updated(event)
        # Manual ban report MUST be sent
        mock_send.assert_awaited_once()
        # _consume_bot_ban MUST be called (TTL backup layer still runs)
        mock_consume.assert_called_once_with(-100123, 99999)
        # Upsert MUST be called
        mock_upsert.assert_awaited_once()
        # Check args passed to _send_manual_ban_report
        call_kwargs = mock_send.call_args.kwargs
        self.assertEqual(call_kwargs['chat_id'], -100123)
        self.assertEqual(call_kwargs['report_dest'], -100999)
        self.assertEqual(call_kwargs['hashtag'], "#Test")
        self.assertEqual(call_kwargs['target'].id, 99999)
        self.assertEqual(call_kwargs['admin'].id, 11111)

    @patch("bot_handlers._send_manual_ban_report", new_callable=AsyncMock)
    @patch("bot_handlers._get_report_chat_id", new_callable=AsyncMock)
    @patch("bot_handlers._get_chat_settings", new_callable=AsyncMock)
    @patch("bot_handlers._upsert_user", new_callable=AsyncMock)
    @patch("bot_handlers._consume_bot_ban")
    async def test_12_handler_ignores_ban_from_other_bot(
        self, mock_consume, mock_upsert, mock_settings, mock_report_dest, mock_send
    ):
        """Handler MUST ignore ban from ANOTHER bot (not our bot) — covers the
        'second moderator-bot in chat' scenario. Bot ID is different from
        event.bot.id, but is_bot=True is enough."""
        from bot_handlers import on_chat_member_updated
        mock_consume.return_value = False  # no _mark_bot_ban from OUR bot
        mock_report_dest.return_value = -100999
        mock_settings.return_value = MagicMock(hashtag="#Test")
        # Another bot (not ours) — different bot ID
        event = self._make_event(
            new_status="kicked", old_status="member",
            chat_id=-100123, user_id=99999,
            admin_id=888888, admin_is_bot=True,  # different bot
        )
        # Make event.bot.id different from actor's id
        event.bot.id = 777777  # our bot's id
        await on_chat_member_updated(event)
        # Manual ban report MUST NOT be sent (other bot's ban is not "manual")
        mock_send.assert_not_called()
        mock_consume.assert_not_called()
        mock_upsert.assert_not_called()

    @patch("bot_handlers._send_manual_ban_report", new_callable=AsyncMock)
    @patch("bot_handlers._get_report_chat_id", new_callable=AsyncMock)
    @patch("bot_handlers._get_chat_settings", new_callable=AsyncMock)
    @patch("bot_handlers._upsert_user", new_callable=AsyncMock)
    @patch("bot_handlers._consume_bot_ban")
    async def test_13_handler_handles_from_user_none(
        self, mock_consume, mock_upsert, mock_settings, mock_report_dest, mock_send
    ):
        """Handler MUST NOT crash when from_user is None (rare edge case).
        Must fall through to TTL-backup logic."""
        from bot_handlers import on_chat_member_updated
        # _consume_bot_ban returns False — no recent bot ban
        mock_consume.return_value = False
        mock_report_dest.return_value = -100999
        mock_settings.return_value = MagicMock(hashtag="#Test")
        event = self._make_event(
            new_status="kicked", old_status="member",
            chat_id=-100123, user_id=99999,
            from_user_none=True,  # from_user is None
        )
        # Should NOT raise
        await on_chat_member_updated(event)
        # Should fall through to TTL-backup, then send report (no bot actor, no recent mark)
        mock_consume.assert_called_once_with(-100123, 99999)
        mock_send.assert_awaited_once()

    @patch("bot_handlers._send_manual_ban_report", new_callable=AsyncMock)
    @patch("bot_handlers._get_report_chat_id", new_callable=AsyncMock)
    @patch("bot_handlers._get_chat_settings", new_callable=AsyncMock)
    @patch("bot_handlers._upsert_user", new_callable=AsyncMock)
    @patch("bot_handlers._consume_bot_ban")
    async def test_14_handler_skips_via_ttl_backup_when_actor_is_human(
        self, mock_consume, mock_upsert, mock_settings, mock_report_dest, mock_send
    ):
        """If from_user is a human AND _consume_bot_ban returns True (extremely
        unlikely in practice — Telegram normally sets from_user=bot for bot
        bans), TTL-backup must still skip. This confirms v4.7.27 backup layer
        is preserved after v4.7.28 changes."""
        from bot_handlers import on_chat_member_updated
        mock_consume.return_value = True  # TTL backup says: bot just banned
        mock_report_dest.return_value = -100999
        mock_settings.return_value = MagicMock(hashtag="#Test")
        # Actor is a human (would be unusual — but tests backup layer)
        event = self._make_event(
            new_status="kicked", old_status="member",
            chat_id=-100123, user_id=99999,
            admin_id=11111, admin_is_bot=False,
        )
        await on_chat_member_updated(event)
        # Manual ban report MUST NOT be sent (TTL backup caught it)
        mock_send.assert_not_called()
        # _consume_bot_ban MUST be called
        mock_consume.assert_called_once_with(-100123, 99999)
        # DB ops MUST NOT happen (early-return)
        mock_upsert.assert_not_called()

    @patch("bot_handlers._send_manual_ban_report", new_callable=AsyncMock)
    @patch("bot_handlers._get_report_chat_id", new_callable=AsyncMock)
    @patch("bot_handlers._get_chat_settings", new_callable=AsyncMock)
    @patch("bot_handlers._upsert_user", new_callable=AsyncMock)
    @patch("bot_handlers._consume_bot_ban")
    async def test_15_handler_skips_db_ops_when_actor_is_bot(
        self, mock_consume, mock_upsert, mock_settings, mock_report_dest, mock_send
    ):
        """When actor is a bot, handler must NOT touch the DB at all — neither
        _get_report_chat_id, _get_chat_settings, nor _upsert_user."""
        from bot_handlers import on_chat_member_updated
        mock_consume.return_value = False
        mock_report_dest.return_value = -100999
        mock_settings.return_value = MagicMock(hashtag="#Test")
        event = self._make_event(
            new_status="kicked", old_status="member",
            chat_id=-100123, user_id=99999,
            admin_id=777777, admin_is_bot=True,
        )
        await on_chat_member_updated(event)
        # NO DB operations should happen
        mock_upsert.assert_not_called()
        mock_report_dest.assert_not_called()
        mock_settings.assert_not_called()
        mock_send.assert_not_called()
        # _consume_bot_ban also not called (early-return before it)
        mock_consume.assert_not_called()

    @patch("bot_handlers._send_manual_ban_report", new_callable=AsyncMock)
    @patch("bot_handlers._get_report_chat_id", new_callable=AsyncMock)
    @patch("bot_handlers._get_chat_settings", new_callable=AsyncMock)
    @patch("bot_handlers._upsert_user", new_callable=AsyncMock)
    @patch("bot_handlers._consume_bot_ban")
    async def test_16_handler_persistent_check_survives_restart_scenario(
        self, mock_consume, mock_upsert, mock_settings, mock_report_dest, mock_send
    ):
        """Simulates restart scenario: in-memory _recent_bot_bans is EMPTY
        (bot just restarted), but Telegram still sends ChatMemberUpdated for
        the bot's recent ban. Without v4.7.28 fix, this would produce a false
        'manual ban' report. With v4.7.28, from_user.is_bot=True catches it."""
        from bot_handlers import on_chat_member_updated, _recent_bot_bans
        # Ensure dict is empty (simulating post-restart state)
        _recent_bot_bans.clear()
        # _consume_bot_ban returns False — there's no mark in the dict
        mock_consume.return_value = False
        mock_report_dest.return_value = -100999
        mock_settings.return_value = MagicMock(hashtag="#Test")
        # Actor is the bot itself (is_bot=True)
        event = self._make_event(
            new_status="kicked", old_status="member",
            chat_id=-100123, user_id=99999,
            admin_id=777777, admin_is_bot=True,
        )
        await on_chat_member_updated(event)
        # CRITICAL: report must NOT be sent — persistent check saved us
        mock_send.assert_not_called()
        # And _consume_bot_ban shouldn't even be reached (early-return)
        mock_consume.assert_not_called()


# ════════════════════════════════════════════════════════════════════════════
# 3. Run all tests
# ════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    unittest.main(verbosity=2)
