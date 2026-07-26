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

from db import async_session, User, Moderator, Punishment, ChatSettings

# ── Конфигурация ────────────────────────────────────────────────────────────
WEB_PASSWORD = os.getenv("WEB_PASSWORD", "")
COOKIE_NAME = "sl_session"
_SESSION_SECRET = os.getenv("SESSION_SECRET", secrets.token_hex(32))

# МСК таймзона
MSK = timezone(timedelta(hours=3))

PAGE_SIZE = 50  # записей на страницу в дашборде


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


# ── Jinja2 filters ──────────────────────────────────────────────────────────
def _msk_time(dt: datetime | None) -> str:
    """Конвертирует UTC datetime в МСК строку."""
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    msk_dt = dt.astimezone(MSK)
    return msk_dt.strftime("%d.%m.%Y %H:%M")


def _duration_fmt(seconds: int | None) -> str:
    """Форматирует секунды длительности в человекочитаемый вид."""
    if seconds is None:
        return "—"
    if seconds == 0:
        return "0"
    parts = []
    days = seconds // 86400
    if days:
        parts.append(f"{days}д")
    hours = (seconds % 86400) // 3600
    if hours:
        parts.append(f"{hours}ч")
    mins = (seconds % 3600) // 60
    if mins:
        parts.append(f"{mins}м")
    return "".join(parts) if parts else "0"


def _telegram_link_base(chat_id: int) -> str:
    """Конструирует базовую ссылку на Telegram канал/чат (без message_id).

    Используется как Jinja2-фильтр: {{ chat_id | tglink }}/{{ msg_id }}
    Для приватных каналов: https://t.me/c/<id_without_-100>
    """
    if chat_id < 0:
        # Приватный канал/супергруппа: убираем префикс -100
        clean_id = str(chat_id).replace("-100", "").lstrip("-")
        return f"https://t.me/c/{clean_id}"
    else:
        # Публичная группа/канал — числовой ID
        return f"https://t.me/c/{chat_id}"


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
    templates.env.filters["msk"] = _msk_time
    templates.env.filters["dur"] = _duration_fmt
    templates.env.filters["tglink"] = _telegram_link_base

    # ── Health check (для Bothost proxy, без авторизации) ─────────────
    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "service": "dedushka-vobzhak",
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
    async def dashboard(request: Request, page: int = 1, _=Depends(require_auth)):
        offset = (page - 1) * PAGE_SIZE

        async with async_session() as session:
            # ── Общая статистика (все время) ────────────────────────────────
            total_stmt = (
                select(Punishment.action_type, func.count(Punishment.id))
                .group_by(Punishment.action_type)
            )
            total_result = await session.execute(total_stmt)
            total_stats = {row[0]: row[1] for row in total_result.all()}
            total_all = sum(total_stats.values())

            # ── Топ нарушителей за 30 дней ──────────────────────────────────
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

            # ── Топ модераторов за 30 дней ──────────────────────────────────
            mod_stmt = (
                select(
                    Punishment.mod_id,
                    Moderator.username,
                    Moderator.first_name,
                    func.count(Punishment.id).label("cnt"),
                )
                .join(Moderator, Punishment.mod_id == Moderator.mod_id)
                .where(Punishment.created_at >= thirty_days_ago)
                .group_by(Punishment.mod_id)
                .order_by(desc("cnt"))
                .limit(10)
            )
            mod_result = await session.execute(mod_stmt)
            top_moderators = mod_result.all()

            # ── Лог санкций (с пагинацией) ──────────────────────────────────
            count_stmt = select(func.count(Punishment.id))
            total_rows_result = await session.execute(count_stmt)
            total_row_count = total_rows_result.scalar() or 0
            total_pages = max(1, (total_row_count + PAGE_SIZE - 1) // PAGE_SIZE)

            stmt = (
                select(Punishment, User, Moderator)
                .join(User, Punishment.user_id == User.user_id)
                .join(Moderator, Punishment.mod_id == Moderator.mod_id)
                .order_by(desc(Punishment.created_at))
                .offset(offset)
                .limit(PAGE_SIZE)
            )
            result = await session.execute(stmt)
            rows = result.all()

            # ── Чат-хэштеги ──────────────────────────────────────────────────
            settings_stmt = select(ChatSettings)
            settings_result = await session.execute(settings_stmt)
            chat_settings = settings_result.scalars().all()

            # ── Карта chat_id → report_chat_id (из ChatSettings) ────────────
            report_chat_map = {
                cs.chat_id: cs.report_chat_id
                for cs in chat_settings
                if cs.report_chat_id is not None
            }

            # ── Глобальный default report_chat_id (chat_id=0 в ChatSettings) ──
            default_rc_row = (
                await session.execute(
                    select(ChatSettings).where(ChatSettings.chat_id == 0)
                )
            ).scalar_one_or_none()
            default_report_chat_id = default_rc_row.report_chat_id if default_rc_row else None

        return templates.TemplateResponse("dashboard.html", {
            "request": request,
            "rows": rows,
            "top_offenders": top_offenders,
            "top_moderators": top_moderators,
            "total_stats": total_stats,
            "total_all": total_all,
            "page": page,
            "total_pages": total_pages,
            "page_size": PAGE_SIZE,
            "chat_settings": chat_settings,
            "report_chat_map": report_chat_map,
            "default_report_chat_id": default_report_chat_id,
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

            # ── Текущие варны (сумма duration_seconds для warn) ──────────
            warn_sum_stmt = (
                select(func.coalesce(func.sum(Punishment.duration_seconds), 0))
                .where(
                    Punishment.user_id == user_id,
                    Punishment.action_type == "warn",
                )
            )
            warn_result = await session.execute(warn_sum_stmt)
            current_warns = int(warn_result.scalar() or 0)

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

            # ── Карта chat_id → report_chat_id (из ChatSettings) ────────────
            settings_stmt = select(ChatSettings)
            settings_result = await session.execute(settings_stmt)
            all_settings = settings_result.scalars().all()
            report_chat_map = {
                cs.chat_id: cs.report_chat_id
                for cs in all_settings
                if cs.report_chat_id is not None
            }

            # ── Глобальный default report_chat_id (chat_id=0 в ChatSettings) ──
            default_rc_row = (
                await session.execute(
                    select(ChatSettings).where(ChatSettings.chat_id == 0)
                )
            ).scalar_one_or_none()
            default_report_chat_id = default_rc_row.report_chat_id if default_rc_row else None

        return templates.TemplateResponse("user.html", {
            "request": request,
            "user": user,
            "counters": counters,
            "current_warns": current_warns,
            "punishments": punishments,
            "action_filter": action_filter,
            "report_chat_map": report_chat_map,
            "default_report_chat_id": default_report_chat_id,
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
