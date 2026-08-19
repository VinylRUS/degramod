"""
web/admin_cleanup.py — очистка тестовых данных БД (#4).

v4.9.0 (Task 4): вынесены GET и POST /admin/cleanup из create_app(), вместе
с хелпером `_cleanup_counts` — раньше он был вложен в create_app(), теперь
это модульная функция: её используют web/admin_settings.py (роут
/admin/settings) и web/admin_keywords.py (toggle-ban-night), задачи 8 и 5.

Хелперы web_app (`_req_logger`, `_wal_checkpoint_async`, `_backup_db_async`)
берутся через модуль (`web_app._helper(...)`), а не импортом имён: тесты
патчат атрибуты модуля, и при `from web_app import ...` патч промахнулся бы
мимо уже связанного имени.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import web_app
from db import DB_PATH
from web.deps import AuthUser, require_csrf_su, require_su

router = APIRouter()


# ──────────────────────────────────────────────────────────────────
#  /admin/cleanup — безопасная очистка тестовых данных (v4.4.5)
#
#  SU-only. Позволяет одним кликом очистить тестовый мусор из БД:
#    • punishments        — ВСЕ записи (тестовые варны/мьюты/баны)
#    • users              — нарушители, НЕ являющиеся модераторами
#    • chat_admins        — опционально (checkbox)
#  СОХРАНЯЮТСЯ: moderators, web_users, chat_settings.
#
#  До удаления — бэкап SQLite-файла в той же папке.
#  После — VACUUM. Бот продолжает работать (WAL).
# ──────────────────────────────────────────────────────────────────
def _cleanup_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Текущие счётчики для preview. Прямые SELECT'ы — быстро и безопасно."""
    c = {}
    c["punishments"] = conn.execute("SELECT COUNT(*) FROM punishments").fetchone()[0]
    c["users"] = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    c["moderators"] = conn.execute("SELECT COUNT(*) FROM moderators").fetchone()[0]
    c["web_users"] = conn.execute("SELECT COUNT(*) FROM web_users").fetchone()[0]
    c["chat_admins"] = conn.execute("SELECT COUNT(*) FROM chat_admins").fetchone()[0]
    c["chat_settings"] = conn.execute("SELECT COUNT(*) FROM chat_settings").fetchone()[0]
    # users, не являющиеся модераторами (кандидаты на удаление)
    c["users_to_delete"] = conn.execute(
        "SELECT COUNT(*) FROM users WHERE user_id NOT IN (SELECT mod_id FROM moderators)"
    ).fetchone()[0]
    return c


# ──────────────────────────────────────────────────────────────────
#  v4.5: /admin/cleanup → редирект на /admin/settings#cleanup
#  Старый маршрут сохранён для обратной совместимости (закладки, тесты).
#  POST /admin/cleanup всё ещё работает как alias к логике cleanup,
#  чтобы не ломать существующие тесты.
# ──────────────────────────────────────────────────────────────────
@router.get("/admin/cleanup", response_class=HTMLResponse)
async def admin_cleanup_page_legacy(
    request: Request,
    flash: str = "",
    _auth: AuthUser = Depends(require_su),
):
    # Редирект на новую страницу Settings (с якорем на блок cleanup).
    # flash пробрасываем через query string.
    target = "/admin/settings"
    if flash:
        target += f"?flash={flash}"
    target += "#cleanup"
    return RedirectResponse(url=target, status_code=303)


@router.post("/admin/cleanup")
async def admin_cleanup_apply(
    request: Request,
    include_chat_admins: str = Form(""),
    _auth: AuthUser = Depends(require_csrf_su),
):
    """v4.5: реальное удаление тестовых данных (логика не изменилась).

    Шаги:
      1. Проверяем что БД существует.
      2. Проверяем что moderators/web_users не пустые (защита от случайного
         запуска на свежей БД).
      3. Создаём бэкап <DB_PATH>.backup-YYYYMMDD-HHMMSS.db.
      4. DELETE FROM punishments (все).
      5. DELETE FROM users WHERE user_id NOT IN (SELECT mod_id FROM moderators).
      6. Если include_chat_admins — DELETE FROM chat_admins.
      7. VACUUM.
      8. Логируем действие, возвращаем результат на страницу.
    """
    if not os.path.exists(DB_PATH):
        return RedirectResponse(
            url="/admin/settings?flash=Database+file+not+found#cleanup",
            status_code=303,
        )

    # ── 1. Pre-flight counts ────────────────────────────────────
    # v4.8.7: blocking SQLite — в потоке.
    def _preflight_counts_sync() -> dict:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            return _cleanup_counts(conn)
        finally:
            conn.close()
    before = await asyncio.to_thread(_preflight_counts_sync)

    # ── 2. Safety: refuse on empty moderators+web_users ─────────
    if before["moderators"] == 0 and before["web_users"] == 0:
        web_app._req_logger.warning(
            "admin_cleanup: refused — empty moderators+web_users (by=%s)",
            _auth.username,
        )
        return RedirectResponse(
            url="/admin/settings?flash=Refused%3A+no+moderators+and+no+web+users+"
                "+present.+Create+at+least+one+admin+before+cleanup.#cleanup",
            status_code=303,
        )

    # ── 3. Backup ───────────────────────────────────────────────
    ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup_path = f"{DB_PATH}.backup-{ts}.db"
    backup_filename = os.path.basename(backup_path)
    # v4.5.1: checkpoint WAL в основной файл перед копированием.
    # v4.8.7: blocking I/O — через async-обёртки.
    await web_app._wal_checkpoint_async()
    try:
        await web_app._backup_db_async(backup_path)
    except OSError as e:
        web_app._req_logger.error("admin_cleanup: backup failed: %s", e)
        return RedirectResponse(
            url=f"/admin/settings?flash=Backup+failed%3A+{e}#cleanup",
            status_code=303,
        )

    web_app._req_logger.info(
        "admin_cleanup: backup created %s (by=%s, include_chat_admins=%s)",
        backup_path, _auth.username, bool(include_chat_admins),
    )

    # ── 4-7. Delete + VACUUM ────────────────────────────────────
    # v4.8.7: DELETE + VACUUM — всё в одном синхронном блоке внутри
    # asyncio.to_thread. Транзакция с `with conn:` атомарна для DELETE;
    # VACUUM выполняется после фиксации транзакции (SQLite требует VACUUM
    # вне активной транзакции). Возврат: (deleted_p, deleted_u, deleted_ca).
    deleted_punishments = 0
    deleted_users = 0
    deleted_chat_admins: int | None = None

    def _delete_and_vacuum_sync() -> tuple[int, int, int | None]:
        dp = du = 0
        dca: int | None = None
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            with conn:
                cur = conn.execute("DELETE FROM punishments")
                dp = cur.rowcount

                cur = conn.execute(
                    "DELETE FROM users WHERE user_id NOT IN "
                    "(SELECT mod_id FROM moderators)"
                )
                du = cur.rowcount

                if include_chat_admins:
                    cur = conn.execute("DELETE FROM chat_admins")
                    dca = cur.rowcount

            # VACUUM вне транзакции (SQLite требует)
            conn.isolation_level = None
            conn.execute("VACUUM")
            conn.isolation_level = ""
        finally:
            conn.close()
        return dp, du, dca

    try:
        deleted_punishments, deleted_users, deleted_chat_admins = (
            await asyncio.to_thread(_delete_and_vacuum_sync)
        )
    except sqlite3.Error as e:
        web_app._req_logger.error(
            "admin_cleanup: deletion failed: %s (backup=%s)",
            e, backup_path,
        )
        return RedirectResponse(
            url=f"/admin/settings?flash=Deletion+failed%3A+{e}.+Restore+from+{backup_filename}#cleanup",
            status_code=303,
        )

    # ── 8. Post-counts ──────────────────────────────────────────
    def _post_counts_sync() -> dict:
        conn = sqlite3.connect(DB_PATH)
        try:
            return _cleanup_counts(conn)
        finally:
            conn.close()
    after = await asyncio.to_thread(_post_counts_sync)

    web_app._req_logger.info(
        "admin_cleanup: done (by=%s) punishments %d→%d, users %d→%d, "
        "chat_admins %d→%d, backup=%s",
        _auth.username,
        before["punishments"], after["punishments"],
        before["users"], after["users"],
        before["chat_admins"], after["chat_admins"],
        backup_filename,
    )

    flash_msg = (
        f"Cleanup+complete%3A+{deleted_punishments}+punishments+deleted%2C+"
        f"{deleted_users}+users+deleted%2C+backup%3A+{backup_filename}"
    )
    return RedirectResponse(
        url=f"/admin/settings?flash={flash_msg}#cleanup",
        status_code=303,
    )
