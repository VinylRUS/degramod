"""
bot.py — Точка входа: FastAPI + Aiogram.
Режим работы определяется автоматически:
  - Если WEBHOOK_URL задан И вебхук удалось установить → webhook
  - Иначе → Long Polling (надёжный фоллбэк)

FastAPI запускается всегда — для веб-панели (когда Bothost починит Traefik).
"""

import asyncio
import logging
import os
import secrets
import socket
import time
from contextlib import asynccontextmanager

import uvicorn
from aiogram import Bot, Dispatcher, types
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from sqlalchemy import select
import fastapi

from bot_handlers import router as mod_router
from db import init_db, async_session, ChatSettings
from web_app import create_app

# v4.5.2: helpers для night mode background task (defined in bot_handlers)
# v4.5.3: добавлен _night_mode_in_window для поддержки per-chat tz + weekend.
# v4.5.4: добавлены helpers для санитарных дней (chat-level ChatPermissions lockdown).
from bot_handlers import (
    _time_str_in_range, _parse_night_mode_permissions, _snapshot_permissions,
    _PERM_FIELDS, _night_mode_in_window,
    parse_sanitary_days_json, is_sanitary_day_today,
)
from datetime import datetime, timezone, timedelta, date
import json
from aiogram.exceptions import TelegramBadRequest

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(name)-24s │ %(levelname)-7s │ %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("shadow_logger")

# ── Env ─────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", "3000"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
WEBHOOK_PATH = "/webhook" if "/webhook" in WEBHOOK_URL else "/webhook"

# v4.5.1: секрет для webhook — Telegram шлёт его в заголовке
# X-Telegram-Bot-Api-Secret-Token на каждый запрос. Без проверки
# кто угодно может POST-нуть фейковый Update на /webhook и заставить
# бота выполнить команды от имени «админа».
# Если env не задан — генерируем случайный (перезаписи при рестарте не
# страшны, так как мы заново делаем set_webhook при старте).
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET") or secrets.token_hex(16)

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN env variable is required")

# ── Диагностика ─────────────────────────────────────────────────────────────
logger.info("=== ENV DUMP ===")
for key, val in sorted(os.environ.items()):
    if key in ("BOT_TOKEN", "API_TOKEN", "BOT_API_TOKEN", "TELEGRAM_BOT_TOKEN",
               "TOKEN", "WEB_PASSWORD", "SESSION_SECRET"):
        val = val[:8] + "..." if val else "(empty)"
    logger.info("  %s = %s", key, val)
logger.info("=== END ENV ===")

_hostname = socket.gethostname()
_host_ip = socket.gethostbyname(_hostname) if _hostname else "?"
logger.info("Hostname: %s | IP: %s | Listening: 0.0.0.0:%d | Webhook: %s",
            _hostname, _host_ip, PORT, WEBHOOK_URL or "(not set)")

# ── Глобальные объекты бота ────────────────────────────────────────────────
# Default parse_mode=None — HTML используется только там где явно указано.
# Это предотвращает ошибки парсинга когда в тексте есть <...> (например "<chat_id>").
bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=None),
)
dp = Dispatcher()
dp.include_router(mod_router)

# Флаги режима
_webhook_set = False
# v4.7.3: _polling_task убран — polling теперь живёт в едином TaskGroup
# внутри lifespan (см. lifespan()). Глобальная переменная больше не нужна.


async def _start_polling():
    """Запуск Long Polling."""
    logger.info("Starting Long Polling...")
    try:
        await dp.start_polling(bot, handle_signals=False)
    except Exception as e:
        logger.error("Polling error: %s", e)


# ── v4.5.2: Night mode background task ─────────────────────────────────────
# Запускается раз в минуту. Проверяет все чаты с night_mode_enabled=True.
# Если текущее МСК-время попадает в [start, end) и night_mode_currently_active
# ещё False — делаем snapshot текущих прав чата, применяем ночные права,
# ставим night_mode_currently_active=True.
# Если время вышло из диапазона и night_mode_currently_active=True —
# восстанавливаем snapshot, ставим night_mode_currently_active=False.
# v4.5.4: ПЕРЕД night mode прогоняем sanitary day tick — если чат в sanitary
# day, night mode его пропускает (не дёргает права). Если sanitary day
# закончился — сначала восстанавливаем права из sanitary snapshot, потом
# night mode может работать как обычно.


async def _night_mode_loop():
    """Background loop: раз в минуту проверяет ночной режим для всех чатов.

    v4.5.4: сначала прогоняет sanitary day tick, потом night mode tick.
    """
    # Подождём 30 сек после старта — пусть вебхук/поллинг запустятся.
    await asyncio.sleep(30)
    logger.info("Night mode background task started (interval=60s)")
    while True:
        try:
            # v4.5.4: sanitary day tick ПЕРВЫМ — он может снять night mode.
            await _sanitary_day_tick()
            await _night_mode_tick()
        except Exception as e:
            logger.error("Night/sanitary mode tick error: %s", e)
        await asyncio.sleep(60)


async def _night_mode_tick():
    """Один проход night mode: для каждого чата с night_mode_enabled=True
    проверяет текущее время (с учётом per-chat tz + weekend schedule) и
    применяет/снимает ночные ограничения.

    v4.5.3: использует _night_mode_in_window вместо _time_str_in_range —
    это учитывает night_mode_tz и night_mode_weekend_start/end.
    """
    now = datetime.now(timezone.utc)
    try:
        async with async_session() as session:
            stmt = select(ChatSettings).where(
                ChatSettings.night_mode_enabled.is_(True),
                ChatSettings.chat_id != 0,  # пропускаем global default
                ChatSettings.is_enabled.is_(True),  # чат активен
            )
            chats = (await session.execute(stmt)).scalars().all()
    except Exception as e:
        logger.warning("Night mode: DB error loading chats: %s", e)
        return

    for cs in chats:
        try:
            # v4.5.4: пропускаем чаты в санитарном дне — night mode не должен
            # дёргать права пока активен sanitary day.
            if cs.sanitary_days_currently_active:
                continue
            tz_name = cs.night_mode_tz or "Europe/Moscow"
            in_window = _night_mode_in_window(
                now=now,
                weekday_start=cs.night_mode_start or "23:00",
                weekday_end=cs.night_mode_end or "07:00",
                weekend_start=cs.night_mode_weekend_start,
                weekend_end=cs.night_mode_weekend_end,
                tz_name=tz_name,
            )
            if in_window and not cs.night_mode_currently_active:
                # Вход в ночной режим
                await _enter_night_mode(cs)
            elif not in_window and cs.night_mode_currently_active:
                # Выход из ночного режима
                await _exit_night_mode(cs)
        except Exception as e:
            logger.error("Night mode error for chat %s: %s", cs.chat_id, e)


def _format_night_notification(
    template: str | None,
    chat_id: int,
    start: str,
    end: str,
    default_text: str,
) -> str:
    """v4.5.3: форматирует текст уведомления о входе/выходе из ночного режима.

    Поддерживает плейсхолдеры: {chat_id}, {start}, {end}.
    Если template = None — возвращает default_text.
    """
    if not template:
        return default_text
    try:
        return template.format(chat_id=chat_id, start=start, end=end)
    except (KeyError, ValueError, IndexError):
        # Битый шаблон — fallback на дефолт.
        return default_text


async def _enter_night_mode(cs: ChatSettings) -> None:
    """Применяет ночные права к чату. Делает snapshot текущих прав.

    v4.5.3: если night_mode_notify=True — отправляет уведомление в чат
    (с кастомным текстом если задан).
    v4.6.0: если cs.day_permissions задан (granular) — snapshot берётся из него,
    иначе как раньше — из текущих ChatPermissions чата.
    """
    try:
        chat_info = await bot.get_chat(chat_id=cs.chat_id)
        current_perms = chat_info.permissions
        # Сохраняем snapshot — что восстанавливать при выходе.
        # v4.6.0: если есть явное day_permissions — используем его как «истинные»
        # дневные права (это то что должно быть после выхода из ночного).
        # Иначе — берём текущие права чата (старое поведение).
        if cs.day_permissions:
            try:
                snapshot_data = json.loads(cs.day_permissions)
                # Гарантируем что все 13 полей присутствуют.
                snapshot_data = {k: bool(snapshot_data.get(k, False)) for k in _PERM_FIELDS}
            except (ValueError, TypeError):
                snapshot_data = {
                    field: bool(getattr(current_perms, field, True)) if current_perms else True
                    for field in _PERM_FIELDS
                }
        else:
            snapshot_data = {
                field: bool(getattr(current_perms, field, True)) if current_perms else True
                for field in _PERM_FIELDS
            }
        snapshot_json = json.dumps(snapshot_data)

        # Применяем ночные права
        night_perms = _parse_night_mode_permissions(cs.night_mode_permissions)
        await bot.set_chat_permissions(chat_id=cs.chat_id, permissions=night_perms)

        # Сохраняем snapshot и флаг
        async with async_session() as session:
            db_cs = (await session.execute(
                select(ChatSettings).where(ChatSettings.chat_id == cs.chat_id)
            )).scalar_one_or_none()
            if db_cs:
                db_cs.night_mode_saved_permissions = snapshot_json
                db_cs.night_mode_currently_active = True
                await session.commit()
        logger.info(
            "Night mode ON for chat %s (perms applied, snapshot saved)",
            cs.chat_id,
        )

        # v4.5.3: уведомление о входе.
        if cs.night_mode_notify:
            try:
                text = _format_night_notification(
                    cs.night_mode_notify_enter_msg,
                    cs.chat_id,
                    cs.night_mode_start or "23:00",
                    cs.night_mode_end or "07:00",
                    default_text=(
                        f"🌙 Ночной режим включён ({cs.night_mode_start} → {cs.night_mode_end}).\n"
                        "Сейчас действуют ночные ограничения."
                    ),
                )
                await bot.send_message(chat_id=cs.chat_id, text=text)
            except TelegramBadRequest as e:
                logger.warning(
                    "Night mode enter notify failed for chat %s: %s",
                    cs.chat_id, e,
                )
    except TelegramBadRequest as e:
        logger.error("Night mode enter failed for chat %s: %s", cs.chat_id, e)


async def _exit_night_mode(cs: ChatSettings) -> None:
    """Восстанавливает права чата из snapshot при выходе из ночного режима.

    v4.5.3: если night_mode_notify=True — отправляет уведомление о выходе.
    v4.6.0: приоритет восстановления:
      1. cs.day_permissions (granular, явно заданные дневные права) — лучший вариант
      2. cs.night_mode_saved_permissions (snapshot с входа в ночной режим)
      3. Fallback: «всё разрешено»
    """
    # v4.6.0: предпочтение — granular day_permissions.
    if cs.day_permissions:
        try:
            data = json.loads(cs.day_permissions)
            from aiogram import types as _tg_types
            restore_perms = _tg_types.ChatPermissions(
                **{k: bool(data.get(k, False)) for k in _PERM_FIELDS}
            )
        except (ValueError, TypeError):
            restore_perms = _fallback_all_true_perms()
    else:
        snapshot_json = cs.night_mode_saved_permissions
        if not snapshot_json:
            restore_perms = _fallback_all_true_perms()
        else:
            try:
                data = json.loads(snapshot_json)
                from aiogram import types as _tg_types
                restore_perms = _tg_types.ChatPermissions(
                    **{k: bool(data.get(k, True)) for k in _PERM_FIELDS}
                )
            except (ValueError, TypeError):
                restore_perms = _fallback_all_true_perms()

    try:
        await bot.set_chat_permissions(chat_id=cs.chat_id, permissions=restore_perms)
        async with async_session() as session:
            db_cs = (await session.execute(
                select(ChatSettings).where(ChatSettings.chat_id == cs.chat_id)
            )).scalar_one_or_none()
            if db_cs:
                db_cs.night_mode_currently_active = False
                db_cs.night_mode_saved_permissions = None
                await session.commit()
        logger.info("Night mode OFF for chat %s (perms restored)", cs.chat_id)

        # v4.5.3: уведомление о выходе.
        if cs.night_mode_notify:
            try:
                text = _format_night_notification(
                    cs.night_mode_notify_exit_msg,
                    cs.chat_id,
                    cs.night_mode_start or "23:00",
                    cs.night_mode_end or "07:00",
                    default_text=(
                        f"☀️ Ночной режим снят ({cs.night_mode_start} → {cs.night_mode_end}).\n"
                        "Обычные права чата восстановлены."
                    ),
                )
                await bot.send_message(chat_id=cs.chat_id, text=text)
            except TelegramBadRequest as e:
                logger.warning(
                    "Night mode exit notify failed for chat %s: %s",
                    cs.chat_id, e,
                )
    except TelegramBadRequest as e:
        logger.error("Night mode exit failed for chat %s: %s", cs.chat_id, e)


# ── v4.5.4: Санитарные дни (chat-level ChatPermissions lockdown) ───────────
# Запускается ПЕРЕД night mode tick. Для каждого чата с configured sanitary
# days проверяет, попадает ли текущая дата (в часовом поясе чата) в один из
# диапазонов. Если да и sanitary ещё не активен — делает snapshot текущих
# ChatPermissions, ставит все права в False (полный lockdown). Если sanitary
# активен, а день закончился — восстанавливает snapshot.
#
# Модераторы не страдают: их права выданы через promote_chat_member
# (Telegram admin rights — can_promote_users, can_delete_messages и т.д.),
# которые override'ят chat-level ChatPermissions. Это позволяет модераторам
# писать в чат даже во время полного lockdown.
#
# ВАЖНО: sanitary day имеет приоритет над night mode. _night_mode_tick
# пропускает чаты с sanitary_days_currently_active=True. При входе в sanitary
# day, если night mode был активен — сначала вызываем _exit_night_mode
# (восстанавливает day-права, чистит night-флаги), потом делаем sanitary
# snapshot уже с day-правами. При выходе из sanitary day — восстанавливаем
# snapshot, и следующий night mode tick сам войдёт в ночной режим если окно
# ещё активно.


def _today_in_tz(tz_name: str | None) -> date:
    """v4.5.4: возвращает текущую дату в указанном часовом поясе.

    Используется для проверки sanitary days — дата считается в локальном
    времени чата (не UTC). Fallback на Europe/Moscow при невалидной зоне.
    """
    tz = None
    if tz_name:
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(tz_name)
        except (ValueError, KeyError, ImportError):
            tz = None
    if tz is None:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Europe/Moscow")
    return datetime.now(timezone.utc).astimezone(tz).date()


def _now_in_tz(tz_name: str | None) -> datetime:
    """v4.7.6: возвращает текущий datetime в указанном часовом поясе.

    Возвращает aware datetime с tzinfo соответствующей зоны.
    Fallback на Europe/Moscow при невалидной зоне.
    Используется для datetime-проверок sanitary periods со временем.
    """
    tz = None
    if tz_name:
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(tz_name)
        except (ValueError, KeyError, ImportError):
            tz = None
    if tz is None:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Europe/Moscow")
    return datetime.now(timezone.utc).astimezone(tz)


async def _sanitary_day_tick():
    """v4.5.4: один проход sanitary day для всех чатов с configured sanitary days.
    v4.7.2: только для чатов с sanitary_days_enabled=True.
    v4.7.6: поддержка datetime-периодов со временем (start_time/end_time).

    Для каждого чата:
      • parse sanitary_days JSON;
      • если список пустой и sanitary не активен — skip;
      • вычислить today/now в часовом поясе чата (night_mode_tz);
      • v4.7.6: если период имеет время — datetime-проверка через now;
        иначе — date-проверка через today (старое поведение);
      • если попадает и sanitary не активен → enter;
      • если НЕ попадает и sanitary активен → exit;
      • иначе → ничего не делаем.
    """
    try:
        async with async_session() as session:
            stmt = select(ChatSettings).where(
                ChatSettings.sanitary_days_enabled.is_(True),  # v4.7.2
                ChatSettings.sanitary_days.isnot(None),
                ChatSettings.sanitary_days != "[]",
                ChatSettings.sanitary_days != "",
                ChatSettings.chat_id != 0,
                ChatSettings.is_enabled.is_(True),
            )
            chats = (await session.execute(stmt)).scalars().all()
    except Exception as e:
        logger.warning("Sanitary day: DB error loading chats: %s", e)
        return

    for cs in chats:
        try:
            pairs = parse_sanitary_days_json(cs.sanitary_days)
            if not pairs:
                continue
            today = _today_in_tz(cs.night_mode_tz)
            now_dt = _now_in_tz(cs.night_mode_tz)
            # v4.7.6: периоды со временем проверяются через now_dt (TZ-aware),
            # без времени — через today (date). is_sanitary_day_today умеет оба.
            is_today = is_sanitary_day_today(pairs, today=today, now_dt=now_dt)

            if is_today and not cs.sanitary_days_currently_active:
                await _enter_sanitary_day(cs)
            elif not is_today and cs.sanitary_days_currently_active:
                await _exit_sanitary_day(cs)
        except Exception as e:
            logger.error("Sanitary day error for chat %s: %s", cs.chat_id, e)


async def _enter_sanitary_day(cs: ChatSettings) -> None:
    """v4.5.4: переводит чат в sanitary day lockdown.

    1. Если night mode сейчас активен — сначала выходим из него (восстанавливаем
       day-права, чистим night-флаги). Это гарантирует, что sanitary snapshot
       содержит настоящие day-права, а не ночные.
    2. Делаем snapshot текущих ChatPermissions.
    3. Ставим все права в False (полный lockdown) — ИЛИ используем granular
       sanitary_days_permissions если задан (v4.6.0).
    4. Сохраняем snapshot и флаг.
    """
    try:
        # 1. Если night mode активен — сначала выходим из него.
        if cs.night_mode_currently_active:
            try:
                await _exit_night_mode(cs)
            except Exception as e:
                logger.warning(
                    "Sanitary day: could not exit night mode first for chat %s: %s "
                    "(will proceed anyway)",
                    cs.chat_id, e,
                )
            # Re-fetch cs т.к. _exit_night_mode коммитил в DB.
            async with async_session() as session:
                fresh = (await session.execute(
                    select(ChatSettings).where(ChatSettings.chat_id == cs.chat_id)
                )).scalar_one_or_none()
                if fresh:
                    cs = fresh

        # 2. Snapshot текущих прав.
        # v4.6.0: если есть явное day_permissions — используем его как snapshot.
        if cs.day_permissions:
            try:
                snapshot_data = json.loads(cs.day_permissions)
                snapshot_data = {k: bool(snapshot_data.get(k, False)) for k in _PERM_FIELDS}
            except (ValueError, TypeError):
                chat_info = await bot.get_chat(chat_id=cs.chat_id)
                current_perms = chat_info.permissions
                snapshot_data = {
                    field: bool(getattr(current_perms, field, True)) if current_perms else True
                    for field in _PERM_FIELDS
                }
        else:
            chat_info = await bot.get_chat(chat_id=cs.chat_id)
            current_perms = chat_info.permissions
            snapshot_data = {
                field: bool(getattr(current_perms, field, True)) if current_perms else True
                for field in _PERM_FIELDS
            }
        snapshot_json = json.dumps(snapshot_data)

        # 3. Применяем sanitary-права.
        # v4.6.0: если cs.sanitary_days_permissions задан — используем granular,
        # иначе — полный lockdown (all False, как раньше).
        if cs.sanitary_days_permissions:
            try:
                data = json.loads(cs.sanitary_days_permissions)
                from aiogram import types as _tg_types
                lockdown_perms = _tg_types.ChatPermissions(
                    **{k: bool(data.get(k, False)) for k in _PERM_FIELDS}
                )
            except (ValueError, TypeError):
                from aiogram import types as _tg_types
                lockdown_perms = _tg_types.ChatPermissions(
                    **{k: False for k in _PERM_FIELDS}
                )
        else:
            from aiogram import types as _tg_types
            lockdown_perms = _tg_types.ChatPermissions(
                **{k: False for k in _PERM_FIELDS}
            )
        await bot.set_chat_permissions(chat_id=cs.chat_id, permissions=lockdown_perms)

        # 4. Сохраняем snapshot и флаг.
        async with async_session() as session:
            db_cs = (await session.execute(
                select(ChatSettings).where(ChatSettings.chat_id == cs.chat_id)
            )).scalar_one_or_none()
            if db_cs:
                db_cs.sanitary_days_saved_permissions = snapshot_json
                db_cs.sanitary_days_currently_active = True
                await session.commit()
        logger.info(
            "Sanitary day ON for chat %s (lockdown applied, snapshot saved)",
            cs.chat_id,
        )
    except TelegramBadRequest as e:
        logger.error("Sanitary day enter failed for chat %s: %s", cs.chat_id, e)


async def _exit_sanitary_day(cs: ChatSettings) -> None:
    """v4.5.4: восстанавливает права чата из snapshot при выходе из sanitary day.

    После восстановления night mode tick (запускается сразу после sanitary
    tick в _night_mode_loop) сам решит, нужно ли входить в ночной режим —
    если окно ещё активно, он сделает свежий snapshot и применит ночные права.

    v4.6.0: приоритет восстановления:
      1. cs.day_permissions (granular, явно заданные дневные права) — лучший вариант
      2. cs.sanitary_days_saved_permissions (snapshot с входа в sanitary)
      3. Fallback: «всё разрешено»

    v4.6.0: после выхода — проставляем last_sanitary_month=current month чтобы
    suppress dashboard warning "нет дат на след. месяц" если санитарный день
    уже прошёл в этом месяце.
    """
    # v4.6.0: предпочтение — granular day_permissions.
    if cs.day_permissions:
        try:
            data = json.loads(cs.day_permissions)
            from aiogram import types as _tg_types
            restore_perms = _tg_types.ChatPermissions(
                **{k: bool(data.get(k, False)) for k in _PERM_FIELDS}
            )
        except (ValueError, TypeError):
            restore_perms = _fallback_all_true_perms()
    else:
        snapshot_json = cs.sanitary_days_saved_permissions
        if not snapshot_json:
            restore_perms = _fallback_all_true_perms()
        else:
            try:
                data = json.loads(snapshot_json)
                from aiogram import types as _tg_types
                restore_perms = _tg_types.ChatPermissions(
                    **{k: bool(data.get(k, True)) for k in _PERM_FIELDS}
                )
            except (ValueError, TypeError):
                restore_perms = _fallback_all_true_perms()

    try:
        await bot.set_chat_permissions(chat_id=cs.chat_id, permissions=restore_perms)

        # v4.6.0: проставляем last_sanitary_month и чистим старые месяцы.
        from datetime import datetime as _dt, timezone as _tz
        # Часовой пояс чата (берём из night_mode_tz — общий для всего chat_settings).
        tz_name = cs.night_mode_tz or "Europe/Moscow"
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(tz_name)
        except (ValueError, KeyError, ImportError):
            tz = _tz.utc
        current_month_str = _dt.now(tz).strftime("%Y-%m")

        async with async_session() as session:
            db_cs = (await session.execute(
                select(ChatSettings).where(ChatSettings.chat_id == cs.chat_id)
            )).scalar_one_or_none()
            if db_cs:
                db_cs.sanitary_days_currently_active = False
                db_cs.sanitary_days_saved_permissions = None
                # v4.6.0: monthly cleanup — удаляем ключ текущего месяца из sanitary_days
                # JSON и проставляем last_sanitary_month чтобы suppress warnings.
                db_cs.last_sanitary_month = current_month_str
                if db_cs.sanitary_days:
                    try:
                        sd_data = json.loads(db_cs.sanitary_days)
                        if isinstance(sd_data, dict):
                            # Удаляем ключ текущего месяца (сан. день прошёл).
                            sd_data.pop(current_month_str, None)
                            db_cs.sanitary_days = json.dumps(sd_data) if sd_data else None
                    except (ValueError, TypeError):
                        pass
                await session.commit()
        logger.info("Sanitary day OFF for chat %s (perms restored)", cs.chat_id)
    except TelegramBadRequest as e:
        logger.error("Sanitary day exit failed for chat %s: %s", cs.chat_id, e)


def _fallback_all_true_perms():
    """v4.6.0: ChatPermissions со всеми True — фоллбэк когда нет ни snapshot,
    ни granular day_permissions. Используется в _exit_night_mode / _exit_sanitary_day.
    """
    from aiogram import types as _tg_types
    return _tg_types.ChatPermissions(
        can_send_messages=True, can_send_audios=True, can_send_documents=True,
        can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
        can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True,
        can_add_web_page_previews=True, can_change_info=True, can_invite_users=True,
        can_pin_messages=True,
    )


# ── v4.7.3: константа hard shutdown timeout ────────────────────────────────
# На SIGTERM даём фоновым задачам максимум 5 секунд на graceful cancel.
# Если за это время они не завершились — логируем и выходим принудительно
# (event loop всё равно закроется, задачи умрут с PendingCancellation).
_SHUTDOWN_TIMEOUT_SECONDS: float = 5.0


# ── v4.7.3: Startup recovery ──────────────────────────────────────────────
# При жёстком SIGTERM в предыдущем запуске чат мог остаться с
# night_mode_currently_active=True или sanitary_days_currently_active=True,
# но права не были восстановлены (тик оборвался на середине). Snapshot лежит
# в БД (night_mode_saved_permissions), так что recovery возможен — нужно
# просто прогнать один tick сразу после старта.
async def _startup_recovery() -> None:
    """v4.7.3: проверяет чаты с зависшими active-флагами и прогоняет tick.

    Если чат имеет night_mode_currently_active=True, но сейчас не в окне
    night mode — _night_mode_tick() восстановит права из snapshot и снимет
    флаг. Если до сих пор в окне — ничего не произойдёт (already active).
    Аналогично для sanitary_days_currently_active.
    """
    try:
        async with async_session() as session:
            from sqlalchemy import or_
            stmt = select(ChatSettings).where(
                or_(
                    ChatSettings.night_mode_currently_active.is_(True),
                    ChatSettings.sanitary_days_currently_active.is_(True),
                ),
                ChatSettings.chat_id != 0,  # пропускаем global default
            )
            stuck = (await session.execute(stmt)).scalars().all()
        if not stuck:
            return
        logger.warning(
            "Startup recovery: %d chats have stuck active flags — "
            "running immediate tick to reconcile:",
            len(stuck),
        )
        for cs in stuck:
            logger.warning(
                "  chat %s: night_active=%s sanitary_active=%s",
                cs.chat_id,
                bool(cs.night_mode_currently_active),
                bool(cs.sanitary_days_currently_active),
            )
        # Прогоняем tick — sanitary ПЕРВЫМ (он имеет приоритет над night).
        # Tick сам разберётся: снимет active-флаг если окно вышло,
        # оставит если всё ещё в окне.
        try:
            await _sanitary_day_tick()
            await _night_mode_tick()
            logger.info("Startup recovery: tick completed, state reconciled")
        except Exception as e:
            logger.error("Startup recovery: tick failed: %s", e)
    except Exception as e:
        logger.warning("Startup recovery: check failed: %s", e)


# ── Lifespan ────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app):
    global _webhook_set

    # ── Startup ─────────────────────────────────────────────────
    await init_db()
    logger.info("DB initialized (WAL mode)")

    # v4.7.3: recovery для чатов с зависшими active-флагами после жёсткого
    # SIGTERM в предыдущем запуске. Должен идти ДО запуска background loop,
    # чтобы loop не подхватил полузавершённое состояние.
    await _startup_recovery()

    # Убираем команды из меню (стелс)
    try:
        await bot.delete_my_commands()
        logger.info("Bot commands cleared (stealth mode)")
    except Exception as e:
        logger.warning("delete_my_commands failed: %s", e)

    # ── Проверяем глобальный default репорт-чат из DB (chat_id=0) ──
    try:
        async with async_session() as _sess:
            _default_cs = (await _sess.execute(
                select(ChatSettings).where(ChatSettings.chat_id == 0)
            )).scalar_one_or_none()
        _default_rc = _default_cs.report_chat_id if _default_cs else None
    except Exception as _e:
        logger.warning("Could not read default report_chat_id from DB: %s", _e)
        _default_rc = None

    if _default_rc:
        try:
            chat_info = await bot.get_chat(chat_id=_default_rc)
            logger.info("Default report chat (DB chat_id=0) OK: id=%s title='%s' type='%s'",
                        chat_info.id, chat_info.title or "", chat_info.type)
        except Exception as e:
            logger.error(
                "⚠️ Default report_chat_id=%s (DB chat_id=0) is NOT accessible: %s\n"
                "   Make sure the bot is added as admin to the report channel/group!\n"
                "   Per-chat overrides can be set via /setreport command.",
                _default_rc, e,
            )
    else:
        logger.warning("Default report_chat_id not set in DB — set via /setreport default <chat_id>")

    # Пробуем установить вебхук
    if WEBHOOK_URL:
        try:
            # v4.5.1: передаём secret_token — Telegram будет слать его
            # в заголовке X-Telegram-Bot-Api-Secret-Token на каждый запрос.
            # Без проверки кто угодно может POST-нуть фейковый Update.
            await bot.set_webhook(
                url=WEBHOOK_URL,
                allowed_updates=["message", "my_chat_member"],
                secret_token=WEBHOOK_SECRET,
            )
            info = await bot.get_webhook_info()
            logger.info("Webhook set to %s (info.url=%s, secret_token=set)", WEBHOOK_URL, info.url)
            _webhook_set = True
        except Exception as e:
            logger.error("set_webhook FAILED: %s — falling back to polling", e)
            _webhook_set = False

    # Если вебхук не установлен — Long Polling
    use_polling = not _webhook_set
    if use_polling:
        if WEBHOOK_URL:
            logger.info("Webhook not confirmed — deleting webhook and starting Long Polling")
            try:
                await bot.delete_webhook()
            except Exception:
                pass
        else:
            logger.info("WEBHOOK_URL not set — using Long Polling mode")

    # v4.7.3: единый asyncio.TaskGroup для ВСЕХ background loops.
    # Раньше задачи создавались через asyncio.create_task и «забывались» —
    # при SIGTERM процесс обрывался, не дожидаясь завершения тика night mode,
    # что могло оставить чат в состоянии «night mode active» с зависшим флагом.
    # Теперь все loops живут в одном TaskGroup, shutdown отменяет их и ждёт
    # завершения с hard timeout = _SHUTDOWN_TIMEOUT_SECONDS (5s).
    polling_task: asyncio.Task | None = None
    try:
        async with asyncio.TaskGroup() as tg:
            # Night mode loop — всегда запускается (даже если все чаты disabled,
            # loop просто ничего не делает; дешевле чем проверять перед запуском).
            night_task = tg.create_task(
                _night_mode_loop(), name="night_mode_loop",
            )
            # Long polling — только если вебхук не установлен.
            if use_polling:
                polling_task = tg.create_task(
                    _start_polling(), name="long_polling",
                )

            yield  # ← uvicorn обслуживает запросы здесь

            # ── Shutdown ────────────────────────────────────────────────
            # На выходе из yield (получен SIGTERM/uvicorn shutdown) — отменяем
            # все фоновые задачи. TaskGroup на выходе из async with дождётся
            # их завершения; мы добавляем сверху asyncio.wait_for с hard cap,
            # чтобы зависшая задача не подвесила весь shutdown.
            logger.info("Shutdown: cancelling background tasks...")
            bg_tasks = [night_task]
            if polling_task is not None:
                bg_tasks.append(polling_task)
            for t in bg_tasks:
                if not t.done():
                    t.cancel()
            try:
                await asyncio.wait_for(
                    asyncio.gather(*bg_tasks, return_exceptions=True),
                    timeout=_SHUTDOWN_TIMEOUT_SECONDS,
                )
                logger.info(
                    "Shutdown: all background tasks cancelled cleanly within %.1fs",
                    _SHUTDOWN_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                still_running = [t.get_name() for t in bg_tasks if not t.done()]
                logger.error(
                    "Shutdown: %.1fs timeout reached — %d tasks still running: %s. "
                    "Force-exiting (event loop will close, tasks will be killed).",
                    _SHUTDOWN_TIMEOUT_SECONDS,
                    len(still_running),
                    still_running,
                )
                # Last-resort: повторный cancel. TaskGroup попытается дождаться
                # на выходе, но event loop уже закрывается —Tasks умрут.
                for t in bg_tasks:
                    if not t.done():
                        t.cancel()
    except* asyncio.CancelledError:
        # Ожидаемо при shutdown — uvicorn может кинуть CancelledError в lifespan.
        pass

    # ── Webhook cleanup ────────────────────────────────────────────
    try:
        await bot.delete_webhook()
    except Exception:
        pass
    await bot.session.close()
    logger.info("Shutdown complete")


# ── Создаём приложение с lifespan ──────────────────────────────────────────
# v4.4: передаём bot в web_app, чтобы эндпоинт /admin/users/create мог дёргать
# bot.get_chat(user_id) для получения профиля из Telegram по TGID.
app = create_app(lifespan=lifespan, bot=bot)


# ── Webhook endpoint — Telegram шлёт сюда обновления ───────────────────────
@app.post(WEBHOOK_PATH)
async def bot_webhook(update: dict, request: fastapi.Request):
    """Telegram отправляет POST с Update на этот эндпоинт.

    v4.5.1: проверяем заголовок X-Telegram-Bot-Api-Secret-Token —
    Telegram присылает тот secret_token, который мы передали в set_webhook.
    Если не совпадает — отбрасываем запрос (защита от подделки Update).
    """
    # v4.5.1: проверка секретного токена webhook
    incoming_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not incoming_secret or incoming_secret != WEBHOOK_SECRET:
        # Не логируем на WARNING — может быть сканер/бот. INFO достаточно.
        logger.info("webhook: rejected (missing/invalid secret_token) from %s",
                    request.client.host if request.client else "?")
        return {"ok": False, "error": "unauthorized"}
    try:
        telegram_update = types.Update.model_validate(update)
        await dp.feed_update(bot=bot, update=telegram_update)
    except Exception as e:
        logger.error("Webhook feed_update error: %s", e)
    return {"ok": True}


# ── Диагностический эндпоинт (не зависит от авторизации) ──────────────────
@app.get("/ping")
async def ping():
    """Простой пинг для проверки, что сервер доступен."""
    return {
        "status": "ok",
        "time": time.time(),
        "webhook_mode": _webhook_set,
        "webhook_url": WEBHOOK_URL,
        "port": PORT,
    }


if __name__ == "__main__":
    logger.info("Starting Uvicorn on 0.0.0.0:%d", PORT)
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=PORT,
        log_level="info",
        timeout_keep_alive=30,
    )
