#!/usr/bin/env python3
"""
test_v560_automute_kinds.py — тесты v5.6.0 (виды автомьютов + decay счётчика).

Что чинится и что добавляется:

  БАГ. Счётчик прогрессивных автомьютов (v4.8.4) был один на
  ``(chat_id, user_id)``, а инкрементировали его все четыре источника
  автомьюта. Автомьют за спам ботами удлинял автомьют за варны и наоборот.
  Теперь у счётчика есть ``kind`` (warns | via_bot | sticker | content),
  и виды не пересекаются.

  ФИЧА. ``chat_settings.automute_decay_days`` — ступенчатый decay: за
  каждые N дней без автомьюта счётчик уменьшается на 1. 0 = отключено
  (счётчик копится вечно, поведение до v5.6.0).

Проверяет:
  T1:  Модель AutomuteCounter: колонка kind, kind в PRIMARY KEY
  T2:  db.AUTOMUTE_KINDS — реестр видов
  T3:  chat_settings.automute_decay_days в модели и в миграции init_db
  T4:  Alembic-ревизия v5.6.0 (down_revision = f2b3c4d5e6f7)
  T5:  Изоляция видов: via_bot не влияет на warns
  T6:  Изоляция сохраняется per-chat и per-user
  T7:  decay=0 — счётчик не тает (старое поведение)
  T8:  decay=7, прошло 21 день, count=5 → effective 2 (ступенчато, -1/7дн)
  T9:  decay не уводит счётчик ниже нуля
  T10: Инкремент после decay идёт от «подтаявшего» значения (2 → 3)
  T11: _reset_automute_count(kind=...) чистит только свой вид
  T12: _reset_all_automute_counts чистит все виды, возвращает разбивку
  T13: Легаси-миграция: строки старой таблицы уезжают в kind='warns'
  T14: Четыре пути автомьюта передают каждый свой kind
  T15: Веб-панель: поле automute_decay_days в форме и в шаблоне
  T16: API: /api/automute-count отдаёт разбивку counts по видам
  T17: APP_VERSION >= v5.6.0 и changelog в base.html

Запуск:
    uv run python tools/run_tests.py -k v560_automute
"""
from _paths import _P  # noqa: E402  (корень вычисляется от __file__)

import asyncio
import os
import sqlite3
import sys
import tempfile
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── Пути ────────────────────────────────────────────────────────────────────
WORK_DIR = Path(_P())
sys.path.insert(0, str(WORK_DIR))

# Устанавливаем временный DB_PATH ДО любого импорта db
_tmpdir = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmpdir, "test_v560.db")

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
    raise AssertionError(f"{name} — {detail}")


def _section(title: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


# ═══════════════════════════════════════════════════════════════════════════
# T1-T4: Статика — модель, реестр видов, настройка, Alembic
# ═══════════════════════════════════════════════════════════════════════════

def test_static():
    _section("T1-T4: Модель, реестр видов, настройка, Alembic")

    # T1: kind в модели и в первичном ключе
    try:
        import db
        cols = {c.name for c in db.AutomuteCounter.__table__.columns}
        assert "kind" in cols, f"нет колонки kind: {sorted(cols)}"
        pk = {c.name for c in db.AutomuteCounter.__table__.primary_key.columns}
        assert pk == {"chat_id", "user_id", "kind"}, f"PK={sorted(pk)}"
        _ok("T1: AutomuteCounter.kind в PK", f"PK={sorted(pk)}")
    except Exception as e:
        _fail("T1: AutomuteCounter.kind", str(e))

    # T2: реестр видов
    try:
        import db
        kinds = set(db.AUTOMUTE_KINDS)
        assert kinds == {"warns", "via_bot", "sticker", "content"}, \
            f"AUTOMUTE_KINDS={sorted(kinds)}"
        _ok("T2: db.AUTOMUTE_KINDS", f"{sorted(kinds)}")
    except Exception as e:
        _fail("T2: db.AUTOMUTE_KINDS", str(e))

    # T3: настройка decay
    try:
        import db
        cs_cols = {c.name for c in db.ChatSettings.__table__.columns}
        assert "automute_decay_days" in cs_cols, \
            "нет chat_settings.automute_decay_days"
        db_src = (WORK_DIR / "db.py").read_text()
        assert "automute_decay_days" in db_src, "колонка не упомянута в db.py"
        # Легаси-путь (DB_USE_LEGACY_MIGRATIONS=1 / fallback) обязан уметь
        # добавить колонку в существующую БД — иначе ORM-SELECT падает.
        assert "ADD COLUMN automute_decay_days" in db_src or \
               '("automute_decay_days"' in db_src, \
               "нет идемпотентной миграции automute_decay_days в init_db"
        _ok("T3: chat_settings.automute_decay_days + миграция")
    except Exception as e:
        _fail("T3: automute_decay_days", str(e))

    # T4: Alembic-ревизия
    try:
        versions = WORK_DIR / "migrations" / "versions"
        rev_files = [
            p for p in versions.glob("*.py")
            if "automute" in p.read_text()
        ]
        new_rev = [p for p in rev_files if "f2b3c4d5e6f7" in p.read_text()
                   and p.name != "2334dcf313d1_v4_8_9_initial_schema.py"]
        assert new_rev, "нет ревизии с down_revision = f2b3c4d5e6f7"
        src = new_rev[0].read_text()
        assert "kind" in src, "ревизия не трогает kind"
        assert "automute_decay_days" in src, "ревизия не добавляет decay-колонку"
        _ok("T4: Alembic-ревизия v5.6.0", new_rev[0].name)
    except Exception as e:
        _fail("T4: Alembic-ревизия", str(e))


# ═══════════════════════════════════════════════════════════════════════════
# T5-T12: Поведение счётчиков (in-memory DB)
# ═══════════════════════════════════════════════════════════════════════════

async def _set_updated_at(session, chat_id, user_id, kind, dt):
    """Отматывает updated_at строки счётчика назад (для проверки decay)."""
    from sqlalchemy import update

    import db as db_mod
    await session.execute(
        update(db_mod.AutomuteCounter)
        .where(
            db_mod.AutomuteCounter.chat_id == chat_id,
            db_mod.AutomuteCounter.user_id == user_id,
            db_mod.AutomuteCounter.kind == kind,
        )
        .values(updated_at=dt)
    )
    await session.commit()


async def test_functional():
    _section("T5-T12: Поведение счётчиков")

    import bot_handlers
    import db as db_mod

    await db_mod.init_db()

    # T5: изоляция видов — главный баг v5.6.0
    async with db_mod.async_session() as session:
        try:
            for _ in range(3):
                await bot_handlers._increment_automute_count(
                    session, -100123, 42, "via_bot")
            await session.commit()
            via = await bot_handlers._get_automute_count(
                session, -100123, 42, "via_bot")
            warns = await bot_handlers._get_automute_count(
                session, -100123, 42, "warns")
            assert via == 3, f"via_bot: ожидалось 3, получено {via}"
            assert warns == 0, \
                f"warns не должен видеть автомьюты за спам ботами, получено {warns}"
            _ok("T5: Изоляция видов (via_bot=3, warns=0)")
        except Exception as e:
            _fail("T5: Изоляция видов", str(e))

        # T6: изоляция сохраняется per-chat / per-user
        try:
            await bot_handlers._increment_automute_count(
                session, -100123, 42, "warns")
            await bot_handlers._increment_automute_count(
                session, -100999, 42, "via_bot")
            await bot_handlers._increment_automute_count(
                session, -100123, 77, "via_bot")
            await session.commit()
            assert await bot_handlers._get_automute_count(
                session, -100123, 42, "warns") == 1
            assert await bot_handlers._get_automute_count(
                session, -100123, 42, "via_bot") == 3
            assert await bot_handlers._get_automute_count(
                session, -100999, 42, "via_bot") == 1
            assert await bot_handlers._get_automute_count(
                session, -100123, 77, "via_bot") == 1
            _ok("T6: Изоляция per-chat/per-user сохранена")
        except Exception as e:
            _fail("T6: Изоляция per-chat/per-user", str(e))

        # T7: decay=0 — счётчик не тает
        try:
            cs = await bot_handlers._get_chat_settings(session, -100500)
            cs.automute_decay_days = 0
            for _ in range(5):
                await bot_handlers._increment_automute_count(
                    session, -100500, 42, "via_bot")
            await session.commit()
            await _set_updated_at(
                session, -100500, 42, "via_bot",
                datetime.now(timezone.utc) - timedelta(days=365),
            )
            got = await bot_handlers._get_automute_count(
                session, -100500, 42, "via_bot")
            assert got == 5, f"decay=0: ожидалось 5, получено {got}"
            _ok("T7: decay=0 — счётчик не тает (5 через год)")
        except Exception as e:
            _fail("T7: decay=0", str(e))

        # T8: ступенчатый decay -1 за каждые N дней
        try:
            cs = await bot_handlers._get_chat_settings(session, -100500)
            cs.automute_decay_days = 7
            await session.commit()
            await _set_updated_at(
                session, -100500, 42, "via_bot",
                datetime.now(timezone.utc) - timedelta(days=21, hours=1),
            )
            got = await bot_handlers._get_automute_count(
                session, -100500, 42, "via_bot")
            assert got == 2, f"5 - 21//7: ожидалось 2, получено {got}"
            # Неполный шаг не считается: 6 дней при decay=7 — ничего не тает.
            await _set_updated_at(
                session, -100500, 42, "via_bot",
                datetime.now(timezone.utc) - timedelta(days=6),
            )
            got6 = await bot_handlers._get_automute_count(
                session, -100500, 42, "via_bot")
            assert got6 == 5, f"6 дней при decay=7: ожидалось 5, получено {got6}"
            _ok("T8: Ступенчатый decay (21д/7 → -3; 6д → без изменений)")
        except Exception as e:
            _fail("T8: Ступенчатый decay", str(e))

        # T9: decay не уходит в минус
        try:
            await _set_updated_at(
                session, -100500, 42, "via_bot",
                datetime.now(timezone.utc) - timedelta(days=700),
            )
            got = await bot_handlers._get_automute_count(
                session, -100500, 42, "via_bot")
            assert got == 0, f"ожидалось 0, получено {got}"
            _ok("T9: decay не уводит счётчик ниже нуля")
        except Exception as e:
            _fail("T9: decay ниже нуля", str(e))

        # T10: инкремент после decay идёт от подтаявшего значения
        try:
            await _set_updated_at(
                session, -100500, 42, "via_bot",
                datetime.now(timezone.utc) - timedelta(days=21, hours=1),
            )
            new = await bot_handlers._increment_automute_count(
                session, -100500, 42, "via_bot")
            await session.commit()
            assert new == 3, f"2 + 1: ожидалось 3, получено {new}"
            # updated_at переписан на «сейчас» — счётчик больше не тает.
            again = await bot_handlers._get_automute_count(
                session, -100500, 42, "via_bot")
            assert again == 3, f"после инкремента: ожидалось 3, получено {again}"
            _ok("T10: Инкремент от подтаявшего значения (2 → 3)")
        except Exception as e:
            _fail("T10: Инкремент после decay", str(e))

        # T11: reset одного вида
        try:
            old = await bot_handlers._reset_automute_count(
                session, -100123, 42, "via_bot")
            await session.commit()
            assert old == 3, f"старое значение: ожидалось 3, получено {old}"
            assert await bot_handlers._get_automute_count(
                session, -100123, 42, "via_bot") == 0
            assert await bot_handlers._get_automute_count(
                session, -100123, 42, "warns") == 1, \
                "reset via_bot не должен трогать warns"
            _ok("T11: reset одного вида не трогает остальные")
        except Exception as e:
            _fail("T11: reset одного вида", str(e))

        # T12: reset всех видов + разбивка
        try:
            for _ in range(2):
                await bot_handlers._increment_automute_count(
                    session, -100123, 42, "sticker")
            await session.commit()
            old_map = await bot_handlers._reset_all_automute_counts(
                session, -100123, 42)
            await session.commit()
            assert isinstance(old_map, dict), f"ожидался dict, получено {type(old_map)}"
            assert old_map.get("warns") == 1, f"warns: {old_map}"
            assert old_map.get("sticker") == 2, f"sticker: {old_map}"
            for kind in db_mod.AUTOMUTE_KINDS:
                assert await bot_handlers._get_automute_count(
                    session, -100123, 42, kind) == 0, f"{kind} не обнулён"
            _ok("T12: _reset_all_automute_counts", f"было {old_map}")
        except Exception as e:
            _fail("T12: reset всех видов", str(e))


# ═══════════════════════════════════════════════════════════════════════════
# T13: Легаси-миграция старой таблицы
# ═══════════════════════════════════════════════════════════════════════════

def test_legacy_migration():
    _section("T13: Легаси-миграция automute_counters")

    try:
        legacy_path = os.path.join(_tmpdir, "legacy_v484.db")
        conn = sqlite3.connect(legacy_path)
        conn.execute("""
            CREATE TABLE automute_counters (
                chat_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (chat_id, user_id)
            )
        """)
        conn.execute(
            "INSERT INTO automute_counters (chat_id, user_id, count, updated_at) "
            "VALUES (-100777, 555, 4, '2026-01-01 00:00:00')"
        )
        conn.commit()
        conn.close()

        # init_db в отдельном процессе: DB_PATH фиксируется на первом импорте db.
        import subprocess
        code = (
            "import asyncio, os, sys; "
            f"sys.path.insert(0, {str(WORK_DIR)!r}); "
            "import db; "
            "asyncio.run(db.init_db())"
        )
        env = dict(os.environ, DB_PATH=legacy_path)
        res = subprocess.run(
            [sys.executable, "-c", code], env=env,
            capture_output=True, text=True, timeout=120,
        )
        assert res.returncode == 0, f"init_db упал: {res.stderr[-800:]}"

        conn = sqlite3.connect(legacy_path)
        cols = [r[1] for r in conn.execute(
            "PRAGMA table_info(automute_counters)").fetchall()]
        assert "kind" in cols, f"kind не появился: {cols}"
        rows = conn.execute(
            "SELECT chat_id, user_id, kind, count FROM automute_counters"
        ).fetchall()
        conn.close()
        assert rows == [(-100777, 555, "warns", 4)], f"строки: {rows}"
        _ok("T13: Легаси-строки уехали в kind='warns'", f"{rows}")
    except Exception as e:
        _fail("T13: Легаси-миграция", str(e))


# ═══════════════════════════════════════════════════════════════════════════
# T14-T17: Проводка — пути автомьюта, веб-панель, API, версия
# ═══════════════════════════════════════════════════════════════════════════

def test_wiring():
    _section("T14-T17: Пути автомьюта, веб-панель, API, версия")

    bh_src = (WORK_DIR / "bot_handlers.py").read_text()

    # T14: каждый из четырёх путей передаёт свой kind
    try:
        for kind in ("warns", "via_bot", "sticker", "content"):
            assert f'"{kind}"' in bh_src, f'kind "{kind}" не встречается'
        # Ни один вызов не остался без kind: все _get/_increment принимают 4 арг.
        import re as _re
        bare = _re.findall(
            r"_(?:get|increment)_automute_count\(\s*session,\s*[^)]*?\)",
            bh_src,
        )
        for call in bare:
            if "def " in call:
                continue
            assert call.count(",") >= 3, f"вызов без kind: {call!r}"
        _ok("T14: Все 4 пути автомьюта передают kind", f"вызовов: {len(bare)}")
    except Exception as e:
        _fail("T14: kind в путях автомьюта", str(e))

    # T15: веб-панель
    try:
        ac_src = (WORK_DIR / "web" / "admin_chats.py").read_text()
        assert "automute_decay_days" in ac_src, \
            "форма настроек чата не принимает automute_decay_days"
        tpl = (WORK_DIR / "templates" / "admin_chats.html").read_text()
        assert 'name="automute_decay_days"' in tpl, \
            "в шаблоне нет input automute_decay_days"
        _ok("T15: Поле automute_decay_days в панели")
    except Exception as e:
        _fail("T15: Поле в панели", str(e))

    # T16: API отдаёт разбивку
    try:
        api_src = (WORK_DIR / "web" / "api.py").read_text()
        assert '"counts"' in api_src, "/api/automute-count не отдаёт counts"
        assert "kind" in api_src, "api.py не знает про kind"
        _ok("T16: /api/automute-count отдаёт разбивку по видам")
    except Exception as e:
        _fail("T16: API разбивка", str(e))

    # T17: версия и changelog
    try:
        import web_app

        from _version import ver
        assert ver(web_app.APP_VERSION) >= ver("v5.6.0"), \
            f"APP_VERSION={web_app.APP_VERSION!r}, ожидалось >= v5.6.0"
        html = (WORK_DIR / "templates" / "base.html").read_text()
        assert "v5.6.0" in html, "v5.6.0 не найден в changelog"
        assert "automute_decay_days" in html, \
            "changelog не упоминает automute_decay_days"
        _ok(f"T17: APP_VERSION = {web_app.APP_VERSION} + changelog")
    except Exception as e:
        _fail("T17: Версия и changelog", str(e))


# ═══════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════

def main():
    print("\n" + "=" * 60)
    print("  v5.6.0 — Виды автомьютов + decay счётчика: тесты")
    print("=" * 60)

    try:
        test_static()
        asyncio.run(test_functional())
        test_legacy_migration()
        test_wiring()
    except AssertionError:
        traceback.print_exc()

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
