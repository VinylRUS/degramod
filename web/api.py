"""
web/api.py — /api/* JSON API роуты.

Сюда переезжают все /api/* JSON-эндпоинты. Хелперы и константы (логгер,
PAGE_SIZE, _SU_WEB_MOD_ID, _msk_time, _duration_fmt) берутся через модуль
web_app (web_app._helper и т.д.), а не импортом имён: тесты патчат
атрибуты модуля, и при `from web_app import ...` патч промахнулся бы
мимо уже связанного имени.

v4.8.10: перенесены /api/presets и /api/automute-count.
v4.9.0 (Task 6): перенесены /api/dashboard, /api/search, /api/unban
(нужен bot=Depends(get_bot)) и /api/reset-automute-count.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import func, select

import web_app
from db import (
    AutomuteCounter,
    Moderator,
    PermissionPreset,
    Punishment,
    User,
    async_session,
)
from web.deps import AuthUser, get_bot, require_admin, require_auth

router = APIRouter()


@router.get("/api/presets")
async def api_presets_list(
    scope: str = "",
    _auth: AuthUser = Depends(require_admin),
):
    """v4.6.0: JSON-API список пресетов (для динамической подгрузки в admin_chats).

    v4.8.10: перенесён из create_app() в web/api.py.
    """
    async with async_session() as session:
        q = select(PermissionPreset).order_by(
            PermissionPreset.scope, PermissionPreset.name
        )
        if scope in ("day", "night", "sanitary"):
            q = q.where(PermissionPreset.scope == scope)
        presets = (await session.execute(q)).scalars().all()
    return JSONResponse({
        "presets": [
            {
                "id": p.id,
                "name": p.name,
                "scope": p.scope,
                "permissions": json.loads(p.permissions) if p.permissions else {},
                "is_system": p.is_system,
            }
            for p in presets
        ]
    })


@router.get("/api/automute-count")
async def api_get_automute_count(
    request: Request,
    chat_id: int = 0,
    user_id: int = 0,
    _auth: AuthUser = Depends(require_auth),
):
    """v4.8.4: Возвращает счётчик автомьютов для (chat_id, user_id).

    v4.8.10: перенесён из create_app() в web/api.py.
    """
    if chat_id == 0 or user_id == 0:
        return JSONResponse(
            {"ok": False, "error": "chat_id and user_id are required"},
            status_code=400,
        )
    try:
        async with async_session() as session:
            counter = (await session.execute(
                select(AutomuteCounter).where(
                    AutomuteCounter.chat_id == chat_id,
                    AutomuteCounter.user_id == user_id,
                )
            )).scalar_one_or_none()
            count = counter.count if counter else 0
        return JSONResponse({
            "ok": True,
            "count": count,
            "chat_id": chat_id,
            "user_id": user_id,
        })
    except Exception as e:
        web_app._req_logger.error(
            "api_get_automute_count: error — chat_id=%s user_id=%s: %s",
            chat_id, user_id, e,
        )
        return JSONResponse(
            {"ok": False, "error": str(e)},
            status_code=500,
        )


# ── GET /api/dashboard — JSON для автообновления таблицы логов ───────
@router.get("/api/dashboard")
async def api_dashboard(
    request: Request,
    page: int = 1,
    action: str = "",
    rev: str = "",
    sort: str = "new",
    last: int = 0,    # id последней показанной записи — вернём только свежее
    _auth: AuthUser = Depends(require_auth),
):
    """v4.9.0 (Task 6): перенесён из create_app() в web/api.py."""
    offset = (page - 1) * web_app.PAGE_SIZE
    async with async_session() as session:
        base = (
            select(Punishment, User, Moderator)
            .join(User, Punishment.user_id == User.user_id)
            .join(Moderator, Punishment.mod_id == Moderator.mod_id)
        )
        if action in ("mute", "warn", "ban", "unmute", "unwarn", "unban"):
            base = base.where(Punishment.action_type == action)
        if rev == "active":
            base = base.where(Punishment.is_revoked.is_(False))
        elif rev == "revoked":
            base = base.where(Punishment.is_revoked.is_(True))
        if sort == "old":
            base = base.order_by(Punishment.created_at.asc())
        elif sort == "type":
            base = base.order_by(Punishment.action_type.asc(),
                                 Punishment.created_at.desc())
        elif sort == "user":
            base = base.order_by(User.username.asc().nullslast(),
                                 Punishment.created_at.desc())
        else:
            base = base.order_by(Punishment.created_at.desc())
        rows = (await session.execute(base.offset(offset).limit(web_app.PAGE_SIZE))).all()

        # Count active totals
        total_stmt = (
            select(Punishment.action_type, func.count(Punishment.id))
            .where(Punishment.is_revoked.is_(False))
            .group_by(Punishment.action_type)
        )
        total_stats = {
            row[0]: row[1]
            for row in (await session.execute(total_stmt)).all()
        }

    return JSONResponse({
        "stats": {
            "total": sum(total_stats.values()),
            "mute": total_stats.get("mute", 0),
            "warn": total_stats.get("warn", 0),
            "ban": total_stats.get("ban", 0),
            "unmute": total_stats.get("unmute", 0),
            "unwarn": total_stats.get("unwarn", 0),
            "unban": total_stats.get("unban", 0),
        },
        "rows": [
            {
                "id": p.id,
                "time": web_app._msk_time(p.created_at),
                "ts": int(p.created_at.replace(tzinfo=timezone.utc).timestamp())
                      if p.created_at.tzinfo is None
                      else int(p.created_at.timestamp()),
                "action": p.action_type,
                "is_revoked": bool(p.is_revoked),
                "user_id": u.user_id,
                "user_name": u.username or u.first_name or str(u.user_id),
                "user_username": u.username,
                "mod_name": m.username or m.first_name or str(m.mod_id),
                "duration": p.duration_seconds,
                "duration_fmt": web_app._duration_fmt(p.duration_seconds) if p.action_type != "warn" else f"{p.duration_seconds or 0} pts",
                "reason": p.reason,
                "message_text": p.message_text,
            }
            for p, u, m in rows
        ],
    })


# ── GET /api/search?q=<query> ──────────────────────────────────────
@router.get("/api/search")
async def api_search(request: Request, q: str = "", _auth: AuthUser = Depends(require_auth)):
    """v4.9.0 (Task 6): перенесён из create_app() в web/api.py."""
    if not q or len(q) < 1:
        return JSONResponse([])
    async with async_session() as session:
        stmt = select(User)
        if q.isdigit():
            stmt = stmt.where(User.user_id == int(q))
        else:
            stmt = stmt.where(User.username.ilike(f"%{q}%"))
        stmt = stmt.limit(20)
        users = (await session.execute(stmt)).scalars().all()
    return JSONResponse([
        {
            "user_id": u.user_id,
            "username": u.username,
            "first_name": u.first_name,
            "last_name": u.last_name,
        }
        for u in users
    ])


@router.post("/api/unban")
async def api_unban(
    request: Request,
    punishment_id: int = Form(...),
    user_id: int = Form(...),
    chat_id: int = Form(...),
    reason: str = Form(""),
    _auth: AuthUser = Depends(require_auth),
    bot=Depends(get_bot),
):
    """v4.8.1: API для разбана юзера (вызывается из /admin/bans).

    Делегирует в bot_handlers.revoke_user_ban — ту же функцию использует
    и TG-команда !unban. Это гарантирует паритет:
      • unban_chat_member (Telegram API) с only_if_banned=True.
      • _revoke_last_action (помечает последний активный бан как снятый).
      • _save_punishment с action_type='unban' (видно в веб-панели).

    Возвращает JSON с {ok: True} или {ok: False, error: ...}.

    v4.9.0 (Task 6): перенесён из create_app() в web/api.py, bot приходит
    через Depends(get_bot).
    """
    # v4.8.1: создаём ephemeral aiogram.Bot если основной недоступен.
    # На самом деле, нам нужен экземпляр Bot для вызова unban_chat_member.
    # create_app получает bot как параметр — используем его.
    # Если bot is None — отказ (нельзя разбанить без Bot API токена).
    if bot is None:
        web_app._req_logger.error(
            "api_unban: bot is None — create_app called without bot? "
            "user_id=%s chat_id=%s by=%s",
            user_id, chat_id, _auth.username,
        )
        return JSONResponse(
            {"ok": False, "error": "Bot instance not available — cannot call unban_chat_member"},
            status_code=503,
        )

    # mod_id: для БД нужен ID модератора. У веб-юзера это tg_user_id
    # привязанного Telegram-аккаунта.
    #
    # v4.8.11: раньше здесь стоял `_auth.tg_user_id or -1`. Учётки
    # веб-панели заводятся только через привязку в боте (sync-admins →
    # /start → пароль), поэтому обычный юзер без tg_user_id — нарушение
    # инварианта, а не штатный случай. Fallback на -1 его заминал: разбан
    # проходил, _upsert_moderator заводил несуществующего модератора -1,
    # и на него вешались все такие записи. Теперь такой запрос отклоняется.
    #
    # Исключение — встроенный su: он создаётся сидом init_db (db.py:1372)
    # и логинится по WEB_PASSWORD, TG ID у него нет по построению.
    mod_id = _auth.tg_user_id
    reason_author: str | None = None
    if mod_id is None:
        if _auth.role != "su":
            web_app._req_logger.warning(
                "api_unban: refused — web user %r has no linked tg_user_id "
                "(punishment_id=%s user_id=%s chat_id=%s)",
                _auth.username, punishment_id, user_id, chat_id,
            )
            return JSONResponse(
                {
                    "ok": False,
                    "error": (
                        "Учётка не привязана к Telegram. Привяжите аккаунт "
                        "через бота, иначе разбан некому записать."
                    ),
                },
                status_code=400,
            )
        # su без привязки: mod_id остаётся служебным, поэтому автора
        # сохраняем в тексте причины — иначе он теряется совсем.
        mod_id = web_app._SU_WEB_MOD_ID
        reason_author = _auth.username

    # Импортируем revoke_user_ban (lazy — чтобы не подтягивать весь модуль
    # при импорте web_app).
    try:
        from bot_handlers import revoke_user_ban
    except ImportError as e:
        web_app._req_logger.error("api_unban: cannot import revoke_user_ban: %s", e)
        return JSONResponse(
            {"ok": False, "error": f"Internal error: {e}"},
            status_code=500,
        )

    # Нормализуем reason — пустая строка → None.
    reason_clean = (reason or "").strip() or None
    if reason_author:
        mark = f"через веб-панель: {reason_author}"
        reason_clean = f"{reason_clean} ({mark})" if reason_clean else mark

    # Логируем попытку (до вызова — для аудита даже при падении).
    web_app._req_logger.info(
        "api_unban: attempt — punishment_id=%s user_id=%s chat_id=%s mod_id=%s (by=%s) reason=%r",
        punishment_id, user_id, chat_id, mod_id, _auth.username, reason_clean,
    )

    result = await revoke_user_ban(
        bot=bot,
        chat_id=chat_id,
        user_id=user_id,
        mod_id=mod_id,
        reason=reason_clean,
        target_user=None,  # веб-юзер не имеет types.User объекта
    )

    if result.get("ok"):
        web_app._req_logger.info(
            "api_unban: success — punishment_id=%s user_id=%s chat_id=%s by=%s",
            punishment_id, user_id, chat_id, _auth.username,
        )
        # Redirect обратно на /admin/bans с flash-сообщением.
        flash_msg = f"Разбан выполнен: user_id={user_id}, chat_id={chat_id}"
        if reason_clean:
            flash_msg += f", причина: {reason_clean}"
        return RedirectResponse(
            url=f"/admin/bans?flash={flash_msg}&chat_id={chat_id}",
            status_code=303,
        )
    else:
        web_app._req_logger.warning(
            "api_unban: failed — punishment_id=%s user_id=%s chat_id=%s error=%s",
            punishment_id, user_id, chat_id, result.get("error"),
        )
        flash_msg = f"❌ Ошибка разбана: {result.get('error', 'unknown')}"
        return RedirectResponse(
            url=f"/admin/bans?flash={flash_msg}&chat_id={chat_id}",
            status_code=303,
        )


# ── v4.8.4: API для сброса счётчика автомьютов ──────────────────────
# POST /api/reset-automute-count — обнуляет automute_counters для
# (chat_id, user_id). Доступ: SU/Admin только.
# Параметры: chat_id (int), user_id (int).
# Возвращает JSON {ok: true, old_count: N} или {ok: false, error: ...}.
@router.post("/api/reset-automute-count")
async def api_reset_automute_count(
    request: Request,
    chat_id: int = Form(...),
    user_id: int = Form(...),
    _auth: AuthUser = Depends(require_admin),
):
    """v4.8.4: Сброс счётчика автомьютов (прогрессивные муты).

    Обнуляет count в automute_counters для (chat_id, user_id).
    Формула: mute_duration = base + (count * 60). После сброса
    следующий автомьют будет = base duration (без штрафа).

    v4.9.0 (Task 6): перенесён из create_app() в web/api.py.
    """
    from sqlalchemy import select as _sel
    web_app._req_logger.info(
        "api_reset_automute_count: attempt — chat_id=%s user_id=%s by=%s",
        chat_id, user_id, _auth.username,
    )
    try:
        async with async_session() as session:
            counter = (await session.execute(
                _sel(AutomuteCounter).where(
                    AutomuteCounter.chat_id == chat_id,
                    AutomuteCounter.user_id == user_id,
                )
            )).scalar_one_or_none()
            if counter is None:
                old_count = 0
            else:
                old_count = counter.count
                counter.count = 0
                counter.updated_at = datetime.now(timezone.utc)
                await session.commit()
        web_app._req_logger.info(
            "api_reset_automute_count: success — chat_id=%s user_id=%s "
            "old_count=%d by=%s",
            chat_id, user_id, old_count, _auth.username,
        )
        return JSONResponse({
            "ok": True,
            "old_count": old_count,
            "chat_id": chat_id,
            "user_id": user_id,
        })
    except Exception as e:
        web_app._req_logger.error(
            "api_reset_automute_count: error — chat_id=%s user_id=%s: %s",
            chat_id, user_id, e,
        )
        return JSONResponse(
            {"ok": False, "error": str(e)},
            status_code=500,
        )
