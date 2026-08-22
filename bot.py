"""
bot.py — Точка входа: FastAPI + Aiogram.
Режим работы определяется автоматически:
  - Если WEBHOOK_URL задан И вебхук удалось установить → webhook
  - Иначе → Long Polling (надёжный фоллбэк)

FastAPI запускается всегда — для веб-панели (когда Bothost починит Traefik).
"""

import asyncio

# v4.8.9: хак `sys.modules.setdefault("bot", _self_module)` удалён.
# Вместо него используется чистый паттерн "service locator": bot.py при
# старте регистрирует свои функции в app_state.py, а bot_handlers.py /
# web_app.py достают их через `from app_state import get_*()`.
# См. 03_TASK_v4.8.9.md §3 и 10_KEY_DECISIONS.md §7.
#
# Раньше (до v4.8.9): бот запускался как `python bot.py` (т.е. __main__,
# а не bot), поэтому `from bot import X` приводил к повторному import bot.py
# со side-effectами (dp.include_router, startup tasks, etc.) →
# RuntimeError: Router is already attached. Хак решал это, но путал IDE
# и ломал тесты. v4.8.9 — чистое решение через app_state.py.
import json
import logging
import os
import secrets
import socket
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone

import fastapi
import uvicorn
from aiogram import Bot, Dispatcher, types
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
)
from sqlalchemy import select

import commands as commands_registry

# v4.8.0: унифицированная логика режимов чата (snapshot/restore/apply).
# Используется в _enter_night_mode, _enter_sanitary_day, _restore_day_state.
# См. chat_modes.py для архитектурных инвариантов и приоритета режимов.
import health_probe

# v4.5.2: helpers для night mode background task (defined in bot_handlers)
# v4.5.3: добавлен _night_mode_in_window для поддержки per-chat tz + weekend.
# v4.5.4: добавлены helpers для санитарных дней (chat-level ChatPermissions lockdown).
from bot_handlers import (
    # v4.7.20: env-chats cleanup при старте — если чат из CHAT_HASHTAGS
    # не отвечает (бот кикнут / чат удалён), помечаем is_enabled=False.
    # Раньше бот при каждом апдейте пересоздавал chat_settings для мёртвых
    # чатов из env (CHAT_HASHTAGS=-1003972381175:Test), и SU не мог их
    # удалить из веб-панели — они "воскресали" при следующем сообщении.
    _CHAT_HASHTAGS,
    _PERM_FIELDS,
    # v4.7.22: SetChatSlowModeDelay — обёртка над setChatSlowModeDelay Telegram
    # Bot API method (aiogram 3.30 не имеет её). Раньше класс был определён в
    # bot.py, но late import `from bot import SetChatSlowModeDelay` из
    # bot_handlers.py вызывал повторный import bot.py как отдельный модуль
    # (т.к. bot.py запускается как __main__, не как bot) → side-effectы
    # (dp.include_router) выполнялись повторно → RuntimeError: Router is
    # already attached. Теперь класс живёт в bot_handlers.py — bot.py
    # импортирует его как обычный symbol.
    SetChatSlowModeDelay,
    # v4.7.20: !alarm integration — _deactivate_alarm используется в
    # _night_mode_tick для (1) auto-off при истечении alarm_active_until,
    # (2) auto-deactivate при входе в night mode (alarm избыточен когда
    # night mode уже ограничивает права).
    _deactivate_alarm,
    # v4.7.18: night mode / day mode notifications go to report chat, not
    # to the public chat. Reuse the existing resolver — it honours per-chat
    # override (ChatSettings.report_chat_id), is_report_chat flag, and the
    # global default (chat_id=0).
    _get_report_chat_id,
    _night_mode_in_window,
    _parse_night_mode_permissions,
    is_sanitary_day_today,
    parse_sanitary_days_json,
    send_latency_alert_to_su,
)
from bot_handlers import router as mod_router
from chat_modes import (
    _alarm_auto_off_tick,  # v4.8.9: перенесено из bot.py
    _apply_chat_permissions,
    _mode_priority,
    _resolve_restore_perms_async,
    _snapshot_chat_permissions,
)
from db import ChatSettings, async_session, init_db_with_fallback
from web_app import create_app

# v4.7.19: TelegramAPIError — базовый класс для ВСЕХ Telegram-ошибок
# (TelegramNotFound, TelegramForbiddenError, TelegramConflictError, ...).
# Раньше ловили только TelegramBadRequest, но TelegramNotFound ("Not Found"
# при отправке в удалённый/несуществующий чат) и TelegramForbiddenError
# ("bot was kicked from chat") — это ОТДЕЛЬНЫЕ классы, не наследники
# TelegramBadRequest. Из-за этого исключение пробивалось наверх в
# _night_mode_tick и засоряло лог ERROR'ами каждую минуту. Теперь ловим
# базовый класс — любая ошибка Telegram логируется как warning и не валит tick.

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

# v4.7.13: убран ENV DUMP — был полезен при отладке webhook/домена,
# сейчас только мусорит в логах. Секреты (BOT_TOKEN, WEB_PASSWORD, etc.)
# по-прежнему не логируем нигде. Запуск кода ниже — единственный источник
# релевантной startup-инфо (webhook URL, port) в логах.
_hostname = socket.gethostname()
_host_ip = socket.gethostbyname(_hostname) if _hostname else "?"
logger.info("Startup: host=%s ip=%s port=%d webhook=%s",
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
        # v4.7.27: явно указываем allowed_updates — иначе aiogram getUpdates
        # по умолчанию НЕ возвращает chat_member updates (только message,
        # my_chat_member). Это ломает детектирование ручных банов.
        await dp.start_polling(
            bot,
            handle_signals=False,
            allowed_updates=["message", "my_chat_member", "chat_member"],
        )
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
            # v4.7.30: alarm auto-off ПЕРВЫМ — снимает зависшие alarm'ы
            # с истёкшим alarm_active_until. Должно идти ДО sanitary/night,
            # чтобы alarm снялся до любых других манипуляций с правами чата.
            await _alarm_auto_off_tick(bot)
            # v4.5.4: sanitary day tick ВТОРЫМ — он может снять night mode.
            await _sanitary_day_tick()
            await _night_mode_tick()
        except Exception as e:
            logger.error("Alarm/sanitary/night mode tick error: %s", e)
        await asyncio.sleep(60)


async def _health_probe_loop():
    """Background loop: раз в минуту проверяет связь с Telegram для /healthz.

    v4.10.2 (Task 16): отдельная таска, а НЕ тик внутри _night_mode_loop.
    Там инвариант порядка (alarm → sanitary → night), и подвисший get_me
    задержал бы снятие режимов чата — права в чате «залипли» бы до
    следующего круга.

    Результат кладётся в health_probe, роут /healthz читает готовый снимок
    и в сеть не ходит: мониторинг опрашивает его куда чаще, чем раз в
    минуту, и каждый такой опрос уходил бы в Bot API.
    """
    # Как и night_mode_loop, ждём подъёма вебхука/поллинга.
    await asyncio.sleep(30)
    logger.info("Health probe background task started (interval=60s)")
    while True:
        # probe_tick сам глушит исключения: таска, падающая от сетевого
        # сбоя, перестала бы следить за здоровьем ровно когда это нужнее.
        await health_probe.probe_tick(bot)

        # v5.0.0 (roadmap 5.0.0-08): устойчивые задержки Telegram — повод
        # предупредить SU до того, как торможение станет отказом. Условие
        # (пять медленных подряд + антиспам 30 минут) живёт в health_probe.
        try:
            if health_probe.should_alert():
                snap = health_probe.snapshot()
                await send_latency_alert_to_su(
                    bot,
                    streak=health_probe._ALERT_STREAK,
                    avg_ms=health_probe.latency_average_ms(),
                    last_ms=snap["telegram_api_latency_ms"],
                )
                health_probe.mark_alert_sent()
        except Exception as e:
            # Сбой алерта не должен останавливать наблюдение за здоровьем.
            logger.warning("health probe: latency alert failed: %s", e)

        await asyncio.sleep(60)


# v4.8.9: _alarm_auto_off_tick перенесена в chat_modes.py (это её домен).
# Импортируется в начале файла через `from chat_modes import _alarm_auto_off_tick`.


async def _night_mode_tick():
    """Один проход night mode: для каждого чата с night_mode_enabled=True
    проверяет текущее время (с учётом per-chat tz + weekend schedule) и
    применяет/снимает ночные ограничения.

    v4.5.3: использует _night_mode_in_window вместо _time_str_in_range —
    это учитывает night_mode_tz и night_mode_weekend_start/end.

    v4.7.30: auto-off alarm вынесен в _alarm_auto_off_tick (отдельная функция
    в _night_mode_loop). Здесь осталась только логика "перед входом в night
    mode снять активный alarm" (т.к. это специфично именно для night mode).
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
                # v4.7.20: перед входом в night mode — снять активный alarm.
                # Night mode и так ограничивает права, alarm избыточен.
                # Snapshot alarm'а будет утерян, но это OK: night mode сохранит
                # свой snapshot прав (в _enter_night_mode), и при выходе из
                # night mode права восстановятся из него.
                if cs.alarm_currently_active:
                    logger.info(
                        "Alarm auto-deactivate on night mode enter: chat %s",
                        cs.chat_id,
                    )
                    started_by_night = None
                    try:
                        async with async_session() as alarm_session:
                            alarm_cs = (await alarm_session.execute(
                                select(ChatSettings).where(
                                    ChatSettings.chat_id == cs.chat_id
                                )
                            )).scalar_one_or_none()
                            if alarm_cs and alarm_cs.alarm_currently_active:
                                started_by_night = alarm_cs.alarm_started_by
                                # reason="night_mode_enter" — в логах будет видно
                                # что alarm снят именно из-за входа в night, а не
                                # из-за таймаута или ручного off.
                                await _deactivate_alarm(
                                    alarm_session, alarm_cs, bot,
                                    cs.chat_id, reason="night_mode_enter",
                                )
                                cs.alarm_currently_active = False
                                cs.alarm_saved_permissions = None
                                cs.alarm_saved_slow_mode_delay = None
                                cs.alarm_active_until = None
                    except Exception as e:
                        logger.error(
                            "Alarm auto-deactivate on night mode enter failed "
                            "for chat %s: %s (continuing — night mode will "
                            "still apply its own perms)",
                            cs.chat_id, e,
                        )
                    # v4.8.0: отправляем событие в modchat.
                    if started_by_night is not None:
                        try:
                            from modchat import _send_alarm_event_to_modchat
                            await _send_alarm_event_to_modchat(
                                bot=bot, chat_id=cs.chat_id, event_type="off_by_mode",
                                mod_id=started_by_night,
                                reason="вход в night mode",
                            )
                        except Exception as modchat_e:
                            logger.debug(
                                "Modchat alarm off-by-night event failed for chat %s: %s",
                                cs.chat_id, modchat_e,
                            )
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
    v4.7.16: если cs.night_mode_slow_mode_delay > 0 — дополнительно
    применяет slow_mode (минимальный интервал между сообщениями).
    Snapshot текущего slow_mode_delay сохраняется в
    night_mode_saved_slow_mode_delay для восстановления при выходе.
    v4.8.0: snapshot делается через _snapshot_chat_permissions (chat_modes.py),
    унифицированно с alarm и sanitary day.
    """
    try:
        # v4.8.0: унифицированный snapshot.
        # day_permissions передаётся для приоритета preset'а над текущими правами.
        snapshot_json, current_slow_mode = await _snapshot_chat_permissions(
            bot=bot, chat_id=cs.chat_id, day_permissions=cs.day_permissions,
        )

        # Применяем ночные права
        night_perms = _parse_night_mode_permissions(cs.night_mode_permissions)
        # v4.8.0: используем унифицированную обёртку _apply_chat_permissions
        # (use_independent_chat_permissions=True внутри).
        ok = await _apply_chat_permissions(bot, cs.chat_id, night_perms)
        if not ok:
            logger.error(
                "Night mode: failed to apply permissions for chat %s — aborting",
                cs.chat_id,
            )
            return

        # v4.7.16: применяем ночной slow_mode если задан (>0).
        # 0 = не трогать slow_mode (backward compat, чат остаётся как есть).
        # Диапазон Telegram: 0..36400 сек. Контроль на стороне записи настройки.
        night_slow = int(cs.night_mode_slow_mode_delay or 0)
        if night_slow > 0:
            try:
                await bot(SetChatSlowModeDelay(
                    chat_id=cs.chat_id, slow_mode_delay=night_slow,
                ))
                logger.info(
                    "Night mode: slow_mode set to %ds for chat %s",
                    night_slow, cs.chat_id,
                )
            except TelegramAPIError as e:
                logger.warning(
                    "Night mode: set_chat_slow_mode_delay(%ds) failed for chat %s: %s",
                    night_slow, cs.chat_id, e,
                )

        # Сохраняем snapshot и флаг
        async with async_session() as session:
            db_cs = (await session.execute(
                select(ChatSettings).where(ChatSettings.chat_id == cs.chat_id)
            )).scalar_one_or_none()
            if db_cs:
                db_cs.night_mode_saved_permissions = snapshot_json
                db_cs.night_mode_currently_active = True
                # v4.7.16: snapshot slow_mode_delay для восстановления при выходе.
                db_cs.night_mode_saved_slow_mode_delay = current_slow_mode
                await session.commit()
        logger.info(
            "Night mode ON for chat %s (perms applied, snapshot saved, "
            "saved_slow_mode=%ds, mode=%s)",
            cs.chat_id, current_slow_mode, _mode_priority(cs),
        )

        # v4.5.3: уведомление о входе.
        # v4.7.18: уведомление идёт в репорт-чат, а НЕ в общий чат. Раньше
        # бот писал «🌙 Ночной режим включён …» прямо в cs.chat_id — это
        # засоряло чат модераторским шумом и было видно обычным юзерам.
        # Теперь — через _get_report_chat_id(session, cs.chat_id):
        #   1. Per-chat override (ChatSettings.report_chat_id для данного chat_id)
        #   2. Любой чат с is_report_chat=True
        #   3. Глобальный default (chat_id=0)
        #   4. None — репорт-чата нет → лог warning, уведомление не отправляем.
        # Это намеренный разрыв с прошлым поведением: если репорт-чат не
        # настроен — уведомление пропускается, а не падает в общий чат.
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
                async with async_session() as notify_session:
                    report_chat_id = await _get_report_chat_id(
                        notify_session, cs.chat_id,
                    )
                if report_chat_id is None:
                    logger.warning(
                        "Night mode enter notify for chat %s: no report chat "
                        "configured — skipping (set report_chat_id or "
                        "is_report_chat=True to receive night mode notifications)",
                        cs.chat_id,
                    )
                else:
                    await bot.send_message(chat_id=report_chat_id, text=text)
                    logger.info(
                        "Night mode enter notify sent to report chat %s "
                        "(source chat %s)",
                        report_chat_id, cs.chat_id,
                    )
            except TelegramAPIError as e:
                # v4.7.19: ловим базовый класс TelegramAPIError вместо
                # TelegramBadRequest — TelegramNotFound ("Not Found" при
                # отправке в удалённый чат) и TelegramForbiddenError ("bot
                # was kicked") не наследуют TelegramBadRequest и раньше
                # пробивались наверх в _night_mode_tick, засоряя лог ERROR'ами.
                # Также логируем report_chat_id — сразу видно, какой ID кривой.
                logger.warning(
                    "Night mode enter notify failed: source_chat=%s, "
                    "report_chat=%s, error=%s",
                    cs.chat_id, report_chat_id, e,
                )
    except TelegramAPIError as e:
        # v4.7.19: та же логика — ловим базовый класс. Если bot.get_chat /
        # bot.set_chat_permissions падают с NotFound (чат удалён/бот кикнут) —
        # логируем как error (это уже не notify, а сам night mode не сработал),
        # но tick продолжается для остальных чатов.
        logger.error("Night mode enter failed for chat %s: %s", cs.chat_id, e)


# ── v4.7.12: Hardcoded Day default (совпадает с системным пресетом db.py:781-799)
# Используется как последний эшелон фолбэка если ни day_permissions, ни snapshot,
# ни системного пресета «Day default» в БД нет (например, БД пустая при первом запуске).
# ВАЖНО: admin-права (can_change_info, can_invite_users, can_pin_messages) всегда False.
_DAY_DEFAULT_HARDCODED = {
    "can_send_messages": True,        # текст
    "can_send_audios": True,          # музыка
    "can_send_photos": True,          # фото
    "can_send_videos": True,          # видео (НЕ video_notes!)
    "can_send_other_messages": True,  # стикеры, GIFs, dice
    "can_send_documents": False,
    "can_send_video_notes": False,
    "can_send_voice_notes": False,
    "can_send_polls": False,
    "can_add_web_page_previews": False,
    "can_change_info": False,         # admin
    "can_invite_users": False,        # admin
    "can_pin_messages": False,        # admin
}


# v4.7.20: кеш системного пресета «Day default».
# Раньше каждый вызов _resolve_day_perms делал SQL-запрос за пресетом — это
# лишняя нагрузка на БД при каждом выходе из ночного режима. Теперь пресет
# кешируется в module-level переменной при первом обращении.
# Если SU меняет/удаляет «Day default» — кеш инвалидируется через
# _invalidate_day_default_cache() (вызывается из web_app.py при редактировании
# системного пресета).
_DAY_DEFAULT_CACHE: dict | None = None
_DAY_DEFAULT_CACHE_LOADED: bool = False  # True если кеш уже пробовали грузить (даже если None)


def _invalidate_day_default_cache() -> None:
    """v4.7.20: инвалидирует кеш «Day default» пресета.

    Вызывается из web_app.py когда SU редактирует/удаляет системный пресет
    «Day default» (scope='day', is_system=True). Следующий вызов
    _resolve_day_perms перечитает пресет из БД.
    """
    global _DAY_DEFAULT_CACHE, _DAY_DEFAULT_CACHE_LOADED
    _DAY_DEFAULT_CACHE = None
    _DAY_DEFAULT_CACHE_LOADED = False
    logger.info("Day default cache invalidated")


async def _load_day_default_cached() -> dict | None:
    """v4.7.20: возвращает кеш системного «Day default» пресета.

    При первом вызове — загружает из БД и кеширует. При последующих —
    возвращает закешированное значение. Если пресета в БД нет — кеширует
    None (чтобы не делать SQL-запрос каждый раз).

    Возвращает dict {field: bool} или None если пресета нет.
    """
    global _DAY_DEFAULT_CACHE, _DAY_DEFAULT_CACHE_LOADED
    if _DAY_DEFAULT_CACHE_LOADED:
        return _DAY_DEFAULT_CACHE
    # Загружаем из БД
    try:
        async with async_session() as session:
            from db import PermissionPreset
            preset = (await session.execute(
                select(PermissionPreset).where(
                    PermissionPreset.name == "Day default",
                    PermissionPreset.scope == "day",
                )
            )).scalar_one_or_none()
            if preset and preset.permissions:
                data = json.loads(preset.permissions)
                _DAY_DEFAULT_CACHE = {
                    k: bool(data.get(k, False)) for k in _PERM_FIELDS
                }
            else:
                _DAY_DEFAULT_CACHE = None
    except Exception as e:
        logger.warning("Failed to load 'Day default' preset for cache: %s", e)
        _DAY_DEFAULT_CACHE = None
    _DAY_DEFAULT_CACHE_LOADED = True
    return _DAY_DEFAULT_CACHE


async def _resolve_day_perms(cs: ChatSettings) -> tuple[object, str]:
    """v4.7.12: возвращает дневные права чата (ChatPermissions) с приоритетом:

      1. cs.day_permissions — явно привязанный к чату day preset (JSON-копия)
      2. Системный пресет «Day default» (scope='day', is_system=True) из БД
         (v4.7.20: кешируется в _DAY_DEFAULT_CACHE)
      3. Hardcoded _DAY_DEFAULT_HARDCODED (на случай пустой БД)

    Returns: (ChatPermissions, source) где source = 'chat_preset' | 'system_default'
             | 'hardcoded' — для логирования.
    Никогда не возвращает all_true — admin-права всегда False.
    """
    from aiogram import types as _tg_types

    # 1. Явно привязанный day preset.
    if cs.day_permissions:
        try:
            data = json.loads(cs.day_permissions)
            perms = _tg_types.ChatPermissions(
                **{k: bool(data.get(k, False)) for k in _PERM_FIELDS}
            )
            return perms, "chat_preset"
        except (ValueError, TypeError):
            logger.warning(
                "Chat %s: day_permissions JSON corrupted (%.80r) — falling back",
                cs.chat_id, cs.day_permissions,
            )

    # 2. Системный пресет «Day default» (v4.7.20: из кеша).
    cached = await _load_day_default_cached()
    if cached is not None:
        perms = _tg_types.ChatPermissions(**{k: cached[k] for k in _PERM_FIELDS})
        return perms, "system_default"

    # 3. Hardcoded fallback.
    perms = _tg_types.ChatPermissions(
        **{k: _DAY_DEFAULT_HARDCODED[k] for k in _PERM_FIELDS}
    )
    return perms, "hardcoded"


def _night_window_active(cs: ChatSettings, now: datetime) -> bool:
    """v4.7.12: проверяет находится ли текущее время в окне night mode для чата.

    Обёртка над _night_mode_in_window с учётом per-chat tz + weekend schedule.
    Если night_mode_enabled=False — всегда False (не в окне).
    """
    if not cs.night_mode_enabled:
        return False
    return _night_mode_in_window(
        now=now,
        weekday_start=cs.night_mode_start or "23:00",
        weekday_end=cs.night_mode_end or "07:00",
        weekend_start=cs.night_mode_weekend_start,
        weekend_end=cs.night_mode_weekend_end,
        tz_name=cs.night_mode_tz or "Europe/Moscow",
    )


async def _restore_day_state(cs: ChatSettings) -> str:
    """v4.7.12: восстанавливает дневные права чата из day preset.

    Применяет к чату права, полученные через _resolve_day_perms(cs).
    Возвращает source ('chat_preset' | 'system_default' | 'hardcoded').

    v4.7.16: дополнительно восстанавливает slow_mode_delay:
      • Если cs.day_slow_mode_delay > 0 — применяем его (preset-driven,
        приоритет над snapshot'ом — как v4.7.12 для ChatPermissions).
      • Иначе если cs.night_mode_saved_slow_mode_delay не None —
        восстанавливаем snapshot.
      • Иначе — не трогаем slow_mode (backward compat).
    Это гарантирует, что при выходе из ночного режима slow_mode
    возвращается к дневному значению (если задан preset) или к
    сохранённому снимку (если preset не задан, но был snapshot).

    v4.8.0: права восстанавливаются через _resolve_restore_perms_async
    (chat_modes.py) — унифицированно с alarm и sanitary. Системный пресет
    «Day default» теперь имеет приоритет над snapshot'ом night_mode (что
    правильно — preset описывает то что ДОЛЖНО быть днём, snapshot — то
    что было до режима, что может отличаться при ручных правках).
    """
    # v4.8.0: унифицированная логика выбора прав для восстановления.
    restore_perms, source = await _resolve_restore_perms_async(
        session=None,  # _resolve_restore_perms_async создаст свою session
        cs=cs,
        saved_permissions_field=cs.night_mode_saved_permissions,
        saved_source_name="night snapshot",
    )
    # NOTE: _resolve_restore_perms_async создаёт свою session внутри,
    # т.к. ей нужен SQL-запрос к permission_presets. Это не оптимально
    # (лишний запрос), но безопасно. В будущем можно пропустить session
    # извне через параметр.

    # v4.8.0: применяем через унифицированную обёртку.
    ok = await _apply_chat_permissions(bot, cs.chat_id, restore_perms)
    if not ok:
        logger.error(
            "Day state: failed to apply permissions for chat %s — aborting",
            cs.chat_id,
        )
        return source  # возвращаем source для логов, даже если не применилось

    # v4.7.16: восстанавливаем slow_mode.
    day_slow = int(cs.day_slow_mode_delay or 0)
    saved_slow = cs.night_mode_saved_slow_mode_delay
    if day_slow > 0:
        target_slow = day_slow
        slow_source = "day_preset"
    elif saved_slow is not None:
        target_slow = int(saved_slow)
        slow_source = "snapshot"
    else:
        target_slow = None
        slow_source = "skip"
    if target_slow is not None:
        try:
            await bot(SetChatSlowModeDelay(
                chat_id=cs.chat_id, slow_mode_delay=target_slow,
            ))
            logger.info(
                "Day state: slow_mode restored to %ds for chat %s (source=%s)",
                target_slow, cs.chat_id, slow_source,
            )
        except TelegramAPIError as e:
            logger.warning(
                "Day state: set_chat_slow_mode_delay(%ds) failed for chat %s: %s",
                target_slow, cs.chat_id, e,
            )

    logger.info(
        "Day state restored for chat %s (source=%s, mode=%s)",
        cs.chat_id, source, _mode_priority(cs),
    )
    return source


async def _exit_night_mode(cs: ChatSettings, allow_auto_enter: bool = True) -> None:
    """v4.7.12: выходит из ночного режима с учётом автопереключения.

    Логика:
      1. Снимает night-флаги (night_mode_currently_active=False,
         snapshot=None). v4.7.16: night_mode_saved_slow_mode_delay
         НЕ очищаем здесь — он нужен _restore_day_state как fallback
         для восстановления slow_mode. Очистим после restore.
      2. Если allow_auto_enter и night_mode_enabled и сейчас в окне —
         сразу вызывает _enter_night_mode(cs) (свежий snapshot + night perms).
         Это автопереход: не ждём следующий tick.
      3. Иначе — восстанавливает дневные права через _restore_day_state(cs).
         _restore_day_state использует day_slow_mode_delay (если задан) или
         night_mode_saved_slow_mode_delay (snapshot) для восстановления
         slow_mode. После успешного restore — чистим snapshot slow_mode.

    Args:
      cs: ChatSettings чата (свежезагруженный из БД).
      allow_auto_enter: если True (по умолчанию) — может сразу войти обратно
        в night mode если включён и в окне. False — явно запрещает автопереход
        (используется при _exit_sanitary_day если night не нужен).

    v4.5.3: если night_mode_notify=True — отправляет уведомление о выходе
    (только если действительно вышли в day, а не перешли в night).
    """
    # Сначала снимаем night-флаги (независимо от того, перейдём ли в night снова).
    # v4.7.16: night_mode_saved_slow_mode_delay оставляем — он нужен
    # _restore_day_state как fallback. Очистим после restore.
    async with async_session() as session:
        db_cs = (await session.execute(
            select(ChatSettings).where(ChatSettings.chat_id == cs.chat_id)
        )).scalar_one_or_none()
        if db_cs:
            db_cs.night_mode_currently_active = False
            db_cs.night_mode_saved_permissions = None
            await session.commit()
            # Синхронизируем объект в памяти.
            cs.night_mode_currently_active = False
            cs.night_mode_saved_permissions = None

    # Автопереход: если night mode включён и сейчас в окне — входим обратно.
    if allow_auto_enter and cs.night_mode_enabled:
        now = datetime.now(timezone.utc)
        if _night_window_active(cs, now):
            logger.info(
                "Chat %s: auto-transition night→night (still in window) — re-entering",
                cs.chat_id,
            )
            await _enter_night_mode(cs)
            return

    # Иначе — восстанавливаем дневные права.
    try:
        source = await _restore_day_state(cs)
        logger.info(
            "Night mode OFF for chat %s (day restored, source=%s)",
            cs.chat_id, source,
        )

        # v4.7.16: чистим snapshot slow_mode ПОСЛЕ успешного restore.
        # Если restore упал выше (бросил исключение) — snapshot остаётся,
        # и следующий tick попытается снова. Это безопасно.
        async with async_session() as session:
            db_cs = (await session.execute(
                select(ChatSettings).where(ChatSettings.chat_id == cs.chat_id)
            )).scalar_one_or_none()
            if db_cs:
                db_cs.night_mode_saved_slow_mode_delay = None
                await session.commit()
            cs.night_mode_saved_slow_mode_delay = None

        # v4.5.3: уведомление о выходе.
        # v4.7.18: уведомление идёт в репорт-чат, а НЕ в общий чат
        # (см. комментарий в _enter_night_mode — та же логика).
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
                async with async_session() as notify_session:
                    report_chat_id = await _get_report_chat_id(
                        notify_session, cs.chat_id,
                    )
                if report_chat_id is None:
                    logger.warning(
                        "Night mode exit notify for chat %s: no report chat "
                        "configured — skipping (set report_chat_id or "
                        "is_report_chat=True to receive night mode notifications)",
                        cs.chat_id,
                    )
                else:
                    await bot.send_message(chat_id=report_chat_id, text=text)
                    logger.info(
                        "Night mode exit notify sent to report chat %s "
                        "(source chat %s)",
                        report_chat_id, cs.chat_id,
                    )
            except TelegramAPIError as e:
                # v4.7.19: ловим базовый класс (см. комментарий в _enter_night_mode).
                logger.warning(
                    "Night mode exit notify failed: source_chat=%s, "
                    "report_chat=%s, error=%s",
                    cs.chat_id, report_chat_id, e,
                )
    except TelegramAPIError as e:
        # v4.7.19: та же логика — ловим базовый класс.
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
    2. v4.7.30: если alarm сейчас активен — сначала деактивируем его (Баг #3
       аудита v4.7.30). Без этого sanitary snapshot сохранит alarm-состояние
       (text-only) как "оригинальные дневные права", и при выходе из sanitary
       day чат восстановит text-only вместо настоящих day-прав. Кроме того,
       alarm-поля в БД останутся set, что приведёт к непредсказуемому
       поведению при последующем !alarm off.
    3. Делаем snapshot текущих ChatPermissions.
    4. Ставим все права в False (полный lockdown) — ИЛИ используем granular
       sanitary_days_permissions если задан (v4.6.0).
    5. Сохраняем snapshot и флаг.
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

        # 2. v4.7.30: если alarm активен — деактивируем перед snapshot'ом.
        # Аналогично логике в _night_mode_tick перед входом в night mode.
        # Восстанавливает права из alarm-snapshot (это настоящие day-права,
        # которые были ДО alarm). После этого cs.alarm_* поля очищены,
        # права чата восстановлены — sanitary snapshot будет корректным.
        if cs.alarm_currently_active:
            logger.info(
                "Sanitary day: auto-deactivating alarm before entering lockdown "
                "for chat %s (alarm_started_by=%s)",
                cs.chat_id, cs.alarm_started_by,
            )
            started_by_sanitary = None
            try:
                async with async_session() as alarm_session:
                    alarm_cs = (await alarm_session.execute(
                        select(ChatSettings).where(
                            ChatSettings.chat_id == cs.chat_id
                        )
                    )).scalar_one_or_none()
                    if alarm_cs and alarm_cs.alarm_currently_active:
                        started_by_sanitary = alarm_cs.alarm_started_by
                        await _deactivate_alarm(
                            alarm_session, alarm_cs, bot,
                            cs.chat_id, reason="sanitary_day_enter",
                        )
                        cs.alarm_currently_active = False
                        cs.alarm_saved_permissions = None
                        cs.alarm_saved_slow_mode_delay = None
                        cs.alarm_active_until = None
            except Exception as e:
                logger.warning(
                    "Sanitary day: could not deactivate alarm first for chat %s: %s "
                    "(will proceed anyway — snapshot may capture alarm state)",
                    cs.chat_id, e,
                )
            # v4.8.0: отправляем событие в modchat.
            if started_by_sanitary is not None:
                try:
                    from modchat import _send_alarm_event_to_modchat
                    await _send_alarm_event_to_modchat(
                        bot=bot, chat_id=cs.chat_id, event_type="off_by_mode",
                        mod_id=started_by_sanitary,
                        reason="вход в sanitary day",
                    )
                except Exception as modchat_e:
                    logger.debug(
                        "Modchat alarm off-by-sanitary event failed for chat %s: %s",
                        cs.chat_id, modchat_e,
                    )

        # 2. Snapshot текущих прав.
        # v4.8.0: унифицированный snapshot через _snapshot_chat_permissions.
        # day_permissions передаётся для приоритета preset'а над текущими правами.
        try:
            snapshot_json, _ = await _snapshot_chat_permissions(
                bot=bot, chat_id=cs.chat_id, day_permissions=cs.day_permissions,
            )
        except TelegramAPIError as e:
            logger.error(
                "Sanitary day: snapshot failed for chat %s: %s — aborting",
                cs.chat_id, e,
            )
            return

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
        # v4.8.0: унифицированная обёртка (use_independent_chat_permissions=True
        # внутри). Для lockdown это особенно важно: мы хотим полный мьют.
        ok = await _apply_chat_permissions(bot, cs.chat_id, lockdown_perms)
        if not ok:
            logger.error(
                "Sanitary day: failed to apply lockdown for chat %s — aborting",
                cs.chat_id,
            )
            return

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
            "Sanitary day ON for chat %s (lockdown applied, snapshot saved, mode=%s)",
            cs.chat_id, _mode_priority(cs),
        )
    except TelegramAPIError as e:
        # v4.7.19: базовый класс — покрывает NotFound, Forbidden и т.п.
        logger.error("Sanitary day enter failed for chat %s: %s", cs.chat_id, e)


async def _exit_sanitary_day(cs: ChatSettings) -> None:
    """v4.7.12: выходит из sanitary day с учётом автопереключения в night mode.

    Логика:
      1. Снимает sanitary-флаги (currently_active=False, snapshot=None).
      2. Проставляет last_sanitary_month=current month (для suppress warnings).
      3. Чистит ключ текущего месяца из sanitary_days JSON.
      4. Если night_mode_enabled и сейчас в окне — сразу _enter_night_mode(cs).
         Это автопереход: не ждём следующий night tick.
      5. Иначе — _restore_day_state(cs) (day preset чата или системный Day default).

    ВАЖНО: admin-права (can_change_info, can_invite_users, can_pin_messages)
    никогда не выдаются вслепую. Если day preset не задан и системного «Day
    default» нет — используется hardcoded-фолбэк с admin-правами OFF.
    """
    # v4.6.0: проставляем last_sanitary_month и чистим старые месяцы.
    from datetime import datetime as _dt
    from datetime import timezone as _tz
    tz_name = cs.night_mode_tz or "Europe/Moscow"
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_name)
    except (ValueError, KeyError, ImportError):
        tz = _tz.utc
    current_month_str = _dt.now(tz).strftime("%Y-%m")

    # 1. Снимаем sanitary-флаги + monthly cleanup.
    async with async_session() as session:
        db_cs = (await session.execute(
            select(ChatSettings).where(ChatSettings.chat_id == cs.chat_id)
        )).scalar_one_or_none()
        if db_cs:
            db_cs.sanitary_days_currently_active = False
            db_cs.sanitary_days_saved_permissions = None
            db_cs.last_sanitary_month = current_month_str
            if db_cs.sanitary_days:
                try:
                    sd_data = json.loads(db_cs.sanitary_days)
                    if isinstance(sd_data, dict):
                        sd_data.pop(current_month_str, None)
                        db_cs.sanitary_days = json.dumps(sd_data) if sd_data else None
                except (ValueError, TypeError):
                    pass
            await session.commit()
            # Синхронизируем объект в памяти.
            cs.sanitary_days_currently_active = False
            cs.sanitary_days_saved_permissions = None

    # 2. Автопереход в night mode если включён и в окне.
    if cs.night_mode_enabled:
        now = datetime.now(timezone.utc)
        if _night_window_active(cs, now):
            logger.info(
                "Chat %s: auto-transition sanitary→night (in night window) — entering night",
                cs.chat_id,
            )
            try:
                await _enter_night_mode(cs)
            except TelegramAPIError as e:
                # v4.7.19: базовый класс — покрывает NotFound, Forbidden и т.п.
                logger.error(
                    "Sanitary→night transition failed for chat %s: %s — falling back to day",
                    cs.chat_id, e,
                )
                try:
                    await _restore_day_state(cs)
                except TelegramAPIError as e2:
                    logger.error(
                        "Day restore fallback failed for chat %s: %s",
                        cs.chat_id, e2,
                    )
            return

    # 3. Иначе — восстанавливаем дневные права из preset.
    try:
        source = await _restore_day_state(cs)
        logger.info(
            "Sanitary day OFF for chat %s (day restored, source=%s)",
            cs.chat_id, source,
        )
    except TelegramAPIError as e:
        # v4.7.19: базовый класс — покрывает NotFound, Forbidden и т.п.
        logger.error("Sanitary day exit failed for chat %s: %s", cs.chat_id, e)


# ── v4.7.3: константа hard shutdown timeout ────────────────────────────────
# На SIGTERM даём фоновым задачам максимум 5 секунд на graceful cancel.
# Если за это время они не завершились — логируем и выходим принудительно
# (event loop всё равно закроется, задачи умрут с PendingCancellation).
_SHUTDOWN_TIMEOUT_SECONDS: float = 5.0


# ── v4.8.9: регистрация функций в app_state ────────────────────────────────
# После определения _exit_night_mode / _enter_sanitary_day / _exit_sanitary_day
# регистрируем их в app_state.py. Это заменяет хак sys.modules.setdefault("bot",
# _self_module) — теперь bot_handlers.py и web_app.py достают эти функции через
# `from app_state import get_exit_night_mode` (см. app_state.py).
from app_state import register as _app_state_register

_app_state_register(
    exit_night_mode=_exit_night_mode,
    enter_sanitary_day=_enter_sanitary_day,
    exit_sanitary_day=_exit_sanitary_day,
)


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

    v4.7.30: добавлен alarm_currently_active в проверку — Баг #2 аудита
    v4.7.30. Если бот крашнулся в момент когда alarm был активен — при
    рестарте _alarm_auto_off_tick() снимет его если alarm_active_until
    истёк, или оставит активным если ещё не вышло время (или alarm без
    длительности). Если alarm завис без alarm_active_until (manual off) —
    он останется активным (это правильно, модератор должен снять вручную),
    но в логе startup recovery будет видно что он есть.
    """
    try:
        async with async_session() as session:
            from sqlalchemy import or_
            stmt = select(ChatSettings).where(
                or_(
                    ChatSettings.night_mode_currently_active.is_(True),
                    ChatSettings.sanitary_days_currently_active.is_(True),
                    ChatSettings.alarm_currently_active.is_(True),
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
                "  chat %s: night_active=%s sanitary_active=%s alarm_active=%s "
                "(alarm_until=%s, alarm_started_by=%s)",
                cs.chat_id,
                bool(cs.night_mode_currently_active),
                bool(cs.sanitary_days_currently_active),
                bool(cs.alarm_currently_active),
                cs.alarm_active_until.isoformat() if cs.alarm_active_until else "N/A",
                cs.alarm_started_by,
            )
        # Прогоняем tick'и в правильном порядке:
        # 1. alarm auto-off ПЕРВЫМ — снимет alarm'ы с истёкшим alarm_active_until
        # 2. sanitary day tick — снимет sanitary если окно вышло
        # 3. night mode tick — снимет night если окно вышло
        try:
            await _alarm_auto_off_tick(bot)
            await _sanitary_day_tick()
            await _night_mode_tick()
            logger.info("Startup recovery: tick completed, state reconciled")
        except Exception as e:
            logger.error("Startup recovery: tick failed: %s", e)
    except Exception as e:
        logger.error("Startup recovery: DB error: %s", e)


async def _verify_env_chats():
    """v4.7.20: проверка чатов из CHAT_HASHTAGS env при старте.

    Проблема: если env содержит чаты, в которых бот больше не состоит
    (тестовые чаты, удалённые группы, чаты откуда бот кикнут) — бот при
    каждом апдейте из такого чата будет пересоздавать chat_settings строку.
    Это приводило к "воскресанию" удалённых чатов в веб-панели.

    Решение: при старте пробуем bot.get_chat() для каждого chat_id из
    _CHAT_HASHTAGS. Если ошибка (чат удалён / бот кикнут / неверный ID) —
    помечаем is_enabled=False чтобы веб-панель показывала чат как disabled,
    а обработчики игнорировали апдейты из него.

    НЕ удаляем строку полностью — сохраняем историю (варны, баны и т.д.).
    SU может включить чат обратно через веб-панель если это была ошибка.
    """
    if not _CHAT_HASHTAGS:
        return  # env пустой — ничего проверять

    logger.info(
        "Verifying %d env-chat(s) from CHAT_HASHTAGS: %s",
        len(_CHAT_HASHTAGS), list(_CHAT_HASHTAGS.keys()),
    )

    disabled_count = 0
    for chat_id, hashtag in _CHAT_HASHTAGS.items():
        try:
            chat_info = await bot.get_chat(chat_id=chat_id)
            logger.info(
                "Env chat %s (hashtag=%s) OK: title='%s' type='%s'",
                chat_id, hashtag, chat_info.title or "", chat_info.type,
            )
        except Exception as e:
            # Чат недоступен — помечаем is_enabled=False
            logger.warning(
                "Env chat %s (hashtag=%s) NOT accessible: %s — marking as disabled",
                chat_id, hashtag, e,
            )
            try:
                async with async_session() as session:
                    cs = (await session.execute(
                        select(ChatSettings).where(
                            ChatSettings.chat_id == chat_id
                        )
                    )).scalar_one_or_none()
                    if cs is None:
                        # Создаём строку сразу disabled — чтобы веб-панель
                        # показывала её с правильным статусом.
                        cs = ChatSettings(
                            chat_id=chat_id,
                            hashtag=hashtag,
                            is_enabled=False,
                        )
                        session.add(cs)
                        logger.info(
                            "Env chat %s: created chat_settings with is_enabled=False",
                            chat_id,
                        )
                    elif cs.is_enabled:
                        cs.is_enabled = False
                        logger.info(
                            "Env chat %s: set is_enabled=False (was True)",
                            chat_id,
                        )
                    else:
                        logger.info(
                            "Env chat %s: already disabled, no change",
                            chat_id,
                        )
                    await session.commit()
                disabled_count += 1
            except Exception as inner_e:
                logger.error(
                    "Env chat %s: failed to mark disabled in DB: %s",
                    chat_id, inner_e,
                )

    if disabled_count > 0:
        logger.warning(
            "Env-chats verification: %d/%d disabled (not accessible by bot)",
            disabled_count, len(_CHAT_HASHTAGS),
        )

async def _publish_bot_commands(bot) -> None:
    """v5.1.0: публикует меню команд по скоупам.

    До v5.1.0 здесь стоял безусловный delete_my_commands() — бот прятал
    меню целиком (стелс). Теперь наружу выходят ровно две публичные
    команды, /mywarns и /rules.

    Мод-команды не публикуются нигде. Скоуп AllChatAdministrators у
    Telegram означает настоящих админов чата, а _is_admin
    (bot_handlers.py) Telegram не спрашивает вовсе — он смотрит ADMIN_IDS,
    WebUser и chat_admins. Админов чата в этой инсталляции заметно больше,
    чем модераторов в БД, поэтому такой скоуп рекламировал бы /ban строго
    более широкому кругу, чем тот, кому команда разрешена.

    AllChatAdministrators при этом не задаётся вовсе, а не задаётся
    пустым: скоупы Telegram не складываются, более узкий замещает более
    широкий целиком, и пустой админский скоуп отобрал бы у админов
    /mywarns и /rules. Не задавая его, мы позволяем им унаследовать
    AllGroupChats.

    Любая ошибка Telegram гасится: меню — не повод не стартовать.
    """
    try:
        group_cmds = [
            BotCommand(command=spec.name, description=spec.description)
            for spec in commands_registry.GROUP_COMMANDS
            if spec.in_menu
        ]
        dm_cmds = [
            BotCommand(command=name, description=description)
            for name, description in commands_registry.DM_MENU_COMMANDS
        ]
        await bot.set_my_commands(group_cmds, scope=BotCommandScopeAllGroupChats())
        await bot.set_my_commands(dm_cmds, scope=BotCommandScopeAllPrivateChats())
        # Default чистим: он служит фолбэком для скоупов, которые мы не задаём.
        await bot.delete_my_commands()
        logger.info(
            "Bot commands published: %d in groups, %d in DM",
            len(group_cmds), len(dm_cmds),
        )
    except Exception as e:
        logger.warning("_publish_bot_commands failed: %s", e)


# ── Lifespan ────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app):
    global _webhook_set

    # ── Startup ─────────────────────────────────────────────────
    # v4.8.9: миграции через Alembic (init_db_with_fallback).
    # По умолчанию — alembic upgrade head. Если env DB_USE_LEGACY_MIGRATIONS=1
    # — fallback на старый init_db() (664 строки идемпотентных ALTER'ов).
    # См. db.run_migrations_async() и 03_TASK_v4.8.9.md §4.
    await init_db_with_fallback()
    logger.info("DB initialized (Alembic migrations applied)")

    # v4.7.3: recovery для чатов с зависшими active-флагами после жёсткого
    # SIGTERM в предыдущем запуске. Должен идти ДО запуска background loop,
    # чтобы loop не подхватил полузавершённое состояние.
    await _startup_recovery()

    # v4.7.20: проверка чатов из CHAT_HASHTAGS env — помечаем is_enabled=False
    # для чатов, в которых бот больше не состоит (тестовые чаты, удалённые
    # группы). Без этого удалённые чаты "воскресали" в веб-панели при каждом
    # апдейте из них (env принудительно создавал chat_settings).
    await _verify_env_chats()

    # v5.1.0: меню команд вместо безусловной очистки (см. _publish_bot_commands).
    # Username нужен фильтрам, чтобы отличать /ban@degradach_bot от
    # /ban@other_bot — ставим до публикации меню.
    try:
        me = await bot.me()
        commands_registry.set_bot_username(me.username)
        logger.info("Bot username: @%s", me.username)
    except Exception as e:
        logger.warning("cannot resolve bot username: %s", e)
    await _publish_bot_commands(bot)

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
            # v4.7.27: добавлен "chat_member" — чтобы получать обновления
            # статусов других участников чата (не только себя). Без этого бот
            # не видит ручные баны, выдаваемые админами через Telegram-клиент
            # (правой кнопкой → "Заблокировать"). Это позволяет отправлять
            # компактный отчёт о ручном бане в reporting chat.
            await bot.set_webhook(
                url=WEBHOOK_URL,
                allowed_updates=["message", "my_chat_member", "chat_member"],
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
            # v4.10.2: пробник Telegram для /healthz — отдельной таской,
            # чтобы его задержки не влияли на тики режимов чата.
            health_task = tg.create_task(
                _health_probe_loop(), name="health_probe_loop",
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
            # v4.10.2: health_task обязан быть в списке. Забыть его —
            # значит подвесить shutdown: TaskGroup ждёт завершения всех
            # задач, а бесконечный while True сам не выйдет.
            bg_tasks = [night_task, health_task]
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
