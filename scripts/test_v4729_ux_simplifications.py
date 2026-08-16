#!/usr/bin/env python3
"""v4.7.29 — UX simplifications: word/link filter + Users page.

Tests cover:
  1. web_app.py — APP_VERSION == "v4.7.29".
  2. admin_presets.html — Word Filter section title in Russian (not "Ban Words").
  3. admin_presets.html — Link Allowlist section title in Russian.
  4. admin_presets.html — "Расширенные настройки" details block exists (regex hidden).
  5. admin_presets.html — Action options use emojis (🗑 Удалить, etc.).
  6. admin_presets.html — No more "is_regex=on — pattern компилируется как Python regex".
  7. admin_presets.html — No more "Паритет с командами" in the main description.
  8. admin_presets.html — Examples mentioned ("казино", "подпишись на канал").
  9. admin.html — "User management" section is GONE.
 10. admin.html — "v4.7.0: ручное создание упразднено" text is GONE.
 11. admin.html — New "Как добавить модератора" section exists.
 12. admin.html — Mentions both methods (Sync admins + /addadmin).
 13. base.html — changelog entry for v4.7.29 exists.

Run:  python scripts/test_v4729_ux_simplifications.py
"""
from __future__ import annotations

import ast
import os
import re
import sys
import unittest


HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)
sys.path.insert(0, PROJECT_ROOT)


def _read(rel: str) -> str:
    with open(os.path.join(PROJECT_ROOT, rel), "r", encoding="utf-8") as f:
        return f.read()


class TestV4729Structural(unittest.TestCase):
    """Structural tests for the v4.7.29 UX simplifications."""

    # ── web_app.py ──────────────────────────────────────────────────────────

    def test_01_app_version_is_v4729(self):
        """web_app.py APP_VERSION must be at least 'v4.7.29' (relaxed for v4.7.30+)."""
        src = _read("web_app.py")
        m = re.search(r'APP_VERSION\s*=\s*"v(\d+)\.(\d+)\.(\d+)"', src)
        self.assertIsNotNone(m, "APP_VERSION not found")
        major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
        self.assertGreaterEqual((major, minor, patch), (4, 7, 29),
                         f"APP_VERSION must be >= v4.7.29, got v{major}.{minor}.{patch}")

    # ── admin_presets.html — Word Filter simplifications ────────────────────

    def test_02_word_filter_section_title_in_russian(self):
        """Word Filter section title must be in Russian."""
        src = _read("templates/admin_presets.html")
        # Old: "Ban Words (Word Filter)" — should be replaced with Russian title
        self.assertNotIn("🚫 Ban Words (Word Filter)", src,
                         "Old English 'Ban Words (Word Filter)' title must be replaced")
        self.assertIn("Запрещённые слова", src,
                      "Word Filter section must have Russian title 'Запрещённые слова'")

    def test_03_link_allowlist_section_title_in_russian(self):
        """Link Allowlist section title must be in Russian."""
        src = _read("templates/admin_presets.html")
        self.assertNotIn("🔗 Link Allowlist", src,
                         "Old English 'Link Allowlist' title must be replaced")
        self.assertIn("Разрешённые домены", src,
                      "Link Allowlist section must have Russian title 'Разрешённые домены'")

    def test_04_regex_hidden_under_advanced_details(self):
        """is_regex checkbox must be hidden under a collapsible <details> block
        labeled 'Расширенные настройки' — so moderators don't accidentally
        enable regex."""
        src = _read("templates/admin_presets.html")
        # The collapsed details block must exist
        self.assertIn("Расширенные настройки", src,
                      "Regex settings must be hidden under 'Расширенные настройки' <details>")
        # The warning text about "только если знаете" should be present
        self.assertTrue(
            "знаете" in src or "знаешь" in src,
            "Regex block must warn users that they should know what regex is",
        )

    def test_05_action_options_have_emojis(self):
        """Action <option> tags must use emojis (🗑 Удалить, ⚠ Варн, 🔇 Мьют, 🚫 Бан)
        instead of plain English 'delete — удалить'."""
        src = _read("templates/admin_presets.html")
        self.assertIn("🗑 Удалить", src, "Action delete must have emoji and Russian label")
        self.assertIn("⚠ Варн", src, "Action warn must have emoji and Russian label")
        self.assertIn("🔇 Мьют", src, "Action mute must have emoji and Russian label")
        self.assertIn("🚫 Бан", src, "Action ban must have emoji and Russian label")
        # Old English options must be gone
        self.assertNotIn("delete — удалить", src,
                         "Old 'delete — удалить' option must be replaced")

    def test_06_no_technical_jargon_in_main_description(self):
        """Main word filter description must NOT mention 'Python regex via re.search'
        or 'pattern компилируется' — that's tech jargon that scares moderators."""
        src = _read("templates/admin_presets.html")
        # Find the word filter section
        idx = src.find("Запрещённые слова")
        self.assertGreater(idx, 0, "Word filter section not found")
        # Take the section up to the next major block (Link Allowlist)
        link_idx = src.find("Разрешённые домены", idx)
        section = src[idx:link_idx] if link_idx > idx else src[idx:idx+5000]
        # Tech jargon that should NOT be in the main description
        self.assertNotIn("re.search", section,
                         "Main description must NOT mention 're.search' (tech jargon)")
        self.assertNotIn("pattern компилируется", section,
                         "Main description must NOT mention 'pattern компилируется'")

    def test_07_no_parity_with_commands_in_top_description(self):
        """Top 'Что здесь настраивается' block should NOT have a verbose
        'Паритет с командами' line — moved to a small footnote."""
        src = _read("templates/admin_presets.html")
        # Find top description block
        idx = src.find("Что здесь настраивается")
        self.assertGreater(idx, 0, "Top 'Что здесь настраивается' block not found")
        # Take first 2000 chars after this point
        section = src[idx:idx + 2000]
        # The verbose "Паритет с командами" line should not be in the top block
        # (we replaced it with a small footnote)
        self.assertNotIn("Паритет с командами", section,
                         "Top block must NOT have verbose 'Паритет с командами' — replaced with footnote")

    def test_08_word_filter_examples_present(self):
        """Word filter description must include concrete examples like 'казино'
        and 'подпишись на канал' to help moderators understand."""
        src = _read("templates/admin_presets.html")
        # Find the SECTION (with emoji), not the bullet in the top block
        idx = src.find("🚫 Запрещённые слова")
        self.assertGreater(idx, 0, "Word Filter section (with 🚫 emoji) not found")
        section = src[idx:idx + 5000]
        self.assertIn("казино", section,
                      "Word filter description must include 'казино' as example")
        # Either "подпишись на канал" or similar example phrase
        self.assertTrue(
            "подпишись" in section.lower() or "подписка" in section.lower(),
            "Word filter description must include a phrase example",
        )

    def test_09_pattern_placeholder_simplified(self):
        """Pattern input placeholder must be a simple word like 'казино',
        NOT a scary regex example like '^https?://\\S+'."""
        src = _read("templates/admin_presets.html")
        # The old scary placeholder is gone
        self.assertNotIn("например: спам or regex: ^https?://", src,
                         "Old scary regex placeholder must be removed")
        # The new simple placeholder exists
        self.assertIn('placeholder="например: казино"', src,
                      "Pattern input must have simple placeholder 'например: казино'")

    def test_10_scope_options_in_russian(self):
        """Scope <select> options must use Russian labels '🌐 Во всех чатах'
        and '📍 ...' instead of English '🌐 Global (all chats)'."""
        src = _read("templates/admin_presets.html")
        self.assertIn("🌐 Во всех чатах", src,
                      "Scope option must be '🌐 Во всех чатах' (Russian)")
        self.assertNotIn("🌐 Global (all chats)", src,
                         "Old English '🌐 Global (all chats)' scope must be replaced")
        # The per-chat option must use 📍 emoji + chat title
        self.assertIn("📍 {{ c.title or c.chat_id }}", src,
                      "Per-chat scope option must use 📍 emoji")

    def test_11_link_allowlist_has_examples(self):
        """Link Allowlist description must include concrete examples (t.me, github.com)."""
        src = _read("templates/admin_presets.html")
        # Find the SECTION (not the bullet in the top block) — section has the emoji
        idx = src.find("🔗 Разрешённые домены")
        self.assertGreater(idx, 0, "Link Allowlist section not found")
        section = src[idx:idx + 3000]
        self.assertIn("t.me", section, "Link allowlist must mention t.me as example")
        self.assertIn("github.com", section, "Link allowlist must mention github.com as example")

    def test_12_link_allowlist_advises_adding_tg(self):
        """Link Allowlist must advise adding at least t.me to avoid blocking
        Telegram links themselves."""
        src = _read("templates/admin_presets.html")
        idx = src.find("🔗 Разрешённые домены")
        self.assertGreater(idx, 0, "Link Allowlist section not found")
        section = src[idx:idx + 3000]
        # Either "Совет" or similar advice about adding t.me
        self.assertTrue(
            ("обязательно" in section.lower() or "совет" in section.lower()) and "t.me" in section,
            "Link allowlist must advise adding t.me to avoid blocking Telegram links",
        )

    # ── admin.html — Users page cleanup ─────────────────────────────────────

    def test_13_user_management_section_removed(self):
        """The verbose 'User management' section in admin.html must be GONE.
        Note: the string may appear in a Jinja comment explaining what was
        removed — that's OK. We strip Jinja comments before checking."""
        src = _read("templates/admin.html")
        # Strip Jinja comments {# ... #} before checking
        visible = re.sub(r'\{#.*?#\}', '', src, flags=re.DOTALL)
        # The old section title "User management" should not exist in visible HTML
        self.assertNotIn("User management", visible,
                         "Old 'User management' section title must be removed from admin.html (visible HTML)")
        # The old verbose v4.7.0 explanation must be gone
        self.assertNotIn("ручное создание пользователей упразднено", visible,
                         "Old v4.7.0 'ручное создание упразднено' text must be removed")
        self.assertNotIn("is_active=False", visible,
                         "Old verbose is_active=False description must be removed")
        self.assertNotIn("can_promote_members", visible,
                         "Old verbose can_promote_members mention must be removed")

    def test_14_how_to_add_moderator_section_exists(self):
        """New 'Как добавить модератора' section must exist in admin.html."""
        src = _read("templates/admin.html")
        self.assertIn("Как добавить модератора", src,
                      "New 'Как добавить модератора' section must exist")
        # anchor #create must still point to it
        self.assertIn('id="create"', src,
                      "Section must have id='create' for anchor navigation")

    def test_15_how_to_add_lists_both_methods(self):
        """The new section must mention BOTH methods:
        1. Sync admins from TG (web-panel, recommended)
        2. /addadmin command (SU only)"""
        src = _read("templates/admin.html")
        # Method 1: Sync admins from TG
        self.assertIn("Sync admins from TG", src,
                      "Method 1 (Sync admins from TG) must be mentioned")
        # Method 2: /addadmin command
        self.assertIn("/addadmin", src,
                      "Method 2 (/addadmin command) must be mentioned")
        # Both methods must be marked with "Способ 1" / "Способ 2"
        self.assertIn("Способ 1", src, "Method 1 must be labeled 'Способ 1'")
        self.assertIn("Способ 2", src, "Method 2 must be labeled 'Способ 2'")

    def test_16_how_to_add_mentions_start_command(self):
        """The new section must mention that new admin should write /start
        to the bot in PM to receive login/password."""
        src = _read("templates/admin.html")
        self.assertIn("/start", src,
                      "Section must mention /start command for new admins to get credentials")

    def test_17_anchor_nav_has_how_to_add(self):
        """The in-page anchor nav must include 'How to add' link."""
        src = _read("templates/admin.html")
        # Find the anchor-nav block
        idx = src.find('class="anchor-nav"')
        self.assertGreater(idx, 0, "anchor-nav block not found")
        # Take the next 500 chars
        nav_block = src[idx:idx + 500]
        self.assertIn("#create", nav_block,
                      "Anchor nav must include link to #create (How to add)")
        self.assertIn("How to add", nav_block,
                      "Anchor nav must have visible text 'How to add'")

    # ── base.html — changelog ───────────────────────────────────────────────

    def test_18_changelog_contains_v4729(self):
        """templates/base.html changelog must mention v4.7.29."""
        src = _read("templates/base.html")
        self.assertIn("v4.7.29", src, "Changelog must contain v4.7.29 entry")
        # Key feature description — either упрощение or UX
        self.assertTrue(
            "упрощение" in src.lower() or "ux" in src.lower(),
            "Changelog must mention UX simplifications",
        )

    def test_19_changelog_mentions_word_filter_simplification(self):
        """Changelog must explain what was simplified (word filter)."""
        src = _read("templates/base.html")
        # Find v4.7.29 section
        idx = src.find("v4.7.29")
        section = src[idx:idx + 5000]
        self.assertIn("Word Filter", section,
                      "Changelog must mention Word Filter simplification")
        # Must mention that regex was hidden
        self.assertTrue(
            "Расширенные" in section or "regex" in section.lower(),
            "Changelog must mention that regex was moved under 'Расширенные настройки'",
        )

    def test_20_changelog_mentions_users_page_cleanup(self):
        """Changelog must explain the Users page cleanup (User management removed)."""
        src = _read("templates/base.html")
        idx = src.find("v4.7.29")
        section = src[idx:idx + 5000]
        self.assertIn("User management", section,
                      "Changelog must mention that 'User management' section was removed")
        self.assertIn("Как добавить модератора", section,
                      "Changelog must mention the new 'Как добавить модератора' section")


# ════════════════════════════════════════════════════════════════════════════
# Run all tests
# ════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    unittest.main(verbosity=2)
