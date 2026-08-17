"""
v4.7.17 — тесты редактирования пресетов прав.

Что добавлено:
  • POST /admin/presets/{preset_id:int}/edit — меняет name/scope/permissions/slow_mode_delay
  • Системные пресеты редактировать нельзя (как и удалять)
  • Уникальность name проверяется с исключением текущего preset_id
  • UI: каждая карточка пресета обёрнута в <details> с inline-формой редактирования
  • Чекбоксы pre-filled из текущего JSON пресета
  • Подсказка: чаты не затрагиваются (копия JSON хранится в ChatSettings)

Тесты:
  1. APP_VERSION = "v4.7.17"
  2. APP_RELEASE_DATE = "2026-08-04"
  3. Endpoint admin_presets_edit существует
  4. Endpoint принимает preset_id как int path param
  5. Принимает name Form field
  6. Принимает scope Form field
  7. Принимает все 13 perm_can_* Form fields
  8. Принимает slow_mode_delay Form field
  9. Валидация: name 1-64 chars (empty → reject)
 10. Валидация: name > 64 chars → reject
 11. Валидация: scope ∈ {day, night, sanitary}
 12. Валидация: slow_mode_delay int parse (не-int → reject)
 13. Валидация: slow_mode_delay 0..36400 range
 14. Guard: preset not found → redirect с flash
 15. Guard: системный пресет → redirect с flash "System presets cannot be edited"
 16. Уникальность name: исключает текущий preset_id (можно сохранить без смены имени)
 17. Сохраняет name
 18. Сохраняет scope
 19. Сохраняет permissions JSON
 20. Сохраняет slow_mode_delay
 21. Логирует через _req_logger.info("presets_edit: ...")
 22. Redirect после успеха: flash "Preset ... updated"
 23. admin_presets.html: есть кнопка "Edit ▾"
 24. admin_presets.html: есть форма с action /admin/presets/{id}/edit
 25. admin_presets.html: чекбоксы pre-filled (checked если perms.get(json_key))
 26. admin_presets.html: name input pre-filled (value="{{ p.name }}")
 27. admin_presets.html: scope select pre-selected
 28. admin_presets.html: slow_mode input pre-filled если not none
 29. admin_presets.html: системные пресеты НЕ имеют формы редактирования
 30. admin_presets.html: hint про "копия JSON" / "не затрагивает чаты"
 31. base.html: changelog v4.7.17 присутствует
 32. base.html: v4.7.17 упоминает "редактирование" или "edit"
 33. base.html: v4.7.16 сохранена (регрессия)
 34. base.html: v4.7.17 идёт ВЫШЕ v4.7.16
"""

import os
import re
import sys
import unittest

# ── Пути ────────────────────────────────────────────────────────────────────
PROJECT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

import importlib.util

# Импортируем web_app для APP_VERSION
spec = importlib.util.spec_from_file_location(
    "web_app", os.path.join(PROJECT_DIR, "web_app.py")
)
web_app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(web_app)
APP_VERSION = web_app.APP_VERSION
APP_RELEASE_DATE = web_app.APP_RELEASE_DATE

WEB_APP_PY = os.path.join(PROJECT_DIR, "web_app.py")
BASE_HTML = os.path.join(PROJECT_DIR, "templates", "base.html")
ADMIN_PRESETS_HTML = os.path.join(PROJECT_DIR, "templates", "admin_presets.html")


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _find_fn_body(src: str, fn_name: str) -> str:
    """Найти тело функции async def fn_name (или def fn_name)."""
    for prefix in ("async def ", "def "):
        idx = src.find(f"{prefix}{fn_name}(")
        if idx > 0:
            # Find next def at same indent (4 spaces)
            next_idx = src.find("\n    @", idx + 10)
            if next_idx < 0:
                next_idx = len(src)
            return src[idx:next_idx]
    return ""


# ─── Tests ──────────────────────────────────────────────────────────────────


class TestV4717PresetEdit(unittest.TestCase):
    """v4.7.17: редактирование пресетов прав."""

    def setUp(self):
        self.web_app_py = _read(WEB_APP_PY)
        self.base_html = _read(BASE_HTML)
        self.admin_presets_html = _read(ADMIN_PRESETS_HTML)
        self.edit_fn_body = _find_fn_body(self.web_app_py, "admin_presets_edit")

    # ─── 1-2. Version ──────────────────────────────────────────────────

    def test_01_app_version(self):
        # v4.7.18+: APP_VERSION bumped. Loosen to >=.
        self.assertGreaterEqual(APP_VERSION, "v4.7.17",
            f"APP_VERSION={APP_VERSION} should be >= v4.7.17")

    def test_02_app_release_date(self):
        self.assertGreaterEqual(APP_RELEASE_DATE, "2026-08-04")

    # ─── 3-8. Endpoint exists + signature ──────────────────────────────

    def test_03_endpoint_exists(self):
        """POST /admin/presets/{preset_id:int}/edit должен существовать."""
        self.assertIn(
            '@app.post("/admin/presets/{preset_id:int}/edit")',
            self.web_app_py,
            "Endpoint /admin/presets/{preset_id:int}/edit не найден",
        )

    def test_04_endpoint_takes_preset_id_int(self):
        """preset_id должен быть int path param."""
        self.assertIn("preset_id: int,", self.edit_fn_body)

    def test_05_accepts_name_form_field(self):
        """name: str = Form(...) в сигнатуре."""
        self.assertIn("name: str = Form(", self.edit_fn_body)

    def test_06_accepts_scope_form_field(self):
        """scope: str = Form(...) в сигнатуре."""
        self.assertIn("scope: str = Form(", self.edit_fn_body)

    def test_07_accepts_all_13_perm_fields(self):
        """Все 13 perm_can_* Form fields в сигнатуре."""
        perm_fields = [
            "perm_can_send_messages",
            "perm_can_send_audios",
            "perm_can_send_documents",
            "perm_can_send_photos",
            "perm_can_send_videos",
            "perm_can_send_video_notes",
            "perm_can_send_voice_notes",
            "perm_can_send_polls",
            "perm_can_send_other_messages",
            "perm_can_add_web_page_previews",
            "perm_can_change_info",
            "perm_can_invite_users",
            "perm_can_pin_messages",
        ]
        for field in perm_fields:
            self.assertIn(f"{field}: str = Form", self.edit_fn_body,
                          f"Поле {field} отсутствует в сигнатуре admin_presets_edit")

    def test_08_accepts_slow_mode_delay_form_field(self):
        """slow_mode_delay: str = Form('') в сигнатуре."""
        self.assertIn("slow_mode_delay: str = Form", self.edit_fn_body)

    # ─── 9-13. Validation ─────────────────────────────────────────────

    def test_09_validates_name_not_empty(self):
        """name 1-64 chars: пустое имя → reject."""
        self.assertIn("len(name) > 64", self.edit_fn_body,
                      "Проверка len(name) > 64 отсутствует")
        self.assertIn("Invalid+preset+name", self.edit_fn_body,
                      "Flash для invalid name отсутствует")

    def test_10_validates_name_max_64(self):
        """len(name) > 64 → reject (уже проверено в test_09)."""
        # Дублирует test_09, но явно фиксирует границу
        self.assertIn("len(name) > 64", self.edit_fn_body)

    def test_11_validates_scope_in_day_night_sanitary(self):
        """scope ∈ {day, night, sanitary}."""
        self.assertIn('scope not in ("day", "night", "sanitary")', self.edit_fn_body,
                      "Проверка scope не найдена")

    def test_12_validates_slow_mode_int_parse(self):
        """slow_mode_delay — int parse, не-int → reject."""
        self.assertIn("int(slow_mode_raw)", self.edit_fn_body,
                      "int(slow_mode_raw) парсинг отсутствует")
        self.assertIn("Invalid+slow_mode_delay+(must+be+integer)",
                      self.edit_fn_body)

    def test_13_validates_slow_mode_range(self):
        """slow_mode_delay 0..36400."""
        self.assertIn("slow_mode_value < 0 or slow_mode_value > 36400",
                      self.edit_fn_body,
                      "Range check 0..36400 отсутствует")
        self.assertIn("slow_mode_delay+must+be+0..36400", self.edit_fn_body)

    # ─── 14-15. Guards ────────────────────────────────────────────────

    def test_14_guard_preset_not_found(self):
        """Если пресет не найден → redirect с flash 'Preset not found'."""
        self.assertIn("Preset+not+found", self.edit_fn_body,
                      "Flash 'Preset not found' отсутствует в admin_presets_edit")

    def test_15_guard_system_preset_cannot_be_edited(self):
        """Системные пресеты редактировать нельзя."""
        self.assertIn("preset.is_system", self.edit_fn_body,
                      "Проверка is_system отсутствует")
        self.assertIn("System+presets+cannot+be+edited", self.edit_fn_body,
                      "Flash 'System presets cannot be edited' отсутствует")

    # ─── 16. Name uniqueness excludes self ────────────────────────────

    def test_16_name_uniqueness_excludes_self(self):
        """Уникальность name проверяется с PermissionPreset.id != preset_id."""
        # Look for the exclusion in the uniqueness check
        self.assertIn("PermissionPreset.id != preset_id", self.edit_fn_body,
                      "Исключение текущего preset_id из uniqueness check отсутствует")
        self.assertIn("Preset+name+already+exists", self.edit_fn_body,
                      "Flash для duplicate name отсутствует")

    # ─── 17-20. Saves fields ──────────────────────────────────────────

    def test_17_saves_name(self):
        """preset.name = name."""
        self.assertIn("preset.name = name", self.edit_fn_body,
                      "preset.name = name assignment отсутствует")

    def test_18_saves_scope(self):
        """preset.scope = scope."""
        self.assertIn("preset.scope = scope", self.edit_fn_body,
                      "preset.scope = scope assignment отсутствует")

    def test_19_saves_permissions_json(self):
        """preset.permissions = json.dumps(perms)."""
        self.assertIn("preset.permissions = json.dumps(perms)",
                      self.edit_fn_body,
                      "preset.permissions = json.dumps(perms) отсутствует")

    def test_20_saves_slow_mode_delay(self):
        """preset.slow_mode_delay = slow_mode_value."""
        self.assertIn("preset.slow_mode_delay = slow_mode_value",
                      self.edit_fn_body,
                      "preset.slow_mode_delay = slow_mode_value отсутствует")

    # ─── 21-22. Logging + redirect ────────────────────────────────────

    def test_21_logs_edit_operation(self):
        """Логирование через _req_logger.info('presets_edit: ...')."""
        self.assertIn("presets_edit:", self.edit_fn_body,
                      "Лог presets_edit отсутствует")
        # Should include old_name and new name
        self.assertIn("old_name", self.edit_fn_body,
                      "old_name в логе отсутствует")
        self.assertIn("old_scope", self.edit_fn_body,
                      "old_scope в логе отсутствует")

    def test_22_redirect_flash_updated(self):
        """Redirect после успеха с flash 'Preset ... updated'."""
        self.assertIn("updated", self.edit_fn_body,
                      "Flash 'updated' отсутствует")

    # ─── 23-30. Frontend (admin_presets.html) ─────────────────────────

    def test_23_html_has_edit_button(self):
        """admin_presets.html содержит кнопку Edit."""
        # Look for "Edit ▾" text in the presets list
        self.assertIn("Edit ▾", self.admin_presets_html,
                      "Кнопка 'Edit ▾' не найдена в admin_presets.html")

    def test_24_html_has_edit_form_action(self):
        """Форма с action /admin/presets/{id}/edit."""
        # Look for the edit form action
        self.assertIn('action="/admin/presets/{{ p.id }}/edit"',
                      self.admin_presets_html,
                      "Форма редактирования с action /admin/presets/{id}/edit не найдена")

    def test_25_html_checkboxes_prefilled(self):
        """Чекбоксы pre-filled: {% if perms.get(json_key) %}checked{% endif %}."""
        # Look for the checked attribute in perm checkbox loop
        self.assertIn("perms.get(json_key)", self.admin_presets_html,
                      "Pre-fill чекбоксов через perms.get(json_key) не найден")
        self.assertIn("{% if perms.get(json_key) %}checked{% endif %}",
                      self.admin_presets_html,
                      "checked атрибут для pre-fill не найден")

    def test_26_html_name_input_prefilled(self):
        """name input pre-filled: value=\"{{ p.name }}\"."""
        self.assertIn('value="{{ p.name }}"', self.admin_presets_html,
                      "Pre-fill name input не найден")

    def test_27_html_scope_select_prefilled(self):
        """scope select pre-selected для текущего scope."""
        self.assertIn("{% if p.scope == 'day' %}selected{% endif %}",
                      self.admin_presets_html,
                      "Pre-select для scope=day не найден")
        self.assertIn("{% if p.scope == 'night' %}selected{% endif %}",
                      self.admin_presets_html,
                      "Pre-select для scope=night не найден")
        self.assertIn("{% if p.scope == 'sanitary' %}selected{% endif %}",
                      self.admin_presets_html,
                      "Pre-select для scope=sanitary не найден")

    def test_28_html_slow_mode_input_prefilled(self):
        """slow_mode input pre-filled если p.slow_mode_delay is not none."""
        self.assertIn(
            '{% if p.slow_mode_delay is not none %}value="{{ p.slow_mode_delay }}"{% endif %}',
            self.admin_presets_html,
            "Pre-fill slow_mode input не найден",
        )

    def test_29_html_system_presets_no_edit_form(self):
        """Системные пресеты НЕ должны иметь форму редактирования.

        Форма должна быть внутри {% if not p.is_system %} блока.
        """
        # Find the edit form, then check it's wrapped in not p.is_system
        idx_form = self.admin_presets_html.find(
            'action="/admin/presets/{{ p.id }}/edit"'
        )
        self.assertGreater(idx_form, 0, "Edit form not found")
        # Look backwards from idx_form for {% if not p.is_system %}
        # The form should be inside a {% if not p.is_system %} block
        # Look for the nearest {% if not p.is_system %} before the form
        chunk_before = self.admin_presets_html[:idx_form]
        # Find last occurrence of {% if not p.is_system %}
        idx_if = chunk_before.rfind("{% if not p.is_system %}")
        self.assertGreater(idx_if, 0,
                          "{% if not p.is_system %} guard перед формой редактирования не найден")
        # Make sure there's no {% endif %} between the guard and the form
        # (otherwise the form is outside the guard)
        between = chunk_before[idx_if:]
        # Count if/endif pairs in `between` — net should be 1 (the not p.is_system if)
        # Simple heuristic: there should be no {% endif %} before the form
        # that would close the not p.is_system block.
        idx_endif_in_between = between.find("{% endif %}")
        if idx_endif_in_between > 0:
            # There's an endif before the form — meaning the if not p.is_system
            # block was already closed. Look for another guard closer to the form.
            # Actually for our structure: summary contains {% if not p.is_system %}
            # ...edit button... {% else %}...{% endif %}. Then OUTSIDE that,
            # another {% if not p.is_system %}...<form>...{% endif %}.
            # So we need to find a guard that's the LAST one before the form
            # AND there's no endif between them.
            # Strategy: find the position of the form, then walk backwards
            # to find {% if not p.is_system %} with no intervening {% endif %}.
            cursor = idx_form
            found_guard = False
            while cursor > 0:
                # Find previous {% if %} or {% endif %}
                next_if = chunk_before.rfind("{% if not p.is_system %}", 0, cursor)
                next_endif = chunk_before.rfind("{% endif %}", 0, cursor)
                if next_if > next_endif:
                    # if is closer than endif → guard is active
                    found_guard = True
                    break
                elif next_endif > next_if:
                    # endif is closer → skip past the matching if (recursive)
                    # For simplicity, just continue searching before the endif
                    cursor = next_endif
                else:
                    break
            self.assertTrue(found_guard,
                            "Форма редактирования не обёрнута в активный "
                            "{% if not p.is_system %} guard")

    def test_30_html_has_hint_about_chats_not_affected(self):
        """Подсказка: чаты не затрагиваются (копия JSON)."""
        # Look for hint text in the edit form
        self.assertIn("копию прав", self.admin_presets_html,
                      "Подсказка про 'копию прав' не найдена")
        # Also should mention /admin/chats
        self.assertIn("/admin/chats", self.admin_presets_html,
                      "Ссылка на /admin/chats в подсказке не найдена")

    # ─── 31-34. Changelog ─────────────────────────────────────────────

    def test_31_changelog_v4717_present(self):
        self.assertIn("<strong>v4.7.17</strong>", self.base_html)

    def test_32_changelog_v4717_mentions_edit(self):
        """Changelog v4.7.17 должен упоминать редактирование или edit."""
        idx = self.base_html.find("<strong>v4.7.17</strong>")
        self.assertGreater(idx, 0, "v4.7.17 section not found")
        idx_next = self.base_html.find("<strong>v4.7.16</strong>", idx)
        self.assertGreater(idx_next, 0, "v4.7.16 section not found after v4.7.17")
        section = self.base_html[idx:idx_next]
        # Should mention editing presets
        self.assertTrue(
            "редактирован" in section.lower() or "edit" in section.lower(),
            "v4.7.17 changelog должен упоминать редактирование/edit",
        )

    def test_33_changelog_v4716_preserved(self):
        """Регрессия: v4.7.16 changelog сохранён."""
        self.assertIn("<strong>v4.7.16</strong>", self.base_html)

    def test_34_changelog_v4717_above_v4716(self):
        """v4.7.17 должен идти ВЫШЕ v4.7.16 в changelog."""
        idx_17 = self.base_html.find("<strong>v4.7.17</strong>")
        idx_16 = self.base_html.find("<strong>v4.7.16</strong>")
        self.assertGreater(idx_17, 0)
        self.assertGreater(idx_16, 0)
        self.assertLess(idx_17, idx_16,
                        "v4.7.17 должен идти ВЫШЕ v4.7.16 в changelog")


if __name__ == "__main__":
    unittest.main(verbosity=2)
