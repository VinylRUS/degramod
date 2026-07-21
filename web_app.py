"""
web_app.py — FastAPI: маршруты, авторизация по кукам (HMAC), Jinja2-шаблоны.
"""

import hashlib
import hmac
import os
import secrets
from datetime import datetime, timezone, timedelta

from fastapi import FastAPI, Request, Response, Depends, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func, desc

from db import async_session, User, Moderator, Punishment

# ── Конфигурация ────────────────────────────────────────────────────────────
WEB_PASSWORD = os.getenv("WEB_PASSWORD", "")
COOKIE_NAME = "sl_session"
# Секрет для подписи кук — генерируется при старте
_SESSION_SECRET = os.getenv("SESSION_SECRET", secrets.token_hex(32))
_ALLOWED_HOSTS = os.getenv("ALLOWED_HOSTS", "").split(",") if os.getenv("ALLOWED_HOSTS") else []


def _sign(value: str) -> str:
    """HMAC-SHA256 подпись значения."""
    return hmac.new(_SESSION_SECRET.encode(), value.encode(), hashlib.sha256).hexdigest()


def _verify_token(token: str) -> bool:
    """Проверка: token = <random_hex>:<signature>"""
    try:
        payload, signature = token.rsplit(":", 1)
        expected = _sign(payload)
        return hmac.compare_digest(signature, expected)
    except ValueError:
        return False


def _make_token() -> str:
    payload = secrets.token_hex(32)
    return f"{payload}:{_sign(payload)}"


# ── Auth dependency ─────────────────────────────────────────────────────────
async def require_auth(request: Request) -> None:
    token = request.cookies.get(COOKIE_NAME)
    if not token or not _verify_token(token):
        raise HTTPException(status_code=303, headers={"Location": "/login"})


# ── Создание приложения ─────────────────────────────────────────────────────
def create_app() -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    # Middleware: Trusted Host
    if _ALLOWED_HOSTS and _ALLOWED_HOSTS != [""]:
        from starlette.middleware.trustedhost import TrustedHostMiddleware
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=_ALLOWED_HOSTS)

    # Bothost проксирует через HTTPS — пробрасываем X-Forwarded-* заголовки
    @app.middleware("http")
    async def proxy_headers_middleware(request: Request, call_next):
        x_forwarded_proto = request.headers.get("x-forwarded-proto", "")
        x_forwarded_host = request.headers.get("x-forwarded-host", "")
        if x_forwarded_proto:
            request.scope["scheme"] = x_forwarded_proto
        if x_forwarded_host:
            request.scope["headers"] = [
                (k, v) for k, v in request.scope.get("headers", [])
                if k != b"host"
            ] + [(b"host", x_forwarded_host.encode())]
        response = await call_next(request)
        return response

    templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

    # ── GET /login ──────────────────────────────────────────────────────
    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        return templates.TemplateResponse("login.html", {"request": request, "error": False})

    # ── POST /login ─────────────────────────────────────────────────────
    @app.post("/login")
    async def login_submit(request: Request):
        form = await request.form()
        password = form.get("password", "")
        if password != WEB_PASSWORD:
            return templates.TemplateResponse("login.html", {"request": request, "error": True})
        token = _make_token()
        response = RedirectResponse(url="/dashboard", status_code=303)
        # Определяем, работает ли приложение за HTTPS-прокси
        forwarded_proto = request.headers.get("x-forwarded-proto", "http")
        is_secure = forwarded_proto == "https"
        response.set_cookie(
            key=COOKIE_NAME,
            value=token,
            httponly=True,
            secure=is_secure,
            samesite="lax",
            max_age=86400 * 7,  # 7 дней
        )
        return response

    # ── GET /logout ─────────────────────────────────────────────────────
    @app.get("/logout")
    async def logout():
        response = RedirectResponse(url="/login", status_code=303)
        response.delete_cookie(COOKIE_NAME)
        return response

    # ── GET /dashboard ──────────────────────────────────────────────────
    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard(request: Request, _=Depends(require_auth)):
        async with async_session() as session:
            # Последние 50 наказаний
            stmt = (
                select(Punishment, User, Moderator)
                .join(User, Punishment.user_id == User.user_id)
                .join(Moderator, Punishment.mod_id == Moderator.mod_id)
                .order_by(desc(Punishment.created_at))
                .limit(50)
            )
            result = await session.execute(stmt)
            rows = result.all()

            # Топ нарушителей за 30 дней
            thirty_days_ago = datetime.now(timezone.utc) - timedelta(days=30)
            top_stmt = (
                select(
                    Punishment.user_id,
                    User.username,
                    User.first_name,
                    User.last_name,
                    func.count(Punishment.id).label("cnt"),
                )
                .join(User, Punishment.user_id == User.user_id)
                .where(Punishment.created_at >= thirty_days_ago)
                .group_by(Punishment.user_id)
                .order_by(desc("cnt"))
                .limit(10)
            )
            top_result = await session.execute(top_stmt)
            top_offenders = top_result.all()

        return templates.TemplateResponse("dashboard.html", {
            "request": request,
            "rows": rows,
            "top_offenders": top_offenders,
        })

    # ── GET /user/<user_id> ─────────────────────────────────────────────
    @app.get("/user/{user_id:int}", response_class=HTMLResponse)
    async def user_page(request: Request, user_id: int, _=Depends(require_auth)):
        async with async_session() as session:
            # Профиль
            user_stmt = select(User).where(User.user_id == user_id)
            user_result = await session.execute(user_stmt)
            user = user_result.scalar_one_or_none()
            if user is None:
                raise HTTPException(status_code=404)

            # Счётчики
            count_stmt = (
                select(Punishment.action_type, func.count(Punishment.id))
                .where(Punishment.user_id == user_id)
                .group_by(Punishment.action_type)
            )
            count_result = await session.execute(count_stmt)
            counters = {row[0]: row[1] for row in count_result.all()}

            # Фильтр по action_type
            action_filter = request.query_params.get("action", "")
            punishment_stmt = (
                select(Punishment, Moderator)
                .join(Moderator, Punishment.mod_id == Moderator.mod_id)
                .where(Punishment.user_id == user_id)
                .order_by(desc(Punishment.created_at))
            )
            if action_filter in ("mute", "warn", "ban", "unmute"):
                punishment_stmt = punishment_stmt.where(Punishment.action_type == action_filter)
            punishment_result = await session.execute(punishment_stmt)
            punishments = punishment_result.all()

        return templates.TemplateResponse("user.html", {
            "request": request,
            "user": user,
            "counters": counters,
            "punishments": punishments,
            "action_filter": action_filter,
        })

    # ── GET /api/search?q=<query> ──────────────────────────────────────
    @app.get("/api/search")
    async def api_search(request: Request, q: str = "", _=Depends(require_auth)):
        if not q or len(q) < 1:
            return JSONResponse([])
        async with async_session() as session:
            stmt = select(User)
            if q.isdigit():
                stmt = stmt.where(User.user_id == int(q))
            else:
                stmt = stmt.where(User.username.ilike(f"%{q}%"))
            stmt = stmt.limit(20)
            result = await session.execute(stmt)
            users = result.scalars().all()
        return JSONResponse([
            {
                "user_id": u.user_id,
                "username": u.username,
                "first_name": u.first_name,
                "last_name": u.last_name,
            }
            for u in users
        ])

    return app
