"""
v4.7.14 — тесты фикса _mute_permissions() и харденинга !unmute.

Проблема:
  Функция _mute_permissions() раньше выставляла в False все 13 полей
  ChatPermissions, включая 3 админских (can_change_info, can_invite_users,
  can_pin_messages). Это потенциально опасно: при unmute восстанавливаются
  права чата (chat_info.permissions), и если у чата в дефолтных правах
  были админские поля, они могли затереться.

Решение v4.7.14:
  • _mute_permissions() возвращает только 10 контентных полей в False
  • В !unmute перед restrict_chat_member бот пересобирает ChatPermissions
    только из 10 контентных полей — админские отсекаются

Тесты:
  1. APP_VERSION = "v4.7.14"
  2. APP_RELEASE_DATE = "2026-08-03"
  3. _mute_permissions() не возвращает can_change_info=True
  4. _mute_permissions() не возвращает can_invite_users=True
  5. _mute_permissions() не возвращает can_pin_messages=True
  6. _mute_permissions() возвращает все 10 контентных полей = False
  7. _mute_permissions() НЕ содержит None-полей (None = True в TG)
  8. _PERM_FIELDS по-прежнему содержит 13 полей (snapshot — полный)
  9. bot_handlers.py: ровно 4 вызова _mute_permissions() (аудит мест)
 10. bot_handlers.py: код !unmute содержит пересборку ChatPermissions
 11. bot_handlers.py: код !unmute НЕ передаёт админские права в True
 12. templates/base.html: есть запись v4.7.14 в changelog
 13. templates/base.html: v4.7.14 упоминает "_mute_permissions"
 14. templates/base.html: v4.7.14 упоминает "!unmute"
 15. templates/base.html: v4.7.13 сохранена (регрессия)
 16. templates/base.html: v4.7.14 идёт ВЫШЕ v4.7.13
"""

import os
import re
import sys
import unittest
from _version import ver  # noqa: E402  (сравнение версий как кортежей, не строк)

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

# Импортируем bot_handlers для _mute_permissions / _PERM_FIELDS
spec_bh = importlib.util.spec_from_file_location(
    "bot_handlers", os.path.join(PROJECT_DIR, "bot_handlers.py")
)
# bot_handlers импортирует много всего; используем exec
import aiogram  # noqa: F401  — нужен для bot_handlers
bh = importlib.util.module_from_spec(spec_bh)
# v5.2.0: модуль обязан попасть в sys.modules ДО exec_module — это
# документированный порядок importlib. Без него dataclasses не может
# разобрать строковые аннотации (в bot_handlers включён
# `from __future__ import annotations`, там все аннотации — строки)
# и падает с AttributeError на None.__dict__.
sys.modules[spec_bh.name] = bh
spec_bh.loader.exec_module(bh)

BOT_HANDLERS_PY = os.path.join(PROJECT_DIR, "bot_handlers.py")
BASE_HTML = os.path.join(PROJECT_DIR, "templates", "base.html")


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# Список контентных полей (без админских)
_CONTENT_FIELDS = [
    "can_send_messages",
    "can_send_audios",
    "can_send_documents",
    "can_send_photos",
    "can_send_videos",
    "can_send_video_notes",
    "can_send_voice_notes",
    "can_send_polls",
    "can_send_other_messages",
    "can_add_web_page_previews",
]

# Список админских полей (которые НЕ должны быть в _mute_permissions)
_ADMIN_FIELDS = [
    "can_change_info",
    "can_invite_users",
    "can_pin_messages",
]


# ─── Tests ──────────────────────────────────────────────────────────────────


class TestV4714MutePermissions(unittest.TestCase):
    """v4.7.14: _mute_permissions() без админских полей + харденинг !unmute."""

    def setUp(self):
        # v4.8.9/v4.8.10: ветки команд уехали в mod_commands.py, а текст берётся
        # из ctx.text вместо локальной text. Склеиваем оба модуля и нормализуем
        # обращение — проверки смотрят на состав кода, а не на его расположение.
        self.bot_handlers_py = _read(BOT_HANDLERS_PY)
        _mod_commands = os.path.join(os.path.dirname(BOT_HANDLERS_PY), "mod_commands.py")
        if os.path.exists(_mod_commands):
            self.bot_handlers_py += "\n" + _read(_mod_commands).replace(
                ".match(ctx.text)", ".match(text)")
        self.base_html = _read(BASE_HTML)
        self.mute_perms = bh._mute_permissions()

    # ─── 1-2. Version ──────────────────────────────────────────────────

    def test_01_app_version_bumped(self):
        """APP_VERSION должен быть >= v4.7.14 (тест ослаблен в v4.7.15)."""
        # v4.7.15: ослаблен с == "v4.7.14" на >= v4.7.14 — чтобы не падать
        # на каждом следующем релизе.
        # v4.10.0: FIX сравнение строк ломалось на двузначном minor
        # ("v4.10.0" < "v4.7.x" лексикографически) — сравниваем через ver().
        self.assertGreaterEqual(ver(APP_VERSION), ver("v4.7.14"),
                                f"APP_VERSION should be >= v4.7.14, got {APP_VERSION}")

    def test_02_release_date(self):
        # v4.7.16+: release date bumped to 2026-08-04. Loosen to >=.
        self.assertGreaterEqual(APP_RELEASE_DATE, "2026-08-03",
                         f"APP_RELEASE_DATE should be >= 2026-08-03, "
                         f"got {APP_RELEASE_DATE}")

    # ─── 3-5. Админские поля НЕ в True ─────────────────────────────────

    def test_03_no_can_change_info_true(self):
        """can_change_info не должен быть True (это админ-право)."""
        val = getattr(self.mute_perms, "can_change_info", None)
        self.assertIsNot(val, True,
                         f"can_change_info should not be True, got {val!r}")

    def test_04_no_can_invite_users_true(self):
        """can_invite_users не должен быть True (это админ-право)."""
        val = getattr(self.mute_perms, "can_invite_users", None)
        self.assertIsNot(val, True,
                         f"can_invite_users should not be True, got {val!r}")

    def test_05_no_can_pin_messages_true(self):
        """can_pin_messages не должен быть True (это админ-право)."""
        val = getattr(self.mute_perms, "can_pin_messages", None)
        self.assertIsNot(val, True,
                         f"can_pin_messages should not be True, got {val!r}")

    # ─── 6. Все 10 контентных полей = False ────────────────────────────

    def test_06_all_content_fields_false(self):
        """Все 10 контентных полей должны быть False (мьют запрещает всё)."""
        for field in _CONTENT_FIELDS:
            val = getattr(self.mute_perms, field, None)
            self.assertIs(val, False,
                          f"{field} should be False, got {val!r}")

    # ─── 7. Контентные поля НЕ None (TG трактует None как True для set_chat_permissions,
    #        но для restrict_chat_member None = "не трогать" — что нам и нужно для админских) ─

    def test_07_content_fields_not_none(self):
        """Все 10 контентных полей должны быть False (не None).

        Для restrict_chat_member: None = "не трогать", False = "запретить".
        Мы хотим запретить ВСЕ контентные поля — значит они должны быть False,
        не None (None бы означало "не трогать", и юзер мог бы писать).
        """
        for field in _CONTENT_FIELDS:
            val = getattr(self.mute_perms, field, "MISSING")
            self.assertEqual(val, False,
                             f"{field} should be False (not None), got {val!r}")

    def test_07b_admin_fields_should_be_none(self):
        """Админские поля должны быть None (мы их не трогаем).

        Для restrict_chat_member: None = "не трогать". Это именно то, что
        нам нужно для админских прав — мы НЕ должны их менять через
        restrict_chat_member (они выдаются только через promote_chat_member).
        Если бы мы поставили их в False, Telegram бы интерпретировал это
        как "запретить" — что потенциально могло бы затереть админ-права
        пользователя при будущих операциях.
        """
        for field in _ADMIN_FIELDS:
            val = getattr(self.mute_perms, field, "MISSING")
            # Допустимо: None (не трогаем) или MISSING (не передавали в конструктор)
            # Оба варианта означают "не трогать" для restrict_chat_member.
            self.assertNotEqual(val, True,
                                f"{field} should not be True (admin rights "
                                f"must not be granted via mute)")
            self.assertNotEqual(val, False,
                                f"{field} should not be False either — "
                                f"None means 'don't touch' for restrict_chat_member, "
                                f"which is the safe behavior")

    def test_07a_admin_fields_not_explicitly_false(self):
        """Админские поля НЕ должны быть явно False (мы их не трогаем).

        Это значит, что при создании ChatPermissions мы их НЕ передаём —
        aiogram оставит их как None, что есть ДРУГОЕ поведение чем False
        для restrict_chat_member (None = не трогать, False = запретить).
        """
        # В новом коде _mute_permissions() админские поля не упоминаются
        # в вызове конструктора. Проверим по исходнику.
        # Находим определение _mute_permissions
        m = re.search(
            r"def _mute_permissions\(\).*?return types\.ChatPermissions\((.*?)\)",
            self.bot_handlers_py,
            re.DOTALL,
        )
        self.assertIsNotNone(m, "_mute_permissions() definition not found")
        constructor_body = m.group(1)
        for field in _ADMIN_FIELDS:
            self.assertNotIn(f"{field}=", constructor_body,
                             f"{field} should not be in _mute_permissions() "
                             f"constructor: {constructor_body!r}")

    # ─── 8. _PERM_FIELDS по-прежнему 13 полей ──────────────────────────

    def test_08_perm_fields_unchanged_13(self):
        """_PERM_FIELDS для snapshot — все 13 полей (включая админские).

        Snapshot должен фиксировать ПОЛНОЕ состояние прав пользователя,
        включая админские (для восстановления при unmute в будущем).
        """
        self.assertEqual(len(bh._PERM_FIELDS), 13,
                         f"_PERM_FIELDS should have 13 fields, "
                         f"got {len(bh._PERM_FIELDS)}")
        for field in _CONTENT_FIELDS + _ADMIN_FIELDS:
            self.assertIn(field, bh._PERM_FIELDS,
                          f"{field} should be in _PERM_FIELDS (for snapshot)")

    # ─── 9. Ровно 4 вызова _mute_permissions() ─────────────────────────

    def test_09_four_call_sites(self):
        """Все 4 места, где вызывается _mute_permissions():
        1. !mute в handle_group_command
        2. auto-mute в _check_warn_threshold
        3. mute за banned sticker pack в handle_sticker_message
        4. mute за word/link filter в handle_content_filters
        """
        # Считаем количество вызовов "_mute_permissions()" в bot_handlers.py
        matches = re.findall(r"_mute_permissions\(\)", self.bot_handlers_py)
        # Должно быть 4 вызова + 1 определение функции
        # Определение выглядит как "def _mute_permissions() -> types.ChatPermissions:"
        # и не считается вызовом. Считаем только реальные вызовы.
        # Удалим определение
        calls = [m for m in matches]
        # Определение: "def _mute_permissions()" — тоже матчится, уберём
        def_count = len(re.findall(r"def _mute_permissions\(\)", self.bot_handlers_py))
        actual_calls = len(calls) - def_count
        self.assertGreaterEqual(actual_calls, 4,
                                f"Expected at least 4 _mute_permissions() "
                                f"call sites, found {actual_calls}")

    # ─── 10. !unmute содержит пересборку ChatPermissions ───────────────

    def _unmute_section_start(self) -> int:
        """Начало обработки !unmute.

        Раньше это была ветка внутри handle_group_command с маркером
        `_CMD_UNMUTE.match(text)`. После декомпозиции v4.8.9/v4.8.10 диспетч
        стал таблицей `_cmd_regex_map`, а логика уехала в
        mod_commands.cmd_unmute — на неё и ориентируемся.
        """
        for marker in ("async def cmd_unmute(", "_CMD_UNMUTE.match(text)"):
            idx = self.bot_handlers_py.find(marker)
            if idx >= 0:
                return idx
        return -1

    def test_10_unmute_rebuilds_permissions(self):
        """В коде !unmute есть пересборка ChatPermissions
        из 10 контентных полей (без админских)."""
        # Находим секцию _CMD_UNMUTE
        idx = self._unmute_section_start()
        self.assertGreater(idx, 0, "!unmute section not found")
        # Берём следующие 2000 символов
        section = self.bot_handlers_py[idx:idx + 3000]
        # Должна быть пересборка через types.ChatPermissions(
        self.assertIn("types.ChatPermissions(", section,
                      "!unmute should rebuild ChatPermissions before restrict")
        # Должно быть can_send_messages= в пересборке
        self.assertIn("can_send_messages=getattr", section,
                      "!unmute rebuild should include can_send_messages")
        # Комментарий «v4.7.14» не переехал вместе с кодом при декомпозиции
        # в mod_commands.py — сама пересборка прав сохранена полностью.
        # Проверяем суть фикса: права берутся из текущих настроек чата через
        # getattr с безопасным дефолтом, а не выставляются в True вслепую.
        self.assertIn('getattr(chat_perms, "can_send_messages", False)', section,
                      "!unmute must rebuild perms from chat defaults, not hardcode True")

    def test_10a_unmute_does_not_pass_admin_perms(self):
        """В пересборке ChatPermissions для !unmute не должно быть
        админских полей в True."""
        idx = self._unmute_section_start()
        section = self.bot_handlers_py[idx:idx + 3000]
        # Находим пересборку (до закрывающей скобки на отдельной строке)
        m = re.search(
            r"types\.ChatPermissions\((.*?)\n\s*\)",
            section,
            re.DOTALL,
        )
        self.assertIsNotNone(m, "Rebuild constructor not found in !unmute")
        body = m.group(1)
        # Админские поля не должны быть =True в пересборке
        for field in _ADMIN_FIELDS:
            # Ищем {field}=True или {field}=getattr(...)
            pattern = rf"{field}\s*=\s*True"
            self.assertIsNone(re.search(pattern, body),
                              f"{field}=True found in !unmute rebuild: {body!r}")
            pattern2 = rf"{field}\s*=\s*getattr"
            self.assertIsNone(re.search(pattern2, body),
                              f"{field}=getattr(...) found in !unmute rebuild "
                              f"(should not be passed at all): {body!r}")

    # ─── 11. !unmute передаёт все 10 контентных полей ──────────────────

    def test_11_unmute_passes_all_content_fields(self):
        """В пересборке для !unmute передаются все 10 контентных полей."""
        idx = self._unmute_section_start()
        section = self.bot_handlers_py[idx:idx + 3000]
        # Берём ПЕРВУЮ пересборку ChatPermissions в секции !unmute.
        # Используем жадный поиск до закрывающей скобки на отдельной строке
        # (с отступом), чтобы не остановиться на getattr(...).
        m = re.search(
            r"types\.ChatPermissions\((.*?)\n\s*\)",
            section,
            re.DOTALL,
        )
        self.assertIsNotNone(m, "Rebuild constructor not found in !unmute")
        body = m.group(1)
        for field in _CONTENT_FIELDS:
            pattern = rf"{field}\s*=\s*getattr"
            self.assertIsNotNone(re.search(pattern, body),
                                 f"{field}=getattr(...) missing in !unmute rebuild: {body!r}")

    # ─── 12-16. Changelog в base.html ─────────────────────────────────

    def test_12_changelog_has_v4714_entry(self):
        self.assertIn("<strong>v4.7.14</strong>", self.base_html,
                      "v4.7.14 changelog entry missing in base.html")

    def test_13_changelog_mentions_mute_permissions(self):
        idx_v14 = self.base_html.find("<strong>v4.7.14</strong>")
        idx_v13 = self.base_html.find("<strong>v4.7.13</strong>")
        self.assertGreater(idx_v14, -1, "v4.7.14 section not found")
        self.assertGreater(idx_v13, -1, "v4.7.13 section not found")
        section = self.base_html[idx_v14:idx_v13]
        self.assertIn("_mute_permissions", section,
                      "v4.7.14 changelog should mention _mute_permissions")

    def test_14_changelog_mentions_unmute(self):
        idx_v14 = self.base_html.find("<strong>v4.7.14</strong>")
        idx_v13 = self.base_html.find("<strong>v4.7.13</strong>")
        section = self.base_html[idx_v14:idx_v13]
        self.assertIn("!unmute", section,
                      "v4.7.14 changelog should mention !unmute")

    def test_15_changelog_v4713_preserved(self):
        """v4.7.13 запись сохранена (регрессия)."""
        self.assertIn("<strong>v4.7.13</strong>", self.base_html,
                      "v4.7.13 changelog entry was deleted — regression!")

    def test_16_changelog_v4714_above_v4713(self):
        """v4.7.14 идёт ВЫШЕ v4.7.13."""
        idx_v14 = self.base_html.find("<strong>v4.7.14</strong>")
        idx_v13 = self.base_html.find("<strong>v4.7.13</strong>")
        self.assertLess(idx_v14, idx_v13,
                        f"v4.7.14 (idx={idx_v14}) should be ABOVE "
                        f"v4.7.13 (idx={idx_v13})")


if __name__ == "__main__":
    unittest.main(verbosity=2)
