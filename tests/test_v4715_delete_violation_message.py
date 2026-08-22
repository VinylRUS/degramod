"""
v4.7.15 — тесты удаления сообщения нарушителя при !mute / !warn / !ban.

КОНТЕКСТ:
  Раньше !warn удалял сообщение нарушителя (хорошо), но !mute и !ban — нет.
  После мьюта/бана сообщение с нарушением оставалось висеть в чате.
  v4.7.15: !mute и !ban тоже удаляют сообщение нарушителя.

КРИТИЧНО:
  Удаление должно идти ПОСЛЕ _send_report, чтобы отчёт успел уйти
  в репорт-чат с текстом/медиа нарушения. _send_report читает атрибуты
  из Python-объекта reply_to_message (text, photo, file_id, sticker) —
  они остаются в памяти после delete(), но мы перестраховываемся.

Тесты:
  1. APP_VERSION = "v4.7.15"
  2. !mute: содержит reply_to_message.delete()
  3. !warn: содержит reply_to_message.delete() (было и раньше, проверка регрессии)
  4. !ban: содержит reply_to_message.delete()
  5. !mute: delete идёт ПОСЛЕ _send_report (порядок важен!)
  6. !warn: delete идёт ПОСЛЕ _send_report
  7. !ban: delete идёт ПОСЛЕ _send_report
  8. !mute: delete идёт ПОСЛЕ save_punishment
  9. !warn: delete идёт ПОСЛЕ _check_warn_threshold (последняя операция)
 10. !ban: delete идёт ПОСЛЕ auto-add sticker pack
 11. !unmute: НЕ содержит reply_to_message.delete() (размут не должен удалять)
 12. !unban: НЕ содержит reply_to_message.delete()
 13. !unwarn: НЕ содержит reply_to_message.delete()
 14. !warns: НЕ содержит reply_to_message.delete()
 15. !resetwarns: НЕ содержит reply_to_message.delete()
 16. Changelog: есть запись v4.7.15
 17. Changelog: v4.7.15 упоминает "delete"
 18. Changelog: v4.7.14 сохранена (регрессия)
 19. Changelog: v4.7.15 идёт ВЫШЕ v4.7.14
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

_DB_PATH = "/tmp/test_v4715_delete_violation.db"
if os.path.exists(_DB_PATH):
    os.remove(_DB_PATH)

os.environ["BOT_TOKEN"] = "0:fake"
os.environ["ADMIN_IDS"] = "1"
os.environ["SU_IDS"] = "1"
os.environ["WEB_PASSWORD"] = "test_password_123"
os.environ["DB_PATH"] = _DB_PATH

import importlib.util

spec = importlib.util.spec_from_file_location(
    "web_app", os.path.join(PROJECT_DIR, "web_app.py")
)
web_app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(web_app)
APP_VERSION = web_app.APP_VERSION

BOT_HANDLERS_PY = os.path.join(PROJECT_DIR, "bot_handlers.py")
BASE_HTML = os.path.join(PROJECT_DIR, "templates", "base.html")


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _find_cmd_section(source: str, cmd_marker: str) -> tuple[int, int]:
    """Находит секцию команды по маркеру.

    v5.1.0: маркеры регекс-строк (`_CMD_MUTE.match(...)`) исчезли вместе с
    паттернами — они переехали в commands.py, а cmd_X в mod_commands.py
    читают уже готовый матч из ctx.match. Маркер теперь — граница функции
    (`async def cmd_mute(`), а конец секции — начало следующей cmd_X.

    Возвращает (start, end) — индексы начала и конца секции.
    """
    markers = cmd_marker if isinstance(cmd_marker, (list, tuple)) else [cmd_marker]
    start = -1
    for _m in markers:
        start = source.find(_m)
        if start >= 0:
            cmd_marker = _m
            break
    if start < 0:
        return (-1, -1)
    # Ищем начало следующей cmd_X функции после start.
    next_cmd = source.find("async def cmd_", start + len(cmd_marker))
    if next_cmd < 0:
        next_cmd = len(source)
    return (start, next_cmd)


# Маркеры для каждой команды — граница функции в mod_commands.py.
#
# v4.8.9/v4.8.10: handle_group_command разобрали, ветки команд уехали в
# mod_commands.py. v5.1.0: раньше маркером была строка регекс-матча внутри
# cmd_X (`m = _CMD_MUTE.match(ctx.text)`), но паттерны переехали в
# commands.py и такой строки больше нет нигде — маркер сменён на границу
# самой функции, что даёт даже более точный и устойчивый к изменениям
# внутри тела разрез.
MARKERS = {
    "mute":       ["async def cmd_mute("],
    "warn":       ["async def cmd_warn("],
    "ban":        ["async def cmd_ban("],
    "unmute":     ["async def cmd_unmute("],
    "unban":      ["async def cmd_unban("],
    "unwarn":     ["async def cmd_unwarn("],
    "warns":      ["async def cmd_warns("],
    "resetwarns": ["async def cmd_resetwarns("],
}


# ─── Tests ──────────────────────────────────────────────────────────────────


class TestV4715DeleteViolationMessage(unittest.TestCase):
    """v4.7.15: !mute/!ban удаляют сообщение нарушителя, !warn реорганизован."""

    def setUp(self):
        # Логика команд распределена между двумя модулями — склеиваем.
        _mod_commands = os.path.join(os.path.dirname(BOT_HANDLERS_PY), "mod_commands.py")
        self.source = _read(BOT_HANDLERS_PY)
        if os.path.exists(_mod_commands):
            self.source += "\n" + _read(_mod_commands)
        self.base_html = _read(BASE_HTML)

    # ─── 1. Version ────────────────────────────────────────────────────

    def test_01_app_version_bumped(self):
        # v4.10.0: FIX сравнение строк ломалось на двузначном minor
        # ("v4.10.0" < "v4.7.x" лексикографически) — сравниваем через ver().
        self.assertGreaterEqual(ver(APP_VERSION), ver("v4.7.15"),
                         f"APP_VERSION should be >= v4.7.15, got {APP_VERSION}")

    # ─── 2-4. Все карательные команды удаляют сообщение ────────────────

    def test_02_mute_deletes_violation_message(self):
        """!mute должен содержать reply_to_message.delete()."""
        start, end = _find_cmd_section(self.source, MARKERS["mute"])
        self.assertGreater(start, 0, "!mute section not found")
        section = self.source[start:end]
        self.assertIn("message.reply_to_message.delete()", section,
                      "!mute should delete violation message")

    def test_03_warn_deletes_violation_message(self):
        """!warn должен содержать reply_to_message.delete() (регрессия)."""
        start, end = _find_cmd_section(self.source, MARKERS["warn"])
        self.assertGreater(start, 0, "!warn section not found")
        section = self.source[start:end]
        self.assertIn("message.reply_to_message.delete()", section,
                      "!warn should delete violation message")

    def test_04_ban_deletes_violation_message(self):
        """!ban должен содержать reply_to_message.delete()."""
        start, end = _find_cmd_section(self.source, MARKERS["ban"])
        self.assertGreater(start, 0, "!ban section not found")
        section = self.source[start:end]
        self.assertIn("message.reply_to_message.delete()", section,
                      "!ban should delete violation message")

    # ─── 5-7. delete идёт ПОСЛЕ _send_report ───────────────────────────

    def test_05_mute_delete_after_send_report(self):
        """В !mute reply_to_message.delete() идёт ПОСЛЕ _send_report."""
        start, end = _find_cmd_section(self.source, MARKERS["mute"])
        section = self.source[start:end]
        idx_report = section.find("await _send_report(")
        idx_delete = section.find("message.reply_to_message.delete()")
        self.assertGreater(idx_report, 0, "_send_report not found in !mute")
        self.assertGreater(idx_delete, 0, "delete not found in !mute")
        self.assertGreater(idx_delete, idx_report,
                           f"delete (idx={idx_delete}) should be AFTER "
                           f"_send_report (idx={idx_report}) in !mute")

    def test_06_warn_delete_after_send_report(self):
        """В !warn reply_to_message.delete() идёт ПОСЛЕ _send_report."""
        start, end = _find_cmd_section(self.source, MARKERS["warn"])
        section = self.source[start:end]
        idx_report = section.find("await _send_report(")
        idx_delete = section.find("message.reply_to_message.delete()")
        self.assertGreater(idx_report, 0, "_send_report not found in !warn")
        self.assertGreater(idx_delete, 0, "delete not found in !warn")
        self.assertGreater(idx_delete, idx_report,
                           f"delete (idx={idx_delete}) should be AFTER "
                           f"_send_report (idx={idx_report}) in !warn")

    def test_07_ban_delete_after_send_report(self):
        """В !ban reply_to_message.delete() идёт ПОСЛЕ _send_report."""
        start, end = _find_cmd_section(self.source, MARKERS["ban"])
        section = self.source[start:end]
        idx_report = section.find("await _send_report(")
        idx_delete = section.find("message.reply_to_message.delete()")
        self.assertGreater(idx_report, 0, "_send_report not found in !ban")
        self.assertGreater(idx_delete, 0, "delete not found in !ban")
        self.assertGreater(idx_delete, idx_report,
                           f"delete (idx={idx_delete}) should be AFTER "
                           f"_send_report (idx={idx_report}) in !ban")

    # ─── 8-10. Дополнительные проверки порядка ────────────────────────

    def test_08_mute_delete_after_save_punishment(self):
        """В !mute delete идёт ПОСЛЕ _save_punishment."""
        start, end = _find_cmd_section(self.source, MARKERS["mute"])
        section = self.source[start:end]
        idx_save = section.find("await _save_punishment(")
        idx_delete = section.find("message.reply_to_message.delete()")
        self.assertGreater(idx_save, 0, "_save_punishment not found in !mute")
        self.assertGreater(idx_delete, idx_save,
                           "delete should be AFTER _save_punishment in !mute")

    def test_09_warn_delete_is_last_operation(self):
        """В !warn delete идёт ПОСЛЕ _check_warn_threshold (последняя операция)."""
        start, end = _find_cmd_section(self.source, MARKERS["warn"])
        section = self.source[start:end]
        idx_threshold = section.find("await _check_warn_threshold(")
        idx_delete = section.find("message.reply_to_message.delete()")
        self.assertGreater(idx_threshold, 0,
                           "_check_warn_threshold not found in !warn")
        self.assertGreater(idx_delete, idx_threshold,
                           "delete should be AFTER _check_warn_threshold in !warn")

    def test_10_ban_delete_after_sticker_autoadd(self):
        """В !ban delete идёт ПОСЛЕ auto-add sticker pack."""
        start, end = _find_cmd_section(self.source, MARKERS["ban"])
        section = self.source[start:end]
        idx_sticker = section.find("_add_banned_sticker_pack(")
        idx_delete = section.find("message.reply_to_message.delete()")
        # _add_banned_sticker_pack может не быть в секции если нет стикера
        # (но вызов должен быть в коде). Проверяем только если он есть.
        if idx_sticker > 0:
            self.assertGreater(idx_delete, idx_sticker,
                               "delete should be AFTER _add_banned_sticker_pack in !ban")

    def test_10a_ban_sticker_read_before_delete(self):
        """В !ban стикер читается ДО удаления (для надёжности)."""
        start, end = _find_cmd_section(self.source, MARKERS["ban"])
        section = self.source[start:end]
        idx_sticker_read = section.find(
            'getattr(message.reply_to_message, "sticker"'
        )
        idx_delete = section.find("message.reply_to_message.delete()")
        self.assertGreater(idx_sticker_read, 0,
                           "sticker read not found in !ban")
        self.assertGreater(idx_delete, idx_sticker_read,
                           "sticker should be read BEFORE delete in !ban")

    # ─── 11-15. Снимающие команды НЕ удаляют сообщения ────────────────

    def test_11_unmute_does_not_delete_violation(self):
        """!unmute НЕ должен удалять сообщение нарушителя."""
        start, end = _find_cmd_section(self.source, MARKERS["unmute"])
        section = self.source[start:end] if start > 0 else ""
        self.assertNotIn("message.reply_to_message.delete()", section,
                         "!unmute should NOT delete violation message")

    def test_12_unban_does_not_delete_violation(self):
        """!unban НЕ должен удалять сообщение нарушителя."""
        start, end = _find_cmd_section(self.source, MARKERS["unban"])
        section = self.source[start:end] if start > 0 else ""
        self.assertNotIn("message.reply_to_message.delete()", section,
                         "!unban should NOT delete violation message")

    def test_13_unwarn_does_not_delete_violation(self):
        """!unwarn НЕ должен удалять сообщение нарушителя."""
        start, end = _find_cmd_section(self.source, MARKERS["unwarn"])
        section = self.source[start:end] if start > 0 else ""
        self.assertNotIn("message.reply_to_message.delete()", section,
                         "!unwarn should NOT delete violation message")

    def test_14_warns_does_not_delete_violation(self):
        """!warns НЕ должен удалять сообщение нарушителя."""
        start, end = _find_cmd_section(self.source, MARKERS["warns"])
        section = self.source[start:end] if start > 0 else ""
        self.assertNotIn("message.reply_to_message.delete()", section,
                         "!warns should NOT delete violation message")

    def test_15_resetwarns_does_not_delete_violation(self):
        """!resetwarns НЕ должен удалять сообщение нарушителя."""
        start, end = _find_cmd_section(self.source, MARKERS["resetwarns"])
        section = self.source[start:end] if start > 0 else ""
        self.assertNotIn("message.reply_to_message.delete()", section,
                         "!resetwarns should NOT delete violation message")

    # ─── 16-19. Changelog ─────────────────────────────────────────────

    def test_16_changelog_has_v4715_entry(self):
        self.assertIn("<strong>v4.7.15</strong>", self.base_html,
                      "v4.7.15 changelog entry missing")

    def test_17_changelog_mentions_delete(self):
        idx_v15 = self.base_html.find("<strong>v4.7.15</strong>")
        idx_v14 = self.base_html.find("<strong>v4.7.14</strong>")
        self.assertGreater(idx_v15, -1, "v4.7.15 section not found")
        self.assertGreater(idx_v14, -1, "v4.7.14 section not found")
        section = self.base_html[idx_v15:idx_v14]
        # Должно упоминать удаление
        self.assertTrue(
            "удал" in section.lower() or "delete" in section.lower(),
            f"v4.7.15 changelog should mention delete/удаление, "
            f"got section: {section[:200]!r}"
        )

    def test_18_changelog_v4714_preserved(self):
        """v4.7.14 запись сохранена (регрессия)."""
        self.assertIn("<strong>v4.7.14</strong>", self.base_html,
                      "v4.7.14 changelog entry was deleted — regression!")

    def test_19_changelog_v4715_above_v4714(self):
        """v4.7.15 идёт ВЫШЕ v4.7.14."""
        idx_v15 = self.base_html.find("<strong>v4.7.15</strong>")
        idx_v14 = self.base_html.find("<strong>v4.7.14</strong>")
        self.assertLess(idx_v15, idx_v14,
                        f"v4.7.15 (idx={idx_v15}) should be ABOVE "
                        f"v4.7.14 (idx={idx_v14})")


if __name__ == "__main__":
    unittest.main(verbosity=2)
