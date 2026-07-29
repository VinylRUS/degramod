"""
web_app.py — FastAPI: маршруты, авторизация по кукам (HMAC), Jinja2-шаблоны.

v4.5 — Редизайн дашборда + Profile + Settings:
  - Дашборд сокращён: Search + 4 stat-карточки (Total/Mutes/Warns/Bans) + Recent sanctions
    с 4 фильтрами (All/Mute/Warn/Ban). Убраны: top offenders/moderators, chat-settings
    (дублировал /admin/chats), change-pw (переехал в /me), anchor-nav.
  - Новый /me (Profile): аватарка из ТГ, инфа об аккаунте, форма смены пароля,
    Refresh-avatar кнопка. Модераторам — инструкция по смене пароля через DM боту.
  - Новый /admin/settings (SU-only): Cleanup + Bot info + Backup now + VACUUM.
    Старый /admin/cleanup делает редирект на /admin/settings#cleanup.
  - Аватарки веб-юзеров: bot.get_user_profile_photos + bot.download → локальный
    файл <data_dir>/avatars/<tg_user_id>.jpg. Отдаются через /avatar/<tg_user_id>.
    Скачиваются при создании юзера, при bind-tg и по кнопке Refresh.
  - Навбар: у кнопки Logout — микро-аватарка + логин текущего юзера (лаконично).

v4.4 — Создание админов через TGID:
  - SU вводит только Telegram ID пользователя.
  - Бот дёргает bot.get_chat(user_id) и подтягивает first_name / last_name / @username.
  - Логин = @username (без @), пароль автогенерируется (16 chars, показывается SU один раз).
  - Юзер сам меняет пароль через /me (v4.5: было /dashboard).

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
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse
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

# ── v4.5: Версия приложения ────────────────────────────────────────────────
# v4.5.5 (29 июля 2026): Проверка прав бота при добавлении в чат —
# my_chat_member извлекает admin privileges, сохраняет bot_rights_missing
# (JSON list), шлёт DM всем Admin/SU если прав не хватает. Веб-панель
# показывает бейдж "⚠️ RIGHTS" + кнопка Recheck. Fallback: stealth_catchall
# при первом сообщении дёргает bot.get_chat_member если my_chat_member
# не пришёл.
# v4.5.4 (29 июля 2026): Санитарные дни — lockdown чата на заданные даты.
# ChatPermissions → all False; модераторов не касается (Telegram admin rights
# override'ят chat-level perms). Ночной режим пропускает чаты в sanitary day.
# Управление: /sanitary CLI command + веб-панель /admin/chats (textarea).
APP_VERSION = "v4.5.5"
APP_RELEASE_DATE = "2026-07-29"

# ── v4.5: Папка для аватарок ───────────────────────────────────────────────
# Хранится в <data_dir>/avatars/ (рядом с БД — переживает пересоздание контейнера
# если data_dir смонтирован как volume). Создаётся при старте.
_DATA_DIR = os.path.dirname(os.getenv("DB_PATH", "/app/data/shadow_logs.db"))
AVATARS_DIR = os.path.join(_DATA_DIR, "avatars")

# ── v4.5.1: Rate-limit на /login (in-memory, по IP) ────────────────────────
# Простая защита от брутфорса паролей админов. Не персистентная (сбрасывается
# при рестарте процесса) — для нашего сценария достаточно.
# Параметры: 5 попыток за 5-минутное окно, потом — 429 до конца окна.
_LOGIN_RATELIMIT_MAX = 5
_LOGIN_RATELIMIT_WINDOW = 300  # 5 минут
_login_attempts: dict[str, list[float]] = {}  # {ip: [timestamps]}


def _check_login_rate_limit(ip: str) -> bool:
    """Возвращает True если IP ещё в рамках лимита, False если превысил."""
    now = time.time()
    attempts = _login_attempts.get(ip, [])
    # Чистим старые
    attempts = [t for t in attempts if now - t < _LOGIN_RATELIMIT_WINDOW]
    if len(attempts) >= _LOGIN_RATELIMIT_MAX:
        _login_attempts[ip] = attempts
        return False
    attempts.append(now)
    _login_attempts[ip] = attempts
    return True


def _client_ip(request: Request) -> str:
    """Достаёт IP клиента из request. Учитывает X-Forwarded-For (за прокси)."""
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        # Берём первый IP из списка
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _wal_checkpoint() -> None:
    """v4.5.1: принудительный checkpoint WAL-файла SQLite в основной DB-файл.

    Без этого `shutil.copy2(DB_PATH, ...)` в WAL-режиме копирует только
    основной файл; свежие записи остаются в -wal файле и в бэкап не попадают.
    PRAGMA wal_checkpoint(TRUNCATE) сбрасывает всё в основной файл и обнуляет
    WAL. Дешёвая операция, безопасна для параллельных запросов (SQLite сам
    разруливает локи). Лучший момент для вызова — прямо перед копированием.
    """
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
    except sqlite3.Error as e:
        _req_logger.warning("wal_checkpoint failed: %s (continuing)", e)

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
      • username    — логин в веб-панели
      • is_su       — True только для role='su' (для обратной совместимости)
      • role        — 'su' | 'admin' | 'moderator'
      • tg_user_id  — Telegram user ID (для аватарки; None если не привязан)
      • avatar_url  — URL аватарки для <img src=...> (None если нет аватарки)
    """
    __slots__ = ("username", "is_su", "role", "tg_user_id", "avatar_url")

    def __init__(self, username: str, is_su: bool, role: str = "admin",
                 tg_user_id: int | None = None, avatar_url: str | None = None):
        self.username = username
        self.is_su = is_su
        # Нормализуем: is_su=True → role='su' (на случай если токен старый без 'r')
        if is_su and role != "su":
            role = "su"
        self.role = role
        self.tg_user_id = tg_user_id
        self.avatar_url = avatar_url


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
        avatar_url = _avatar_url(wu.tg_user_id, wu.tg_photo_updated_at)
    return AuthUser(
        username=payload["u"],
        is_su=(role == "su"),
        role=role,
        tg_user_id=wu.tg_user_id,
        avatar_url=avatar_url,
    )


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


# v4.5.2: фильтр для отображения preset имени ночного режима по JSON-снапшоту
def _night_mode_preset_name(perms_json: str | None) -> str:
    """Распознаёт preset ('text_only' / 'strict' / 'none' / 'custom') по JSON-снапшоту прав."""
    if not perms_json:
        return "text_only"
    try:
        data = json.loads(perms_json)
    except (ValueError, TypeError):
        return "text_only"
    # Все True → none (без ограничений)
    if all(data.get(k, False) for k in (
        "can_send_messages", "can_send_audios", "can_send_documents",
        "can_send_photos", "can_send_videos", "can_send_video_notes",
        "can_send_voice_notes", "can_send_polls", "can_send_other_messages",
        "can_add_web_page_previews",
    )):
        return "none"
    # Все False → strict (полный мьют)
    if not any(data.get(k, False) for k in (
        "can_send_messages", "can_send_audios", "can_send_documents",
        "can_send_photos", "can_send_videos", "can_send_video_notes",
        "can_send_voice_notes", "can_send_polls", "can_send_other_messages",
    )):
        return "strict"
    # can_send_messages=True, остальное False → text_only
    if data.get("can_send_messages", False) and not any(
        data.get(k, False) for k in (
            "can_send_audios", "can_send_documents", "can_send_photos",
            "can_send_videos", "can_send_video_notes", "can_send_voice_notes",
            "can_send_polls", "can_send_other_messages",
        )
    ):
        return "text_only"
    return "custom"


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
                RichTextBold(text="Profile"),
                " (ссылка в правом верхнем углу) → блок ",
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


# ── v4.5: Хелперы для аватарок ──────────────────────────────────────────────

def _avatar_path(tg_user_id: int) -> str:
    """Локальный путь к файлу аватарки данного юзера."""
    return os.path.join(AVATARS_DIR, f"{tg_user_id}.jpg")


def _avatar_url(tg_user_id: int | None, photo_updated_at: datetime | None) -> str | None:
    """URL для <img src=...>. Возвращает None если аватарки нет (шаблон покажет placeholder).

    Параметр ?v=<ts> — cache-buster: после обновления аватарки timestamp меняется,
    и браузер перекачивает картинку.
    """
    if not tg_user_id:
        return None
    if not os.path.exists(_avatar_path(tg_user_id)):
        return None
    # Используем photo_updated_at если есть, иначе mtime файла
    if photo_updated_at is not None:
        # Нормализуем tz
        if photo_updated_at.tzinfo is None:
            photo_updated_at = photo_updated_at.replace(tzinfo=timezone.utc)
        ts = int(photo_updated_at.timestamp())
    else:
        try:
            ts = int(os.path.getmtime(_avatar_path(tg_user_id)))
        except OSError:
            ts = 0
    return f"/avatar/{tg_user_id}?v={ts}"


async def _fetch_and_save_avatar(bot, tg_user_id: int) -> bool:
    """Скачивает аватарку пользователя из Telegram и сохраняет локально.

    Возвращает True если аватарка успешно скачана и сохранена,
    False — если у юзера нет аватарки или произошла ошибка.
    Не бросает исключений (best-effort).

    Использует bot.get_user_profile_photos → берём самый большой размер →
    bot.get_file → HTTP-GET по file_path. Сохраняем как JPEG.
    """
    if bot is None or not tg_user_id:
        return False
    try:
        photos = await bot.get_user_profile_photos(user_id=tg_user_id, limit=1)
    except Exception as e:
        _req_logger.info("avatar fetch failed for tg_user_id=%s: %s", tg_user_id, e)
        return False
    if not photos or not photos.total_count or not photos.photos:
        return False
    # photos.photos[0] — список размеров для последнего фото; берём последний (самый большой)
    sizes = photos.photos[0]
    if not sizes:
        return False
    biggest = sizes[-1]
    try:
        file = await bot.get_file(file_id=biggest.file_id)
    except Exception as e:
        _req_logger.info("avatar get_file failed for tg_user_id=%s: %s", tg_user_id, e)
        return False
    if not file.file_path:
        return False
    try:
        # bot.download() в aiogram 3.x возвращает bytes при destination=None
        data = await bot.download(file=file.file_path, destination=None)
    except Exception as e:
        _req_logger.info("avatar download failed for tg_user_id=%s: %s", tg_user_id, e)
        return False
    if data is None:
        return False
    # aiogram 3.x: bot.download может вернуть BytesIO или bytes
    if hasattr(data, "read"):
        data = data.read()
    if not data:
        return False
    # Сохраняем
    try:
        os.makedirs(AVATARS_DIR, exist_ok=True)
        with open(_avatar_path(tg_user_id), "wb") as f:
            f.write(data)
    except OSError as e:
        _req_logger.warning("avatar save failed for tg_user_id=%s: %s", tg_user_id, e)
        return False
    return True


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
    templates.env.filters["night_mode_preset_name"] = _night_mode_preset_name
    # v4.5.3: from_json filter — парсит JSON-строку в dict для шаблона.
    # Используется в admin_chats.html для чтения night_mode_permissions JSON.
    templates.env.filters["from_json"] = lambda s: json.loads(s) if s else {}
    # v4.5.4: format_sanitary_days filter — конвертирует JSON sanitary_days
    # в multiline-текст для textarea (одна дата/диапазон на строку).
    try:
        from bot_handlers import format_sanitary_days_textarea as _fmt_san
        templates.env.filters["format_sanitary_days"] = _fmt_san
    except ImportError:
        templates.env.filters["format_sanitary_days"] = lambda s: ""
    # v4.5.5: parse_bot_rights filter — парсит JSON bot_rights_missing в list[str].
    # Возвращает [] для None/пустой/битой строки (fail-safe в шаблоне).
    try:
        from bot_handlers import parse_bot_rights_missing as _parse_bot_rights
        templates.env.filters["parse_bot_rights"] = _parse_bot_rights
    except ImportError:
        templates.env.filters["parse_bot_rights"] = lambda s: []
    # v4.5.2: глобальные переменные для всех шаблонов (версия в футере)
    templates.env.globals["app_version"] = APP_VERSION
    templates.env.globals["app_release_date"] = APP_RELEASE_DATE

    # v4.5: создаём папку для аватарок при старте (если ещё нет)
    try:
        os.makedirs(AVATARS_DIR, exist_ok=True)
    except OSError as e:
        _req_logger.warning("create_app: cannot create AVATARS_DIR %s: %s", AVATARS_DIR, e)

    # ── Health check ─────────────────────────────────────────────────
    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "service": "dedushka-vobzhak",
            "version": APP_VERSION,
            "time": datetime.now(timezone.utc).isoformat(),
        }

    # ── v4.5: Endpoint для отдачи аватарок ────────────────────────────
    # Возвращает файл <AVATARS_DIR>/<tg_user_id>.jpg. Если файла нет —
    # 404 (шаблон должен проверить avatar_url перед рендером <img>).
    # v4.5.1: добавлена проверка require_auth — чтобы посторонние не могли
    # перебирать tg_user_id и тащить аватарки. Шаблоны используют тот же
    # домен, так что браузер автоматически шлёт cookie с сессией.
    @app.get("/avatar/{tg_user_id:int}")
    async def get_avatar(tg_user_id: int, _auth: AuthUser = Depends(require_auth)):
        path = _avatar_path(tg_user_id)
        if not os.path.exists(path):
            raise HTTPException(status_code=404)
        return FileResponse(path, media_type="image/jpeg")

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
        # v4.5.1: rate-limit по IP — 5 попыток за 5 минут
        ip = _client_ip(request)
        if not _check_login_rate_limit(ip):
            _req_logger.warning("login rate-limited for ip=%s", ip)
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

    # ── POST /logout (v4.5.1: было GET, изменили на POST) ──────────────
    # GET /logout был CSRF-уязвим: любой сайт с <img src=".../logout">
    # разлогинивал юзера. POST + SameSite=lax cookie полностью закрывают
    # этот вектор (для POST-запроса с другого origin браузер не шлёт cookie).
    # Шаблон base.html использует <form method="post" action="/logout">.
    @app.post("/logout")
    async def logout():
        response = RedirectResponse(url="/login", status_code=303)
        response.delete_cookie(COOKIE_NAME)
        return response

    # GET /logout — редирект на /login (для старых закладок). Не вылогинивает,
    # просто редиректит — чтобы случайно зашедший по старой ссылке не потерял
    # сессию без действия.
    @app.get("/logout")
    async def logout_legacy():
        return RedirectResponse(url="/login", status_code=303)

    # ── GET /dashboard ──────────────────────────────────────────────────
    # v4.5: урезанный дашборд. Только:
    #   • Search user (поиск нарушителя)
    #   • 4 stat-карточки: Total / Mutes / Warns / Bans
    #   • Recent sanctions — лог с 4 фильтрами (All/Mute/Warn/Ban)
    # Убрано: top offenders/moderators, chat-settings (дублировал /admin/chats),
    # change-pw (переехал в /me), anchor-nav (нечего навигировать).
    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard(
        request: Request,
        page: int = 1,
        action: str = "",
        rev: str = "",          # "all" / "active" / "revoked"; по умолчанию ""
        sort: str = "new",      # "new" / "old" / "type" / "user"
        pw_msg: str = "",       # legacy: оставлен для редиректов от старой /me/password
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

            # ── Лог санкций: базовый запрос + фильтры ────────────────────
            base = (
                select(Punishment, User, Moderator)
                .join(User, Punishment.user_id == User.user_id)
                .join(Moderator, Punishment.mod_id == Moderator.mod_id)
            )

            # Action filter (v4.5: в UI осталось 4 кнопки — All/Mute/Warn/Ban.
            # URL с action=unmute/unwarn/unban по-прежнему работает для прямых
            # ссылок, но в дашборде кнопок для этого нет.)
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

        return templates.TemplateResponse("dashboard.html", {
            "request": request,
            "rows": rows,
            "total_stats": total_stats,
            "total_all": total_all,
            "page": page,
            "total_pages": total_pages,
            "page_size": PAGE_SIZE,
            "action_filter": action,
            "rev_filter": rev,
            "sort": sort,
            "auth_user": _auth,
            "app_version": APP_VERSION,
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

        # ── 8. v4.5: Скачиваем аватарку (best-effort) ───────────────────
        avatar_ok = await _fetch_and_save_avatar(bot, tg_id)
        if avatar_ok:
            async with async_session() as session:
                wu_for_avatar = (await session.execute(
                    select(WebUser).where(WebUser.tg_user_id == tg_id)
                )).scalar_one_or_none()
                if wu_for_avatar is not None:
                    wu_for_avatar.tg_photo_updated_at = datetime.now(timezone.utc)
                    await session.commit()
            _req_logger.info("admin_users_create: avatar saved for tg_user_id=%s", tg_id)
        else:
            _req_logger.info("admin_users_create: no avatar for tg_user_id=%s (skipped)", tg_id)

        # ── 9. Welcome-DM ───────────────────────────────────────────────
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
        # v4.5: скачиваем аватарку для привязанного TG ID (best-effort)
        avatar_ok = await _fetch_and_save_avatar(bot, tg_id)
        if avatar_ok:
            async with async_session() as session:
                wu_for_avatar = (await session.execute(
                    select(WebUser).where(WebUser.id == user_id)
                )).scalar_one_or_none()
                if wu_for_avatar is not None:
                    wu_for_avatar.tg_photo_updated_at = datetime.now(timezone.utc)
                    await session.commit()
            _req_logger.info("admin_users_bind_tg: avatar saved for tg_id=%s", tg_id)
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
            # v4.5.1: чистим chat_admins для tg_user_id удаляемого юзера.
            # Раньше запись оставалась в БД, и _is_admin через fallback
            # продолжал давать доступ «TG-only модератору» — фактически
            # удалённый аккаунт сохранял модеративные права.
            if wu.tg_user_id:
                cas = (await session.execute(
                    select(ChatAdmin).where(ChatAdmin.user_id == wu.tg_user_id)
                )).scalars().all()
                for ca in cas:
                    await session.delete(ca)
            await session.delete(wu)
            await session.commit()
        _req_logger.info(
            "admin_users_delete: deleted web_user id=%s by=%s "
            "(also cleared chat_admins for tg_user_id=%s)",
            user_id, _auth.username, wu.tg_user_id,
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

            # v4.5.1: список доступных репорт-чатов (is_report_chat=True)
            # — для dropdown в шаблоне admin_chats.html вместо свободного
            # текстового поля. Так SU не может ввести несуществующий chat_id
            # и удивляться, что отчёты не приходят.
            report_chat_options = [
                {
                    "chat_id": r.chat_id,
                    "title": r.title or f"(id {r.chat_id})",
                    "hashtag": r.hashtag,
                }
                for r in rows
                if r.is_report_chat and r.chat_id != 0
            ]

        return templates.TemplateResponse("admin_chats.html", {
            "request": request,
            "chats": rows,
            "stats": stats,
            "mod_counts": mod_counts,
            "report_chat_options": report_chat_options,
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
        warn_decay_days: str = Form(""),
        link_filter_action: str = Form("delete"),
        night_mode_start: str = Form("23:00"),
        night_mode_end: str = Form("07:00"),
        night_mode_preset: str = Form("text_only"),
        # v4.5.3: расширенная настройка ночного режима.
        night_mode_tz: str = Form("Europe/Moscow"),
        night_mode_weekend_start: str = Form(""),
        night_mode_weekend_end: str = Form(""),
        night_mode_notify: str = Form(""),
        night_mode_notify_enter_msg: str = Form(""),
        night_mode_notify_exit_msg: str = Form(""),
        # Custom permissions grid (10 чекбоксов). В HTML unchecked чекбоксы
        # НЕ отправляются — поэтому "" → False, "on" → True.
        # Эти поля используются ТОЛЬКО когда night_mode_preset == "custom".
        perm_can_send_messages: str = Form(""),
        perm_can_send_audios: str = Form(""),
        perm_can_send_documents: str = Form(""),
        perm_can_send_photos: str = Form(""),
        perm_can_send_videos: str = Form(""),
        perm_can_send_video_notes: str = Form(""),
        perm_can_send_voice_notes: str = Form(""),
        perm_can_send_polls: str = Form(""),
        perm_can_send_other_messages: str = Form(""),
        perm_can_add_web_page_previews: str = Form(""),
        # v4.5.4: sanitary days textarea. Multiline-текст, одна запись на
        # строку ('YYYY-MM-DD' или 'YYYY-MM-DD - YYYY-MM-DD').
        sanitary_days_text: str = Form(""),
        _auth: AuthUser = Depends(require_admin),
    ):
        """Обновляет настройки чата (включая v4.5.2: warn decay, link filter, night mode)."""
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
            decay = _parse_int(warn_decay_days, "warn_decay_days", 0)
        except ValueError as e:
            return RedirectResponse(
                url=f"/admin/chats?flash={e}",
                status_code=303,
            )

        # v4.5.2: валидация link_filter_action и night mode preset
        if link_filter_action not in ("delete", "warn", "mute", "ban"):
            return RedirectResponse(
                url="/admin/chats?flash=Invalid+link_filter_action",
                status_code=303,
            )
        if night_mode_preset not in ("text_only", "strict", "none", "custom"):
            return RedirectResponse(
                url="/admin/chats?flash=Invalid+night_mode_preset",
                status_code=303,
            )

        # v4.5.2: валидация HH:MM для night mode
        import re as _re
        _hhmm_re = _re.compile(r"^([01][0-9]|2[0-3]):([0-5][0-9])$")
        nm_start = (night_mode_start or "").strip()
        nm_end = (night_mode_end or "").strip()
        if not _hhmm_re.match(nm_start):
            return RedirectResponse(
                url=f"/admin/chats?flash=Invalid+night_mode_start+(use+HH:MM)",
                status_code=303,
            )
        if not _hhmm_re.match(nm_end):
            return RedirectResponse(
                url=f"/admin/chats?flash=Invalid+night_mode_end+(use+HH:MM)",
                status_code=303,
            )

        # v4.5.3: валидация tz (IANA timezone).
        nm_tz = (night_mode_tz or "Europe/Moscow").strip()
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(nm_tz)
        except (ValueError, KeyError):
            return RedirectResponse(
                url=f"/admin/chats?flash=Invalid+night_mode_tz+(use+IANA+name+like+Europe/Moscow)",
                status_code=303,
            )

        # v4.5.3: валидация weekend schedule (опционально).
        # Если одно из полей задано — оба обязательны.
        nm_wknd_start = (night_mode_weekend_start or "").strip()
        nm_wknd_end = (night_mode_weekend_end or "").strip()
        if (nm_wknd_start or nm_wknd_end) and not (nm_wknd_start and nm_wknd_end):
            return RedirectResponse(
                url=f"/admin/chats?flash=Weekend+schedule+requires+both+start+and+end",
                status_code=303,
            )
        if nm_wknd_start and not _hhmm_re.match(nm_wknd_start):
            return RedirectResponse(
                url=f"/admin/chats?flash=Invalid+night_mode_weekend_start+(use+HH:MM)",
                status_code=303,
            )
        if nm_wknd_end and not _hhmm_re.match(nm_wknd_end):
            return RedirectResponse(
                url=f"/admin/chats?flash=Invalid+night_mode_weekend_end+(use+HH:MM)",
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

        # v4.5.2: строим JSON-снапшот permissions по preset
        # Импортируем хелпер из bot_handlers (не идеально — web_app зависит от bot_handlers,
        # но это уже было раньше: web_app импортирует from db).
        # Чтобы не тащить aiogram.types в web_app, делаем permissions dict inline.
        _ALL_PERM_KEYS = (
            "can_send_messages", "can_send_audios", "can_send_documents",
            "can_send_photos", "can_send_videos", "can_send_video_notes",
            "can_send_voice_notes", "can_send_polls", "can_send_other_messages",
            "can_add_web_page_previews", "can_change_info", "can_invite_users",
            "can_pin_messages",
        )
        if night_mode_preset == "strict":
            night_perms_json = json.dumps({k: False for k in _ALL_PERM_KEYS})
        elif night_mode_preset == "none":
            night_perms_json = json.dumps({k: True for k in _ALL_PERM_KEYS})
        elif night_mode_preset == "custom":
            # v4.5.3: строим JSON из 10 чекбоксов. can_change_info/invite/pin
            # не редактируются через UI (для простоты) — берём из текущего
            # состояния чата, не трогаем. Ставим False если у чата нет preserve.
            # UI показывает только 10 «отправляемых» прав; остальные 3
            # (change_info/invite/pin) — копируем из ранее сохранённого JSON,
            # иначе False.
            # Это безопасно: ночной режим ограничивает отправку, не админ-права.
            custom_flags = {
                "can_send_messages":          perm_can_send_messages == "on",
                "can_send_audios":            perm_can_send_audios == "on",
                "can_send_documents":         perm_can_send_documents == "on",
                "can_send_photos":            perm_can_send_photos == "on",
                "can_send_videos":            perm_can_send_videos == "on",
                "can_send_video_notes":       perm_can_send_video_notes == "on",
                "can_send_voice_notes":       perm_can_send_voice_notes == "on",
                "can_send_polls":             perm_can_send_polls == "on",
                "can_send_other_messages":   perm_can_send_other_messages == "on",
                "can_add_web_page_previews": perm_can_add_web_page_previews == "on",
                # Эти 3 не редактируются через UI — ставим False (ночной режим
                # не должен давать обычным юзерам админ-права).
                "can_change_info":            False,
                "can_invite_users":           False,
                "can_pin_messages":           False,
            }
            night_perms_json = json.dumps(custom_flags)
        else:  # text_only (default)
            night_perms_json = json.dumps({
                "can_send_messages": True,
                "can_send_audios": False, "can_send_documents": False,
                "can_send_photos": False, "can_send_videos": False,
                "can_send_video_notes": False, "can_send_voice_notes": False,
                "can_send_polls": False, "can_send_other_messages": False,
                "can_add_web_page_previews": False,
                "can_change_info": False, "can_invite_users": False,
                "can_pin_messages": False,
            })

        # v4.5.4: парсим sanitary_days textarea. Поддерживаем:
        #   'YYYY-MM-DD'              — однодневный санитарный день
        #   'YYYY-MM-DD:YYYY-MM-DD'   — диапазон
        #   'YYYY-MM-DD - YYYY-MM-DD' — диапазон с пробелами
        # Строки с '#' в начале — комментарии. Пустые строки игнорируются.
        # Невалидные строки → redirect с ошибкой (вместо тихого пропуска),
        # чтобы SU видел что именно не так.
        try:
            from bot_handlers import (
                parse_sanitary_days_textarea, serialize_sanitary_days,
            )
        except ImportError:
            return RedirectResponse(
                url=f"/admin/chats?flash=Server+error+(bot_handlers+import+failed)",
                status_code=303,
            )
        san_pairs, san_errors = parse_sanitary_days_textarea(sanitary_days_text)
        if san_errors:
            # Беру первую ошибку — для flash-сообщения.
            first_err = san_errors[0].replace(" ", "+")
            return RedirectResponse(
                url=f"/admin/chats?flash=Sanitary+days:+{first_err}",
                status_code=303,
            )
        sanitary_days_json = serialize_sanitary_days(san_pairs) if san_pairs else None

        async with async_session() as session:
            cs = (await session.execute(
                select(ChatSettings).where(ChatSettings.chat_id == chat_id)
            )).scalar_one_or_none()
            if cs is None:
                return RedirectResponse(
                    url=f"/admin/chats?flash=Chat+{chat_id}+not+found",
                    status_code=303,
                )
            # v4.5.1: валидация report_chat_id — должен указывать на чат,
            # помеченный is_report_chat=True (либо None для сброса).
            if rc is not None:
                rc_target = (await session.execute(
                    select(ChatSettings).where(ChatSettings.chat_id == rc)
                )).scalar_one_or_none()
                if rc_target is None or not rc_target.is_report_chat:
                    return RedirectResponse(
                        url=(
                            f"/admin/chats?flash=Report+chat+{rc}+is+not+marked+"
                            "as+report+chat.+Use+the+%E2%98%86+Make+report+button+"
                            "on+that+chat+first."
                        ),
                        status_code=303,
                    )
            cs.hashtag = ht or None
            cs.report_chat_id = rc
            cs.warns_to_mute = wtm if wtm is not None else 0
            cs.mute_duration_seconds = mdb if mdb is not None else 3600
            cs.warns_to_ban = wtb if wtb is not None else 0
            # v4.5.2: новые поля
            cs.warn_decay_days = decay if decay is not None else 0
            cs.link_filter_action = link_filter_action
            cs.night_mode_start = nm_start
            cs.night_mode_end = nm_end
            cs.night_mode_permissions = night_perms_json
            # v4.5.3: расширенная настройка ночного режима.
            cs.night_mode_tz = nm_tz
            cs.night_mode_weekend_start = nm_wknd_start or None
            cs.night_mode_weekend_end = nm_wknd_end or None
            cs.night_mode_notify = (night_mode_notify == "on")
            # Пустая строка из формы → None (дефолтный шаблон).
            enter_msg = (night_mode_notify_enter_msg or "").strip()
            exit_msg = (night_mode_notify_exit_msg or "").strip()
            cs.night_mode_notify_enter_msg = enter_msg or None
            cs.night_mode_notify_exit_msg = exit_msg or None
            # v4.5.4: sanitary days. Сохраняем JSON (или None если пусто).
            # Не сбрасываем sanitary_days_currently_active/saved_permissions
            # здесь — это делает _sanitary_day_tick при выходе из sanitary day.
            cs.sanitary_days = sanitary_days_json
            cs.updated_at = datetime.now(timezone.utc)
            await session.commit()

        _req_logger.info(
            "admin_chats_update: chat_id=%s updated by=%s (hashtag=%s, "
            "report_chat_id=%s, warns_to_mute=%s, mute_dur=%s, warns_to_ban=%s, "
            "warn_decay=%s, link_filter_action=%s, night=%s-%s [%s], tz=%s, "
            "weekend=%s-%s, notify=%s, sanitary=%s)",
            chat_id, _auth.username, ht, rc, wtm, mdb, wtb,
            decay, link_filter_action, nm_start, nm_end, night_mode_preset,
            nm_tz, nm_wknd_start or "-", nm_wknd_end or "-",
            night_mode_notify == "on",
            len(san_pairs) if san_pairs else 0,
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
        v4.5.2: добавлены toggle для cas, link_filter, night_mode.

        Поле form: field=enabled|private|report_chat|cas|link_filter|night_mode — что переключать.
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
        valid_fields = {"enabled", "private", "report_chat", "cas", "link_filter", "night_mode"}
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
            elif field == "cas":
                cs.cas_check_enabled = not cs.cas_check_enabled
                msg = f"Chat+{chat_id}+CAS+{'enabled' if cs.cas_check_enabled else 'disabled'}"
            elif field == "link_filter":
                cs.link_filter_enabled = not cs.link_filter_enabled
                msg = f"Chat+{chat_id}+Link+filter+{'enabled' if cs.link_filter_enabled else 'disabled'}"
            elif field == "night_mode":
                cs.night_mode_enabled = not cs.night_mode_enabled
                if not cs.night_mode_enabled:
                    cs.night_mode_currently_active = False
                msg = f"Chat+{chat_id}+Night+mode+{'enabled' if cs.night_mode_enabled else 'disabled'}"
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
    #  /admin/chats/{chat_id}/delete — v4.4.8: удалить чат полностью.
    #
    #  Бот ЛИВАЕТ из чата через bot.leave_chat (best-effort — если бот уже
    #  не в чате,_telegram вернёт ошибку, мы её просто логируем).
    #  Из БД удаляются:
    #    • chat_settings — настройки чата
    #    • chat_admins — связи модераторов с этим чатом
    #    • punishments — история наказаний в этом чате
    #
    #  Ограничения:
    #    • Нельзя удалить chat_id=0 (глобальные дефолтные настройки).
    #    • Доступ: require_admin (как и остальные /admin/chats/* маршруты).
    # ──────────────────────────────────────────────────────────────────
    @app.post("/admin/chats/{chat_id_str}/delete")
    async def admin_chats_delete(
        chat_id_str: str,
        _auth: AuthUser = Depends(require_admin),
    ):
        """v4.4.8: полностью удаляет чат. Бот ливает, записи из БД чистятся."""
        try:
            chat_id = int(chat_id_str)
        except (ValueError, TypeError):
            return RedirectResponse(
                url=f"/admin/chats?flash=Invalid+chat_id",
                status_code=303,
            )

        # Защита: chat_id=0 — это глобальный дефолт, его нельзя удалять.
        if chat_id == 0:
            return RedirectResponse(
                url="/admin/chats?flash=Cannot+delete+default+settings+(chat_id=0)",
                status_code=303,
            )

        # Считаем что будем удалять (для лога и флэша).
        async with async_session() as session:
            cs = (await session.execute(
                select(ChatSettings).where(ChatSettings.chat_id == chat_id)
            )).scalar_one_or_none()
            if cs is None:
                return RedirectResponse(
                    url=f"/admin/chats?flash=Chat+{chat_id}+not+found",
                    status_code=303,
                )

            pun_count = (await session.execute(
                select(func.count(Punishment.id)).where(Punishment.chat_id == chat_id)
            )).scalar() or 0
            ca_count = (await session.execute(
                select(func.count(ChatAdmin.id)).where(ChatAdmin.chat_id == chat_id)
            )).scalar() or 0

            chat_title = cs.title or "(no title)"

            # 1. Удаляем punishments для этого чата.
            if pun_count:
                await session.execute(
                    Punishment.__table__.delete().where(Punishment.chat_id == chat_id)
                )
            # 2. Удаляем chat_admins для этого чата.
            if ca_count:
                await session.execute(
                    ChatAdmin.__table__.delete().where(ChatAdmin.chat_id == chat_id)
                )
            # 3. Удаляем саму chat_settings.
            await session.execute(
                ChatSettings.__table__.delete().where(ChatSettings.chat_id == chat_id)
            )
            await session.commit()

        # 4. Лучше-эффорт: бот ливает из чата.
        #    Если бот уже не в чате / нет прав / chat_id невалидный —
        #    Telegram вернёт BadRequest, мы его просто логируем.
        leave_msg = ""
        if bot is not None:
            try:
                await bot.leave_chat(chat_id=chat_id)
                leave_msg = "+bot+left"
                _req_logger.info("admin_chats_delete: bot left chat_id=%s", chat_id)
            except TelegramBadRequest as e:
                _req_logger.warning(
                    "admin_chats_delete: bot.leave_chat(%s) failed: %s",
                    chat_id, e,
                )
                leave_msg = "+bot+leave+failed+(already+not+in+chat?)"
            except Exception as e:
                _req_logger.warning(
                    "admin_chats_delete: bot.leave_chat(%s) unexpected error: %s",
                    chat_id, e,
                )
                leave_msg = "+bot+leave+error"
        else:
            leave_msg = "+no+bot+instance"

        msg = (
            f"Chat+{chat_id}+({chat_title.replace(' ', '+')})+deleted+"
            f"({pun_count}+punishments,+{ca_count}+admins){leave_msg}"
        )
        _req_logger.info(
            "admin_chats_delete: chat_id=%s title='%s' by=%s "
            "(punishments=%s, chat_admins=%s, leave=%s)",
            chat_id, chat_title, _auth.username, pun_count, ca_count, leave_msg,
        )
        return RedirectResponse(url=f"/admin/chats?flash={msg}", status_code=303)

    # ──────────────────────────────────────────────────────────────────
    #  /admin/chats/{chat_id}/recheck-bot-rights — v4.5.5
    #
    #  POST. SU/admin. Дёргает bot.get_chat_member(chat_id, bot.id),
    #  вычисляет недостающие права, сохраняет в ChatSettings.bot_rights_missing
    #  + bot_rights_checked_at. Если прав не хватает — шлёт DM всем Admin/SU.
    #  Redirect обратно на /admin/chats с flash-сообщением о результате.
    # ──────────────────────────────────────────────────────────────────
    @app.post("/admin/chats/{chat_id_str}/recheck-bot-rights")
    async def admin_chats_recheck_bot_rights(
        chat_id_str: str,
        _auth: AuthUser = Depends(require_admin),
    ):
        try:
            chat_id = int(chat_id_str)
        except (ValueError, TypeError):
            return RedirectResponse(
                url="/admin/chats?flash=Invalid+chat_id", status_code=303,
            )
        if chat_id == 0:
            return RedirectResponse(
                url="/admin/chats?flash=Cannot+recheck+rights+for+default+settings+(chat_id=0)",
                status_code=303,
            )

        # Получаем ChatMember из TG и вычисляем права.
        try:
            from bot_handlers import _check_and_persist_bot_rights, _BOT_RIGHT_LABELS
            if bot is None:
                return RedirectResponse(
                    url="/admin/chats?flash=Bot+instance+not+available",
                    status_code=303,
                )
            member = await bot.get_chat_member(chat_id=chat_id, user_id=bot.id)
            missing = await _check_and_persist_bot_rights(
                bot, chat_id=chat_id, chat_title=None,
                new_chat_member=member, notify=True,
            )
        except Exception as e:
            _req_logger.warning(
                "admin_chats_recheck_bot_rights: chat_id=%s by=%s error=%s",
                chat_id, _auth.username, e,
            )
            return RedirectResponse(
                url=f"/admin/chats?flash=Recheck+failed:+{str(e).replace(' ', '+')[:120]}",
                status_code=303,
            )

        if not missing:
            msg = f"Chat+{chat_id}:+bot+has+full+admin+rights+(can_change_info,+can_delete_messages,+can_restrict_members)"
        else:
            # Человекочитаемый список недостающих прав.
            labels = [_BOT_RIGHT_LABELS.get(r, r).split(" (")[0] for r in missing]
            msg = f"Chat+{chat_id}:+bot+is+missing+rights:+{',+'.join(labels)}"

        _req_logger.info(
            "admin_chats_recheck_bot_rights: chat_id=%s by=%s missing=%s",
            chat_id, _auth.username, missing,
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

    # ──────────────────────────────────────────────────────────────────
    #  /me/password — смена своего пароля (v4.4 → v4.5: переехал из /dashboard в /me)
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
                url="/me?pw_msg=SU+password+is+managed+via+WEB_PASSWORD+env+variable",
                status_code=303,
            )

        # Валидация
        if len(new_password) < 6:
            return RedirectResponse(
                url="/me?pw_msg=New+password+must+be+at+least+6+chars",
                status_code=303,
            )
        if new_password != confirm:
            return RedirectResponse(
                url="/me?pw_msg=New+password+and+confirmation+do+not+match",
                status_code=303,
            )

        async with async_session() as session:
            wu = (await session.execute(
                select(WebUser).where(WebUser.username == _auth.username)
            )).scalar_one_or_none()
            if wu is None or not wu.is_active or not wu.password_hash:
                return RedirectResponse(
                    url="/me?pw_msg=Account+not+found",
                    status_code=303,
                )
            # Проверяем старый пароль
            if not _verify_password(old_password, wu.password_hash):
                return RedirectResponse(
                    url="/me?pw_msg=Current+password+is+incorrect",
                    status_code=303,
                )
            # Проверяем, что новый пароль отличается от старого
            if _verify_password(new_password, wu.password_hash):
                return RedirectResponse(
                    url="/me?pw_msg=New+password+must+differ+from+current",
                    status_code=303,
                )
            wu.password_hash = _hash_password(new_password)
            await session.commit()

        _req_logger.info(
            "me_change_password: user=%s changed own password",
            _auth.username,
        )
        return RedirectResponse(
            url="/me?pw_msg=Password+changed+successfully",
            status_code=303,
        )

    # ──────────────────────────────────────────────────────────────────
    #  v4.5: /me — личный профиль пользователя
    #
    #  Показывает:
    #    • Аватарку из TG (большая, 96×96) + кнопку Refresh
    #    • Инфу об аккаунте: логин, роль, TG ID, дата создания, последний логин
    #    • Для admin/moderator — форму смены пароля
    #    • Для SU — предупреждение что пароль в env WEB_PASSWORD
    #    • Для moderator — дополнительно инструкцию по смене пароля через DM боту
    # ──────────────────────────────────────────────────────────────────
    @app.get("/me", response_class=HTMLResponse)
    async def me_profile(
        request: Request,
        pw_msg: str = "",
        _auth: AuthUser = Depends(require_auth),
    ):
        async with async_session() as session:
            wu = (await session.execute(
                select(WebUser).where(WebUser.username == _auth.username)
            )).scalar_one_or_none()
            if wu is None:
                # Сессия валидна, но юзер пропал — редирект на логин
                return RedirectResponse(url="/login", status_code=303)
            # Создаём snapshot со всеми нужными полями (wu может быть удалён из сессии)
            profile = {
                "username": wu.username,
                "role": wu.role or ("su" if wu.is_su else "admin"),
                "is_su": wu.is_su,
                "is_active": wu.is_active,
                "tg_user_id": wu.tg_user_id,
                "tg_first_name": wu.tg_first_name,
                "tg_last_name": wu.tg_last_name,
                "tg_username": wu.tg_username,
                "created_at": wu.created_at,
                "last_login_at": wu.last_login_at,
                "created_by": wu.created_by,
                "avatar_url": _avatar_url(wu.tg_user_id, wu.tg_photo_updated_at),
                "photo_updated_at": wu.tg_photo_updated_at,
            }

        return templates.TemplateResponse("profile.html", {
            "request": request,
            "auth_user": _auth,
            "app_version": APP_VERSION,
            "profile": profile,
            "pw_msg": pw_msg or None,
            "web_public_url": WEB_PUBLIC_URL,
        })

    @app.post("/me/avatar/refresh")
    async def me_avatar_refresh(
        request: Request,
        _auth: AuthUser = Depends(require_auth),
    ):
        """v4.5: принудительно скачивает аватарку из TG и обновляет timestamp."""
        if not _auth.tg_user_id:
            return RedirectResponse(
                url="/me?pw_msg=No+TG+ID+bound+to+your+account",
                status_code=303,
            )
        if bot is None:
            return RedirectResponse(
                url="/me?pw_msg=Bot+instance+not+available",
                status_code=303,
            )
        ok = await _fetch_and_save_avatar(bot, _auth.tg_user_id)
        if ok:
            async with async_session() as session:
                wu = (await session.execute(
                    select(WebUser).where(WebUser.username == _auth.username)
                )).scalar_one_or_none()
                if wu is not None:
                    wu.tg_photo_updated_at = datetime.now(timezone.utc)
                    await session.commit()
            _req_logger.info(
                "me_avatar_refresh: avatar updated for user=%s tg_id=%s",
                _auth.username, _auth.tg_user_id,
            )
            return RedirectResponse(
                url="/me?pw_msg=Avatar+updated+successfully",
                status_code=303,
            )
        else:
            return RedirectResponse(
                url="/me?pw_msg=Could+not+fetch+avatar+(no+profile+photo+or+API+error)",
                status_code=303,
            )

    # ──────────────────────────────────────────────────────────────────
    #  v4.5: /admin/settings — Settings (SU-only)
    #
    #  Раздел:
    #    • Bot info (версия, uptime, чаты, модераторы, наказания)
    #    • Backup now (создать копию БД вручную)
    #    • Cleanup (preview + apply — встроенный, как было в /admin/cleanup)
    #    • VACUUM (оптимизация файла БД без удаления данных)
    # ──────────────────────────────────────────────────────────────────
    def _bot_info() -> dict:
        """Собирает snapshot инфо о боте для страницы Settings."""
        info = {
            "version": APP_VERSION,
            "uptime_seconds": 0,
            "db_path": DB_PATH,
            "db_size_bytes": 0,
            "chats_total": 0,
            "chats_enabled": 0,
            "chats_disabled": 0,
            "moderators_total": 0,
            "web_users_total": 0,
            "punishments_total": 0,
        }
        # Размер файла БД
        try:
            if os.path.exists(DB_PATH):
                info["db_size_bytes"] = os.path.getsize(DB_PATH)
        except OSError:
            pass
        # Uptime: считаем от времени старта процесса (точнее — от создания app)
        # Просто используем время старта процесса из env если задано, иначе — 0
        # (не критично — это всего лишь отображение)
        try:
            import psutil
            info["uptime_seconds"] = int(time.time() - psutil.Process().create_time())
        except Exception:
            pass
        # Счётчики из БД
        try:
            conn = sqlite3.connect(DB_PATH)
            try:
                info["chats_total"] = conn.execute(
                    "SELECT COUNT(*) FROM chat_settings WHERE chat_id != 0"
                ).fetchone()[0]
                info["chats_enabled"] = conn.execute(
                    "SELECT COUNT(*) FROM chat_settings WHERE chat_id != 0 AND is_enabled=1"
                ).fetchone()[0]
                info["chats_disabled"] = conn.execute(
                    "SELECT COUNT(*) FROM chat_settings WHERE chat_id != 0 AND is_enabled=0"
                ).fetchone()[0]
                info["moderators_total"] = conn.execute(
                    "SELECT COUNT(*) FROM moderators"
                ).fetchone()[0]
                info["web_users_total"] = conn.execute(
                    "SELECT COUNT(*) FROM web_users"
                ).fetchone()[0]
                info["punishments_total"] = conn.execute(
                    "SELECT COUNT(*) FROM punishments"
                ).fetchone()[0]
            finally:
                conn.close()
        except sqlite3.Error as e:
            _req_logger.warning("_bot_info: sqlite error: %s", e)
        return info

    @app.get("/admin/settings", response_class=HTMLResponse)
    async def admin_settings_page(
        request: Request,
        flash: str = "",
        _auth: AuthUser = Depends(require_su),
    ):
        """v4.5: страница настроек системы (SU-only)."""
        # Cleanup preview (live counts из БД)
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

        return templates.TemplateResponse("admin_settings.html", {
            "request": request,
            "auth_user": _auth,
            "app_version": APP_VERSION,
            "flash": flash or None,
            "counts": counts,
            "db_path": DB_PATH,
            "db_path_dir": os.path.dirname(DB_PATH) or ".",
            "bot_info": _bot_info(),
        })

    @app.post("/admin/settings/backup")
    async def admin_settings_backup(
        request: Request,
        _auth: AuthUser = Depends(require_su),
    ):
        """v4.5: создаёт резервную копию БД без удаления данных."""
        if not os.path.exists(DB_PATH):
            return RedirectResponse(
                url="/admin/settings?flash=Database+file+not+found",
                status_code=303,
            )
        ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup_path = f"{DB_PATH}.backup-{ts}.db"
        backup_filename = os.path.basename(backup_path)
        # v4.5.1: checkpoint WAL в основной файл перед копированием — иначе
        # свежие записи (последние несколько секунд) в бэкап не попадут.
        _wal_checkpoint()
        try:
            shutil.copy2(DB_PATH, backup_path)
        except OSError as e:
            _req_logger.error("admin_settings_backup: backup failed: %s", e)
            return RedirectResponse(
                url=f"/admin/settings?flash=Backup+failed%3A+{e}",
                status_code=303,
            )
        _req_logger.info(
            "admin_settings_backup: created %s (by=%s)",
            backup_path, _auth.username,
        )
        return RedirectResponse(
            url=f"/admin/settings?flash=Backup+created%3A+{backup_filename}",
            status_code=303,
        )

    @app.post("/admin/settings/vacuum")
    async def admin_settings_vacuum(
        request: Request,
        _auth: AuthUser = Depends(require_su),
    ):
        """v4.5: запускает VACUUM на файле БД (оптимизация без удаления данных)."""
        if not os.path.exists(DB_PATH):
            return RedirectResponse(
                url="/admin/settings?flash=Database+file+not+found",
                status_code=303,
            )
        try:
            size_before = os.path.getsize(DB_PATH)
            conn = sqlite3.connect(DB_PATH)
            try:
                conn.isolation_level = None
                conn.execute("VACUUM")
                conn.isolation_level = ""
            finally:
                conn.close()
            size_after = os.path.getsize(DB_PATH)
        except sqlite3.Error as e:
            _req_logger.error("admin_settings_vacuum: VACUUM failed: %s", e)
            return RedirectResponse(
                url=f"/admin/settings?flash=VACUUM+failed%3A+{e}",
                status_code=303,
            )
        _req_logger.info(
            "admin_settings_vacuum: VACUUM done (by=%s) size %d→%d bytes",
            _auth.username, size_before, size_after,
        )
        delta = size_before - size_after
        return RedirectResponse(
            url=f"/admin/settings?flash=VACUUM+done.+DB+size%3A+{size_before}+%E2%86%92+{size_after}+bytes+"
                f"(-{delta}+bytes+freed)",
            status_code=303,
        )

    # ──────────────────────────────────────────────────────────────────
    #  v4.5: /admin/cleanup → редирект на /admin/settings#cleanup
    #  Старый маршрут сохранён для обратной совместимости (закладки, тесты).
    #  POST /admin/cleanup всё ещё работает как alias к логике cleanup,
    #  чтобы не ломать существующие тесты.
    # ──────────────────────────────────────────────────────────────────
    @app.get("/admin/cleanup", response_class=HTMLResponse)
    async def admin_cleanup_page_legacy(
        request: Request,
        flash: str = "",
        _auth: AuthUser = Depends(require_su),
    ):
        # Редирект на новую страницу Settings (с якорем на блок cleanup).
        # flash пробрасываем через query string.
        target = "/admin/settings"
        if flash:
            target += f"?flash={flash}"
        target += "#cleanup"
        return RedirectResponse(url=target, status_code=303)

    @app.post("/admin/cleanup")
    async def admin_cleanup_apply(
        request: Request,
        include_chat_admins: str = Form(""),
        _auth: AuthUser = Depends(require_su),
    ):
        """v4.5: реальное удаление тестовых данных (логика не изменилась).

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
                url="/admin/settings?flash=Database+file+not+found#cleanup",
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
                url="/admin/settings?flash=Refused%3A+no+moderators+and+no+web+users+"
                    "+present.+Create+at+least+one+admin+before+cleanup.#cleanup",
                status_code=303,
            )

        # ── 3. Backup ───────────────────────────────────────────────
        ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup_path = f"{DB_PATH}.backup-{ts}.db"
        backup_filename = os.path.basename(backup_path)
        # v4.5.1: checkpoint WAL в основной файл перед копированием.
        _wal_checkpoint()
        try:
            shutil.copy2(DB_PATH, backup_path)
        except OSError as e:
            _req_logger.error("admin_cleanup: backup failed: %s", e)
            return RedirectResponse(
                url=f"/admin/settings?flash=Backup+failed%3A+{e}#cleanup",
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
                url=f"/admin/settings?flash=Deletion+failed%3A+{e}.+Restore+from+{backup_filename}#cleanup",
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

        flash_msg = (
            f"Cleanup+complete%3A+{deleted_punishments}+punishments+deleted%2C+"
            f"{deleted_users}+users+deleted%2C+backup%3A+{backup_filename}"
        )
        return RedirectResponse(
            url=f"/admin/settings?flash={flash_msg}#cleanup",
            status_code=303,
        )

    return app
