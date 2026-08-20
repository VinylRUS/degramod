"""
web/admin_settings.py — страница настроек системы и интеграция с GitHub (#8).

v4.9.0 (Task 8): вынесены GET /admin/settings, POST /admin/settings/backup,
POST /admin/settings/vacuum, GET /admin/settings/github, POST
/admin/settings/github и POST /admin/settings/github/test из create_app().
Вместе с роутами переехали два вложенных хелпера:
  • `_bot_info` — snapshot версии/uptime/памяти/счётчиков БД для страницы
    Settings, зовёт только `admin_settings_page`;
  • `_load_github_settings_row` — гарантирует singleton-строку GithubSettings
    (id=1), зовут три github-роута (`_get`, `_post`, `_test`).

`admin_settings_page` также зовёт `_cleanup_counts` — он не отсюда, а из
`web/admin_cleanup.py` (Task 4); это единственный межмодульный импорт между
роутерами web/ во всей декомпозиции.

Хелперы и константы web_app (`_req_logger`, `_wal_checkpoint_async`,
`_backup_db_async`, `_APP_START_TIME`) берутся через модуль
(`web_app._helper`), а не импортом имён: тесты патчат атрибуты модуля, и при
`from web_app import ...` патч промахнулся бы мимо уже связанного имени.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import time
from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

import health_probe
import web_app
from db import DB_PATH, GithubSettings, _decrypt_pat, _encrypt_pat, async_session
from web.admin_cleanup import _cleanup_counts
from web.deps import APP_VERSION, AuthUser, get_templates, require_csrf_su, require_su

router = APIRouter()


# ──────────────────────────────────────────────────────────────────
#  v4.5: /admin/settings — Settings (SU-only)
#
#  Раздел:
#    • Bot info (версия, uptime, чаты, модераторы, наказания)
#    • Backup now (создать копию БД вручную)
#    • Cleanup (preview + apply — встроенный, как было в /admin/cleanup)
#    • VACUUM (оптимизация файла БД без удаления данных)
# ──────────────────────────────────────────────────────────────────
async def _bot_info() -> dict:
    """Собирает snapshot инфо о боте для страницы Settings.

    v4.8.6: переписано без psutil (его нет в requirements.txt, поэтому
    uptime всегда показывал 0s). Uptime считается от _APP_START_TIME
    (время импорта модуля). Memory RSS читается из /proc/self/status
    (Linux only — на Bothost работает, в Windows dev-окружении fallback 0).
    v4.8.7: blocking SQLite (6 COUNT-запросов) вынесен в asyncio.to_thread
    — не фризит event loop при рендере страницы Settings.
    """
    info = {
        "version": APP_VERSION,
        "uptime_seconds": int(time.time() - web_app._APP_START_TIME),
        "db_path": DB_PATH,
        "db_size_bytes": 0,
        "memory_rss_bytes": 0,
        "python_version": sys.version.split()[0],
        "chats_total": 0,
        "chats_enabled": 0,
        "chats_disabled": 0,
        "moderators_total": 0,
        "web_users_total": 0,
        "punishments_total": 0,
    }
    # Размер файла БД
    try:
        if os.path.exists(DB_PATH):
            info["db_size_bytes"] = os.path.getsize(DB_PATH)
    except OSError:
        pass
    # Memory RSS. v4.10.2 (Task 16): чтение /proc/self/status вынесено в
    # health_probe — тот же код понадобился /healthz, и держать две копии,
    # расходящиеся при первой же правке, незачем.
    info["memory_rss_bytes"] = health_probe.memory_rss_bytes()
    # Счётчики из БД — v4.8.7: blocking SQLite в потоке.
    def _counts_sync() -> dict:
        result = {
            "chats_total": 0, "chats_enabled": 0, "chats_disabled": 0,
            "moderators_total": 0, "web_users_total": 0, "punishments_total": 0,
        }
        conn = sqlite3.connect(DB_PATH)
        try:
            result["chats_total"] = conn.execute(
                "SELECT COUNT(*) FROM chat_settings WHERE chat_id != 0"
            ).fetchone()[0]
            result["chats_enabled"] = conn.execute(
                "SELECT COUNT(*) FROM chat_settings WHERE chat_id != 0 AND is_enabled=1"
            ).fetchone()[0]
            result["chats_disabled"] = conn.execute(
                "SELECT COUNT(*) FROM chat_settings WHERE chat_id != 0 AND is_enabled=0"
            ).fetchone()[0]
            result["moderators_total"] = conn.execute(
                "SELECT COUNT(*) FROM moderators"
            ).fetchone()[0]
            result["web_users_total"] = conn.execute(
                "SELECT COUNT(*) FROM web_users"
            ).fetchone()[0]
            result["punishments_total"] = conn.execute(
                "SELECT COUNT(*) FROM punishments"
            ).fetchone()[0]
        finally:
            conn.close()
        return result
    try:
        counts = await asyncio.to_thread(_counts_sync)
        info.update(counts)
    except sqlite3.Error as e:
        web_app._req_logger.warning("_bot_info: sqlite error: %s", e)
    return info


@router.get("/admin/settings", response_class=HTMLResponse)
async def admin_settings_page(
    request: Request,
    flash: str = "",
    _auth: AuthUser = Depends(require_su),
    templates: Jinja2Templates = Depends(get_templates),
):
    """v4.5: страница настроек системы (SU-only). v4.8.5: + GitHub Projects."""
    # Cleanup preview (live counts из БД)
    if not os.path.exists(DB_PATH):
        counts = {
            "punishments": 0, "users": 0, "moderators": 0,
            "web_users": 0, "chat_admins": 0, "chat_settings": 0,
            "users_to_delete": 0,
        }
    else:
        # v4.8.7: blocking SQLite — в потоке, чтобы не фризить event loop.
        def _get_counts_sync():
            conn = sqlite3.connect(DB_PATH)
            try:
                return _cleanup_counts(conn)
            finally:
                conn.close()
        counts = await asyncio.to_thread(_get_counts_sync)

    # v4.8.5: текущие настройки GitHub (для pre-fill формы).
    github_settings: dict = {}
    try:
        async with async_session() as session:
            from db import GithubSettings as _GS
            gs = (await session.execute(
                select(_GS).where(_GS.id == 1)
            )).scalar_one_or_none()
            if gs is not None:
                github_settings = {
                    "is_active": gs.is_active,
                    "is_pat_set": bool(gs.pat_encrypted),
                    "repo_owner": gs.repo_owner or "",
                    "repo_name": gs.repo_name or "",
                    "project_node_id": gs.project_node_id or "",
                    "project_number": gs.project_number,
                    "project_owner_login": gs.project_owner_login or "",
                    # v4.8.5.3: имя Status-опции для авто-присвоения.
                    "project_status_option_name": gs.project_status_option_name or "Предложено",
                    "updated_at": gs.updated_at.strftime("%Y-%m-%d %H:%M UTC") if gs.updated_at else None,
                    "updated_by": gs.updated_by,
                }
    except Exception as e:
        web_app._req_logger.warning("admin_settings_page: failed to load github_settings: %s", e)
        github_settings = {}

    # v4.8.5: статистика идей (для отображения в разделе GitHub).
    idea_stats: dict = {}
    try:
        conn = sqlite3.connect(DB_PATH)
        try:
            cur = conn.execute(
                "SELECT COUNT(*), "
                "SUM(CASE WHEN github_issue_url IS NOT NULL THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN error_message IS NOT NULL THEN 1 ELSE 0 END), "
                "MAX(created_at) "
                "FROM idea_log"
            )
            row = cur.fetchone()
            idea_stats = {
                "total": row[0] or 0,
                "succeeded": row[1] or 0,
                "failed": row[2] or 0,
                "last_at": row[3],
            }
        finally:
            conn.close()
    except sqlite3.Error as e:
        web_app._req_logger.warning("admin_settings_page: idea_log stats failed: %s", e)
        idea_stats = {"total": 0, "succeeded": 0, "failed": 0, "last_at": None}

    return templates.TemplateResponse("admin_settings.html", {
        "request": request,
        "auth_user": _auth,
        "app_version": APP_VERSION,
        "flash": flash or None,
        "counts": counts,
        "db_path_dir": os.path.dirname(DB_PATH) or ".",
        "bot_info": await _bot_info(),
        "github_settings": github_settings,
        "idea_stats": idea_stats,
    })


@router.post("/admin/settings/backup")
async def admin_settings_backup(
    request: Request,
    _auth: AuthUser = Depends(require_csrf_su),
):
    """v4.5: создаёт резервную копию БД без удаления данных."""
    if not os.path.exists(DB_PATH):
        return RedirectResponse(
            url="/admin/settings?flash=Database+file+not+found",
            status_code=303,
        )
    ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup_path = f"{DB_PATH}.backup-{ts}.db"
    backup_filename = os.path.basename(backup_path)
    # v4.5.1: checkpoint WAL в основной файл перед копированием — иначе
    # свежие записи (последние несколько секунд) в бэкап не попадут.
    # v4.8.7: blocking I/O обёрнут в asyncio.to_thread — event loop
    # остаётся живым, бот продолжает отвечать в чатах.
    await web_app._wal_checkpoint_async()
    try:
        await web_app._backup_db_async(backup_path)
    except OSError as e:
        web_app._req_logger.error("admin_settings_backup: backup failed: %s", e)
        return RedirectResponse(
            url=f"/admin/settings?flash=Backup+failed%3A+{e}",
            status_code=303,
        )
    web_app._req_logger.info(
        "admin_settings_backup: created %s (by=%s)",
        backup_path, _auth.username,
    )
    return RedirectResponse(
        url=f"/admin/settings?flash=Backup+created%3A+{backup_filename}",
        status_code=303,
    )


@router.post("/admin/settings/vacuum")
async def admin_settings_vacuum(
    request: Request,
    _auth: AuthUser = Depends(require_csrf_su),
):
    """v4.5: запускает VACUUM на файле БД (оптимизация без удаления данных)."""
    if not os.path.exists(DB_PATH):
        return RedirectResponse(
            url="/admin/settings?flash=Database+file+not+found",
            status_code=303,
        )
    # v4.8.7: VACUUM — блокирующая синхронная операция. Выносим в
    # поток через asyncio.to_thread — event loop остаётся живым, бот
    # продолжает обрабатывать обновления во время VACUUM.
    def _vacuum_sync() -> tuple[int, int]:
        size_before = os.path.getsize(DB_PATH)
        conn = sqlite3.connect(DB_PATH)
        try:
            conn.isolation_level = None
            conn.execute("VACUUM")
            conn.isolation_level = ""
        finally:
            conn.close()
        size_after = os.path.getsize(DB_PATH)
        return size_before, size_after

    try:
        size_before, size_after = await asyncio.to_thread(_vacuum_sync)
    except sqlite3.Error as e:
        web_app._req_logger.error("admin_settings_vacuum: VACUUM failed: %s", e)
        return RedirectResponse(
            url=f"/admin/settings?flash=VACUUM+failed%3A+{e}",
            status_code=303,
        )
    web_app._req_logger.info(
        "admin_settings_vacuum: VACUUM done (by=%s) size %d→%d bytes",
        _auth.username, size_before, size_after,
    )
    delta = size_before - size_after
    return RedirectResponse(
        url=f"/admin/settings?flash=VACUUM+done.+DB+size%3A+{size_before}+%E2%86%92+{size_after}+bytes+"
            f"(-{delta}+bytes+freed)",
        status_code=303,
    )


# ══════════════════════════════════════════════════════════════════
#  v4.8.5: GitHub Projects — настройки для !idea → Issues
# ══════════════════════════════════════════════════════════════════

async def _load_github_settings_row(session) -> GithubSettings:
    """Гарантирует что singleton-строка GithubSettings (id=1) есть в БД
    и возвращает её. Если нет — создаёт пустую.
    """
    gs = (await session.execute(
        select(GithubSettings).where(GithubSettings.id == 1)
    )).scalar_one_or_none()
    if gs is None:
        gs = GithubSettings(id=1, is_active=False)
        session.add(gs)
        await session.commit()
        await session.refresh(gs)
    return gs


@router.get("/admin/settings/github")
async def admin_settings_github_get(
    request: Request,
    _auth: AuthUser = Depends(require_su),
):
    """v4.8.5: возвращает текущие настройки GitHub Projects как JSON.

    Используется JavaScript-формой для pre-fill значений при загрузке
    страницы. PAT возвращается в виде признака is_pat_set (True/False),
    а НЕ в открытом виде.
    """
    async with async_session() as session:
        gs = await _load_github_settings_row(session)
        return {
            "is_active": gs.is_active,
            "is_pat_set": bool(gs.pat_encrypted),
            "repo_owner": gs.repo_owner or "",
            "repo_name": gs.repo_name or "",
            "project_node_id": gs.project_node_id or "",
            "project_number": gs.project_number,
            "project_owner_login": gs.project_owner_login or "",
            # v4.8.5.3: имя Status-опции для авто-присвоения.
            "project_status_option_name": gs.project_status_option_name or "Предложено",
            "updated_at": gs.updated_at.isoformat() if gs.updated_at else None,
            "updated_by": gs.updated_by,
        }


@router.post("/admin/settings/github")
async def admin_settings_github_post(
    request: Request,
    pat: str = Form(""),
    repo_owner: str = Form(""),
    repo_name: str = Form(""),
    project_node_id: str = Form(""),
    project_number: str = Form(""),
    project_owner_login: str = Form(""),
    project_status_option_name: str = Form(""),
    is_active: str = Form(""),
    _auth: AuthUser = Depends(require_csrf_su),
):
    """v4.8.5: сохраняет настройки GitHub Projects.

    Логика:
      • PAT: если поле pat непустое — шифруем и сохраняем. Если пустое —
        оставляем старый PAT (не затираем). Это чтобы SU мог менять
        другие поля (repo_owner, project_number) не перезаводя PAT.
      • repo_owner/repo_name/project_*: просто сохраняем как есть.
      • is_active: '1' если чекбокс включён, иначе '0'.
      • v4.8.5.3: project_status_option_name — имя Status-опции для
        авто-присвоения (default 'Предложено'). Пустое значение
        сохраняется как default.
      • Сохранение НЕ запускает test_connection — это отдельный endpoint.

    После сохранения — редирект на /admin/settings#github с flash-статусом.
    """
    # Валидация: если is_active=True, то PAT, repo_owner, repo_name должны
    # быть заполнены (без них `!idea` не сможет создать Issue).
    is_active_flag = is_active == "1"
    async with async_session() as session:
        gs = await _load_github_settings_row(session)

        # PAT: только если задан новый.
        if pat.strip():
            try:
                gs.pat_encrypted = _encrypt_pat(pat.strip())
            except Exception as e:
                web_app._req_logger.error(
                    "admin_settings_github_post: encrypt PAT failed: %s", e,
                )
                return RedirectResponse(
                    url=f"/admin/settings?flash=PAT+encryption+failed%3A+{e}#github",
                    status_code=303,
                )

        gs.repo_owner = repo_owner.strip() or None
        gs.repo_name = repo_name.strip() or None
        gs.project_node_id = project_node_id.strip() or None
        gs.project_owner_login = project_owner_login.strip() or None
        # v4.8.5.3: имя Status-опции (default 'Предложено' если пусто).
        gs.project_status_option_name = (
            project_status_option_name.strip() or "Предложено"
        )

        # project_number — int или None.
        pn = project_number.strip()
        if pn:
            try:
                gs.project_number = int(pn)
            except ValueError:
                gs.project_number = None
        else:
            gs.project_number = None

        # Если активируется — проверяем что PAT и repo заполнены.
        if is_active_flag:
            missing = []
            if not gs.pat_encrypted:
                missing.append("PAT")
            if not gs.repo_owner:
                missing.append("repo_owner")
            if not gs.repo_name:
                missing.append("repo_name")
            if missing:
                web_app._req_logger.warning(
                    "admin_settings_github_post: cannot activate — missing %s",
                    ", ".join(missing),
                )
                return RedirectResponse(
                    url=f"/admin/settings?flash=Cannot+activate%3A+missing+{'+'.join(missing)}#github",
                    status_code=303,
                )

        gs.is_active = is_active_flag
        gs.updated_by = _auth.username

        await session.commit()

    web_app._req_logger.info(
        "admin_settings_github_post: saved (by=%s, active=%s, repo=%s/%s)",
        _auth.username, is_active_flag, repo_owner, repo_name,
    )
    return RedirectResponse(
        url="/admin/settings?flash=GitHub+settings+saved.#github",
        status_code=303,
    )


@router.post("/admin/settings/github/test")
async def admin_settings_github_test(
    request: Request,
    _auth: AuthUser = Depends(require_csrf_su),
):
    """v4.8.5: проверка подключения к GitHub.

    Шаги:
      1. Читаем настройки из БД (PAT расшифровываем).
      2. Вызываем github_client.test_connection() — он создаёт + закрывает
         тестовый Issue в репо и проверяет Project (если задан).
      3. Логируем результат.
      4. Возвращаем JSON с ok/message/details — фронтенд показывает
         пользователю.

    Если PAT не задан или не расшифровывается — возвращаем ошибку.
    """
    async with async_session() as session:
        gs = await _load_github_settings_row(session)
        if not gs.pat_encrypted:
            return {
                "ok": False,
                "message": "PAT не задан. Сначала сохраните форму с PAT.",
            }
        try:
            pat = _decrypt_pat(gs.pat_encrypted)
        except Exception as e:
            web_app._req_logger.error(
                "admin_settings_github_test: decrypt PAT failed: %s", e,
            )
            return {
                "ok": False,
                "message": f"Не удалось расшифровать PAT: {e}. "
                "Перезаведите токен через форму.",
            }
        if not gs.repo_owner or not gs.repo_name:
            return {
                "ok": False,
                "message": "repo_owner и repo_name должны быть заполнены.",
            }

    # Вызываем test_connection.
    try:
        from github_client import GithubApiError, get_project_node_id, test_connection
        # Если project_node_id пустой, но project_owner_login + project_number
        # заданы — резолвим node_id автоматически.
        project_node_id = gs.project_node_id
        if not project_node_id and gs.project_owner_login and gs.project_number:
            try:
                project_node_id = await get_project_node_id(
                    pat=pat,
                    owner_login=gs.project_owner_login,
                    project_number=gs.project_number,
                )
                # Сохраняем резолвнутый node_id в БД — при следующем
                # тесте не надо резолвить заново.
                async with async_session() as session:
                    gs2 = await _load_github_settings_row(session)
                    gs2.project_node_id = project_node_id
                    gs2.updated_by = _auth.username
                    await session.commit()
            except GithubApiError as e:
                return {
                    "ok": False,
                    "message": f"Не удалось резолвить Project node_id "
                    f"по owner='{gs.project_owner_login}' "
                    f"number={gs.project_number}: {e}",
                }

        result = await test_connection(
            pat=pat,
            owner=gs.repo_owner,
            repo=gs.repo_name,
            project_node_id=project_node_id,
        )
    except GithubApiError as e:
        web_app._req_logger.warning(
            "admin_settings_github_test: test_connection failed: %s", e,
        )
        return {"ok": False, "message": str(e)}
    except Exception as e:
        web_app._req_logger.exception(
            "admin_settings_github_test: unexpected error: %s", e,
        )
        return {"ok": False, "message": f"Unexpected error: {e}"}

    web_app._req_logger.info(
        "admin_settings_github_test: result ok=%s (by=%s, message=%s)",
        result.ok, _auth.username, result.message,
    )
    return {
        "ok": result.ok,
        "message": result.message,
        "details": result.details,
    }
