"""
cleanup_test_data.py — Безопасная очистка тестовых данных из БД бота.

ЧТО УДАЛЯЕТСЯ:
  • punishments       — ВСЕ записи (тестовые варны/мьюты/баны/unmute/unwarn/unban)
  • users             — нарушители, НЕ состоящие в moderators
                        (модераторы, случайно попавшие в users, сохраняются)
  • chat_admins       — опционально (флаг --include-chat-admins),
                        по умолчанию НАСТРОЙКИ ЧАТОВ НЕ ТРОГАЮТСЯ

ЧТО СОХРАНЯЕТСЯ (ВАЖНО — АДМИНЫ И МОДЕРАТОРЫ):
  • moderators        — все модераторы Telegram (не трогается)
  • web_users         — SU + все админы/модераторы веб-панели (не трогается)
  • chat_settings     — настройки чатов: хэштеги, пороги варнов, report-chat (не трогается)

БЕЗОПАСНОСТЬ:
  1. По умолчанию работает в --dry-run (просто показывает что удалил бы).
  2. Перед реальным удалением делает бэкап файла БД (*.backup-<timestamp>.db).
  3. Перед удалением проверяет что таблицы moderators/web_users НЕ пустые —
     иначе отказывается удалять (защита от случайного запуска на пустой БД,
     где может быть что-то пошло не так).
  4. В конце делает VACUUM для сжатия файла.

ИСПОЛЬЗОВАНИЕ:
  # Шаг 1 — посмотреть что было бы удалено (БЕЗ изменений):
  python scripts/cleanup_test_data.py

  # Шаг 2 — выполнить очистку:
  python scripts/cleanup_test_data.py --apply

  # Шаг 3 — то же самое + очистить chat_admins (доп. админы чатов):
  python scripts/cleanup_test_data.py --apply --include-chat-admins

  # Указать свой путь к БД:
  DB_PATH=/path/to/shadow_logs.db python scripts/cleanup_test_data.py --apply
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime

# ── Путь к БД ───────────────────────────────────────────────────────────────
DB_PATH = os.getenv("DB_PATH", "/app/data/shadow_logs.db")


# ── Цветной вывод (опционально, отключается если не tty) ────────────────────
def _c(text: str, color: str) -> str:
    if not sys.stdout.isatty():
        return text
    codes = {
        "red":    "\033[31m",
        "green":  "\033[32m",
        "yellow": "\033[33m",
        "cyan":   "\033[36m",
        "bold":   "\033[1m",
        "reset":  "\033[0m",
    }
    return f"{codes.get(color, '')}{text}{codes['reset']}"


def _count(conn: sqlite3.Connection, table: str, where: str = "") -> int:
    sql = f"SELECT COUNT(*) FROM {table}"
    if where:
        sql += f" WHERE {where}"
    return conn.execute(sql).fetchone()[0]


def _show_counts(conn: sqlite3.Connection, label: str, include_chat_admins: bool) -> dict[str, int]:
    """Печатает текущие счётчики по таблицам. Возвращает словарь счётчиков."""
    print(_c(f"\n=== {label} ===", "bold"))
    counts = {
        "users":         _count(conn, "users"),
        "moderators":    _count(conn, "moderators"),
        "punishments":   _count(conn, "punishments"),
        "web_users":     _count(conn, "web_users"),
        "chat_admins":   _count(conn, "chat_admins"),
        "chat_settings": _count(conn, "chat_settings"),
    }
    # Сколько users НЕ являются модераторами (т.е. кандидаты на удаление)
    counts["users_to_delete"] = _count(
        conn,
        "users",
        where="user_id NOT IN (SELECT mod_id FROM moderators)",
    )
    print(f"  users              : {counts['users']:>6}  "
          f"(из них нарушителей-не-модераторов: {counts['users_to_delete']})")
    print(f"  moderators         : {counts['moderators']:>6}  {_c('[СОХРАНЯЕМ]', 'green')}")
    print(f"  web_users          : {counts['web_users']:>6}  {_c('[СОХРАНЯЕМ]', 'green')}")
    print(f"  punishments        : {counts['punishments']:>6}  {_c('[УДАЛЯЕМ ВСЕ]', 'red')}")
    print(f"  chat_admins        : {counts['chat_admins']:>6}  "
          f"{_c('[УДАЛЯЕМ]', 'red') if include_chat_admins else _c('[СОХРАНЯЕМ]', 'green')}")
    print(f"  chat_settings      : {counts['chat_settings']:>6}  {_c('[СОХРАНЯЕМ]', 'green')}")
    return counts


def _backup_db(db_path: str) -> str:
    """Копирует db_path в <db_path>.backup-YYYYMMDD-HHMMSS-microsec.db. Возвращает путь бэкапа."""
    # %f — микросекунды, чтобы избежать коллизий при быстрых повторных вызовах
    ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup_path = f"{db_path}.backup-{ts}.db"
    shutil.copy2(db_path, backup_path)
    return backup_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Безопасная очистка тестовых данных БД бота (админов/модераторов НЕ трогает).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Реально применить изменения. Без этого флага — только dry-run (по умолчанию).",
    )
    parser.add_argument(
        "--include-chat-admins",
        action="store_true",
        help="Также очистить таблицу chat_admins (доп. админы чатов, добавленные через /addadmin). "
             "По умолчанию chat_admins не трогается.",
    )
    parser.add_argument(
        "--db-path",
        default=DB_PATH,
        help=f"Путь к SQLite-файлу (по умолчанию: $DB_PATH или {DB_PATH}).",
    )
    args = parser.parse_args()

    db_path = args.db_path
    if not os.path.exists(db_path):
        print(_c(f"ОШИБКА: файл БД не найден: {db_path}", "red"), file=sys.stderr)
        print("Укажите путь через --db-path или переменную окружения DB_PATH.", file=sys.stderr)
        return 2

    print(_c(f"БД: {db_path}", "cyan"))
    print(_c(f"Режим: {'APPLY (реальное удаление)' if args.apply else 'DRY-RUN (без изменений)'}", "yellow"))
    if args.include_chat_admins:
        print(_c("Флаг: --include-chat-admins (также очистим chat_admins)", "yellow"))

    # ── Подключаемся ────────────────────────────────────────────────────────
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")

    # ── До очистки ──────────────────────────────────────────────────────────
    before = _show_counts(conn, "ДО ОЧИСТКИ", args.include_chat_admins)

    # ── Защита: проверяем что moderators/web_users не пустые ────────────────
    # (если кто-то запустит скрипт на свежей БД без модераторов —
    #  это скорее всего ошибка, и удаление всех users будет потерей данных)
    if before["moderators"] == 0 and before["web_users"] == 0:
        print(_c(
            "\nОТКАЗ: в БД нет ни одного модератора и ни одного веб-юзера. "
            "Скрипт отказывается работать вслепую — создайте хотя бы одного "
            "админа/модератора перед очисткой.",
            "red",
        ), file=sys.stderr)
        conn.close()
        return 3
    if before["moderators"] == 0:
        print(_c(
            "\nВНИМАНИЕ: таблица moderators пуста. Все записи из users будут "
            "удалены (нет модераторов для исключения). Продолжаем, но проверьте логику.",
            "yellow",
        ))

    # ── Подготавливаем SQL удалений ─────────────────────────────────────────
    # Сначала отключаем foreign_keys временно, потому что SQLite не даст
    # удалить users когда есть punishments (но мы удаляем punishments первыми,
    # так что по идее ок. Однако для подстраховки — отдельно.)
    # На самом деле мы уже сделали PRAGMA foreign_keys=ON, и порядок удаления
    # ниже это учитывает: сначала punishments, потом users.

    deletions: list[tuple[str, str]] = [
        ("punishments", "DELETE FROM punishments"),
        (
            "users (не модераторы)",
            "DELETE FROM users WHERE user_id NOT IN (SELECT mod_id FROM moderators)",
        ),
    ]
    if args.include_chat_admins:
        deletions.append(("chat_admins", "DELETE FROM chat_admins"))

    # ── DRY-RUN: показываем что удалили бы ──────────────────────────────────
    if not args.apply:
        print(_c("\n=== DRY-RUN: изменения НЕ применены ===", "yellow"))
        print("Будет выполнено:")
        for label, sql in deletions:
            print(f"  • {label}")
            print(f"      SQL: {sql}")
        print(_c("\nДля реального запуска добавьте флаг --apply", "bold"))
        conn.close()
        return 0

    # ── APPLY: бэкап + удаление ─────────────────────────────────────────────
    print(_c("\n=== APPLY: выполняем очистку ===", "bold"))

    backup_path = _backup_db(db_path)
    print(_c(f"Бэкап создан: {backup_path}", "green"))

    try:
        with conn:
            for label, sql in deletions:
                cur = conn.execute(sql)
                print(f"  • {label}: удалено {cur.rowcount} строк")
        # VACUUM вне транзакции (SQLite требует)
        conn.isolation_level = None
        conn.execute("VACUUM")
        conn.isolation_level = ""
        print(_c("VACUUM выполнен — файл БД сжат.", "green"))
    except sqlite3.Error as e:
        print(_c(f"\nОШИБКА при удалении: {e}", "red"), file=sys.stderr)
        print(_c(f"Бэкап для восстановления: {backup_path}", "yellow"), file=sys.stderr)
        conn.close()
        return 4

    # ── После очистки ───────────────────────────────────────────────────────
    after = _show_counts(conn, "ПОСЛЕ ОЧИСТКИ", args.include_chat_admins)

    print(_c("\n=== ИТОГ ===", "bold"))
    print(f"  punishments: {before['punishments']:>6} → {after['punishments']:>6}")
    print(f"  users      : {before['users']:>6} → {after['users']:>6}  "
          f"(удалено {before['users'] - after['users']} тестовых нарушителей)")
    print(f"  moderators : {before['moderators']:>6} → {after['moderators']:>6}  "
          f"{_c('[не изменилось]', 'green')}")
    print(f"  web_users  : {before['web_users']:>6} → {after['web_users']:>6}  "
          f"{_c('[не изменилось]', 'green')}")
    print(f"  Бэкап      : {backup_path}")
    print(_c("\nГотово. Если что-то пошло не так — восстановите из бэкапа:", "green"))
    print(f"  cp {backup_path} {db_path}")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
