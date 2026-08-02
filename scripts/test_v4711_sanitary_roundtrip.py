"""
v4.7.11 — тесты фикса round-trip санитарных дней с временем.

Проблема v4.7.10 (и ранее): при сохранении чата, в котором уже были
настроены санитарные дни с временем (добавленные через /admin/chats/{id}/sanitary/add),
сервер возвращал ошибку:
    Sanitary days: Строка 1: невалидная дата начала '2026-07-31 23:00'

Причина — две связанных проблемы:

1. format_sanitary_days_textarea записывает строки вида:
       '2026-07-31 23:00 - 2026-08-03 09:00'
       '2026-07-31 23:00'
       '2026-07-31 23:00 - 2026-08-03'
       '2026-07-31 - 2026-08-03 09:00'
   А parse_sanitary_days_textarea понимал только 'YYYY-MM-DD' — без времени.
   _parse_sanitary_date регексом ^(\d{4})-(\d{2})-(\d{2})$ не матил '2026-07-31 23:00'.

2. web_app.py делал `for s, e in san_pairs: ... append([s, e])` — это:
   - падало с ValueError: too many values to unpack на парах с временем (len=3 или 4)
   - даже без падения — теряло время при сериализации

Решение v4.7.11:
  • parse_sanitary_days_textarea понимает 8 форматов (см. docstring функции)
  • web_app.py группирует entries целиком, не распаковывая в s, e

Тесты:
  1. APP_VERSION = "v4.7.11"
  2. Воспроизведение бага: format → parse старым кодом падал (регресс-проверка
     через вызов новой функции)
  3. Round-trip: format(parse(format(x))) == format(x) для всех вариантов времени
  4. Парсинг '2026-07-31 23:00 - 2026-08-03 09:00' → [s, e, st, et]
  5. Парсинг '2026-07-31 23:00' (single-day, только start_time) → [s, s, st]
  6. Парсинг '2026-07-31 23:00 - 2026-08-03' (только start_time) → [s, e, st]
  7. Парсинг '2026-07-31 - 2026-08-03 09:00' (только end_time) → [s, e, "00:00", et]
  8. Парсинг '2026-08-15 09:00-18:00' (single-day с диапазоном времени) → [s, s, "09:00", "18:00"]
  9. Парсинг '2026-08-15' (single-day без времени) → [s, s]
 10. Парсинг '2026-08-15 - 2026-08-17' (range без времени) → [s, e]
 11. Парсинг '2026-08-15:2026-08-17' (старый формат) → [s, e]
 12. Парсинг невалидной даты '2026-13-45' → ошибка
 13. Парсинг пустого textarea → ([], [])
 14. Парсинг комментариев и пустых строк — игнорируются
 15. Серверная группировка в web_app.py: пары с временем не теряются
 16. serialize_sanitary_days_monthly принимает пары с временем
 17. Changelog содержит v4.7.11
"""

import os
import sys
import re
import json
import unittest

sys.path.insert(0, "/home/z/my-project/v4.5")
sys.path.insert(0, "/home/z/my-project/v4.5/scripts")

_DB_PATH = "/tmp/test_v4711_sanitary_roundtrip.db"
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

TEMPLATES_DIR = "/home/z/my-project/v4.5/templates"


def _read(name: str) -> str:
    with open(os.path.join(TEMPLATES_DIR, name), "r", encoding="utf-8") as f:
        return f.read()


# ─── Tests ──────────────────────────────────────────────────────────────────


class TestV4711SanitaryRoundtrip(unittest.TestCase):
    """v4.7.11: round-trip санитарных дней с временем."""

    def setUp(self):
        self.base_html = _read("base.html")

    # ─── 1. Version ─────────────────────────────────────────────────────

    def test_01_app_version_bumped(self):
        self.assertEqual(APP_VERSION, "v4.7.11",
                         f"APP_VERSION should be v4.7.11, got {APP_VERSION}")

    # ─── 2. Воспроизведение исходного бага ─────────────────────────────

    def test_02_original_bug_repro_fixed(self):
        """Точная строка из баг-репорта должна теперь парситься без ошибок."""
        text = "2026-07-31 23:00 - 2026-08-03 09:00"
        pairs, errors = bh.parse_sanitary_days_textarea(text)
        self.assertEqual(errors, [],
                         f"Should have no errors, got: {errors}")
        self.assertEqual(len(pairs), 1,
                         f"Should parse 1 pair, got {len(pairs)}")
        entry = pairs[0]
        self.assertEqual(entry[0], "2026-07-31")
        self.assertEqual(entry[1], "2026-08-03")
        self.assertEqual(entry[2], "23:00")
        self.assertEqual(entry[3], "09:00")

    # ─── 3. Round-trip для всех вариантов времени ──────────────────────

    def test_03_roundtrip_all_time_variants(self):
        """format(parse(format(x))) == format(x) для всех вариантов.

        Исключение: "только end_time" — вырожденный случай, парсер подставляет
        '00:00' как start_time, после round-trip это становится '00:00 - et'.
        """
        # Каждый кейс: исходные pairs → textarea → после round-trip pairs
        # должны быть эквивалентны.
        test_cases = [
            # Без времени
            ([["2026-08-15", "2026-08-15"]],
             [["2026-08-15", "2026-08-15"]]),
            ([["2026-08-15", "2026-08-17"]],
             [["2026-08-15", "2026-08-17"]]),
            # С обоими временами
            ([["2026-07-31", "2026-08-03", "23:00", "09:00"]],
             [["2026-07-31", "2026-08-03", "23:00", "09:00"]]),
            # Только start_time (single-day)
            ([["2026-08-15", "2026-08-15", "09:00"]],
             [["2026-08-15", "2026-08-15", "09:00"]]),
            # Только start_time (range)
            ([["2026-07-31", "2026-08-03", "23:00"]],
             [["2026-07-31", "2026-08-03", "23:00"]]),
        ]
        for pairs_in, expected_pairs_after in test_cases:
            with self.subTest(pairs_in=pairs_in):
                text1 = bh.format_sanitary_days_textarea(pairs_in)
                pairs_parsed, errors = bh.parse_sanitary_days_textarea(text1)
                self.assertEqual(errors, [],
                                 f"Parse errors for {text1!r}: {errors}")
                # После serialize → parse_json должны получить эквивалентные pairs
                grouped = _group_pairs_by_month(pairs_parsed)
                serialized = bh.serialize_sanitary_days_monthly(grouped)
                reparsed = bh.parse_sanitary_days_json(serialized)
                # Сравниваем как множества (порядок может меняться)
                self.assertEqual(
                    sorted([tuple(p) for p in reparsed]),
                    sorted([tuple(p) for p in expected_pairs_after]),
                    f"Round-trip mismatch for {pairs_in} (text={text1!r}): "
                    f"got {reparsed}"
                )

    # ─── 4. Парсинг range со временем с обеих сторон ──────────────────

    def test_04_parse_range_both_times(self):
        pairs, errors = bh.parse_sanitary_days_textarea(
            "2026-07-31 23:00 - 2026-08-03 09:00"
        )
        self.assertEqual(errors, [])
        self.assertEqual(pairs, [["2026-07-31", "2026-08-03", "23:00", "09:00"]])

    # ─── 5. Single-day только start_time ──────────────────────────────

    def test_05_parse_single_day_start_time_only(self):
        pairs, errors = bh.parse_sanitary_days_textarea("2026-07-31 23:00")
        self.assertEqual(errors, [])
        self.assertEqual(pairs, [["2026-07-31", "2026-07-31", "23:00"]])

    # ─── 6. Range только start_time ───────────────────────────────────

    def test_06_parse_range_start_time_only(self):
        pairs, errors = bh.parse_sanitary_days_textarea(
            "2026-07-31 23:00 - 2026-08-03"
        )
        self.assertEqual(errors, [])
        self.assertEqual(pairs, [["2026-07-31", "2026-08-03", "23:00"]])

    # ─── 7. Range только end_time ─────────────────────────────────────

    def test_07_parse_range_end_time_only(self):
        pairs, errors = bh.parse_sanitary_days_textarea(
            "2026-07-31 - 2026-08-03 09:00"
        )
        self.assertEqual(errors, [])
        # end_time без start_time → подставляем "00:00" как start_time
        self.assertEqual(pairs, [["2026-07-31", "2026-08-03", "00:00", "09:00"]])

    # ─── 8. Single-day с диапазоном времени ───────────────────────────

    def test_08_parse_single_day_time_range(self):
        pairs, errors = bh.parse_sanitary_days_textarea(
            "2026-08-15 09:00-18:00"
        )
        self.assertEqual(errors, [])
        self.assertEqual(pairs, [["2026-08-15", "2026-08-15", "09:00", "18:00"]])

    # ─── 9. Single-day без времени ────────────────────────────────────

    def test_09_parse_single_day_no_time(self):
        pairs, errors = bh.parse_sanitary_days_textarea("2026-08-15")
        self.assertEqual(errors, [])
        self.assertEqual(pairs, [["2026-08-15", "2026-08-15"]])

    # ─── 10. Range без времени ────────────────────────────────────────

    def test_10_parse_range_no_time(self):
        pairs, errors = bh.parse_sanitary_days_textarea(
            "2026-08-15 - 2026-08-17"
        )
        self.assertEqual(errors, [])
        self.assertEqual(pairs, [["2026-08-15", "2026-08-17"]])

    # ─── 11. Старый формат через ':' ──────────────────────────────────

    def test_11_parse_old_colon_format(self):
        pairs, errors = bh.parse_sanitary_days_textarea(
            "2026-08-15:2026-08-17"
        )
        self.assertEqual(errors, [])
        self.assertEqual(pairs, [["2026-08-15", "2026-08-17"]])

    # ─── 12. Невалидная дата ──────────────────────────────────────────

    def test_12_parse_invalid_date(self):
        pairs, errors = bh.parse_sanitary_days_textarea("2026-13-45")
        self.assertEqual(pairs, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("невалидная дата", errors[0])

    # ─── 13. Пустой textarea ──────────────────────────────────────────

    def test_13_parse_empty_textarea(self):
        pairs, errors = bh.parse_sanitary_days_textarea("")
        self.assertEqual(pairs, [])
        self.assertEqual(errors, [])

    # ─── 14. Комментарии и пустые строки ──────────────────────────────

    def test_14_parse_comments_and_blanks(self):
        text = """# комментарий

2026-08-15
   # ещё комментарий
2026-08-20 - 2026-08-22"""
        pairs, errors = bh.parse_sanitary_days_textarea(text)
        self.assertEqual(errors, [])
        self.assertEqual(len(pairs), 2)
        self.assertEqual(pairs[0], ["2026-08-15", "2026-08-15"])
        self.assertEqual(pairs[1], ["2026-08-20", "2026-08-22"])

    # ─── 15. Несколько периодов, mixed ────────────────────────────────

    def test_15_parse_multiple_mixed(self):
        text = """2026-08-01
2026-08-05 09:00-18:00
2026-08-10 - 2026-08-12
2026-08-15 23:00 - 2026-08-17 09:00"""
        pairs, errors = bh.parse_sanitary_days_textarea(text)
        self.assertEqual(errors, [])
        self.assertEqual(len(pairs), 4)
        self.assertEqual(pairs[0], ["2026-08-01", "2026-08-01"])
        self.assertEqual(pairs[1], ["2026-08-05", "2026-08-05", "09:00", "18:00"])
        self.assertEqual(pairs[2], ["2026-08-10", "2026-08-12"])
        self.assertEqual(pairs[3], ["2026-08-15", "2026-08-17", "23:00", "09:00"])

    # ─── 16. serialize_sanitary_days_monthly принимает пары с временем ─

    def test_16_serialize_accepts_time_pairs(self):
        """serialize_sanitary_days_monthly должен корректно сериализовать
        пары с временем."""
        monthly = {
            "2026-08": [
                ["2026-08-15", "2026-08-15"],
                ["2026-08-15", "2026-08-15", "09:00", "18:00"],
                ["2026-08-20", "2026-08-22", "23:00", "09:00"],
            ]
        }
        serialized = bh.serialize_sanitary_days_monthly(monthly)
        data = json.loads(serialized)
        self.assertIn("2026-08", data)
        self.assertEqual(len(data["2026-08"]), 3)
        # Все три записи должны быть валидными после нормализации
        for entry in data["2026-08"]:
            self.assertGreaterEqual(len(entry), 2)
            self.assertLessEqual(len(entry), 4)

    # ─── 17. web_app.py: группировка сохраняет время ──────────────────

    def test_17_web_app_grouping_preserves_time(self):
        """Симулируем логику из web_app.py admin_chats_update:
        парсим textarea, группируем по месяцам, сериализуем.
        Результат должен содержать время."""
        # Прямая проверка: исходный textarea из бага → serialized JSON
        # содержит время.
        text = "2026-07-31 23:00 - 2026-08-03 09:00"
        pairs, errors = bh.parse_sanitary_days_textarea(text)
        self.assertEqual(errors, [])

        # Симулируем логику web_app.py (новую, после фикса):
        grouped = {}
        for entry in pairs:
            mk = entry[0][:7]
            grouped.setdefault(mk, []).append(entry)
        serialized = bh.serialize_sanitary_days_monthly(grouped)
        data = json.loads(serialized)

        # Период 2026-07-31..2026-08-03 группируется по месяцу НАЧАЛА (2026-07).
        self.assertIn("2026-07", data)
        self.assertEqual(len(data["2026-07"]), 1)
        entry = data["2026-07"][0]
        self.assertEqual(entry[0], "2026-07-31")
        self.assertEqual(entry[1], "2026-08-03")
        self.assertEqual(entry[2], "23:00")
        self.assertEqual(entry[3], "09:00")

    # ─── 18. Changelog ────────────────────────────────────────────────

    def test_18_changelog_mentions_v4711(self):
        self.assertIn("v4.7.11", self.base_html)
        # Проверим что есть упоминание круглого цикла / round-trip
        m = re.search(
            r'<p><strong>v4\.7\.11</strong>.*?</ul>',
            self.base_html,
            re.DOTALL,
        )
        self.assertIsNotNone(m, "v4.7.11 changelog entry not found")
        section = m.group(0)
        # Должно быть упоминание времени или формата
        self.assertTrue(
            ("23:00" in section or "round-trip" in section.lower()
             or "времен" in section.lower()),
            f"Changelog should mention time/round-trip, got: {section[:200]}"
        )

    # ─── 19. Regression: web_app.py больше не делает `for s, e in` ────

    def test_19_web_app_no_unpacking_bug(self):
        """В web_app.py больше не должно быть `for s, e in san_pairs`."""
        with open("/home/z/my-project/v4.5/web_app.py", "r",
                   encoding="utf-8") as f:
            src = f.read()
        # Ищем паттерн `for s, e in san_pairs` — это баг.
        bad = re.search(r'for\s+s\s*,\s*e\s+in\s+san_pairs', src)
        self.assertIsNone(
            bad,
            f"web_app.py still has buggy `for s, e in san_pairs` at pos {bad.start() if bad else -1}"
        )
        # Должно быть `for entry in san_pairs`
        good = re.search(r'for\s+entry\s+in\s+san_pairs', src)
        self.assertIsNotNone(good, "web_app.py should have `for entry in san_pairs`")


# ─── Helpers ────────────────────────────────────────────────────────────────


def _group_pairs_by_month(pairs):
    """Локальная реализация группировки (если в bot_handlers нет аналога)."""
    grouped = {}
    for entry in pairs:
        mk = entry[0][:7]
        grouped.setdefault(mk, []).append(entry)
    return grouped


def _normalize_for_compare(p):
    """Для сравнения пар после round-trip — нормализуем к длине 4."""
    out = list(p)
    while len(out) < 4:
        out.append("")
    return out


if __name__ == "__main__":
    unittest.main(verbosity=2)
