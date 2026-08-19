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
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

_req_logger = logging.getLogger("shadow_logger.requests")

from db import (
    DB_PATH,
    WebUser,
    _hash_password,  # noqa: F401 — реэкспорт: тесты зовут web_app._hash_password напрямую
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
APP_VERSION = "v4.10.0"
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
    from web.admin_chats import router as admin_chats_router
    from web.admin_cleanup import router as admin_cleanup_router
    from web.admin_keywords import router as admin_keywords_router
    from web.admin_presets import router as admin_presets_router
    from web.admin_settings import router as admin_settings_router
    from web.admin_users import router as admin_users_router
    from web.api import router as api_router
    from web.auth import router as auth_router
    from web.health import router as health_router
    from web.me import router as me_router
    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(me_router)
    app.include_router(api_router)
    app.include_router(admin_bans_router)
    app.include_router(admin_chats_router)
    app.include_router(admin_cleanup_router)
    app.include_router(admin_keywords_router)
    app.include_router(admin_presets_router)
    app.include_router(admin_settings_router)
    app.include_router(admin_users_router)

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

    # ── /admin/users* — v4.9.0 перенесены в web/admin_users.py ──
    # Раньше тут были inline @app.get("/admin/users") и семь
    # @app.post("/admin/users/...") (create/toggle/reset/role/edit-chats/
    # bind-tg/delete).
    # Теперь — в web/admin_users.py, подключены через app.include_router выше.

    # ── /admin/chats* — v4.9.0 перенесены в web/admin_chats.py ──
    # Раньше тут были inline @app.get("/admin/chats") и шесть
    # @app.post("/admin/chats/...") (update/toggle/delete/sync-admins/
    # sanitary/add/sanitary/{idx}/delete).
    # Теперь — в web/admin_chats.py, подключены через app.include_router выше.

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

    # ── /admin/presets и /admin/presets/* (create/edit/delete/words/links) ──
    # v4.9.0 перенесены в web/admin_presets.py.
    # Раньше тут были inline @app.get("/admin/presets") и семь
    # @app.post(...) роутов CRUD для пресетов прав, word filter и link
    # allowlist. Теперь — в web/admin_presets.py, подключены через
    # app.include_router выше.
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
