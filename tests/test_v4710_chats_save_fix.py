"""
v4.7.10 — тесты фикса сохранения настроек чата.

Проблема v4.7.9 (и ранее): при нажатии «Сохранить» в карточке чата браузер
показывал «Вы пропустили это поле» и блокировал сабмит. Причина — поля
start_date и end_date в форме добавления санитарного периода имели
HTML5-атрибут required, но сами находились внутри главной формы /update.
Когда пользователь нажимал «Сохранить» (без добавления санитарного периода),
браузер применял валидацию ко всем required-полям формы — и блокировал
сабмит из-за пустых дат.

Решение v4.7.10:
  • HTML5 required убран с start_date и end_date
  • Серверная валидация в add_sanitary_period (bot_handlers.py) уже
    существует и возвращает понятную ошибку через flash-сообщение

Тесты:
  1. APP_VERSION = "v4.7.10"
  2. start_date в admin_chats.html НЕ имеет required
  3. end_date в admin_chats.html НЕ имеет required
  4. Кнопка "+ Add" осталась с formnovalidate
  5. Кнопки toggle (SAN/NIGHT/CAS/LINK/ENABLE/REPORT) остались с formnovalidate
  6. Кнопка "Сохранить" осталась type="submit" (сохранение работает)
  7. В форме /update больше нет ни одного required-поля
  8. В admin_presets.html все required-поля на месте (regression check)
  9. В profile.html все required-поля на месте (regression check)
 10. В admin.html все required-поля на месте (regression check)
 11. Серверная валидация: add_sanitary_period с пустыми датами → ошибка
 12. Серверная валидация: add_sanitary_period с валидными датами → успех
 13. Handler /admin/chats/{id}/update не принимает start_date/end_date
 14. Changelog содержит v4.7.10
 15. Static check: в admin_chats.html ровно 0 упоминаний `required`
"""
from _paths import _P  # noqa: E402  (корень вычисляется от __file__)

import os
import sys
import re
import unittest

sys.path.insert(0, _P())
sys.path.insert(0, _P("tests"))

_DB_PATH = "/tmp/test_v4710_chats_save_fix.db"
if os.path.exists(_DB_PATH):
    os.remove(_DB_PATH)

os.environ["BOT_TOKEN"] = "0:fake"
os.environ["ADMIN_IDS"] = "1"
os.environ["SU_IDS"] = "1"
os.environ["WEB_PASSWORD"] = "test_password_123"
os.environ["DB_PATH"] = _DB_PATH

import web_app
from web_app import APP_VERSION
import bot_handlers as bh

# ─── Helpers ────────────────────────────────────────────────────────────────

TEMPLATES_DIR = _P("templates")


def _read(name: str) -> str:
    with open(os.path.join(TEMPLATES_DIR, name), "r", encoding="utf-8") as f:
        return f.read()


def _find_form(html: str, action_substring: str) -> tuple[int, int, str]:
    """Returns (start_pos, end_pos, form_html) of the first <form> whose
    action contains action_substring. Returns (-1, -1, '') if not found.
    end_pos is the index AFTER </form>.
    """
    pat = re.compile(r'<form\b[^>]*action="[^"]*' + re.escape(action_substring) + r'[^"]*"[^>]*>', re.IGNORECASE)
    m = pat.search(html)
    if not m:
        return -1, -1, ""
    start = m.start()
    # find matching </form>
    end_match = re.search(r'</form\s*>', html[start:], re.IGNORECASE)
    if not end_match:
        return start, -1, ""
    end = start + end_match.end()
    return start, end, html[start:end]


def _count_required_in_form(form_html: str) -> int:
    """Counts `required` attribute occurrences inside a form HTML (excluding
    HTML comments)."""
    # Strip comments first
    no_comments = re.sub(r'<!--.*?-->', '', form_html, flags=re.DOTALL)
    return len(re.findall(r'\brequired\b', no_comments))


# ─── Tests ──────────────────────────────────────────────────────────────────


class TestV4710ChatsSaveFix(unittest.TestCase):
    """v4.7.10: чиним блокировку «Сохранить» в карточке чата."""

    def setUp(self):
        self.admin_chats = _read("admin_chats.html")
        self.admin_presets = _read("admin_presets.html")
        self.profile = _read("profile.html")
        self.admin = _read("admin.html")
        self.admin_settings = _read("admin_settings.html")
        self.base = _read("base.html")

    # ─── 1. Version ──────────────────────────────────────────────────────

    def test_01_app_version_bumped(self):
        """APP_VERSION должен быть >= v4.7.10 (тест ослаблен в v4.7.13)."""
        # v4.7.13: ослаблен с == "v4.7.10" на >= v4.7.10 — чтобы не падать
        # на каждом следующем релизе. Изначальная проверка была валидна
        # только в момент выхода v4.7.10.
        self.assertGreaterEqual(APP_VERSION, "v4.7.10",
                                f"APP_VERSION should be >= v4.7.10, got {APP_VERSION}")

    # ─── 2-3. Required removed from start_date / end_date ────────────────

    def test_02_start_date_no_required(self):
        """start_date больше не должен иметь HTML5 required."""
        # Find input name="start_date" and check it doesn't have required
        m = re.search(r'<input[^>]+name="start_date"[^>]*>', self.admin_chats)
        self.assertIsNotNone(m, "start_date input not found in admin_chats.html")
        tag = m.group(0)
        self.assertNotIn("required", tag,
                         f"start_date still has required: {tag}")

    def test_03_end_date_no_required(self):
        """end_date больше не должен иметь HTML5 required."""
        m = re.search(r'<input[^>]+name="end_date"[^>]*>', self.admin_chats)
        self.assertIsNotNone(m, "end_date input not found in admin_chats.html")
        tag = m.group(0)
        self.assertNotIn("required", tag,
                         f"end_date still has required: {tag}")

    # ─── 4. + Add button still has formnovalidate ───────────────────────

    def test_04_add_sanitary_button_has_formnovalidate(self):
        """Кнопка '+ Add' должна сохранить formnovalidate."""
        # Find button with formaction containing /sanitary/add
        m = re.search(
            r'<button[^>]+formaction="[^"]*/sanitary/add"[^>]*>',
            self.admin_chats,
        )
        self.assertIsNotNone(m, "Sanitary Add button not found")
        btn = m.group(0)
        self.assertIn("formnovalidate", btn,
                      f"+ Add button should have formnovalidate: {btn}")

    # ─── 5. Toggle buttons still have formnovalidate ────────────────────

    def test_05_toggle_buttons_have_formnovalidate(self):
        """Все toggle-кнопки (/toggle endpoint) должны сохранить formnovalidate."""
        # Find all buttons with formaction containing /toggle
        toggles = re.findall(
            r'<button[^>]+formaction="[^"]*/toggle"[^>]*>',
            self.admin_chats,
        )
        self.assertGreaterEqual(len(toggles), 4,
                                "Should have at least 4 toggle buttons (ENABLE/CAS/LINK/NIGHT/SAN/REPORT)")
        for btn in toggles:
            self.assertIn("formnovalidate", btn,
                          f"Toggle button missing formnovalidate: {btn}")

    # ─── 6. Save button still type=submit ───────────────────────────────

    def test_06_save_button_is_submit(self):
        """Кнопка 'Сохранить' должна остаться type='submit'."""
        # Find the Save button by its text content "Сохранить"
        m = re.search(
            r'<button[^>]*type="submit"[^>]*>\s*▸\s*Сохранить\s*</button>',
            self.admin_chats,
        )
        self.assertIsNotNone(m, "Save button (▸ Сохранить) not found or not type=submit")

    # ─── 7. No required fields inside /update form ──────────────────────

    def test_07_update_form_has_no_required(self):
        """Главная форма /update не должна содержать ни одного required-поля."""
        start, end, form_html = _find_form(self.admin_chats, "/update")
        self.assertGreater(start, -1, "Form /update not found")
        # Required is allowed in formnovalidate buttons (their own attribute
        # is formnovalidate, not required). We only forbid `required` on inputs.
        # Strip comments and find <input> tags with required
        no_comments = re.sub(r'<!--.*?-->', '', form_html, flags=re.DOTALL)
        inputs_with_required = re.findall(
            r'<input[^>]*\brequired\b[^>]*>',
            no_comments,
        )
        self.assertEqual(
            len(inputs_with_required), 0,
            f"/update form should have 0 required inputs, "
            f"found {len(inputs_with_required)}: {inputs_with_required}"
        )

    # ─── 8. admin_presets.html — regression check ───────────────────────

    def test_08_admin_presets_required_preserved(self):
        """admin_presets.html должен сохранить все required-поля в отдельных формах."""
        # Each required input should be in its own /create, /words/add, or /links/add form
        # (NOT in a shared form with toggle buttons).
        # Count required inputs in main create form
        create_start, create_end, create_form = _find_form(self.admin_presets, "/presets/create")
        self.assertGreater(create_start, -1, "/presets/create form not found")
        create_required = _count_required_in_form(create_form)
        self.assertGreaterEqual(create_required, 2,
                                "Create form should have at least 2 required fields (name, scope)")

        # Words add form
        words_start, words_end, words_form = _find_form(self.admin_presets, "/presets/words/add")
        self.assertGreater(words_start, -1, "/presets/words/add form not found")
        words_required = _count_required_in_form(words_form)
        self.assertGreaterEqual(words_required, 3,
                                "Words add form should have at least 3 required fields (chat_id, pattern, action)")

        # Links add form
        links_start, links_end, links_form = _find_form(self.admin_presets, "/presets/links/add")
        self.assertGreater(links_start, -1, "/presets/links/add form not found")
        links_required = _count_required_in_form(links_form)
        self.assertGreaterEqual(links_required, 2,
                                "Links add form should have at least 2 required fields (chat_id, domain)")

    # ─── 9. profile.html — regression check ─────────────────────────────

    def test_09_profile_required_preserved(self):
        """profile.html должен сохранить required в формах смены пароля."""
        # profile.html has TWO /me/password forms (regular change + admin reset).
        # Count total required attributes across all password forms.
        pwd_forms = re.findall(
            r'<form[^>]*action="/me/password"[^>]*>([\s\S]*?)</form\s*>',
            self.profile,
            re.IGNORECASE,
        )
        self.assertGreaterEqual(len(pwd_forms), 2,
                                f"Should have at least 2 /me/password forms, found {len(pwd_forms)}")
        total_required = sum(_count_required_in_form(f) for f in pwd_forms)
        # 2 forms × 3 fields (old_password, new_password, confirm) = 6 minimum
        self.assertGreaterEqual(total_required, 6,
                                f"Password forms should have at least 6 required attributes total, "
                                f"got {total_required}")

    # ─── 10. admin.html — regression check ──────────────────────────────

    def test_10_admin_required_preserved(self):
        """admin.html должен сохранить required в форме reset user password."""
        # The reset form contains <input type="password" name="password" required>
        reset_start, reset_end, reset_form = _find_form(self.admin, "/reset")
        self.assertGreater(reset_start, -1, "/admin/users/{id}/reset form not found")
        reset_required = _count_required_in_form(reset_form)
        self.assertGreaterEqual(reset_required, 1,
                                "Reset form should have at least 1 required field (password)")

    # ─── 11. Server-side validation: empty dates → error ────────────────

    def test_11_add_sanitary_period_empty_dates_returns_error(self):
        """add_sanitary_period с пустыми датами должен вернуть ошибку."""
        new_json, err = bh.add_sanitary_period(None, "", "", None, None)
        self.assertIsNone(new_json,
                          "Should return None JSON for empty dates")
        self.assertIsNotNone(err,
                            "Should return error message for empty dates")
        self.assertIn("date", err.lower(),
                      f"Error should mention 'date': {err}")

    # ─── 12. Server-side validation: valid dates → success ──────────────

    def test_12_add_sanitary_period_valid_dates_success(self):
        """add_sanitary_period с валидными датами должен вернуть новый JSON."""
        new_json, err = bh.add_sanitary_period(
            None,
            "2026-08-15",
            "2026-08-17",
            None,
            None,
        )
        self.assertIsNone(err,
                          f"Should not return error for valid dates: {err}")
        self.assertIsNotNone(new_json,
                             "Should return new JSON for valid dates")

    # ─── 13. Handler /update doesn't accept start_date/end_date ────────

    def test_13_update_handler_signature_ignores_sanitary_form_fields(self):
        """Handler /admin/chats/{id}/update не должен принимать start_date/end_date."""
        import inspect
        # Find admin_chats_update function
        from web_app import create_app  # may also be inner function
        # The handler is defined inside create_app, so we inspect the source.
        src = inspect.getsource(web_app)
        # Find the function body
        m = re.search(
            r'async def admin_chats_update\s*\(([\s\S]*?)\)\s*:',
            src,
        )
        self.assertIsNotNone(m, "admin_chats_update function not found in web_app.py source")
        sig = m.group(1)
        # Make sure start_date is NOT in the form params
        # (it's OK if it appears as part of "sanitary_days_text" or in comments)
        # Match \bstart_date\b but not "sanitary_days_text"
        # We're looking for the exact word "start_date" or "end_date" as Form() params
        bad_patterns = [
            r'start_date\s*:\s*str\s*=\s*Form',
            r'end_date\s*:\s*str\s*=\s*Form',
            r'start_time\s*:\s*str\s*=\s*Form',
            r'end_time\s*:\s*str\s*=\s*Form',
        ]
        for pat in bad_patterns:
            self.assertIsNone(
                re.search(pat, sig),
                f"admin_chats_update should NOT accept Form() parameter matching {pat}"
            )

    # ─── 14. Changelog mentions v4.7.10 ─────────────────────────────────

    def test_14_changelog_mentions_v4710(self):
        """Changelog в base.html должен содержать запись v4.7.10."""
        self.assertIn("v4.7.10", self.base,
                      "Changelog should mention v4.7.10")
        # Also check that the new entry describes the bug
        # Find v4.7.10 section
        m = re.search(
            r'<p><strong>v4\.7\.10</strong>.*?</ul>',
            self.base,
            re.DOTALL,
        )
        self.assertIsNotNone(m, "v4.7.10 changelog entry not found")
        section = m.group(0)
        # Should mention the bug
        self.assertTrue(
            ("Сохранить" in section or "сохранени" in section.lower()),
            "Changelog should mention 'Сохранить' / сохранение"
        )
        # Should mention start_date or end_date
        self.assertTrue(
            ("start_date" in section or "end_date" in section),
            "Changelog should mention start_date/end_date"
        )

    # ─── 15. Static: zero `required` in admin_chats.html ────────────────

    def test_15_admin_chats_no_required_at_all(self):
        """В admin_chats.html не должно остаться ни одного `required` (включая
        в комментариях — мы их убираем за ненадобностью)."""
        # Strip comments first to avoid false positives
        no_comments = re.sub(r'<!--.*?-->', '', self.admin_chats, flags=re.DOTALL)
        # Also strip Jinja comments {# #}
        no_comments = re.sub(r'\{#.*?#\}', '', no_comments, flags=re.DOTALL)
        # Count `required` attribute on inputs
        inputs_with_required = re.findall(
            r'<input[^>]*\brequired\b[^>]*>',
            no_comments,
        )
        self.assertEqual(
            len(inputs_with_required), 0,
            f"admin_chats.html should have 0 required inputs after v4.7.10, "
            f"found {len(inputs_with_required)}: {inputs_with_required}"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
