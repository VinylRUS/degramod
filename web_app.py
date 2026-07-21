"""
web_app.py — FastAPI: маршруты, авторизация по кукам (HMAC), Jinja2-шаблоны.
Минимальная конфигурация для Bothost: без TrustedHostMiddleware, без кастомных middleware.
"""

import hashlib
import hmac
import logging
import os
import secrets
import time
from datetime import datetime, timezone, timedelta

from fastapi import FastAPI, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func, desc

_req_logger = logging.getLogger("shadow_logger.requests")

from db import async_session, User, Moderator, Punishment

# ── Конфигурация ────────────────────────────────────────────────────────────
WEB_PASSWORD = os.getenv("WEB_PASSWORD", "")
COOKIE_NAME = "sl_session"
_SESSION_SECRET = os.getenv("SESSION_SECRET", secrets.token_hex(32))


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
def create_app(lifespan=None) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    # ── Request logging middleware ────────────────────────────────────
    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        start = time.time()
        _req_logger.info(">>> %s %s", request.method, request.url.path)
        try:
            response = await call_next(request)
            elapsed = time.time() - start
            _req_logger.info("<<< %s %s → %d (%.0fms)", request.method, request.url.path, response.status_code, elapsed * 1000)
            return response
        except Exception as e:
            elapsed = time.time() - start
            _req_logger.error("!!! %s %s → ERROR %s (%.0fms)", request.method, request.url.path, e, elapsed * 1000)
            raise

    templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

    # ── Health check (для Bothost proxy, без авторизации) ─────────────
    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "service": "shadow-logger",
            "time": datetime.now(timezone.utc).isoformat(),
        }

    # ── Root → редирект на login ────────────────────────────────────────
    @app.get("/")
    async def root():
        return RedirectResponse(url="/login", status_code=302)

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
        response.set_cookie(
            key=COOKIE_NAME,
            value=token,
            httponly=True,
            secure=False,      # Bothost проксирует SSL, куки должны работать и по HTTP
            samesite="lax",
            max_age=86400 * 7,
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
            stmt = (
                select(Punishment, User, Moderator)
                .join(User, Punishment.user_id == User.user_id)
                .join(Moderator, Punishment.mod_id == Moderator.mod_id)
                .order_by(desc(Punishment.created_at))
                .limit(50)
            )
            result = await session.execute(stmt)
            rows = result.all()

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
            user_stmt = select(User).where(User.user_id == user_id)
            user_result = await session.execute(user_stmt)
            user = user_result.scalar_one_or_none()
            if user is None:
                raise HTTPException(status_code=404)

            count_stmt = (
                select(Punishment.action_type, func.count(Punishment.id))
                .where(Punishment.user_id == user_id)
                .group_by(Punishment.action_type)
            )
            count_result = await session.execute(count_stmt)
            counters = {row[0]: row[1] for row in count_result.all()}

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
