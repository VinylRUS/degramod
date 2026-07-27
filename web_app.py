"""
web_app.py — FastAPI: маршруты, авторизация по кукам (HMAC), Jinja2-шаблоны.

v4.4 — Создание админов через TGID:
  - SU вводит только Telegram ID пользователя.
  - Бот дёргает bot.get_chat(user_id) и подтягивает first_name / last_name / @username.
  - Логин = @username (без @), пароль автогенерируется (16 chars, показывается SU один раз).
  - Юзер может сам сменить пароль через блок на /dashboard.

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
import shutil
import sqlite3
import time
from datetime import datetime, timezone, timedelta

from fastapi import FastAPI, Request, Depends, HTTPException, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func, desc, or_

# Rich Messages (Bot API 10.2 / aiogram 3.30) — для приветствия новому админу.
# Импортируем лениво внутри функции, чтобы не тащить зависимость на aiogram.types
# при статическом импорте модуля (на случай если бот запускается без aiogram).
from aiogram.types import (
    InputRichMessage,
    InputRichBlockSectionHeading,
    InputRichBlockParagraph,
    InputRichBlockFooter,
    RichTextUrl,
    RichTextBold,
    RichTextSpoiler,
)
from aiogram.exceptions import TelegramBadRequest

_req_logger = logging.getLogger("shadow_logger.requests")

from db import (
    async_session, User, Moderator, Punishment, ChatSettings, ChatAdmin, WebUser,
    _hash_password, _verify_password, DB_PATH,
)

# ── Конфигурация ────────────────────────────────────────────────────────────
WEB_PASSWORD = os.getenv("WEB_PASSWORD", "")
COOKIE_NAME = "sl_session"
_SESSION_SECRET = os.getenv("SESSION_SECRET", secrets.token_hex(32))

# МСК таймзона
MSK = timezone(timedelta(hours=3))

PAGE_SIZE = 50  # записей на страницу в дашборде

# ── Публичный URL веб-панели ────────────────────────────────────────────────
# Дублируется из bot_handlers.py намеренно (web_app.py не должен зависеть от
# bot_handlers — там вся логика бота, тут только веб-слой). Значение по умолчанию
# — production-инсталляция на Bothost. Меняется через env только если деплой
# на другой домен.
WEB_PUBLIC_URL = (os.getenv("WEB_PUBLIC_URL") or "https://degraban.bothost.tech").rstrip("/")


# ── Токены сессий ───────────────────────────────────────────────────────────
# Токен = base64url(JSON{u:<username>, s:<is_su 0/1>, r:<role>, t:<issued_ts>, n:<nonce>}) : <hmac>
# HMAC гарантирует целостность — пользователь не может подменить username, is_su или role.
# Поле 'r' (role) добавлено в v4.4.6. Старые токены (без 'r') считаются 'admin'
# если s=0, 'su' если s=1 — для обратной совместимости.

def _sign(value: str) -> str:
    return hmac.new(_SESSION_SECRET.encode(), value.encode(), hashlib.sha256).hexdigest()


def _make_token(username: str, is_su: bool, role: str = "admin") -> str:
    payload = {
        "u": username,
        "s": 1 if is_su else 0,
        "r": role,
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
    """Информация о текущем пользователе, доступная в обработчиках.

    Поля:
      • username — логин в веб-панели
      • is_su    — True только для role='su' (для обратной совместимости)
      • role     — 'su' | 'admin' | 'moderator'
    """
    __slots__ = ("username", "is_su", "role")

    def __init__(self, username: str, is_su: bool, role: str = "admin"):
        self.username = username
        self.is_su = is_su
        # Нормализуем: is_su=True → role='su' (на случай если токен старый без 'r')
        if is_su and role != "su":
            role = "su"
        self.role = role


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
        # Берём роль из БД (а не из токена) — на случай если SU понизил роль
        # после выдачи токена. Токен лишь подтверждает что пользователь залогинился.
        role = wu.role or ("su" if wu.is_su else "admin")
    return AuthUser(username=payload["u"], is_su=(role == "su"), role=role)


async def require_su(request: Request, _: AuthUser = Depends(require_auth)) -> AuthUser:
    """Только SU. Для /admin/users, /admin/cleanup."""
    if _.role != "su":
        raise HTTPException(status_code=303, headers={"Location": "/dashboard"})
    return _


async def require_admin(request: Request, _: AuthUser = Depends(require_auth)) -> AuthUser:
    """SU или admin. Для /admin/moderators, /admin/chats.

    Moderator → redirect на /dashboard.
    """
    if _.role not in ("su", "admin"):
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


# ── v4.4: генерация пароля и signed-flash для показа пароля один раз ────────
_PASSWORD_LEN = 16  # длина автогенерированного пароля (base64url ≈ 107 бит энтропии)


def _generate_password() -> str:
    """Генерирует случайный пароль длиной _PASSWORD_LEN символов (base64url, без padding)."""
    # token_urlsafe(12) даёт 16 символов. Подходит.
    return secrets.token_urlsafe(12)[:_PASSWORD_LEN]


def _sign_flash(payload: dict) -> str:
    """Подписывает payload (dict) для передачи в query string.

    Возвращает base64url(JSON):<hmac>. Payload должен быть коротким.
    Используется для показа пароля один раз после создания юзера.
    """
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    sig = _sign(raw)
    import base64
    b64 = base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
    return f"{b64}.{sig}"


def _verify_flash(token: str, max_age_seconds: int = 120) -> dict | None:
    """Проверяет подпись и свежесть signed-flash. Возвращает payload или None."""
    if not token or "." not in token:
        return None
    b64, sig = token.rsplit(".", 1)
    try:
        import base64
        # восстановим padding
        padding = "=" * (-len(b64) % 4)
        raw = base64.urlsafe_b64decode(b64 + padding).decode()
    except Exception:
        return None
    expected = _sign(raw)
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    ts = payload.get("t", 0)
    if not isinstance(ts, (int, float)) or abs(time.time() - ts) > max_age_seconds:
        return None
    return payload


# ── v4.4.2: Welcome-сообщение новому админу в ЛС ───────────────────────────
# v4.4.6: параметр role — 'admin' | 'moderator'. Текст приветствия адаптируется.
async def _send_admin_welcome(bot, tg_user_id: int, login: str, password: str,
                              first_name: str | None = None,
                              role: str = "admin") -> tuple[bool, str]:
    """Отправляет новому админу/модератору в ЛС приветствие с данными для входа.

    Использует Rich Messages (Bot API 10.2): пароль скрыт под спойлером.

    Возвращает (ok, message):
      - ok=True, message="ok"        — успешно отправлено
      - ok=False, message="<reason>" — отправка не удалась (юзер заблокировал
                                       бота, бот не передан, и т.д.)
    """
    if bot is None:
        return False, "bot is None"

    # Имя для приветствия (если есть)
    greeting_name = ""
    if first_name:
        greeting_name = f", {first_name}"

    web_url = WEB_PUBLIC_URL or "https://degraban.bothost.tech"
    # '/' в конце для человекочитаемой ссылки (без /user/<id>, просто корень)
    web_root_url = web_url + "/"

    # Роль-специфичный заголовок и текст
    if role == "moderator":
        heading = f"🔎 Доступ к веб-панели (модератор){greeting_name}"
        intro_lines = [
            "Вас добавили как модератора в систему «Дедушка Вобжак». ",
            "Веб-панель: ",
            RichTextUrl(text=web_root_url, url=web_root_url),
        ]
        rights_line = (
            "Ваши права: только просмотр логов нарушителей (раздел Dashboard). "
            "Управление админами, чатами и модераторами недоступно."
        )
    else:
        # admin (по умолчанию)
        heading = f"🎉 Доступ к веб-панели (админ){greeting_name}"
        intro_lines = [
            "Вас добавили как админа в систему «Дедушка Вобжак». ",
            "Веб-панель: ",
            RichTextUrl(text=web_root_url, url=web_root_url),
        ]
        rights_line = (
            "Ваши права: управление модераторами чатов и настройками чатов "
            "(хэштег, пороги варнов), а также просмотр логов."
        )

    blocks = [
        InputRichBlockSectionHeading(
            text=heading,
            size=2,
        ),
        InputRichBlockParagraph(text=intro_lines),
        InputRichBlockParagraph(text=rights_line),
        InputRichBlockParagraph(text="Данные для входа (скрыты под спойлером):"),
        # Блок со спойлером — клик по «Показать» раскрывает логин/пароль.
        # Используем RichTextSpoiler с вложенным RichTextBold для выделения
        # самих значений. Telegram показывает спойлер как затемнённый текст,
        # раскрываемый по клику — это безопаснее чем plain text.
        InputRichBlockParagraph(
            text=RichTextSpoiler(
                text=[
                    "Логин: ", RichTextBold(text=login), "\n",
                    "Пароль: ", RichTextBold(text=password),
                ]
            )
        ),
        InputRichBlockParagraph(
            text=[
                "🔐 После первого входа смените пароль: раздел ",
                RichTextBold(text="Dashboard"),
                " → блок ",
                RichTextBold(text="Change my password"),
                " (нужно указать текущий пароль и новый).",
            ]
        ),
        InputRichBlockFooter(
            text=f"⏱ {datetime.now(MSK).strftime('%d.%m.%Y %H:%M')} МСК"
        ),
    ]

    try:
        await bot.send_rich_message(
            chat_id=tg_user_id,
            rich_message=InputRichMessage(blocks=blocks),
        )
        return True, "ok"
    except TelegramBadRequest as e:
        return False, f"TelegramBadRequest: {e}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


# ── Создание приложения ─────────────────────────────────────────────────────
def create_app(lifespan=None, bot=None) -> FastAPI:
    """Создаёт FastAPI-приложение.

    :param lifespan: async context manager для startup/shutdown (передаётся в FastAPI).
    :param bot: экземпляр aiogram.Bot — нужен для эндпоинта создания админа через TGID
                (дёргает bot.get_chat(user_id) для получения профиля из Telegram).
                Если None — эндпоинт /admin/users/create вернёт 503.
    """
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
            token = _make_token("su", is_su=True, role="su")
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
            token = _make_token(wu.username, is_su=wu.is_su, role=wu.role or "admin")
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
        pw_msg: str = "",       # v4.4: сообщение о смене пароля
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
            "pw_msg": pw_msg or None,
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
    #  /admin/users — управление всеми пользователями (v4.4.7)
    #
    #  v4.4.7: Объединяет бывш. Admins + Moderators. Создаётся одна
    #  сущность WebUser с ролью admin/moderator; модераторам при
    #  создании можно сразу назначить чаты (мультивыбор).
    #
    #  Доступ: только SU. (Admin не может создавать других юзеров.)
    # ──────────────────────────────────────────────────────────────────
    @app.get("/admin/users", response_class=HTMLResponse)
    async def admin_users_page(
        request: Request,
        flash: str = "",
        created: str = "",
        _auth: AuthUser = Depends(require_su),
    ):
        created_info = None
        if created:
            payload = _verify_flash(created, max_age_seconds=180)
            if payload and "u" in payload and "p" in payload:
                created_info = {
                    "username": payload["u"],
                    "password": payload["p"],
                    "tg_user_id": payload.get("tg"),
                    "welcome_sent": bool(payload.get("w", 0)),
                }

        async with async_session() as session:
            users = (await session.execute(
                select(WebUser).order_by(WebUser.is_su.desc(), WebUser.created_at.asc())
            )).scalars().all()

            # Для каждого moderator-юзера подгружаем список привязанных чатов
            user_chats: dict[int, list[tuple[int, str | None, str | None]]] = {}
            # {web_user.tg_user_id: [(chat_id, chat_title, hashtag), ...]}
            if users:
                tg_ids = [u.tg_user_id for u in users if u.tg_user_id]
                if tg_ids:
                    rows = (await session.execute(
                        select(
                            ChatAdmin.user_id,
                            ChatAdmin.chat_id,
                            ChatSettings.title,
                            ChatSettings.hashtag,
                        )
                        .outerjoin(ChatSettings, ChatAdmin.chat_id == ChatSettings.chat_id)
                        .where(ChatAdmin.user_id.in_(tg_ids))
                        .order_by(ChatAdmin.user_id, ChatAdmin.chat_id)
                    )).all()
                    for r in rows:
                        user_chats.setdefault(r[0], []).append((r[1], r[2], r[3]))

            # Список всех чатов для мультивыбора (исключая default chat_id=0)
            all_chats = (await session.execute(
                select(ChatSettings.chat_id, ChatSettings.title, ChatSettings.hashtag)
                .where(ChatSettings.chat_id != 0)
                .order_by(ChatSettings.title.asc(), ChatSettings.chat_id.asc())
            )).all()

            # Также подгружаем TG-only модераторов (chat_admins без веб-аккаунта)
            tg_only_moderators = []
            if users:
                web_tg_ids = {u.tg_user_id for u in users if u.tg_user_id}
                ca_rows = (await session.execute(
                    select(
                        ChatAdmin.id,
                        ChatAdmin.chat_id,
                        ChatAdmin.user_id,
                        ChatAdmin.created_at,
                        ChatSettings.title.label("chat_title"),
                        ChatSettings.hashtag.label("chat_hashtag"),
                    )
                    .outerjoin(ChatSettings, ChatAdmin.chat_id == ChatSettings.chat_id)
                    .order_by(ChatAdmin.chat_id.asc(), ChatAdmin.user_id.asc())
                )).all()
                for r in ca_rows:
                    if r[2] not in web_tg_ids:
                        tg_only_moderators.append(r)

        return templates.TemplateResponse("admin.html", {
            "request": request,
            "web_users": users,
            "user_chats": user_chats,
            "all_chats": all_chats,
            "tg_only_moderators": tg_only_moderators,
            "auth_user": _auth,
            "flash": flash or None,
            "created_info": created_info,
        })

    @app.post("/admin/users/create")
    async def admin_users_create(
        request: Request,
        tg_user_id: str = Form(...),
        role: str = Form("admin"),
        chat_ids: list[str] = Form(None),
        _auth: AuthUser = Depends(require_su),
    ):
        """v4.4.7: создаёт веб-пользователя по Telegram ID с ролью admin/moderator.

        role=moderator: дополнительно принимает список chat_ids (мультивыбор) —
        для каждого выбранного чата создаётся запись в chat_admins, что даёт
        модератору право использовать !warn/!mute/!ban в этих чатах.
        role=admin: chat_ids игнорируются (админ имеет права во всех публичных
        чатах автоматически).
        """
        # ── 0. Валидация role ──────────────────────────────────────────
        role = (role or "admin").strip().lower()
        if role not in ("admin", "moderator"):
            return RedirectResponse(
                url="/admin/users?flash=Invalid+role.+Must+be+%27admin%27+or+%27moderator%27",
                status_code=303,
            )

        # ── 1. Валидация TGID ───────────────────────────────────────────
        tg_raw = (tg_user_id or "").strip()
        try:
            tg_id = int(tg_raw)
        except (ValueError, TypeError):
            return RedirectResponse(
                url="/admin/users?flash=Telegram+ID+must+be+a+number",
                status_code=303,
            )
        if tg_id <= 0:
            return RedirectResponse(
                url="/admin/users?flash=Telegram+ID+must+be+positive",
                status_code=303,
            )

        # ── 2. bot должен быть передан ──────────────────────────────────
        if bot is None:
            _req_logger.error("admin_users_create: bot is None — create_app called without bot?")
            return RedirectResponse(
                url="/admin/users?flash=Bot+instance+not+available",
                status_code=303,
            )

        # ── 3. bot.get_chat ─────────────────────────────────────────────
        try:
            chat = await bot.get_chat(chat_id=tg_id)
        except Exception as e:
            _req_logger.warning("admin_users_create: bot.get_chat(%s) failed: %s", tg_id, e)
            return RedirectResponse(
                url="/admin/users?flash=Cannot+fetch+user+from+Telegram.+"
                    "The+user+must+have+interacted+with+the+bot+at+least+once+"
                    "(sent+%2Fstart+or+any+message).",
                status_code=303,
            )

        # ── 4. Профиль из Chat ──────────────────────────────────────────
        tg_username = getattr(chat, "username", None)
        tg_first_name = getattr(chat, "first_name", None)
        tg_last_name = getattr(chat, "last_name", None)
        if not tg_username:
            return RedirectResponse(
                url=f"/admin/users?flash=User+{tg_id}+has+no+%40username+in+Telegram.+"
                    "Cannot+create+login+without+it.",
                status_code=303,
            )
        login = tg_username.strip().lstrip("@").lower()
        if login == "su":
            return RedirectResponse(
                url="/admin/users?flash=%27su%27+is+reserved",
                status_code=303,
            )
        if not (5 <= len(login) <= 32):
            return RedirectResponse(
                url=f"/admin/users?flash=Telegram+username+%27{login}%27+has+invalid+length+"
                    f"(must+be+5-32+chars)",
                status_code=303,
            )

        # ── 5. Парсим выбранные чаты (только для moderator) ─────────────
        chosen_chat_ids: list[int] = []
        if role == "moderator" and chat_ids:
            for raw in chat_ids:
                raw = (raw or "").strip()
                if not raw:
                    continue
                try:
                    cid = int(raw)
                    if cid != 0 and cid not in chosen_chat_ids:
                        chosen_chat_ids.append(cid)
                except (ValueError, TypeError):
                    pass

        # ── 6. Генерация пароля ─────────────────────────────────────────
        password = _generate_password()

        # ── 7. Сохранение ───────────────────────────────────────────────
        async with async_session() as session:
            existing_by_tg = (await session.execute(
                select(WebUser).where(WebUser.tg_user_id == tg_id)
            )).scalar_one_or_none()
            if existing_by_tg:
                return RedirectResponse(
                    url=f"/admin/users?flash=Telegram+ID+{tg_id}+already+bound+to+"
                        f"admin+%27{existing_by_tg.username}%27",
                    status_code=303,
                )
            existing_by_login = (await session.execute(
                select(WebUser).where(WebUser.username == login)
            )).scalar_one_or_none()
            if existing_by_login:
                return RedirectResponse(
                    url=f"/admin/users?flash=Admin+with+username+%27{login}%27+already+exists",
                    status_code=303,
                )

            session.add(WebUser(
                username=login,
                password_hash=_hash_password(password),
                is_su=False,
                is_active=True,
                created_by=_auth.username,
                tg_user_id=tg_id,
                tg_first_name=tg_first_name,
                tg_last_name=tg_last_name,
                tg_username=login,
                role=role,
            ))
            await session.flush()

            # Для moderator — создаём записи в chat_admins
            if role == "moderator" and chosen_chat_ids:
                for cid in chosen_chat_ids:
                    # Проверяем что ещё нет такой записи
                    existing_ca = (await session.execute(
                        select(ChatAdmin).where(
                            ChatAdmin.chat_id == cid,
                            ChatAdmin.user_id == tg_id,
                        )
                    )).scalars().first()
                    if existing_ca is None:
                        session.add(ChatAdmin(
                            chat_id=cid,
                            user_id=tg_id,
                            added_by=None,
                        ))
            await session.commit()

        _req_logger.info(
            "admin_users_create: created web_user username=%s tg_user_id=%s role=%s chats=%s by=%s",
            login, tg_id, role, chosen_chat_ids if role == "moderator" else "(all)",
            _auth.username,
        )

        # ── 8. Welcome-DM ───────────────────────────────────────────────
        welcome_ok, welcome_err = await _send_admin_welcome(
            bot=bot, tg_user_id=tg_id, login=login, password=password,
            first_name=tg_first_name, role=role,
        )
        if welcome_ok:
            _req_logger.info(
                "admin_users_create: welcome sent to tg_user_id=%s (login=%s)",
                tg_id, login,
            )
        else:
            _req_logger.warning(
                "admin_users_create: welcome FAILED for tg_user_id=%s (login=%s): %s",
                tg_id, login, welcome_err,
            )

        flash_token = _sign_flash({
            "u": login, "p": password, "tg": tg_id,
            "t": int(time.time()),
            "w": 1 if welcome_ok else 0,
        })
        return RedirectResponse(url=f"/admin/users?created={flash_token}", status_code=303)

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
                return RedirectResponse(url="/admin/users", status_code=303)
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

    @app.post("/admin/users/{user_id:int}/role")
    async def admin_users_change_role(
        user_id: int,
        request: Request,
        _auth: AuthUser = Depends(require_su),
    ):
        """v4.4.7: меняет роль пользователя admin↔moderator.

        При повышении moderator→admin: удаляем все его записи из chat_admins
        (они больше не нужны — админ имеет права во всех публичных чатах).
        При понижении admin→moderator: записи в chat_admins не трогаем —
        модератор должен будет сам указать чаты через edit-chats (или останется
        с теми чатами, что успел получить до понижения; если их не было —
        он не сможет модерировать ничего, пока SU не привяжет его к чатам).
        """
        form = await request.form()
        new_role = (form.get("role") or "").strip().lower()
        if new_role not in ("admin", "moderator"):
            return RedirectResponse(
                url="/admin/users?flash=Invalid+role",
                status_code=303,
            )
        async with async_session() as session:
            wu = (await session.execute(
                select(WebUser).where(WebUser.id == user_id)
            )).scalar_one_or_none()
            if wu is None or wu.is_su:
                return RedirectResponse(url="/admin/users", status_code=303)
            old_role = wu.role
            if old_role == new_role:
                return RedirectResponse(url="/admin/users", status_code=303)
            wu.role = new_role
            # Повышение moderator→admin: чистим chat_admins
            if old_role == "moderator" and new_role == "admin" and wu.tg_user_id:
                await session.execute(
                    select(ChatAdmin).where(ChatAdmin.user_id == wu.tg_user_id)
                )
                # Удаляем все записи chat_admins для этого юзера
                for ca in (await session.execute(
                    select(ChatAdmin).where(ChatAdmin.user_id == wu.tg_user_id)
                )).scalars().all():
                    await session.delete(ca)
            await session.commit()
        _req_logger.info(
            "admin_users_change_role: user_id=%s %s→%s by=%s",
            user_id, old_role, new_role, _auth.username,
        )
        return RedirectResponse(
            url=f"/admin/users?flash=Role+changed%3A+{old_role}+%E2%86%92+{new_role}",
            status_code=303,
        )

    @app.post("/admin/users/{user_id:int}/edit-chats")
    async def admin_users_edit_chats(
        user_id: int,
        request: Request,
        _auth: AuthUser = Depends(require_su),
    ):
        """v4.4.7: редактирует список чатов, к которым привязан модератор.

        Принимает form-поле chat_ids (множественный выбор). Все ранее привязанные
        чаты, которых нет в новом списке, удаляются. Все новые — добавляются.
        Работает только для пользователей с role=moderator (для admin/SU игнорируется).
        """
        form = await request.form()
        # Получаем список выбранных chat_ids (Form может прийти как list или scalar)
        raw_chats = form.getlist("chat_ids")
        chosen: set[int] = set()
        for raw in raw_chats:
            raw = (raw or "").strip()
            if not raw:
                continue
            try:
                cid = int(raw)
                if cid != 0:
                    chosen.add(cid)
            except (ValueError, TypeError):
                pass

        async with async_session() as session:
            wu = (await session.execute(
                select(WebUser).where(WebUser.id == user_id)
            )).scalar_one_or_none()
            if wu is None or wu.is_su:
                return RedirectResponse(url="/admin/users", status_code=303)
            if wu.role != "moderator" or not wu.tg_user_id:
                return RedirectResponse(
                    url="/admin/users?flash=Edit+chats+only+for+moderator+role",
                    status_code=303,
                )
            # Текущие привязки
            existing = (await session.execute(
                select(ChatAdmin).where(ChatAdmin.user_id == wu.tg_user_id)
            )).scalars().all()
            existing_cids = {ca.chat_id for ca in existing}

            # Удалить те, которых нет в новом списке
            for ca in existing:
                if ca.chat_id not in chosen:
                    await session.delete(ca)
            # Добавить новые
            for cid in chosen:
                if cid not in existing_cids:
                    session.add(ChatAdmin(
                        chat_id=cid, user_id=wu.tg_user_id, added_by=None,
                    ))
            await session.commit()
        _req_logger.info(
            "admin_users_edit_chats: user_id=%s new_chats=%s by=%s",
            user_id, sorted(chosen), _auth.username,
        )
        return RedirectResponse(
            url=f"/admin/users?flash=Chats+updated+for+user+{wu.username}",
            status_code=303,
        )

    @app.post("/admin/users/{user_id:int}/bind-tg")
    async def admin_users_bind_tg(
        user_id: int,
        request: Request,
        _auth: AuthUser = Depends(require_su),
    ):
        """v4.4.7: привязывает/отвязывает TG ID для существующего пользователя.

        Используется SU чтобы привязать свой собственный TG ID к SU-аккаунту
        (для получения DM о новых чатах), либо чтобы перепривязать TG ID
        другого пользователя (если сменил аккаунт).
        """
        form = await request.form()
        tg_raw = (form.get("tg_user_id") or "").strip()
        if not tg_raw:
            # Пустое значение = отвязать
            async with async_session() as session:
                wu = (await session.execute(
                    select(WebUser).where(WebUser.id == user_id)
                )).scalar_one_or_none()
                if wu is None:
                    return RedirectResponse(url="/admin/users", status_code=303)
                if wu.is_su and wu.role == "su":
                    # SU можно отвязать (но тогда он не будет получать DM)
                    pass
                wu.tg_user_id = None
                await session.commit()
            return RedirectResponse(
                url="/admin/users?flash=TG+ID+unbound",
                status_code=303,
            )
        try:
            tg_id = int(tg_raw)
        except (ValueError, TypeError):
            return RedirectResponse(
                url="/admin/users?flash=TG+ID+must+be+a+number",
                status_code=303,
            )
        if tg_id <= 0:
            return RedirectResponse(
                url="/admin/users?flash=TG+ID+must+be+positive",
                status_code=303,
            )

        async with async_session() as session:
            wu = (await session.execute(
                select(WebUser).where(WebUser.id == user_id)
            )).scalar_one_or_none()
            if wu is None:
                return RedirectResponse(url="/admin/users", status_code=303)
            # Проверка что TG ID не занят другим пользователем
            other = (await session.execute(
                select(WebUser).where(
                    WebUser.tg_user_id == tg_id,
                    WebUser.id != user_id,
                )
            )).scalar_one_or_none()
            if other:
                return RedirectResponse(
                    url=f"/admin/users?flash=TG+ID+{tg_id}+already+bound+to+"
                        f"%27{other.username}%27",
                    status_code=303,
                )
            wu.tg_user_id = tg_id
            # Best-effort: обновляем профиль из TG
            if bot is not None:
                try:
                    chat = await bot.get_chat(chat_id=tg_id)
                    wu.tg_first_name = getattr(chat, "first_name", None)
                    wu.tg_last_name = getattr(chat, "last_name", None)
                    tg_un = getattr(chat, "username", None)
                    if tg_un:
                        wu.tg_username = tg_un.strip().lstrip("@").lower()
                except Exception as e:
                    _req_logger.info("bind-tg: bot.get_chat failed (non-critical): %s", e)
            await session.commit()
        _req_logger.info(
            "admin_users_bind_tg: user_id=%s tg_id=%s by=%s",
            user_id, tg_id, _auth.username,
        )
        return RedirectResponse(
            url=f"/admin/users?flash=TG+ID+{tg_id}+bound+to+{wu.username}",
            status_code=303,
        )

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
        _req_logger.info(
            "admin_users_delete: deleted web_user id=%s by=%s",
            user_id, _auth.username,
        )
        return RedirectResponse(url="/admin/users", status_code=303)

    # ──────────────────────────────────────────────────────────────────
    #  /admin/chats — управление настройками чатов (v4.4.7)
    #
    #  v4.4.7: добавлены toggles для is_enabled / is_private / is_report_chat.
    #  Чаты создаются автоматически (ботом при добавлении в чат), здесь —
    #  только редактирование настроек.
    #
    #  Доступ: SU + admin (require_admin). Moderator → redirect на /dashboard.
    # ──────────────────────────────────────────────────────────────────
    @app.get("/admin/chats", response_class=HTMLResponse)
    async def admin_chats_page(
        request: Request,
        flash: str = "",
        _auth: AuthUser = Depends(require_admin),
    ):
        """Страница управления настройками чатов (v4.4.7)."""
        async with async_session() as session:
            stmt = (
                select(ChatSettings)
                .order_by(ChatSettings.chat_id.asc())
            )
            rows = (await session.execute(stmt)).scalars().all()

            # Статистика наказаний
            stats: dict[int, int] = {}
            if rows:
                chat_ids = [r.chat_id for r in rows if r.chat_id != 0]
                if chat_ids:
                    stat_rows = (await session.execute(
                        select(Punishment.chat_id, func.count(Punishment.id))
                        .where(Punishment.chat_id.in_(chat_ids))
                        .group_by(Punishment.chat_id)
                    )).all()
                    stats = {cid: cnt for cid, cnt in stat_rows}

            # Кол-во модераторов (chat_admins) на каждый чат
            mod_counts: dict[int, int] = {}
            if rows:
                chat_ids = [r.chat_id for r in rows if r.chat_id != 0]
                if chat_ids:
                    mc_rows = (await session.execute(
                        select(ChatAdmin.chat_id, func.count(ChatAdmin.id))
                        .where(ChatAdmin.chat_id.in_(chat_ids))
                        .group_by(ChatAdmin.chat_id)
                    )).all()
                    mod_counts = {cid: cnt for cid, cnt in mc_rows}

        return templates.TemplateResponse("admin_chats.html", {
            "request": request,
            "chats": rows,
            "stats": stats,
            "mod_counts": mod_counts,
            "auth_user": _auth,
            "flash": flash or None,
        })

    @app.post("/admin/chats/{chat_id_str}/update")
    async def admin_chats_update(
        chat_id_str: str,
        hashtag: str = Form(""),
        report_chat_id: str = Form(""),
        warns_to_mute: str = Form(""),
        mute_duration_seconds: str = Form(""),
        warns_to_ban: str = Form(""),
        _auth: AuthUser = Depends(require_admin),
    ):
        """Обновляет основные настройки чата (hashtag, thresholds, report_chat_id)."""
        try:
            chat_id = int(chat_id_str)
        except (ValueError, TypeError):
            return RedirectResponse(
                url=f"/admin/chats?flash=Invalid+chat_id+%27{chat_id_str}%27",
                status_code=303,
            )

        def _parse_int(raw: str, field_name: str, min_val: int = 0) -> int | None:
            raw = (raw or "").strip()
            if field_name == "report_chat_id" and raw == "":
                return None
            try:
                v = int(raw)
            except (ValueError, TypeError):
                raise ValueError(f"{field_name} must be a number")
            if v < min_val:
                raise ValueError(f"{field_name} must be >= {min_val}")
            return v

        try:
            wtm = _parse_int(warns_to_mute, "warns_to_mute", 0)
            mdb = _parse_int(mute_duration_seconds, "mute_duration_seconds", 0)
            wtb = _parse_int(warns_to_ban, "warns_to_ban", 0)
            rc = _parse_int(report_chat_id, "report_chat_id", -10**15)
        except ValueError as e:
            return RedirectResponse(
                url=f"/admin/chats?flash={e}",
                status_code=303,
            )

        ht = (hashtag or "").strip()
        if ht and not ht.startswith("#"):
            ht = "#" + ht
        if len(ht) > 64:
            return RedirectResponse(
                url="/admin/chats?flash=Hashtag+too+long+(max+64)",
                status_code=303,
            )

        async with async_session() as session:
            cs = (await session.execute(
                select(ChatSettings).where(ChatSettings.chat_id == chat_id)
            )).scalar_one_or_none()
            if cs is None:
                return RedirectResponse(
                    url=f"/admin/chats?flash=Chat+{chat_id}+not+found",
                    status_code=303,
                )
            cs.hashtag = ht or None
            cs.report_chat_id = rc
            cs.warns_to_mute = wtm if wtm is not None else 0
            cs.mute_duration_seconds = mdb if mdb is not None else 3600
            cs.warns_to_ban = wtb if wtb is not None else 0
            cs.updated_at = datetime.now(timezone.utc)
            await session.commit()

        _req_logger.info(
            "admin_chats_update: chat_id=%s updated by=%s (hashtag=%s, "
            "report_chat_id=%s, warns_to_mute=%s, mute_dur=%s, warns_to_ban=%s)",
            chat_id, _auth.username, ht, rc, wtm, mdb, wtb,
        )
        return RedirectResponse(
            url=f"/admin/chats?flash=Chat+{chat_id}+settings+updated",
            status_code=303,
        )

    @app.post("/admin/chats/{chat_id_str}/toggle")
    async def admin_chats_toggle(
        chat_id_str: str,
        request: Request,
        _auth: AuthUser = Depends(require_admin),
    ):
        """v4.4.7: переключает is_enabled / is_private / is_report_chat для чата.

        Поле form: field=enabled|private|report_chat — что переключать.
        """
        try:
            chat_id = int(chat_id_str)
        except (ValueError, TypeError):
            return RedirectResponse(
                url=f"/admin/chats?flash=Invalid+chat_id",
                status_code=303,
            )
        form = await request.form()
        field = (form.get("field") or "").strip().lower()
        valid_fields = {"enabled", "private", "report_chat"}
        if field not in valid_fields:
            return RedirectResponse(
                url=f"/admin/chats?flash=Invalid+toggle+field",
                status_code=303,
            )

        async with async_session() as session:
            cs = (await session.execute(
                select(ChatSettings).where(ChatSettings.chat_id == chat_id)
            )).scalar_one_or_none()
            if cs is None:
                return RedirectResponse(
                    url=f"/admin/chats?flash=Chat+{chat_id}+not+found",
                    status_code=303,
                )
            if field == "enabled":
                cs.is_enabled = not cs.is_enabled
                msg = f"Chat+{chat_id}+{'enabled' if cs.is_enabled else 'disabled'}"
            elif field == "private":
                cs.is_private = not cs.is_private
                msg = f"Chat+{chat_id}+{'now+private' if cs.is_private else 'now+public'}"
            else:  # report_chat
                if cs.is_report_chat:
                    cs.is_report_chat = False
                    msg = f"Chat+{chat_id}+no+longer+report+chat"
                else:
                    # Снимаем флаг с других чатов (репорт-чат может быть только один)
                    others = (await session.execute(
                        select(ChatSettings).where(
                            ChatSettings.is_report_chat.is_(True),
                            ChatSettings.chat_id != chat_id,
                        )
                    )).scalars().all()
                    for o in others:
                        o.is_report_chat = False
                    cs.is_report_chat = True
                    msg = f"Chat+{chat_id}+is+now+the+report+chat"
            cs.updated_at = datetime.now(timezone.utc)
            await session.commit()
        _req_logger.info(
            "admin_chats_toggle: chat_id=%s field=%s by=%s",
            chat_id, field, _auth.username,
        )
        return RedirectResponse(url=f"/admin/chats?flash={msg}", status_code=303)

    # ──────────────────────────────────────────────────────────────────
    #  /admin/cleanup — безопасная очистка тестовых данных (v4.4.5)
    #
    #  SU-only. Позволяет одним кликом очистить тестовый мусор из БД:
    #    • punishments        — ВСЕ записи (тестовые варны/мьюты/баны)
    #    • users              — нарушители, НЕ являющиеся модераторами
    #    • chat_admins        — опционально (checkbox)
    #  СОХРАНЯЮТСЯ: moderators, web_users, chat_settings.
    #
    #  До удаления — бэкап SQLite-файла в той же папке.
    #  После — VACUUM. Бот продолжает работать (WAL).
    # ──────────────────────────────────────────────────────────────────
    def _cleanup_counts(conn: sqlite3.Connection) -> dict[str, int]:
        """Текущие счётчики для preview. Прямые SELECT'ы — быстро и безопасно."""
        c = {}
        c["punishments"] = conn.execute("SELECT COUNT(*) FROM punishments").fetchone()[0]
        c["users"] = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        c["moderators"] = conn.execute("SELECT COUNT(*) FROM moderators").fetchone()[0]
        c["web_users"] = conn.execute("SELECT COUNT(*) FROM web_users").fetchone()[0]
        c["chat_admins"] = conn.execute("SELECT COUNT(*) FROM chat_admins").fetchone()[0]
        c["chat_settings"] = conn.execute("SELECT COUNT(*) FROM chat_settings").fetchone()[0]
        # users, не являющиеся модераторами (кандидаты на удаление)
        c["users_to_delete"] = conn.execute(
            "SELECT COUNT(*) FROM users WHERE user_id NOT IN (SELECT mod_id FROM moderators)"
        ).fetchone()[0]
        return c

    @app.get("/admin/cleanup", response_class=HTMLResponse)
    async def admin_cleanup_page(
        request: Request,
        flash: str = "",
        _auth: AuthUser = Depends(require_su),
    ):
        """Страница очистки БД. Показывает live-превью + форму подтверждения."""
        # Если файла БД нет — рендерим с заглушкой (тесты / dev-режим)
        if not os.path.exists(DB_PATH):
            counts = {
                "punishments": 0, "users": 0, "moderators": 0,
                "web_users": 0, "chat_admins": 0, "chat_settings": 0,
                "users_to_delete": 0,
            }
        else:
            conn = sqlite3.connect(DB_PATH)
            try:
                counts = _cleanup_counts(conn)
            finally:
                conn.close()

        return templates.TemplateResponse("admin_cleanup.html", {
            "request": request,
            "auth_user": _auth,
            "counts": counts,
            "flash": flash or None,
            "result": None,
            "db_path": DB_PATH,
            "db_path_dir": os.path.dirname(DB_PATH) or ".",
        })

    @app.post("/admin/cleanup")
    async def admin_cleanup_apply(
        request: Request,
        include_chat_admins: str = Form(""),
        _auth: AuthUser = Depends(require_su),
    ):
        """Реальное удаление тестовых данных.

        Шаги:
          1. Проверяем что БД существует.
          2. Проверяем что moderators/web_users не пустые (защита от случайного
             запуска на свежей БД).
          3. Создаём бэкап <DB_PATH>.backup-YYYYMMDD-HHMMSS.db.
          4. DELETE FROM punishments (все).
          5. DELETE FROM users WHERE user_id NOT IN (SELECT mod_id FROM moderators).
          6. Если include_chat_admins — DELETE FROM chat_admins.
          7. VACUUM.
          8. Логируем действие, возвращаем результат на страницу.
        """
        if not os.path.exists(DB_PATH):
            return RedirectResponse(
                url="/admin/cleanup?flash=Database+file+not+found",
                status_code=303,
            )

        # ── 1. Pre-flight counts ────────────────────────────────────
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            before = _cleanup_counts(conn)
        finally:
            conn.close()

        # ── 2. Safety: refuse on empty moderators+web_users ─────────
        if before["moderators"] == 0 and before["web_users"] == 0:
            _req_logger.warning(
                "admin_cleanup: refused — empty moderators+web_users (by=%s)",
                _auth.username,
            )
            return RedirectResponse(
                url="/admin/cleanup?flash=Refused%3A+no+moderators+and+no+web+users+"
                    "+present.+Create+at+least+one+admin+before+cleanup.",
                status_code=303,
            )

        # ── 3. Backup ───────────────────────────────────────────────
        # %f — микросекунды, чтобы избежать коллизий при быстрых повторных вызовах
        ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup_path = f"{DB_PATH}.backup-{ts}.db"
        backup_filename = os.path.basename(backup_path)
        try:
            shutil.copy2(DB_PATH, backup_path)
        except OSError as e:
            _req_logger.error("admin_cleanup: backup failed: %s", e)
            return RedirectResponse(
                url=f"/admin/cleanup?flash=Backup+failed%3A+{e}",
                status_code=303,
            )

        _req_logger.info(
            "admin_cleanup: backup created %s (by=%s, include_chat_admins=%s)",
            backup_path, _auth.username, bool(include_chat_admins),
        )

        # ── 4-7. Delete + VACUUM ────────────────────────────────────
        deleted_punishments = 0
        deleted_users = 0
        deleted_chat_admins: int | None = None
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.execute("PRAGMA foreign_keys=ON")
            with conn:
                cur = conn.execute("DELETE FROM punishments")
                deleted_punishments = cur.rowcount

                cur = conn.execute(
                    "DELETE FROM users WHERE user_id NOT IN "
                    "(SELECT mod_id FROM moderators)"
                )
                deleted_users = cur.rowcount

                if include_chat_admins:
                    cur = conn.execute("DELETE FROM chat_admins")
                    deleted_chat_admins = cur.rowcount

            # VACUUM вне транзакции (SQLite требует)
            conn.isolation_level = None
            conn.execute("VACUUM")
            conn.isolation_level = ""
            conn.close()
        except sqlite3.Error as e:
            _req_logger.error(
                "admin_cleanup: deletion failed: %s (backup=%s)",
                e, backup_path,
            )
            return RedirectResponse(
                url=f"/admin/cleanup?flash=Deletion+failed%3A+{e}.+Restore+from+{backup_filename}",
                status_code=303,
            )

        # ── 8. Post-counts ──────────────────────────────────────────
        conn = sqlite3.connect(DB_PATH)
        try:
            after = _cleanup_counts(conn)
        finally:
            conn.close()

        _req_logger.info(
            "admin_cleanup: done (by=%s) punishments %d→%d, users %d→%d, "
            "chat_admins %d→%d, backup=%s",
            _auth.username,
            before["punishments"], after["punishments"],
            before["users"], after["users"],
            before["chat_admins"], after["chat_admins"],
            backup_filename,
        )

        # Рендерим страницу с блоком результата (не редирект — чтобы
        # пользователь сразу видел итог)
        return templates.TemplateResponse("admin_cleanup.html", {
            "request": request,
            "auth_user": _auth,
            "counts": after,
            "flash": None,
            "result": {
                "deleted_punishments": deleted_punishments,
                "deleted_users": deleted_users,
                "kept_users": after["users"],
                "deleted_chat_admins": deleted_chat_admins,
                "preserved_moderators": after["moderators"],
                "preserved_web_users": after["web_users"],
                "preserved_chat_settings": after["chat_settings"],
                "backup_filename": backup_filename,
                "backup_path": backup_path,
            },
            "db_path": DB_PATH,
            "db_path_dir": os.path.dirname(DB_PATH) or ".",
        })

    # ──────────────────────────────────────────────────────────────────
    #  /me/password — смена своего пароля (v4.4)
    #  Доступен всем авторизованным, но SU пароль хранится в env — ему форма
    #  показывает предупреждение и ничего не делает.
    # ──────────────────────────────────────────────────────────────────
    @app.post("/me/password")
    async def me_change_password(
        request: Request,
        old_password: str = Form(...),
        new_password: str = Form(...),
        confirm: str = Form(...),
        _auth: AuthUser = Depends(require_auth),
    ):
        # SU пароль в env — менять через /me нельзя.
        if _auth.is_su:
            return RedirectResponse(
                url="/dashboard?pw_msg=SU+password+is+managed+via+WEB_PASSWORD+env+variable",
                status_code=303,
            )

        # Валидация
        if len(new_password) < 6:
            return RedirectResponse(
                url="/dashboard?pw_msg=New+password+must+be+at+least+6+chars",
                status_code=303,
            )
        if new_password != confirm:
            return RedirectResponse(
                url="/dashboard?pw_msg=New+password+and+confirmation+do+not+match",
                status_code=303,
            )

        async with async_session() as session:
            wu = (await session.execute(
                select(WebUser).where(WebUser.username == _auth.username)
            )).scalar_one_or_none()
            if wu is None or not wu.is_active or not wu.password_hash:
                return RedirectResponse(
                    url="/dashboard?pw_msg=Account+not+found",
                    status_code=303,
                )
            # Проверяем старый пароль
            if not _verify_password(old_password, wu.password_hash):
                return RedirectResponse(
                    url="/dashboard?pw_msg=Current+password+is+incorrect",
                    status_code=303,
                )
            # Проверяем, что новый пароль отличается от старого
            if _verify_password(new_password, wu.password_hash):
                return RedirectResponse(
                    url="/dashboard?pw_msg=New+password+must+differ+from+current",
                    status_code=303,
                )
            wu.password_hash = _hash_password(new_password)
            await session.commit()

        _req_logger.info(
            "me_change_password: user=%s changed own password",
            _auth.username,
        )
        return RedirectResponse(
            url="/dashboard?pw_msg=Password+changed+successfully",
            status_code=303,
        )

    return app
