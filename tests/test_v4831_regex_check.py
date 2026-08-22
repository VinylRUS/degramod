"""v4.8.3.1 — разбор аргументов у стелс-команд !smute / !swarn / !sban.

Хотфикс v4.8.3.1 переосмыслил три regex: в v4.8.3 `_CMD_SWARN`/`_CMD_SBAN`
не матчили вызов с одним только target, а `_CMD_SMUTE` делал всё тело
опциональным, из-за чего `!smute` без длительности проходил дальше и падал
на `_parse_duration(None)`.

v4.10.1 (Task 18): файл был диагностическим скриптом — печатал разбор и
всегда возвращал 0, не проверяя ничего. Хуже: regex в нём лежали
**копиями** из v4.8.3 и уже разошлись с актуальными (`_CMD_SWARN` в коде
давно без внешней необязательной группы). Тест на копии не защищает ничего,
поэтому теперь regex импортируются из `bot_handlers` — проверяется рабочий
код, а не его слепок.

v5.1.0 (Task 2): паттерны переехали в `commands.py` и потеряли якорь «^!» —
теперь матчат нормализованную строку («smute 1d», не «!smute 1d»). Импорт
переведён на `commands.spec_by_name(...)`, а матчинг — через
`commands.resolve(text, None)`, как это делает production-диспетчер.

Что фиксируем:
  • у всех трёх команд target и reason опциональны и распознаются порознь;
  • `!smute` принимает длительность и латиницей, и кириллицей (`1d`, `1д`);
  • голая команда матчится с пустыми группами — обработчик сам решает, что
    делать с отсутствующей длительностью (см. `_parse_duration`).
"""
from __future__ import annotations

import os
import sys
import unittest

from _paths import _P

os.environ.setdefault("BOT_TOKEN", "1:test")
os.environ.setdefault("ADMIN_IDS", "1")

sys.path.insert(0, _P())

import commands  # noqa: E402


def _resolve(text: str):
    return commands.resolve(text, None)


class TestSmuteRegex(unittest.TestCase):
    """`!smute [target] <duration> [reason]`."""

    def _groups(self, text: str) -> dict:
        found = _resolve(text)
        self.assertIsNotNone(found, f"не сматчилось: {text!r}")
        spec, m = found
        self.assertEqual(spec.name, "smute")
        return m.groupdict()

    def test_bare_command_matches_with_empty_groups(self):
        """Голая команда матчится — длительность проверяет обработчик.

        Именно этот случай ронял бота в v4.8.3: regex не матчил, ветка
        уходила в _parse_duration(None) и падала на None.strip().
        """
        self.assertEqual(self._groups("!smute"),
                         {"target": None, "dur": None, "reason": None})

    def test_duration_only(self):
        self.assertEqual(self._groups("!smute 1d"),
                         {"target": None, "dur": "1d", "reason": None})

    def test_duration_and_reason(self):
        self.assertEqual(self._groups("!smute 1d Причина"),
                         {"target": None, "dur": "1d", "reason": "Причина"})

    def test_username_target(self):
        self.assertEqual(self._groups("!smute @user 1d"),
                         {"target": "@user", "dur": "1d", "reason": None})

    def test_numeric_target_with_reason(self):
        self.assertEqual(self._groups("!smute 12345 1d Причина"),
                         {"target": "12345", "dur": "1d", "reason": "Причина"})

    def test_cyrillic_duration(self):
        """Длительность кириллицей: модераторы пишут «1д», не переключая раскладку."""
        self.assertEqual(self._groups("!smute 1д")["dur"], "1д")

    def test_minutes(self):
        self.assertEqual(self._groups("!smute 30m")["dur"], "30m")


class TestSwarnSbanRegex(unittest.TestCase):
    """`!swarn` / `!sban` — `[target] [reason]`, обе части опциональны."""

    def _check(self, cmd_name: str, text: str, target, reason):
        found = _resolve(text)
        self.assertIsNotNone(found, f"не сматчилось: {text!r}")
        spec, m = found
        self.assertEqual(spec.name, cmd_name, f"команда у {text!r}")
        self.assertEqual(m.group("target"), target, f"target у {text!r}")
        self.assertEqual(m.group("reason"), reason, f"reason у {text!r}")

    def test_swarn_variants(self):
        for text, target, reason in [
            ("!swarn", None, None),
            ("!swarn Причина", None, "Причина"),
            ("!swarn @user", "@user", None),
            ("!swarn @user Причина", "@user", "Причина"),
            ("!swarn 12345", "12345", None),
            ("!swarn 12345 Причина", "12345", "Причина"),
        ]:
            with self.subTest(text=text):
                self._check("swarn", text, target, reason)

    def test_sban_variants(self):
        for text, target, reason in [
            ("!sban", None, None),
            ("!sban Причина", None, "Причина"),
            ("!sban @user", "@user", None),
            ("!sban @user Причина", "@user", "Причина"),
            ("!sban 12345", "12345", None),
            ("!sban 12345 Причина", "12345", "Причина"),
        ]:
            with self.subTest(text=text):
                self._check("sban", text, target, reason)

    def test_target_alone_matches(self):
        """Регресс v4.8.3: один только target не матчился, команда молчала."""
        self.assertIsNotNone(_resolve("!swarn @user"))
        self.assertIsNotNone(_resolve("!sban @user"))


class TestCaseInsensitive(unittest.TestCase):

    def test_uppercase_accepted(self):
        """re.IGNORECASE: !SMUTE и !SBan должны матчиться."""
        self.assertIsNotNone(_resolve("!SMUTE 1d"))
        self.assertIsNotNone(_resolve("!SBan @user"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
