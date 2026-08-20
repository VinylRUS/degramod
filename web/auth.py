"""
web/auth.py — роуты входа и выхода.

v4.8.9: вынесены /logout (POST и GET) как proof-of-concept декомпозиции.
v4.9.0 (Task 10): добавлены GET и POST /login.

Хелперы и константы берутся через модуль web_app (web_app._client_ip и
т.д.), а не импортом имён: тесты патчат атрибуты модуля, и при
`from web_app import ...` патч промахнулся бы мимо уже связанного имени.
"""
from __future__ import annotations

import hmac
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

import web_app
from db import WebUser, async_session
from web.deps import COOKIE_NAME, get_templates

router = APIRouter()


# ── GET /login ──────────────────────────────────────────────────────
@router.get("/login", response_class=HTMLResponse)
async def login_page(
    request: Request,
    templates: Jinja2Templates = Depends(get_templates),
):
    return templates.TemplateResponse("login.html", {"request": request, "error": False})


# ── POST /login ─────────────────────────────────────────────────────
@router.post("/login")
async def login_submit(
    request: Request,
    templates: Jinja2Templates = Depends(get_templates),
):
    # v4.5.1: rate-limit по IP — 5 попыток за 5 минут
    ip = web_app._client_ip(request)
    if not web_app._check_login_rate_limit(ip):
        web_app._req_logger.warning("login rate-limited for ip=%s", ip)
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "error": True,
                "error_msg": "Too many login attempts. Try again in 5 minutes.",
            },
            status_code=429,
        )

    form = await request.form()
    username = (form.get("username") or "").strip().lower()
    password = form.get("password", "")

    # SU login — пароль из env WEB_PASSWORD
    if username == "su":
        # v4.8.7: константное сравнение (hmac.compare_digest) вместо
        # != — закрывает теоретический timing attack на SU-пароль.
        # Для обычных админов PBKDF2 с 200k итераций уже даёт
        # ~60ms на проверку, что шумит timing — там != было ок.
        # SU пароль хранится в env как plaintext (без хеша), поэтому
        # сравнение должно быть константным.
        if not web_app.WEB_PASSWORD or not hmac.compare_digest(password, web_app.WEB_PASSWORD):
            return templates.TemplateResponse("login.html", {"request": request, "error": True})
        token = web_app._make_token("su", is_su=True, role="su")
        response = RedirectResponse(url="/dashboard", status_code=303)
        response.set_cookie(
            key=COOKIE_NAME, value=token, httponly=True,
            secure=web_app._COOKIE_SECURE, samesite="lax", max_age=86400 * 7,
        )
        # Обновляем last_login_at (v4.7.8: обёрнуто в try/except —
        # обновление метрики не должно блокировать логин. Если БД
        # повреждена или колонка отсутствует — логируем и идём дальше).
        try:
            async with async_session() as session:
                su_user = (await session.execute(
                    select(WebUser).where(WebUser.username == "su")
                )).scalar_one_or_none()
                if su_user:
                    su_user.last_login_at = datetime.now(timezone.utc)
                    await session.commit()
        except Exception as e:
            web_app._req_logger.exception("login: failed to update su.last_login_at: %s", e)
        return response

    # Обычный админ — проверяем по web_users
    if not username:
        return templates.TemplateResponse("login.html", {"request": request, "error": True})
    async with async_session() as session:
        wu = (await session.execute(
            select(WebUser).where(WebUser.username == username)
        )).scalar_one_or_none()
        if (wu is None or not wu.is_active
                or not wu.password_hash
                or not web_app._verify_password(password, wu.password_hash)):
            return templates.TemplateResponse("login.html", {"request": request, "error": True})
        # v4.7.8: last_login_at update — обёрнут в try/except аналогично SU.
        try:
            wu.last_login_at = datetime.now(timezone.utc)
            await session.commit()
        except Exception as e:
            web_app._req_logger.exception("login: failed to update %s.last_login_at: %s", username, e)
        token = web_app._make_token(wu.username, is_su=wu.is_su, role=wu.role or "admin")
        response = RedirectResponse(url="/dashboard", status_code=303)
        response.set_cookie(
            key=COOKIE_NAME, value=token, httponly=True,
            secure=web_app._COOKIE_SECURE, samesite="lax", max_age=86400 * 7,
        )
        return response


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
