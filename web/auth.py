"""
web/auth.py — v4.8.9: роуты /logout (вынесены из create_app как PoC).

POST /logout — реальный logout (удаляет cookie).
GET /logout — legacy редирект на /login (для старых закладок).

POST /login и GET /login пока остаются в create_app() — они используют
много общего state (rate-limit, _check_login_rate_limit, _client_ip).
Перенос — TODO v4.9.0.
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import RedirectResponse

from web.deps import COOKIE_NAME

router = APIRouter()


@router.post("/logout")
async def logout():
    """POST /logout — удаляет cookie сессии, редирект на /login.

    v4.5.1: было GET /logout, но GET уязвим к CSRF через <img src="/logout">.
    POST + SameSite=lax cookie полностью закрывают этот вектор.
    Шаблон base.html использует <form method="post" action="/logout">.
    """
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(COOKIE_NAME)
    return response


@router.get("/logout")
async def logout_legacy():
    """GET /logout — редирект на /login без удаления cookie.

    Для старых закладок. Не вылогинивает — чтобы случайно зашедший по старой
    ссылке не потерял сессию без действия.
    """
    return RedirectResponse(url="/login", status_code=303)
