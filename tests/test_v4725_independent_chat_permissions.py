"""v4.7.25 — regression tests for use_independent_chat_permissions=True.

Баг: при переключении ночь → день юзерам выдавались лишние права
(голосовые сообщения, видеосообщения), не указанные в Day default preset.

Корневая причина: bot.set_chat_permissions(...) вызывался БЕЗ параметра
use_independent_chat_permissions=True. В Telegram Bot API это включает
legacy-dependent-режим с правилом импликации:
  can_send_other_messages=True  →  can_send_video_notes=True
                                   (и messages/audios/docs/photos/videos)
  can_send_voice_notes=True     →  can_send_video_notes=True (и др.)

Day default preset имеет can_send_other_messages=True (стикеры/GIFs/dice)
и can_send_video_notes=False, can_send_voice_notes=False. В legacy-режиме
False-значения тихо перезаписывались на True из-за импликации.

Фикс: во все 5 вызовов set_chat_permissions добавлен
use_independent_chat_permissions=True. Independent-режим убирает
правило импликации — каждое право устанавливается ровно так, как передано.

Тесты:
  1. APP_VERSION >= v4.7.25
  2. Day default preset в db.py содержит can_send_voice_notes=False
     и can_send_video_notes=False (это необходимо для проверки что пресет
     действительно запрещает эти права — если бы пресет был True, фикса
     независимости было бы недостаточно)
  3. Hardcoded _DAY_DEFAULT_HARDCODED в bot.py тоже содержит
     can_send_voice_notes=False и can_send_video_notes=False
  4. _night_mode_permissions_preset('text_only') возвращает ChatPermissions
     с can_send_voice_notes=False и can_send_video_notes=False
  5. _night_mode_permissions_preset('strict') (mute) тоже False на обоих
  6. Все 5 вызовов bot.set_chat_permissions в кодовой базе передают
     use_independent_chat_permissions=True:
       - bot.py: _enter_night_mode, _restore_day_state, _enter_sanitary_day
       - bot_handlers.py: _deactivate_alarm, handle_alarm_command
  7. Не должно остаться ни одного вызова set_chat_permissions БЕЗ
     use_independent_chat_permissions (в prod-коде bot.py и bot_handlers.py)
  8. Changelog в base.html содержит запись про v4.7.25
  9. В base.html changelog упомянуты "голосовые сообщения" и "видеосообщения"
     (проверка что changelog адекватно описывает баг)
 10. _alarm_permissions() в bot_handlers.py возвращает ChatPermissions с
     can_send_voice_notes=False и can_send_video_notes=False
     (alarm mode должен запрещать всё кроме текста)
"""
import ast
import re
import sys
import os
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BOT_PY = ROOT / "bot.py"
BOT_HANDLERS_PY = ROOT / "bot_handlers.py"
DB_PY = ROOT / "db.py"
WEB_APP_PY = ROOT / "web_app.py"
BASE_HTML = ROOT / "templates" / "base.html"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


class TestV4725IndependentChatPermissions(unittest.TestCase):
    """v4.7.25: regression tests for use_independent_chat_permissions=True."""

    def setUp(self):
        self.bot_src = _read(BOT_PY)
        self.handlers_src = _read(BOT_HANDLERS_PY)
        self.db_src = _read(DB_PY)
        self.web_src = _read(WEB_APP_PY)
        self.html_src = _read(BASE_HTML)

    # ── 1. Version ─────────────────────────────────────────────────────────
    def test_01_app_version_bumped(self):
        """APP_VERSION >= v4.7.25."""
        m = re.search(r'APP_VERSION\s*=\s*"v(\d+)\.(\d+)\.(\d+)"', self.web_src)
        self.assertIsNotNone(m, "APP_VERSION not found in web_app.py")
        major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
        self.assertGreaterEqual(
            (major, minor, patch),
            (4, 7, 25),
            f"APP_VERSION=v{major}.{minor}.{patch} should be >= v4.7.25",
        )

    # ── 2. Day default preset в db.py ──────────────────────────────────────
    def test_02_day_default_preset_forbids_voice_and_video_notes(self):
        """Day default preset в db.py: voice_notes=False, video_notes=False.

        Это необходимое условие для фикса — если пресет не запрещает эти
        права, то independent-режим сам по себе не поможет.
        """
        # Находим _DAY_DEFAULT dict в db.py
        m = re.search(
            r'_DAY_DEFAULT\s*=\s*\{(.*?)\}',
            self.db_src,
            re.DOTALL,
        )
        self.assertIsNotNone(m, "_DAY_DEFAULT dict not found in db.py")
        body = m.group(1)
        self.assertIn('"can_send_voice_notes": False', body,
                      "Day default должен явно запрещать can_send_voice_notes")
        self.assertIn('"can_send_video_notes": False', body,
                      "Day default должен явно запрещать can_send_video_notes")
        # И должен разрешать other_messages (стикеры/GIFs) — это ключевой
        # True-флаг, который в legacy-режиме триггерил импликацию.
        self.assertIn('"can_send_other_messages": True', body,
                      "Day default должен разрешать can_send_other_messages "
                      "(стикеры/GIFs/dice) — иначе пресет был бы text_only")

    # ── 3. Hardcoded fallback в bot.py ─────────────────────────────────────
    def test_03_hardcoded_day_default_forbids_voice_and_video_notes(self):
        """_DAY_DEFAULT_HARDCODED в bot.py: voice_notes=False, video_notes=False."""
        m = re.search(
            r'_DAY_DEFAULT_HARDCODED\s*=\s*\{(.*?)\}',
            self.bot_src,
            re.DOTALL,
        )
        self.assertIsNotNone(m, "_DAY_DEFAULT_HARDCODED dict not found in bot.py")
        body = m.group(1)
        self.assertIn('"can_send_voice_notes": False', body)
        self.assertIn('"can_send_video_notes": False', body)
        self.assertIn('"can_send_other_messages": True', body)

    # ── 4. Night mode 'text_only' preset ───────────────────────────────────
    def test_04_night_text_only_preset_forbids_voice_and_video_notes(self):
        """_night_mode_permissions_preset('text_only') в bot_handlers.py
        возвращает ChatPermissions с can_send_voice_notes=False и
        can_send_video_notes=False.
        """
        # Ищем функцию и парсим её тело через AST.
        tree = ast.parse(self.handlers_src)
        func = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_night_mode_permissions_preset":
                func = node
                break
        self.assertIsNotNone(func, "_night_mode_permissions_preset not found")
        # Ищем return types.ChatPermissions(...) внутри функции и проверяем
        # что у неё есть kwargs can_send_voice_notes=False и
        # can_send_video_notes=False.
        found_voice_false = False
        found_video_false = False
        for node in ast.walk(func):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == "ChatPermissions":
                    for kw in node.keywords:
                        if kw.arg == "can_send_voice_notes":
                            v = kw.value
                            if isinstance(v, ast.Constant) and v.value is False:
                                found_voice_false = True
                        if kw.arg == "can_send_video_notes":
                            v = kw.value
                            if isinstance(v, ast.Constant) and v.value is False:
                                found_video_false = True
        self.assertTrue(found_voice_false,
                        "text_only preset должен ставить can_send_voice_notes=False")
        self.assertTrue(found_video_false,
                        "text_only preset должен ставить can_send_video_notes=False")

    # ── 5. _mute_permissions (strict) ──────────────────────────────────────
    def test_05_mute_permissions_forbids_voice_and_video_notes(self):
        """_mute_permissions() в bot_handlers.py: voice=False, video=False.

        strict = полный мьют — все контентные права False, включая
        voice/video notes. Это garantir что night mode 'strict' действительно
        запрещает всё.
        """
        m = re.search(
            r'def _mute_permissions\(\)\s*->\s*types\.ChatPermissions:\s*'
            r'(?:.*?)*?return types\.ChatPermissions\((.*?)\)',
            self.handlers_src,
            re.DOTALL,
        )
        self.assertIsNotNone(m, "_mute_permissions not found")
        body = m.group(1)
        self.assertIn("can_send_voice_notes=False", body)
        self.assertIn("can_send_video_notes=False", body)

    # ── 6. Все вызовы set_chat_permissions передают independent=True ─
    # v4.8.0: вызовы унифицированы через _apply_chat_permissions (chat_modes.py),
    # которая внутри использует use_independent_chat_permissions=True.
    # Поэтому тест проверяет ИЛИ прямой вызов с independent=True в bot.py/
    # bot_handlers.py, ИЛИ вызов через _apply_chat_permissions.
    def test_06_all_set_chat_permissions_calls_pass_independent_true(self):
        """Все вызовы set_chat_permissions в bot.py и bot_handlers.py
        должны либо передавать use_independent_chat_permissions=True напрямую,
        либо идти через _apply_chat_permissions (которая внутри использует
        independent=True).

        Это центральная проверка фикса v4.7.25. Использует AST вместо regex
        чтобы не ловить false positive в комментариях/строках.

        v4.8.0: с рефакторингом #9 вызовы из bot.py (_enter_night_mode,
        _restore_day_state, _enter_sanitary_day) и из bot_handlers.py
        (_deactivate_alarm, handle_alarm_command) переехали в chat_modes.py
        через _apply_chat_permissions. Эти 5 вызовов не проверяются тут —
        они проверяются в test_v480_chat_modes_unified.py.
        """
        for src, fname in [
            (self.bot_src, "bot.py"),
            (self.handlers_src, "bot_handlers.py"),
        ]:
            tree = ast.parse(src)
            call_count = 0
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    if node.func.attr == "set_chat_permissions":
                        call_count += 1
                        # Собираем все kwargs в строку для readable assertion.
                        kw_strs = []
                        for kw in node.keywords:
                            if isinstance(kw.value, ast.Constant):
                                kw_strs.append(f"{kw.arg}={kw.value.value!r}")
                            else:
                                kw_strs.append(f"{kw.arg}=...")
                        call_repr = ", ".join(kw_strs)
                        self.assertIn(
                            "use_independent_chat_permissions=True",
                            call_repr,
                            f"{fname}: set_chat_permissions call missing "
                            f"use_independent_chat_permissions=True. "
                            f"Call kwargs: {call_repr}",
                        )
            # v4.8.0: в bot.py вызовы переехали в chat_modes.py — в bot.py
            # прямых вызовов может не быть вообще. bot_handlers.py: 0
            # (_deactivate_alarm и handle_alarm_command используют _apply_chat_permissions).
            # Убираем assertGreaterEqual — проверяем только корректность если вызовы есть.
            # Реальное наличие вызовов проверяется в test_v480_chat_modes_unified.py.

    # ── 7. Не должно быть вызовов без independent=True в prod-коде ─────────
    def test_07_no_set_chat_permissions_without_independent_in_prod(self):
        """В prod-коде bot.py и bot_handlers.py не должно быть вызовов
        set_chat_permissions без use_independent_chat_permissions=True.
        """
        for src, fname in [
            (self.bot_src, "bot.py"),
            (self.handlers_src, "bot_handlers.py"),
        ]:
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    if node.func.attr == "set_chat_permissions":
                        found = False
                        for kw in node.keywords:
                            if kw.arg == "use_independent_chat_permissions":
                                v = kw.value
                                if isinstance(v, ast.Constant) and v.value is True:
                                    found = True
                        self.assertTrue(
                            found,
                            f"{fname}: set_chat_permissions call at line "
                            f"{node.lineno} missing "
                            f"use_independent_chat_permissions=True",
                        )

    # ── 8. Changelog содержит v4.7.25 ──────────────────────────────────────
    def test_08_changelog_contains_v4725(self):
        """В base.html changelog есть запись про v4.7.25."""
        self.assertIn("v4.7.25", self.html_src,
                      "Changelog в base.html должен содержать v4.7.25")
        # Проверяем что это именно hotfix-запись с упоминанием бага.
        # Ищем секцию v4.7.25 — между этим заголовком и следующим v4.7.X.
        m = re.search(
            r'v4\.7\.25</strong>(.*?)(?=v4\.7\.\d+</strong>)',
            self.html_src,
            re.DOTALL,
        )
        self.assertIsNotNone(m, "v4.7.25 changelog section not found")
        section = m.group(1)
        self.assertIn("use_independent_chat_permissions", section)
        self.assertIn("импликац", section.lower(),
                      "Changelog должен упоминать правило импликации")

    # ── 9. Changelog упоминает "голосовые" и "видеосообщения" ──────────────
    def test_09_changelog_mentions_specific_rights(self):
        """Changelog v4.7.25 упоминает конкретные права: голосовые сообщения
        и видеосообщения — чтобы быть понятным пользователю.
        """
        m = re.search(
            r'v4\.7\.25</strong>(.*?)(?=v4\.7\.\d+</strong>)',
            self.html_src,
            re.DOTALL,
        )
        self.assertIsNotNone(m, "v4.7.25 changelog section not found")
        section = m.group(1)
        self.assertIn("голосовые сообщения", section.lower(),
                      "Changelog должен упоминать 'голосовые сообщения'")
        self.assertIn("видеосообщения", section.lower(),
                      "Changelog должен упоминать 'видеосообщения'")

    # ── 10. _alarm_permissions forbid voice/video ─────────────────────────
    def test_10_alarm_permissions_forbid_voice_and_video_notes(self):
        """_alarm_permissions() в bot_handlers.py: voice=False, video=False.

        Alarm mode = только текст, всё медиа запрещено. Проверка что
        alarm_permissions действительно ставит эти поля в False (т.к. теперь
        с independent=True это будет работать корректно).
        """
        m = re.search(
            r'def _alarm_permissions\(\)\s*->\s*types\.ChatPermissions:\s*'
            r'(?:.*?)*?return types\.ChatPermissions\((.*?)\)',
            self.handlers_src,
            re.DOTALL,
        )
        self.assertIsNotNone(m, "_alarm_permissions not found")
        body = m.group(1)
        self.assertIn("can_send_voice_notes=False", body,
                      "_alarm_permissions должен запрещать can_send_voice_notes")
        self.assertIn("can_send_video_notes=False", body,
                      "_alarm_permissions должен запрещать can_send_video_notes")

    # ── 11. Подсчёт вызовов set_chat_permissions (v4.8.0: ослаблен) ────────
    def test_11_set_chat_permissions_call_count(self):
        """В кодовой базе должны быть вызовы set_chat_permissions (хотя бы один).

        v4.8.0: с рефакторингом #9 многие вызовы переехали в chat_modes.py.
        Поэтому проверяем только что в bot.py + bot_handlers.py есть хотя бы
        упоминание set_chat_permissions (для регрессии — если кто-то случайно
        удалит все вызовы). Реальное наличие вызовов проверяется в
        test_v480_chat_modes_unified.py.
        """
        bot_count = self.bot_src.count("set_chat_permissions")
        handlers_count = self.handlers_src.count("set_chat_permissions")
        # v4.8.0: просто проверяем что хотя бы одно упоминание есть
        # (включая комментарии и импорты). Если все вызовы уйдут в chat_modes.py,
        # бот.py может иметь только import — это нормально.
        total = bot_count + handlers_count
        self.assertGreater(
            total, 0,
            f"bot.py + bot_handlers.py: expected >0 set_chat_permissions mentions, "
            f"got {total}",
        )

    def _find_async_func(self, tree, name):
        """Ищет async или sync функцию по имени."""
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                return node
        return None

    def _has_set_chat_permissions_with_independent(self, func) -> bool:
        """Проверяет что в теле функции есть вызов set_chat_permissions с
        use_independent_chat_permissions=True."""
        for node in ast.walk(func):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == "set_chat_permissions":
                    for kw in node.keywords:
                        if kw.arg == "use_independent_chat_permissions":
                            v = kw.value
                            if isinstance(v, ast.Constant) and v.value is True:
                                return True
        return False

    # ── 12. bot.py: _restore_day_state использует independent (прямо или через _apply) ───
    # v4.8.0: проверка что _restore_day_state либо вызывает set_chat_permissions
    # с independent=True напрямую, либо использует _apply_chat_permissions
    # (которая внутри использует independent=True).
    def test_12_restore_day_state_uses_independent(self):
        """Конкретная проверка: _restore_day_state в bot.py либо вызывает
        set_chat_permissions с use_independent_chat_permissions=True напрямую,
        либо использует _apply_chat_permissions (v4.8.0 рефакторинг).
        """
        tree = ast.parse(self.bot_src)
        func = self._find_async_func(tree, "_restore_day_state")
        self.assertIsNotNone(func, "_restore_day_state not found in bot.py")
        ok = (
            self._has_set_chat_permissions_with_independent(func)
            or self._has_apply_chat_permissions_call(func)
        )
        self.assertTrue(
            ok,
            "_restore_day_state должна либо вызывать set_chat_permissions с "
            "use_independent_chat_permissions=True напрямую, либо использовать "
            "_apply_chat_permissions (v4.8.0 унификация)",
        )

    # ── 13. bot.py: _enter_night_mode использует independent (прямо или через _apply) ──
    def test_13_enter_night_mode_uses_independent(self):
        """_enter_night_mode в bot.py: тоже independent (прямо или через _apply).

        Хотя ночной пресет text_only (все False кроме can_send_messages=True)
        не триггерит импликацию в legacy-режиме, мы всё равно передаём
        independent=True для консистентности и защиты от будущих изменений
        night preset (если кто-то выберет custom preset с other_messages=True).
        """
        tree = ast.parse(self.bot_src)
        func = self._find_async_func(tree, "_enter_night_mode")
        self.assertIsNotNone(func, "_enter_night_mode not found in bot.py")
        ok = (
            self._has_set_chat_permissions_with_independent(func)
            or self._has_apply_chat_permissions_call(func)
        )
        self.assertTrue(
            ok,
            "_enter_night_mode должна использовать independent (прямо или через _apply)",
        )

    # ── 14. bot.py: _enter_sanitary_day использует independent (прямо или через _apply)
    def test_14_enter_sanitary_day_uses_independent(self):
        """_enter_sanitary_day в bot.py: independent (прямо или через _apply)."""
        tree = ast.parse(self.bot_src)
        func = self._find_async_func(tree, "_enter_sanitary_day")
        self.assertIsNotNone(func, "_enter_sanitary_day not found in bot.py")
        ok = (
            self._has_set_chat_permissions_with_independent(func)
            or self._has_apply_chat_permissions_call(func)
        )
        self.assertTrue(
            ok,
            "_enter_sanitary_day должна использовать independent (прямо или через _apply)",
        )

    def _has_apply_chat_permissions_call(self, func) -> bool:
        """v4.8.0: проверяет что в теле функции есть вызов _apply_chat_permissions
        (которая внутри использует use_independent_chat_permissions=True).
        """
        for node in ast.walk(func):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == "_apply_chat_permissions":
                    return True
            # Также проверяем aliases (если импортировано под другим именем).
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if "_apply_chat_permissions" in node.func.id:
                    return True
        return False


if __name__ == "__main__":
    unittest.main(verbosity=2)
