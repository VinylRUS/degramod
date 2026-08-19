"""
web/me.py — v4.8.10: роуты пользователя (вынесены из create_app как PoC).

Сюда переезжают роуты /avatar/* и / (root redirect). Они простые —
не используют templates и bot.

v4.8.10: перенесены / (root) и /avatar/{tg_user_id}.
Остальные /me/*, /dashboard, /user/* — TODO v4.9.0 (используют templates и вложенные helpers из create_app).
"""
from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, RedirectResponse

from web.deps import AuthUser, require_auth
from web_app import _avatar_path

router = APIRouter()


@router.get("/")
async def root():
    """Root → редирект на /login.

    v4.8.10: перенесён из create_app() в web/me.py.
    """
    return RedirectResponse(url="/login", status_code=302)


@router.get("/avatar/{tg_user_id:int}")
async def get_avatar(tg_user_id: int, _auth: AuthUser = Depends(require_auth)):
    """Отдаёт файл аватарки <AVATARS_DIR>/<tg_user_id>.jpg.

    v4.5.1: добавлена проверка require_auth — чтобы посторонние не могли
    перебирать tg_user_id и тащить аватарки.

    v4.8.10: перенесён из create_app() в web/me.py.
    """
    path = _avatar_path(tg_user_id)
    if not os.path.exists(path):
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="image/jpeg")
