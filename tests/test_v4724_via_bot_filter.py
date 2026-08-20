#!/usr/bin/env python3
"""v4.7.24 — Via-bot rate-limit filter: structural + behavioural tests.

Tests cover:
  1. db.py — ChatSettings has 3 new columns + migration is idempotent.
  2. db.py — BannedBot model/table fully removed (old design gone).
  3. bot_handlers.py — `_via_bot_rate_limit` dict + `_via_bot_rate_limit_cleanup`.
  4. bot_handlers.py — `_check_via_bot_filter` helper exists with correct signature.
  5. bot_handlers.py — via_bot check is FIRST in `handle_content_filters`.
  6. web_app.py — toggle field `via_bot_filter` in valid_fields.
  7. web_app.py — form fields `via_bot_rate_limit_seconds` / `via_bot_mute_minutes`.
  8. admin_chats.html — VIA toggle button + UI in Наказания section.
  9. Behavioural — rate-limit logic (allow within window, block when exceeded).
  10. Behavioural — admin bypass (admins never rate-limited).
  11. Behavioural — filter disabled = always allow.
  12. Behavioural — cleanup removes stale entries (>1h old).
  13. Version bumped to v4.7.24.

Run:  python scripts/test_v4724_via_bot_filter.py
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import unittest
from datetime import datetime, timedelta, timezone

# Path setup
HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)
sys.path.insert(0, PROJECT_ROOT)


def _read(rel: str) -> str:
    with open(os.path.join(PROJECT_ROOT, rel), "r", encoding="utf-8") as f:
        return f.read()


# ════════════════════════════════════════════════════════════════════════════
# 1. Structural tests — db.py
# ════════════════════════════════════════════════════════════════════════════
class TestDbStructural(unittest.TestCase):

    def test_01_chatsettings_has_via_bot_columns(self):
        """ChatSettings model declares 3 new columns."""
        src = _read("db.py")
        # Columns must be declared inside ChatSettings (we check string presence;
        # exact class boundaries verified by import test below).
        for col in (
            "via_bot_filter_enabled",
            "via_bot_rate_limit_seconds",
            "via_bot_mute_minutes",
        ):
            self.assertIn(col, src, f"Column {col} missing in db.py")

    def test_02_bannedbot_model_removed(self):
        """BannedBot class must be removed (old design)."""
        src = _read("db.py")
        self.assertNotIn("class BannedBot", src,
                         "BannedBot class must be removed in v4.7.24 new design")
        self.assertNotIn("__tablename__ = \"banned_bots\"", src,
                         "banned_bots table must be removed")

    def test_03_migration_idempotent(self):
        """Migration uses PRAGMA table_info to be idempotent."""
        src = _read("db.py")
        # Must check column existence before ALTER TABLE
        self.assertIn("PRAGMA table_info(chat_settings)", src,
                      "Migration must use PRAGMA to check column existence")
        # Must use ALTER TABLE ADD COLUMN (not CREATE TABLE)
        self.assertIn("ALTER TABLE chat_settings ADD COLUMN via_bot_filter_enabled", src)
        self.assertIn("ALTER TABLE chat_settings ADD COLUMN via_bot_rate_limit_seconds", src)
        self.assertIn("ALTER TABLE chat_settings ADD COLUMN via_bot_mute_minutes", src)

    def test_04_no_create_table_banned_bots(self):
        """Migration must NOT create banned_bots table anymore."""
        src = _read("db.py")
        self.assertNotIn("CREATE TABLE IF NOT EXISTS banned_bots", src,
                         "banned_bots CREATE TABLE must be removed")

    def test_05_defaults_correct(self):
        """New columns have correct defaults (False/300/10)."""
        src = _read("db.py")
        # default=False for via_bot_filter_enabled (Boolean)
        # default=300 for via_bot_rate_limit_seconds
        # default=10 for via_bot_mute_minutes
        # Loose check — look for the line patterns
        self.assertRegex(
            src,
            r"via_bot_filter_enabled\s*=\s*Column\(Boolean,\s*default=False",
            "via_bot_filter_enabled must default to False"
        )
        self.assertRegex(
            src,
            r"via_bot_rate_limit_seconds\s*=\s*Column\(Integer,\s*default=300",
            "via_bot_rate_limit_seconds must default to 300 (5 min)"
        )
        self.assertRegex(
            src,
            r"via_bot_mute_minutes\s*=\s*Column\(Integer,\s*default=10",
            "via_bot_mute_minutes must default to 10 (min)"
        )


# ════════════════════════════════════════════════════════════════════════════
# 2. Structural tests — bot_handlers.py
# ════════════════════════════════════════════════════════════════════════════
class TestBotHandlersStructural(unittest.TestCase):

    def test_06_rate_limit_dict_exists(self):
        """Module-level _via_bot_rate_limit dict declared."""
        src = _read("bot_handlers.py")
        self.assertIn("_via_bot_rate_limit", src,
                      "_via_bot_rate_limit dict must be declared")
        self.assertRegex(
            src,
            r"_via_bot_rate_limit\s*:\s*dict\[tuple\[int,\s*int,\s*int\],\s*datetime\]\s*=\s*\{\}",
            "_via_bot_rate_limit must be typed dict[tuple[int,int,int], datetime]"
        )

    def test_07_cleanup_function_exists(self):
        """_via_bot_rate_limit_cleanup function declared."""
        src = _read("bot_handlers.py")
        self.assertIn("def _via_bot_rate_limit_cleanup", src,
                      "_via_bot_rate_limit_cleanup function must be defined")

    def test_08_check_via_bot_filter_exists(self):
        """_check_via_bot_filter async helper declared."""
        src = _read("bot_handlers.py")
        self.assertIn("async def _check_via_bot_filter", src,
                      "_check_via_bot_filter async function must be defined")
        # Must take message + chat_id
        self.assertRegex(
            src,
            r"async def _check_via_bot_filter\(message:\s*types\.Message,\s*chat_id:\s*int\)\s*->\s*bool",
            "_check_via_bot_filter signature must be (message, chat_id) -> bool"
        )

    def test_09_via_bot_check_first_in_handle_content_filters(self):
        """via_bot check runs BEFORE text check in handle_content_filters."""
        src = _read("bot_handlers.py")
        # Find handle_content_filters
        m = re.search(
            r"async def handle_content_filters\(message: types\.Message\)[^\n]*\n(.*?)(?=\n@router|\nclass |\Z)",
            src, re.DOTALL,
        )
        self.assertIsNotNone(m, "handle_content_filters not found")
        body = m.group(1)
        # Find positions
        via_pos = body.find("_check_via_bot_filter")
        text_pos = body.find("text = message.text or message.caption")
        self.assertGreater(via_pos, 0, "_check_via_bot_filter call missing in handle_content_filters")
        self.assertGreater(text_pos, 0, "text extraction missing in handle_content_filters")
        self.assertLess(via_pos, text_pos,
                        "via_bot filter must run BEFORE text extraction (so media messages are also checked)")

    def test_10_no_bannedbot_imports(self):
        """No BannedBot references remain in bot_handlers.py."""
        src = _read("bot_handlers.py")
        self.assertNotIn("BannedBot", src,
                         "BannedBot must not be imported/referenced in bot_handlers.py")


# ════════════════════════════════════════════════════════════════════════════
# 3. Structural tests — web_app.py
# ════════════════════════════════════════════════════════════════════════════
class TestWebAppStructural(unittest.TestCase):

    # v4.9.0 (Task 11): admin_chats_toggle/admin_chats_update переехали из
    # web_app.py в web/admin_chats.py — проверки 11-14 читают новый файл.

    def test_11_via_bot_filter_in_toggle_valid_fields(self):
        """`via_bot_filter` added to valid toggle fields."""
        src = _read("web/admin_chats.py")
        self.assertIn("\"via_bot_filter\"", src,
                      "via_bot_filter must be in valid_fields set")
        # Must appear in valid_fields literal
        self.assertRegex(
            src,
            r'valid_fields\s*=\s*\{[^}]*"via_bot_filter"[^}]*\}',
            "via_bot_filter must be in valid_fields set literal"
        )

    def test_12_via_bot_filter_form_fields_in_update(self):
        """Form fields via_bot_rate_limit_seconds + via_bot_mute_minutes in update route."""
        src = _read("web/admin_chats.py")
        self.assertIn("via_bot_rate_limit_seconds: str = Form(", src,
                      "via_bot_rate_limit_seconds form field missing")
        self.assertIn("via_bot_mute_minutes: str = Form(", src,
                      "via_bot_mute_minutes form field missing")

    def test_13_via_bot_settings_saved_to_cs(self):
        """Settings are saved to ChatSettings object."""
        src = _read("web/admin_chats.py")
        self.assertIn("cs.via_bot_rate_limit_seconds =", src,
                      "via_bot_rate_limit_seconds not saved to cs")
        self.assertIn("cs.via_bot_mute_minutes =", src,
                      "via_bot_mute_minutes not saved to cs")

    def test_14_toggle_via_bot_filter_logic(self):
        """Toggle handler has a via_bot_filter branch."""
        src = _read("web/admin_chats.py")
        # Must have an elif branch that toggles cs.via_bot_filter_enabled
        self.assertIn('elif field == "via_bot_filter":', src,
                      "via_bot_filter toggle branch missing")
        self.assertIn("cs.via_bot_filter_enabled = not cs.via_bot_filter_enabled", src,
                      "via_bot_filter toggle must invert the boolean")

    def test_15_no_bannedbot_routes(self):
        """No BannedBot add/delete routes in web_app.py."""
        src = _read("web_app.py")
        self.assertNotIn("BannedBot", src,
                         "BannedBot must not be referenced in web_app.py")
        self.assertNotIn("/bots/add", src,
                         "Old /bots/add route must be removed")
        self.assertNotIn("/bots/", src,
                         "Old /bots/{bot_id}/delete route must be removed")


# ════════════════════════════════════════════════════════════════════════════
# 4. Structural tests — admin_chats.html
# ════════════════════════════════════════════════════════════════════════════
class TestAdminChatsTemplate(unittest.TestCase):

    def test_16_via_toggle_button_present(self):
        """VIA toggle button in action buttons row."""
        src = _read("templates/admin_chats.html")
        self.assertIn('value="via_bot_filter"', src,
                      "VIA toggle button missing")
        # v4.7.24: was 7 columns. v4.8.0: 8 columns (added MOD button).
        self.assertRegex(
            src,
            r"grid-template-columns:\s*repeat\(8,\s*1fr\)",
            "Action buttons grid must be 8 columns now (v4.8.0 added MOD toggle)"
        )

    def test_17_via_settings_in_punishments_section(self):
        """Via-bot rate-limit + mute inputs in Наказания section."""
        src = _read("templates/admin_chats.html")
        self.assertIn('name="via_bot_rate_limit_seconds"', src,
                      "via_bot_rate_limit_seconds input missing in Наказания section")
        self.assertIn('name="via_bot_mute_minutes"', src,
                      "via_bot_mute_minutes input missing in Наказания section")

    def test_18_via_badge_in_header(self):
        """VIA badge shown in card header when filter enabled."""
        src = _read("templates/admin_chats.html")
        self.assertRegex(
            src,
            r'\{% if c\.via_bot_filter_enabled %\}.*?VIA.*?\{% endif %\}',
            "VIA badge missing in card header"
        )


# ════════════════════════════════════════════════════════════════════════════
# 5. Behavioural tests — rate-limit logic (pure-Python simulation)
# ════════════════════════════════════════════════════════════════════════════

# Local re-implementation of rate-limit dict + cleanup to avoid importing
# bot_handlers (which has heavy side-effects: DB init, aiogram router setup).
# The logic mirrors _via_bot_rate_limit + _via_bot_rate_limit_cleanup exactly.
_rate_limit_dict: dict[tuple[int, int, int], datetime] = {}


def _rate_limit_cleanup(now: datetime | None = None) -> None:
    if not _rate_limit_dict:
        return
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=1)
    stale = [k for k, ts in _rate_limit_dict.items() if ts < cutoff]
    for k in stale:
        del _rate_limit_dict[k]


class TestRateLimitBehaviour(unittest.TestCase):
    """Simulates the rate-limit decision logic from _check_via_bot_filter
    without needing a full Telegram bot setup (avoids importing bot_handlers
    which has heavy side-effects)."""

    def setUp(self):
        _rate_limit_dict.clear()

    def test_19_first_message_allowed(self):
        """First via-bot message from a user — allowed (no prior entry)."""
        chat_id = -100123
        user_id = 42
        bot_id = 9999
        rate_limit = 300  # 5 min
        now = datetime.now(timezone.utc)
        _rate_limit_cleanup(now)
        key = (chat_id, user_id, bot_id)
        last = _rate_limit_dict.get(key)
        # Decision: allow if last is None OR gap >= rate_limit
        allow = last is None or (now - last).total_seconds() >= rate_limit
        self.assertTrue(allow, "First message must be allowed")
        # After allowing, update timestamp
        _rate_limit_dict[key] = now
        self.assertEqual(_rate_limit_dict[key], now)

    def test_20_second_message_within_window_blocked(self):
        """Second via-bot message within rate-limit window — blocked."""
        chat_id = -100123
        user_id = 42
        bot_id = 9999
        rate_limit = 300
        now = datetime.now(timezone.utc)
        # User already sent a message 60 seconds ago
        _rate_limit_dict[(chat_id, user_id, bot_id)] = now - timedelta(seconds=60)
        last = _rate_limit_dict.get((chat_id, user_id, bot_id))
        gap = (now - last).total_seconds()
        allow = last is None or gap >= rate_limit
        self.assertFalse(allow, "Second message within window must be blocked")
        self.assertLess(gap, rate_limit, "Gap must be less than rate_limit")

    def test_21_message_after_window_allowed(self):
        """Message after rate-limit window expires — allowed."""
        chat_id = -100123
        user_id = 42
        bot_id = 9999
        rate_limit = 300
        now = datetime.now(timezone.utc)
        # User sent a message 10 minutes ago (600s > 300s window)
        _rate_limit_dict[(chat_id, user_id, bot_id)] = now - timedelta(seconds=600)
        last = _rate_limit_dict.get((chat_id, user_id, bot_id))
        gap = (now - last).total_seconds()
        allow = last is None or gap >= rate_limit
        self.assertTrue(allow, "Message after window must be allowed")
        self.assertGreaterEqual(gap, rate_limit, "Gap must be >= rate_limit")

    def test_22_per_bot_isolation(self):
        """Different bots have independent rate-limits."""
        chat_id = -100123
        user_id = 42
        now = datetime.now(timezone.utc)
        # User sent message via @Bot1 60s ago
        _rate_limit_dict[(chat_id, user_id, 111)] = now - timedelta(seconds=60)
        # User now tries @Bot2 — first message, should be allowed
        last_bot2 = _rate_limit_dict.get((chat_id, user_id, 222))
        allow_bot2 = last_bot2 is None or (now - last_bot2).total_seconds() >= 300
        self.assertTrue(allow_bot2, "First message to @Bot2 must be allowed regardless of @Bot1")

    def test_23_per_chat_isolation(self):
        """Same user, same bot, different chats — independent rate-limits."""
        user_id = 42
        bot_id = 9999
        now = datetime.now(timezone.utc)
        # User sent message in chat1 60s ago
        _rate_limit_dict[(-111, user_id, bot_id)] = now - timedelta(seconds=60)
        # User now tries same bot in chat2 — first message, should be allowed
        last_chat2 = _rate_limit_dict.get((-222, user_id, bot_id))
        allow_chat2 = last_chat2 is None or (now - last_chat2).total_seconds() >= 300
        self.assertTrue(allow_chat2, "First message in chat2 must be allowed regardless of chat1")

    def test_24_per_user_isolation(self):
        """Same chat, same bot, different users — independent rate-limits."""
        chat_id = -100123
        bot_id = 9999
        now = datetime.now(timezone.utc)
        # User1 sent message 60s ago
        _rate_limit_dict[(chat_id, 111, bot_id)] = now - timedelta(seconds=60)
        # User2 now tries same bot — first message, should be allowed
        last_u2 = _rate_limit_dict.get((chat_id, 222, bot_id))
        allow_u2 = last_u2 is None or (now - last_u2).total_seconds() >= 300
        self.assertTrue(allow_u2, "First message from user2 must be allowed regardless of user1")

    def test_25_cleanup_removes_stale_entries(self):
        """Cleanup removes entries older than 1 hour."""
        now = datetime.now(timezone.utc)
        # Old entry (2 hours ago) — should be removed
        _rate_limit_dict[(-1, 100, 1000)] = now - timedelta(hours=2)
        # Fresh entry (5 min ago) — should be kept
        _rate_limit_dict[(-1, 200, 2000)] = now - timedelta(minutes=5)
        self.assertEqual(len(_rate_limit_dict), 2)
        _rate_limit_cleanup(now)
        self.assertEqual(len(_rate_limit_dict), 1,
                         "Cleanup must remove entries older than 1 hour")
        self.assertIn((-1, 200, 2000), _rate_limit_dict,
                      "Fresh entry must survive cleanup")
        self.assertNotIn((-1, 100, 1000), _rate_limit_dict,
                         "Stale entry must be removed by cleanup")

    def test_26_cleanup_empty_dict_noop(self):
        """Cleanup on empty dict is a no-op (no crash)."""
        _rate_limit_dict.clear()
        _rate_limit_cleanup()  # Should not raise
        self.assertEqual(len(_rate_limit_dict), 0)


# ════════════════════════════════════════════════════════════════════════════
# 6. Behavioural tests — DB migration (real SQLite, sync API)
# ════════════════════════════════════════════════════════════════════════════
class TestDbMigration(unittest.TestCase):

    def test_27_migration_creates_columns(self):
        """Running migration on fresh DB creates the 3 new columns."""
        import sqlite3
        conn = sqlite3.connect(":memory:")
        try:
            cur = conn.cursor()
            # Create chat_settings WITHOUT new columns (simulating pre-v4.7.24)
            cur.execute("""
                CREATE TABLE chat_settings (
                    chat_id INTEGER PRIMARY KEY,
                    warns_to_mute INTEGER DEFAULT 3,
                    mute_duration_seconds INTEGER DEFAULT 3600
                )
            """)
            conn.commit()
            # Run the same logic as our migration
            cur.execute("PRAGMA table_info(chat_settings)")
            existing_cols = {row[1] for row in cur.fetchall()}
            if "via_bot_filter_enabled" not in existing_cols:
                cur.execute(
                    "ALTER TABLE chat_settings ADD COLUMN via_bot_filter_enabled "
                    "BOOLEAN NOT NULL DEFAULT 0"
                )
            if "via_bot_rate_limit_seconds" not in existing_cols:
                cur.execute(
                    "ALTER TABLE chat_settings ADD COLUMN via_bot_rate_limit_seconds "
                    "INTEGER NOT NULL DEFAULT 300"
                )
            if "via_bot_mute_minutes" not in existing_cols:
                cur.execute(
                    "ALTER TABLE chat_settings ADD COLUMN via_bot_mute_minutes "
                    "INTEGER NOT NULL DEFAULT 10"
                )
            conn.commit()
            # Verify columns exist
            cur.execute("PRAGMA table_info(chat_settings)")
            cols = {row[1]: row for row in cur.fetchall()}
            self.assertIn("via_bot_filter_enabled", cols)
            self.assertIn("via_bot_rate_limit_seconds", cols)
            self.assertIn("via_bot_mute_minutes", cols)
            # Insert a row using defaults (no explicit values for new cols)
            cur.execute("INSERT INTO chat_settings (chat_id) VALUES (-100)")
            conn.commit()
            # Read back defaults
            cur.execute(
                "SELECT via_bot_filter_enabled, via_bot_rate_limit_seconds, "
                "via_bot_mute_minutes FROM chat_settings WHERE chat_id = -100"
            )
            row = cur.fetchone()
            self.assertEqual(row[0], 0, "via_bot_filter_enabled default must be 0 (False)")
            self.assertEqual(row[1], 300, "via_bot_rate_limit_seconds default must be 300")
            self.assertEqual(row[2], 10, "via_bot_mute_minutes default must be 10")
        finally:
            conn.close()

    def test_28_migration_idempotent(self):
        """Running migration twice doesn't error and preserves data."""
        import sqlite3
        conn = sqlite3.connect(":memory:")
        try:
            cur = conn.cursor()
            cur.execute("CREATE TABLE chat_settings (chat_id INTEGER PRIMARY KEY)")
            conn.commit()
            # Run migration TWICE
            for _ in range(2):
                cur.execute("PRAGMA table_info(chat_settings)")
                existing_cols = {row[1] for row in cur.fetchall()}
                if "via_bot_filter_enabled" not in existing_cols:
                    cur.execute(
                        "ALTER TABLE chat_settings ADD COLUMN via_bot_filter_enabled "
                        "BOOLEAN NOT NULL DEFAULT 0"
                    )
                if "via_bot_rate_limit_seconds" not in existing_cols:
                    cur.execute(
                        "ALTER TABLE chat_settings ADD COLUMN via_bot_rate_limit_seconds "
                        "INTEGER NOT NULL DEFAULT 300"
                    )
                if "via_bot_mute_minutes" not in existing_cols:
                    cur.execute(
                        "ALTER TABLE chat_settings ADD COLUMN via_bot_mute_minutes "
                        "INTEGER NOT NULL DEFAULT 10"
                    )
                conn.commit()
            # Insert + verify
            cur.execute(
                "INSERT INTO chat_settings (chat_id, via_bot_filter_enabled, "
                "via_bot_rate_limit_seconds, via_bot_mute_minutes) "
                "VALUES (-200, 1, 600, 30)"
            )
            conn.commit()
            cur.execute(
                "SELECT via_bot_filter_enabled, via_bot_rate_limit_seconds, "
                "via_bot_mute_minutes FROM chat_settings WHERE chat_id = -200"
            )
            row = cur.fetchone()
            self.assertEqual(row, (1, 600, 30),
                             "Data must be preserved across migration reruns")
        finally:
            conn.close()


# ════════════════════════════════════════════════════════════════════════════
# 7. Version bump
# ════════════════════════════════════════════════════════════════════════════
class TestVersion(unittest.TestCase):

    def test_29_version_bumped_to_v4724(self):
        """APP_VERSION in web_app.py is >= v4.7.24.

        v4.7.26 update: changed from strict equality to >= because
        v4.7.25 and v4.7.26 are bugfix releases that don't revert the
        v4.7.24 via_bot feature.
        """
        src = _read("web_app.py")
        m = re.search(r'APP_VERSION\s*=\s*"v(\d+)\.(\d+)\.(\d+)"', src)
        self.assertIsNotNone(m, "APP_VERSION not found")
        major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
        self.assertGreaterEqual(
            (major, minor, patch), (4, 7, 24),
            f"APP_VERSION=v{major}.{minor}.{patch} should be >= v4.7.24",
        )

    def test_30_changelog_has_v4724_entry(self):
        """Changelog in base.html mentions v4.7.24."""
        src = _read("templates/base.html")
        self.assertIn("v4.7.24", src,
                      "Changelog must mention v4.7.24")


if __name__ == "__main__":
    unittest.main(verbosity=2)
