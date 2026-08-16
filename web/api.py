"""
web/api.py — v4.8.10: JSON API роуты (вынесены из create_app как PoC).

Сюда переезжают все /api/* JSON-эндпоинты. Они не используют templates
и bot — только async_session + module-level helpers из web_app.py.

v4.8.10: перенесены /api/presets и /api/automute-count.
Остальные /api/* (/api/dashboard, /api/search, /api/unban,
/api/reset-automute-count) — TODO v4.9.0 (используют вложенные helpers
из create_app, нужно сначала вынести их на уровень модуля).
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from db import AutomuteCounter, PermissionPreset, async_session
from web.deps import AuthUser, require_admin, require_auth

# Module-level helpers из web_app.py (логгер).
from web_app import _req_logger

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
        _req_logger.error(
            "api_get_automute_count: error — chat_id=%s user_id=%s: %s",
            chat_id, user_id, e,
        )
        return JSONResponse(
            {"ok": False, "error": str(e)},
            status_code=500,
        )
