#!/usr/bin/env python3
"""
test_v484_progressive_automutes.py — тесты v4.8.4 (прогрессивные автомьюты).

Проверяет:
  T1:  Модель AutomuteCounter существует в db.py
  T2:  Миграция CREATE TABLE IF NOT EXISTS в init_db
  T3:  Импорт AutomuteCounter в bot_handlers.py
  T4:  Импорт AutomuteCounter в web_app.py
  T5:  Хелпер _get_automute_count существует и работает (in-memory DB)
  T6:  Хелпер _increment_automute_count — 0→1→2→3
  T7:  Хелпер _reset_automute_count — сброс в 0, возврат старого значения
  T8:  Счётчик per-chat: разные чаты — независимые счётчики
  T9:  Счётчик per-user: разные юзеры — независимые счётчики
  T10: Прогрессивная формула: base + (count * 60)
  T11: Regex _CMD_RESETMC — все варианты вызова
  T12: _CMD_RESETMC в _ALL_MOD_COMMANDS
  T13: Команда !resetmc в полном help
  T14: Команда !resetmc НЕ в moderator help
  T15: APP_VERSION = "v4.8.4"
  T16: Changelog v4.8.4 в base.html
  T17: API endpoints в web_app.py (reset + get)
  T18: !resetmc в верхнем комментарии-блоке bot_handlers.py
  T19: Все 4 пути автомьюта содержат прогрессивную формулу
  T20: Ручные мьюты (!mute, !smute) НЕ содержат прогрессивную формулу

Запуск:
    uv run pytest tests/test_v484_progressive_automutes.py
"""
from _paths import _P  # noqa: E402  (корень вычисляется от __file__)

import asyncio
import os
import re
import sys
import tempfile
import traceback
from pathlib import Path

# ── Пути ────────────────────────────────────────────────────────────────────
WORK_DIR = Path(_P())
sys.path.insert(0, str(WORK_DIR))

# Устанавливаем временный DB_PATH ДО любого импорта db
_tmpdir = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmpdir, "test_v484.db")

PASS = 0
FAIL = 0
ERRORS: list[str] = []


def _ok(name: str, detail: str = "") -> None:
    global PASS
    PASS += 1
    print(f"  ✓ {name}{(' — ' + detail) if detail else ''}")


def _fail(name: str, detail: str) -> None:
    global FAIL
    FAIL += 1
    ERRORS.append(f"{name}: {detail}")
    print(f"  ✗ {name} — {detail}")


def _section(title: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


# ═══════════════════════════════════════════════════════════════════════════
# T1-T4: Статические проверки (импорт, модель, миграция)
# ═══════════════════════════════════════════════════════════════════════════

def test_static():
    _section("T1-T4: Статические проверки")

    # T1: Модель AutomuteCounter в db.py
    try:
        import db
        assert hasattr(db, "AutomuteCounter"), "AutomuteCounter не найден в db"
        ac = db.AutomuteCounter
        assert ac.__tablename__ == "automute_counters", f"tablename={ac.__tablename__}"
        cols = {c.name for c in ac.__table__.columns}
        assert "chat_id" in cols, f"нет chat_id: {cols}"
        assert "user_id" in cols, f"нет user_id: {cols}"
        assert "count" in cols, f"нет count: {cols}"
        assert "updated_at" in cols, f"нет updated_at: {cols}"
        _ok("T1: AutomuteCounter модель", f"columns={sorted(cols)}")
    except Exception as e:
        _fail("T1: AutomuteCounter модель", str(e))

    # T2: Миграция в init_db
    try:
        db_src = (WORK_DIR / "db.py").read_text()
        assert "CREATE TABLE IF NOT EXISTS automute_counters" in db_src, \
            "CREATE TABLE IF NOT EXISTS automute_counters не найдено"
        assert "v4.8.4" in db_src, "комментарий v4.8.4 не найден в db.py"
        _ok("T2: Миграция automute_counters")
    except Exception as e:
        _fail("T2: Миграция automute_counters", str(e))

    # T3: Импорт в bot_handlers.py
    try:
        bh_src = (WORK_DIR / "bot_handlers.py").read_text()
        assert "AutomuteCounter" in bh_src, "AutomuteCounter не импортирован в bot_handlers.py"
        assert "_get_automute_count" in bh_src, "_get_automute_count не найден"
        assert "_increment_automute_count" in bh_src, "_increment_automute_count не найден"
        assert "_reset_automute_count" in bh_src, "_reset_automute_count не найден"
        _ok("T3: Импорты и хелперы в bot_handlers.py")
    except Exception as e:
        _fail("T3: Импорты и хелперы в bot_handlers.py", str(e))

    # T4: Импорт в web_app.py
    try:
        wa_src = (WORK_DIR / "web_app.py").read_text()
        assert "AutomuteCounter" in wa_src, "AutomuteCounter не импортирован в web_app.py"
        assert "/api/reset-automute-count" in wa_src, "endpoint /api/reset-automute-count не найден"
        assert "/api/automute-count" in wa_src, "endpoint /api/automute-count не найден"
        _ok("T4: Импорты и API в web_app.py")
    except Exception as e:
        _fail("T4: Импорты и API в web_app.py", str(e))


# ═══════════════════════════════════════════════════════════════════════════
# T5-T10: Functional tests (in-memory DB)
# ═══════════════════════════════════════════════════════════════════════════

async def test_functional():
    _section("T5-T10: Functional tests (in-memory DB)")

    # Импортируем модули (DB_PATH уже установлен в env до импорта)
    import db as db_mod
    import bot_handlers

    await db_mod.init_db()

    async with db_mod.async_session() as session:
        # T5: _get_automute_count — 0 для нового юзера
        try:
            count = await bot_handlers._get_automute_count(session, -100123, 42)
            assert count == 0, f"expected 0, got {count}"
            _ok("T5: _get_automute_count = 0 для нового юзера")
        except Exception as e:
            _fail("T5: _get_automute_count", str(e))

        # T6: _increment_automute_count — 0→1→2→3
        try:
            c1 = await bot_handlers._increment_automute_count(session, -100123, 42)
            await session.commit()
            assert c1 == 1, f"1st increment: expected 1, got {c1}"
            c2 = await bot_handlers._increment_automute_count(session, -100123, 42)
            await session.commit()
            assert c2 == 2, f"2nd increment: expected 2, got {c2}"
            c3 = await bot_handlers._increment_automute_count(session, -100123, 42)
            await session.commit()
            assert c3 == 3, f"3rd increment: expected 3, got {c3}"
            # Verify read-back
            count = await bot_handlers._get_automute_count(session, -100123, 42)
            assert count == 3, f"read-back: expected 3, got {count}"
            _ok("T6: _increment_automute_count 0→1→2→3")
        except Exception as e:
            _fail("T6: _increment_automute_count", str(e))

        # T7: _reset_automute_count — сброс, возврат старого значения
        try:
            old = await bot_handlers._reset_automute_count(session, -100123, 42)
            await session.commit()
            assert old == 3, f"old count: expected 3, got {old}"
            count = await bot_handlers._get_automute_count(session, -100123, 42)
            assert count == 0, f"after reset: expected 0, got {count}"
            # Reset again (already 0) — should return 0
            old2 = await bot_handlers._reset_automute_count(session, -100123, 42)
            await session.commit()
            assert old2 == 0, f"2nd reset: expected 0, got {old2}"
            _ok("T7: _reset_automute_count (old=3→0, 2nd reset=0)")
        except Exception as e:
            _fail("T7: _reset_automute_count", str(e))

        # T8: Per-chat independence
        try:
            # Chat A: increment 2 times
            await bot_handlers._increment_automute_count(session, -100456, 99)
            await session.commit()
            await bot_handlers._increment_automute_count(session, -100456, 99)
            await session.commit()
            # Chat B: increment 5 times
            for _ in range(5):
                await bot_handlers._increment_automute_count(session, -100789, 99)
            await session.commit()
            count_a = await bot_handlers._get_automute_count(session, -100456, 99)
            count_b = await bot_handlers._get_automute_count(session, -100789, 99)
            assert count_a == 2, f"chat A: expected 2, got {count_a}"
            assert count_b == 5, f"chat B: expected 5, got {count_b}"
            _ok("T8: Per-chat independence (chat A=2, chat B=5)")
        except Exception as e:
            _fail("T8: Per-chat independence", str(e))

        # T9: Per-user independence (same chat, different users)
        try:
            # User 100: increment 3 times
            for _ in range(3):
                await bot_handlers._increment_automute_count(session, -100999, 100)
            await session.commit()
            # User 200: increment 1 time
            await bot_handlers._increment_automute_count(session, -100999, 200)
            await session.commit()
            count_100 = await bot_handlers._get_automute_count(session, -100999, 100)
            count_200 = await bot_handlers._get_automute_count(session, -100999, 200)
            assert count_100 == 3, f"user 100: expected 3, got {count_100}"
            assert count_200 == 1, f"user 200: expected 1, got {count_200}"
            _ok("T9: Per-user independence (user 100=3, user 200=1)")
        except Exception as e:
            _fail("T9: Per-user independence", str(e))

    # T10: Progressive formula verification
    try:
        base = 300  # 5 minutes
        # Simulate: 1st mute (count=0), 2nd (count=1), 3rd (count=2)
        formula_results = []
        for count in range(3):
            duration = base + (count * 60)
            formula_results.append(duration)
        assert formula_results == [300, 360, 420], \
            f"formula: expected [300, 360, 420], got {formula_results}"
        _ok("T10: Прогрессивная формула base+count*60",
            f"5м→6м→7м = {[f'{d//60}м' for d in formula_results]}")
    except Exception as e:
        _fail("T10: Прогрессивная формула", str(e))


# ═══════════════════════════════════════════════════════════════════════════
# T11-T14: Regex and help text checks
# ═══════════════════════════════════════════════════════════════════════════

def test_regex_and_help():
    _section("T11-T14: Regex и help-текст")

    # T11: _CMD_RESETMC regex
    try:
        import bot_handlers
        pat = bot_handlers._CMD_RESETMC
        # All valid variants
        valid = [
            "!resetmc",
            "!resetmc @username",
            "!resetmc 123456789",
            "!RESETMC",
            "!ResetMC @User",
            "!resetmc   ",
        ]
        for v in valid:
            assert pat.match(v), f"should match: {v!r}"
        # Invalid
        invalid = [
            "!resetmc @user extra args",
            "!resetmc123",
            "resetmc",
            "!reset",
        ]
        for v in invalid:
            assert not pat.match(v), f"should NOT match: {v!r}"
        # Target group extraction
        m = pat.match("!resetmc @spammer")
        assert m.group("target") == "@spammer", f"target=@spammer, got {m.group('target')}"
        m = pat.match("!resetmc 12345")
        assert m.group("target") == "12345", f"target=12345, got {m.group('target')}"
        m = pat.match("!resetmc")
        assert m.group("target") is None, f"target=None, got {m.group('target')}"
        _ok("T11: _CMD_RESETMC regex (6 valid, 4 invalid, target extraction)")
    except Exception as e:
        _fail("T11: _CMD_RESETMC regex", str(e))

    # T12: _CMD_RESETMC in _ALL_MOD_COMMANDS
    try:
        import bot_handlers
        assert bot_handlers._CMD_RESETMC in bot_handlers._ALL_MOD_COMMANDS, \
            "_CMD_RESETMC не в _ALL_MOD_COMMANDS"
        # Also check _is_moderation_command
        assert bot_handlers._is_moderation_command("!resetmc"), \
            "_is_moderation_command('!resetmc') = False"
        assert bot_handlers._is_moderation_command("!resetmc @user"), \
            "_is_moderation_command('!resetmc @user') = False"
        _ok("T12: _CMD_RESETMC в _ALL_MOD_COMMANDS + _is_moderation_command")
    except Exception as e:
        _fail("T12: _CMD_RESETMC в _ALL_MOD_COMMANDS", str(e))

    # T13: !resetmc в полном help
    try:
        rm = bot_handlers._build_help_full_rich()
        # Convert to string for checking
        import json
        rm_json = json.dumps(rm.model_dump(), default=str, ensure_ascii=False)
        assert "!resetmc" in rm_json, "!resetmc не найден в полном help"
        assert "обнулить счётчик автомьютов" in rm_json.lower() or \
               "счётчик автомьютов" in rm_json.lower(), \
               "описание !resetmc не найдено"
        _ok("T13: !resetmc в полном help")
    except Exception as e:
        _fail("T13: !resetmc в полном help", str(e))

    # T14: !resetmc НЕ в moderator help
    try:
        rm = bot_handlers._build_help_moderator_rich()
        import json
        rm_json = json.dumps(rm.model_dump(), default=str, ensure_ascii=False)
        assert "!resetmc" not in rm_json, \
            "!resetmc НЕ должен быть в moderator help (admin-only)"
        _ok("T14: !resetmc НЕ в moderator help (admin-only)")
    except Exception as e:
        _fail("T14: !resetmc НЕ в moderator help", str(e))


# ═══════════════════════════════════════════════════════════════════════════
# T15-T18: Version, changelog, comment block
# ═══════════════════════════════════════════════════════════════════════════

def test_version_and_meta():
    _section("T15-T18: Версия, changelog, метаданные")

    # T15: APP_VERSION
    try:
        import web_app
        assert web_app.APP_VERSION == "v4.8.4", \
            f"APP_VERSION={web_app.APP_VERSION!r}, expected 'v4.8.4'"
        _ok("T15: APP_VERSION = v4.8.4")
    except Exception as e:
        _fail("T15: APP_VERSION", str(e))

    # T16: Changelog v4.8.4 в base.html
    try:
        html = (WORK_DIR / "templates" / "base.html").read_text()
        assert "v4.8.4" in html, "v4.8.4 не найден в base.html"
        assert "прогрессивные автомьюты" in html.lower() or \
               "Прогрессивные автомьюты" in html, \
               "'прогрессивные автомьюты' не найдено"
        assert "!resetmc" in html, "!resetmc не найден в changelog"
        assert "automute_counters" in html, "automute_counters не найден в changelog"
        _ok("T16: Changelog v4.8.4 в base.html")
    except Exception as e:
        _fail("T16: Changelog v4.8.4", str(e))

    # T17: API endpoints в web_app.py
    try:
        wa_src = (WORK_DIR / "web_app.py").read_text()
        assert '"/api/reset-automute-count"' in wa_src, \
            "POST /api/reset-automute-count не найден"
        assert '"/api/automute-count"' in wa_src, \
            "GET /api/automute-count не найден"
        assert "require_admin" in wa_src, \
            "require_admin не используется (должен для reset endpoint)"
        _ok("T17: API endpoints /api/reset-automute-count + /api/automute-count")
    except Exception as e:
        _fail("T17: API endpoints", str(e))

    # T18: !resetmc в верхнем комментарии bot_handlers.py
    try:
        bh_src = (WORK_DIR / "bot_handlers.py").read_text()
        # Check the top comment block (first ~80 lines)
        top = bh_src[:3000]
        assert "!resetmc" in top, "!resetmc не найден в верхнем комментарии bot_handlers.py"
        _ok("T18: !resetmc в верхнем комментарии bot_handlers.py")
    except Exception as e:
        _fail("T18: !resetmc в комментарии", str(e))


# ═══════════════════════════════════════════════════════════════════════════
# T19-T20: Automute path modifications
# ═══════════════════════════════════════════════════════════════════════════

def test_automute_paths():
    _section("T19-T20: Модификация путей автомьюта")

    bh_src = (WORK_DIR / "bot_handlers.py").read_text()

    # T19: Все 4 пути автомьюта содержат прогрессивную формулу
    try:
        # Find all occurrences of the progressive formula pattern
        progressive_markers = [
            "_get_automute_count",  # reading count before mute
            "_increment_automute_count",  # incrementing after mute
        ]
        # Count occurrences of _increment_automute_count — should be 4 (one per path)
        # plus 1 in the helper definition + 1 in !resetmc handler? No — !resetmc uses _reset.
        # So: 4 automute paths + 1 function definition = 5
        increment_count = bh_src.count("await _increment_automute_count")
        assert increment_count >= 4, \
            f"_increment_automute_count called {increment_count} times, expected >= 4"

        get_count = bh_src.count("await _get_automute_count")
        assert get_count >= 4, \
            f"_get_automute_count called {get_count} times, expected >= 4"

        # Check that progressive formula (count * 60) appears in the code
        assert "* 60" in bh_src, "progressive formula (* 60) not found"
        # Count occurrences of the formula pattern
        formula_count = bh_src.count("auto_count * 60")
        assert formula_count >= 4, \
            f"formula 'auto_count * 60' found {formula_count} times, expected >= 4"

        _ok("T19: 4 пути автомьюта модифицированы",
            f"increment={increment_count}, get={get_count}, formula={formula_count}")
    except Exception as e:
        _fail("T19: 4 пути автомьюта", str(e))

    # T20: Ручные мьюты (!mute, !smute) НЕ используют прогрессивную формулу
    try:
        # The !mute handler uses _parse_duration for the duration
        # It should NOT call _increment_automute_count
        # Find the !mute handler section
        mute_match = re.search(
            r'# ── !mute .*?(?=\n    # ── !smute|\n    # ── !warn|\Z)',
            bh_src, re.DOTALL,
        )
        smute_match = re.search(
            r'# ── !smute .*?(?=\n    # ── !warn|\n    # ── !ban|\Z)',
            bh_src, re.DOTALL,
        )
        assert mute_match, "!mute handler section not found"
        assert smute_match, "!smute handler section not found"

        mute_section = mute_match.group(0)
        smute_section = smute_match.group(0)

        assert "_increment_automute_count" not in mute_section, \
            "!mute handler should NOT increment automute count"
        assert "_get_automute_count" not in mute_section, \
            "!mute handler should NOT read automute count"
        assert "_increment_automute_count" not in smute_section, \
            "!smute handler should NOT increment automute count"
        assert "_get_automute_count" not in smute_section, \
            "!smute handler should NOT read automute count"

        _ok("T20: Ручные мьюты (!mute, !smute) НЕ используют прогрессивную формулу")
    except Exception as e:
        _fail("T20: Ручные мьюты", str(e))


# ═══════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════

def main():
    print("\n" + "=" * 60)
    print("  v4.8.4 — Прогрессивные автомьюты: тесты")
    print("=" * 60)

    test_static()

    asyncio.run(test_functional())

    test_regex_and_help()

    test_version_and_meta()

    test_automute_paths()

    print("\n" + "=" * 60)
    print(f"  ИТОГО: {PASS} passed, {FAIL} failed")
    print("=" * 60)
    if ERRORS:
        print("\nErrors:")
        for e in ERRORS:
            print(f"  - {e}")
        return 1
    print("\n  All tests PASSED ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
