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
    # v4.7.18: night mode / day mode notifications go to report chat, not
    # to the public chat. Reuse the existing resolver — it honours per-chat
    # override (ChatSettings.report_chat_id), is_report_chat flag, and the
    # global default (chat_id=0).
    _get_report_chat_id,
    # v4.7.20: !alarm integration — _deactivate_alarm используется в
    # _night_mode_tick для (1) auto-off при истечении alarm_active_until,
    # (2) auto-deactivate при входе в night mode (alarm избыточен когда
    # night mode уже ограничивает права).
    _deactivate_alarm,
    # v4.7.20: env-chats cleanup при старте — если чат из CHAT_HASHTAGS
    # не отвечает (бот кикнут / чат удалён), помечаем is_enabled=False.
    # Раньше бот при каждом апдейте пересоздавал chat_settings для мёртвых
    # чатов из env (CHAT_HASHTAGS=-1003972381175:Test), и SU не мог их
    # удалить из веб-панели — они "воскресали" при следующем сообщении.
    _CHAT_HASHTAGS,
)
from datetime import datetime, timezone, timedelta, date
import json
from aiogram.exceptions import TelegramBadRequest, TelegramAPIError
# v4.7.19: TelegramAPIError — базовый класс для ВСЕХ Telegram-ошибок
# (TelegramNotFound, TelegramForbiddenError, TelegramConflictError, ...).
# Раньше ловили только TelegramBadRequest, но TelegramNotFound ("Not Found"
# при отправке в удалённый/несуществующий чат) и TelegramForbiddenError
# ("bot was kicked from chat") — это ОТДЕЛЬНЫЕ классы, не наследники
# TelegramBadRequest. Из-за этого исключение пробивалось наверх в
# _night_mode_tick и засоряло лог ERROR'ами каждую минуту. Теперь ловим
# базовый класс — любая ошибка Telegram логируется как warning и не валит tick.
from aiogram.methods.base import TelegramMethod


# v4.7.16: aiogram 3.30 не имеет обёртки для setChatSlowModeDelay (появилась
# в более поздних версиях). Создаём минимальный TelegramMethod-класс — он
# проходит через стандартный pipeline aiogram (session, retry, error handling).
# Возвращает True (как и все set_chat_* методы Telegram).
# После апгрейда aiogram — заменить на bot.set_chat_slow_mode_delay(...).
class SetChatSlowModeDelay(TelegramMethod[bool]):
    """Обёртка над Telegram Bot API method setChatSlowModeDelay.

    Use this method to change the slow mode delay in a chat. The bot must
    be an administrator in the chat for this to work and must have the
    can_restrict_members administrator right. Returns True on success.

    Source: https://core.telegram.org/bots/api#setchatslowmodedelay
    """
    __returning__ = bool
    __api_method__ = "setChatSlowModeDelay"

    chat_id: int | str
    slow_mode_delay: int

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

            # v4.7.20: auto-off alarm если alarm_active_until истёк.
            # Проверяем ПЕРЕД night mode логикой — чтобы alarm не конфликтовал
            # с night. Если alarm_active_until is None — alarm до ручного off.
            if (cs.alarm_currently_active
                    and cs.alarm_active_until is not None
                    and now >= cs.alarm_active_until):
                logger.info(
                    "Alarm auto-off: chat %s alarm_active_until=%s (now=%s)",
                    cs.chat_id, cs.alarm_active_until.isoformat(), now.isoformat(),
                )
                try:
                    async with async_session() as alarm_session:
                        alarm_cs = (await alarm_session.execute(
                            select(ChatSettings).where(
                                ChatSettings.chat_id == cs.chat_id
                            )
                        )).scalar_one_or_none()
                        if alarm_cs and alarm_cs.alarm_currently_active:
                            # Используем _deactivate_alarm из bot_handlers.
                            # Восстанавливает права из snapshot/preset.
                            await _deactivate_alarm(
                                alarm_session, alarm_cs, bot,
                                cs.chat_id, reason="auto_off_timeout",
                            )
                            # Синхронизируем cs в памяти — чтобы后续 night mode
                            # логика видела, что alarm снят.
                            cs.alarm_currently_active = False
                            cs.alarm_saved_permissions = None
                            cs.alarm_saved_slow_mode_delay = None
                            cs.alarm_active_until = None
                except Exception as e:
                    logger.error(
                        "Alarm auto-off failed for chat %s: %s",
                        cs.chat_id, e,
                    )

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
                    try:
                        async with async_session() as alarm_session:
                            alarm_cs = (await alarm_session.execute(
                                select(ChatSettings).where(
                                    ChatSettings.chat_id == cs.chat_id
                                )
                            )).scalar_one_or_none()
                            if alarm_cs and alarm_cs.alarm_currently_active:
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
    """
    try:
        chat_info = await bot.get_chat(chat_id=cs.chat_id)
        current_perms = chat_info.permissions
        # v4.7.16: snapshot текущего slow_mode_delay (chat-level свойство,
        # не входит в ChatPermissions). Берём ПЕРЕД любыми изменениями.
        current_slow_mode = getattr(chat_info, "slow_mode_delay", 0) or 0
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
            "saved_slow_mode=%ds)",
            cs.chat_id, current_slow_mode,
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
    """
    restore_perms, source = await _resolve_day_perms(cs)
    await bot.set_chat_permissions(chat_id=cs.chat_id, permissions=restore_perms)

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
        "Day state restored for chat %s (source=%s)",
        cs.chat_id, source,
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
    from datetime import datetime as _dt, timezone as _tz
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

    # v4.7.20: проверка чатов из CHAT_HASHTAGS env — помечаем is_enabled=False
    # для чатов, в которых бот больше не состоит (тестовые чаты, удалённые
    # группы). Без этого удалённые чаты "воскресали" в веб-панели при каждом
    # апдейте из них (env принудительно создавал chat_settings).
    await _verify_env_chats()

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
