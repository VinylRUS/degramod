"""
web_app.py — FastAPI: маршруты, авторизация по кукам (HMAC), Jinja2-шаблоны.

v4.3 — Поддержка нескольких админ-аккаунтов:
  - SU (super-user) логинится через env WEB_PASSWORD
  - SU может создавать/редактировать/удалять/блокировать других админов через /admin/users
  - Сессия хранит username и is_su в подписанном токене
  - /api/dashboard и /api/user/<id>/punishments отдают JSON для автообновления страниц
"""

import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from datetime import datetime, timezone, timedelta

from fastapi import FastAPI, Request, Depends, HTTPException, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func, desc, or_

_req_logger = logging.getLogger("shadow_logger.requests")

from db import (
    async_session, User, Moderator, Punishment, ChatSettings, WebUser,
    _hash_password, _verify_password,
)

# ── Конфигурация ────────────────────────────────────────────────────────────
WEB_PASSWORD = os.getenv("WEB_PASSWORD", "")
COOKIE_NAME = "sl_session"
_SESSION_SECRET = os.getenv("SESSION_SECRET", secrets.token_hex(32))

# МСК таймзона
MSK = timezone(timedelta(hours=3))

PAGE_SIZE = 50  # записей на страницу в дашборде


# ── Токены сессий ───────────────────────────────────────────────────────────
# Токен = base64url(JSON{u:<username>, s:<is_su 0/1>, t:<issued_ts>, n:<nonce>}) : <hmac>
# HMAC гарантирует целостность — пользователь не может подменить username или is_su.

def _sign(value: str) -> str:
    return hmac.new(_SESSION_SECRET.encode(), value.encode(), hashlib.sha256).hexdigest()


def _make_token(username: str, is_su: bool) -> str:
    payload = {
        "u": username,
        "s": 1 if is_su else 0,
        "t": int(time.time()),
        "n": secrets.token_hex(8),
    }
    raw = json.dumps(payload, separators=(",", ":"))
    sig = _sign(raw)
    return f"{raw}:{sig}"


def _verify_token(token: str) -> dict | None:
    """Возвращает payload (dict) или None если токен невалиден."""
    try:
        raw, signature = token.rsplit(":", 1)
        expected = _sign(raw)
        if not hmac.compare_digest(signature, expected):
            return None
        payload = json.loads(raw)
        return payload if {"u", "s", "t"} <= set(payload.keys()) else None
    except (ValueError, json.JSONDecodeError):
        return None


# ── Auth dependency ─────────────────────────────────────────────────────────
class AuthUser:
    __slots__ = ("username", "is_su")

    def __init__(self, username: str, is_su: bool):
        self.username = username
        self.is_su = is_su


async def require_auth(request: Request) -> AuthUser:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    payload = _verify_token(token)
    if not payload:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    # SU-сессия считается активной пока SU-аккаунт не удалён из web_users.
    # Проверяем только что аккаунт существует и активен.
    async with async_session() as session:
        wu = (
            await session.execute(
                select(WebUser).where(WebUser.username == payload["u"])
            )
        ).scalar_one_or_none()
        if wu is None or not wu.is_active:
            raise HTTPException(status_code=303, headers={"Location": "/login"})
    return AuthUser(username=payload["u"], is_su=bool(payload["s"]))


async def require_su(request: Request, _: AuthUser = Depends(require_auth)) -> AuthUser:
    if not _.is_su:
        raise HTTPException(status_code=303, headers={"Location": "/dashboard"})
    return _


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
    if chat_id < 0:
        clean_id = str(chat_id).replace("-100", "").lstrip("-")
        return f"https://t.me/c/{clean_id}"
    else:
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
            _req_logger.info("<<< %s %s → %d (%.0fms)", request.method, request.url.path,
                             response.status_code, elapsed * 1000)
            return response
        except Exception as e:
            elapsed = time.time() - start
            _req_logger.error("!!! %s %s → ERROR %s (%.0fms)", request.method, request.url.path,
                              e, elapsed * 1000)
            raise

    templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))
    templates.env.filters["msk"] = _msk_time
    templates.env.filters["dur"] = _duration_fmt
    templates.env.filters["tglink"] = _telegram_link_base

    # ── Health check ─────────────────────────────────────────────────
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
        username = (form.get("username") or "").strip().lower()
        password = form.get("password", "")

        # SU login — пароль из env WEB_PASSWORD
        if username == "su":
            if not WEB_PASSWORD or password != WEB_PASSWORD:
                return templates.TemplateResponse("login.html", {"request": request, "error": True})
            token = _make_token("su", is_su=True)
            response = RedirectResponse(url="/dashboard", status_code=303)
            response.set_cookie(
                key=COOKIE_NAME, value=token, httponly=True,
                secure=False, samesite="lax", max_age=86400 * 7,
            )
            # Обновляем last_login_at
            async with async_session() as session:
                su_user = (await session.execute(
                    select(WebUser).where(WebUser.username == "su")
                )).scalar_one_or_none()
                if su_user:
                    su_user.last_login_at = datetime.now(timezone.utc)
                    await session.commit()
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
                    or not _verify_password(password, wu.password_hash)):
                return templates.TemplateResponse("login.html", {"request": request, "error": True})
            wu.last_login_at = datetime.now(timezone.utc)
            await session.commit()
            token = _make_token(wu.username, is_su=wu.is_su)
            response = RedirectResponse(url="/dashboard", status_code=303)
            response.set_cookie(
                key=COOKIE_NAME, value=token, httponly=True,
                secure=False, samesite="lax", max_age=86400 * 7,
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
    async def dashboard(
        request: Request,
        page: int = 1,
        action: str = "",
        rev: str = "",          # "all" / "active" / "revoked"; по умолчанию ""
        sort: str = "new",      # "new" / "old" / "type" / "user"
        _auth: AuthUser = Depends(require_auth),
    ):
        offset = (page - 1) * PAGE_SIZE

        async with async_session() as session:
            # ── Общая статистика (все время, только активные) ───────────
            total_stmt = (
                select(Punishment.action_type, func.count(Punishment.id))
                .where(Punishment.is_revoked.is_(False))
                .group_by(Punishment.action_type)
            )
            total_result = await session.execute(total_stmt)
            total_stats = {row[0]: row[1] for row in total_result.all()}
            total_all = sum(total_stats.values())

            # ── Топ нарушителей за 30 дней (только активные) ─────────────
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
                .where(
                    Punishment.created_at >= thirty_days_ago,
                    Punishment.is_revoked.is_(False),
                )
                .group_by(Punishment.user_id)
                .order_by(desc("cnt"))
                .limit(10)
            )
            top_offenders = (await session.execute(top_stmt)).all()

            # ── Топ модераторов за 30 дней (все их действия, incl. unwarn) ──
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
            top_moderators = (await session.execute(mod_stmt)).all()

            # ── Лог санкций: базовый запрос + фильтры ────────────────────
            base = (
                select(Punishment, User, Moderator)
                .join(User, Punishment.user_id == User.user_id)
                .join(Moderator, Punishment.mod_id == Moderator.mod_id)
            )

            # Action filter
            if action in ("mute", "warn", "ban", "unmute", "unwarn", "unban"):
                base = base.where(Punishment.action_type == action)

            # Revoked filter: по умолчанию показываем всё ("")
            if rev == "active":
                base = base.where(Punishment.is_revoked.is_(False))
            elif rev == "revoked":
                base = base.where(Punishment.is_revoked.is_(True))

            # Sorting
            if sort == "old":
                base = base.order_by(Punishment.created_at.asc())
            elif sort == "type":
                base = base.order_by(Punishment.action_type.asc(),
                                     Punishment.created_at.desc())
            elif sort == "user":
                base = base.order_by(User.username.asc().nullslast(),
                                     Punishment.created_at.desc())
            else:  # "new" / default
                base = base.order_by(Punishment.created_at.desc())

            # ── Count total для пагинации (с теми же фильтрами) ─────────
            count_base = (
                select(func.count(Punishment.id))
                .join(User, Punishment.user_id == User.user_id)
            )
            if action in ("mute", "warn", "ban", "unmute", "unwarn", "unban"):
                count_base = count_base.where(Punishment.action_type == action)
            if rev == "active":
                count_base = count_base.where(Punishment.is_revoked.is_(False))
            elif rev == "revoked":
                count_base = count_base.where(Punishment.is_revoked.is_(True))
            total_row_count = (await session.execute(count_base)).scalar() or 0
            total_pages = max(1, (total_row_count + PAGE_SIZE - 1) // PAGE_SIZE)

            rows = (await session.execute(
                base.offset(offset).limit(PAGE_SIZE)
            )).all()

            # ── Чат-настройки ──────────────────────────────────────────────
            settings_result = await session.execute(select(ChatSettings))
            chat_settings = settings_result.scalars().all()

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
            "default_report_chat_id": default_report_chat_id,
            "action_filter": action,
            "rev_filter": rev,
            "sort": sort,
            "auth_user": _auth,
        })

    # ── GET /user/<user_id> ─────────────────────────────────────────────
    @app.get("/user/{user_id:int}", response_class=HTMLResponse)
    async def user_page(
        request: Request,
        user_id: int,
        action: str = "",
        rev: str = "",
        _auth: AuthUser = Depends(require_auth),
    ):
        async with async_session() as session:
            user = (await session.execute(
                select(User).where(User.user_id == user_id)
            )).scalar_one_or_none()
            if user is None:
                raise HTTPException(status_code=404)

            # Счётчики: только активные (is_revoked=False)
            count_stmt = (
                select(Punishment.action_type, func.count(Punishment.id))
                .where(
                    Punishment.user_id == user_id,
                    Punishment.is_revoked.is_(False),
                )
                .group_by(Punishment.action_type)
            )
            counters = {
                row[0]: row[1]
                for row in (await session.execute(count_stmt)).all()
            }

            # Текущие варны (только активные)
            warn_sum_stmt = (
                select(func.coalesce(func.sum(Punishment.duration_seconds), 0))
                .where(
                    Punishment.user_id == user_id,
                    Punishment.action_type == "warn",
                    Punishment.is_revoked.is_(False),
                )
            )
            current_warns = int((await session.execute(warn_sum_stmt)).scalar() or 0)

            # История
            punishment_stmt = (
                select(Punishment, Moderator)
                .join(Moderator, Punishment.mod_id == Moderator.mod_id)
                .where(Punishment.user_id == user_id)
                .order_by(desc(Punishment.created_at))
            )
            if action in ("mute", "warn", "ban", "unmute", "unwarn", "unban"):
                punishment_stmt = punishment_stmt.where(Punishment.action_type == action)
            if rev == "active":
                punishment_stmt = punishment_stmt.where(Punishment.is_revoked.is_(False))
            elif rev == "revoked":
                punishment_stmt = punishment_stmt.where(Punishment.is_revoked.is_(True))
            punishments = (await session.execute(punishment_stmt)).all()

        return templates.TemplateResponse("user.html", {
            "request": request,
            "user": user,
            "counters": counters,
            "current_warns": current_warns,
            "punishments": punishments,
            "action_filter": action,
            "rev_filter": rev,
            "auth_user": _auth,
        })

    # ── GET /api/dashboard — JSON для автообновления таблицы логов ───────
    @app.get("/api/dashboard")
    async def api_dashboard(
        request: Request,
        page: int = 1,
        action: str = "",
        rev: str = "",
        sort: str = "new",
        last: int = 0,    # id последней показанной записи — вернём только свежее
        _auth: AuthUser = Depends(require_auth),
    ):
        offset = (page - 1) * PAGE_SIZE
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
            rows = (await session.execute(base.offset(offset).limit(PAGE_SIZE))).all()

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
                    "time": _msk_time(p.created_at),
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
                    "duration_fmt": _duration_fmt(p.duration_seconds) if p.action_type != "warn" else f"{p.duration_seconds or 0} pts",
                    "reason": p.reason,
                    "message_text": p.message_text,
                }
                for p, u, m in rows
            ],
        })

    # ── GET /api/search?q=<query> ──────────────────────────────────────
    @app.get("/api/search")
    async def api_search(request: Request, q: str = "", _auth: AuthUser = Depends(require_auth)):
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

    # ──────────────────────────────────────────────────────────────────
    #  /admin/users — управление админ-аккаунтами (только SU)
    # ──────────────────────────────────────────────────────────────────
    @app.get("/admin/users", response_class=HTMLResponse)
    async def admin_users_page(
        request: Request,
        flash: str = "",
        _auth: AuthUser = Depends(require_su),
    ):
        async with async_session() as session:
            users = (await session.execute(
                select(WebUser).order_by(WebUser.is_su.desc(), WebUser.created_at.asc())
            )).scalars().all()
        return templates.TemplateResponse("admin.html", {
            "request": request,
            "web_users": users,
            "auth_user": _auth,
            "flash": flash or None,
        })

    @app.post("/admin/users/create")
    async def admin_users_create(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
        _auth: AuthUser = Depends(require_su),
    ):
        username = username.strip().lower()
        if username == "su":
            return RedirectResponse(url="/admin/users?flash=%27su%27+is+reserved", status_code=303)
        if not username or len(username) < 3 or len(username) > 32:
            return RedirectResponse(url="/admin/users?flash=Username+must+be+3-32+chars", status_code=303)
        if len(password) < 6:
            return RedirectResponse(url="/admin/users?flash=Password+must+be+at+least+6+chars", status_code=303)
        async with async_session() as session:
            existing = (await session.execute(
                select(WebUser).where(WebUser.username == username)
            )).scalar_one_or_none()
            if existing:
                return RedirectResponse(
                    url=f"/admin/users?flash=User+%27{username}%27+already+exists",
                    status_code=303,
                )
            session.add(WebUser(
                username=username,
                password_hash=_hash_password(password),
                is_su=False,
                is_active=True,
                created_by=_auth.username,
            ))
            await session.commit()
        return RedirectResponse(url="/admin/users", status_code=303)

    @app.post("/admin/users/{user_id:int}/toggle")
    async def admin_users_toggle(
        user_id: int,
        _auth: AuthUser = Depends(require_su),
    ):
        async with async_session() as session:
            wu = (await session.execute(
                select(WebUser).where(WebUser.id == user_id)
            )).scalar_one_or_none()
            if wu is None:
                return RedirectResponse(url="/admin/users", status_code=303)
            if wu.is_su:
                return RedirectResponse(url="/admin/users", status_code=303)  # SU нельзя блокировать
            wu.is_active = not wu.is_active
            await session.commit()
        return RedirectResponse(url="/admin/users", status_code=303)

    @app.post("/admin/users/{user_id:int}/reset")
    async def admin_users_reset(
        request: Request,
        user_id: int,
        password: str = Form(...),
        _auth: AuthUser = Depends(require_su),
    ):
        if len(password) < 6:
            return RedirectResponse(url="/admin/users?flash=Password+must+be+at+least+6+chars", status_code=303)
        async with async_session() as session:
            wu = (await session.execute(
                select(WebUser).where(WebUser.id == user_id)
            )).scalar_one_or_none()
            if wu is None or wu.is_su:
                return RedirectResponse(url="/admin/users", status_code=303)
            wu.password_hash = _hash_password(password)
            await session.commit()
        return RedirectResponse(url="/admin/users", status_code=303)

    @app.post("/admin/users/{user_id:int}/delete")
    async def admin_users_delete(
        user_id: int,
        _auth: AuthUser = Depends(require_su),
    ):
        async with async_session() as session:
            wu = (await session.execute(
                select(WebUser).where(WebUser.id == user_id)
            )).scalar_one_or_none()
            if wu is None or wu.is_su:
                return RedirectResponse(url="/admin/users", status_code=303)
            await session.delete(wu)
            await session.commit()
        return RedirectResponse(url="/admin/users", status_code=303)

    return app
