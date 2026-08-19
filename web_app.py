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

import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import shutil
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from aiogram.exceptions import TelegramBadRequest

# Rich Messages (Bot API 10.2 / aiogram 3.30) — для приветствия новому админу.
# Импортируем лениво внутри функции, чтобы не тащить зависимость на aiogram.types
# при статическом импорте модуля (на случай если бот запускается без aiogram).
from aiogram.types import (
    InputRichBlockFooter,
    InputRichBlockParagraph,
    InputRichBlockSectionHeading,
    InputRichMessage,
    RichTextBold,
    RichTextSpoiler,
    RichTextUrl,
)
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

_req_logger = logging.getLogger("shadow_logger.requests")

from db import (
    DB_PATH,
    ChatAdmin,
    ChatSettings,
    LinkAllowlist,
    PermissionPreset,
    Punishment,
    WebUser,
    WordFilter,
    _hash_password,
    _verify_password,  # noqa: F401 — реэкспорт: web/auth.py зовёт web_app._verify_password
    async_session,
)

# ── Конфигурация ────────────────────────────────────────────────────────────
WEB_PASSWORD = os.getenv("WEB_PASSWORD", "")
COOKIE_NAME = "sl_session"

# v4.8.7: SESSION_SECRET теперь обязательна. Раньше дефолтилась в
# secrets.token_hex(32) — безопасно (каждый рестарт разлогинивает всех),
# но сюрприз для пользователя: после деплоя все куки инвалидны без видимой
# причины. Теперь: если env не задан при create_app() — падаем со стартовой
# ошибкой. На уровне импорта оставляем дефолт secrets.token_hex(32), чтобы
# не сломать тесты и AST-анализ — реальная проверка идёт в create_app().
_SESSION_SECRET = os.getenv("SESSION_SECRET") or secrets.token_hex(32)
_SESSION_SECRET_EXPLICIT = bool(os.getenv("SESSION_SECRET"))

# v4.8.7: Cookie secure flag — должен быть True при HTTPS-деплое.
# Bothost по умолчанию обслуживает через HTTPS (https://*.bothost.tech),
# так что дефолт = True. Если локальная dev-инсталляция на http://localhost,
# нужно явно выставить WEB_COOKIE_SECURE=0 в env.
_COOKIE_SECURE = os.getenv("WEB_COOKIE_SECURE", "1") == "1"

# МСК таймзона
MSK = timezone(timedelta(hours=3))

PAGE_SIZE = 50  # записей на страницу в дашборде

# ── v4.5: Версия приложения ────────────────────────────────────────────────
# v4.6.0 (30 июля 2026): Гранулярные права + пользовательские пресеты +
# monthly санитарные дни + dashboard warnings card.
#   • Новая таблица permission_presets: именованные наборы ChatPermissions
#     для day / night / sanitary. Системные пресеты «Full lockdown»,
#     «Text only», «Day default» — неудаляемые, создаются при init_db.
#     Пользовательские пресеты — через /admin/presets.
#   • Новые поля в ChatSettings: day_permissions (явные дневные права,
#     NULL = старое поведение через snapshot), sanitary_days_permissions
#     (права на время сан. дня, NULL = all False), last_sanitary_month
#     (для suppress предупреждений «нет дат на след. месяц»).
#   • Sanitary days: формат изменён на monthly — JSON-объект вида
#     {"2026-08": [["2026-08-02","2026-08-03"]], "2026-09": []}.
#     При выходе из последнего сан. дня месяца — ключ этого месяца
#     удаляется, ставится last_sanitary_month.
#   • Dashboard warnings card: критические ошибки бота > нет дат на
#     сан. дни > у бота нет прав > прочее. Сортировка по важности.
# v4.5.6 (29 июля 2026): Ephemeral-сообщения (подтверждения модератору и
# уведомления нарушителю о варне) теперь авто-удаляются через 30 секунд.
# v4.5.5: Проверка прав бота при добавлении в чат + DM Admin/SU если прав
# не хватает, бейдж ⚠ RIGHTS и кнопка Recheck в /admin/chats.
# v4.5.4: Санитарные дни — lockdown чата на заданные даты.
APP_VERSION = "v4.9.0"
APP_RELEASE_DATE = "2026-08-17"

# v4.8.11: служебный mod_id для действий встроенного su из веб-панели.
# У su нет привязанного Telegram-аккаунта (создаётся сидом init_db, логин по
# WEB_PASSWORD), поэтому настоящего mod_id у него не существует. Все остальные
# учётки обязаны иметь tg_user_id — см. api_unban.
_SU_WEB_MOD_ID = -1

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


# v4.8.8: доверяем X-Forwarded-For только от известных прокси.
# Раньше _client_ip безусловно верил XFF — любой запрос мог подставить
# произвольный IP и обойти rate-limit на /login (5 попыток с одного IP).
# Теперь: если peer (прямой соединитель) в списке TRUSTED_PROXIES,
# берём первый IP из XFF. Иначе — берём сам peer.
# Это безопасный дефолт: если env не задан, лимит может стать глобальным
# (всё приходит с 127.0.0.1 за reverse proxy), но это лучше, чем дыра.
# Для Bothost: TRUSTED_PROXIES=127.0.0.1 (или IP их nginx).
_TRUSTED_PROXIES = frozenset(
    ip.strip() for ip in os.getenv("TRUSTED_PROXIES", "").split(",") if ip.strip()
)


def _client_ip(request: Request) -> str:
    """IP клиента. X-Forwarded-For учитывается только если прямой peer
    — доверенный прокси (см. _TRUSTED_PROXIES). Иначе берётся peer соединения."""
    peer = request.client.host if request.client else "unknown"
    if peer in _TRUSTED_PROXIES:
        xff = request.headers.get("X-Forwarded-For", "")
        if xff:
            # Берём первый (самый левый) IP из списка — это исходный клиент.
            return xff.split(",")[0].strip()
    return peer


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


# v4.8.7: async-обёртки для блокирующих SQLite-операций. Раньше в async-роутах
# (/admin/settings, /admin/settings/backup, /admin/settings/vacuum,
# /admin/cleanup) вызывались синхронные sqlite3.connect / shutil.copy2 /
# VACUUM — это блокировало event loop на всё время операции. Пока SU жмёт
# «Backup» или «Cleanup» с тяжёлым VACUUM, бот не отвечал ни в одном чате
# (polling приостанавливался). asyncio.to_thread() выносит блокирующий I/O
# в пул потоков — event loop остаётся живым, бот продолжает работать.
# Все функции ниже — тонкие обёртки, чтобы вызывать их через await.
async def _wal_checkpoint_async() -> None:
    await asyncio.to_thread(_wal_checkpoint)


async def _backup_db_async(backup_path: str) -> None:
    """Синхронный shutil.copy2 в потоке — не блокирует event loop."""
    await asyncio.to_thread(shutil.copy2, DB_PATH, backup_path)

# ── Публичный URL веб-панели ────────────────────────────────────────────────
# Дублируется из bot_handlers.py намеренно (web_app.py не должен зависеть от
# bot_handlers — там вся логика бота, тут только веб-слой). Значение по умолчанию
# — production-инсталляция на Bothost. Меняется через env только если деплой
# на другой домен.
WEB_PUBLIC_URL = (os.getenv("WEB_PUBLIC_URL") or "https://degraban.bothost.tech").rstrip("/")


# v4.8.12: текстовые поля форм объявлены как `Form("")`, а не `Form(...)`.
#
# После обновления FastAPI 0.115.6 → 0.141.1 (Starlette 0.41 → 1.6) пустое
# значение формы приравнено к отсутствующему: `Form(...)` отбивает его
# валидацией и отдаёт машинный 422 JSON ещё до хендлера. Раньше хендлер
# получал "" и сам возвращал редирект с понятным flash-сообщением.
#
# Все затронутые роуты проверяют пустоту сами (`if not name.strip()`,
# `len(password) < 6` и т.п.), поэтому обязательность на уровне схемы только
# мешала. Числовые поля (`punishment_id`, `user_id`, `chat_id`) остались
# `Form(...)`: они приходят из сгенерированных форм, руками не набираются,
# и 422 для них — корректная реакция. Контракт закреплён в
# tests/test_v4812_empty_form_fields.py.

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


# v4.8.7: срок годности токена сессии. Раньше поле 't' (issued_ts) писалось
# в payload, но _verify_token его не проверял — утёкшая кука оставалась
# валидной бесконечно (пока SU-аккаунт активен). Теперь: токен протухает
# через _SESSION_TTL_SECONDS (7 дней по умолчанию, как cookie max_age).
# При истечении — _verify_token возвращает None → require_auth кидает
# редирект на /login. Cookie max_age и server-side TTL синхронизированы:
# если браузер почистил куку раньше — пользователь сам разлогинился,
# если кука утекла — сервер всё равно её отбросит через 7 дней.
_SESSION_TTL_SECONDS = int(os.getenv("WEB_SESSION_TTL_SECONDS", str(86400 * 7)))


def _verify_token(token: str) -> dict | None:
    """Возвращает payload (dict) или None если токен невалиден или протух."""
    try:
        raw, signature = token.rsplit(":", 1)
        expected = _sign(raw)
        if not hmac.compare_digest(signature, expected):
            return None
        payload = json.loads(raw)
        if not {"u", "s", "t"} <= set(payload.keys()):
            return None
        # v4.8.7: проверка срока годности. Если токен старше
        # _SESSION_TTL_SECONDS — считаем протухшим (return None).
        # compare в int — нет float edge cases.
        issued_at = int(payload["t"])
        age = int(time.time()) - issued_at
        if age > _SESSION_TTL_SECONDS:
            return None
        # Защита от "future timestamps" (часы убежали вперёд при выдаче) —
        # токен из будущего считаем невалидным (5 сек tolerance).
        if age < -5:
            return None
        return payload
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


# ── CSRF (v4.8.8) ───────────────────────────────────────────────────────────
# SameSite=lax на куке закрывает основной вектор CSRF в современных браузерах,
# но это одна линия обороны — если куку когда-нибудь переведут на SameSite=none
# или браузер пользователя не поддерживает SameSite, панель станет уязвимой.
# Токен привязан к username сессии тем же HMAC-секретом, что и сама кука:
# отдельного хранилища не нужно, токен безперационно валидируется на сервере.
# Шаблон получает csrf_token автоматически через context_processor ниже —
# во все формы достаточно добавить:
#   <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
#
# Зависимости require_csrf_* совмещают проверку CSRF с проверкой роли:
#   require_csrf_auth    → auth + CSRF
#   require_csrf_admin   → auth + admin+ + CSRF
#   require_csrf_su      → auth + su + CSRF
# В POST-роутах они заменяют соответствующие require_* (без csrf).

def _csrf_token_for_username(username: str) -> str:
    """CSRF-токен, привязанный к username. Подписан тем же секретом, что сессия."""
    return _sign(f"csrf:{username}")


def _csrf_token_from_request(request: Request) -> str:
    """Достаёт CSRF-токен для текущего запроса (по куке сессии).
    Возвращает пустую строку, если пользователь не залогинен —
    в этом случае форма всё равно отрендерится, но POST без токена отклонится."""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return ""
    payload = _verify_token(token)
    if not payload:
        return ""
    return _csrf_token_for_username(payload["u"])


async def _validate_csrf(request: Request, auth: AuthUser) -> None:
    """Проверяет csrf_token из form data. Raises HTTPException(403) если невалиден."""
    try:
        form = await request.form()
    except Exception:
        # form() может бросить, если body уже прочитан (например, JSON). Для
        # JSON-API роутов CSRF не используется — там заголовок X-CSRF-Token.
        form = None
    supplied = ""
    if form is not None:
        supplied = form.get("csrf_token", "") or ""
    if not supplied:
        # Fallback: заголовок X-CSRF-Token (для fetch-вызовов из JS).
        supplied = request.headers.get("X-CSRF-Token", "") or ""
    expected = _csrf_token_for_username(auth.username)
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=403, detail="CSRF token missing or invalid")


async def require_csrf_auth(request: Request) -> AuthUser:
    """Auth + CSRF. Используется в POST-роутах вместо require_auth."""
    auth = await require_auth(request)
    await _validate_csrf(request, auth)
    return auth


async def require_csrf_su(
    request: Request, _: AuthUser = Depends(require_csrf_auth)
) -> AuthUser:
    """SU + CSRF. Заменяет require_su в POST-роутах."""
    if _.role != "su":
        raise HTTPException(status_code=303, headers={"Location": "/dashboard"})
    return _


async def require_csrf_admin(
    request: Request, _: AuthUser = Depends(require_csrf_auth)
) -> AuthUser:
    """Admin+ + CSRF. Заменяет require_admin в POST-роутах."""
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


# ── v4.8.6: время старта web_app для uptime без psutil ──────────────────────
# Захватывается при импорте модуля (близко к моменту запуска процесса).
_APP_START_TIME: float = time.time()


# ── Создание приложения ─────────────────────────────────────────────────────
def create_app(lifespan=None, bot=None) -> FastAPI:
    """Создаёт FastAPI-приложение.

    :param lifespan: async context manager для startup/shutdown (передаётся в FastAPI).
    :param bot: экземпляр aiogram.Bot — нужен для эндпоинта создания админа через TGID
                (дёргает bot.get_chat(user_id) для получения профиля из Telegram).
                Если None — эндпоинт /admin/users/create вернёт 503.
    """
    # v4.8.7: SESSION_SECRET обязательна при реальном старте приложения.
    # Если env не задан — падаем с понятным сообщением вместо тихого
    # разлогинивания всех пользователей при каждом рестарте. Тесты обычно
    # вызывают create_app(bot=None) — для них делаем исключение через
    # WEB_ALLOW_NO_SECRET=1 (тесты сами выставляют).
    if not _SESSION_SECRET_EXPLICIT and os.getenv("WEB_ALLOW_NO_SECRET") != "1":
        raise RuntimeError(
            "SESSION_SECRET env var is required. Generate one with:\n"
            "  python -c \"import secrets; print(secrets.token_hex(32))\"\n"
            "and set it in Bothost panel env vars. Without it, all session "
            "cookies would be invalidated on every restart. For local dev "
            "or tests, set WEB_ALLOW_NO_SECRET=1 to bypass this check."
        )
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
    # v4.7.6: новые фильтры для UI sanitary days.
    try:
        from bot_handlers import (
            format_sanitary_period_human as _fmt_san_period,
        )
        from bot_handlers import (
            get_sanitary_periods_flat as _san_flat,
        )
        templates.env.filters["format_sanitary_period"] = _fmt_san_period
        templates.env.filters["sanitary_periods_flat"] = _san_flat
    except ImportError:
        templates.env.filters["format_sanitary_period"] = lambda e: ""
        templates.env.filters["sanitary_periods_flat"] = lambda s: []
    # v4.5.2: глобальные переменные для всех шаблонов (версия в футере)
    templates.env.globals["app_version"] = APP_VERSION
    templates.env.globals["app_release_date"] = APP_RELEASE_DATE

    # v4.8.8: обёртка над TemplateResponse — автоматически прокидывает csrf_token
    # в контекст каждого шаблона. Все 14 вызовов templates.TemplateResponse(...)
    # передают request в context — по нему вычисляется токен. Если пользователь
    # не залогинен (например, /login) — токен пустой, форма рендерится без поля.
    # В шаблонах: {{ csrf_token }} — значение, {{ csrf_field() }} — готовый <input>.
    _orig_template_response = templates.TemplateResponse

    def _template_response_with_csrf(name, context=None, **kwargs):
        ctx = dict(context or {})
        req = ctx.get("request")
        if req is not None and "csrf_token" not in ctx:
            ctx["csrf_token"] = _csrf_token_from_request(req)
        # v4.8.12: Starlette 1.0 убрал старую сигнатуру TemplateResponse(name,
        # context) — request стал обязательным первым аргументом. Со старым
        # порядком `name` получал контекст-словарь, и Jinja падал на
        # «cannot use 'tuple' as a dict key» при построении ключа кеша шаблона.
        # Все 14 вызовов кладут request в контекст, поэтому берём его оттуда.
        return _orig_template_response(req, name, ctx, **kwargs)

    templates.TemplateResponse = _template_response_with_csrf

    # csrf_field() — хелпер для шаблонов: рендерит <input type="hidden" ...>.
    # Читает csrf_token из контекста шаблона через @pass_context — иначе Jinja
    # не передаёт контекстные переменные в глобальные callable.
    # Возвращает Markup (не str), чтобы Jinja не эскейпил HTML-теги.
    from jinja2 import pass_context
    from markupsafe import Markup

    @pass_context
    def _csrf_field(ctx):
        token = ctx.get("csrf_token", "")
        if not token:
            return ""
        return Markup(f'<input type="hidden" name="csrf_token" value="{token}">')

    templates.env.globals["csrf_field"] = _csrf_field

    # v4.5: создаём папку для аватарок при старте (если ещё нет)
    try:
        os.makedirs(AVATARS_DIR, exist_ok=True)
    except OSError as e:
        _req_logger.warning("create_app: cannot create AVATARS_DIR %s: %s", AVATARS_DIR, e)

    # ── v4.9.0 (Task 10): зависимости для роутеров из web/ ──────────────
    # Роутеры не могут замкнуться на локальные переменные create_app, поэтому
    # templates и bot кладутся в состояние приложения, а web/deps.py отдаёт
    # их провайдерами get_templates/get_bot.
    # Именно app.state, а не модульные синглтоны: сюита зовёт create_app()
    # многократно в одном процессе (test_v460_granular_perms.py — 15 раз),
    # и глобальное состояние текло бы между экземплярами.
    app.state.templates = templates
    app.state.bot = bot

    # ── v4.8.9: Routers из web/ package ──────────────────────────────────
    # Декомпозиция create_app() — см. 03_TASK_v4.8.9.md §2 и web/__init__.py.
    # v4.8.9: /health и /logout перенесены.
    # v4.8.10: / (root), /avatar/*, /api/presets, /api/automute-count перенесены.
    # Остальные 47 роутов — inline ниже, TODO v4.9.0.
    from web.admin_bans import router as admin_bans_router
    from web.admin_cleanup import router as admin_cleanup_router
    from web.admin_keywords import router as admin_keywords_router
    from web.admin_settings import router as admin_settings_router
    from web.api import router as api_router
    from web.auth import router as auth_router
    from web.health import router as health_router
    from web.me import router as me_router
    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(me_router)
    app.include_router(api_router)
    app.include_router(admin_bans_router)
    app.include_router(admin_cleanup_router)
    app.include_router(admin_keywords_router)
    app.include_router(admin_settings_router)

    # ── v4.5: Endpoint для отдачи аватарок — v4.8.10 перенесён в web/me.py ─
    # Раньше тут был inline @app.get("/avatar/{tg_user_id:int}").
    # Теперь — в web/me.py, подключён через app.include_router выше.

    # ── Root → редирект на login — v4.8.10 перенесён в web/me.py ────────
    # Раньше тут был inline @app.get("/").

    # ── GET /login и POST /login — v4.9.0 перенесены в web/auth.py ──────
    # Раньше тут были inline @app.get("/login") и @app.post("/login").
    # Теперь — в web/auth.py, подключены через app.include_router выше.

    # ── POST /logout и GET /logout — v4.8.9 перенесены в web/auth.py ────
    # Раньше тут были inline @app.post("/logout") и @app.get("/logout").
    # Теперь они в web/auth.py, подключены через app.include_router выше.
    # v4.5.1: GET /logout был CSRF-уязвим (<img src="/logout"> разлогинивал).
    # POST + SameSite=lax cookie закрывают вектор.

    # ── GET /dashboard, GET /user/<user_id> — v4.9.0 перенесены в web/me.py ──
    # Раньше тут были inline @app.get("/dashboard") и @app.get("/user/{user_id:int}").
    # Теперь — в web/me.py, подключены через app.include_router выше.

    # ── GET /api/dashboard, GET /api/search — v4.9.0 перенесены в web/api.py ──
    # Раньше тут были inline @app.get("/api/dashboard") и @app.get("/api/search").
    # Теперь — в web/api.py, подключены через app.include_router выше.

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
        tg_user_id: str = Form(""),
        role: str = Form("admin"),
        chat_ids: list[str] = Form(None),
        _auth: AuthUser = Depends(require_csrf_su),
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
        _auth: AuthUser = Depends(require_csrf_su),
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
        password: str = Form(""),
        _auth: AuthUser = Depends(require_csrf_su),
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
        _auth: AuthUser = Depends(require_csrf_su),
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
        _auth: AuthUser = Depends(require_csrf_su),
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
        _auth: AuthUser = Depends(require_csrf_su),
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
        _auth: AuthUser = Depends(require_csrf_su),
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

            # v4.6.0: список всех пресетов для dropdown в admin_chats.html.
            presets_rows = (await session.execute(
                select(PermissionPreset).order_by(
                    PermissionPreset.scope, PermissionPreset.name
                )
            )).scalars().all()
            presets_by_scope = {"day": [], "night": [], "sanitary": []}
            for p in presets_rows:
                if p.scope in presets_by_scope:
                    presets_by_scope[p.scope].append(p)

        return templates.TemplateResponse("admin_chats.html", {
            "request": request,
            "chats": rows,
            "stats": stats,
            "mod_counts": mod_counts,
            "report_chat_options": report_chat_options,
            "presets_by_scope": presets_by_scope,
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
        # v4.5.3: расширенная настройка ночного режима.
        night_mode_start: str = Form("23:00"),
        night_mode_end: str = Form("07:00"),
        night_mode_tz: str = Form("Europe/Moscow"),
        night_mode_weekend_start: str = Form(""),
        night_mode_weekend_end: str = Form(""),
        night_mode_notify: str = Form(""),
        night_mode_notify_enter_msg: str = Form(""),
        night_mode_notify_exit_msg: str = Form(""),
        # v4.5.4: sanitary days textarea. Multiline-текст, одна запись на
        # строку ('YYYY-MM-DD' или 'YYYY-MM-DD - YYYY-MM-DD').
        sanitary_days_text: str = Form(""),
        # v4.7.24: via-bot rate-limit filter (настройки в разделе «Наказания»).
        # via_bot_rate_limit_seconds — grace-окно (по умолчанию 300 = 5 мин).
        # via_bot_mute_minutes — длительность мьюта при превышении (по умолчанию 10).
        via_bot_rate_limit_seconds: str = Form("300"),
        via_bot_mute_minutes: str = Form("10"),
        # v4.6.1: пресеты прав — только выбор из dropdown. Custom grids убраны,
        # свои наборы прав создаются на странице /admin/presets.
        # preset_id="" или "__none__" → NULL (старое поведение, через snapshot).
        # preset_id="__lockdown__" → all False (только для sanitary, default).
        # preset_id=<int> → берём permissions из пресета.
        # v4.7.16: из пресета также копируется slow_mode_delay (если задан).
        day_preset_id: str = Form(""),
        night_preset_id: str = Form(""),
        sanitary_preset_id: str = Form("__lockdown__"),
        _auth: AuthUser = Depends(require_csrf_admin),
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
            # v4.7.24: via-bot rate-limit settings (1..86400 sec / 1..1440 min)
            vb_rl = _parse_int(via_bot_rate_limit_seconds, "via_bot_rate_limit_seconds", 1)
            vb_mm = _parse_int(via_bot_mute_minutes, "via_bot_mute_minutes", 1)
        except ValueError as e:
            return RedirectResponse(
                url=f"/admin/chats?flash={e}",
                status_code=303,
            )

        # v4.7.24: sanity-clip — rate-limit не больше 24h, mute не больше 24h
        if vb_rl is not None and vb_rl > 86400:
            vb_rl = 86400
        if vb_mm is not None and vb_mm > 1440:
            vb_mm = 1440

        # v4.5.2: валидация link_filter_action.
        # v4.6.1: night_mode_preset валидация убрана — presetId из БД валидируется ниже.
        if link_filter_action not in ("delete", "warn", "mute", "ban"):
            return RedirectResponse(
                url="/admin/chats?flash=Invalid+link_filter_action",
                status_code=303,
            )

        # v4.5.2: валидация HH:MM для night mode
        import re as _re
        _hhmm_re = _re.compile(r"^([01][0-9]|2[0-3]):([0-5][0-9])$")
        nm_start = (night_mode_start or "").strip()
        nm_end = (night_mode_end or "").strip()
        if not _hhmm_re.match(nm_start):
            return RedirectResponse(
                url="/admin/chats?flash=Invalid+night_mode_start+(use+HH:MM)",
                status_code=303,
            )
        if not _hhmm_re.match(nm_end):
            return RedirectResponse(
                url="/admin/chats?flash=Invalid+night_mode_end+(use+HH:MM)",
                status_code=303,
            )

        # v4.5.3: валидация tz (IANA timezone).
        nm_tz = (night_mode_tz or "Europe/Moscow").strip()
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(nm_tz)
        except (ValueError, KeyError):
            return RedirectResponse(
                url="/admin/chats?flash=Invalid+night_mode_tz+(use+IANA+name+like+Europe/Moscow)",
                status_code=303,
            )

        # v4.5.3: валидация weekend schedule (опционально).
        # Если одно из полей задано — оба обязательны.
        nm_wknd_start = (night_mode_weekend_start or "").strip()
        nm_wknd_end = (night_mode_weekend_end or "").strip()
        if (nm_wknd_start or nm_wknd_end) and not (nm_wknd_start and nm_wknd_end):
            return RedirectResponse(
                url="/admin/chats?flash=Weekend+schedule+requires+both+start+and+end",
                status_code=303,
            )
        if nm_wknd_start and not _hhmm_re.match(nm_wknd_start):
            return RedirectResponse(
                url="/admin/chats?flash=Invalid+night_mode_weekend_start+(use+HH:MM)",
                status_code=303,
            )
        if nm_wknd_end and not _hhmm_re.match(nm_wknd_end):
            return RedirectResponse(
                url="/admin/chats?flash=Invalid+night_mode_weekend_end+(use+HH:MM)",
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

        # v4.6.1: night_mode_permissions теперь берётся только из night_preset_id.
        # Старый dropdown night_mode_preset (text_only/strict/none/custom) и custom grid
        # (perm_can_send_*) убраны из UI — свои наборы прав создаются на /admin/presets.
        # Изначально night_perms_json = None (NULL в БД = night mode не меняет права).
        # Если night_preset_id указывает на валидный пресет — берём его permissions.
        night_perms_json: str | None = None

        # v4.5.4 / v4.6.0: парсим sanitary_days.
        # v4.6.1: monthly_sanitary_days_json убран — UI шлёт только sanitary_days_text
        # (textarea). Парсинг остаётся тем же.
        try:
            from bot_handlers import (
                parse_sanitary_days_textarea,
                serialize_sanitary_days_monthly,
            )
        except ImportError:
            return RedirectResponse(
                url="/admin/chats?flash=Server+error+(bot_handlers+import+failed)",
                status_code=303,
            )

        # v4.6.1: UI присылает только textarea (sanitary_days_text).
        # Парсим и группируем по месяцам автоматически.
        # v4.7.11: парсер теперь возвращает entries длиной 2/3/4 (с опциональным
        # временем). Группировка должна сохранять время, иначе round-trip
        # format→parse→serialize терял данные и валидация падала на строках
        # вида '2026-07-31 23:00 - 2026-08-03 09:00'.
        san_pairs, san_errors = parse_sanitary_days_textarea(sanitary_days_text)
        if san_errors:
            first_err = san_errors[0].replace(" ", "+")
            return RedirectResponse(
                url=f"/admin/chats?flash=Sanitary+days:+{first_err}",
                status_code=303,
            )
        if san_pairs:
            grouped: dict[str, list[list[str]]] = {}
            for entry in san_pairs:
                # entry: [start_iso, end_iso] / [s, e, st] / [s, e, st, et]
                mk = entry[0][:7]  # YYYY-MM
                grouped.setdefault(mk, []).append(entry)
            sanitary_days_json = serialize_sanitary_days_monthly(grouped)
        else:
            sanitary_days_json = None

        # v4.6.1: пресеты прав — только выбор из dropdown, без custom grids.
        # Загружаем все пресеты одним запросом для валидации.
        async with async_session() as _ps:
            preset_records = (await _ps.execute(
                select(PermissionPreset)
            )).scalars().all()
        preset_by_id = {p.id: p for p in preset_records}

        _ALL_PERM_KEYS = (
            "can_send_messages", "can_send_audios", "can_send_documents",
            "can_send_photos", "can_send_videos", "can_send_video_notes",
            "can_send_voice_notes", "can_send_polls", "can_send_other_messages",
            "can_add_web_page_previews", "can_change_info", "can_invite_users",
            "can_pin_messages",
        )

        def _resolve_perms(preset_id_field: str, scope: str) -> tuple[str | None, int | None]:
            """v4.6.1: Возвращает (JSON-строка permissions, slow_mode_delay) для ChatSettings.

            Логика (custom grids убраны — только выбор пресета):
              • preset_id_field == "__none__" или "" → (None, None) (старое поведение / snapshot)
              • preset_id_field == "__lockdown__" → (all False, None) (default для sanitary)
              • preset_id_field == int (валидный ID) → берём из preset_by_id
                (с проверкой scope). v4.7.16: slow_mode_delay тоже из пресета.
              • невалидный ID или несоответствие scope → (None, None) (safe fallback)

            v4.7.16: slow_mode_delay — None = не менять, 0 = выкл, >0 = N сек.
            Копируется в ChatSettings.day_slow_mode_delay / night_mode_slow_mode_delay.
            """
            pid = (preset_id_field or "").strip()
            if pid in ("", "__none__"):
                return None, None
            if pid == "__lockdown__":
                return json.dumps({k: False for k in _ALL_PERM_KEYS}), None
            try:
                pid_int = int(pid)
            except (ValueError, TypeError):
                return None, None
            preset = preset_by_id.get(pid_int)
            if preset is None or preset.scope != scope:
                return None, None
            return preset.permissions, preset.slow_mode_delay

        day_perms_json, day_slow = _resolve_perms(day_preset_id, "day")
        sanitary_perms_json, _sanitary_slow = _resolve_perms(sanitary_preset_id, "sanitary")
        # v4.6.1: night_perms_json — только из night_preset_id.
        night_slow: int | None = None
        if night_preset_id and night_preset_id not in ("", "__none__"):
            night_resolved, night_slow_candidate = _resolve_perms(night_preset_id, "night")
            if night_resolved is not None:
                night_perms_json = night_resolved
                night_slow = night_slow_candidate

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
            enter_msg = (night_mode_notify_enter_msg or "").strip()
            exit_msg = (night_mode_notify_exit_msg or "").strip()
            cs.night_mode_notify_enter_msg = enter_msg or None
            cs.night_mode_notify_exit_msg = exit_msg or None
            # v4.5.4 / v4.6.0: sanitary days. Сохраняем monthly JSON (или None).
            cs.sanitary_days = sanitary_days_json
            # v4.6.0: гранулярные права.
            cs.day_permissions = day_perms_json
            cs.sanitary_days_permissions = sanitary_perms_json
            # v4.7.16: slow_mode копируется из пресета (как permissions).
            # None = пресет не выбран → 0 (не менять slow_mode, backward compat).
            # 0 = выкл. >0 = N сек. См. PermissionPreset.slow_mode_delay.
            cs.day_slow_mode_delay = day_slow if day_slow is not None else 0
            cs.night_mode_slow_mode_delay = night_slow if night_slow is not None else 0
            # v4.7.24: via-bot rate-limit settings (toggle ставится отдельно
            # через /toggle поле=via_bot_filter).
            cs.via_bot_rate_limit_seconds = vb_rl if vb_rl is not None else 300
            cs.via_bot_mute_minutes = vb_mm if vb_mm is not None else 10
            cs.updated_at = datetime.now(timezone.utc)
            await session.commit()

        _req_logger.info(
            "admin_chats_update: chat_id=%s updated by=%s (hashtag=%s, "
            "report_chat_id=%s, warns_to_mute=%s, mute_dur=%s, warns_to_ban=%s, "
            "warn_decay=%s, link_filter_action=%s, night=%s-%s [%s], tz=%s, "
            "weekend=%s-%s, notify=%s, sanitary=%s, day_perms=%s, san_perms=%s, "
            "night_preset_id=%s, day_slow=%s, night_slow=%s, "
            "via_bot_rl=%ss, via_bot_mute=%smin)",
            chat_id, _auth.username, ht, rc, wtm, mdb, wtb,
            decay, link_filter_action, nm_start, nm_end,
            night_preset_id or "(none)",
            nm_tz, nm_wknd_start or "-", nm_wknd_end or "-",
            night_mode_notify == "on",
            sanitary_days_json or "(none)",
            "yes" if day_perms_json else "no",
            "yes" if sanitary_perms_json else "no",
            night_preset_id or "(none)",
            day_slow if day_slow is not None else "(unchanged)",
            night_slow if night_slow is not None else "(unchanged)",
            vb_rl if vb_rl is not None else 300,
            vb_mm if vb_mm is not None else 10,
        )
        return RedirectResponse(
            url=f"/admin/chats?flash=Chat+{chat_id}+settings+updated",
            status_code=303,
        )

    @app.post("/admin/chats/{chat_id_str}/toggle")
    async def admin_chats_toggle(
        chat_id_str: str,
        request: Request,
        _auth: AuthUser = Depends(require_csrf_admin),
    ):
        """v4.4.7: переключает is_enabled / is_report_chat для чата.
        v4.5.2: добавлены toggle для cas, link_filter, night_mode.
        v4.7.2: добавлен toggle для sanitary_days.
        v4.7.6: упразднён toggle 'private' (система private/non-private удалена).
        v4.7.24: добавлен toggle для via_bot_filter (rate-limit «via @Bot»).
        v4.8.0: добавлен toggle для mod_chat (взаимоисключение с report_chat).

        Поле form: field=enabled|report_chat|cas|link_filter|night_mode|sanitary_days|via_bot_filter|mod_chat — что переключать.
        """
        try:
            chat_id = int(chat_id_str)
        except (ValueError, TypeError):
            return RedirectResponse(
                url="/admin/chats?flash=Invalid+chat_id",
                status_code=303,
            )
        form = await request.form()
        field = (form.get("field") or "").strip().lower()
        valid_fields = {"enabled", "report_chat", "cas", "link_filter", "night_mode", "sanitary_days", "via_bot_filter", "mod_chat"}
        if field not in valid_fields:
            return RedirectResponse(
                url="/admin/chats?flash=Invalid+toggle+field",
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
            elif field == "cas":
                cs.cas_check_enabled = not cs.cas_check_enabled
                msg = f"Chat+{chat_id}+CAS+{'enabled' if cs.cas_check_enabled else 'disabled'}"
            elif field == "link_filter":
                cs.link_filter_enabled = not cs.link_filter_enabled
                msg = f"Chat+{chat_id}+Link+filter+{'enabled' if cs.link_filter_enabled else 'disabled'}"
            elif field == "night_mode":
                cs.night_mode_enabled = not cs.night_mode_enabled
                if not cs.night_mode_enabled:
                    # v4.7.2: при выключении — снимаем active, но НЕ выходим
                    # через _exit_night_mode (это требует bot instance и может
                    # затормозить). Tick сам увидит enabled=False и не тронет.
                    # Если режим сейчас активен, восстановление прав произойдёт
                    # через _exit_night_mode при следующем tick (он проверяет
                    # enabled и пропустит, оставив active=True). Поэтому тут
                    # явно сбрасываем active=False чтобы UI был консистентен,
                    # но права в TG останутся night до вмешательства SU.
                    # Лучше: дёрнуть _exit_night_mode если бот доступен.
                    if cs.night_mode_currently_active and bot is not None:
                        try:
                            # v4.8.9: app_state вместо `from bot import`
                            from app_state import get_exit_night_mode
                            _exit_night_mode = get_exit_night_mode()
                            await _exit_night_mode(cs)
                            # Re-fetch т.к. _exit_night_mode коммитил.
                            await session.refresh(cs)
                        except Exception as e:
                            _req_logger.warning(
                                "toggle night_mode off: exit failed for chat %s: %s",
                                chat_id, e,
                            )
                            cs.night_mode_currently_active = False
                    else:
                        cs.night_mode_currently_active = False
                msg = f"Chat+{chat_id}+Night+mode+{'enabled' if cs.night_mode_enabled else 'disabled'}"
            elif field == "sanitary_days":
                # v4.7.2: явный toggle для санитарных дней.
                cs.sanitary_days_enabled = not cs.sanitary_days_enabled
                if not cs.sanitary_days_enabled and cs.sanitary_days_currently_active:
                    # Выходим из sanitary day если он сейчас активен.
                    if bot is not None:
                        try:
                            # v4.8.9: app_state вместо `from bot import`
                            from app_state import get_exit_sanitary_day
                            _exit_sanitary_day = get_exit_sanitary_day()
                            await _exit_sanitary_day(cs)
                            await session.refresh(cs)
                        except Exception as e:
                            _req_logger.warning(
                                "toggle sanitary off: exit failed for chat %s: %s",
                                chat_id, e,
                            )
                            cs.sanitary_days_currently_active = False
                    else:
                        cs.sanitary_days_currently_active = False
                msg = f"Chat+{chat_id}+Sanitary+days+{'enabled' if cs.sanitary_days_enabled else 'disabled'}"
            elif field == "via_bot_filter":
                # v4.7.24: toggle for via-bot rate-limit filter.
                # Включает/выключает фильтр «via @Bot» сообщений. Настройки
                # (rate_limit, mute_minutes) сохраняются в /update отдельно.
                cs.via_bot_filter_enabled = not cs.via_bot_filter_enabled
                msg = (
                    f"Chat+{chat_id}+Via-bot+filter+"
                    f"{'enabled' if cs.via_bot_filter_enabled else 'disabled'}"
                )
            elif field == "report_chat":
                if cs.is_report_chat:
                    cs.is_report_chat = False
                    msg = f"Chat+{chat_id}+no+longer+report+chat"
                elif cs.is_mod_chat:
                    # v4.8.0: взаимоисключение — нельзя быть report_chat и
                    # mod_chat одновременно. Если чат сейчас mod_chat — отказ.
                    msg = (
                        f"Chat+{chat_id}+is+mod+chat"
                        f"%3B+cannot+be+report+chat+too"
                    )
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
            # v4.8.0: mod_chat toggle — взаимоисключение с report_chat.
            # Нельзя быть одновременно report_chat и mod_chat (разные цели:
            # report_chat — журнал санкций с rich-превью, modchat — оперативные
            # оповещения для дежурного модератора в кратком формате).
            # Если чат уже report_chat — отказ (UI должен скрывать кнопку,
            # но проверка на бэке обязательна для безопасности).
            if field == "mod_chat":
                if cs.is_report_chat:
                    msg = (
                        f"Chat+{chat_id}+is+report+chat"
                        f"%3B+cannot+be+mod+chat+too"
                    )
                elif cs.is_mod_chat:
                    cs.is_mod_chat = False
                    cs.mod_chat_id = None
                    msg = f"Chat+{chat_id}+no+longer+mod+chat"
                else:
                    # Снимаем флаг с других чатов (modchat тоже может быть
                    # только один — по аналогии с report_chat).
                    others = (await session.execute(
                        select(ChatSettings).where(
                            ChatSettings.is_mod_chat.is_(True),
                            ChatSettings.chat_id != chat_id,
                        )
                    )).scalars().all()
                    for o in others:
                        o.is_mod_chat = False
                        o.mod_chat_id = None
                    cs.is_mod_chat = True
                    cs.mod_chat_id = chat_id
                    msg = f"Chat+{chat_id}+is+now+the+mod+chat"
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
        _auth: AuthUser = Depends(require_csrf_admin),
    ):
        """v4.4.8: полностью удаляет чат. Бот ливает, записи из БД чистятся."""
        try:
            chat_id = int(chat_id_str)
        except (ValueError, TypeError):
            return RedirectResponse(
                url="/admin/chats?flash=Invalid+chat_id",
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
    #  /admin/chats/{chat_id}/sync-admins — v4.7.0: авто-обнаружение
    #  TG-админов чата и создание/обновление WebUser.
    #
    #  Логика sync (per-chat, по кнопке SU):
    #    1. Получаем TG-админов через bot.get_chat_administrators.
    #    2. Пропускаем ботов и анонимных (если есть).
    #    3. Для каждого TG-админа:
    #       a. Если уже есть WebUser с этим tg_user_id:
    #          - Если is_pending=True → оставляем как есть (ждёт /start).
    #          - Если is_active=True → проверяем роль и chat_admins.
    #            Обновляем role: can_promote_members → admin, иначе moderator.
    #            (SU-override роль не трогаем — SU всегда SU.)
    #          - Гарантируем наличие chat_admins записи (для moderator).
    #       b. Если нет WebUser → создаём pending (is_active=False,
    #          is_pending=True, без пароля, auto_discovered=True).
    #          Логин: @username (если есть) или tg<TGID>.
    #          Role: admin если can_promote_members, иначе moderator.
    #    4. Для каждого существующего активного WebUser-moderator, привязанного
    #       к этому чату через chat_admins, но НЕ найденного среди текущих
    #       TG-админов → is_active=False (полная деактивация по решению SU).
    #       Admin-роль не понижаем (он может быть админом в других чатах).
    #
    #  Ограничения:
    #    • is_report_chat чаты игнорируются (репорт-чат не модерируется).
    #    • Только SU (кнопка доступна только SU в UI).
    # ──────────────────────────────────────────────────────────────────
    @app.post("/admin/chats/{chat_id_str}/sync-admins")
    async def admin_chats_sync_admins(
        chat_id_str: str,
        _auth: AuthUser = Depends(require_csrf_su),
    ):
        """v4.7.0: sync TG-admins of a chat → WebUser (pending or activate)."""
        try:
            chat_id = int(chat_id_str)
        except (ValueError, TypeError):
            return RedirectResponse(
                url="/admin/chats?flash=Invalid+chat_id",
                status_code=303,
            )
        if chat_id == 0:
            return RedirectResponse(
                url="/admin/chats?flash=Cannot+sync+default+settings",
                status_code=303,
            )
        if bot is None:
            return RedirectResponse(
                url="/admin/chats?flash=Bot+instance+not+available",
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
            if cs.is_report_chat:
                return RedirectResponse(
                    url="/admin/chats?flash=Report+chat+ignored+(no+admins+to+sync)",
                    status_code=303,
                )

        # 1. Получаем TG-админов.
        try:
            tg_admins = await bot.get_chat_administrators(chat_id=chat_id)
        except TelegramBadRequest as e:
            _req_logger.warning(
                "sync_admins: get_chat_administrators(%s) failed: %s",
                chat_id, e,
            )
            return RedirectResponse(
                url=f"/admin/chats?flash=Telegram+error:+{str(e).replace(' ', '+')[:200]}",
                status_code=303,
            )
        except Exception as e:
            _req_logger.warning(
                "sync_admins: get_chat_administrators(%s) unexpected: %s",
                chat_id, e,
            )
            return RedirectResponse(
                url=f"/admin/chats?flash=Unexpected+error:+{str(e).replace(' ', '+')[:200]}",
                status_code=303,
            )

        # Фильтруем ботов (is_bot=True) — у нас нет смысла создавать учётки для ботов.
        tg_admins = [a for a in tg_admins if not getattr(a.user, "is_bot", False)]

        # 2. Словарь tg_user_id → (can_promote, tg_user_obj) для удобства.
        tg_admin_map: dict[int, tuple[bool, object]] = {}
        for a in tg_admins:
            uid = getattr(a.user, "id", None)
            if uid is None:
                continue
            # can_promote_members есть и у creator, и у administrator с этим правом.
            can_promote = bool(getattr(a, "can_promote_members", False)) or \
                          getattr(a, "status", "") == "creator"
            tg_admin_map[uid] = (can_promote, a.user)

        # 3. Существующие WebUser по tg_user_id (одним запросом).
        tg_ids = list(tg_admin_map.keys())
        existing_wus: dict[int, WebUser] = {}
        if tg_ids:
            async with async_session() as session:
                rows = (await session.execute(
                    select(WebUser).where(WebUser.tg_user_id.in_(tg_ids))
                )).scalars().all()
                for wu in rows:
                    existing_wus[wu.tg_user_id] = wu

        # 4. Существующие chat_admins для этого чата (для деактивации отсутствующих).
        async with async_session() as session:
            existing_ca_rows = (await session.execute(
                select(ChatAdmin).where(ChatAdmin.chat_id == chat_id)
            )).scalars().all()
        existing_ca_uids: set[int] = {ca.user_id for ca in existing_ca_rows}

        # 5. Считаем что сделали — для флэша и лога.
        created_pending = 0
        created_admin = 0
        created_moderator = 0
        updated_role = 0
        already_ok = 0
        deactivated = 0

        async with async_session() as session:
            # 5a. Обработка найденных TG-админов.
            for uid, (can_promote, tg_user) in tg_admin_map.items():
                desired_role = "admin" if can_promote else "moderator"
                wu = existing_wus.get(uid)
                if wu is None:
                    # Создаём pending.
                    tg_username = getattr(tg_user, "username", None)
                    if tg_username:
                        login = tg_username.strip().lstrip("@").lower()
                    else:
                        login = f"tg{uid}"
                    # Гарантируем уникальность логина (если вдруг занят).
                    base_login = login
                    suffix = 1
                    while True:
                        exists = (await session.execute(
                            select(WebUser.id).where(WebUser.username == login)
                        )).first()
                        if not exists:
                            break
                        suffix += 1
                        login = f"{base_login}{suffix}"
                    new_wu = WebUser(
                        username=login,
                        password_hash=None,
                        is_su=False,
                        is_active=False,
                        is_pending=True,
                        auto_discovered=True,
                        role=desired_role,
                        tg_user_id=uid,
                        tg_first_name=getattr(tg_user, "first_name", None),
                        tg_last_name=getattr(tg_user, "last_name", None),
                        tg_username=tg_username,
                    )
                    session.add(new_wu)
                    if desired_role == "admin":
                        created_admin += 1
                    else:
                        created_moderator += 1
                    created_pending += 1
                    # Гарантируем chat_admins для moderator.
                    if desired_role == "moderator":
                        ca = ChatAdmin(
                            chat_id=chat_id,
                            user_id=uid,
                            added_by=None,
                        )
                        session.add(ca)
                else:
                    # WebUser уже есть.
                    if wu.is_pending:
                        # Ждёт /start — не трогаем.
                        already_ok += 1
                        # Но роль можем обновить (если изменилась).
                        if not wu.is_su and wu.role != desired_role:
                            wu.role = desired_role
                            updated_role += 1
                        # И chat_admins гарантия.
                        if desired_role == "moderator" and uid not in existing_ca_uids:
                            session.add(ChatAdmin(
                                chat_id=chat_id, user_id=uid, added_by=None,
                            ))
                            existing_ca_uids.add(uid)
                        continue
                    if not wu.is_active:
                        # Не pending и не active — деактивирован ранее. Пропускаем
                        # (SU должен сам реактивировать через change-role/deactivate).
                        already_ok += 1
                        continue
                    # Активный WebUser.
                    if wu.is_su:
                        # SU не трогаем.
                        already_ok += 1
                        continue
                    # Обновляем роль если нужно.
                    if wu.role != desired_role:
                        wu.role = desired_role
                        updated_role += 1
                        # При повышении moderator→admin — чистим chat_admins
                        # (админу они не нужны).
                        if desired_role == "admin":
                            for ca in (await session.execute(
                                select(ChatAdmin).where(ChatAdmin.user_id == uid)
                            )).scalars().all():
                                await session.delete(ca)
                            existing_ca_uids.discard(uid)
                    # Гарантируем chat_admins для moderator.
                    if desired_role == "moderator" and uid not in existing_ca_uids:
                        session.add(ChatAdmin(
                            chat_id=chat_id, user_id=uid, added_by=None,
                        ))
                        existing_ca_uids.add(uid)
                    already_ok += 1

            # 5b. Деактивация отсутствующих: для каждого uid в existing_ca_uids,
            # которого нет среди текущих TG-админов → если есть WebUser с role=moderator
            # → is_active=False (по решению SU "всегда deact").
            for uid in list(existing_ca_uids):
                if uid in tg_admin_map:
                    continue  # всё ещё админ — не трогаем
                wu = (await session.execute(
                    select(WebUser).where(WebUser.tg_user_id == uid)
                )).scalar_one_or_none()
                if wu is None or wu.is_su:
                    continue
                if wu.role == "moderator" and wu.is_active:
                    wu.is_active = False
                    deactivated += 1
                # Удаляем chat_admins запись для этого чата (он больше не админ тут).
                for ca in (await session.execute(
                    select(ChatAdmin).where(
                        ChatAdmin.chat_id == chat_id,
                        ChatAdmin.user_id == uid,
                    )
                )).scalars().all():
                    await session.delete(ca)
                existing_ca_uids.discard(uid)

            await session.commit()

        msg_parts = [
            f"created={created_pending}",
            f"(admin={created_admin},mod={created_moderator})",
            f"updated_role={updated_role}",
            f"deactivated={deactivated}",
            f"already_ok={already_ok}",
        ]
        msg = "+".join(msg_parts)
        _req_logger.info(
            "sync_admins: chat_id=%s by=%s — %s",
            chat_id, _auth.username, msg,
        )
        return RedirectResponse(
            url=f"/admin/chats?flash=Sync+{chat_id_str}+done:+{msg.replace(' ', '+')}",
            status_code=303,
        )

    # ──────────────────────────────────────────────────────────────────
    #  /admin/chats/{chat_id}/sanitary/add — v4.7.6: добавить период
    #  санитарных дней через UI (date+time picker).
    #
    #  Поля формы: start_date (YYYY-MM-DD), end_date (YYYY-MM-DD),
    #  start_time (HH:MM, опционально), end_time (HH:MM, опционально).
    #  Если время не задано — период full-day (старое поведение).
    #
    #  Доступ: require_admin (как и другие /admin/chats/*).
    # ──────────────────────────────────────────────────────────────────
    @app.post("/admin/chats/{chat_id_str}/sanitary/add")
    async def admin_chats_sanitary_add(
        chat_id_str: str,
        request: Request,
        _auth: AuthUser = Depends(require_csrf_admin),
    ):
        """v4.7.6: добавить период санитарных дней."""
        try:
            chat_id = int(chat_id_str)
        except (ValueError, TypeError):
            return RedirectResponse(
                url="/admin/chats?flash=Invalid+chat_id",
                status_code=303,
            )
        form = await request.form()
        start_date = (form.get("start_date") or "").strip()
        end_date = (form.get("end_date") or "").strip()
        start_time = (form.get("start_time") or "").strip() or None
        end_time = (form.get("end_time") or "").strip() or None

        try:
            from bot_handlers import add_sanitary_period
        except ImportError:
            return RedirectResponse(
                url="/admin/chats?flash=Server+error+(bot_handlers+import)",
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
            new_json, err = add_sanitary_period(
                cs.sanitary_days, start_date, end_date, start_time, end_time,
            )
            if err:
                return RedirectResponse(
                    url=f"/admin/chats?flash=Sanitary+add+failed:+{err.replace(' ', '+')}",
                    status_code=303,
                )
            cs.sanitary_days = new_json
            cs.updated_at = datetime.now(timezone.utc)
            await session.commit()

        _req_logger.info(
            "sanitary_add: chat_id=%s by=%s start=%s%s end=%s%s",
            chat_id, _auth.username,
            start_date, f" {start_time}" if start_time else "",
            end_date, f" {end_time}" if end_time else "",
        )
        return RedirectResponse(
            url=f"/admin/chats?flash=Sanitary+period+added+for+chat+{chat_id}",
            status_code=303,
        )

    # ──────────────────────────────────────────────────────────────────
    #  /admin/chats/{chat_id}/sanitary/{idx}/delete — v4.7.6: удалить период
    #  санитарных дней по глобальному индексу.
    #
    #  idx = позиция в плоском list от parse_sanitary_days_json.
    #  Доступ: require_admin.
    # ──────────────────────────────────────────────────────────────────
    @app.post("/admin/chats/{chat_id_str}/sanitary/{idx_str}/delete")
    async def admin_chats_sanitary_delete(
        chat_id_str: str,
        idx_str: str,
        _auth: AuthUser = Depends(require_csrf_admin),
    ):
        """v4.7.6: удалить период санитарных дней по индексу."""
        try:
            chat_id = int(chat_id_str)
            idx = int(idx_str)
        except (ValueError, TypeError):
            return RedirectResponse(
                url="/admin/chats?flash=Invalid+chat_id+or+index",
                status_code=303,
            )
        try:
            from bot_handlers import delete_sanitary_period
        except ImportError:
            return RedirectResponse(
                url="/admin/chats?flash=Server+error+(bot_handlers+import)",
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
            new_json, err = delete_sanitary_period(cs.sanitary_days, idx)
            if err:
                return RedirectResponse(
                    url=f"/admin/chats?flash=Sanitary+delete+failed:+{err.replace(' ', '+')}",
                    status_code=303,
                )
            cs.sanitary_days = new_json
            cs.updated_at = datetime.now(timezone.utc)
            await session.commit()

        _req_logger.info(
            "sanitary_delete: chat_id=%s by=%s idx=%s",
            chat_id, _auth.username, idx,
        )
        return RedirectResponse(
            url=f"/admin/chats?flash=Sanitary+period+deleted+for+chat+{chat_id}",
            status_code=303,
        )

    # ── /admin/keywords, /admin/keywords/add, /admin/keywords/{id}/delete,
    #    /admin/keywords/{id}/toggle-ban-night — v4.9.0 перенесены в
    #    web/admin_keywords.py ──
    # Раньше тут были inline @app.get/@app.post("/admin/keywords"...).
    # Теперь — в web/admin_keywords.py, подключены через app.include_router выше.

    # ── _cleanup_counts / /admin/cleanup — v4.9.0 перенесены в web/admin_cleanup.py ──
    # Раньше тут был вложенный хелпер _cleanup_counts и inline
    # @app.get/@app.post("/admin/cleanup"). Теперь — в web/admin_cleanup.py,
    # роуты подключены через app.include_router выше; _cleanup_counts теперь
    # импортируется напрямую в web/admin_settings.py (Task 8) — там же
    # переехал admin_settings_page, который его зовёт.

    # ── /me/password, /me, /me/avatar/refresh — v4.9.0 перенесены в web/me.py ──
    # Раньше тут были inline @app.post("/me/password"), @app.get("/me") и
    # @app.post("/me/avatar/refresh"). Теперь — в web/me.py, подключены через
    # app.include_router выше.

    # ── /admin/settings, /admin/settings/backup, /admin/settings/vacuum,
    #    /admin/settings/github, /admin/settings/github/test — v4.9.0
    #    перенесены в web/admin_settings.py ──
    # Раньше тут были вложенные хелперы _bot_info / _load_github_settings_row
    # и inline @app.get/@app.post("/admin/settings..."). Теперь — в
    # web/admin_settings.py, роуты подключены через app.include_router выше.

    # ── GET+POST /admin/cleanup — v4.9.0 перенесены в web/admin_cleanup.py ───
    # Раньше тут были inline @app.get("/admin/cleanup") (редирект-алиас) и
    # @app.post("/admin/cleanup") (реальное удаление тестовых данных).
    # Теперь — в web/admin_cleanup.py, подключены через app.include_router выше.

    # ── v4.6.0: Permission Presets ────────────────────────────────────
    @app.get("/admin/presets", response_class=HTMLResponse)
    async def admin_presets_page(
        request: Request,
        flash: str = "",
        _auth: AuthUser = Depends(require_admin),
    ):
        """v4.6.0: страница управления пресетами прав для day/night/sanitary.

        v4.7.5: добавлены секции Word filter (ban words) и Link allowlist.
        Теперь страница — единое место для всех «глобальных списков» модерации.

        Доступ: SU + admin (moderator не имеет доступа — ему нечего тут делать).
        """
        async with async_session() as session:
            presets = (await session.execute(
                select(PermissionPreset).order_by(
                    PermissionPreset.scope, PermissionPreset.name
                )
            )).scalars().all()

            # v4.7.5: Word filter — все активные паттерны, отсортированы
            # по chat_id (global=0 первым), затем по created_at desc.
            words = (await session.execute(
                select(WordFilter)
                .where(WordFilter.is_active.is_(True))
                .order_by(WordFilter.chat_id.asc(), WordFilter.created_at.desc())
            )).scalars().all()

            # v4.7.5: Link allowlist — все домены, аналогичная сортировка.
            links = (await session.execute(
                select(LinkAllowlist)
                .order_by(LinkAllowlist.chat_id.asc(), LinkAllowlist.created_at.asc())
            )).scalars().all()

            # v4.7.5: список чатов для dropdown в add-формах
            # (chat_id=0 — global; реальные чаты для per-chat правил).
            chats = (await session.execute(
                select(ChatSettings)
                .where(ChatSettings.chat_id != 0)
                .order_by(ChatSettings.title.asc())
            )).scalars().all()

        # Группируем по scope для UI.
        grouped = {"day": [], "night": [], "sanitary": []}
        for p in presets:
            if p.scope in grouped:
                grouped[p.scope].append(p)

        return templates.TemplateResponse(
            "admin_presets.html",
            {
                "request": request,
                "presets_by_scope": grouped,
                "word_filters": words,
                "link_allowlist": links,
                "chats": chats,
                "flash": flash,
                "app_version": APP_VERSION,
                "app_release_date": APP_RELEASE_DATE,
                "auth_user": _auth,
            },
        )

    @app.post("/admin/presets/create")
    async def admin_presets_create(
        name: str = Form(""),
        scope: str = Form(""),
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
        perm_can_change_info: str = Form(""),
        perm_can_invite_users: str = Form(""),
        perm_can_pin_messages: str = Form(""),
        # v4.7.16: slow_mode_delay (chat-level, separate from ChatPermissions).
        # Empty/blank = None (не менять slow_mode при применении пресета).
        # 0 = выкл. >0 = N сек. Telegram limit: 0..36400.
        slow_mode_delay: str = Form(""),
        _auth: AuthUser = Depends(require_csrf_admin),
    ):
        """v4.6.0: создать новый пользовательский пресет. v4.7.16: + slow_mode_delay."""
        name = (name or "").strip()
        if not name or len(name) > 64:
            return RedirectResponse(
                url="/admin/presets?flash=Invalid+preset+name+(1-64+chars)",
                status_code=303,
            )
        if scope not in ("day", "night", "sanitary"):
            return RedirectResponse(
                url="/admin/presets?flash=Invalid+scope",
                status_code=303,
            )

        # v4.7.16: парсим slow_mode_delay. Empty = None (не менять).
        slow_mode_raw = (slow_mode_delay or "").strip()
        slow_mode_value: int | None = None
        if slow_mode_raw:
            try:
                slow_mode_value = int(slow_mode_raw)
            except ValueError:
                return RedirectResponse(
                    url="/admin/presets?flash=Invalid+slow_mode_delay+(must+be+integer)",
                    status_code=303,
                )
            if slow_mode_value < 0 or slow_mode_value > 36400:
                return RedirectResponse(
                    url="/admin/presets?flash=slow_mode_delay+must+be+0..36400",
                    status_code=303,
                )

        perms = {
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
            "can_change_info":            perm_can_change_info == "on",
            "can_invite_users":           perm_can_invite_users == "on",
            "can_pin_messages":           perm_can_pin_messages == "on",
        }

        async with async_session() as session:
            # Уникальность name.
            existing = (await session.execute(
                select(PermissionPreset).where(PermissionPreset.name == name)
            )).scalar_one_or_none()
            if existing is not None:
                return RedirectResponse(
                    url=f"/admin/presets?flash=Preset+name+already+exists:+{name.replace(' ', '+')}",
                    status_code=303,
                )
            preset = PermissionPreset(
                name=name, scope=scope,
                permissions=json.dumps(perms),
                slow_mode_delay=slow_mode_value,
                is_system=False,
            )
            session.add(preset)
            await session.commit()
            _req_logger.info(
                "presets_create: name=%r scope=%s slow_mode=%s by=%s",
                name, scope, slow_mode_value, _auth.username,
            )

        # v4.7.20: если создали пресет с name="Day default" (маловероятно т.к.
        # системный уже существует, но на всякий случай) — инвалидируем кеш.
        # Lazy import чтобы избежать circular import (bot.py импортирует web_app).
        if name == "Day default" and scope == "day":
            try:
                import bot as _bot_module
                _bot_module._invalidate_day_default_cache()
            except Exception:
                pass  # бот может быть не загружен в тестовом окружении

        return RedirectResponse(
            url=f"/admin/presets?flash=Preset+{name.replace(' ', '+')}+created",
            status_code=303,
        )

    # ── v4.7.17: редактирование пресетов ──────────────────────────────
    # Раньше пресеты можно было только создать и удалить. Если опечатка в имени,
    # лишний чекбокс или забыли slow_mode — единственный путь был удалить и
    # пересоздать. Теперь — Edit: меняет name/scope/permissions/slow_mode in-place.
    # Системные пресеты нельзя редактировать (как и удалять) — это гарантия того,
    # что «Full lockdown» / «Text only» / «Day default» всегда остаются каноничными.
    # Уникальность name проверяется с исключением текущего пресета (иначе нельзя
    # сохранить пресет, не меняя имя).
    # Замечание про chats: чаты, привязанные к пресету, хранят КОПИЮ JSON в
    # ChatSettings (day_permissions / night_mode_permissions / ...). Поэтому
    # редактирование пресета НЕ затрагивает уже настроенные чаты — это by design,
    # как и для удаления. Чтобы обновить права в конкретном чате, нужно
    # перенастроить его на странице /admin/chats (или дождаться следующего
    # входа/выхода из night mode).
    @app.post("/admin/presets/{preset_id:int}/edit")
    async def admin_presets_edit(
        preset_id: int,
        name: str = Form(""),
        scope: str = Form(""),
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
        perm_can_change_info: str = Form(""),
        perm_can_invite_users: str = Form(""),
        perm_can_pin_messages: str = Form(""),
        slow_mode_delay: str = Form(""),
        _auth: AuthUser = Depends(require_csrf_admin),
    ):
        """v4.7.17: редактировать пресет (name/scope/permissions/slow_mode_delay).

        Системные пресеты редактировать нельзя (как и удалять).
        Уникальность name проверяется с исключением текущего preset_id.
        Валидация полей — идентична admin_presets_create.
        """
        name = (name or "").strip()
        if not name or len(name) > 64:
            return RedirectResponse(
                url="/admin/presets?flash=Invalid+preset+name+(1-64+chars)",
                status_code=303,
            )
        if scope not in ("day", "night", "sanitary"):
            return RedirectResponse(
                url="/admin/presets?flash=Invalid+scope",
                status_code=303,
            )

        # v4.7.16: парсим slow_mode_delay. Empty = None (не менять).
        slow_mode_raw = (slow_mode_delay or "").strip()
        slow_mode_value: int | None = None
        if slow_mode_raw:
            try:
                slow_mode_value = int(slow_mode_raw)
            except ValueError:
                return RedirectResponse(
                    url="/admin/presets?flash=Invalid+slow_mode_delay+(must+be+integer)",
                    status_code=303,
                )
            if slow_mode_value < 0 or slow_mode_value > 36400:
                return RedirectResponse(
                    url="/admin/presets?flash=slow_mode_delay+must+be+0..36400",
                    status_code=303,
                )

        perms = {
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
            "can_change_info":            perm_can_change_info == "on",
            "can_invite_users":           perm_can_invite_users == "on",
            "can_pin_messages":           perm_can_pin_messages == "on",
        }

        async with async_session() as session:
            preset = (await session.execute(
                select(PermissionPreset).where(PermissionPreset.id == preset_id)
            )).scalar_one_or_none()
            if preset is None:
                return RedirectResponse(
                    url="/admin/presets?flash=Preset+not+found",
                    status_code=303,
                )
            if preset.is_system:
                return RedirectResponse(
                    url="/admin/presets?flash=System+presets+cannot+be+edited",
                    status_code=303,
                )

            # Уникальность name — исключаем текущий preset_id.
            existing = (await session.execute(
                select(PermissionPreset).where(
                    PermissionPreset.name == name,
                    PermissionPreset.id != preset_id,
                )
            )).scalar_one_or_none()
            if existing is not None:
                return RedirectResponse(
                    url=f"/admin/presets?flash=Preset+name+already+exists:+{name.replace(' ', '+')}",
                    status_code=303,
                )

            old_name = preset.name
            old_scope = preset.scope
            preset.name = name
            preset.scope = scope
            preset.permissions = json.dumps(perms)
            preset.slow_mode_delay = slow_mode_value
            # updated_at апдейтится автоматически через onupdate=lambda.
            await session.commit()
            _req_logger.info(
                "presets_edit: id=%d name=%r->%r scope=%s->%s slow_mode=%s by=%s",
                preset_id, old_name, name, old_scope, scope,
                slow_mode_value, _auth.username,
            )

        # v4.7.20: если изменился name/scope/permissions пресета с name="Day default"
        # (или newName="Day default") — инвалидируем кеш _DAY_DEFAULT_CACHE.
        # Системные пресеты редактировать нельзя, но на всякий случай.
        if (old_name == "Day default" or name == "Day default") and (old_scope == "day" or scope == "day"):
            try:
                import bot as _bot_module
                _bot_module._invalidate_day_default_cache()
            except Exception:
                pass

        return RedirectResponse(
            url=f"/admin/presets?flash=Preset+{name.replace(' ', '+')}+updated",
            status_code=303,
        )

    @app.post("/admin/presets/{preset_id:int}/delete")
    async def admin_presets_delete(
        preset_id: int,
        _auth: AuthUser = Depends(require_csrf_admin),
    ):
        """v4.6.0: удалить пользовательский пресет. Системные неудаляемы."""
        async with async_session() as session:
            preset = (await session.execute(
                select(PermissionPreset).where(PermissionPreset.id == preset_id)
            )).scalar_one_or_none()
            if preset is None:
                return RedirectResponse(
                    url="/admin/presets?flash=Preset+not+found",
                    status_code=303,
                )
            if preset.is_system:
                return RedirectResponse(
                    url="/admin/presets?flash=System+presets+cannot+be+deleted",
                    status_code=303,
                )
            name = preset.name
            scope = preset.scope
            await session.delete(preset)
            await session.commit()
            _req_logger.info(
                "presets_delete: id=%d name=%r by=%s",
                preset_id, name, _auth.username,
            )

        # v4.7.20: если удалили пресет с name="Day default" (пользовательский,
        # не системный) — инвалидируем кеш _DAY_DEFAULT_CACHE.
        if name == "Day default" and scope == "day":
            try:
                import bot as _bot_module
                _bot_module._invalidate_day_default_cache()
            except Exception:
                pass

        return RedirectResponse(
            url=f"/admin/presets?flash=Preset+{name.replace(' ', '+')}+deleted",
            status_code=303,
        )

    # ── v4.7.5: Word filter (ban words) CRUD ─────────────────────────
    # v4.8.6: stub bot-команды /addword /delword /listwords удалены.
    # Word filter теперь управляется только через этот web UI.
    # chat_id=0 — глобальный паттерн (применяется ко всем чатам).
    # is_regex=True — pattern интерпретируется как re.search, иначе — case-insensitive substring.
    # action: delete|warn|mute|ban.
    # ──────────────────────────────────────────────────────────────────
    @app.post("/admin/presets/words/add")
    async def admin_presets_words_add(
        chat_id: str = Form("0"),
        pattern: str = Form(""),
        is_regex: str = Form(""),
        action: str = Form("delete"),
        _auth: AuthUser = Depends(require_csrf_admin),
    ):
        """v4.7.5: добавить паттерн в word filter через веб-панель.

        Валидация:
          • pattern — непустой, 1-255 символов.
          • is_regex=True → pattern должен компилироваться re.compile без ошибок.
          • action ∈ {delete, warn, mute, ban}.
          • chat_id — число (0 для global).
          • Дубликат (chat_id + pattern + is_active=True) — обновляем action/is_regex.
        """
        # Парсим chat_id
        chat_id = (chat_id or "0").strip()
        try:
            chat_id_int = int(chat_id)
        except ValueError:
            return RedirectResponse(
                url="/admin/presets?flash=Invalid+chat_id+(must+be+number+or+0+for+global)",
                status_code=303,
            )

        pattern = (pattern or "").strip()
        if not pattern or len(pattern) > 255:
            return RedirectResponse(
                url="/admin/presets?flash=Invalid+pattern+(1-255+chars)",
                status_code=303,
            )

        is_regex_bool = is_regex == "on"

        if action not in ("delete", "warn", "mute", "ban"):
            return RedirectResponse(
                url="/admin/presets?flash=Invalid+action",
                status_code=303,
            )

        if is_regex_bool:
            import re as _re
            try:
                _re.compile(pattern)
            except _re.error as e:
                return RedirectResponse(
                    url=f"/admin/presets?flash=Invalid+regex:+{str(e).replace(' ', '+')[:80]}",
                    status_code=303,
                )

        async with async_session() as session:
            existing = (await session.execute(
                select(WordFilter).where(
                    WordFilter.chat_id == chat_id_int,
                    WordFilter.pattern == pattern,
                    WordFilter.is_active.is_(True),
                )
            )).scalar_one_or_none()
            if existing:
                existing.action = action
                existing.is_regex = is_regex_bool
                await session.commit()
                _req_logger.info(
                    "wordfilter_add (update existing): chat_id=%d pattern=%r by=%s",
                    chat_id_int, pattern, _auth.username,
                )
                return RedirectResponse(
                    url=f"/admin/presets?flash=Pattern+updated:+{pattern[:40].replace(' ', '+')}",
                    status_code=303,
                )
            session.add(WordFilter(
                chat_id=chat_id_int,
                pattern=pattern,
                is_regex=is_regex_bool,
                action=action,
                created_by=_auth.tg_user_id,
            ))
            await session.commit()
            _req_logger.info(
                "wordfilter_add: chat_id=%d pattern=%r action=%s regex=%s by=%s",
                chat_id_int, pattern, action, is_regex_bool, _auth.username,
            )

        return RedirectResponse(
            url=f"/admin/presets?flash=Pattern+added:+{pattern[:40].replace(' ', '+')}",
            status_code=303,
        )

    @app.post("/admin/presets/words/{word_id:int}/delete")
    async def admin_presets_words_delete(
        word_id: int,
        _auth: AuthUser = Depends(require_csrf_admin),
    ):
        """v4.7.5: удалить паттерн из word filter (soft-delete — is_active=False).

        Soft-delete выбран для паритета с /delword (команда ботa тоже ставит
        is_active=False). Это сохраняет историю добавлений для аудита.
        """
        async with async_session() as session:
            wf = (await session.execute(
                select(WordFilter).where(WordFilter.id == word_id)
            )).scalar_one_or_none()
            if wf is None:
                return RedirectResponse(
                    url="/admin/presets?flash=Pattern+not+found",
                    status_code=303,
                )
            if not wf.is_active:
                return RedirectResponse(
                    url="/admin/presets?flash=Pattern+already+deleted",
                    status_code=303,
                )
            wf.is_active = False
            await session.commit()
            _req_logger.info(
                "wordfilter_delete: id=%d pattern=%r chat_id=%d by=%s",
                word_id, wf.pattern, wf.chat_id, _auth.username,
            )

        return RedirectResponse(
            url=f"/admin/presets?flash=Pattern+deleted:+{wf.pattern[:40].replace(' ', '+')}",
            status_code=303,
        )

    # ── v4.7.5: Link allowlist CRUD ──────────────────────────────────
    # Паритет с командами /linkallow, /linkallowlist.
    # chat_id=0 — глобальный allowlist. Сравнение по подстроке домена.
    # ──────────────────────────────────────────────────────────────────
    @app.post("/admin/presets/links/add")
    async def admin_presets_links_add(
        chat_id: str = Form("0"),
        domain: str = Form(""),
        _auth: AuthUser = Depends(require_csrf_admin),
    ):
        """v4.7.5: добавить домен в link allowlist через веб-панель.

        Валидация (паритет с /linkallow):
          • domain — непустой, после нормализации должен содержать точку.
          • Убирается scheme и path если есть.
          • Дубликат (chat_id + domain) — отказ с flash.
        """
        chat_id = (chat_id or "0").strip()
        try:
            chat_id_int = int(chat_id)
        except ValueError:
            return RedirectResponse(
                url="/admin/presets?flash=Invalid+chat_id",
                status_code=303,
            )

        domain = (domain or "").strip().lower()
        # Нормализация: убираем scheme если есть
        if "://" in domain:
            try:
                from urllib.parse import urlparse
                parsed = urlparse(domain)
                domain = parsed.netloc.lower()
            except ValueError:
                pass
        domain = domain.lstrip("@").strip("/")
        if not domain or "." not in domain:
            return RedirectResponse(
                url="/admin/presets?flash=Invalid+domain+(must+contain+dot,+e.g.+t.me)",
                status_code=303,
            )

        async with async_session() as session:
            existing = (await session.execute(
                select(LinkAllowlist).where(
                    LinkAllowlist.chat_id == chat_id_int,
                    LinkAllowlist.domain == domain,
                )
            )).scalar_one_or_none()
            if existing:
                return RedirectResponse(
                    url=f"/admin/presets?flash=Domain+already+in+allowlist:+{domain}",
                    status_code=303,
                )
            session.add(LinkAllowlist(
                chat_id=chat_id_int,
                domain=domain,
                created_by=_auth.tg_user_id,
            ))
            await session.commit()
            _req_logger.info(
                "linkallowlist_add: chat_id=%d domain=%r by=%s",
                chat_id_int, domain, _auth.username,
            )

        return RedirectResponse(
            url=f"/admin/presets?flash=Domain+added:+{domain}",
            status_code=303,
        )

    @app.post("/admin/presets/links/{link_id:int}/delete")
    async def admin_presets_links_delete(
        link_id: int,
        _auth: AuthUser = Depends(require_csrf_admin),
    ):
        """v4.7.5: удалить домен из link allowlist (hard delete).

        Hard delete — потому что в таблице link_allowlist нет is_active флага
        (в отличие от WordFilter). Это паритет с тем, как если бы строку
        удалили через прямой SQL.
        """
        async with async_session() as session:
            link = (await session.execute(
                select(LinkAllowlist).where(LinkAllowlist.id == link_id)
            )).scalar_one_or_none()
            if link is None:
                return RedirectResponse(
                    url="/admin/presets?flash=Domain+not+found",
                    status_code=303,
                )
            domain = link.domain
            chat_id = link.chat_id
            await session.delete(link)
            await session.commit()
            _req_logger.info(
                "linkallowlist_delete: id=%d domain=%r chat_id=%d by=%s",
                link_id, domain, chat_id, _auth.username,
            )

        return RedirectResponse(
            url=f"/admin/presets?flash=Domain+removed:+{domain}",
            status_code=303,
        )

    # v4.8.10: /api/presets перенесён в web/api.py.
    # Раньше тут был inline @app.get("/api/presets") — JSON-API список пресетов.
    # Теперь — в web/api.py, подключён через app.include_router выше.

    # ── GET /admin/bans — v4.9.0 перенесён в web/admin_bans.py ───────────
    # Раньше тут был inline @app.get("/admin/bans").
    # Теперь — в web/admin_bans.py, подключён через app.include_router выше.

    # ── POST /api/unban, POST /api/reset-automute-count — v4.9.0 перенесены в web/api.py ──
    # Раньше тут были inline @app.post("/api/unban") и @app.post("/api/reset-automute-count").
    # Теперь — в web/api.py, подключены через app.include_router выше.

    # v4.8.10: /api/automute-count перенесён в web/api.py.
    # Раньше тут был inline @app.get("/api/automute-count") — счётчик автомьютов.
    # Теперь — в web/api.py, подключён через app.include_router выше.

    return app
