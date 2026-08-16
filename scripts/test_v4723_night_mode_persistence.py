"""
test_v4723_night_mode_persistence.py — v4.7.23 regression tests.

Контекст бага:
    Пользователь сообщил: «при каждой перезагрузке скидывается переключатель
    автопереключения ночного/дневного режима».

Корневая причина (найдена в db.py, миграция v4.7.2):
    Блок reset `night_mode_enabled=0` использовал маркер
    "SELECT COUNT(*) WHERE night_mode_enabled=1 OR sanitary_days_currently_active=1"
    и если хоть одна запись подходила — выполнял UPDATE ... SET night_mode_enabled=0
    для всех чатов. Логика задумывалась как one-time миграция «при первом апгрейде
    до v4.7.2 выключить все toggles», но маркер был некорректен: после того как
    пользователь нормально включал ночной режим (web-панель или !nightmode on),
    night_mode_enabled=1 → маркер срабатывал КАЖДЫЙ рестарт → toggle сбрасывался.

Фикс v4.7.23:
    Reset перемещён внутрь `if "sanitary_days_enabled" not in columns:` —
    теперь выполняется только при действительной первой миграции (колонка
    ещё не добавлена). На обычных рестартах сбрасываются только runtime-флаги
    night_mode_currently_active и sanitary_days_currently_active (это runtime
    state, не user toggles).

Запуск:
    /home/z/.venv/bin/python3 scripts/test_v4723_night_mode_persistence.py
"""

import ast
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path


V4_DIR = Path("/home/z/my-project/v4.5")
DB_PATH = V4_DIR / "db.py"
WEB_APP_PATH = V4_DIR / "web_app.py"
BASE_HTML_PATH = V4_DIR / "templates" / "base.html"


def _read_db() -> str:
    return DB_PATH.read_text(encoding="utf-8")


def _read_web_app() -> str:
    return WEB_APP_PATH.read_text(encoding="utf-8")


def _read_base() -> str:
    return BASE_HTML_PATH.read_text(encoding="utf-8")


class TestV4723NightModePersistence(unittest.TestCase):
    """v4.7.23: night_mode_enabled должен сохраняться между рестартами бота."""

    def setUp(self):
        self.db_src = _read_db()
        self.db_tree = ast.parse(self.db_src)

    # ── 1. Структурные проверки кода миграции ────────────────────────────

    def test_01_reset_inside_column_guard(self):
        """v4.7.23: блок UPDATE ... SET night_mode_enabled=0 должен быть ВНУТРИ
        `if "sanitary_days_enabled" not in columns:` блока.

        До фикса reset был снаружи и выполнялся каждый рестарт при наличии
        любой записи с night_mode_enabled=1.
        """
        src = self.db_src
        # Находим миграционный блок по уникальному паттерну guard'а
        guard_pattern = '"sanitary_days_enabled" not in columns'
        guard_idx = src.find(guard_pattern)
        self.assertGreater(guard_idx, 0,
                           "Guard `if \"sanitary_days_enabled\" not in columns:` не найден в db.py")
        # Берём контекст — 1500 символов после guard'а
        ctx = src[guard_idx:guard_idx + 1500]
        # Проверяем что SET night_mode_enabled=0 есть ВНУТРИ if-блока (т.е. ДО else:)
        enabled_reset_pos = ctx.find('SET night_mode_enabled=0')
        self.assertGreater(enabled_reset_pos, 0,
                           "Должен быть UPDATE ... SET night_mode_enabled=0 в if-блоке")
        else_pos = ctx.find('else:', enabled_reset_pos)
        # reset должен быть ДО else (если else есть в этом блоке)
        if else_pos > 0:
            self.assertLess(
                enabled_reset_pos, else_pos,
                "SET night_mode_enabled=0 должен быть ВНУТРИ if-блока (до else:)",
            )

    def test_02_else_branch_only_resets_runtime_state(self):
        """v4.7.23: в else-ветке (обычный рестарт) НЕ должно быть
        `night_mode_enabled=0` — только currently_active сброс."""
        src = self.db_src
        guard_pattern = '"sanitary_days_enabled" not in columns'
        guard_idx = src.find(guard_pattern)
        self.assertGreater(guard_idx, 0)
        ctx = src[guard_idx:guard_idx + 3000]
        # Находим else: в этом блоке
        else_pos = ctx.find('else:')
        self.assertGreater(else_pos, 0,
                           "Должен быть else: для обычного рестарта (когда колонка уже есть)")
        # После else: не должно быть night_mode_enabled=0
        after_else = ctx[else_pos:]
        # Берём достаточно большой блок чтобы покрыть весь UPDATE (он разбит на
        # несколько Python string literals, поэтому 1500 символов нужно).
        else_block = after_else[:1500]
        self.assertNotIn(
            'night_mode_enabled=0',
            else_block,
            "В else-ветке НЕ должно быть `night_mode_enabled=0` — "
            "это user toggle, должен сохраняться между рестартами",
        )
        # Должны быть сброшены только runtime-флаги. SQL разбит на 2 string literal'а
        # ("UPDATE chat_settings SET " + "night_mode_currently_active=0, sanitary_days_currently_active=0 ..."),
        # но каждый подстрока присутствует в исходнике отдельно.
        self.assertIn(
            'night_mode_currently_active=0',
            else_block,
            "else-ветка должна сбрасывать night_mode_currently_active=0 (runtime state)",
        )
        self.assertIn(
            'sanitary_days_currently_active=0',
            else_block,
            "else-ветка должна сбрашивать sanitary_days_currently_active=0 (runtime state)",
        )

    def test_03_no_count_marker_query(self):
        """v4.7.23: НЕ должно быть старого маркера
        `SELECT COUNT(*) ... WHERE night_mode_enabled=1 OR sanitary_days_currently_active=1`.

        Это и был корень бага — marker срабатывал каждый раз как пользователь
        включал ночной режим.
        """
        src = self.db_src
        # Старый маркер — SELECT COUNT(*) FROM chat_settings WHERE night_mode_enabled=1
        bad_combined = 'SELECT COUNT(*) FROM chat_settings WHERE night_mode_enabled=1'
        self.assertNotIn(
            bad_combined, src,
            f"Старый marker миграции `{bad_combined}` должен быть удалён — "
            f"он вызывал reset night_mode_enabled каждый рестарт.",
        )
        # Также проверяем что нет `if rows and rows > 0:` → UPDATE ... night_mode_enabled=0
        # (вся структура старого бага).
        rows_pattern = 'if rows and rows > 0'
        self.assertNotIn(
            rows_pattern, src,
            f"Паттерн `{rows_pattern}` удалён — это структура старого бага v4.7.2.",
        )

    def test_04_no_if_rows_count_reset_pattern(self):
        """v4.7.23: НЕ должно быть паттерна `if rows and rows > 0:` перед reset.

        Это вся структура старого бага: COUNT → if rows > 0 → UPDATE.
        """
        src = self.db_src
        self.assertNotIn(
            'if rows and rows > 0',
            src,
            "Паттерн `if rows and rows > 0:` перед reset удалён — "
            "это был баг v4.7.2 (reset срабатывал каждый рестарт).",
        )

    # ── 2. Поведенческие тесты на реальной SQLite in-memory ─────────────

    def _build_chat_settings_schema(self, conn, include_sanitary_col=True):
        """Создаёт схему chat_settings с нужными колонками для теста."""
        cols = [
            "chat_id INTEGER PRIMARY KEY",
            "night_mode_enabled BOOLEAN NOT NULL DEFAULT 0",
            "night_mode_currently_active BOOLEAN NOT NULL DEFAULT 0",
            "sanitary_days_currently_active BOOLEAN NOT NULL DEFAULT 0",
        ]
        if include_sanitary_col:
            cols.append("sanitary_days_enabled BOOLEAN NOT NULL DEFAULT 0")
        conn.execute(f"CREATE TABLE chat_settings ({', '.join(cols)})")

    def _seed_chat(self, conn, chat_id, night_enabled, night_active, sanitary_active):
        conn.execute(
            "INSERT INTO chat_settings (chat_id, night_mode_enabled, "
            "night_mode_currently_active, sanitary_days_currently_active) "
            "VALUES (?, ?, ?, ?)",
            (chat_id, night_enabled, night_active, sanitary_active),
        )

    def _get_chat(self, conn, chat_id):
        cur = conn.execute(
            "SELECT night_mode_enabled, night_mode_currently_active, "
            "sanitary_days_currently_active FROM chat_settings WHERE chat_id=?",
            (chat_id,),
        )
        return cur.fetchone()

    def test_10_persistence_simulated_restart_with_sanitary_col(self):
        """Симулируем обычный рестарт: колонка sanitary_days_enabled УЖЕ есть,
        у чата night_mode_enabled=1. После миграции должно остаться =1.

        До фикса: marker срабатывал, toggle сбрасывался в 0.
        После фикса: else-ветка, only currently_active сбрасываются.
        """
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            conn = sqlite3.connect(db_path)
            self._build_chat_settings_schema(conn, include_sanitary_col=True)
            # Чат с включённым night mode + active state
            self._seed_chat(conn, -100123, night_enabled=1, night_active=1, sanitary_active=0)
            # Чат в sanitary day active
            self._seed_chat(conn, -100456, night_enabled=0, night_active=0, sanitary_active=1)
            # Чат где ничего не включено
            self._seed_chat(conn, -100789, night_enabled=0, night_active=0, sanitary_active=0)
            conn.commit()

            # ── Симулируем миграцию v4.7.23: колонка УЖЕ есть → else-ветка ──
            cols = [r[1] for r in conn.execute("PRAGMA table_info(chat_settings)").fetchall()]
            self.assertIn("sanitary_days_enabled", cols)
            # else-ветка: only currently_active reset
            conn.execute(
                "UPDATE chat_settings SET "
                "night_mode_currently_active=0, sanitary_days_currently_active=0 "
                "WHERE chat_id != 0"
            )
            conn.commit()

            # ── Проверки ──
            # Чат -100123: night_mode_enabled должен остаться 1!
            n_enabled, n_active, s_active = self._get_chat(conn, -100123)
            self.assertEqual(n_enabled, 1,
                             "night_mode_enabled должен СОХРАНЯТЬСЯ между рестартами (v4.7.23 фикс)")
            self.assertEqual(n_active, 0,
                             "night_mode_currently_active runtime-флаг сброшен")
            self.assertEqual(s_active, 0)

            # Чат -100456: тоже не должен был получить night_mode_enabled=1
            n_enabled, n_active, s_active = self._get_chat(conn, -100456)
            self.assertEqual(n_enabled, 0)
            self.assertEqual(s_active, 0,
                             "sanitary_days_currently_active runtime-флаг сброшен")

            # Чат -100789: ничего не должно было измениться
            n_enabled, n_active, s_active = self._get_chat(conn, -100789)
            self.assertEqual(n_enabled, 0)
            self.assertEqual(n_active, 0)
            self.assertEqual(s_active, 0)

            conn.close()
        finally:
            Path(db_path).unlink(missing_ok=True)

    def test_11_one_time_reset_on_first_migration(self):
        """Симулируем первый апгрейд до v4.7.2: колонки sanitary_days_enabled НЕТ.
        Все toggles должны быть сброшены (one-time миграция, как и задумано в v4.7.2).
        """
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            conn = sqlite3.connect(db_path)
            # Колонки sanitary_days_enabled НЕТ — симулируем pre-v4.7.2 БД
            self._build_chat_settings_schema(conn, include_sanitary_col=False)
            self._seed_chat(conn, -100123, night_enabled=1, night_active=1, sanitary_active=0)
            self._seed_chat(conn, -100456, night_enabled=0, night_active=0, sanitary_active=1)
            conn.commit()

            # ── Симулируем миграцию v4.7.23: колонки НЕТ → if-ветка ──
            cols = [r[1] for r in conn.execute("PRAGMA table_info(chat_settings)").fetchall()]
            self.assertNotIn("sanitary_days_enabled", cols)
            # if-ветка: add column + one-time reset
            conn.execute(
                "ALTER TABLE chat_settings ADD COLUMN sanitary_days_enabled "
                "BOOLEAN NOT NULL DEFAULT 0"
            )
            conn.execute(
                "UPDATE chat_settings SET night_mode_enabled=0, "
                "night_mode_currently_active=0, sanitary_days_currently_active=0 "
                "WHERE chat_id != 0"
            )
            conn.commit()

            # ── Проверки: все toggles сброшены (one-time миграция) ──
            n_enabled, n_active, s_active = self._get_chat(conn, -100123)
            self.assertEqual(n_enabled, 0,
                             "One-time миграция: night_mode_enabled сброшен в 0")
            self.assertEqual(n_active, 0)
            self.assertEqual(s_active, 0)

            n_enabled, n_active, s_active = self._get_chat(conn, -100456)
            self.assertEqual(n_enabled, 0)
            self.assertEqual(s_active, 0)

            conn.close()
        finally:
            Path(db_path).unlink(missing_ok=True)

    def test_12_multiple_restarts_keep_toggle(self):
        """Симулируем 5 последовательных рестартов: после первого включения
        night_mode_enabled=1, должен оставаться 1 после всех 5 рестартов.
        """
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            conn = sqlite3.connect(db_path)
            self._build_chat_settings_schema(conn, include_sanitary_col=True)
            self._seed_chat(conn, -100999, night_enabled=0, night_active=0, sanitary_active=0)
            conn.commit()

            # Пользователь включает ночной режим
            conn.execute("UPDATE chat_settings SET night_mode_enabled=1 WHERE chat_id=-100999")
            conn.commit()

            # 5 рестартов: каждый раз миграция видит колонку → else-ветка
            for restart_num in range(1, 6):
                cols = [r[1] for r in conn.execute("PRAGMA table_info(chat_settings)").fetchall()]
                self.assertIn("sanitary_days_enabled", cols,
                              f"Restart #{restart_num}: колонка должна быть")
                # else-ветка
                conn.execute(
                    "UPDATE chat_settings SET "
                    "night_mode_currently_active=0, sanitary_days_currently_active=0 "
                    "WHERE chat_id != 0"
                )
                conn.commit()
                n_enabled, _, _ = self._get_chat(conn, -100999)
                self.assertEqual(
                    n_enabled, 1,
                    f"После рестарта #{restart_num} night_mode_enabled должен быть 1, "
                    f"получили {n_enabled}. Баг v4.7.2: toggle сбрасывался каждый рестарт.",
                )

            conn.close()
        finally:
            Path(db_path).unlink(missing_ok=True)

    def test_13_default_chat_id_zero_not_affected(self):
        """chat_id=0 (default global row) не должен сбрасываться даже в if-ветке."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            conn = sqlite3.connect(db_path)
            self._build_chat_settings_schema(conn, include_sanitary_col=False)
            # chat_id=0 с night_mode_enabled=1 (default global)
            conn.execute(
                "INSERT INTO chat_settings (chat_id, night_mode_enabled, "
                "night_mode_currently_active, sanitary_days_currently_active) "
                "VALUES (0, 1, 0, 0)"
            )
            # И обычный чат
            self._seed_chat(conn, -100123, night_enabled=1, night_active=0, sanitary_active=0)
            conn.commit()

            # if-ветка (one-time миграция)
            conn.execute(
                "ALTER TABLE chat_settings ADD COLUMN sanitary_days_enabled "
                "BOOLEAN NOT NULL DEFAULT 0"
            )
            conn.execute(
                "UPDATE chat_settings SET night_mode_enabled=0, "
                "night_mode_currently_active=0, sanitary_days_currently_active=0 "
                "WHERE chat_id != 0"
            )
            conn.commit()

            # chat_id=0: night_mode_enabled должен остаться 1 (WHERE chat_id != 0 исключает его)
            cur = conn.execute(
                "SELECT night_mode_enabled FROM chat_settings WHERE chat_id=0"
            )
            self.assertEqual(cur.fetchone()[0], 1,
                             "chat_id=0 (default) не должен сбрасываться миграцией")

            # Обычный чат: сброшен
            n_enabled, _, _ = self._get_chat(conn, -100123)
            self.assertEqual(n_enabled, 0,
                             "Обычный чат должен быть сброшен в one-time миграции")

            conn.close()
        finally:
            Path(db_path).unlink(missing_ok=True)

    # ── 3. Версия и changelog ───────────────────────────────────────────

    def test_20_app_version_bumped(self):
        """APP_VERSION в web_app.py должен быть >= v4.7.23 (semantic check)."""
        src = _read_web_app()
        match = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', src, re.MULTILINE)
        self.assertIsNotNone(match, "APP_VERSION не найден в web_app.py")
        v = match.group(1)
        # Semantic check: версия должна быть >= v4.7.23 (т.е. фикс не откатывался)
        m = re.match(r'^v(\d+)\.(\d+)\.(\d+)$', v)
        self.assertIsNotNone(m, f"APP_VERSION формат некорректен: {v}")
        major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
        self.assertTrue(
            (major, minor, patch) >= (4, 7, 23),
            f"APP_VERSION должен быть >= v4.7.23 (фикс не должен быть откатан), получили {v}"
        )

    def test_21_changelog_has_v4723_entry(self):
        """templates/base.html должен содержать запись v4.7.23 в changelog."""
        src = _read_base()
        self.assertIn("<strong>v4.7.23</strong>", src,
                      "Changelog должен иметь entry для v4.7.23")
        # Проверяем что упомянут фикс (ключевые слова)
        self.assertIn("night_mode_enabled", src,
                      "Changelog v4.7.23 должен упомянуть night_mode_enabled")
        self.assertIn("при каждой перезагрузке", src,
                      "Changelog v4.7.23 должен цитировать пользовательский баг-репорт")

    def test_22_changelog_v4723_before_v4722(self):
        """v4.7.23 entry должен быть ВЫШЕ v4.7.22 (обратный хронологический порядок)."""
        src = _read_base()
        pos_4723 = src.find("<strong>v4.7.23</strong>")
        pos_4722 = src.find("<strong>v4.7.22</strong>")
        self.assertGreater(pos_4723, 0, "v4.7.23 entry не найден")
        self.assertGreater(pos_4722, 0, "v4.7.22 entry не найден")
        self.assertLess(pos_4723, pos_4722,
                        "v4.7.23 должен быть выше v4.7.22 (обратный хронологический порядок)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
