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
_polling_task = None


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
    """
    try:
        chat_info = await bot.get_chat(chat_id=cs.chat_id)
        current_perms = chat_info.permissions
        # Сохраняем snapshot — что восстанавливать при выходе
        snapshot_data = {}
        for field in _PERM_FIELDS:
            snapshot_data[field] = bool(getattr(current_perms, field, True)) if current_perms else True
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
    """
    snapshot_json = cs.night_mode_saved_permissions
    if not snapshot_json:
        # Нет snapshot — даём дефолтные "всё разрешено"
        from aiogram import types as _tg_types
        restore_perms = _tg_types.ChatPermissions(
            can_send_messages=True, can_send_audios=True, can_send_documents=True,
            can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
            can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True,
            can_add_web_page_previews=True, can_change_info=True, can_invite_users=True,
            can_pin_messages=True,
        )
    else:
        try:
            data = json.loads(snapshot_json)
            from aiogram import types as _tg_types
            restore_perms = _tg_types.ChatPermissions(
                **{k: data.get(k, True) for k in _PERM_FIELDS}
            )
        except (ValueError, TypeError):
            from aiogram import types as _tg_types
            restore_perms = _tg_types.ChatPermissions(can_send_messages=True)

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


async def _sanitary_day_tick():
    """v4.5.4: один проход sanitary day для всех чатов с configured sanitary days.

    Для каждого чата:
      • parse sanitary_days JSON;
      • если список пустой и sanitary не активен — skip;
      • вычислить today в часовом поясе чата (night_mode_tz);
      • если today попадает в один из диапазонов и sanitary не активен → enter;
      • если today НЕ попадает и sanitary активен → exit;
      • иначе → ничего не делаем.
    """
    try:
        async with async_session() as session:
            stmt = select(ChatSettings).where(
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
            is_today = is_sanitary_day_today(pairs, today=today)

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
    3. Ставим все права в False (полный lockdown).
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
        chat_info = await bot.get_chat(chat_id=cs.chat_id)
        current_perms = chat_info.permissions
        snapshot_data = {}
        for field in _PERM_FIELDS:
            snapshot_data[field] = bool(getattr(current_perms, field, True)) if current_perms else True
        snapshot_json = json.dumps(snapshot_data)

        # 3. Применяем полный lockdown — все права False.
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
    """
    snapshot_json = cs.sanitary_days_saved_permissions
    if not snapshot_json:
        # Нет snapshot — даём дефолтные "всё разрешено".
        from aiogram import types as _tg_types
        restore_perms = _tg_types.ChatPermissions(
            can_send_messages=True, can_send_audios=True, can_send_documents=True,
            can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
            can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True,
            can_add_web_page_previews=True, can_change_info=True, can_invite_users=True,
            can_pin_messages=True,
        )
    else:
        try:
            data = json.loads(snapshot_json)
            from aiogram import types as _tg_types
            restore_perms = _tg_types.ChatPermissions(
                **{k: data.get(k, True) for k in _PERM_FIELDS}
            )
        except (ValueError, TypeError):
            from aiogram import types as _tg_types
            restore_perms = _tg_types.ChatPermissions(can_send_messages=True)

    try:
        await bot.set_chat_permissions(chat_id=cs.chat_id, permissions=restore_perms)
        async with async_session() as session:
            db_cs = (await session.execute(
                select(ChatSettings).where(ChatSettings.chat_id == cs.chat_id)
            )).scalar_one_or_none()
            if db_cs:
                db_cs.sanitary_days_currently_active = False
                db_cs.sanitary_days_saved_permissions = None
                await session.commit()
        logger.info("Sanitary day OFF for chat %s (perms restored)", cs.chat_id)
    except TelegramBadRequest as e:
        logger.error("Sanitary day exit failed for chat %s: %s", cs.chat_id, e)


# ── Lifespan ────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app):
    global _webhook_set, _polling_task

    # ── Startup ─────────────────────────────────────────────────
    await init_db()
    logger.info("DB initialized (WAL mode)")

    # Убираем команды из меню (стелс)
    try:
        await bot.delete_my_commands()
        logger.info("Bot commands cleared (stealth mode)")
    except Exception as e:
        logger.warning("delete_my_commands failed: %s", e)

    # v4.5.2: запускаем night mode background task
    night_mode_task = asyncio.create_task(_night_mode_loop())

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
    if not _webhook_set:
        if WEBHOOK_URL:
            logger.info("Webhook not confirmed — deleting webhook and starting Long Polling")
            try:
                await bot.delete_webhook()
            except Exception:
                pass
        else:
            logger.info("WEBHOOK_URL not set — using Long Polling mode")
        _polling_task = asyncio.create_task(_start_polling())

    yield

    # ── Shutdown ────────────────────────────────────────────────
    if _polling_task:
        _polling_task.cancel()
        try:
            await _polling_task
        except asyncio.CancelledError:
            pass
    # v4.5.2: отменяем night mode background task
    night_mode_task.cancel()
    try:
        await night_mode_task
    except asyncio.CancelledError:
        pass
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
