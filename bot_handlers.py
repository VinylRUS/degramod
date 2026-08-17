"""
bot_handlers.py — Дедушка Вобжак: скрытый модераторский бот + отчёты в чат + настройки.

★★★ СТЕЛС-РЕЖИМ: бот НЕ реагирует ни на какие команды от обычных юзеров.
Ни /start, ни /help, ни любые другие — молча игнорируются.
Только ADMIN_IDS и ChatAdmin могут использовать команды.
Нарушитель НЕ получает публичных уведомлений от бота — он не должен
догадываться о его существовании. Ephemeral-подтверждения получают
только модераторы (видны только им в группе, через receiver_user_id).

v4.4.9 ИСКЛЮЧЕНИЕ: при !warn нарушитель получает ephemeral-сообщение
(видно только ему через receiver_user_id=target.id) с причиной варна и
текущим счётчиком. Без этого варн был бесполезной санкцией — юзер даже
не знал, что его предупредили. Остальные участники чата этого сообщения
не видят, стелс для всех кроме наказанного сохраняется. ★★★

v4.4.10 РЕДИЗАЙН ОТЧЁТА В РЕПОРТ-ЧАТЕ:
• Структура: SectionHeading → Divider → List (нарушитель/причина/веб-профиль)
  → Divider → Details «📎 Сообщение юзера» (текст+медиа под спойлером, по
  умолчанию свёрнуто) → Divider → Details «Доп. инфо» → Divider → Footer.
• Модератор перенесён в Footer (кликабельное имя, без приписки «Модератор:»)
  — раньше он был отдельным параграфом с эмодзи 👮, теперь компактнее.
• Длинный URL веб-профиля спрятан под коротким текстом «Открыть профиль →»
  через RichTextUrl — больше URL не ломается посередине на мобиле.
• ID нарушителя оформлен как inline-код (моноширинный) — выделяется визуально,
  легко копируется долгим тапом на мобильном.
• Текст+медиа нарушителя обёрнуты в Details (сворачиваемый блок «📎 Сообщение
  юзера») — не торчит открыто, чтобы модератор случайно не увидел шок-контент.
  По тапу на «📎 Сообщение юзера» разворачивается.
• Divider'ы (горизонтальные линии) визуально разделяют секции — на мобиле
  больше не «стена текста», а чёткие блоки. ★★★

Команды в группах (reply на сообщение нарушителя):
  Громкие (публичное сообщение в чат, причина обязательна):
    !mute <1d/2h/30m> <причина>  — замьютить (полный мьют — все виды отправки)
    !warn <причина>               — выдать варн (1 поинт) + удалить сообщение нарушителя
    !ban <причина>                — забанить (v4.5.2: если reply на стикер — пак автодобавляется в BannedStickerPack)
  Тихие (стелс, ephemeral только модератору, причина необязательна) — v4.8.1:
    !smute <1d/2h/30m> [причина]  — замьютить без публичного сообщения
    !swarn [причина]              — выдать варн без паблисити (нарушитель получает ephemeral)
    !sban [причина]               — забанить без публичного сообщения
  Прочее в группах:
    !unmute                       — размьютить (выдаёт текущие права чата)
    !unban                        — разбанить (only_if_banned — безопасный)
    !unwarn [N]                   — снять N последних варнов (по умолчанию 1; cap = текущее кол-во)
    !warns                        — показать текущее кол-во варнов юзера (в личку админу)
    !resetwarns                   — обнулить варны юзера
    !resetmc [@user|tgid]         — обнулить счётчик автомьютов (v4.8.4: прогрессивные муты)
    !alarm on [1ч/1h/30м/30m/2д/2d] / !alarm off  — тревога (v4.7.20b): режим усиленных ограничений

Команды в личке (только для ADMIN_IDS):
  /addadmin chat_id user_id      — добавить админа в чат
  /deladmin chat_id user_id      — убрать админа
  /sethashtag chat_id #хэштег   — установить хэштег чата
  /setreport chat_id report_chat_id — задать чат для отчётов (0 = сбросить, использовать default)
  /warns_mute chat_id число      — варнов до авто-мьюта (0 = выкл)
  /warns_ban chat_id число       — варнов до авто-бана (0 = выкл)
  /mute_duration chat_id 1d/2h/30m — длительность мьюта
  /settings chat_id              — показать текущие настройки

v4.5.2 — новые команды в личке (только для ADMIN_IDS).
v4.8.1: word_filter команды /addword//delword//listwords удалены (используйте
       KeywordWatch через веб-панель /admin/keywords или групповые
       !addkeyword/!delkeyword/!listkeywords):
  /bansticker <pack_name_or_link> [punishment] [duration]
      — добавить стикерпак в бан-лист. punishment: delete|warn|mute|ban (default: delete).
        Для mute — длительность в формате 1d/2h/30m.
        pack_name_or_link может быть как именем пака, так и ссылкой https://t.me/addstickers/<name>.
        Если punishment не указан — берётся из глобального default (delete).
  /liststickers [chat_id]        — показать забаненные стикерпаки (все или для чата)
  /delsticker <pack_name> [chat_id] — убрать стикерпак из бан-листа (без chat_id — из global)
  /linkfilter <chat_id> on|off   — включить/выключить link filter для чата
  /linkallow <chat_id|global> <domain> — добавить домен в allowlist
  /linkallowlist [chat_id]       — показать allowlist (глобальный или для чата)
  /cas <chat_id> on|off          — включить/выключить CAS-проверку для чата
  /nightmode <chat_id> <start> <end> [permissions]
      — настроить ночной режим (start/end в формате HH:ММ, permissions: strict|text_only|none)
  /nightmode <chat_id> off       — выключить ночной режим
  /warndecay <chat_id> <days>    — установить срок действия варна (0 = отключено)
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import secrets
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse

import aiohttp
from aiogram import BaseMiddleware, F, Router, types
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.filters import BaseFilter, Command

# v4.7.22: aiogram 3.30 не имеет обёртки для setChatSlowModeDelay (появилась
# в более поздних версиях). Создаём минимальный TelegramMethod-класс — он
# проходит через стандартный pipeline aiogram (session, retry, error handling).
# Возвращает True (как и все set_chat_* методы Telegram).
# После апгрейда aiogram — заменить на bot.set_chat_slow_mode_delay(...).
#
# ВАЖНО: класс живёт в bot_handlers.py (НЕ в bot.py), потому что:
# 1. bot.py запускается как __main__ — late import `from bot import X` из
#    bot_handlers.py вызывал бы повторный import bot.py как отдельный модуль
#    со всеми side-effectами (dp.include_router, startup tasks, etc.) →
#    RuntimeError: Router is already attached.
# 2. bot_handlers.py импортируется bot.py ОДИН раз при старте — class definition
#    выполняется один раз, и bot.py может импортировать класс через existing
#    `from bot_handlers import ...` line.
# 3. Все потребители (handle_alarm_command, _deactivate_alarm в bot_handlers.py;
#    _enter_night_mode, _restore_day_state в bot.py) получают класс без late import.
from aiogram.methods.base import TelegramMethod
from aiogram.types import (
    BufferedInputFile,
    InputMediaAnimation,
    InputMediaAudio,
    InputMediaPhoto,
    InputMediaVideo,
    InputMediaVoiceNote,
    InputRichBlockAnimation,
    InputRichBlockAudio,
    InputRichBlockBlockQuotation,
    InputRichBlockDetails,
    InputRichBlockDivider,
    InputRichBlockFooter,
    InputRichBlockList,
    InputRichBlockListItem,
    InputRichBlockParagraph,
    InputRichBlockPhoto,
    InputRichBlockSectionHeading,
    InputRichBlockVideo,
    InputRichBlockVoiceNote,
    InputRichMessage,
    RichTextBold,
    RichTextCode,
    RichTextSpoiler,
    RichTextUrl,
)
from sqlalchemy import desc, func, select

from db import (
    AutomuteCounter,
    BannedStickerPack,
    ChatAdmin,
    ChatSettings,
    GithubSettings,
    IdeaLog,
    LinkAllowlist,
    Moderator,
    Punishment,
    User,
    WebUser,
    _decrypt_pat,
    _hash_password,
    async_session,
)

# v4.8.3: модуль для скачивания стикеров/фото в BytesIO и конвертации
# WebP/TGS → PNG, WebM — как есть. Используется в _build_media_block и
# _send_report для inline-блоков в Rich Messages.
from sticker_cache import (
    download_photo_bytes,
    download_sticker_for_rich_message,
)


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


logger = logging.getLogger("shadow_logger.bot_handlers")

router = Router()

# ── v4.7.3: Semaphore для ephemeral auto-delete ──────────────────────────
# Каждое ephemeral-сообщение (varn-уведомление нарушителю, подтверждение
# модератору) порождает fire-and-forget корутину `sleep(N) → delete_message`.
# Раньше таких корутин создавалось без ограничений — поток из 1000 варнов
# за минуту породил бы 1000 спящих задач. Semaphore(100) ограничивает
# количество ОДНОВРЕМЕННО ожидающих auto-delete-задач: 101-я ждёт освобождения
# слота. В Python 3.10+ Semaphore лениво привязывается к event loop при
# первом acquire(), так что module-level объявление безопасно.
_EPHEMERAL_DELETE_SEM = asyncio.Semaphore(100)

# ── v4.8.7: Strong refs для fire-and-forget задач ────────────────────────
# asyncio.create_task() без сохранения ссылки может быть убит GC на середине
# выполнения (Python docs: «Important: Save a reference to the result of this
# method, to avoid a task disappearing mid-execution»). Раньше все 5 вызовов
# ниже были fire-and-forget — эфемерные сообщения «иногда» не удалялись.
#
# Решение: _background_tasks хранит strong refs до завершения задачи.
# task.add_done_callback(_background_tasks.discard) удаляет ссылку по
# завершении (включая exception — чтобы не копить протухшие задачи).
# Источник: https://docs.python.org/3/library/asyncio-task.html#asyncio.create_task
_background_tasks: set[asyncio.Task] = set()


def _spawn_background_task(coro, *, label: str = "bg") -> asyncio.Task:
    """Создаёт asyncio.Task с сильной ссылкой в _background_tasks.

    Аналог asyncio.create_task, но GC-безопасный. После завершения задачи
    (нормально или с исключением) ссылка автоматически удаляется из set.

    Args:
        coro: корутина для запуска.
        label: метка для логирования (для отладки «какая задача зависла»).

    Returns:
        asyncio.Task — можно await'ить или cancel().
    """
    task = asyncio.create_task(coro)
    _background_tasks.add(task)

    def _on_done(t: asyncio.Task) -> None:
        _background_tasks.discard(t)
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            logger.warning(
                "background task '%s' failed: %s: %s",
                label, type(exc).__name__, exc,
            )

    task.add_done_callback(_on_done)
    return task


# ── v4.8.7: TG flood control (429 / RetryAfter) ───────────────────────────
# Telegram API кидает 429 с retry_after когда бот слишком часто шлёт запросы.
# aiogram 3.30 поднимает TelegramRetryAfter (подкласс TelegramAPIError) с
# атрибутом retry_after: int. Раньше во всём проекте этот эксепшн ловился
# общим `except TelegramAPIError` и тихо логировался — бот просто переставал
# реагировать на команду (например, !ban срабатывал в БД, но public-сообщение
# в чат не шло).
#
# Решение: обёртка tg_safe_call для КРИТИЧНЫХ Telegram-вызовов (где потеря
# вызова = нарушенная бизнес-логика). Для best-effort вызовов (ephemeral,
# audit-в-DM, удаление команды модератора) — НЕ оборачиваем: там 429 не
# критичен, retry_after может быть 30+ сек и лучше показать ошибку сразу.
#
# Важно: передаём CALLABLE, возвращающий корутину (а не саму корутину) —
# после исключения корутину нельзя «перезапустить», нужно создавать новую.
# Синтаксис: await tg_safe_call(lambda: bot.send_message(...), label=...)
#
# Параметры (env):
#   TG_FLOOD_MAX_RETRIES — сколько ретраев после 429 (default 3).
#     После исчерпания — exception пробрасывается наверх (как раньше).
#   TG_FLOOD_RETRY_CAP — cap на sleep (default 30 сек). Если retry_after=120,
#     sleep 30 (не блокируем хендлер на 2 минуты), exception пробрасывается.
_MAX_RETRIES = max(0, int(os.getenv("TG_FLOOD_MAX_RETRIES", "3")))
_RETRY_CAP = max(1, int(os.getenv("TG_FLOOD_RETRY_CAP", "30")))


async def tg_safe_call(factory, *, label: str = "tg_call"):
    """Вызывает Telegram API с автоматическим retry на 429 / RetryAfter.

    Если Telegram поднимает TelegramRetryAfter — sleep(retry_after) и ретраит.
    После _MAX_RETRIES попыток (или если retry_after > _RETRY_CAP) — exception
    пробрасывается наверх (видимо в логах как раньше).

    Использование:
        # Было: await message.bot.send_message(chat_id=..., text=...)
        # Стало:
        await tg_safe_call(
            lambda: message.bot.send_message(chat_id=..., text=...),
            label="send_ban_public_notice",
        )

    Args:
        factory: callable без аргументов, возвращающий НОВУЮ корутину при
                 каждом вызове. После exception корутину нельзя использовать
                 повторно, поэтому нужна фабрика.
        label: метка для логирования при ретраях.

    Returns:
        Результат await factory().
    """
    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES + 1):  # 1.._MAX_RETRIES+1 попыток
        try:
            return await factory()
        except TelegramRetryAfter as e:
            last_exc = e
            sleep_s = min(e.retry_after, _RETRY_CAP)
            if attempt >= _MAX_RETRIES:
                logger.warning(
                    "tg_safe_call('%s'): TelegramRetryAfter retry_after=%ds "
                    "exceeded max_retries=%d — giving up",
                    label, e.retry_after, _MAX_RETRIES,
                )
                raise
            logger.warning(
                "tg_safe_call('%s'): TelegramRetryAfter retry_after=%ds, "
                "sleeping %ds (attempt %d/%d)",
                label, e.retry_after, sleep_s, attempt + 1, _MAX_RETRIES,
            )
            await asyncio.sleep(sleep_s)
    # Unreachable — but just in case:
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("tg_safe_call: unreachable state")


# ── Конфигурация из окружения ──────────────────────────────────────────────
_raw_admins = os.getenv("ADMIN_IDS", "")
ADMIN_IDS: set[int] = {int(x.strip()) for x in _raw_admins.split(",") if x.strip()}

# Жёсткая привязка chat_id → хэштег (можно переопределить через /sethashtag)
_CHAT_HASHTAGS: dict[int, str] = {}
_raw_hashtags = os.getenv("CHAT_HASHTAGS", "")  # формат: "chat_id1:хэштег1,chat_id2:хэштег2"
for pair in _raw_hashtags.split(","):
    pair = pair.strip()
    if ":" in pair:
        cid, tag = pair.split(":", 1)
        _CHAT_HASHTAGS[int(cid.strip())] = tag.strip()

# МСК-таймзона
MSK = timezone(timedelta(hours=3))

# ── Публичный URL веб-панели (для кликабельных ссылок в отчётах) ───────────
# Если env WEB_PUBLIC_URL не задан — используется дефолт production-инсталляции
# (Bothost). Env позволяет переопределить для локальных/dev инсталляций.
# Формат: только схема+домен(+порт), без завершающего '/'.
WEB_PUBLIC_URL = (os.getenv("WEB_PUBLIC_URL") or "https://degraban.bothost.tech").rstrip("/")


# ── Парсинг длительности: 1d, 2h, 30m, 1d12h ──────────────────────────────
_DURATION_RE = re.compile(
    r"(?:(\d+)(?:d|д))?(?:(\d+)(?:h|ч))?(?:(\d+)(?:m|м))?", re.IGNORECASE
)


def _parse_duration(text: str) -> int | None:
    """Возвращает длительность в секундах или None."""
    m = _DURATION_RE.fullmatch(text.strip())
    if not m:
        return None
    days = int(m.group(1) or 0)
    hours = int(m.group(2) or 0)
    mins = int(m.group(3) or 0)
    total = days * 86400 + hours * 3600 + mins * 60
    return total if total > 0 else None


def _format_duration(seconds: int) -> str:
    """Форматирует секунды в человекочитаемый вид."""
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
    return "".join(parts) if parts else "0м"


# ── v4.7.24: Via-bot rate-limit (in-memory) ─────────────────────────────────
# Хранит timestamp последнего РАЗРЕШЁННОГО via-bot сообщения для каждого
# (chat_id, user_id, bot_id). Если записи нет или она старше
# chat_settings.via_bot_rate_limit_seconds — сообщение разрешаем и обновляем
# timestamp. Иначе — delete + mute.
#
# Ключ: (chat_id, user_id, bot_id). bot_id берётся из message.via_bot.id.
# Per-bot: юзер может отправить 1 сообщение @Bot1 + 1 сообщение @Bot2
# в одном окне (если оба бота используются). Это более user-friendly.
#
# Словарь не персистится между restart'ами — это намеренно: при рестарте
# grace-окно сбрасывается, что не страшно (просто позволяет юзеру ещё
# одно сообщение). При росте словаря старые записи (>1 часа) удаляются
# в _via_bot_rate_limit_cleanup() при каждом вызове.
_via_bot_rate_limit: dict[tuple[int, int, int], datetime] = {}


def _via_bot_rate_limit_cleanup(now: datetime | None = None) -> None:
    """Удаляет записи старше 1 часа — чтобы словарь не рос безгранично."""
    if not _via_bot_rate_limit:
        return
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=1)
    stale = [k for k, ts in _via_bot_rate_limit.items() if ts < cutoff]
    for k in stale:
        del _via_bot_rate_limit[k]


# ── v4.7.27: Дедупликация ручных банов ──────────────────────────────────────
# Когда бот сам банит пользователя через !ban / autoban / CAS / sticker-pack /
# content-filter — Telegram присылает ChatMemberUpdated с new_status="kicked".
# Без дедупликации бот отправил бы ВТОРОЙ отчёт в reporting chat с пометкой
# «ручной бан», хотя на самом деле бан выполнен самим ботом.
#
# Решение: каждый вызов bot.ban_chat_member() в кодовой базе помечается через
# _mark_bot_ban(chat_id, user_id). Когда прилетает ChatMemberUpdated с
# status="kicked", handler _consume_bot_ban проверяет — был ли недавний бот-бан.
# Если да — запись удаляется, и handler молча возвращает (отчёт уже отправлен
# в обычном flow). Если нет — это ручной бан админом через Telegram-клиент,
# отправляем компактный отчёт.
#
# TTL = 10 секунд: достаточно для нормального прохождения update от Telegram
# (обычно <2 сек), при этом не слишком долго — чтобы «честный» ручной бан
# через 11 сек после !ban не был пропущен.
_recent_bot_bans: dict[tuple[int, int], float] = {}
_BOT_BAN_DEDUP_TTL_SEC: float = 10.0


def _mark_bot_ban(chat_id: int, user_id: int) -> None:
    """Отметить, что бот сам забанил пользователя — чтобы ChatMemberUpdated
    handler не отправил повторный отчёт о «ручном бане».

    Вызывается сразу после успешного ``await bot.ban_chat_member(...)`` во
    ВСЕХ точках кодовой базы:
      * ``handle_group_command`` (!ban команда модератора)
      * ``_check_warn_threshold`` (автобан по порогу варнов)
      * ``handle_new_members`` (CAS auto-ban)
      * ``handle_sticker_message`` (ban за banned sticker pack)
      * ``handle_content_filters`` (ban за запрещённое слово/ссылку)

    См. также ``_consume_bot_ban`` — вызывается из ``on_chat_member_updated``.
    """
    _recent_bot_bans[(chat_id, user_id)] = time.monotonic()


def _consume_bot_ban(chat_id: int, user_id: int) -> bool:
    """Проверяет, был ли недавний бот-бан для (chat_id, user_id).

    Если да — удаляет запись (она больше не нужна, бан уже обработан в
    обычном flow) и возвращает True.
    Если нет — возвращает False (это ручной бан от админа через клиент).

    Заодно чистит просроченные записи (TTL = ``_BOT_BAN_DEDUP_TTL_SEC``).
    Используется ``time.monotonic`` (а не ``time.time``) — monotonic не
    прыгает при смене системных часов (NTP), что важно для TTL-логики.
    """
    now = time.monotonic()
    # Чистим просроченные записи (раз уж зашли)
    if _recent_bot_bans:
        expired = [
            k for k, ts in _recent_bot_bans.items()
            if now - ts > _BOT_BAN_DEDUP_TTL_SEC
        ]
        for k in expired:
            _recent_bot_bans.pop(k, None)
    ts = _recent_bot_bans.pop((chat_id, user_id), None)
    return ts is not None


# ── Full mute permissions — запретить ВСЕ виды отправки ──────────────────────
def _mute_permissions() -> types.ChatPermissions:
    """ChatPermissions для полного мьюта: запрещает отправку всех типов контента.

    Telegram интерпретирует None в ChatPermissions как True (разрешено),
    поэтому нужно явно ставить False на каждое контентное поле.

    v4.7.14: убраны 3 админских поля (can_change_info / can_invite_users /
    can_pin_messages). Это права администратора — они выдаются через
    promote_chat_member, а restrict_chat_member их не должен касаться.
    Раньше их явная установка в False приводила к тому, что при unmute
    (когда восстанавливаются права чата через chat_info.permissions) эти
    поля затирали админские права, если они были у пользователя.
    Хотя Telegram обычно игнорирует такие попытки для не-админов,
    поведение было логически некорректным и потенциально опасным.
    """
    return types.ChatPermissions(
        can_send_messages=False,
        can_send_audios=False,
        can_send_documents=False,
        can_send_photos=False,
        can_send_videos=False,
        can_send_video_notes=False,
        can_send_voice_notes=False,
        can_send_polls=False,
        can_send_other_messages=False,
        can_add_web_page_previews=False,
    )


# ── Команды в группах ──────────────────────────────────────────────────────
# v4.8.1: реформа команд ban/warn/mute.
#   • Громкие (!ban/!warn/!mute) — причина ОБЯЗАТЕЛЬНА. После: публичное
#     сообщение в чат + отчёт в репорт-чат (без ephemeral модератору).
#   • Тихие (!sban/!swarn/!smute) — причина НЕОБЯЗАТЕЛЬНА. После: ephemeral
#     модератору (и ephemeral нарушителю для !swarn) + отчёт в репорт-чат.
#     Поведение совпадает с v4.8.0 !ban/!warn/!mute.
#
# v4.8.3: расширение способов указания цели наказания.
#   • Раньше все команды работали ТОЛЬКО по reply на сообщение нарушителя.
#   • Теперь можно указать цель первым аргументом: @username или TGID.
#   • Reply остаётся приоритетным: если есть reply — цель из аргумента игнорируется.
#   • Скриншот: модератор может приложить фото к команде (caption содержит !ban ...).
#
# Группа target: (?P<target>@\w+|\d+) — @username (начинается с @) или TGID (только цифры).
# Если target не указан — команда требует reply (для !ban/!warn/!mute) или
# работает по reply если он есть (для !sban/!swarn/!smute — иначе target=None,
# что приведёт к ошибке резолва в _resolve_punishment_target).
_CMD_MUTE = re.compile(
    r"^!mute\s+(?:(?P<target>@\w+|\d+)\s+)?(?P<dur>\d+[a-zа-я]+)\s+(?P<reason>.+)$",
    re.IGNORECASE,
)  # dur + reason обязательны; target опционален (если нет — нужен reply).
_CMD_WARN = re.compile(
    r"^!warn\s+(?:(?P<target>@\w+|\d+)\s+)?(?P<reason>(?!@\w+$|\d+$).+)$",
    re.IGNORECASE,
)  # reason обязательна; target опционален.
# v4.8.6: добавлен negative lookahead (?!@\w+$|\d+$) в reason — иначе
# `!warn @username` (без причины) матчило reason="@username" и бан влетал
# на reply-target с некорректной причиной. Теперь такой ввод не матчится
# → handler вернёт "укажите причину".
_CMD_BAN = re.compile(
    r"^!ban\s+(?:(?P<target>@\w+|\d+)\s+)?(?P<reason>(?!@\w+$|\d+$).+)$",
    re.IGNORECASE,
)  # reason обязательна; target опционален.
# v4.8.6: аналогичный фикс как для _CMD_WARN (см. выше).
# v4.8.1: тихие команды (stealth). s = silent/stealth.
# v4.8.3.1 HOTFIX: regex переосмыслен — _CMD_SWARN/_CMD_SBAN в v4.8.3 не матчили
#   `!swarn Причина` (bare reason без @username/TGID), потому что внутренняя
#   группа `(?P<reason>.+)` требовала свой собственный `\s+`, но он уже был
#   съеден внешней `\s+`. Исправлено: target и reason — два независимых
#   optional-блока на верхнем уровне, каждый со своим `\s+`. Это позволяет
#   матчить все варианты: `!swarn`, `!swarn Причина`, `!swarn @user`,
#   `!swarn @user Причина`, `!swarn 12345`, `!swarn 12345 Причина`.
# v4.8.3.1 HOTFIX: _CMD_SMUTE в v4.8.3 делал всё тело опциональным, поэтому
#   `!smute` без длительности матчило (dur=None), а handler звал
#   `_parse_duration(None)` → AttributeError. Regex оставлен как есть
#   (dur внутри опциональной группы), но handler теперь явно проверяет
#   `dur is None` и отправляет ephemeral с подсказкой формата.
_CMD_SMUTE = re.compile(
    r"^!smute(?:\s+(?:(?P<target>@\w+|\d+)\s+)?(?P<dur>\d+[a-zа-я]+)(?:\s+(?P<reason>.+))?)?$",
    re.IGNORECASE,
)  # dur обяз. ЕСЛИ есть аргументы; reason опц.; target опц.
_CMD_SWARN = re.compile(
    r"^!swarn(?:\s+(?P<target>@\w+|\d+))?(?:\s+(?P<reason>.+))?$",
    re.IGNORECASE,
)  # reason опциональна; target опционален; любой из них может быть один.
_CMD_SBAN = re.compile(
    r"^!sban(?:\s+(?P<target>@\w+|\d+))?(?:\s+(?P<reason>.+))?$",
    re.IGNORECASE,
)  # reason опциональна; target опционален; любой из них может быть один.
_CMD_UNMUTE = re.compile(r"^!unmute\s*$", re.IGNORECASE)
_CMD_UNBAN = re.compile(r"^!unban\s*$", re.IGNORECASE)
_CMD_UNWARN = re.compile(r"^!unwarn(?:\s+(\d+))?\s*$", re.IGNORECASE)
_CMD_WARNS = re.compile(r"^!warns\s*$", re.IGNORECASE)
_CMD_RESETWARNS = re.compile(r"^!resetwarns\s*$", re.IGNORECASE)
# v4.8.4: !resetmc — сброс счётчика автомьютов (прогрессивные муты).
# Цель: reply на сообщение, ИЛИ !resetmc @username, ИЛИ !resetmc <tgid>.
# Доступ: только SU/Admin (как !resetwarns).
_CMD_RESETMC = re.compile(
    r"^!resetmc(?:\s+(?P<target>@\w+|\d+))?\s*$",
    re.IGNORECASE,
)
# v4.7.20: !alarm on [duration] / !alarm off
# Длительность: опциональная, форматы "1ч" / "1h" / "30м" / "30m" / "2д" / "2d".
# Если не указана — alarm активен до ручного !alarm off.
# Примеры: "!alarm on", "!alarm on 1ч", "!alarm on 2h", "!alarm off"
_CMD_ALARM = re.compile(
    r"^!alarm\s+(on|off|вкл|выкл)"           # on/off (или русские алиасы)
    r"(?:\s+(\d+)\s*(ч|h|м|m|д|d))?"         # опциональная длительность
    r"\s*$",
    re.IGNORECASE,
)


# ── Список всех команд модерации (для ранней проверки, что текст вообще ────
#    является командой, ДО удаления сообщения модератора). v4.4.8 FIX.
# v4.8.1: добавлены тихие команды !sban/!swarn/!smute.
# v4.8.4: добавлена команда !resetmc (сброс счётчика автомьютов).
_ALL_MOD_COMMANDS: tuple[re.Pattern, ...] = (
    _CMD_MUTE, _CMD_WARN, _CMD_BAN,
    _CMD_SMUTE, _CMD_SWARN, _CMD_SBAN,
    _CMD_UNMUTE, _CMD_UNBAN, _CMD_UNWARN,
    _CMD_WARNS, _CMD_RESETWARNS, _CMD_RESETMC,
    _CMD_ALARM,
)


def _is_moderation_command(text: str) -> bool:
    """Возвращает True, если текст — это одна из модераторских команд бота.

    Используется как ранняя guard-проверка в handle_group_command, чтобы
    бот не удалял обычные ответы модератора в чате (только сообщения с командой).
    """
    # Быстрый short-circuit: команды начинаются с '!'. Если не начинается —
    # точно не команда, не трогаем сообщение.
    stripped = text.lstrip()
    if not stripped.startswith("!"):
        return False
    # Точное соответствие одному из зарегистрированных паттернов.
    return any(p.match(stripped) for p in _ALL_MOD_COMMANDS)


# ── v4.7.26: Custom-фильтры для команд — исправляют баг с propagation ──────
# Проблема: handle_group_command (L2909) и handle_alarm_command (L3588)
# использовали общий фильтр F.chat.type.in_(...) + (F.reply_to_message).
# В aiogram 3.x первый matching handler останавливает propagation — поэтому
# для любого reply-сообщения (даже без команды) handle_group_command
# перехватывал управление и return'ил, не давая handle_content_filters
# (word/link/via_bot filter) сработать. Аналогично handle_alarm_command
# перехватывал ВСЕ group messages (даже без !alarm).
# Фикс: фильтр должен матчить ТОЛЬКО когда текст реально является командой.
# Тогда не-команды проваливаются к следующему handler'у.
class _ModerationCommandFilter(BaseFilter):
    """v4.7.26: матчит только сообщения, содержащие модераторскую команду.

    Проверяет text на соответствие любому из _ALL_MOD_COMMANDS паттернов.
    Используется в handle_group_command чтобы НЕ перехватывать обычные
    reply-сообщения (тогда они проваливаются в handle_content_filters
    для word/link/via_bot проверки).

    v4.8.3: если message.text пустой — проверяем message.caption.
    Это позволяет модератору отправить фото (скриншот нарушения) с
    командой в caption (например «!ban @user Дурачок» под фото).
    """

    async def __call__(self, message: types.Message) -> bool:
        text = message.text or message.caption
        if not text:
            return False
        return _is_moderation_command(text)


class _AlarmCommandFilter(BaseFilter):
    """v4.7.26: матчит только сообщения вида '!alarm on|off|вкл|выкл ...'.

    Используется в handle_alarm_command чтобы НЕ перехватывать обычные
    текстовые сообщения (тогда они проваливаются в handle_content_filters
    для word/link/via_bot проверки).
    """

    async def __call__(self, message: types.Message) -> bool:
        text = message.text
        if not text:
            return False
        return bool(_CMD_ALARM.match(text))


# ── v4.4.8: middleware для полной блокировки disabled-чатов ──────────────
# Когда в /admin/chats ставят метку Disable (is_enabled=False), бот должен
# ПЕРЕСТАТЬ ВОСПРИНИМАТЬ ВООБЩЕ ВСЁ в этом чате: ни команды модераторов,
# ни авто-создание chat_settings, ни catchall-обработку. Просто молча
# игнорируем каждое сообщение, как будто бота там нет.
#
# Это outer_middleware — выполняется ДО любых фильтров и хэндлеров router.message.
# На my_chat_member не распространяется (нам важно по-прежнему ловить добавление
# бота обратно в чат, чтобы можно было его снова включить).
class _DisabledChatMiddleware(BaseMiddleware):
    """v4.4.8: полностью игнорирует сообщения в чатах с is_enabled=False.

    Логика:
      • Личные сообщения пропускает как есть (у них нет chat_settings).
      • Для group/supergroup — проверяем chat_settings.is_enabled.
        - Если settings нет — пропускаем (catchall сам создаст).
        - Если settings есть и is_enabled=False — молча return (short-circuit).
        - Если is_enabled=True — пропускаем к хэндлерам.
      • Любая ошибка БД — логируем и пропускаем (fail-open, чтобы не положить
        бота целиком из-за сбоя БД; модераторские команды всё равно проверят
        is_enabled через _is_admin).
    """

    async def __call__(self, handler, event: types.Message, data: dict):
        # Только для групп; личные сообщения не фильтруем.
        chat_type = event.chat.type if event.chat else None
        if chat_type not in ("group", "supergroup"):
            return await handler(event, data)

        try:
            async with async_session() as session:
                settings = (await session.execute(
                    select(ChatSettings).where(ChatSettings.chat_id == event.chat.id)
                )).scalar_one_or_none()
        except Exception as e:
            logger.warning(
                "DisabledChatMiddleware: DB check failed for chat_id=%s: %s — fail-open",
                event.chat.id, e,
            )
            return await handler(event, data)

        # Нет настроек — чат ещё не зарегистрирован. Пропускаем, чтобы
        # stealth_catchall_group мог его создать (это не disabled-чат).
        if settings is None:
            return await handler(event, data)

        # Чат выключен — полностью игнорируем сообщение.
        if not settings.is_enabled:
            return  # short-circuit: не вызываем handler

        return await handler(event, data)


# Регистрируем middleware как outer — обрабатывает ВСЕ router.message события.
router.message.outer_middleware(_DisabledChatMiddleware())


# ── Permissions snapshot ───────────────────────────────────────────────────
_PERM_FIELDS = [
    "can_send_messages",
    "can_send_audios",
    "can_send_documents",
    "can_send_photos",
    "can_send_videos",
    "can_send_video_notes",
    "can_send_voice_notes",
    "can_send_polls",
    "can_send_other_messages",
    "can_add_web_page_previews",
    "can_change_info",
    "can_invite_users",
    "can_pin_messages",
]


def _snapshot_permissions(member: types.ChatMember) -> str | None:
    """Сериализует текущие пермишены ChatMember в JSON-строку."""
    perms = getattr(member, "permissions", None)
    if perms is None:
        return None
    data = {field: bool(getattr(perms, field, False)) for field in _PERM_FIELDS}
    return json.dumps(data, ensure_ascii=False)


def _restore_permissions(snapshot_json: str) -> types.ChatPermissions:
    """Десериализует JSON-снапшот в объект ChatPermissions."""
    data = json.loads(snapshot_json)
    return types.ChatPermissions(**{k: data.get(k, False) for k in _PERM_FIELDS})


# ── v4.7.20: !alarm helpers ────────────────────────────────────────────────
# !alarm — экстренная "паническая кнопка" для модераторов. При включении:
#   • отключаются ВСЕ медиа (фото, видео, стикеры, голосовые, документы и т.д.)
#   • остаётся только текст (can_send_messages=True)
#   • ставится slow_mode_delay=30 сек
# Аналогично ночному режиму, но включается вручную и НЕ должно конфликтовать
# с night mode: при входе в night mode активный alarm автоматически снимается
# (т.к. night mode и так ограничивает права — alarm избыточен).
#
# Используется когда в чате начинается флуд медиа (стикерами, гифками) и
# нужно быстро всё заглушить, не прибегая к санитарному дню (который
# требует планирования дат) или ночному режиму (который работает по расписанию).

# Slow mode при alarm (секунд). 30 — достаточно чтобы дать флудеру остыть,
# но не слишком долго чтобы мешать нормальному диалогу.
_ALARM_SLOW_MODE_DELAY = 30


def _alarm_permissions() -> types.ChatPermissions:
    """ChatPermissions для !alarm: только текст, всё медиа запрещено.

    Аналог strict night mode, но задаётся явно (не через пресет) —
    чтобы !alarm работал даже если в чате нет настроенного night mode.
    can_send_messages=True (текст разрешён), всё остальное False.
    Админские права (can_change_info / can_invite_users / can_pin_messages)
    НЕ трогаем — это отдельный уровень прав через promote_chat_member.
    """
    return types.ChatPermissions(
        can_send_messages=True,
        can_send_audios=False,
        can_send_documents=False,
        can_send_photos=False,
        can_send_videos=False,
        can_send_video_notes=False,
        can_send_voice_notes=False,
        can_send_polls=False,
        can_send_other_messages=False,
        can_add_web_page_previews=False,
        # v4.7.14: НЕ включаем админские поля — restrict_chat_member
        # их не должен касаться. См. _mute_permissions().
    )


def _parse_alarm_duration(value: str | None, unit: str | None) -> timedelta | None:
    """Парсит длительность для !alarm on N<unit>.

    Поддерживаемые единицы:
      • "ч" / "h" — часы
      • "м" / "m" — минуты
      • "д" / "d" — дни

    Возвращает timedelta или None (если value/unit пустые).
    Raises ValueError если value не число или <= 0.
    """
    if not value or not unit:
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"Некорректное число длительности: {value!r}")
    if n <= 0:
        raise ValueError(f"Длительность должна быть > 0, получено {n}")
    unit_lower = unit.lower()
    if unit_lower in ("ч", "h"):
        return timedelta(hours=n)
    if unit_lower in ("м", "m"):
        return timedelta(minutes=n)
    if unit_lower in ("д", "d"):
        return timedelta(days=n)
    raise ValueError(f"Неизвестная единица длительности: {unit!r}")


def _format_alarm_duration(td: timedelta) -> str:
    """Человекочитаемое описание длительности для DM-уведомления."""
    total_sec = int(td.total_seconds())
    if total_sec >= 86400:
        days, rem = divmod(total_sec, 86400)
        if rem == 0:
            return f"{days}д"
        hours = rem // 3600
        return f"{days}д {hours}ч"
    if total_sec >= 3600:
        hours, rem = divmod(total_sec, 3600)
        if rem == 0:
            return f"{hours}ч"
        minutes = rem // 60
        return f"{hours}ч {minutes}м"
    minutes = total_sec // 60
    return f"{minutes}м"


def _alarm_perms_source_to_human(source: str) -> str:
    """v4.7.30: переводит техническую source-строку из _deactivate_alarm
    в человекочитаемое описание для DM-уведомления модератору.

    Зачем: раньше DM при !alarm off всегда писал "восстановлено из snapshot",
    но реально права могли восстановиться из day_permissions preset. Был Баг #5
    аудита v4.7.30 — недостоверная информация модератору.

    Возвращает читаемое описание. Если source неизвестен — fallback.
    """
    if not source:
        return "(источник неизвестен)"
    if source == "day_permissions preset":
        return "из day_permissions пресета чата"
    if source == "alarm_saved_permissions snapshot":
        return "из сохранённого snapshot (состояние до alarm)"
    if source == "hardcoded default (all allowed)":
        return "из системного дефолта (всё разрешено)"
    # Fallback — показываем как есть, чтобы ничего не потерять.
    return f"из источника: {source}"


def _alarm_slow_source_to_human(source: str) -> str:
    """v4.7.30: переводит техническую slow_source-строку из _deactivate_alarm
    в человекочитаемое описание для DM-уведомления модератору.

    Аналогично _alarm_perms_source_to_human — для slow_mode_delay.
    """
    if not source:
        return "0 сек (выключен)"
    if source == "default 0":
        return "0 сек (выключен)"
    if source.startswith("day_slow_mode_delay="):
        # Формат: "day_slow_mode_delay=60"
        try:
            val = int(source.split("=", 1)[1])
            return f"{val} сек (из day_slow_mode_delay пресета)"
        except (ValueError, IndexError):
            return source
    if source.startswith("alarm_saved_slow_mode_delay="):
        # Формат: "alarm_saved_slow_mode_delay=30"
        try:
            val = int(source.split("=", 1)[1])
            return f"{val} сек (из сохранённого snapshot до alarm)"
        except (ValueError, IndexError):
            return source
    # Fallback
    return source


async def _deactivate_alarm(
    session,
    cs: ChatSettings,
    bot: types.Bot,
    chat_id: int,
    *,
    reason: str = "manual",
) -> tuple[bool, str, str]:
    """Снимает alarm и восстанавливает права чата.

    Логика восстановления (приоритет):
      1. cs.day_permissions (если задан пресет)
      2. cs.alarm_saved_permissions (snapshot ДО alarm)
      3. Системный пресет "Day default"
      4. Hardcoded _DAY_DEFAULT (всё разрешено, без админских полей)

    Slow mode восстанавливается из:
      1. cs.day_slow_mode_delay (если > 0)
      2. cs.alarm_saved_slow_mode_delay (snapshot)
      3. 0 (выключить)

    v4.7.30: возвращает кортеж (success, perms_source, slow_source) вместо
    просто bool (Баг #5 аудита v4.7.30). Нужно для достоверного DM-сообщения
    в handle_alarm_command — раньше бот всегда писал "восстановлено из
    snapshot" даже если реально восстановил из preset. Теперь вызывающий
    код знает, откуда реально восстановлены права, и пишет правду.

    v4.8.0: права восстанавливаются через _resolve_restore_perms_sync
    (chat_modes.py) — унифицированно с night и sanitary. Системный пресет
    «Day default» пока НЕ проверяется здесь (sync-вариант без обращения к БД
    для permission_presets) — это упрощение сделано намеренно, чтобы не
    дёргать лишний SQL. Если в будущем понадобится — переключить на async
    вариант. Hardcoded fallback остаётся последним эшелоном.

    Параметры:
      session — async SQLAlchemy session (вызывающий код управляет транзакцией)
      cs — объект ChatSettings (поле alarm_currently_active должно быть True)
      bot — экземпляр Bot для вызова set_chat_permissions / SetChatSlowModeDelay
      chat_id — ID чата
      reason — причина деактивации ("manual", "auto_off_timeout",
               "night_mode_enter", "sanitary_day_enter") — для логов

    Возвращает:
      (True, perms_source_str, slow_source_str) при успехе
      (False, "", "") при неудаче (например, set_chat_permissions упал)

    Логирует все шаги.
    """
    if not cs.alarm_currently_active:
        logger.info("Alarm deactivate: chat %s not active (reason=%s)", chat_id, reason)
        return False, "", ""

    # Шаг 1: определяем права для восстановления.
    # v4.8.0: используем унифицированную функцию из chat_modes.py.
    from chat_modes import _apply_chat_permissions, _resolve_restore_perms_sync
    perms_to_restore, perms_source = _resolve_restore_perms_sync(
        cs=cs,
        saved_permissions_field=cs.alarm_saved_permissions,
        saved_source_name="alarm_saved_permissions snapshot",
    )

    # Шаг 2: определяем slow_mode для восстановления
    slow_to_restore: int = 0
    slow_source = "default 0"
    if cs.day_slow_mode_delay and cs.day_slow_mode_delay > 0:
        slow_to_restore = cs.day_slow_mode_delay
        slow_source = f"day_slow_mode_delay={slow_to_restore}"
    elif cs.alarm_saved_slow_mode_delay is not None:
        slow_to_restore = cs.alarm_saved_slow_mode_delay
        slow_source = f"alarm_saved_slow_mode_delay={slow_to_restore}"

    # Шаг 3: применяем права (v4.8.0: через унифицированную обёртку)
    ok = await _apply_chat_permissions(bot, chat_id, perms_to_restore)
    if not ok:
        return False, "", ""
    logger.info(
        "Alarm deactivate: chat %s perms restored from %s",
        chat_id, perms_source,
    )

    # Шаг 4: применяем slow_mode
    # v4.7.22: SetChatSlowModeDelay определён в bot_handlers.py (top-level),
    # импорт не нужен. Раньше был late import `from bot import SetChatSlowModeDelay`,
    # но это вызывал повторный import bot.py как отдельного модуля (bot.py запускается
    # как __main__) → RuntimeError: Router is already attached.
    try:
        await bot(SetChatSlowModeDelay(
            chat_id=chat_id, slow_mode_delay=slow_to_restore,
        ))
        logger.info(
            "Alarm deactivate: chat %s slow_mode restored to %s (from %s)",
            chat_id, slow_to_restore, slow_source,
        )
    except TelegramAPIError as e:
        logger.warning(
            "Alarm deactivate: set_chat_slow_mode_delay failed for chat %s: %s "
            "(continuing — alarm fields will still be cleared)",
            chat_id, e,
        )

    # Шаг 5: очищаем поля alarm в БД
    # Сохраняем started_by ДО зануления — для лога ниже.
    started_by_log = cs.alarm_started_by
    cs.alarm_currently_active = False
    cs.alarm_saved_permissions = None
    cs.alarm_saved_slow_mode_delay = None
    cs.alarm_active_until = None
    cs.alarm_started_by = None
    await session.commit()

    logger.info(
        "Alarm deactivated: chat %s reason=%s started_by=%s (perms=%s, slow=%s)",
        chat_id, reason, started_by_log, perms_source, slow_source,
    )
    return True, perms_source, slow_source


def _user_mention_html(user: types.User) -> str:
    """Возвращает HTML-mention юзера для модераторских уведомлений.

    Имя экранируется от HTML-инъекций, ссылка ведёт на профиль юзера
    (``tg://user?id=...``) — модератор может кликнуть и открыть профиль.
    """
    name = (user.first_name or "") + (
        f" {user.last_name}" if user.last_name else ""
    )
    name = name.strip() or f"id:{user.id}"
    name = html.escape(name, quote=False)
    return f'<a href="tg://user?id={user.id}">{name}</a>'


def _user_display_name(user: types.User) -> str:
    """Возвращает 'Имя Фамилия' или 'id:<user_id>' если имени нет.

    Используется в RichTextUrl.text — без экранирования (Rich Messages
    сами управляют разметкой), без HTML.
    """
    name = (user.first_name or "") + (
        f" {user.last_name}" if user.last_name else ""
    )
    return name.strip() or f"id:{user.id}"


# ── Helpers: получение/сохранение настроек чата ────────────────────────────
async def _get_chat_settings(session, chat_id: int) -> ChatSettings:
    """Возвращает настройки чата, создаёт с дефолтами если нет."""
    stmt = select(ChatSettings).where(ChatSettings.chat_id == chat_id)
    result = await session.execute(stmt)
    settings = result.scalar_one_or_none()
    if settings is None:
        settings = ChatSettings(chat_id=chat_id)
        if chat_id in _CHAT_HASHTAGS:
            settings.hashtag = _CHAT_HASHTAGS[chat_id]
        session.add(settings)
        await session.flush()
    return settings


async def _is_admin(session, chat_id: int, user_id: int) -> bool:
    """v4.4.7: унифицированная проверка прав на модеративные команды в чате.

    Логика:
      • ADMIN_IDS env (глобальные супер-админы) — всегда True.
      • Чат выключен (chat_settings.is_enabled=False) — всегда False.
      • SU (web_users.role='su', привязан к TG ID) — True во всех чатах.
      • Admin (web_users.role='admin') — True в обычных чатах; False в приватных.
      • Moderator (web_users.role='moderator') — True только если есть запись
        в chat_admins (явная привязка к этому чату).
      • Активен только если web_users.is_active=True.
      • Если нет веб-аккаунта, но есть запись в chat_admins (старый сценарий) — True.
        Это сохраняет обратную совместимость с TG-only модераторами, добавленными
        через /addadmin без создания веб-профиля.

    v4.5.1 FIX: если WebUser существует, но is_active=False — возвращаем False
    СРАЗУ, не падая в fallback к TG-only проверке. Раньше деактивированный
    модератор сохранял доступ через chat_admins (запись не удалялась при toggle),
    что делало кнопку «Disable» в веб-панели бесполезной.
    """
    # 1. Глобальные супер-админы из env
    if user_id in ADMIN_IDS:
        return True

    # 2. Настройки чата — может быть чат выключен
    settings = await _get_chat_settings(session, chat_id)
    if not settings.is_enabled:
        return False

    # 3. Ищем веб-профиль по TG ID
    wu = (await session.execute(
        select(WebUser).where(WebUser.tg_user_id == user_id)
    )).scalar_one_or_none()
    if wu:
        # v4.5.1 FIX: WebUser есть, но деактивирован → доступ закрыт НАВСЕГДА,
        # даже если в chat_admins осталась запись. SU должен иметь возможность
        # полностью отзывать доступ через /admin/users toggle.
        if not wu.is_active:
            return False
        if wu.role == "su":
            return True
        if wu.role == "admin":
            # v4.7.6: упразднена система private/non-private чатов.
            # Раньше: админ не лезет в приватные чаты (return not settings.is_private).
            # Теперь: админ имеет доступ во все чаты (как и SU, но без права удалять чат).
            return True
        if wu.role == "moderator":
            # модератор — только если явно привязан к этому чату
            ca = (await session.execute(
                select(ChatAdmin).where(
                    ChatAdmin.chat_id == chat_id,
                    ChatAdmin.user_id == user_id,
                )
            )).scalars().first()
            return ca is not None
        # Неизвестная роль — запрещаем (safe default)
        return False

    # 4. Fallback: TG-only модератор (нет веб-аккаунта, но есть chat_admins)
    # Сюда попадаем ТОЛЬКО если WebUser не найден вовсе. Деактивированный
    # аккаунт сюда уже не дойдёт (return False выше).
    ca = (await session.execute(
        select(ChatAdmin).where(
            ChatAdmin.chat_id == chat_id,
            ChatAdmin.user_id == user_id,
        )
    )).scalars().first()
    return ca is not None


async def _get_web_user_role(session, user_id: int) -> str | None:
    """v4.5.1: возвращает роль WebUser по tg_user_id, либо None если профиля нет.

    Используется для проверки, имеет ли модератор право на !resetwarns
    (только admin/su). Возвращает роль даже если is_active=False —
    вызывающий код сам решит что с этим делать.
    """
    wu = (await session.execute(
        select(WebUser).where(WebUser.tg_user_id == user_id)
    )).scalar_one_or_none()
    if wu is None:
        return None
    return wu.role or ("su" if wu.is_su else "admin")


async def _get_chat_admins(session, chat_id: int) -> list[ChatAdmin]:
    """Возвращает список дополнительных админов чата."""
    stmt = select(ChatAdmin).where(ChatAdmin.chat_id == chat_id)
    result = await session.execute(stmt)
    return list(result.scalars().all())


# ── v4.4.7: авто-обнаружение чатов ─────────────────────────────────────────
async def _ensure_chat_settings(
    session, chat_id: int, title: str | None = None,
) -> tuple[ChatSettings, bool]:
    """Создаёт chat_settings для чата, если её ещё нет.

    Возвращает (settings, created). Если created=True и у SU привязан TG ID —
    вызывающий код должен отправить SU уведомление (через _notify_su_about_chat).
    """
    stmt = select(ChatSettings).where(ChatSettings.chat_id == chat_id)
    settings = (await session.execute(stmt)).scalar_one_or_none()
    if settings is None:
        settings = ChatSettings(chat_id=chat_id, title=title)
        if chat_id in _CHAT_HASHTAGS:
            settings.hashtag = _CHAT_HASHTAGS[chat_id]
        session.add(settings)
        await session.flush()
        return settings, True
    # Обновляем title если он изменился и новый передан
    if title and settings.title != title:
        settings.title = title
    return settings, False


async def _notify_su_about_chat(bot, chat_id: int, chat_title: str | None) -> None:
    """Отправляет SU в ЛС уведомление о новом чате (best-effort).

    Находит всех SU-юзеров с привязанным tg_user_id и шлёт им сообщение.
    Ошибки игнорируются (это уведомление, не критично).
    """
    try:
        async with async_session() as session:
            sus = (await session.execute(
                select(WebUser).where(
                    WebUser.role == "su",
                    WebUser.is_active.is_(True),
                    WebUser.tg_user_id.is_not(None),
                )
            )).scalars().all()
        if not sus:
            return
        title_display = chat_title or "(без названия)"
        text = (
            f"🆕 Бот добавлен в новый чат:\n"
            f"   <b>{html.escape(title_display, quote=False)}</b>\n"
            f"   ID: <code>{chat_id}</code>\n\n"
            f"Настройте чат в веб-панели: Chats → выберите этот чат.\n"
            f"Можно: задать хэштег, выбрать чат для отчётов, "
            f"пометить как приватный, выключить бота в чате."
        )
        for su in sus:
            try:
                await bot.send_message(chat_id=su.tg_user_id, text=text, parse_mode="HTML")
            except Exception as e:
                logger.info("notify_su_about_chat: failed for su tg=%s: %s", su.tg_user_id, e)
    except Exception as e:
        logger.warning("notify_su_about_chat: %s", e)


# ── Helpers: upsert ────────────────────────────────────────────────────────
async def _upsert_user(session, user_id: int, username: str | None,
                       first_name: str | None, last_name: str | None) -> User:
    stmt = select(User).where(User.user_id == user_id)
    result = await session.execute(stmt)
    user = result.scalar_one_or_none()
    if user is None:
        user = User(user_id=user_id, username=username,
                    first_name=first_name, last_name=last_name)
        session.add(user)
    else:
        if username:
            user.username = username
        if first_name:
            user.first_name = first_name
        if last_name:
            user.last_name = last_name
        user.last_seen = datetime.now(timezone.utc)
    await session.flush()
    return user


async def _upsert_moderator(session, mod_id: int,
                            username: str | None, first_name: str | None) -> Moderator:
    stmt = select(Moderator).where(Moderator.mod_id == mod_id)
    result = await session.execute(stmt)
    mod = result.scalar_one_or_none()
    if mod is None:
        mod = Moderator(mod_id=mod_id, username=username, first_name=first_name)
        session.add(mod)
    else:
        if username:
            mod.username = username
        if first_name:
            mod.first_name = first_name
    await session.flush()
    return mod


# ── v4.8.3: Резолв цели наказания ─────────────────────────────────────────
async def _resolve_punishment_target(
    message: types.Message,
    target_str: str | None,
    chat_id: int,
) -> tuple[types.User | None, str | None]:
    """Резолвит цель наказания из reply / @username / TGID.

    Возвращает кортеж (user, error_message):
      • Если успех — (types.User, None).
      • Если неудача — (None, str) — error_message для ephemeral модератору.

    Приоритет:
      1. Если message.reply_to_message существует — берём from_user из reply.
         (target_str игнорируется — reply приоритетнее.)
      2. Если target_str начинается с '@' — это username:
         a) Сначала проверяем entities сообщения — может быть MessageEntityTextMention
            (inline-mention через клиентское подсказки, содержит user.id напрямую).
         b) Иначе ищем в БД User.username (нормализуем — убираем @, lower case).
            Работает только если юзер ранее был в чате и бот его сохранил.
         c) Если не найден — ошибка.
      3. Если target_str — чистые цифры — это TGID:
         Парсим int, проверяем через bot.get_chat_member что юзер в чате.
         Если да — создаём синтетический User-объект (id, без username/first_name).
      4. Если target_str is None и reply нет — ошибка «укажите цель».
    """
    # 1. Reply — приоритет.
    if message.reply_to_message is not None:
        return message.reply_to_message.from_user, None

    # Нет reply — нужен target_str.
    if not target_str:
        return None, (
            "❌ Не указана цель. Используйте reply на сообщение нарушителя, "
            "либо укажите @username или TGID первым аргументом.\n"
            "Пример: <code>!ban @username Причина</code> или "
            "<code>!ban 12345678 Причина</code>"
        )

    # 2. @username
    if target_str.startswith("@"):
        username_raw = target_str[1:]  # убираем @
        username_lower = username_raw.lower()

        # 2a. Проверяем entities — может быть inline-mention с user.id
        entities = message.entities or message.caption_entities or []
        for ent in entities:
            # MessageEntityTextMention имеет user с id напрямую
            if ent.type == "text_mention" and ent.user is not None:
                # Проверяем что это упоминание именно нашего @username
                # (а не какого-то другого в тексте).
                # ent.offset/length указывают на подстроку в message.text/caption.
                full_text = message.text or message.caption or ""
                mentioned_text = full_text[ent.offset:ent.offset + ent.length]
                if mentioned_text.lstrip("@").lower() == username_lower:
                    return ent.user, None

        # 2b. Ищем в БД
        async with async_session() as session:
            stmt = select(User).where(func.lower(User.username) == username_lower)
            result = await session.execute(stmt)
            db_user = result.scalar_one_or_none()

        if db_user is not None:
            # Создаём синтетический User-объект из БД-записи.
            synth_user = types.User(
                id=db_user.user_id,
                is_bot=False,
                first_name=db_user.first_name or "",
                last_name=db_user.last_name or "",
                username=db_user.username,
            )
            return synth_user, None

        # 2c. Не найден
        return None, (
            f"❌ Пользователь <code>{target_str}</code> не найден в БД бота.\n"
            f"Возможно, он никогда не писал в чат, который бот модерировал.\n"
            f"Используйте reply на сообщение нарушителя или его TGID."
        )

    # 3. TGID (чистые цифры)
    if target_str.isdigit():
        try:
            user_id = int(target_str)
        except ValueError:
            return None, f"❌ Некорректный TGID: <code>{target_str}</code>"

        if user_id <= 0:
            return None, "❌ TGID должен быть положительным числом."

        # Проверяем что юзер есть в чате (иначе банить некого).
        try:
            member = await message.bot.get_chat_member(
                chat_id=chat_id, user_id=user_id,
            )
        except TelegramAPIError as e:
            return None, (
                f"❌ Не удалось найти пользователя с TGID <code>{user_id}</code> "
                f"в этом чате: {e}"
            )

        # member.user — это types.User (даже если юзер покинул чат, но был ранее).
        if member and member.user:
            return member.user, None

        return None, (
            f"❌ Пользователь с TGID <code>{user_id}</code> не найден в чате."
        )

    # 4. Не распознано
    return None, (
        f"❌ Не удалось распознать цель: <code>{target_str}</code>\n"
        f"Используйте @username (с @), TGID (только цифры), или reply."
    )


async def _save_punishment(session, user_id: int, mod_id: int,
                           chat_id: int, action_type: str,
                           duration_seconds: int | None,
                           reason: str | None,
                           message_text: str | None,
                           permissions_snapshot: str | None = None,
                           report_message_id: int | None = None) -> Punishment:
    """Сохраняет запись о санкции в БД.

    Параметр ``report_message_id`` оставлен для обратной совместимости
    (soft-deprecated). После перехода на Rich Messages медиа встраивается
    инлайн в rich-сообщение, и отдельный message_id пересылки не нужен.
    Колонка в DB сохраняется для старых записей, новые не пишут.
    """
    punishment = Punishment(
        user_id=user_id,
        mod_id=mod_id,
        chat_id=chat_id,
        action_type=action_type,
        duration_seconds=duration_seconds,
        reason=reason,
        message_text=message_text,
        permissions_snapshot=permissions_snapshot,
        # report_message_id больше не записываем — soft-deprecated
    )
    session.add(punishment)
    await session.commit()
    return punishment


async def _fetch_last_snapshot(session, user_id: int, chat_id: int) -> str | None:
    """Находит снапшот пермишенов из последнего mute/ban для данного юзера в чате."""
    stmt = (
        select(Punishment.permissions_snapshot)
        .where(
            Punishment.user_id == user_id,
            Punishment.chat_id == chat_id,
            Punishment.action_type.in_(["mute", "ban"]),
            Punishment.permissions_snapshot.isnot(None),
        )
        .order_by(desc(Punishment.created_at))
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def _count_warns(session, user_id: int, chat_id: int) -> int:
    """Считает общее количество активных (не снятых, не «погашенных») варн-поинтов
    для юзера в чате.

    Использует `duration_seconds` как поинты варна (обычно 1).
    Снятые варны (``is_revoked=True``) не учитываются.
    v4.5.1: варны, погашенные авто-мьютом/баном (``consumed_by_action IS NOT NULL``),
    тоже не учитываются — это исправляет баг с повторным триггером автомьюта
    при каждом следующем !warn.
    v4.5.2: warn decay — если у чата выставлено ``warn_decay_days > 0``, варны
    старше этого количества дней не учитываются. Сама запись в БД сохраняется
    (для истории/веб-панели), но не влияет на пороги. 0 = отключено (по умолчанию).
    """
    stmt = (
        select(func.coalesce(func.sum(Punishment.duration_seconds), 0))
        .where(
            Punishment.user_id == user_id,
            Punishment.chat_id == chat_id,
            Punishment.action_type == "warn",
            Punishment.is_revoked.is_(False),
            Punishment.consumed_by_action.is_(None),
        )
    )
    # v4.5.2: warn decay
    cs = await _get_chat_settings(session, chat_id)
    decay_days = cs.warn_decay_days or 0
    if decay_days > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(days=decay_days)
        stmt = stmt.where(Punishment.created_at >= cutoff)
    result = await session.execute(stmt)
    return int(result.scalar() or 0)


async def _revoke_last_warns(
    session, user_id: int, chat_id: int, count: int, revoked_by_mod_id: int,
) -> int:
    """Помечает последние N активных варнов как снятые.

    Возвращает количество фактически снятых варнов (может быть меньше count,
    если активных варнов меньше запрошенного).
    v4.5.1: «погашенные» авто-действиями варны (consumed_by_action IS NOT NULL)
    тоже снимаются — иначе !unwarn после автомьюта ничего не снимет.
    """
    stmt = (
        select(Punishment)
        .where(
            Punishment.user_id == user_id,
            Punishment.chat_id == chat_id,
            Punishment.action_type == "warn",
            Punishment.is_revoked.is_(False),
        )
        .order_by(desc(Punishment.created_at))
        .limit(count)
    )
    result = await session.execute(stmt)
    warns_to_revoke = result.scalars().all()
    now = datetime.now(timezone.utc)
    revoked_count = 0
    for p in warns_to_revoke:
        p.is_revoked = True
        p.revoked_at = now
        p.revoked_by_mod_id = revoked_by_mod_id
        revoked_count += 1
    if revoked_count:
        await session.commit()
    return revoked_count


async def _mark_warns_consumed(
    session, user_id: int, chat_id: int, action: str,
) -> int:
    """v4.5.1: помечает все активные варны юзера в чате как «погашенные»
    авто-действием (auto_mute / auto_ban).

    Не трогает is_revoked — варны остаются видны в логе как активные,
    но _count_warns их больше не считает. Это исправляет баг, когда
    после первого автомьюта каждый следующий !warn снова триггерил мьют
    (total_warns продолжал расти и всегда был >= warns_to_mute).

    Возвращает количество помеченных варнов.
    """
    stmt = (
        select(Punishment)
        .where(
            Punishment.user_id == user_id,
            Punishment.chat_id == chat_id,
            Punishment.action_type == "warn",
            Punishment.is_revoked.is_(False),
            Punishment.consumed_by_action.is_(None),
        )
    )
    result = await session.execute(stmt)
    warns = result.scalars().all()
    for p in warns:
        p.consumed_by_action = action
    if warns:
        await session.commit()
    return len(warns)


async def _revoke_last_action(
    session, user_id: int, chat_id: int, action_type: str,
    revoked_by_mod_id: int,
) -> bool:
    """Помечает последнюю активную санкцию (mute/ban) как снятую.

    Возвращает True если запись найдена и помечена, иначе False.
    """
    stmt = (
        select(Punishment)
        .where(
            Punishment.user_id == user_id,
            Punishment.chat_id == chat_id,
            Punishment.action_type == action_type,
            Punishment.is_revoked.is_(False),
        )
        .order_by(desc(Punishment.created_at))
        .limit(1)
    )
    result = await session.execute(stmt)
    punishment = result.scalar_one_or_none()
    if punishment is None:
        return False
    punishment.is_revoked = True
    punishment.revoked_at = datetime.now(timezone.utc)
    punishment.revoked_by_mod_id = revoked_by_mod_id
    await session.commit()
    return True


# ── v4.8.4: Прогрессивные автомьюты — хелперы для счётчика ──────────────────
# Счётчик automute_counters хранит количество автомьютов per (chat_id, user_id).
# Формула: mute_duration = base_duration + (count * 60 сек), где count —
# значение ДО инкремента (0 для первого мута). Не сбрасывается при !resetwarns
# или !unmute — только через !resetmc или веб-панель.

async def _get_automute_count(session, chat_id: int, user_id: int) -> int:
    """Возвращает текущее значение счётчика автомьютов (0 если записи нет)."""
    counter = (await session.execute(
        select(AutomuteCounter).where(
            AutomuteCounter.chat_id == chat_id,
            AutomuteCounter.user_id == user_id,
        )
    )).scalar_one_or_none()
    return counter.count if counter else 0


async def _increment_automute_count(session, chat_id: int, user_id: int) -> int:
    """Инкрементирует счётчик автомьютов. Возвращает НОВОЕ значение.

    Создаёт запись если её не было (0 → 1). Вызывающий код должен
    сделать commit (или коммитит в рамках своей транзакции).
    """
    counter = (await session.execute(
        select(AutomuteCounter).where(
            AutomuteCounter.chat_id == chat_id,
            AutomuteCounter.user_id == user_id,
        )
    )).scalar_one_or_none()
    if counter is None:
        counter = AutomuteCounter(chat_id=chat_id, user_id=user_id, count=1)
        session.add(counter)
    else:
        counter.count += 1
    counter.updated_at = datetime.now(timezone.utc)
    await session.flush()
    return counter.count


async def _reset_automute_count(session, chat_id: int, user_id: int) -> int:
    """Сбрасывает счётчик автомьютов в 0. Возвращает СТАРОЕ значение.

    Если записи не было — возвращает 0 (нечего сбрасывать).
    """
    counter = (await session.execute(
        select(AutomuteCounter).where(
            AutomuteCounter.chat_id == chat_id,
            AutomuteCounter.user_id == user_id,
        )
    )).scalar_one_or_none()
    if counter is None:
        return 0
    old_count = counter.count
    counter.count = 0
    counter.updated_at = datetime.now(timezone.utc)
    await session.flush()
    return old_count


# ═══════════════════════════════════════════════════════════════════════════
# v4.5.2 — Хелперы для новых функций: CAS, word filter, link filter, stickers
# ═══════════════════════════════════════════════════════════════════════════

# ── CAS (Combot Anti-Spam) integration (#2) ────────────────────────────────
# Документация: https://api.cas.chat/
# Запрос: GET https://api.cas.chat/v1/status?user_id=<id>
# Ответ: {"ok": true/false, "result": {...}} — ok=true если юзер в CAS-базе.
# Таймаут 3 сек — если CAS лежит, не блокируем вход.
_CAS_API_URL = "https://api.cas.chat/v1/status"
_CAS_TIMEOUT = aiohttp.ClientTimeout(total=3.0)


async def _cas_check_user(user_id: int) -> tuple[bool, str | None]:
    """Проверяет юзера в CAS (Combot Anti-Spam).

    Возвращает кортеж (is_banned, reason).
      • is_banned=True если юзер в CAS-базе (он спамер).
      • reason — строка с причиной бана из CAS (или None).
    На любой сетевой ошибке / таймауте возвращает (False, None) — fail-open:
    лучше пропустить потенциального спамера, чем заблокировать вход при сбое CAS.
    """
    try:
        async with aiohttp.ClientSession(timeout=_CAS_TIMEOUT) as http:
            async with http.get(_CAS_API_URL, params={"user_id": str(user_id)}) as resp:
                if resp.status != 200:
                    return (False, None)
                data = await resp.json(content_type=None)
                if not isinstance(data, dict):
                    return (False, None)
                if data.get("ok") is True:
                    result = data.get("result") or {}
                    reason = (
                        result.get("reason") or "CAS ban (Combot Anti-Spam)"
                    )
                    return (True, str(reason))
                return (False, None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
        logger.warning("CAS check failed for user_id=%s: %s (fail-open)", user_id, e)
        return (False, None)


# ── Word filter (#7) ────────────────────────────────────────────────────────
# v4.8.6: WordFilter остаётся активной моделью — управляется через web UI
# /admin/presets (раздел «Запрещённые слова»). Bot-команды /addword /delword
# /listwords удалены окончательно. KeywordWatch (см. ниже) — отдельная
# система для night-mode автобана, работает через !addkeyword/!listkeywords.


# ── Link filter (#8) ────────────────────────────────────────────────────────
# Регэксп для извлечения URL из текста — простая эвристика.
# Ловит: http(s)://... , www.domain.tld, domain.tld/path (если в tld есть точка).
# НЕ ловит email (намеренно — email отдельно не фильтруем).
_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?([a-z0-9\-]+(?:\.[a-z0-9\-]+)+)(?:/[^\s]*)?",
    re.IGNORECASE,
)


def _extract_urls(text: str) -> list[str]:
    """Извлекает список доменов из текста (без scheme, без path)."""
    if not text:
        return []
    found: list[str] = []
    for m in _URL_RE.finditer(text):
        domain = m.group(1).lower()
        # отсекаем слишком короткие (типа a.b) и слишком длинные
        if 4 <= len(domain) <= 253:
            found.append(domain)
    return found


async def _link_filter_check(
    session, chat_id: int, text: str,
) -> tuple[bool, list[str]]:
    """Проверяет, есть ли в тексте запрещённые ссылки.

    Возвращает (has_blocked, blocked_domains).
    Разрешены домены из allowlist (global chat_id=0 + per-chat chat_id=<id>).
    Сравнение по подстроке: allowlist 't.me' разрешит 't.me', 'telegram.t.me' и т.п.
    """
    if not text:
        return (False, [])
    urls = _extract_urls(text)
    if not urls:
        return (False, [])

    # Загружаем allowlist (global + per-chat)
    stmt = (
        select(LinkAllowlist.domain)
        .where(LinkAllowlist.chat_id.in_([0, chat_id]))
    )
    allowed_rows = (await session.execute(stmt)).scalars().all()
    allowed_lower = [d.lower() for d in allowed_rows]

    blocked: list[str] = []
    for domain in urls:
        is_allowed = any(
            domain == a or domain.endswith("." + a) for a in allowed_lower
        )
        if not is_allowed:
            blocked.append(domain)
    return (bool(blocked), blocked)


# ── Banned sticker packs (#15) ─────────────────────────────────────────────
async def _check_banned_sticker(
    session, chat_id: int, pack_name: str,
) -> BannedStickerPack | None:
    """Возвращает активный BannedStickerPack для пака в чате, либо None.

    Проверяет per-chat (chat_id=<id>) и global (chat_id=0). Per-chat имеет приоритет.
    """
    if not pack_name:
        return None
    # per-chat (chat_id != 0) имеет приоритет над global (chat_id=0).
    # Используем CASE для сортировки: per-chat=0, global=1.
    from sqlalchemy import case
    stmt = (
        select(BannedStickerPack)
        .where(
            BannedStickerPack.chat_id.in_([0, chat_id]),
            BannedStickerPack.pack_name == pack_name,
            BannedStickerPack.is_active.is_(True),
        )
        .order_by(
            case((BannedStickerPack.chat_id == 0, 1), else_=0),  # per-chat first
            BannedStickerPack.created_at.desc(),
        )
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def _add_banned_sticker_pack(
    session,
    chat_id: int,
    pack_name: str,
    punishment: str = "delete",
    mute_duration: int | None = None,
    reason: str | None = None,
    added_by_mod_id: int | None = None,
    added_via: str = "manual",
) -> BannedStickerPack:
    """Добавляет стикерпак в бан-лист. Если уже есть (active) — обновляет punishment.

    Возвращает сохранённый объект BannedStickerPack.
    """
    existing = (
        await session.execute(
            select(BannedStickerPack).where(
                BannedStickerPack.chat_id == chat_id,
                BannedStickerPack.pack_name == pack_name,
                BannedStickerPack.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if existing:
        # Обновляем punishment и причину
        existing.punishment = punishment
        existing.mute_duration = mute_duration
        if reason:
            existing.reason = reason
        if added_by_mod_id is not None:
            existing.added_by_mod_id = added_by_mod_id
        existing.added_via = added_via
        await session.commit()
        return existing
    pack = BannedStickerPack(
        chat_id=chat_id,
        pack_name=pack_name,
        punishment=punishment,
        mute_duration=mute_duration,
        reason=reason,
        added_by_mod_id=added_by_mod_id,
        added_via=added_via,
    )
    session.add(pack)
    await session.commit()
    return pack


def _parse_sticker_pack_link(text: str) -> str | None:
    """Извлекает pack_name из ссылки или возвращает text как есть.

    Поддерживаемые форматы:
      • https://t.me/addstickers/<pack_name>
      • https://t.me/addstickers/<pack_name>?...
      • <pack_name> (просто имя — проходит как есть)
    Возвращает pack_name (lowercased) или None если не парсится.
    """
    text = text.strip()
    if not text:
        return None
    # Если это ссылка
    if "://" in text or text.startswith("t.me/"):
        try:
            parsed = urlparse(text if "://" in text else "https://" + text)
            if parsed.path.startswith("/addstickers/"):
                pack_name = parsed.path[len("/addstickers/"):].split("/")[0]
                if pack_name:
                    return pack_name
            return None
        except ValueError:
            return None
    # Иначе — считаем что это pack_name (проверим что нет пробелов и слешей)
    if "/" in text or " " in text:
        return None
    return text


# ── Night mode permissions presets (#29-33 user-requested) ────────────────
def _night_mode_permissions_preset(preset: str) -> types.ChatPermissions:
    """Возвращает ChatPermissions для пресета ночного режима.

    • 'strict'    — полный мьют (никто ничего не может писать).
    • 'text_only' — только текстовые сообщения (без медиа, стикеров и т.д.).
                    Это дефолтный пресет — ночью обычно хотят тишину по медиа,
                    но оставить возможность написать.
    • 'none'      — никаких ограничений (ночной режим не делает ничего).
                    Полезно если хочешь просто логировать время.
    • любой другой — эквивалент text_only (safe default).
    """
    if preset == "strict":
        return _mute_permissions()
    if preset == "none":
        return types.ChatPermissions(
            can_send_messages=True,
            can_send_audios=True,
            can_send_documents=True,
            can_send_photos=True,
            can_send_videos=True,
            can_send_video_notes=True,
            can_send_voice_notes=True,
            can_send_polls=True,
            can_send_other_messages=True,
            can_add_web_page_previews=True,
            can_change_info=True,
            can_invite_users=True,
            can_pin_messages=True,
        )
    # 'text_only' или неизвестный — безопасный дефолт
    return types.ChatPermissions(
        can_send_messages=True,
        can_send_audios=False,
        can_send_documents=False,
        can_send_photos=False,
        can_send_videos=False,
        can_send_video_notes=False,
        can_send_voice_notes=False,
        can_send_polls=False,
        can_send_other_messages=False,  # стикеры, GIF, кружочки
        can_add_web_page_previews=False,
        can_change_info=False,
        can_invite_users=False,
        can_pin_messages=False,
    )


def _parse_night_mode_permissions(json_str: str | None) -> types.ChatPermissions:
    """Десериализует JSON-снапшот ChatPermissions (из night_mode_permissions)."""
    if not json_str:
        # Дефолт — text_only
        return _night_mode_permissions_preset("text_only")
    try:
        data = json.loads(json_str)
        return types.ChatPermissions(**{k: data.get(k, False) for k in _PERM_FIELDS})
    except (ValueError, TypeError):
        return _night_mode_permissions_preset("text_only")


def _time_str_in_range(now: datetime, start: str, end: str, tz_name: str | None = None) -> bool:
    """Проверяет, находится ли текущее время (в заданной зоне) в диапазоне [start, end).

    start/end в формате 'HH:MM'. Если end <= start — диапазон пересекает полночь
    (например 23:00 → 07:00 = с 23:00 до 07:00 следующего дня).

    v4.5.3: tz_name — IANA timezone (Europe/Moscow, Asia/Yekaterinburg, ...).
    Если None или некорректна — fallback на MSK (Europe/Moscow).
    """
    def _parse_hhmm(s: str) -> tuple[int, int]:
        parts = s.split(":")
        if len(parts) != 2:
            return (0, 0)
        try:
            return (int(parts[0]) % 24, int(parts[1]) % 60)
        except ValueError:
            return (0, 0)

    # v4.5.3: выбор часового пояса. zoneinfo доступен в Python 3.9+.
    tz = MSK
    if tz_name:
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(tz_name)
        except (ValueError, KeyError, ImportError):
            # Некорректная зона — fallback на MSK.
            tz = MSK

    now_local = now.astimezone(tz)
    now_min = now_local.hour * 60 + now_local.minute
    sh, sm = _parse_hhmm(start)
    eh, em = _parse_hhmm(end)
    start_min = sh * 60 + sm
    end_min = eh * 60 + em
    if end_min > start_min:
        return start_min <= now_min < end_min
    else:
        # Пересекает полночь: активен если now >= start ИЛИ now < end
        return now_min >= start_min or now_min < end_min


def _night_mode_in_window(
    now: datetime,
    weekday_start: str,
    weekday_end: str,
    weekend_start: str | None,
    weekend_end: str | None,
    tz_name: str | None = None,
) -> bool:
    """v4.5.3: Проверяет, находится ли текущее время в окне ночного режима.

    Учитывает отдельное расписание на сб/вс если оно задано (не None).
    Если weekend_start/end = None — используется будничное расписание каждый день.

    Суббота и воскресенье трактуются как "выходные" (ISO weekday 6 и 7).
    Это покрывает большинство русскоязычных чатов; если нужна другая логика
    (например, пятница как выходной) — это можно вынести в настройки в будущих
    версиях.
    """
    # Выбор tz
    tz = MSK
    if tz_name:
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(tz_name)
        except (ValueError, KeyError, ImportError):
            tz = MSK
    now_local = now.astimezone(tz)
    is_weekend = now_local.weekday() >= 5  # 5 = Sat, 6 = Sun

    if is_weekend and weekend_start and weekend_end:
        return _time_str_in_range(now, weekend_start, weekend_end, tz_name=tz_name)
    return _time_str_in_range(now, weekday_start, weekday_end, tz_name=tz_name)


# v4.5.3: Карта коротких алиасов прав (для /nightmode custom) → полных имён.
_NIGHT_PERM_ALIASES: dict[str, str] = {
    "msgs": "can_send_messages",
    "audios": "can_send_audios",
    "docs": "can_send_documents",
    "photos": "can_send_photos",
    "videos": "can_send_videos",
    "vnotes": "can_send_video_notes",
    "voices": "can_send_voice_notes",
    "polls": "can_send_polls",
    "other": "can_send_other_messages",
    "links": "can_add_web_page_previews",
}


def _build_custom_night_permissions(
    base_preset: str,
    overrides: dict[str, bool],
) -> types.ChatPermissions:
    """v4.5.3: строит ChatPermissions из базового preset + точечных override'ов.

    base_preset: 'strict' | 'text_only' | 'none' — стартовая точка.
    overrides: dict[alias_or_full_name, bool] — точечные изменения.
       Алиасы: msgs, audios, docs, photos, videos, vnotes, voices, polls, other, links.
       Полные имена тоже принимаются (can_send_messages и т.д.).
    """
    perms = _night_mode_permissions_preset(base_preset)
    for key, value in overrides.items():
        full_name = _NIGHT_PERM_ALIASES.get(key, key)
        if hasattr(perms, full_name):
            setattr(perms, full_name, bool(value))
    return perms


# ── v4.5.4: Санитарные дни (mute all non-moderators) ────────────────────────

# Регэксп для парсинга даты "YYYY-MM-DD". Валидацию диапазона (день 1-31,
# месяц 1-12) делаем в _parse_sanitary_date; regex ловит только формат.
import re as _san_re

_SAN_DATE_RE = _san_re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_SAN_TIME_RE = _san_re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
# v4.7.11: 'YYYY-MM-DD' или 'YYYY-MM-DD HH:MM' — одна часть диапазона с
# опциональным временем. Используется parse_sanitary_days_textarea для
# round-trip с format_sanitary_days_textarea.
_SAN_DT_PART_RE = _san_re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})(?:\s+([01]?\d|2[0-3]):([0-5]\d))?$"
)


def _parse_sanitary_date(s: str) -> date | None:
    """Парсит 'YYYY-MM-DD' в date. Возвращает None при невалидной дате."""
    s = (s or "").strip()
    m = _SAN_DATE_RE.match(s)
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def _parse_sanitary_dt_part(s: str) -> tuple[date | None, str | None]:
    """v4.7.11: парсит часть диапазона — 'YYYY-MM-DD' или 'YYYY-MM-DD HH:MM'.

    Возвращает (date, time_str|None). Если строка невалидна — (None, None).
    Нормализует '9:00' → '09:00'.
    """
    s = (s or "").strip()
    m = _SAN_DT_PART_RE.match(s)
    if not m:
        return None, None
    try:
        d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None, None
    t = None
    if m.group(4):
        t = f"{int(m.group(4)):02d}:{int(m.group(5)):02d}"
    return d, t


def _parse_sanitary_time(s: str | None) -> str | None:
    """v4.7.6: парсит 'HH:MM' (24-часовой формат) → нормализованная строка 'HH:MM'.

    Возвращает None если невалидно или пусто. Нормализует '9:00' → '09:00'.
    """
    if not s:
        return None
    s = str(s).strip()
    m = _SAN_TIME_RE.match(s)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    return f"{h:02d}:{mi:02d}"


def parse_sanitary_days_json(json_str: str | None) -> list[list[str]]:
    """v4.5.4 + v4.6.0 + v4.7.6: парсит JSON sanitary_days в list периодов.

    Каждый период = [start_iso, end_iso, start_hhmm?, end_hhmm?].
    v4.7.6: добавлены опциональные поля start_time и end_time (HH:MM).
    Если время не задано — период считается full-day (старое поведение).

    v4.6.0: поддерживает 2 формата хранения:
      1. Старый (плоский массив пар): [["2026-08-01","2026-08-01"], ...]
      2. Новый (monthly): {"2026-08": [["2026-08-01","2026-08-01"]], "2026-09": []}

    Если данные — dict (новый формат) — берём ВСЕ пары из всех месяцев.
    Если данные — list (старый формат) — берём как есть (обратная совместимость).

    Невалидные записи пропускаются. Возвращает [] для пустого/битого JSON.
    """
    if not json_str:
        return []
    try:
        data = json.loads(json_str)
    except (ValueError, TypeError):
        return []

    # v4.6.0: новый формат — dict по месяцам.
    if isinstance(data, dict):
        out: list[list[str]] = []
        for month_key, entries in data.items():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                p = _normalize_sanitary_entry(entry)
                if p is not None:
                    out.append(p)
        return out

    # Старый формат — плоский list.
    if not isinstance(data, list):
        return []
    out: list[list[str]] = []
    for entry in data:
        p = _normalize_sanitary_entry(entry)
        if p is not None:
            out.append(p)
    return out


def _normalize_sanitary_entry(entry) -> list[str] | None:
    """v4.7.6: нормализует одну запись sanitary-периода.

    Поддерживаемые форматы:
      • [start, end]                  — full-day (старый)
      • [start, end, start_time]      — start в HH:MM, end — full-day
      • [start, end, start_time, end_time] — start_time .. end_time
      • [start, end, null, null]      — null = full-day (нет времени)

    Возвращает [start_iso, end_iso, start_hhmm?, end_hhmm?] или None.
    Поле со значением None/пусто — опускается.
    """
    if not isinstance(entry, (list, tuple)) or len(entry) < 2:
        return None
    s, e = entry[0], entry[1]
    if not isinstance(s, str) or not isinstance(e, str):
        return None
    ds = _parse_sanitary_date(s)
    de = _parse_sanitary_date(e)
    if ds is None or de is None:
        return None
    if de < ds:
        de = ds
    out: list[str] = [ds.isoformat(), de.isoformat()]
    # v4.7.6: опциональные поля времени.
    if len(entry) >= 3:
        st = _parse_sanitary_time(entry[2] if not isinstance(entry[2], (list, tuple)) else None) if entry[2] else None
        if st:
            out.append(st)
    if len(entry) >= 4:
        et = _parse_sanitary_time(entry[3] if not isinstance(entry[3], (list, tuple)) else None) if entry[3] else None
        if et:
            out.append(et)
        elif len(out) == 3:
            # start_time задан, end_time — нет. Оставляем как есть (3 поля).
            pass
    return out


def parse_sanitary_days_monthly(
    json_str: str | None,
    month_key: str | None = None,
) -> dict[str, list[list[str]]]:
    """v4.6.0 + v4.7.6: парсит JSON sanitary_days в dict по месяцам.

    Возвращает dict {"YYYY-MM": [[start_iso, end_iso, start_hhmm?, end_hhmm?], ...], ...}.

    v4.7.6: каждое вложенное tuple может содержать опциональные start_time/end_time.

    Если month_key задан — возвращает dict только с этим месяцем
    (пустой список если месяца нет в данных).

    Старый формат (плоский массив) автоматически конвертируется:
    пары группируются по месяцу даты начала.
    """
    if not json_str:
        return {} if month_key is None else {month_key: []}
    try:
        data = json.loads(json_str)
    except (ValueError, TypeError):
        return {} if month_key is None else {month_key: []}

    if isinstance(data, dict):
        result: dict[str, list[list[str]]] = {}
        for mk, entries in data.items():
            if not isinstance(entries, list):
                continue
            month_pairs: list[list[str]] = []
            for entry in entries:
                p = _normalize_sanitary_entry(entry)
                if p is not None:
                    month_pairs.append(p)
            result[mk] = month_pairs
        if month_key is not None:
            return {month_key: result.get(month_key, [])}
        return result

    if isinstance(data, list):
        grouped: dict[str, list[list[str]]] = {}
        for entry in data:
            p = _normalize_sanitary_entry(entry)
            if p is None:
                continue
            ds = _parse_sanitary_date(p[0])
            mk = ds.strftime("%Y-%m")
            grouped.setdefault(mk, []).append(p)
        if month_key is not None:
            return {month_key: grouped.get(month_key, [])}
        return grouped

    return {} if month_key is None else {month_key: []}


def serialize_sanitary_days(pairs: list[list[str]]) -> str:
    """v4.5.4: сериализует list пар [start_iso, end_iso] в JSON-строку.

    v4.6.0: эта функция сохранена для обратной совместимости, но новые записи
    рекомендуется хранить через serialize_sanitary_days_monthly (dict по месяцам).

    Каждая пара должна быть [start, end] ISO-строками; имена валидируются
    через _parse_sanitary_date (невалидные пропускаются).
    """
    norm: list[list[str]] = []
    for p in pairs:
        if not isinstance(p, (list, tuple)) or len(p) != 2:
            continue
        ds = _parse_sanitary_date(str(p[0]))
        de = _parse_sanitary_date(str(p[1]))
        if ds is None or de is None:
            continue
        if de < ds:
            de = ds
        norm.append([ds.isoformat(), de.isoformat()])
    return json.dumps(norm)


def serialize_sanitary_days_monthly(
    monthly: dict[str, list[list[str]]],
) -> str:
    """v4.6.0 + v4.7.6: сериализует dict по месяцам в JSON-строку.

    Каждая пара валидируется и нормализуется. Пустые значения и пустые dict
    → пустая строка "[]" (не None, чтобы UI отличил «нет настроек» от «пусто»).

    v4.7.6: поддерживает опциональные поля времени (start_time, end_time).
    Записи могут быть длиной 2 (без времени) или 3-4 (с временем).

    Формат: {"2026-08": [["2026-08-02","2026-08-03","23:00","09:00"]], "2026-09": []}
    """
    if not monthly:
        return "[]"
    out: dict[str, list[list[str]]] = {}
    for mk, pairs in monthly.items():
        if not isinstance(pairs, list):
            continue
        norm: list[list[str]] = []
        for p in pairs:
            normalized = _normalize_sanitary_entry(p)
            if normalized is None:
                continue
            norm.append(normalized)
        out[mk] = norm
    return json.dumps(out)


def is_sanitary_day_today(
    pairs: list[list[str]] | str | None,
    today: date | None = None,
    now_dt: datetime | None = None,
) -> bool:
    """v4.5.4 + v4.7.6: проверяет, попадает ли today/now в один из диапазонов.

    v4.7.6: если в периоде задано время — используется now_dt (datetime)
    для datetime-сравнения. Если now_dt не передан — берётся текущий момент в UTC.
    Если время НЕ задано — старая логика по date (inclusive [start, end]).

    Принимает как уже распарсенный list периодов, так и сырую JSON-строку.
    """
    if today is None:
        today = datetime.now(timezone.utc).date()
    if isinstance(pairs, str):
        pairs = parse_sanitary_days_json(pairs)
    if not pairs:
        return False
    for entry in pairs:
        if len(entry) >= 3:
            # Период со временем — datetime-проверка.
            if now_dt is None:
                now_dt = datetime.now(timezone.utc)
            if is_sanitary_active_now_at(entry, now_dt):
                return True
            continue
        # Период без времени — date-проверка.
        s, e = entry[0], entry[1]
        ds = _parse_sanitary_date(s)
        de = _parse_sanitary_date(e)
        if ds is None or de is None:
            continue
        if ds <= today <= de:
            return True
    return False


def is_sanitary_active_now_at(
    entry: list[str],
    now_dt: datetime,
) -> bool:
    """v4.7.6: проверяет, попадает ли datetime в один период со временем.

    entry имеет формат [start_iso, end_iso, start_hhmm?, end_hhmm?].
    Логика:
      • start_dt = start_date + start_time (или 00:00 если start_time не задан).
      • end_dt   = end_date + end_time (или 23:59:59 если end_time не задан).
      • True если start_dt <= now_dt <= end_dt.

    now_dt интерпретируется как локальное время в TZ чата (caller передаёт
    правильное значение — обычно now в night_mode_tz).
    """
    if not entry or len(entry) < 2:
        return False
    ds = _parse_sanitary_date(entry[0])
    de = _parse_sanitary_date(entry[1])
    if ds is None or de is None:
        return False
    start_time = entry[2] if len(entry) >= 3 else None
    end_time = entry[3] if len(entry) >= 4 else None

    sh, sm = (0, 0)
    if start_time:
        parsed = _parse_sanitary_time(start_time)
        if parsed:
            sh, sm = int(parsed[:2]), int(parsed[3:5])
    eh, em = (23, 59)
    if end_time:
        parsed = _parse_sanitary_time(end_time)
        if parsed:
            eh, em = int(parsed[:2]), int(parsed[3:5])

    # now_dt может быть naive (считается уже в TZ чата) или aware (UTC).
    # Нам нужна только date+time, без TZ-конвертации здесь.
    nd = now_dt.replace(tzinfo=None) if now_dt.tzinfo else now_dt

    start_dt = datetime(ds.year, ds.month, ds.day, sh, sm, 0)
    end_dt = datetime(de.year, de.month, de.day, eh, em, 59)
    return start_dt <= nd <= end_dt


def parse_sanitary_days_textarea(
    text: str,
) -> tuple[list[list[str]], list[str]]:
    """v4.5.4 + v4.7.11: парсит textarea (одна запись на строку) в list пар.

    Принимает строки вида (v4.7.11 добавлена поддержка времени):
      'YYYY-MM-DD'                              — однодневный сан. день
      'YYYY-MM-DD HH:MM'                        — однодневный с start_time
      'YYYY-MM-DD HH:MM-HH:MM'                  — однодневный с диапазоном времени
      'YYYY-MM-DD:YYYY-MM-DD'                   — диапазон (включая обе даты)
      'YYYY-MM-DD - YYYY-MM-DD'                 — диапазон с пробелами вокруг '-'
      'YYYY-MM-DD HH:MM - YYYY-MM-DD HH:MM'     — диапазон со временем с обеих сторон
      'YYYY-MM-DD HH:MM - YYYY-MM-DD'           — диапазон с start_time только
      'YYYY-MM-DD - YYYY-MM-DD HH:MM'           — диапазон с end_time только

    Это обратимо к format_sanitary_days_textarea — то, что мы записываем в
    textarea при показе существующих данных, мы обязаны уметь читать обратно.

    Возвращает (pairs, errors). Каждая пара — list[str] длиной 2/3/4:
      [start_iso, end_iso]                       — без времени
      [start_iso, end_iso, start_time]           — только start_time
      [start_iso, end_iso, start_time, end_time] — оба времени
    errors — список строк с описанием проблем (используется для feedback).
    """
    pairs: list[list[str]] = []
    errors: list[str] = []
    text = (text or "").strip()
    if not text:
        return pairs, errors
    for i, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        # Поддерживаем разделители ':' и ' - ' и ' to '.
        # v4.7.11: проверяем ' - ' ПЕРВЫМ, т.к. он встречается в формате
        # 'YYYY-MM-DD HH:MM - YYYY-MM-DD HH:MM' и отделяет даты, а не время.
        # Внутренний '-' между временем (HH:MM-HH:MM) не содержит пробелов
        # вокруг, поэтому коллизии с ' - ' нет.
        # v4.7.11: ':' как разделитель диапазона конфликтует с ':' внутри
        # времени HH:MM (например '2026-07-31 23:00'). Используем ':' только
        # если левая часть — это валидная дата YYYY-MM-DD без времени.
        sep = None
        for cand in (" - ", " to ", " — ", " – "):
            if cand in line:
                sep = cand
                break
        if sep is None and ":" in line:
            # ':' может быть разделителем диапазона в старом формате
            # 'YYYY-MM-DD:YYYY-MM-DD' — НО не в формате времени 'HH:MM'.
            # Проверяем: берём подстроку до первого ':' — если она матчит
            # как YYYY-MM-DD (без времени), то ':' — разделитель диапазона.
            idx = line.find(":")
            left_of_colon = line[:idx]
            if _SAN_DATE_RE.match(left_of_colon):
                sep = ":"
        if sep:
            parts = line.split(sep, 1)
            ds, st = _parse_sanitary_dt_part(parts[0].strip())
            de, et = _parse_sanitary_dt_part(parts[1].strip())
            if ds is None:
                errors.append(f"Строка {i}: невалидная дата начала '{parts[0].strip()}'")
                continue
            if de is None:
                errors.append(f"Строка {i}: невалидная дата конца '{parts[1].strip()}'")
                continue
            if de < ds:
                de = ds
            entry: list[str] = [ds.isoformat(), de.isoformat()]
            if st and et:
                entry.append(st)
                entry.append(et)
            elif st:
                # Только start_time — без end_time (format_sanitary_days_textarea
                # такое генерирует для случая 'elif st:' ветки).
                entry.append(st)
            elif et:
                # Только end_time — генерируется 'elif et:' веткой. Чтобы
                # не терять данные, сохраняем как [s, e, "00:00", et].
                entry.append("00:00")
                entry.append(et)
            pairs.append(entry)
        else:
            # Single-day: 'YYYY-MM-DD' / 'YYYY-MM-DD HH:MM' / 'YYYY-MM-DD HH:MM-HH:MM'.
            # Сначала проверим формат с диапазоном времени 'HH:MM-HH:MM'.
            m = _san_re.match(
                r"^(\d{4}-\d{2}-\d{2})\s+([01]?\d|2[0-3]):([0-5]\d)-([01]?\d|2[0-3]):([0-5]\d)$",
                line,
            )
            if m:
                d = _parse_sanitary_date(m.group(1))
                if d is None:
                    errors.append(f"Строка {i}: невалидная дата '{line}' (нужен YYYY-MM-DD)")
                    continue
                st = f"{int(m.group(2)):02d}:{int(m.group(3)):02d}"
                et = f"{int(m.group(4)):02d}:{int(m.group(5)):02d}"
                pairs.append([d.isoformat(), d.isoformat(), st, et])
                continue
            # Теперь 'YYYY-MM-DD HH:MM' (только start_time).
            m = _san_re.match(
                r"^(\d{4}-\d{2}-\d{2})\s+([01]?\d|2[0-3]):([0-5]\d)$",
                line,
            )
            if m:
                d = _parse_sanitary_date(m.group(1))
                if d is None:
                    errors.append(f"Строка {i}: невалидная дата '{line}' (нужен YYYY-MM-DD)")
                    continue
                st = f"{int(m.group(2)):02d}:{int(m.group(3)):02d}"
                pairs.append([d.isoformat(), d.isoformat(), st])
                continue
            # Только дата 'YYYY-MM-DD'.
            d = _parse_sanitary_date(line)
            if d is None:
                errors.append(f"Строка {i}: невалидная дата '{line}' (нужен YYYY-MM-DD)")
                continue
            pairs.append([d.isoformat(), d.isoformat()])
    return pairs, errors


def format_sanitary_days_textarea(pairs: list[list[str]] | str | None) -> str:
    """v4.5.4 + v4.6.0 + v4.7.6: форматирование списка пар в textarea-строки.

    v4.7.6: если в периоде есть время — оно добавляется к дате.
    Однодневные пары (start == end) без времени выводятся одной датой.
    Многодневные — через ' - '.

    Используется в <details> Raw JSON (advanced) для совместимости с bot-командами.
    """
    if isinstance(pairs, str):
        pairs = parse_sanitary_days_json(pairs)
    if not pairs:
        return ""
    lines: list[str] = []
    for entry in pairs:
        s = entry[0]
        e = entry[1]
        st = entry[2] if len(entry) >= 3 else None
        et = entry[3] if len(entry) >= 4 else None
        # С временем.
        if st and et:
            if s == e:
                lines.append(f"{s} {st}-{et}")
            else:
                lines.append(f"{s} {st} - {e} {et}")
        elif st:
            if s == e:
                lines.append(f"{s} {st}")
            else:
                lines.append(f"{s} {st} - {e}")
        elif et:
            if s == e:
                lines.append(f"{s} - {et}")
            else:
                lines.append(f"{s} - {e} {et}")
        else:
            # Без времени — старое поведение.
            if s == e:
                lines.append(s)
            else:
                lines.append(f"{s} - {e}")
    return "\n".join(lines)


def format_sanitary_period_human(entry: list[str]) -> str:
    """v4.7.6: форматирует один период для UI-списка назначенных периодов.

    Возвращает строку вида:
      • '31.07.2026 23:00 → 03.08.2026 09:00'  (с временем)
      • '2026-08-01'                             (однодневный без времени)
      • '2026-08-01 - 2026-08-03'                (диапазон без времени)
    """
    if not entry or len(entry) < 2:
        return ""
    s_iso = entry[0]
    e_iso = entry[1]
    start_time = entry[2] if len(entry) >= 3 else None
    end_time = entry[3] if len(entry) >= 4 else None

    def _fmt_date_ru(iso: str) -> str:
        d = _parse_sanitary_date(iso)
        if d is None:
            return iso
        return f"{d.day:02d}.{d.month:02d}.{d.year:04d}"

    s_disp = _fmt_date_ru(s_iso)
    e_disp = _fmt_date_ru(e_iso)

    if start_time and end_time:
        return f"{s_disp} {start_time} → {e_disp} {end_time}"
    if start_time and not end_time:
        return f"{s_disp} {start_time} → {e_disp}"
    if not start_time and end_time:
        return f"{s_disp} → {e_disp} {end_time}"
    # Без времени.
    if s_iso == e_iso:
        return s_iso
    return f"{s_iso} - {e_iso}"


def add_sanitary_period(
    json_str: str | None,
    start_date: str,
    end_date: str,
    start_time: str | None = None,
    end_time: str | None = None,
) -> tuple[str | None, str | None]:
    """v4.7.6: добавляет период в sanitary_days JSON.

    Парсит текущий JSON в monthly-формат, добавляет новую запись в нужный месяц
    (по start_date), сериализует обратно.

    Возвращает (new_json, error). При ошибке валидации — (None, 'сообщение').
    """
    ds = _parse_sanitary_date(start_date)
    de = _parse_sanitary_date(end_date)
    if ds is None:
        return None, f"Invalid start date '{start_date}' (use YYYY-MM-DD)"
    if de is None:
        return None, f"Invalid end date '{end_date}' (use YYYY-MM-DD)"
    if de < ds:
        de = ds

    st = _parse_sanitary_time(start_time) if start_time else None
    et = _parse_sanitary_time(end_time) if end_time else None

    monthly = parse_sanitary_days_monthly(json_str)
    mk = ds.strftime("%Y-%m")
    if mk not in monthly:
        monthly[mk] = []
    entry: list[str] = [ds.isoformat(), de.isoformat()]
    if st:
        entry.append(st)
    if et:
        entry.append(et)
    monthly[mk].append(entry)
    return serialize_sanitary_days_monthly(monthly), None


def delete_sanitary_period(
    json_str: str | None,
    index: int,
) -> tuple[str | None, str | None]:
    """v4.7.6: удаляет период из sanitary_days JSON по глобальному индексу.

    Глобальный индекс = позиция периода в плоском list от parse_sanitary_days_json
    (который итерирует месяцы в порядке их появления в dict).

    Возвращает (new_json, error). При ошибке — (None, 'сообщение').
    """
    pairs = parse_sanitary_days_json(json_str)
    if index < 0 or index >= len(pairs):
        return None, f"Invalid period index {index} (have {len(pairs)} periods)"
    target = pairs[index]

    # Парсим в monthly-формат и находим/удаляем target.
    monthly = parse_sanitary_days_monthly(json_str)
    for mk in list(monthly.keys()):
        entries = monthly[mk]
        for i, e in enumerate(entries):
            if e == target:
                del entries[i]
                # Если месяц пустой — оставляем ключ (для UI: показываем что месяц был).
                # Не удаляем ключ чтобы не сбросить last_sanitary_month маркер.
                monthly[mk] = entries
                return serialize_sanitary_days_monthly(monthly), None
    return None, "Period not found in monthly structure"


def get_sanitary_periods_flat(json_str: str | None) -> list[list[str]]:
    """v4.7.6: возвращает плоский list периодов из JSON (для UI-списка).

    Аналог parse_sanitary_days_json, но гарантированно возвращает
    только валидные нормализованные периоды.
    """
    return parse_sanitary_days_json(json_str)


# ── Отправка отчёта в чат (Rich Messages, Bot API 10.2) ─────────────────────

# Карта типов медиа → фабрика inline-блока для Rich Message.
# Стикеры, документы, кружки (video_note), контакты, локации, опросы —
# НЕ имеют соответствующего RichBlock-типа, поэтому для них inline-блок
# не строится (контент просто показывается текстом в BlockQuotation).
def _build_media_block(msg: types.Message):
    """Возвращает InputRichBlock* для inline-медиа или None.

    Поддерживаются: photo, video, animation, audio, voice.
    Стикеры/документы/кружки — без inline-блока (только текст в blockquote).

    v4.8.3: для стикеров используйте асинхронную _build_sticker_block —
    она скачивает стикер, конвертирует WebP/TGS → PNG, WebM — как есть,
    и возвращает InputRichBlockPhoto / InputRichBlockAnimation.
    """
    try:
        if msg.photo:
            # photo — список PhotoSize, берём последний (самый большой)
            file_id = msg.photo[-1].file_id
            return InputRichBlockPhoto(photo=InputMediaPhoto(media=file_id))
        if msg.video:
            return InputRichBlockVideo(video=InputMediaVideo(media=msg.video.file_id))
        if msg.animation:
            return InputRichBlockAnimation(
                animation=InputMediaAnimation(media=msg.animation.file_id)
            )
        if msg.audio:
            return InputRichBlockAudio(audio=InputMediaAudio(media=msg.audio.file_id))
        if msg.voice:
            return InputRichBlockVoiceNote(
                voice_note=InputMediaVoiceNote(media=msg.voice.file_id)
            )
    except Exception as e:
        logger.warning("Could not build media block: %s", e)
    return None


# ── v4.8.3: стикер inline в Rich Message ──────────────────────────────────
async def _build_sticker_block(
    bot: types.Bot,
    sticker: types.Sticker,
) -> tuple[object | None, str | None]:
    """Скачивает стикер и возвращает inline-блок для Rich Message.

    Returns:
        (block, None) — успех. block: InputRichBlockPhoto (для PNG) или
        InputRichBlockAnimation (для WebM).
        (None, error_message) — неудача. Caller fallback'ит на отдельный
        send_sticker с sticker.file_id.

    Типы стикеров:
      • Static WebP → PNG через Pillow → InputRichBlockPhoto.
      • Video WebM → как есть → InputRichBlockAnimation.
      • Animated TGS → PNG через rlottie (если установлен) → InputRichBlockPhoto.
        Если rlottie нет — (None, error), caller fallback'ит на send_sticker.
    """
    buf, fmt, err = await download_sticker_for_rich_message(bot, sticker)
    if buf is None:
        return None, err or "failed to download sticker"

    try:
        if fmt == "png":
            # PNG-стикер — как photo. BufferedInputFile обернёт bytes.
            # v4.8.6: InputFile стал ABC в aiogram 3.30 — теперь нужен
            # BufferedInputFile (для bytes) или FSInputFile (для пути).
            return InputRichBlockPhoto(
                photo=InputMediaPhoto(media=BufferedInputFile(buf, filename="sticker.png"))
            ), None
        if fmt == "webm":
            # WebM-стикер — как animation.
            return InputRichBlockAnimation(
                animation=InputMediaAnimation(
                    media=BufferedInputFile(buf, filename="sticker.webm")
                )
            ), None
        return None, f"unknown format: {fmt}"
    except Exception as e:
        logger.warning("Could not build sticker rich block: %s", e)
        return None, f"build block failed: {e}"
    finally:
        # BytesIO можно закрыть после того, как InputFile его прочитал.
        # InputFile читает лениво — закрывать сразу нельзя. Но GC закроет.
        # Оставляем как есть — GC разберётся.
        pass


async def _build_screenshot_block(
    bot: types.Bot,
    photo_sizes: list,
) -> tuple[object | None, str | None]:
    """Скачивает largest photo size (скриншот модератора) и возвращает
    InputRichBlockPhoto для Rich Message.

    Returns:
        (block, None) — успех.
        (None, error_message) — неудача.
    """
    buf, err = await download_photo_bytes(bot, photo_sizes)
    if buf is None:
        return None, err or "failed to download photo"
    try:
        return InputRichBlockPhoto(
            photo=InputMediaPhoto(media=BufferedInputFile(buf, filename="screenshot.jpg"))
        ), None
    except Exception as e:
        logger.warning("Could not build screenshot rich block: %s", e)
        return None, f"build block failed: {e}"


async def _get_report_chat_id(session, chat_id: int) -> int | None:
    """Возвращает ID чата для отчётов (v4.4.7).

    Приоритет:
      1. Per-chat override (ChatSettings.report_chat_id для данного chat_id)
      2. Любой чат с пометкой is_report_chat=True (выбирается первый попавшийся)
      3. Глобальный default (ChatSettings.report_chat_id для chat_id=0)
      4. None (отчёты отключены)
    """
    # 1. Per-chat override
    settings = await _get_chat_settings(session, chat_id)
    if settings.report_chat_id is not None and settings.report_chat_id != 0:
        return settings.report_chat_id

    # 2. Любой чат с is_report_chat=True
    rc = (await session.execute(
        select(ChatSettings.chat_id).where(
            ChatSettings.is_report_chat.is_(True),
            ChatSettings.chat_id != 0,
        ).limit(1)
    )).scalars().first()
    if rc is not None:
        return rc

    # 3. Глобальный default (chat_id=0) — legacy
    default_settings = await _get_chat_settings(session, 0)
    if default_settings.report_chat_id is not None and default_settings.report_chat_id != 0:
        return default_settings.report_chat_id

    # 4. Disabled
    return None


async def _send_audit_to_report(
    bot: types.Bot,
    chat_id: int,
    mod: types.User,
    target: types.User,
    action_label: str,
    detail: str = "",
    count: int | None = None,
) -> None:
    """v4.5.1: отправляет краткое audit-сообщение в репорт-чат о снятии санкции.

    Используется для !unwarn / !unban / !unmute / !resetwarns — чтобы в логе
    репорт-чата было видно, кто что снял вручную.

    Формат: «↩️ @moderator снял {action_label} с @target ({detail}) — вручную»

    :param action_label: «варн», «3 варна», «мьют», «бан» и т.д.
    :param detail: необязательная приписка (например, «по истечении срока»)
    :param count: если передан — добавляем «N шт.» к action_label
    """
    try:
        async with async_session() as session:
            report_chat_id = await _get_report_chat_id(session, chat_id)
        if report_chat_id is None:
            return  # некуда слать — тихо выходим

        mod_mention = _user_mention_html(mod)
        target_mention = _user_mention_html(target)
        label = action_label
        if count is not None and count > 0:
            label = f"{count} {action_label}"
        suffix = f" ({detail})" if detail else ""
        text = (
            f"↩️ <b>Снятие санкции</b>\n"
            f"<b>Действие:</b> {html.escape(label, quote=False)}{suffix}\n"
            f"<b>С кого:</b> {target_mention}\n"
            f"<b>Кем:</b> {mod_mention} (вручную)"
        )
        await bot.send_message(
            chat_id=report_chat_id,
            text=text,
            parse_mode="HTML",
        )
    except TelegramAPIError as e:
        logger.info(
            "audit message to report chat %s failed: %s",
            chat_id, e,
        )
    except Exception as e:
        logger.warning("send_audit_to_report unexpected error: %s", e)


async def _send_report(
    bot: types.Bot,
    chat_id: int,
    target: types.User,
    action_type: str,
    reason: str | None,
    mod: types.User | None = None,
    warn_points: int | None = None,
    duration_seconds: int | None = None,
    reply_to_message: types.Message | None = None,
    sticker_pack_info: tuple[str, bool] | None = None,
    moderator_screenshot: types.Message | None = None,
) -> None:
    """Отправляет Rich-отчёт о санкции в репорт-чат (Bot API 10.2).

    Приоритет репорт-чата: per-chat override → default (chat_id=0) → disabled.
    Если репорт-чат не задан — молча ничего не делает.

    v4.4.10 Структура Rich-сообщения (редизайн под мобильный вид):
      1. SectionHeading — 🔇 МУТ / 🚫 БАН / ⚠️ ВАРН / 🔊 РАЗМУТ
      2. Divider        — горизонтальная линия
      3. List           — список ключевых полей (нарушитель/причина/веб-профиль).
                          Каждый пункт — ListItem с Paragraph внутри. Эмодзи-маркеры
                          выровнены нативным списком, URL «Веб-профиль» спрятан под
                          коротким текстом «Открыть профиль →» — больше не ломается.
                          ID нарушителя оформлен как inline-код (моноширинный).
      4. Divider        — разделитель
      5. Details        — «📎 Сообщение юзера» (is_open=False): сворачиваемый блок,
                          куда входит текст сообщения нарушителя (как BlockQuotation)
                          и фото/видео/гиф/стикер из него. По умолчанию скрыт
                          (защита от шок-контента), разворачивается по тапу.
      6. Divider        — разделитель (только если есть скриншот модератора)
      6b. Details       — «📷 Скриншот от модератора» (is_open=False) — фото,
                          приложенное модератором к команде (message.photo + caption).
      7. Divider        — разделитель
      8. Details        — «Доп. инфо» (чат/длительность/варнов всего) — сворачиваемо
      9. Divider        — разделитель
      10. Footer        — время МСК + хэштег чата + кликабельное имя модератора
                          (без приписки «Модератор:», просто имя).

    v4.8.3: изменения:
      • Стикеры теперь inline в Details «📎 Сообщение юзера» (через
        _build_sticker_block: WebP→PNG, WebM, TGS→PNG если rlottie установлен).
        Если конвертация не удалась — fallback: стикер отправляется отдельным
        send_sticker после rich-отчёта (как в v4.8.2).
      • Скриншот от модератора (moderator_screenshot=message, если message.photo
        и message.caption содержит команду) — отдельный Details «📷 Скриншот
        от модератора». BytesIO в памяти, на диск не пишем.
      • Стикерпак-нотификация (sticker_pack_info=(pack_name, was_newly_added)):
        если was_newly_added=True — добавляем в List пункт
        «📦 Использованный стикерпак забанен: <pack_name>».

    Returns: None (медиа теперь inline в Details-блоке rich message).
    """
    # ── Определяем репорт-чат ──────────────────────────────────
    async with async_session() as session:
        report_dest = await _get_report_chat_id(session, chat_id)

    if not report_dest:
        return None

    # ── Хэштег чата + счётчик варнов (один запрос в БД) ────────
    async with async_session() as session:
        settings = await _get_chat_settings(session, chat_id)
        hashtag = settings.hashtag or ""
        total_warns: int | None = None
        if action_type in ("warn", "unwarn") and warn_points is not None:
            total_warns = await _count_warns(session, target.id, chat_id)

    # ── Заголовок действия ─────────────────────────────────────
    action_labels = {
        "mute": "🔇 МУТ",
        "ban": "🚫 БАН",
        "warn": "⚠️ ВАРН",
        "unmute": "🔊 РАЗМУТ",
        "unban": "🎉 РАЗБАН",
        "unwarn": "↩️ СНЯТИЕ ВАРНА",
    }
    action_label = action_labels.get(action_type, action_type.upper())

    # ── Нарушитель: имя кликабельно → tg://user?id=… ───────────
    full_name = (target.first_name or "") + (
        f" {target.last_name}" if target.last_name else ""
    )
    display_name = full_name.strip() or "(без имени)"

    # ── Контент нарушителя ─────────────────────────────────────
    text_content: str | None = None
    media_block = None
    sticker_file_id: str | None = None  # для fallback если inline не сработал
    sticker_inline_ok = False  # True если стикер успешно встроен inline

    if reply_to_message is not None:
        text_content = reply_to_message.text or reply_to_message.caption
        # v4.8.3: для стикеров — отдельная (асинхронная) логика с конвертацией.
        if reply_to_message.sticker is not None:
            sticker = reply_to_message.sticker
            sticker_file_id = sticker.file_id  # сохраняем для fallback'а
            sticker_block, sticker_err = await _build_sticker_block(bot, sticker)
            if sticker_block is not None:
                media_block = sticker_block
                sticker_inline_ok = True
            else:
                # TGS без rlottie или другая ошибка — fallback ниже.
                logger.info(
                    "Sticker inline build failed, will use send_sticker fallback: %s",
                    sticker_err,
                )
        else:
            # Обычное медиа (photo/video/animation/audio/voice) — синхронно.
            media_block = _build_media_block(reply_to_message)
        if media_block is None and text_content is None and not sticker_file_id:
            desc = _get_message_content_desc(reply_to_message)
            if desc:
                text_content = desc

    # ── Список блоков (v4.4.10 редизайн) ───────────────────────
    blocks: list = []
    blocks.append(InputRichBlockSectionHeading(text=action_label, size=2))
    blocks.append(InputRichBlockDivider())

    # ── List: нарушитель / причина / стикерпак / веб-профиль ────
    # Каждый ListItem — отдельный пункт с нативным буллетом. Эмодзи-маркеры
    # выровнены самим Telegram, не «плывут» как в наборе отдельных Paragraph'ов.
    list_items: list[InputRichBlockListItem] = []

    # Пункт 1: нарушитель (имя кликабельно + @username + ID моноширинно)
    offender_item_text: list = [
        "👤 ",
        RichTextUrl(
            text=display_name,
            url=f"tg://user?id={target.id}",
        ),
    ]
    if target.username:
        offender_item_text.append(f"  @{target.username}")
    # ID оформлен как inline-код (моноширинный) — выделяется визуально,
    # легко копируется на мобильном (долгий тап → Copy).
    offender_item_text.append("  ")
    offender_item_text.append(RichTextCode(text=f"ID: {target.id}"))
    list_items.append(
        InputRichBlockListItem(
            blocks=[InputRichBlockParagraph(text=offender_item_text)]
        )
    )

    # Пункт 2: причина (если есть)
    if reason:
        list_items.append(
            InputRichBlockListItem(
                blocks=[InputRichBlockParagraph(text=f"📝 {reason}")]
            )
        )

    # Пункт 3 (v4.8.3): стикерпак забанен (если был newly_added)
    if sticker_pack_info is not None:
        pack_name, was_newly_added = sticker_pack_info
        if was_newly_added:
            list_items.append(
                InputRichBlockListItem(
                    blocks=[InputRichBlockParagraph(
                        text=f"📦 Использованный стикерпак забанен: {pack_name}"
                    )]
                )
            )

    # Пункт 4: веб-профиль — короткий текст вместо длинного URL
    if WEB_PUBLIC_URL:
        web_url = f"{WEB_PUBLIC_URL}/user/{target.id}"
        list_items.append(
            InputRichBlockListItem(
                blocks=[InputRichBlockParagraph(
                    text=[
                        "🌐 ",
                        RichTextUrl(text="Открыть профиль →", url=web_url),
                    ]
                )]
            )
        )

    blocks.append(InputRichBlockList(items=list_items))

    # ── Details: текст+медиа под спойлером ──────────────────────
    # Текст сообщения нарушителя (как BlockQuotation) и все медиа (фото/видео/
    # гиф/стикер) обёрнуты в сворачиваемый Details «📎 Сообщение юзера».
    # По умолчанию is_open=False — модератор не видит содержимое, пока не тапнет
    # по заголовку. Защита от шок-контента, который иначе сразу бросается в
    # глаза при открытии репорт-чата.
    if media_block is not None or text_content:
        media_details_blocks: list = []
        if text_content:
            media_details_blocks.append(
                InputRichBlockBlockQuotation(
                    blocks=[InputRichBlockParagraph(text=text_content)]
                )
            )
        if media_block is not None:
            media_details_blocks.append(media_block)
        # Если стикер не удалось встроить inline — добавляем текст-плейсхолдер.
        if sticker_file_id and not sticker_inline_ok:
            media_details_blocks.append(
                InputRichBlockParagraph(
                    text="📎 Анимированный стикер прикреплён следующим сообщением."
                )
            )
        blocks.append(InputRichBlockDivider())
        blocks.append(
            InputRichBlockDetails(
                summary="📎 Сообщение юзера",
                is_open=False,
                blocks=media_details_blocks,
            )
        )

    # ── v4.8.3: Details «📷 Скриншот от модератора» (опционально) ──
    # Если модератор приложил фото к команде (caption содержит !ban ...) —
    # добавляем отдельный сворачиваемый Details с этим фото. Это НЕ сообщение
    # юзера, а приложение модератора (доказательство нарушения).
    screenshot_block = None
    if moderator_screenshot is not None and moderator_screenshot.photo:
        screenshot_block, ss_err = await _build_screenshot_block(
            bot, moderator_screenshot.photo,
        )
        if screenshot_block is not None:
            blocks.append(InputRichBlockDivider())
            blocks.append(
                InputRichBlockDetails(
                    summary="📷 Скриншот от модератора",
                    is_open=False,
                    blocks=[screenshot_block],
                )
            )
        else:
            logger.warning(
                "Screenshot inline build failed: %s — skip screenshot block",
                ss_err,
            )

    # ── Details: доп. инфо (сворачиваемое) ─────────────────────
    details_lines: list[str] = [f"Чат: {chat_id}"]
    if duration_seconds:
        details_lines.append(f"Длительность: {_format_duration(duration_seconds)}")
    if total_warns is not None:
        details_lines.append(f"Варнов всего: {total_warns}")
    blocks.append(InputRichBlockDivider())
    blocks.append(
        InputRichBlockDetails(
            summary="Доп. инфо",
            blocks=[InputRichBlockParagraph(text="\n".join(details_lines))],
        )
    )

    # ── Footer: время МСК + хэштег + кликабельное имя модератора ─
    # v4.4.10: модератор перенесён из отдельного параграфа в Footer.
    # Имя кликабельное (tg://user?id=…), приписки «Модератор:» нет —
    # экономит место, выглядит чище. Если модератор не передан (action
    # выполнен автоматически) — просто время + хэштег.
    now_msk = datetime.now(MSK)
    time_str = now_msk.strftime("%d.%m.%Y %H:%M") + " МСК"
    footer_text: list = [f"🕐 {time_str}"]
    if hashtag:
        footer_text.append(f" | {hashtag}")
    if mod is not None:
        mod_name = _user_display_name(mod)
        footer_text.append(" | ")
        footer_text.append(
            RichTextUrl(text=mod_name, url=f"tg://user?id={mod.id}")
        )
    blocks.append(InputRichBlockDivider())
    blocks.append(InputRichBlockFooter(text=footer_text))

    rich_msg = InputRichMessage(blocks=blocks)

    # ── Plain-text версия для fallback'а ───────────────────────
    offender_lines_plain: list[str] = [f"👤 {display_name}"]
    if target.username:
        offender_lines_plain.append(f"   @{target.username}")
    offender_lines_plain.append(f"   ID: {target.id}")
    offender_text_plain = "\n".join(offender_lines_plain)

    mod_text_plain: str | None = None
    if mod is not None:
        # В fallback'е имя модератора идёт в конце (как в rich-версии),
        # но с припиской для ясности (plain text не позволяет кликать).
        mod_name = _user_display_name(mod)
        mod_text_plain = f"{mod_name}"
        if mod.username:
            mod_text_plain += f" @{mod.username}"

    web_url_plain: str | None = None
    if WEB_PUBLIC_URL:
        web_url_plain = f"{WEB_PUBLIC_URL}/user/{target.id}"

    try:
        await bot.send_rich_message(chat_id=report_dest, rich_message=rich_msg)
    except TelegramAPIError as e:
        logger.error("Failed to send rich report to chat %s: %s", report_dest, e)
        # ── Fallback: простой plain-text отчёт ──────────────
        try:
            await _send_report_plain_fallback(
                bot=bot,
                report_dest=report_dest,
                action_label=action_label,
                offender_text=offender_text_plain,
                mod_text=mod_text_plain,
                web_url=web_url_plain,
                reason=reason,
                text_content=text_content,
                duration_seconds=duration_seconds,
                total_warns=total_warns,
                time_str=time_str,
                hashtag=hashtag,
            )
        except TelegramAPIError as e2:
            logger.error("Plain-text fallback also failed: %s", e2)

    # ── v4.8.3: send_sticker fallback ТОЛЬКО если inline не сработал ──
    # Если стикер успешно встроен в Details «📎 Сообщение юзера» — отдельное
    # сообщение НЕ отправляем (раньше в v4.8.2 отправляли всегда).
    # Fallback срабатывает для TGS-стикеров, когда rlottie не установлен
    # или упал при конвертации.
    if sticker_file_id and not sticker_inline_ok:
        try:
            await bot.send_sticker(chat_id=report_dest, sticker=sticker_file_id)
        except TelegramAPIError as e:
            logger.warning("Failed to attach sticker to report chat %s: %s",
                           report_dest, e)

    return None


async def _send_report_plain_fallback(
    *,
    bot: types.Bot,
    report_dest: int,
    action_label: str,
    offender_text: str,
    mod_text: str | None,
    web_url: str | None,
    reason: str | None,
    text_content: str | None,
    duration_seconds: int | None,
    total_warns: int | None,
    time_str: str,
    hashtag: str,
) -> None:
    """Резервный plain-text отчёт, если Rich Message не удалась.

    В plain text URL'ы распознаются Telegram автоматически — веб-ссылка
    остаётся кликабельной. tg://user?id=… в plain text НЕ распознаётся,
    поэтому кликабельные упоминания нарушителя/модератора опускаем —
    только текстовая информация.

    v4.4.10: Структура повторяет rich-версию — модератор идёт в самом конце
    (после времени), без приписки «Модератор:», просто имя.
    """
    parts: list[str] = []
    if hashtag:
        parts.append(hashtag)
    parts.append(action_label)
    parts.append("")
    parts.append(offender_text)
    if reason:
        parts.append(f"📝 {reason}")
    if web_url:
        parts.append(f"🌐 Открыть профиль: {web_url}")
    if text_content:
        parts.append(f"💬 Контент: {text_content[:500]}")
    if duration_seconds:
        parts.append(f"⏱ Длительность: {_format_duration(duration_seconds)}")
    if total_warns is not None:
        parts.append(f"⚠️ Варнов всего: {total_warns}")
    parts.append(f"🕐 {time_str}")
    if mod_text:
        parts.append(f" | {mod_text}")
    await bot.send_message(chat_id=report_dest, text="\n".join(parts))


# ── v4.7.27: Отчёт о ручном бане ───────────────────────────────────────────
async def _send_manual_ban_report(
    *,
    bot: types.Bot,
    chat_id: int,
    target: types.User,
    admin: types.User | None,
    report_dest: int,
    hashtag: str,
) -> None:
    """Компактный отчёт о ручном бане (админ забанил через Telegram-клиент).

    Отличается от обычного отчёта (``_send_report``):

      • Заголовок ``🚫 БАН (ручной)`` — чтобы визуально отличать от бана,
        выполненного ботом.
      • НЕТ причины — Telegram Bot API не сообщает, за что админ забанил
        пользователя (поле ``reason`` отсутствует в ChatMemberUpdated).
      • НЕТ текста/медиа сообщения — бот не знает, какое сообщение
        triggered бан (админ мог вообще не от чего банить, а просто
        длинным тапом по юзернейму).
      • Зато добавлено поле ``🛡 Админ`` (если ``admin`` не None и не бот):
        имя + @username + ID. Это позволяет видеть, КТО из админов банил.
        Берётся из ``event.from_user`` поля ChatMemberUpdated — Telegram
        всегда его передаёт для действий админов.
      • Footer содержит кликабельное имя админа (как в обычном отчёте).

    Структура Rich-сообщения (mirror v4.4.10 редизайна, но урезанная):

      1. SectionHeading — «🚫 БАН (ручной)»
      2. Divider
      3. List:
         • Нарушитель (имя кликабельно + @username + ID моноширинно)
         • 🛡 Админ (если есть) — имя кликабельно + @username + ID
         • 🌐 Веб-профиль (если WEB_PUBLIC_URL задан)
      4. Divider
      5. Details — «Доп. инфо»: только chat_id (нет длительности/варнов)
      6. Divider
      7. Footer — время МСК + хэштег + кликабельное имя админа

    Fallback: если ``send_rich_message`` падает — plain text в том же духе.
    """
    # ── Нарушитель ─────────────────────────────────────────────
    full_name = (target.first_name or "") + (
        f" {target.last_name}" if target.last_name else ""
    )
    display_name = full_name.strip() or "(без имени)"

    # ── Список блоков (v4.4.10 редизайн, урезанная версия) ────
    blocks: list = []
    blocks.append(InputRichBlockSectionHeading(text="🚫 БАН (ручной)", size=2))
    blocks.append(InputRichBlockDivider())

    # ── List: нарушитель / админ / веб-профиль ─────────────────
    list_items: list[InputRichBlockListItem] = []

    # Пункт 1: нарушитель (имя кликабельно + @username + ID моноширинно)
    offender_item_text: list = [
        "👤 ",
        RichTextUrl(
            text=display_name,
            url=f"tg://user?id={target.id}",
        ),
    ]
    if target.username:
        offender_item_text.append(f"  @{target.username}")
    offender_item_text.append("  ")
    offender_item_text.append(RichTextCode(text=f"ID: {target.id}"))
    list_items.append(
        InputRichBlockListItem(
            blocks=[InputRichBlockParagraph(text=offender_item_text)]
        )
    )

    # Пункт 2: админ (если есть и не бот)
    if admin is not None and not admin.is_bot:
        admin_full_name = (admin.first_name or "") + (
            f" {admin.last_name}" if admin.last_name else ""
        )
        admin_display = admin_full_name.strip() or "(без имени)"
        admin_item_text: list = [
            "🛡 ",
            RichTextUrl(
                text=admin_display,
                url=f"tg://user?id={admin.id}",
            ),
        ]
        if admin.username:
            admin_item_text.append(f"  @{admin.username}")
        admin_item_text.append("  ")
        admin_item_text.append(RichTextCode(text=f"ID: {admin.id}"))
        list_items.append(
            InputRichBlockListItem(
                blocks=[InputRichBlockParagraph(text=admin_item_text)]
            )
        )

    # Пункт 3: веб-профиль
    if WEB_PUBLIC_URL:
        web_url = f"{WEB_PUBLIC_URL}/user/{target.id}"
        list_items.append(
            InputRichBlockListItem(
                blocks=[InputRichBlockParagraph(
                    text=[
                        "🌐 ",
                        RichTextUrl(text="Открыть профиль →", url=web_url),
                    ]
                )]
            )
        )

    blocks.append(InputRichBlockList(items=list_items))

    # ── Details: доп. инфо (только chat_id — без длительности/варнов) ──
    details_lines: list[str] = [f"Чат: {chat_id}"]
    blocks.append(InputRichBlockDivider())
    blocks.append(
        InputRichBlockDetails(
            summary="Доп. инфо",
            blocks=[InputRichBlockParagraph(text="\n".join(details_lines))],
        )
    )

    # ── Footer: время МСК + хэштег + кликабельное имя админа ───
    now_msk = datetime.now(MSK)
    time_str = now_msk.strftime("%d.%m.%Y %H:%M") + " МСК"
    footer_text: list = [f"🕐 {time_str}"]
    if hashtag:
        footer_text.append(f" | {hashtag}")
    if admin is not None and not admin.is_bot:
        admin_name = _user_display_name(admin)
        footer_text.append(" | ")
        footer_text.append(
            RichTextUrl(text=admin_name, url=f"tg://user?id={admin.id}")
        )
    blocks.append(InputRichBlockDivider())
    blocks.append(InputRichBlockFooter(text=footer_text))

    rich_msg = InputRichMessage(blocks=blocks)

    # ── Plain-text версия для fallback'а ───────────────────────
    offender_lines_plain: list[str] = [f"👤 {display_name}"]
    if target.username:
        offender_lines_plain.append(f"   @{target.username}")
    offender_lines_plain.append(f"   ID: {target.id}")
    offender_text_plain = "\n".join(offender_lines_plain)

    admin_text_plain: str | None = None
    if admin is not None and not admin.is_bot:
        admin_name = _user_display_name(admin)
        admin_text_plain = f"🛡 {admin_name}"
        if admin.username:
            admin_text_plain += f" @{admin.username}"
        admin_text_plain += f" ID: {admin.id}"

    web_url_plain: str | None = None
    if WEB_PUBLIC_URL:
        web_url_plain = f"{WEB_PUBLIC_URL}/user/{target.id}"

    try:
        await bot.send_rich_message(chat_id=report_dest, rich_message=rich_msg)
    except TelegramAPIError as e:
        logger.error("Failed to send manual-ban report to chat %s: %s",
                     report_dest, e)
        # ── Fallback: простой plain-text отчёт ──────────────
        try:
            parts: list[str] = []
            if hashtag:
                parts.append(hashtag)
            parts.append("🚫 БАН (ручной)")
            parts.append("")
            parts.append(offender_text_plain)
            if admin_text_plain:
                parts.append(admin_text_plain)
            if web_url_plain:
                parts.append(f"🌐 Открыть профиль: {web_url_plain}")
            parts.append(f"Чат: {chat_id}")
            parts.append(f"🕐 {time_str}")
            await bot.send_message(chat_id=report_dest, text="\n".join(parts))
        except TelegramAPIError as e2:
            logger.error("Plain-text manual-ban fallback also failed: %s", e2)

    return None


async def _schedule_ephemeral_delete(
    bot: types.Bot,
    chat_id: int,
    message_id: int,
    delete_after: float,
    *,
    label: str = "ephemeral",
) -> None:
    """v4.7.20: общая функция для планировки авто-удаления ephemeral-сообщения.

    Используется и в _send_ephemeral (модератору), и в _send_user_warn_notification
    (нарушителю). Раньше логика дублировалась в двух местах — теперь в одном.

    Логика:
      • Если delete_after <= 0 или message_id falsy — ничего не делаем.
      • Иначе: создаём fire-and-forget корутину которая ждёт delete_after сек
        (через Semaphore(100) — ограничение на одновременные sleep'ы),
        затем вызывает bot.delete_ephemeral_message (Bot API 10.2).
      • На success: logger.info("{label} deleted: ...").
      • На TelegramAPIError: logger.warning (message may already be gone).
      • На CancelledError: logger.debug (shutdown) + re-raise.
      • На прочее Exception: logger.warning.

    Параметр label — для логов ("ephemeral" для модераторских подтверждений,
    "Warn ephemeral" для уведомлений нарушителю). Без label логи будут менее
    информативны при диагностике.
    """
    if not (delete_after and delete_after > 0 and message_id):
        return

    async def _del_ephemeral():
        try:
            async with _EPHEMERAL_DELETE_SEM:
                await asyncio.sleep(delete_after)
                await bot.delete_ephemeral_message(
                    chat_id=chat_id, message_id=message_id,
                )
            logger.info(
                "%s deleted: chat=%s msg=%s",
                label.capitalize() if label else "Ephemeral",
                chat_id, message_id,
            )
        except asyncio.CancelledError:
            # Shutdown в процессе — сообщение останется (acceptable для
            # ephemeral, оно видно только одному юзеру). Sem уже освобождён.
            logger.debug(
                "%s auto-delete cancelled (shutdown?) chat=%s msg=%s",
                label.capitalize() if label else "Ephemeral",
                chat_id, message_id,
            )
            raise  # propagate cancellation корректно
        except TelegramAPIError as e:
            logger.warning(
                "%s auto-delete in chat %s msg %s failed: %s "
                "(message may already be gone, or method unavailable)",
                label.capitalize() if label else "Ephemeral",
                chat_id, message_id, e,
            )
        except Exception as e:
            logger.warning(
                "%s auto-delete unexpected error: %s",
                label.capitalize() if label else "Ephemeral",
                e,
            )

    # v4.8.7: strong ref через _spawn_background_task — GC не убьёт задачу до delete.
    _spawn_background_task(_del_ephemeral(), label="del_ephemeral")


async def _send_ephemeral(
    *,
    bot: types.Bot,
    chat_id: int,
    recipient: types.User,
    text: str,
    delete_after: float = 30.0,
) -> None:
    """Отправляет ephemeral-сообщение модератору в группе (Bot API 10.2).

    Сообщение видно ТОЛЬКО указанному юзеру (``receiver_user_id``) —
    остальные участники чата (включая нарушителя) его не видят. Используется
    для подтверждений модератору при !warn, !mute, !ban, !unmute, чтобы
    модератор точно видел, кого он только что наказал/размьютил.

    Стелс-режим бота при этом сохраняется: нарушитель не получает никаких
    уведомлений и не догадывается о существовании бота.

    Если отправка не удалась (модератор заблокировал бота или ограничил
    ephemeral-сообщения) — тихо логируем и продолжаем.

    v4.5.6: ``delete_after`` (по умолчанию 30 секунд). Ephemeral-сообщения
    в Telegram-клиентах не «испаряются» автоматически — они остаются в чате
    как видимые только получателю сообщения и могут реотображаться при
    перезапуске клиента / скроллинге. Поэтому бот сам удаляет их через
    ``delete_after`` секунд фоновой таской. ``delete_after=0`` отключает
    авто-удаление (полезно для тестов).

    v4.7.20: авто-удаление вынесено в общую функцию _schedule_ephemeral_delete
    (используется также в _send_user_warn_notification). Используется
    bot.delete_ephemeral_message (Bot API 10.2) — обычный delete_message
    для ephemeral НЕ работает (см. BUG#2 в аудите v4.7.20).
    """
    try:
        sent = await bot.send_message(
            chat_id=chat_id,
            text=text,
            receiver_user_id=recipient.id,
            parse_mode="HTML",
        )
    except TelegramAPIError as e:
        # v4.7.20: TelegramAPIError (базовый класс) ловит все подклассы —
        # TelegramBadRequest, TelegramNotFound, TelegramForbiddenError,
        # TelegramConflictError. Раньше ловили только TelegramBadRequest —
        # но Forbidden (юзер заблокировал бота) и NotFound пробивались выше
        # и роняли весь handler. См. v4.7.19 фикс для night mode.
        logger.info(
            "Ephemeral message to moderator %s in chat %s failed: %s "
            "(this is normal if user restricted ephemeral messages)",
            recipient.id, chat_id, e,
        )
        return
    except Exception as e:
        logger.warning("Ephemeral message unexpected error: %s", e)
        return

    # v4.7.20: логируем успех отправки — нужно для диагностики BUG#2
    # (когда ephemeral отправляется, но не удаляется). Без этого лога
    # было непонятно: отправка упала или авто-удаление не сработало.
    logger.info(
        "Ephemeral sent: chat=%s recipient=%s msg=%s (will delete in %ss)",
        chat_id, recipient.id, getattr(sent, "message_id", None), delete_after,
    )

    # v4.7.20: планировка авто-удаления через общую функцию.
    await _schedule_ephemeral_delete(
        bot=bot, chat_id=chat_id,
        message_id=getattr(sent, "message_id", None),
        delete_after=delete_after,
        label="ephemeral",
    )


# ── v4.8.1: публичное сообщение о наказании в чат ───────────────────────────
# Громкие команды (!ban/!warn/!mute) публикуют в чат, где применили санкцию,
# короткое сообщение. Это делает модерацию видимой участникам чата — они
# понимают, что нарушитель наказан и за что. Пересланное сообщение нарушителя
# НЕ прикладывается (оно остаётся только в репорт-чате как rich-превью).
#
# Формат (HTML):
#   ban:  Пользователь "<display_name>" забанен за "<reason>"
#   warn: Пользователь "<display_name>" получил варн за "<reason>"
#   mute: Пользователь "<display_name>" замутан за "<reason>" на "<duration>"
#
# Все поля HTML-экранируются (display_name и reason могут содержать <>).
# duration приходит уже отформатированным через _format_duration — его
# тоже экранируем (он состоит только из цифр и букв, но для консистентности).
async def _send_public_punishment_notice(
    *,
    bot: types.Bot,
    chat_id: int,
    target: types.User,
    action: str,
    reason: str | None,
    duration: int | None = None,
) -> None:
    """Публикует публичное сообщение о наказании в чат (v4.8.1).

    Используется только для ГРОМКИХ команд (!ban/!warn/!mute). Для тихих
    (!sban/!swarn/!smute) — не вызывается (там остаётся ephemeral).

    :param action: 'ban' | 'warn' | 'mute'
    :param reason: причина (HTML-экранируется). Для ban/warn — обязательна
                   (на уровне regex). Для mute — тоже обязательна.
    :param duration: длительность мьюта в секундах (только для action='mute').
    """
    display_name = _user_display_name(target)
    name_safe = html.escape(display_name, quote=False)
    reason_safe = html.escape(reason, quote=False) if reason else ""

    if action == "ban":
        text = f'Пользователь "<b>{name_safe}</b>" забанен за "<i>{reason_safe}</i>"'
    elif action == "warn":
        text = f'Пользователь "<b>{name_safe}</b>" получил варн за "<i>{reason_safe}</i>"'
    elif action == "mute":
        dur_str = _format_duration(duration) if duration else ""
        dur_safe = html.escape(dur_str, quote=False)
        text = (
            f'Пользователь "<b>{name_safe}</b>" замутан за "<i>{reason_safe}</i>" '
            f'на "<b>{dur_safe}</b>"'
        )
    else:
        logger.warning("_send_public_punishment_notice: unknown action=%r", action)
        return

    try:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
        )
    except TelegramAPIError as e:
        logger.warning(
            "Public punishment notice failed (chat=%s action=%s target=%s): %s",
            chat_id, action, target.id, e,
        )


# ── v4.8.1: переиспользуемая функция разбана ────────────────────────────────
# Вынесена из inline-обработчика !unban для переиспользования в веб-панели
# (POST /api/unban). Делает:
#   1. unban_chat_member (Telegram API) — only_if_banned=True.
#   2. _revoke_last_action (БД) — помечает последний активный бан как снятый
#      (is_revoked=True, revoked_at=now, revoked_by_mod_id=<mod_id>).
#   3. _save_punishment (БД) — отдельная запись action_type='unban' с reason
#      и mod_id автора разбана. Видна в веб-панели как отдельная строка.
#
# Логирует все ошибки, но не пробрасывает исключения наверх (критичные
# ошибки Telegram API возвращаются через return value — словарь с ошибкой).
#
# Возвращает dict с полями:
#   {"ok": True} при успехе.
#   {"ok": False, "error": "<message>"} при ошибке Telegram API.
#   {"ok": False, "error": "<message>", "kind": "db"} при ошибке БД.
async def revoke_user_ban(
    *,
    bot: types.Bot,
    chat_id: int,
    user_id: int,
    mod_id: int,
    reason: str | None = None,
    target_user: types.User | None = None,
) -> dict:
    """v4.8.1: Разбанивает юзера — переиспользуемая логика.

    Используется:
      • Обработчиком команды !unban (модератор в чате).
      • Эндпоинтом POST /api/unban (веб-панель — модератор в браузере).

    :param bot: экземпляр aiogram.Bot
    :param chat_id: ID чата, где был выдан бан
    :param user_id: ID юзера для разбана
    :param mod_id: ID модератора, выполняющего разбан (web user tg_user_id
                   или TG user_id модератора из чата)
    :param reason: причина разбана (опциональна — сохраняется в БД)
    :param target_user: объект types.User нарушителя (опционален). Если
                        передан — используется для upsert + report. Если
                        None — upsert делается с user_id только.
    :return: dict с ok=True/False и описанием ошибки при неудаче.
    """
    # 1. Telegram API: unban_chat_member (only_if_banned=True — безопасный).
    # v4.8.7: tg_safe_call — ретраит при 429/RetryAfter автоматически.
    try:
        await tg_safe_call(
            lambda: bot.unban_chat_member(
                chat_id=chat_id, user_id=user_id, only_if_banned=True,
            ),
            label="revoke_user_ban/unban_chat_member",
        )
    except TelegramAPIError as e:
        logger.error(
            "revoke_user_ban: unban_chat_member failed (chat=%s user=%s mod=%s): %s",
            chat_id, user_id, mod_id, e,
        )
        return {"ok": False, "error": f"unban_chat_member failed: {e}"}

    # 2. БД: upsert user (если есть данные) + moderator + revoke + save punishment.
    try:
        async with async_session() as session:
            if target_user is not None:
                await _upsert_user(
                    session, target_user.id, target_user.username,
                    target_user.first_name, target_user.last_name,
                )
            else:
                # Минимальный upsert — только user_id, без профиля.
                # _upsert_user не принимает partial — используем прямой SELECT
                # + UPDATE/INSERT через ORM. Дешёвый fallback для web-API,
                # где у нас есть только user_id.
                from db import User as _U
                existing = (await session.execute(
                    select(_U).where(_U.user_id == user_id)
                )).scalar_one_or_none()
                if existing is None:
                    session.add(_U(user_id=user_id))
                    await session.flush()
            await _upsert_moderator(session, mod_id, None, None)
            # Помечаем последний активный бан как снятый.
            await _revoke_last_action(
                session, user_id, chat_id, "ban", revoked_by_mod_id=mod_id,
            )
            # Сохраняем отдельную запись action_type='unban'.
            await _save_punishment(
                session, user_id, mod_id, chat_id,
                "unban", None, reason, None,
            )
    except Exception as e:
        logger.error(
            "revoke_user_ban: DB error (chat=%s user=%s mod=%s): %s",
            chat_id, user_id, mod_id, e,
        )
        return {"ok": False, "error": f"DB error: {e}", "kind": "db"}

    logger.info(
        "revoke_user_ban: success (chat=%s user=%s mod=%s reason=%r)",
        chat_id, user_id, mod_id, reason,
    )
    return {"ok": True}


# ── v4.4.9: уведомление НАРУШИТЕЛЮ при !warn (видно только ему) ──────────
# Bot API 10.2 (aiogram 3.30) позволяет отправлять сообщение в группу так,
# чтобы его видел только один конкретный юзер (через receiver_user_id).
# Раньше нарушитель вообще не знал, что ему выдали варн — это делало варн
# бесполезным как воспитательную меру. Теперь нарушитель видит:
#   • что ему выдали варн
#   • причину
#   • текущее кол-во варнов
#   • пороги мьюта/бана (если настроены)
# Остальные участники чата этого сообщения НЕ видят — стелс бота для всех
# кроме нарушителя сохраняется. Сам факт существования бота раскрывается
# только тому, кого наказали, и только когда наказание — варн (не мьют/бан).
async def _send_user_warn_notification(
    *,
    bot: types.Bot,
    chat_id: int,
    target: types.User,
    reason: str,
    total_warns: int,
    settings: ChatSettings,
    delete_after: float = 30.0,
) -> None:
    """Отправляет нарушителю ephemeral-сообщение о выданном варне.

    Сообщение видно ТОЛЬКО target-юзеру (``receiver_user_id=target.id``).
    Остальные участники чата его не видят. Если отправка не удалась
    (юзер заблокировал бота или ограничил ephemeral-сообщения) —
    тихо логируем и продолжаем; варн в БД уже сохранён.

    v4.5.6: ``delete_after`` (по умолчанию 30 секунд). Аналогично
    ``_send_ephemeral`` — бот сам удаляет уведомление через заданное
    время, чтобы оно не висело в чате и не реотображалось при
    перезапуске клиента. ``delete_after=0`` отключает авто-удаление.
    """
    reason_safe = html.escape(reason, quote=False) if reason else "(не указана)"

    # Пороговая информация (показываем только если хоть один порог > 0)
    # Защищаемся от None (на случай если ChatSettings создан без дефолтов).
    wtm = settings.warns_to_mute or 0
    wtb = settings.warns_to_ban or 0
    threshold_lines: list[str] = []
    if wtm > 0 or wtb > 0:
        parts: list[str] = []
        if wtm > 0:
            parts.append(f"мьют при {wtm}")
        if wtb > 0:
            parts.append(f"бан при {wtb}")
        threshold_lines.append("Лимиты: " + ", ".join(parts) + ".")

        # Дополнительное предупреждение, если юзер подошёл к границе
        if wtb > 0 and total_warns == wtb - 1:
            threshold_lines.append("⚠️ Следующий варн — бан.")
        elif wtm > 0 and total_warns == wtm - 1:
            threshold_lines.append("⚠️ Следующий варн — мьют.")
        elif wtb > 0 and total_warns >= wtb:
            threshold_lines.append("Вы превысили лимит варнов — возможен бан.")

    threshold_str = "\n".join(threshold_lines)

    text = (
        f"⚠️ <b>Вам выдано предупреждение</b>\n\n"
        f"<b>Причина:</b> {reason_safe}\n"
        f"<b>Всего предупреждений:</b> {total_warns}"
        + (f"\n\n{threshold_str}" if threshold_str else "")
    )

    try:
        sent = await bot.send_message(
            chat_id=chat_id,
            text=text,
            receiver_user_id=target.id,
            parse_mode="HTML",
        )
    except TelegramAPIError as e:
        # v4.7.20: TelegramAPIError ловит все подклассы (BadRequest,
        # Forbidden, NotFound, Conflict). Раньше только TelegramBadRequest —
        # но если юзер заблокировал бота (Forbidden), исключение пробивалось
        # выше и ронило весь handler. См. _send_ephemeral для деталей.
        logger.info(
            "Warn notification to user %s in chat %s failed: %s "
            "(this is normal if user restricted ephemeral messages)",
            target.id, chat_id, e,
        )
        return
    except Exception as e:
        logger.warning("Warn notification to user unexpected error: %s", e)
        return

    # v4.7.20: логируем успех отправки — для диагностики BUG#2
    logger.info(
        "Warn ephemeral sent: chat=%s target=%s msg=%s (will delete in %ss)",
        chat_id, target.id, getattr(sent, "message_id", None), delete_after,
    )

    # v4.7.20: планировка авто-удаления через общую функцию _schedule_ephemeral_delete
    # (shared с _send_ephemeral). Раньше логика дублировалась.
    await _schedule_ephemeral_delete(
        bot=bot, chat_id=chat_id,
        message_id=getattr(sent, "message_id", None),
        delete_after=delete_after,
        label="Warn ephemeral",
    )


# ── Авто-санкция при превышении порога варнов ──────────────────────────────
async def _check_warn_threshold(
    bot: types.Bot,
    chat_id: int,
    target: types.User,
    mod: types.User,
) -> None:
    """Проверяет, не достигнут ли порог варнов для автосанкции."""
    async with async_session() as session:
        settings = await _get_chat_settings(session, chat_id)
        total_warns = await _count_warns(session, target.id, chat_id)

        # Проверяем бан
        if settings.warns_to_ban > 0 and total_warns >= settings.warns_to_ban:
            # Снимаем слепок пермишенов ДО бана
            perm_snapshot = None
            try:
                member = await bot.get_chat_member(chat_id=chat_id, user_id=target.id)
                perm_snapshot = _snapshot_permissions(member)
            except TelegramAPIError:
                pass

            try:
                # v4.8.7: tg_safe_call — ретраит при 429/RetryAfter.
                await tg_safe_call(
                    lambda: bot.ban_chat_member(chat_id=chat_id, user_id=target.id),
                    label="_check_warn_threshold/autoban",
                )
                # v4.7.27: помечаем бан от бота — для дедупликации в on_chat_member_updated
                _mark_bot_ban(chat_id, target.id)
                await _upsert_user(session, target.id, target.username,
                                   target.first_name, target.last_name)
                await _upsert_moderator(session, mod.id, mod.username, mod.first_name)
                await _save_punishment(
                    session, target.id, mod.id, chat_id,
                    "ban", None, f"Автобан: {total_warns} варнов", None,
                    permissions_snapshot=perm_snapshot,
                )
                # v4.5.1: гасим варны, чтобы следующий !warn не триггерил
                # автобан повторно. Варны остаются видны в логе (is_revoked=False),
                # но _count_warns их больше не считает.
                consumed = await _mark_warns_consumed(
                    session, target.id, chat_id, "auto_ban",
                )
                logger.info(
                    "Auto-ban: marked %d warns as consumed_by_action=auto_ban "
                    "for user %s in chat %s",
                    consumed, target.id, chat_id,
                )
                await _send_report(bot, chat_id, target, "ban",
                                   f"Автобан: {total_warns} варнов",
                                   mod=mod)
                logger.info("Auto-ban triggered for user %s in chat %s (%d warns)",
                            target.id, chat_id, total_warns)
                # ── Ephemeral-уведомление модератору (видно только ему) ────
                # Нарушитель НЕ уведомляется — стелс-режим бота сохраняется.
                await _send_ephemeral(
                    bot=bot, chat_id=chat_id, recipient=mod,
                    text=(
                        f"🤖 Автобан: {_user_mention_html(target)} "
                        f"({total_warns} варнов)."
                    ),
                )
            except TelegramAPIError as e:
                logger.error("Auto-ban failed: %s", e)
            return

        # Проверяем мьют
        if settings.warns_to_mute > 0 and total_warns >= settings.warns_to_mute:
            # Снимаем слепок пермишенов ДО мьюта
            perm_snapshot = None
            try:
                member = await bot.get_chat_member(chat_id=chat_id, user_id=target.id)
                perm_snapshot = _snapshot_permissions(member)
            except TelegramAPIError:
                pass

            mute_dur = settings.mute_duration_seconds or 3600
            # v4.8.4: прогрессивный автомьют — base + (count * 60 сек).
            # count = значение ДО инкремента (0 для первого мута).
            auto_count = await _get_automute_count(session, chat_id, target.id)
            mute_dur = mute_dur + (auto_count * 60)
            until_date = int(datetime.now(timezone.utc).timestamp()) + mute_dur
            try:
                # v4.8.7: tg_safe_call — ретраит при 429/RetryAfter.
                await tg_safe_call(
                    lambda: bot.restrict_chat_member(
                        chat_id=chat_id,
                        user_id=target.id,
                        permissions=_mute_permissions(),
                        until_date=until_date,
                    ),
                    label="_check_warn_threshold/automute",
                )
                await _upsert_user(session, target.id, target.username,
                                   target.first_name, target.last_name)
                await _upsert_moderator(session, mod.id, mod.username, mod.first_name)
                await _save_punishment(
                    session, target.id, mod.id, chat_id,
                    "mute", mute_dur, f"Автомьют: {total_warns} варнов", None,
                    permissions_snapshot=perm_snapshot,
                )
                # v4.5.1: гасим варны, чтобы следующий !warn не триггерил
                # автомьют повторно. Без этого баг: warns_to_mute=3,
                # warns_to_ban=999999 → 4-й !warn снова триггерит мьют,
                # 5-й — снова, и так до бесконечности.
                consumed = await _mark_warns_consumed(
                    session, target.id, chat_id, "auto_mute",
                )
                # v4.8.4: инкремент счётчика автомьютов (после успешного мьюта).
                new_count = await _increment_automute_count(session, chat_id, target.id)
                await session.commit()
                logger.info(
                    "Auto-mute: marked %d warns as consumed_by_action=auto_mute "
                    "for user %s in chat %s",
                    consumed, target.id, chat_id,
                )
                await _send_report(bot, chat_id, target, "mute",
                                   f"Автомьют: {total_warns} варнов",
                                   mod=mod,
                                   duration_seconds=mute_dur)
                logger.info(
                    "Auto-mute triggered for user %s in chat %s "
                    "(%d warns, %s, automute_count %d→%d)",
                    target.id, chat_id, total_warns,
                    _format_duration(mute_dur), auto_count, new_count,
                )
                # ── Ephemeral-уведомление модератору (видно только ему) ────
                # Нарушитель НЕ уведомляется — стелс-режим бота сохраняется.
                # v4.8.4: показываем итоговую длительность мута (без разбивки).
                await _send_ephemeral(
                    bot=bot, chat_id=chat_id, recipient=mod,
                    text=(
                        f"🤖 Автомьют: {_user_mention_html(target)} "
                        f"({total_warns} варнов, {_format_duration(mute_dur)})."
                    ),
                )
            except TelegramAPIError as e:
                logger.error("Auto-mute failed: %s", e)


# ── Получить описание контента пересланного сообщения ──────────────────────
def _get_message_content_desc(msg: types.Message) -> str | None:
    """Возвращает текстовое описание контента сообщения для причины."""
    if msg.text:
        return msg.text
    if msg.caption:
        return msg.caption
    if msg.photo:
        return "🖼 [Фото]"
    if msg.video:
        return "🎬 [Видео]"
    if msg.sticker:
        return f"🎭 [Стикер: {msg.sticker.emoji or ''}]"
    if msg.animation:
        return "🎞 [GIF]"
    if msg.voice:
        return "🎤 [Голосовое]"
    if msg.audio:
        return "🎵 [Аудио]"
    if msg.document:
        return f"📄 [Документ: {msg.document.file_name or ''}]"
    if msg.video_note:
        return "📹 [Кружок]"
    if msg.contact:
        return "📞 [Контакт]"
    if msg.location:
        return "📍 [Геолокация]"
    if msg.poll:
        return f"📊 [Опрос: {msg.poll.question}]"
    if msg.dice:
        return f"🎲 [Dice: {msg.dice.emoji}]"
    return None


# ═══════════════════════════════════════════════════════════════════════════
# v4.8.10 (бонус): !mywarns — самопроверка варнов обычными юзерами
# ═══════════════════════════════════════════════════════════════════════════
# Доступна ВСЕМ юзерам (не только модераторам). Запрос @Gleb (11 авг 2026).
#
# Поведение:
#   - В группе: бот удаляет команду → отправляет DM юзеру с инфо по текущему чату.
#     Если DM невозможен (юзер не запускал /start) — молча игнорирует.
#   - В DM: бот отвечает напрямую, сводка по всем чатам с активными варнами.
#
# Содержимое (per-chat):
#   - Количество активных варнов
#   - Дата последнего варна (без причины, без модератора — приватность)
#
# Таймаут: per-user per-chat, 5 минут, silent (молча игнорирует при превышении).

_CMD_MYWARNS = re.compile(r"^!mywarns\s*$", re.IGNORECASE)
_MYWARNS_TIMEOUT_SECONDS = 300  # 5 минут
_mywarns_last_call: dict[tuple[int, int], float] = {}  # (user_id, chat_id) → timestamp

# v4.8.11: strftime("%b") зависит от локали, а она в проекте нигде не задаётся.
# В slim-образе локали нет, поэтому юзер видел «12 Aug 2026» вместо «12 авг 2026».
_RU_MONTHS_SHORT = (
    "янв", "фев", "мар", "апр", "мая", "июн",
    "июл", "авг", "сен", "окт", "ноя", "дек",
)


def _format_msk_date_ru(dt: datetime) -> str:
    """Дата в МСК с русским месяцем: «12 авг 2026, 15:30»."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    msk_dt = dt.astimezone(MSK)
    month = _RU_MONTHS_SHORT[msk_dt.month - 1]
    return f"{msk_dt.day:02d} {month} {msk_dt.year}, {msk_dt:%H:%M}"


def _mywarns_prune_stale(now: float) -> None:
    """v4.8.11: чистит протухшие записи реестра таймаутов.

    Ключ — (user_id, chat_id), и записи не удалялись никогда: каждый юзер в
    каждом чате оставлял запись навсегда. Бот работает месяцами без рестарта,
    так что словарь рос всё это время. Записи старше таймаута уже не влияют
    на решение, поэтому их можно выбрасывать.
    """
    stale = [
        key for key, ts in _mywarns_last_call.items()
        if now - ts >= _MYWARNS_TIMEOUT_SECONDS
    ]
    for key in stale:
        del _mywarns_last_call[key]


async def _format_user_warns_for_chat(session, user_id: int, chat_id: int) -> str | None:
    """Форматирует сводку варнов юзера для конкретного чата.

    Возвращает None если варнов нет. Иначе строку вида:
      «• Варнов: 2
        • Последний: 12 авг 2026, 15:30»
    """
    total = await _count_warns(session, user_id, chat_id)
    if total == 0:
        return None

    # Последний варн (для даты)
    last_warn = (await session.execute(
        select(Punishment)
        .where(
            Punishment.user_id == user_id,
            Punishment.chat_id == chat_id,
            Punishment.action_type == "warn",
            Punishment.is_revoked.is_(False),
            Punishment.consumed_by_action.is_(None),
        )
        .order_by(Punishment.created_at.desc())
        .limit(1)
    )).scalar_one_or_none()

    lines = [f"• Варнов: {total}"]
    if last_warn and last_warn.created_at:
        # v4.8.11: русский месяц вместо strftime("%b") — см. _format_msk_date_ru.
        lines.append(f"• Последний: {_format_msk_date_ru(last_warn.created_at)}")

    return "\n".join(lines)


async def _format_user_warns_summary(session, user_id: int) -> str:
    """Форматирует сводку варнов юзера по всем чатам (для DM).

    Возвращает строку для ответа в DM. Если варнов нет — «✅ У вас нет активных варнов.»
    """
    # Находим все чаты, где у юзера есть активные варны
    chat_ids_with_warns = (await session.execute(
        select(Punishment.chat_id)
        .where(
            Punishment.user_id == user_id,
            Punishment.action_type == "warn",
            Punishment.is_revoked.is_(False),
            Punishment.consumed_by_action.is_(None),
        )
        .distinct()
    )).scalars().all()

    if not chat_ids_with_warns:
        return "✅ У вас нет активных варнов."

    # Для каждого чата — форматирование
    parts = ["📊 Ваши активные варны:"]
    for chat_id in chat_ids_with_warns:
        # Пытаемся получить название чата из ChatSettings
        cs = await _get_chat_settings(session, chat_id)
        chat_title = cs.title if cs and cs.title else f"Чат {chat_id}"

        warn_info = await _format_user_warns_for_chat(session, user_id, chat_id)
        if warn_info:
            parts.append(f"\nЧат «{chat_title}»:")
            parts.append(warn_info)

    return "\n".join(parts)


@router.message(F.chat.type.in_(["group", "supergroup"]), F.text.regexp(r"^!mywarns\s*$"))
async def handle_mywarns_group(message: types.Message) -> None:
    """!mywarns в группе — удаляет команду, отправляет DM с варнами в этом чате.

    v4.8.10 (бонус): доступна всем юзерам. Per-user per-chat timeout 5 мин, silent.
    """
    if not message.from_user or not message.text:
        return
    if not _CMD_MYWARNS.match(message.text):
        return

    user_id = message.from_user.id
    chat_id = message.chat.id
    now = time.time()

    # v4.8.11: удаляем команду ДО проверки таймаута.
    #
    # Приватность — единственная причина, по которой команда вообще удаляется:
    # никто не должен видеть, что юзер проверяет свои варны. Раньше выход по
    # таймауту происходил раньше удаления, и при повторном вызове «!mywarns»
    # оставался висеть в чате — то есть защита отваливалась ровно в том случае,
    # ради которого она существует.
    try:
        await message.delete()
    except TelegramAPIError as e:
        logger.warning("mywarns: cannot delete command message in chat %s: %s", chat_id, e)

    # Timeout: per-user per-chat, 5 минут, silent
    key = (user_id, chat_id)
    last = _mywarns_last_call.get(key, 0)
    if now - last < _MYWARNS_TIMEOUT_SECONDS:
        # Silent — молча игнорируем, ответ не отправляем
        # (чтобы юзер не понял что произошёл таймаут).
        logger.debug("mywarns timeout for user %s in chat %s (%.0f sec left)",
                     user_id, chat_id, _MYWARNS_TIMEOUT_SECONDS - (now - last))
        return
    _mywarns_prune_stale(now)
    _mywarns_last_call[key] = now

    # Получаем варны для этого чата
    async with async_session() as session:
        warn_info = await _format_user_warns_for_chat(session, user_id, chat_id)

    if warn_info is None:
        text = "✅ У вас нет активных варнов в этом чате."
    else:
        text = f"📊 Ваши варны в этом чате:\n{warn_info}"

    # Отправляем DM. Если невозможно — молча игнорируем.
    # v4.8.11: через tg_safe_call — при flood control (429) сообщение уходит
    # после ретрая. Без обёртки оно терялось, а таймаут уже был записан, так
    # что повторить запрос юзер смог бы только через 5 минут.
    try:
        await tg_safe_call(
            lambda: message.bot.send_message(chat_id=user_id, text=text),
            label="mywarns_group_dm",
        )
    except TelegramAPIError as e:
        logger.debug("mywarns: cannot send DM to user %s: %s", user_id, e)


@router.message(F.chat.type == "private", F.text.regexp(r"^!mywarns\s*$"))
async def handle_mywarns_dm(message: types.Message) -> None:
    """!mywarns в DM — отвечает напрямую, сводка по всем чатам.

    v4.8.10 (бонус): доступна всем юзерам. Per-user timeout 5 мин (per-chat=DM chat id), silent.
    """
    if not message.from_user or not message.text:
        return
    if not _CMD_MYWARNS.match(message.text):
        return

    user_id = message.from_user.id
    chat_id = message.chat.id  # Для DM это = user_id, но используем как ключ таймаута

    # Timeout: per-user per-chat (DM chat id), 5 минут, silent
    now = time.time()
    key = (user_id, chat_id)
    last = _mywarns_last_call.get(key, 0)
    if now - last < _MYWARNS_TIMEOUT_SECONDS:
        logger.debug("mywarns DM timeout for user %s (%.0f sec left)",
                     user_id, _MYWARNS_TIMEOUT_SECONDS - (now - last))
        return
    _mywarns_prune_stale(now)
    _mywarns_last_call[key] = now

    # Сводка по всем чатам
    async with async_session() as session:
        text = await _format_user_warns_summary(session, user_id)

    # v4.8.11: через tg_safe_call и с обработкой ошибки — раньше ответ шёл
    # голым await, и TelegramRetryAfter улетал наверх, роняя хендлер.
    try:
        await tg_safe_call(lambda: message.reply(text), label="mywarns_dm_reply")
    except TelegramAPIError as e:
        logger.debug("mywarns: cannot reply in DM to user %s: %s", user_id, e)


# ═══════════════════════════════════════════════════════════════════════════
# Обработчики команд в ГРУППАХ
# ═══════════════════════════════════════════════════════════════════════════

@router.message(
    F.chat.type.in_(["group", "supergroup"]),
    _ModerationCommandFilter(),
)
async def handle_group_command(message: types.Message) -> None:
    """Обрабатывает !mute, !warn, !ban, !unmute, !unban, !unwarn в группах.

    v4.7.26: фильтр _ModerationCommandFilter теперь стоит в декораторе —
    handler вызывается ТОЛЬКО когда сообщение является командой. Раньше
    handler перехватывал все reply-сообщения и return'ил без обработки,
    что в aiogram 3.x останавливает propagation → handle_content_filters
    (word/link/via_bot filter) не вызывался для reply-сообщений.

    v4.8.3: убрано требование F.reply_to_message из декоратора — теперь
    команды !ban/!sban/!warn/!swarn/!mute/!smute можно отправлять БЕЗ reply,
    указав цель первым аргументом (@username или TGID). Команды снятия
    (!unban/!unmute/!unwarn) по-прежнему требуют reply — проверка внутри.
    Также: команда может быть в message.caption (если модератор приложил
    скриншот нарушения) — _ModerationCommandFilter это учитывает.
    """
    # v4.8.3: команда может быть в message.text ИЛИ message.caption
    # (если модератор приложил скриншот нарушения).
    text = message.text or message.caption
    if not text:
        return
    # ── v4.4.8 FIX: не трогаем сообщения модератора, если это не команда ──
    # Раньше бот удалял ЛЮБОЙ ответ модератора в чате (т.к. удаление шло
    # ДО проверки на соответствие команде). Теперь сначала проверяем, что
    # текст реально является одной из модераторских команд — и только тогда
    # удаляем. Обычные ответы модератора больше не исчезают.
    # v4.7.26: проверка _is_moderation_command уже в фильтре — но оставляем
    # как defensive guard (вдруг filter изменят).
    if not _is_moderation_command(text):
        return

    # Проверяем, что отправитель — админ
    chat_id = message.chat.id
    async with async_session() as session:
        is_adm = await _is_admin(session, chat_id, message.from_user.id)
    if not is_adm:
        return

    mod = message.from_user

    # ── v4.8.3: резолв цели наказания ──────────────────────────────────
    # Команды снятия (!unmute/!unban/!unwarn/!warns/!resetwarns) — работают
    # ТОЛЬКО по reply. Если reply нет — отказываем.
    # Наказательные команды (!ban/!sban/!warn/!swarn/!mute/!smute) —
    # резолвятся через _resolve_punishment_target (reply → @username → TGID).
    # v4.8.4: !resetmc — тоже резолвится через _resolve_punishment_target
    # (reply → @username → TGID), но это не наказательная команда
    # (сброс счётчика, не мьют/варн/бан).Self/friendly-fire checks не применяются.
    is_punitive_cmd = bool(
        _CMD_MUTE.match(text) or _CMD_WARN.match(text) or _CMD_BAN.match(text)
        or _CMD_SMUTE.match(text) or _CMD_SWARN.match(text) or _CMD_SBAN.match(text)
    )
    is_resetmc_cmd = bool(_CMD_RESETMC.match(text))

    if is_punitive_cmd or is_resetmc_cmd:
        # Парсим target из команды (если есть) — для передачи в хелпер.
        # Берём первый matching паттерн и достаём target-группу.
        cmd_target_str: str | None = None
        for pat in (_CMD_BAN, _CMD_SBAN, _CMD_WARN, _CMD_SWARN,
                    _CMD_MUTE, _CMD_SMUTE, _CMD_RESETMC):
            m_pat = pat.match(text)
            if m_pat and m_pat.groupdict().get("target"):
                cmd_target_str = m_pat.group("target")
                break

        target, target_err = await _resolve_punishment_target(
            message, cmd_target_str, chat_id,
        )
        if target_err is not None:
            # Не удалось зарезолвить цель — ephemeral модератору и выход.
            try:
                await _send_ephemeral(
                    bot=message.bot, chat_id=chat_id, recipient=mod,
                    text=target_err,
                )
            except Exception:
                pass
            try:
                await message.delete()
            except TelegramAPIError:
                pass
            return
        # target теперь types.User (либо из reply, либо из БД, либо синтетический
        # из TGID — _resolve_punishment_target возвращает User-объект).
        target_content: str | None = None
        if message.reply_to_message is not None:
            target_content = _get_message_content_desc(message.reply_to_message)
    else:
        # Команды снятия — требуют reply.
        if message.reply_to_message is None:
            try:
                await _send_ephemeral(
                    bot=message.bot, chat_id=chat_id, recipient=mod,
                    text=(
                        "❌ Эта команда требует reply на сообщение пользователя, "
                        "к которому применяется действие."
                    ),
                )
            except Exception:
                pass
            try:
                await message.delete()
            except TelegramAPIError:
                pass
            return
        target: types.User = message.reply_to_message.from_user
        target_content = _get_message_content_desc(message.reply_to_message)
    if is_punitive_cmd:
        if target.id == mod.id:
            try:
                await _send_ephemeral(
                    bot=message.bot, chat_id=chat_id, recipient=mod,
                    text="❌ Нельзя применить наказание к самому себе.",
                )
            except Exception:
                pass
            # Удаляем сообщение модератора с командой (всё равно)
            try:
                await message.delete()
            except TelegramAPIError:
                pass
            return
        # friendly-fire: target тоже админ в этом чате?
        async with async_session() as session:
            target_is_adm = await _is_admin(session, chat_id, target.id)
        if target_is_adm:
            try:
                await _send_ephemeral(
                    bot=message.bot, chat_id=chat_id, recipient=mod,
                    text=(
                        f"❌ Нельзя наказать {_user_mention_html(target)}: "
                        f"это модератор/админ в этом чате."
                    ),
                )
            except Exception:
                pass
            try:
                await message.delete()
            except TelegramAPIError:
                pass
            return

    # v4.5.2: удаляем сообщение модератора с командой, ТОЛЬКО если
    # auto_delete_commands=True для этого чата. Раньше удалялось всегда —
    # теперь SU может отключить авто-удаление per-chat (для прозрачности:
    # команда остаётся видимой, модератор виден).
    async with async_session() as session:
        cs = await _get_chat_settings(session, chat_id)
        auto_del = cs.auto_delete_commands if cs else True
    if auto_del:
        try:
            await message.delete()
        except TelegramAPIError:
            logger.warning("Не удалось удалить сообщение модератора %s в чате %s",
                           mod.id, chat_id)

    # ── v4.8.10: 11 команд вынесены в mod_commands.py ──────────────────────
    # Раньше тут были inline-блоки для !mute, !smute, !warn, !swarn, !sban,
    # !unmute, !unban, !unwarn, !warns, !resetwarns, !resetmc — суммарно
    # ~850 строк. Теперь все они в mod_commands.COMMANDS dict, и handle_group_command
    # просто вызывает нужную функцию по имени команды.
    # cmd_ban уже был вынесен в v4.8.9 — он тоже в COMMANDS.
    #
    # Порядок проверок сохранён: бан-команды первыми, потом мьют/варн, потом
    # команды снятия/информации. Это важно для regex-приоритета (например,
    # !ban матчится раньше !bansticker, если бы он был мод-командой).
    from mod_commands import COMMANDS as _MOD_COMMANDS
    from mod_commands import ModContext as _ModContext

    _ctx = _ModContext(
        chat_id=chat_id,
        mod=mod,
        target=target,
        target_content=target_content,
        text=text,
    )

    # Проверяем команды в порядке объявления в handle_group_command (важно для regex).
    # Каждая cmd_X сама проверяет свой regex через ctx.text и возвращает None если не матчит.
    _cmd_handlers_in_order = [
        _MOD_COMMANDS["mute"],
        _MOD_COMMANDS["smute"],
        _MOD_COMMANDS["warn"],
        _MOD_COMMANDS["swarn"],
        _MOD_COMMANDS["sban"],
        _MOD_COMMANDS["unmute"],
        _MOD_COMMANDS["unban"],
        _MOD_COMMANDS["unwarn"],
        _MOD_COMMANDS["warns"],
        _MOD_COMMANDS["resetwarns"],
        _MOD_COMMANDS["resetmc"],
    ]
    for _handler in _cmd_handlers_in_order:
        _matched_before = _handler.__name__.replace("cmd_", "")
        # Каждая cmd_X внутри себя делает regex-проверку и возвращает None если не матчит.
        # Если сматчилась — выполняет команду и return'ит (внутри функции).
        # Но мы не видим return из функции здесь. Поэтому проверяем regex здесь
        # и вызываем только если матчит.
        _cmd_regex_map = {
            "mute": _CMD_MUTE,
            "smute": _CMD_SMUTE,
            "warn": _CMD_WARN,
            "swarn": _CMD_SWARN,
            "sban": _CMD_SBAN,
            "unmute": _CMD_UNMUTE,
            "unban": _CMD_UNBAN,
            "unwarn": _CMD_UNWARN,
            "warns": _CMD_WARNS,
            "resetwarns": _CMD_RESETWARNS,
            "resetmc": _CMD_RESETMC,
        }
        _regex = _cmd_regex_map.get(_matched_before)
        if _regex and _regex.match(text):
            await _handler(message, _ctx)
            return

    # Ни одна команда не сматчилась — это не мод-команда.
    # (filter _ModerationCommandFilter в декораторе уже должен был отсечь,
    # но это defensive guard для случая если filter изменят.)


# ═══════════════════════════════════════════════════════════════════════════
# v4.7.20: !alarm on/off — отдельный handler (не требует reply)
# ═══════════════════════════════════════════════════════════════════════════

@router.message(
    F.chat.type.in_(["group", "supergroup"]),
    _AlarmCommandFilter(),
)
async def handle_alarm_command(message: types.Message) -> None:
    """!alarm on [duration] / !alarm off — экстренная блокировка медиа в чате.

    Доступ: любой модератор (ChatAdmin или ADMIN_IDS). Не требует reply
    на сообщение нарушителя — это "режимная" команда, применяется ко всему чату.

    Поведение !alarm on:
      • Если чат в night mode → отказ (DM модератору: "сейчас ночной режим,
        alarm избыточен"). Ночной режим сам по себе ограничивает права.
      • Если alarm уже активен → обновляем alarm_active_until (продлеваем
        или сокращаем — в зависимости от новой duration).
      • Иначе: snapshot текущих прав → применяем alarm_permissions + slow_mode 30s
      • Логируем, отправляем подтверждение в DM модератору

    Поведение !alarm off:
      • Если alarm не активен → DM: "Alarm не активен"
      • Иначе: восстанавливаем права (через _deactivate_alarm) → DM: "Alarm снят"

    Стелс: обычные юзеры (не модераторы) полностью игнорируются — бот
    не отвечает им в чате, не шлёт DM, не удаляет их сообщение. Только
    модераторы получают DM-подтверждение.

    v4.7.26: фильтр _AlarmCommandFilter теперь стоит в декораторе —
    handler вызывается ТОЛЬКО когда сообщение является !alarm командой.
    Раньше handler перехватывал ВСЕ group messages (даже не !alarm) и
    return'ил, что в aiogram 3.x останавливает propagation →
    handle_content_filters (word/link/via_bot filter) не вызывался.
    """
    text = message.text
    # text не None — гарантировано _AlarmCommandFilter'ом.

    m = _CMD_ALARM.match(text)
    if not m:
        return  # Defensive — filter уже это проверил, но на всякий случай.

    # Парсим аргументы
    action_raw = m.group(1).lower()
    duration_value = m.group(2)
    duration_unit = m.group(3)

    is_on = action_raw in ("on", "вкл")
    is_off = action_raw in ("off", "выкл")
    if not (is_on or is_off):
        return  # На всякий случай — regex должен гарантировать

    chat_id = message.chat.id
    mod = message.from_user

    # ── Проверка прав: только модераторы (ChatAdmin в БД или ADMIN_IDS env) ──
    # Стелс: если пишет не модератор — полностью игнорируем (return).
    async with async_session() as session:
        is_adm = await _is_admin(session, chat_id, mod.id)
    if not is_adm:
        return  # Молча игнорируем — стелс

    # ── Удаляем сообщение модератора с командой (если auto_delete_commands) ──
    async with async_session() as session:
        cs = await _get_chat_settings(session, chat_id)
        auto_del = cs.auto_delete_commands if cs else True
    if auto_del:
        try:
            await message.delete()
        except TelegramAPIError:
            logger.warning("Alarm: cannot delete command message in chat %s", chat_id)

    # ── !alarm off ─────────────────────────────────────────────────────────
    if is_off:
        async with async_session() as session:
            cs = await _get_chat_settings(session, chat_id)
            if not cs.alarm_currently_active:
                # Alarm не активен — уведомляем модератора в DM
                await _send_alarm_dm(
                    bot=message.bot, user_id=mod.id,
                    text="ℹ️ Alarm не активен в чате — снимать нечего.",
                )
                return
            # v4.7.30: _deactivate_alarm теперь возвращает (ok, perms_source, slow_source)
            # — используем их для достоверного DM (Баг #5 аудита v4.7.30).
            ok, perms_source, slow_source = await _deactivate_alarm(
                session, cs, message.bot, chat_id, reason="manual",
            )
        if ok:
            # v4.7.30: переводим source-строки в человекочитаемые описания,
            # чтобы модератор понимал, ОТКУДА реально восстановлены права.
            # Раньше всегда писали "из snapshot" — это было неправдой если
            # был задан day_permissions preset.
            perms_desc = _alarm_perms_source_to_human(perms_source)
            slow_desc = _alarm_slow_source_to_human(slow_source)
            await _send_alarm_dm(
                bot=message.bot, user_id=mod.id,
                text=(
                    f"✅ Alarm снят в чате.\n"
                    f"• Права восстановлены: {perms_desc}\n"
                    f"• Slow mode восстановлен: {slow_desc}"
                ),
            )
            # v4.8.0: отправляем событие в modchat.
            try:
                from modchat import _send_alarm_event_to_modchat
                await _send_alarm_event_to_modchat(
                    bot=message.bot, chat_id=chat_id, event_type="off",
                    mod_user=mod,
                )
            except Exception as e:
                logger.debug("Modchat alarm-off event failed: %s", e)
        else:
            await _send_alarm_dm(
                bot=message.bot, user_id=mod.id,
                text=(
                    "⚠️ Alarm не удалось снять — ошибка при восстановлении прав. "
                    "Проверьте логи и при необходимости восстановите права вручную."
                ),
            )
        return

    # ── !alarm on ──────────────────────────────────────────────────────────
    # Парсим длительность (опциональная)
    duration_td: timedelta | None = None
    if duration_value and duration_unit:
        try:
            duration_td = _parse_alarm_duration(duration_value, duration_unit)
        except ValueError as e:
            await _send_alarm_dm(
                bot=message.bot, user_id=mod.id,
                text=f"❌ Некорректная длительность: {e}\n"
                     f"Поддерживаемые форматы: 1ч, 1h, 30м, 30m, 2д, 2d.",
            )
            return

    async with async_session() as session:
        cs = await _get_chat_settings(session, chat_id)

        # ── Проверка: нельзя включить alarm в night mode ──────────────────
        if cs.night_mode_currently_active:
            logger.info(
                "Alarm on rejected: chat %s is in night mode (mod=%s)",
                chat_id, mod.id,
            )
            await _send_alarm_dm(
                bot=message.bot, user_id=mod.id,
                text=(
                    "🌙 Сейчас активен ночной режим — !alarm включить нельзя.\n"
                    "Ночной режим уже ограничивает права чата (медиа отключены, "
                    "slow_mode активен). Alarm будет избыточен.\n"
                    "Дождитесь окончания ночного режима или отключите его."
                ),
            )
            return

        # ── Проверка: нельзя включить alarm в sanitary day ────────────────
        if cs.sanitary_days_currently_active:
            logger.info(
                "Alarm on rejected: chat %s is in sanitary day (mod=%s)",
                chat_id, mod.id,
            )
            await _send_alarm_dm(
                bot=message.bot, user_id=mod.id,
                text=(
                    "🚫 Сейчас активен санитарный день — !alarm включить нельзя.\n"
                    "Санитарный день уже ограничивает права чата (полный локдаун). "
                    "Alarm будет избыточен."
                ),
            )
            return

        # ── Если alarm уже активен — продлеваем/обновляем duration ────────
        if cs.alarm_currently_active:
            old_started_by = cs.alarm_started_by
            # v4.7.30: узнать кто включил alarm до нас — для DM (Баг #7 аудита
            # v4.7.30). Пытаемся найти профиль модератора в БД. Best-effort:
            # если не нашли (старая запись, удалённый юзер) — fallback на ID.
            prev_mod_display = f"id:{old_started_by}" if old_started_by else "неизвестно"
            if old_started_by:
                try:
                    prev_mod = (await session.execute(
                        select(Moderator).where(Moderator.tg_user_id == old_started_by)
                    )).scalar_one_or_none()
                    if prev_mod:
                        # Собираем "Имя @username (id:NNN)" — максимум информации.
                        parts = []
                        if prev_mod.first_name:
                            parts.append(prev_mod.first_name)
                        if prev_mod.username:
                            parts.append(f"@{prev_mod.username}")
                        parts.append(f"id:{old_started_by}")
                        prev_mod_display = " ".join(parts)
                except Exception as e:
                    logger.debug(
                        "Alarm extend: could not load prev moderator profile for %s: %s "
                        "(using fallback display)",
                        old_started_by, e,
                    )
            if duration_td:
                new_until = datetime.now(timezone.utc) + duration_td
                cs.alarm_active_until = new_until
                await session.commit()
                logger.info(
                    "Alarm extended: chat %s new_until=%s (mod=%s, prev_started_by=%s)",
                    chat_id, new_until.isoformat(), mod.id, old_started_by,
                )
                await _send_alarm_dm(
                    bot=message.bot, user_id=mod.id,
                    text=(
                        f"⏱ Alarm уже был активен — продлён до {_format_alarm_duration(duration_td)}.\n"
                        f"• Предыдущий alarm включил: {prev_mod_display}\n"
                        f"• Alarm active until: {new_until.isoformat()}"
                    ),
                )
                # v4.8.0: отправляем продление в modchat (с консолидацией).
                try:
                    from modchat import _send_alarm_event_to_modchat
                    await _send_alarm_event_to_modchat(
                        bot=message.bot, chat_id=chat_id, event_type="extend",
                        mod_user=mod,
                        duration_str=_format_alarm_duration(duration_td),
                        active_until=new_until,
                        prev_mod_display=prev_mod_display,
                    )
                except Exception as e:
                    logger.debug("Modchat alarm-extend event failed: %s", e)
            else:
                # Снимаем auto-off, alarm будет до ручного отключения
                cs.alarm_active_until = None
                await session.commit()
                logger.info(
                    "Alarm set to manual off: chat %s (was timed, mod=%s)",
                    chat_id, mod.id,
                )
                await _send_alarm_dm(
                    bot=message.bot, user_id=mod.id,
                    text=(
                        f"⏱ Alarm уже был активен — auto-off отключён.\n"
                        f"• Предыдущий alarm включил: {prev_mod_display}\n"
                        f"• Теперь alarm будет активен до ручного !alarm off."
                    ),
                )
                # v4.8.0: продление с пустой длительностью (auto-off отключён) —
                # тоже отправляем как extend в modchat, но без duration_str.
                try:
                    from modchat import _send_alarm_event_to_modchat
                    await _send_alarm_event_to_modchat(
                        bot=message.bot, chat_id=chat_id, event_type="extend",
                        mod_user=mod,
                        duration_str=None,
                        active_until=None,
                        prev_mod_display=prev_mod_display,
                    )
                except Exception as e:
                    logger.debug("Modchat alarm-extend (no duration) event failed: %s", e)
            return

        # ── Включаем alarm с нуля: snapshot → apply ───────────────────────
        # Шаг 1: snapshot текущих прав чата.
        # v4.8.0: используем унифицированную функцию из chat_modes.py.
        # НЕ передаём day_permissions — alarm должен сохранить «то что есть
        # сейчас», а не «то что должно быть днём по пресету». Это правильно,
        # потому что alarm — это экстренный режим поверх любого состояния.
        # При restore (через _deactivate_alarm) приоритет будет у preset'а.
        from chat_modes import _apply_chat_permissions as _v480_apply
        from chat_modes import _snapshot_chat_permissions as _v480_snapshot
        snapshot_perms: str | None = None
        snapshot_slow: int = 0
        try:
            snapshot_perms, snapshot_slow = await _v480_snapshot(
                bot=message.bot, chat_id=chat_id, day_permissions=None,
            )
        except TelegramAPIError as e:
            logger.error(
                "Alarm on: get_chat failed for chat %s: %s — cannot snapshot, aborting",
                chat_id, e,
            )
            await _send_alarm_dm(
                bot=message.bot, user_id=mod.id,
                text=f"❌ Не удалось получить текущие права чата: {e}",
            )
            return

        # Шаг 2: применяем alarm_permissions (v4.8.0: унифицированная обёртка).
        ok = await _v480_apply(message.bot, chat_id, _alarm_permissions())
        if not ok:
            logger.error(
                "Alarm on: set_chat_permissions failed for chat %s",
                chat_id,
            )
            await _send_alarm_dm(
                bot=message.bot, user_id=mod.id,
                text=(
                    "❌ Не удалось применить ограничения alarm.\n"
                    "У бота нет прав администратора в чате?"
                ),
            )
            return

        # Шаг 3: применяем slow_mode 30s.
        # v4.7.22: SetChatSlowModeDelay определён в bot_handlers.py (top-level),
        # импорт не нужен. Раньше был late import `from bot import SetChatSlowModeDelay`,
        # но это вызывал повторный import bot.py как отдельного модуля (bot.py запускается
        # как __main__) → RuntimeError: Router is already attached.
        try:
            await message.bot(SetChatSlowModeDelay(
                chat_id=chat_id, slow_mode_delay=_ALARM_SLOW_MODE_DELAY,
            ))
        except TelegramAPIError as e:
            logger.warning(
                "Alarm on: set_chat_slow_mode_delay failed for chat %s: %s "
                "(continuing — perms already applied)",
                chat_id, e,
            )

        # Шаг 4: сохраняем состояние в БД
        cs.alarm_currently_active = True
        cs.alarm_saved_permissions = snapshot_perms
        cs.alarm_saved_slow_mode_delay = snapshot_slow
        cs.alarm_started_by = mod.id
        if duration_td:
            cs.alarm_active_until = datetime.now(timezone.utc) + duration_td
        else:
            cs.alarm_active_until = None
        await session.commit()

        logger.info(
            "Alarm activated: chat %s mod=%s duration=%s snapshot_slow=%s",
            chat_id, mod.id,
            _format_alarm_duration(duration_td) if duration_td else "manual",
            snapshot_slow,
        )

    # Шаг 5: DM модератору
    if duration_td:
        await _send_alarm_dm(
            bot=message.bot, user_id=mod.id,
            text=(
                f"🚨 Alarm включён в чате на {_format_alarm_duration(duration_td)}.\n"
                f"• Медиа отключены (только текст)\n"
                f"• Slow mode: {_ALARM_SLOW_MODE_DELAY} сек\n"
                f"• Auto-off: {cs.alarm_active_until.isoformat() if cs.alarm_active_until else 'N/A'}\n\n"
                f"Снять раньше: !alarm off"
            ),
        )
    else:
        await _send_alarm_dm(
            bot=message.bot, user_id=mod.id,
            text=(
                f"🚨 Alarm включён в чате (без авто-отключения).\n"
                f"• Медиа отключены (только текст)\n"
                f"• Slow mode: {_ALARM_SLOW_MODE_DELAY} сек\n\n"
                f"Снять: !alarm off"
            ),
        )

    # v4.8.0: отправляем событие в modchat (если задан).
    # Не падаем если modchat не задан — это нормально, DM остаётся как раньше.
    try:
        from modchat import _send_alarm_event_to_modchat
        await _send_alarm_event_to_modchat(
            bot=message.bot, chat_id=chat_id, event_type="on",
            mod_user=mod,
            duration_str=_format_alarm_duration(duration_td) if duration_td else None,
            active_until=cs.alarm_active_until,
        )
    except Exception as e:
        logger.debug("Modchat alarm-on event failed: %s", e)


async def _send_alarm_dm(bot: types.Bot, user_id: int, text: str) -> None:
    """Отправляет DM модератору с уведомлением об alarm.

    Если юзер не запускал бота в DM (Forbidden) — логируем warning.
    Не падаем, не пробрасываем исключение выше.
    """
    try:
        await bot.send_message(chat_id=user_id, text=text, parse_mode="HTML")
    except TelegramAPIError as e:
        logger.warning(
            "Alarm DM to user %s failed: %s (user may not have started bot in DM)",
            user_id, e,
        )
    except Exception as e:
        logger.warning("Alarm DM to user %s unexpected error: %s", user_id, e)


# ═══════════════════════════════════════════════════════════════════════════
# Обработчики команд в ЛИЧКЕ (настройки)
# ═══════════════════════════════════════════════════════════════════════════

@router.message(F.chat.type == "private", Command("addadmin"))
async def cmd_addadmin(message: types.Message) -> None:
    """Добавляет админа в чат. Формат: /addadmin <chat_id> <user_id>"""
    if message.from_user.id not in ADMIN_IDS:
        return

    parts = message.text.split()
    if len(parts) < 3:
        await message.reply("📋 Формат: /addadmin chat_id user_id", parse_mode=None)
        return

    try:
        chat_id = int(parts[1])
        user_id = int(parts[2])
    except ValueError:
        await message.reply("❌ chat_id и user_id должны быть числами")
        return

    async with async_session() as session:
        # Проверяем, не добавлен ли уже
        stmt = select(ChatAdmin).where(
            ChatAdmin.chat_id == chat_id,
            ChatAdmin.user_id == user_id,
        )
        result = await session.execute(stmt)
        if result.scalar_one_or_none():
            await message.reply(f"⚠️ Пользователь {user_id} уже админ в чате {chat_id}")
            return

        admin = ChatAdmin(
            chat_id=chat_id, user_id=user_id,
            added_by=message.from_user.id,
        )
        session.add(admin)
        await session.commit()

    await message.reply(f"✅ Пользователь {user_id} добавлен в админы чата {chat_id}")


@router.message(F.chat.type == "private", Command("deladmin"))
async def cmd_deladmin(message: types.Message) -> None:
    """Убирает админа из чата. Формат: /deladmin <chat_id> <user_id>"""
    if message.from_user.id not in ADMIN_IDS:
        return

    parts = message.text.split()
    if len(parts) < 3:
        await message.reply("📋 Формат: /deladmin chat_id user_id", parse_mode=None)
        return

    try:
        chat_id = int(parts[1])
        user_id = int(parts[2])
    except ValueError:
        await message.reply("❌ chat_id и user_id должны быть числами")
        return

    async with async_session() as session:
        stmt = select(ChatAdmin).where(
            ChatAdmin.chat_id == chat_id,
            ChatAdmin.user_id == user_id,
        )
        result = await session.execute(stmt)
        admin = result.scalar_one_or_none()
        if not admin:
            await message.reply(f"⚠️ Пользователь {user_id} не админ в чате {chat_id}")
            return

        await session.delete(admin)
        await session.commit()

    await message.reply(f"✅ Пользователь {user_id} убран из админов чата {chat_id}")


@router.message(F.chat.type == "private", Command("sethashtag"))
async def cmd_sethashtag(message: types.Message) -> None:
    """Устанавливает хэштег чата. Формат: /sethashtag <chat_id> #хэштег"""
    if message.from_user.id not in ADMIN_IDS:
        return

    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        await message.reply("📋 Формат: /sethashtag chat_id #хэштег", parse_mode=None)
        return

    try:
        chat_id = int(parts[1])
    except ValueError:
        await message.reply("❌ chat_id должен быть числом")
        return

    hashtag = parts[2].strip()
    if not hashtag.startswith("#"):
        hashtag = "#" + hashtag

    async with async_session() as session:
        settings = await _get_chat_settings(session, chat_id)
        settings.hashtag = hashtag
        await session.commit()

    await message.reply(f"✅ Хэштег чата {chat_id}: {hashtag}")


@router.message(F.chat.type == "private", Command("warns_mute"))
async def cmd_warns_mute(message: types.Message) -> None:
    """Устанавливает порог варнов до автомьюта. Формат: /warns_mute <chat_id> <число>"""
    if message.from_user.id not in ADMIN_IDS:
        return

    parts = message.text.split()
    if len(parts) < 3:
        await message.reply("📋 Формат: /warns_mute chat_id число\n💡 0 = отключить автомьют", parse_mode=None)
        return

    try:
        chat_id = int(parts[1])
        count = int(parts[2])
    except ValueError:
        await message.reply("❌ chat_id и число должны быть числами")
        return

    async with async_session() as session:
        settings = await _get_chat_settings(session, chat_id)
        settings.warns_to_mute = count
        await session.commit()

    status = f"{count} варнов" if count > 0 else "отключён"
    await message.reply(f"✅ Автомьют в чате {chat_id}: {status}")


@router.message(F.chat.type == "private", Command("warns_ban"))
async def cmd_warns_ban(message: types.Message) -> None:
    """Устанавливает порог варнов до автобана. Формат: /warns_ban <chat_id> <число>"""
    if message.from_user.id not in ADMIN_IDS:
        return

    parts = message.text.split()
    if len(parts) < 3:
        await message.reply("📋 Формат: /warns_ban chat_id число\n💡 0 = отключить автобан", parse_mode=None)
        return

    try:
        chat_id = int(parts[1])
        count = int(parts[2])
    except ValueError:
        await message.reply("❌ chat_id и число должны быть числами")
        return

    async with async_session() as session:
        settings = await _get_chat_settings(session, chat_id)
        settings.warns_to_ban = count
        await session.commit()

    status = f"{count} варнов" if count > 0 else "отключён"
    await message.reply(f"✅ Автобан в чате {chat_id}: {status}")


@router.message(F.chat.type == "private", Command("mute_duration"))
async def cmd_mute_duration(message: types.Message) -> None:
    """Устанавливает длительность автомьюта. Формат: /mute_duration <chat_id> <1d/2h/30m>"""
    if message.from_user.id not in ADMIN_IDS:
        return

    parts = message.text.split()
    if len(parts) < 3:
        await message.reply("📋 Формат: /mute_duration chat_id 1d/2h/30m", parse_mode=None)
        return

    try:
        chat_id = int(parts[1])
    except ValueError:
        await message.reply("❌ chat_id должен быть числом")
        return

    duration = _parse_duration(parts[2])
    if duration is None:
        await message.reply("❌ Неверный формат длительности. Пример: 1d, 2h, 30m, 1д, 2ч, 30м, 1d12h30m")
        return

    async with async_session() as session:
        settings = await _get_chat_settings(session, chat_id)
        settings.mute_duration_seconds = duration
        await session.commit()

    await message.reply(f"✅ Длительность автомьюта в чате {chat_id}: {_format_duration(duration)}")


@router.message(F.chat.type == "private", Command("setreport"))
async def cmd_setreport(message: types.Message) -> None:
    """Задает чат для отчётов.

    Форматы:
      /setreport default <report_chat_id> — глобальный для ВСЕХ чатов
      /setreport <chat_id> <report_chat_id> — индивидуально для чата
      /setreport default 0 — сбросить глобальный (отчёты отключены)
      /setreport <chat_id> 0 — сбросить индивидуальный (fallback на default)
    """
    if message.from_user.id not in ADMIN_IDS:
        return

    parts = message.text.split()
    if len(parts) < 3:
        await message.reply(
            "📋 Форматы:\n"
            "  /setreport default report_chat_id — для ВСЕХ чатов\n"
            "  /setreport chat_id report_chat_id — для конкретного чата\n"
            "  0 = сбросить (default fallback или отключить)",
            parse_mode=None,
        )
        return

    arg1 = parts[1]
    arg2 = parts[2]

    # Определяем: default или конкретный chat_id
    is_default = arg1.lower() in ("default", "все", "all")
    if is_default:
        target_chat_id = 0  # Специальный ID — глобальный default
    else:
        try:
            target_chat_id = int(arg1)
        except ValueError:
            await message.reply("❌ chat_id должен быть числом или 'default'", parse_mode=None)
            return

    try:
        report_chat_id = int(arg2)
    except ValueError:
        await message.reply("❌ report_chat_id должен быть числом", parse_mode=None)
        return

    async with async_session() as session:
        settings = await _get_chat_settings(session, target_chat_id)
        if report_chat_id == 0:
            # Сброс
            settings.report_chat_id = None
            await session.commit()
            if is_default:
                await message.reply("✅ Глобальный репорт-чат сброшен (отчёты отключены)", parse_mode=None)
            else:
                await message.reply(f"✅ Репорт-чат для {target_chat_id} сброшен → fallback на default", parse_mode=None)
        else:
            # Проверяем, что бот может достучаться до указанного чата
            try:
                await message.bot.get_chat(report_chat_id)
            except TelegramAPIError as e:
                await message.reply(
                    f"❌ Бот не может найти чат {report_chat_id}.\n"
                    f"Убедитесь, что бот добавлен в этот чат и имеет права отправки.\n"
                    f"Ошибка: {e}",
                    parse_mode=None,
                )
                return

            settings.report_chat_id = report_chat_id
            await session.commit()
            scope = "глобальный (для всех чатов)" if is_default else f"для чата {target_chat_id}"
            await message.reply(f"✅ Репорт-чат ({scope}): {report_chat_id}", parse_mode=None)


@router.message(F.chat.type == "private", Command("settings"))
async def cmd_settings(message: types.Message) -> None:
    """Показывает текущие настройки чата. Формат: /settings <chat_id>"""
    if message.from_user.id not in ADMIN_IDS:
        return

    parts = message.text.split()
    if len(parts) < 2:
        await message.reply("📋 Формат: /settings chat_id", parse_mode=None)
        return

    try:
        chat_id = int(parts[1])
    except ValueError:
        await message.reply("❌ chat_id должен быть числом")
        return

    async with async_session() as session:
        settings = await _get_chat_settings(session, chat_id)
        admins = await _get_chat_admins(session, chat_id)

    hashtag = settings.hashtag or "(не задан)"
    report_chat_str = f"<code>{settings.report_chat_id}</code>" if settings.report_chat_id else "(не задан — fallback на default)"
    warns_mute = f"{settings.warns_to_mute} варнов" if settings.warns_to_mute > 0 else "отключён"
    warns_ban = f"{settings.warns_to_ban} варнов" if settings.warns_to_ban > 0 else "отключён"
    mute_dur = _format_duration(settings.mute_duration_seconds or 3600)

    admin_list = ""
    if admins:
        admin_lines = []
        for a in admins:
            admin_lines.append(f"  • <code>{a.user_id}</code> (добавил: <code>{a.added_by or '?'}</code>)")
        admin_list = "\n👤 Доп. админы:\n" + "\n".join(admin_lines)

    text = (
        f"⚙️ <b>Настройки чата</b> <code>{chat_id}</code>\n\n"
        f"🏷 Хэштег: {hashtag}\n"
        f"📢 Репорт-чат: {report_chat_str}\n"
        f"⚠️ Варнов до мьюта: {warns_mute}\n"
        f"⚠️ Варнов до бана: {warns_ban}\n"
        f"⏱ Длительность мьюта: {mute_dur}\n"
        f"{admin_list}"
    )

    await message.reply(text, parse_mode="HTML")


# ═══════════════════════════════════════════════════════════════════════════
# v4.5.2 — Команды управления фильтрами (в личке боту, только ADMIN_IDS)
# ═══════════════════════════════════════════════════════════════════════════


@router.message(F.chat.type == "private", Command("bansticker"))
async def cmd_bansticker(message: types.Message) -> None:
    """v4.5.2 (#15): /bansticker <pack_or_link> [punishment] [duration]

    Добавляет стикерпак в бан-лист. punishment: delete|warn|mute|ban (default: delete).
    Для mute — длительность в формате 1d/2h/30m.
    """
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = message.text.split(maxsplit=3)
    if len(parts) < 2:
        await message.reply(
            "📋 Формат: /bansticker <pack_or_link> [delete|warn|mute|ban] [dur]\n"
            "💡 pack_or_link — имя пака или https://t.me/addstickers/<name>\n"
            "💡 dur — для mute (1d/2h/30m, по умолчанию 1h)",
            parse_mode=None,
        )
        return

    pack_arg = parts[1]
    pack_name = _parse_sticker_pack_link(pack_arg)
    if not pack_name:
        await message.reply(
            f"❌ Не удалось распознать pack_name из: {pack_arg}\n"
            f"💡 Форматы: <pack_name> или https://t.me/addstickers/<pack_name>",
            parse_mode=None,
        )
        return

    punishment = "delete"
    mute_dur = None
    if len(parts) >= 3:
        punishment = parts[2].lower().strip()
        if punishment not in ("delete", "warn", "mute", "ban"):
            await message.reply(
                f"❌ punishment должен быть delete/warn/mute/ban (получили '{punishment}')",
                parse_mode=None,
            )
            return
    if punishment == "mute":
        if len(parts) >= 4:
            mute_dur = _parse_duration(parts[3])
            if mute_dur is None:
                await message.reply(
                    f"❌ Не удалось распознать длительность: {parts[3]}\n"
                    f"💡 Формат: 1d, 2h, 30m, 1d12h",
                    parse_mode=None,
                )
                return
        else:
            mute_dur = 3600  # 1h default

    # По умолчанию — global (chat_id=0)
    try:
        async with async_session() as session:
            await _add_banned_sticker_pack(
                session,
                chat_id=0,
                pack_name=pack_name,
                punishment=punishment,
                mute_duration=mute_dur,
                reason=f"Added via /bansticker by {message.from_user.id}",
                added_by_mod_id=message.from_user.id,
                added_via="manual",
            )
        dur_str = f", duration: {_format_duration(mute_dur)}" if mute_dur else ""
        await message.reply(
            f"✅ Стикерпак <code>{pack_name}</code> добавлен в бан-лист (global)\n"
            f"Punishment: <b>{punishment}</b>{dur_str}",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error("/bansticker failed: %s", e)
        await message.reply(f"❌ Ошибка: {e}", parse_mode="HTML")


@router.message(F.chat.type == "private", Command("liststickers"))
async def cmd_liststickers(message: types.Message) -> None:
    """v4.5.2 (#15): /liststickers [chat_id] — показать забаненные стикерпаки."""
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = message.text.split(maxsplit=1)
    chat_filter = None
    if len(parts) >= 2:
        try:
            chat_filter = int(parts[1])
        except ValueError:
            await message.reply("❌ chat_id должен быть числом", parse_mode=None)
            return

    async with async_session() as session:
        stmt = select(BannedStickerPack).where(BannedStickerPack.is_active.is_(True))
        if chat_filter is not None:
            stmt = stmt.where(BannedStickerPack.chat_id == chat_filter)
        stmt = stmt.order_by(BannedStickerPack.chat_id.asc(), BannedStickerPack.created_at.desc())
        packs = (await session.execute(stmt)).scalars().all()

    if not packs:
        await message.reply("📭 Нет забаненных стикерпаков.", parse_mode=None)
        return

    lines = ["🎭 <b>Забаненные стикерпаки</b>:\n"]
    for p in packs:
        scope = "global" if p.chat_id == 0 else f"chat {p.chat_id}"
        dur_str = f", dur: {_format_duration(p.mute_duration)}" if p.mute_duration else ""
        lines.append(
            f"  • <code>{p.pack_name}</code> [{scope}] — {p.punishment}{dur_str}"
        )
    await message.reply("\n".join(lines), parse_mode="HTML")


@router.message(F.chat.type == "private", Command("delsticker"))
async def cmd_delsticker(message: types.Message) -> None:
    """v4.5.2 (#15): /delsticker <pack_name> [chat_id] — убрать пак из бан-листа."""
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = message.text.split(maxsplit=2)
    if len(parts) < 2:
        await message.reply(
            "📋 Формат: /delsticker <pack_name> [chat_id]\n"
            "💡 Без chat_id — убирает из global (chat_id=0)",
            parse_mode=None,
        )
        return

    pack_name = parts[1]
    chat_id = 0
    if len(parts) >= 3:
        try:
            chat_id = int(parts[2])
        except ValueError:
            await message.reply("❌ chat_id должен быть числом", parse_mode=None)
            return

    async with async_session() as session:
        pack = (await session.execute(
            select(BannedStickerPack).where(
                BannedStickerPack.pack_name == pack_name,
                BannedStickerPack.chat_id == chat_id,
                BannedStickerPack.is_active.is_(True),
            )
        )).scalar_one_or_none()
        if pack is None:
            await message.reply(
                f"⚠️ Пак <code>{pack_name}</code> не найден в бан-листе (chat_id={chat_id})",
                parse_mode="HTML",
            )
            return
        pack.is_active = False
        await session.commit()

    scope = "global" if chat_id == 0 else f"chat {chat_id}"
    await message.reply(
        f"✅ Пак <code>{pack_name}</code> убран из бан-листа [{scope}]",
        parse_mode="HTML",
    )


# v4.8.6: stub-команды /addword, /delword, /listwords удалены окончательно.
# WordFilter был объявлен deprecated в v4.8.0 и заменён на KeywordWatch
# (команды !addkeyword/!delkeyword/!listkeywords + веб-панель /admin/keywords).
# Сами модели WordFilter и таблица word_filters остаются активными —
# они используются web UI в /admin/presets (Word filter section).


@router.message(F.chat.type == "private", Command("linkfilter"))
async def cmd_linkfilter(message: types.Message) -> None:
    """v4.5.2 (#8): /linkfilter chat_id on|off — включить/выключить link filter."""
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        await message.reply(
            "📋 Формат: /linkfilter chat_id on|off\n"
            "💡 on — блокировать ссылки кроме allowlist\n"
            "    off — не фильтровать ссылки",
            parse_mode=None,
        )
        return

    try:
        chat_id = int(parts[1])
    except ValueError:
        await message.reply("❌ chat_id должен быть числом", parse_mode=None)
        return

    flag = parts[2].lower().strip()
    if flag not in ("on", "off", "1", "0", "true", "false", "yes", "no"):
        await message.reply("❌ Должно быть on/off", parse_mode=None)
        return
    enabled = flag in ("on", "1", "true", "yes")

    async with async_session() as session:
        settings = await _get_chat_settings(session, chat_id)
        settings.link_filter_enabled = enabled
        await session.commit()

    status = "включён" if enabled else "выключен"
    await message.reply(
        f"✅ Link filter в чате {chat_id}: <b>{status}</b>",
        parse_mode="HTML",
    )


@router.message(F.chat.type == "private", Command("linkallow"))
async def cmd_linkallow(message: types.Message) -> None:
    """v4.5.2 (#8): /linkallow chat_id|global <domain> — добавить домен в allowlist."""
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        await message.reply(
            "📋 Формат: /linkallow chat_id|global <domain>\n"
            "💡 Пример: /linkallow global github.com\n"
            "    /linkallow -1001234567890 my-site.ru",
            parse_mode=None,
        )
        return

    scope_arg = parts[1].lower().strip()
    if scope_arg == "global":
        chat_id = 0
    else:
        try:
            chat_id = int(scope_arg)
        except ValueError:
            await message.reply(
                "❌ chat_id должен быть числом или 'global'",
                parse_mode=None,
            )
            return

    domain = parts[2].strip().lower()
    # Простая нормализация — убираем scheme и path если есть
    if "://" in domain:
        try:
            parsed = urlparse(domain if "://" in domain else "https://" + domain)
            domain = parsed.netloc.lower()
        except ValueError:
            pass
    domain = domain.lstrip("@").strip("/")
    if not domain or "." not in domain:
        await message.reply(
            f"❌ Некорректный домен: {parts[2]}\n"
            f"💡 Формат: t.me, github.com, my-site.ru (без scheme и path)",
            parse_mode=None,
        )
        return

    async with async_session() as session:
        # Проверяем дубликат
        existing = (await session.execute(
            select(LinkAllowlist).where(
                LinkAllowlist.chat_id == chat_id,
                LinkAllowlist.domain == domain,
            )
        )).scalar_one_or_none()
        if existing:
            scope = "global" if chat_id == 0 else f"chat {chat_id}"
            await message.reply(
                f"⚠️ Домен <code>{domain}</code> уже в allowlist [{scope}]",
                parse_mode="HTML",
            )
            return
        session.add(LinkAllowlist(
            chat_id=chat_id,
            domain=domain,
            created_by=message.from_user.id,
        ))
        await session.commit()

    scope = "global" if chat_id == 0 else f"chat {chat_id}"
    await message.reply(
        f"✅ Домен <code>{domain}</code> добавлен в allowlist [{scope}]",
        parse_mode="HTML",
    )


@router.message(F.chat.type == "private", Command("linkallowlist"))
async def cmd_linkallowlist(message: types.Message) -> None:
    """v4.5.2 (#8): /linkallowlist [chat_id] — показать allowlist."""
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = message.text.split(maxsplit=1)
    chat_filter = None
    if len(parts) >= 2:
        scope_arg = parts[1].lower().strip()
        if scope_arg == "global":
            chat_filter = 0
        else:
            try:
                chat_filter = int(scope_arg)
            except ValueError:
                await message.reply("❌ chat_id должен быть числом или 'global'", parse_mode=None)
                return

    async with async_session() as session:
        stmt = select(LinkAllowlist)
        if chat_filter is not None:
            stmt = stmt.where(LinkAllowlist.chat_id == chat_filter)
        stmt = stmt.order_by(LinkAllowlist.chat_id.asc(), LinkAllowlist.created_at.asc())
        rows = (await session.execute(stmt)).scalars().all()

    if not rows:
        await message.reply("📭 Allowlist пуст.", parse_mode=None)
        return

    lines = ["🔗 <b>Link allowlist</b>:\n"]
    for r in rows:
        scope = "global" if r.chat_id == 0 else f"chat {r.chat_id}"
        lines.append(f"  • <code>{r.domain}</code> [{scope}]")
    await message.reply("\n".join(lines), parse_mode="HTML")


# ── v4.8.0: Keyword-watch + Modchat команды ─────────────────────────────────
# !setkeywords word1,word2,word3 — полная замена списка (SU-only).
# !addkeyword «фраза» [--ban-night] — добавить фразу (SU-only).
# !delkeyword «фраза» — удалить фразу (SU-only).
# !listkeywords — показать список (SU-only).
# !setmodchat <chat_id> — назначить чат как modchat (SU-only).

@router.message(F.chat.type == "private", Command("setkeywords"))
async def cmd_setkeywords(message: types.Message) -> None:
    """v4.8.0: !setkeywords word1,word2,word3 — полная замена списка фраз.

    SU-only. Фразы с пробелами нужно заключать в кавычки или использовать
    запятую как разделитель. Опциональный суффикс --ban-night после фразы
    включает автобан ночью для этой фразы.

    Примеры:
      !setkeywords казино, "срал в торт детишкам" --ban-night, @admin
    """
    if message.from_user.id not in ADMIN_IDS:
        return
    parts_str = message.text or ""
    # Убираем команду из строки.
    if parts_str.startswith("/setkeywords"):
        parts_str = parts_str[len("/setkeywords"):].strip()
    elif parts_str.startswith("!setkeywords"):
        parts_str = parts_str[len("!setkeywords"):].strip()
    if not parts_str:
        await message.reply(
            "📋 Формат: !setkeywords word1, word2, \"фраза с пробелом\" [--ban-night]\n"
            "Полная замена списка keyword-watch фраз. Старые фразы будут удалены.",
            parse_mode=None,
        )
        return
    # Парсим фразы через запятую.
    phrases_raw = [p.strip() for p in parts_str.split(",") if p.strip()]
    parsed: list[tuple[str, bool]] = []
    for ph in phrases_raw:
        ban_night = False
        if ph.endswith("--ban-night"):
            ban_night = True
            ph = ph[:-len("--ban-night")].strip()
        # Убираем кавычки если есть.
        if (ph.startswith('"') and ph.endswith('"')) or (ph.startswith("'") and ph.endswith("'")):
            ph = ph[1:-1]
        if ph:
            parsed.append((ph, ban_night))
    if not parsed:
        await message.reply("❌ Не удалось распарсить фразы.", parse_mode=None)
        return
    try:
        from db import KeywordWatch
        async with async_session() as session:
            # Удаляем все старые фразы.
            existing = (await session.execute(
                select(KeywordWatch).where(KeywordWatch.chat_id == 0)
            )).scalars().all()
            for kw in existing:
                await session.delete(kw)
            # Добавляем новые.
            for ph, ban_night in parsed:
                session.add(KeywordWatch(
                    chat_id=0, phrase=ph, ban_in_night_mode=ban_night,
                    created_by=message.from_user.id,
                ))
            await session.commit()
    except Exception as e:
        logger.error("setkeywords failed: %s", e)
        await message.reply(f"❌ Ошибка: {e}", parse_mode=None)
        return
    await message.reply(
        f"✅ Keyword-watch список обновлён ({len(parsed)} фраз).\n"
        f"Из них с автобаном ночью: {sum(1 for _, b in parsed if b)}",
        parse_mode=None,
    )


@router.message(F.chat.type == "private", Command("addkeyword"))
async def cmd_addkeyword(message: types.Message) -> None:
    """v4.8.0: !addkeyword «фраза» [--ban-night] — добавить фразу."""
    if message.from_user.id not in ADMIN_IDS:
        return
    parts_str = message.text or ""
    if parts_str.startswith("/addkeyword"):
        parts_str = parts_str[len("/addkeyword"):].strip()
    elif parts_str.startswith("!addkeyword"):
        parts_str = parts_str[len("!addkeyword"):].strip()
    if not parts_str:
        await message.reply(
            "📋 Формат: !addkeyword \"фраза\" [--ban-night]\n"
            "Добавляет фразу в keyword-watch список.",
            parse_mode=None,
        )
        return
    ban_night = False
    if parts_str.endswith("--ban-night"):
        ban_night = True
        parts_str = parts_str[:-len("--ban-night")].strip()
    if (parts_str.startswith('"') and parts_str.endswith('"')) or (parts_str.startswith("'") and parts_str.endswith("'")):
        parts_str = parts_str[1:-1]
    if not parts_str:
        await message.reply("❌ Пустая фраза.", parse_mode=None)
        return
    try:
        from db import KeywordWatch
        async with async_session() as session:
            # Проверяем, есть ли уже такая фраза (case-insensitive).
            existing = (await session.execute(
                select(KeywordWatch).where(
                    KeywordWatch.chat_id == 0,
                    KeywordWatch.is_active.is_(True),
                )
            )).scalars().all()
            for kw in existing:
                if kw.phrase.lower() == parts_str.lower():
                    # Обновляем ban_in_night_mode если различается.
                    if kw.ban_in_night_mode != ban_night:
                        kw.ban_in_night_mode = ban_night
                        await session.commit()
                        await message.reply(
                            f"✅ Фраза «{parts_str}» уже была в списке — "
                            f"обновлён флаг ban_in_night_mode={ban_night}.",
                            parse_mode=None,
                        )
                    else:
                        await message.reply(
                            f"ℹ️ Фраза «{parts_str}» уже в списке.",
                            parse_mode=None,
                        )
                    return
            session.add(KeywordWatch(
                chat_id=0, phrase=parts_str, ban_in_night_mode=ban_night,
                created_by=message.from_user.id,
            ))
            await session.commit()
    except Exception as e:
        logger.error("addkeyword failed: %s", e)
        await message.reply(f"❌ Ошибка: {e}", parse_mode=None)
        return
    suffix = " (с автобаном ночью)" if ban_night else ""
    await message.reply(f"✅ Добавлена фраза «{parts_str}»{suffix}.", parse_mode=None)


@router.message(F.chat.type == "private", Command("delkeyword"))
async def cmd_delkeyword(message: types.Message) -> None:
    """v4.8.0: !delkeyword «фраза» — удалить фразу."""
    if message.from_user.id not in ADMIN_IDS:
        return
    parts_str = message.text or ""
    if parts_str.startswith("/delkeyword"):
        parts_str = parts_str[len("/delkeyword"):].strip()
    elif parts_str.startswith("!delkeyword"):
        parts_str = parts_str[len("!delkeyword"):].strip()
    if not parts_str:
        await message.reply("📋 Формат: !delkeyword \"фраза\"", parse_mode=None)
        return
    if (parts_str.startswith('"') and parts_str.endswith('"')) or (parts_str.startswith("'") and parts_str.endswith("'")):
        parts_str = parts_str[1:-1]
    try:
        from db import KeywordWatch
        async with async_session() as session:
            existing = (await session.execute(
                select(KeywordWatch).where(
                    KeywordWatch.chat_id == 0,
                    KeywordWatch.is_active.is_(True),
                )
            )).scalars().all()
            found = None
            for kw in existing:
                if kw.phrase.lower() == parts_str.lower():
                    found = kw
                    break
            if found is None:
                await message.reply(f"❌ Фраза «{parts_str}» не найдена.", parse_mode=None)
                return
            found.is_active = False
            await session.commit()
    except Exception as e:
        logger.error("delkeyword failed: %s", e)
        await message.reply(f"❌ Ошибка: {e}", parse_mode=None)
        return
    await message.reply(f"✅ Удалена фраза «{parts_str}».", parse_mode=None)


@router.message(F.chat.type == "private", Command("listkeywords"))
async def cmd_listkeywords(message: types.Message) -> None:
    """v4.8.0: !listkeywords — показать список фраз."""
    if message.from_user.id not in ADMIN_IDS:
        return
    try:
        from db import KeywordWatch
        async with async_session() as session:
            rows = (await session.execute(
                select(KeywordWatch).where(
                    KeywordWatch.chat_id == 0,
                    KeywordWatch.is_active.is_(True),
                ).order_by(KeywordWatch.created_at.asc())
            )).scalars().all()
    except Exception as e:
        logger.error("listkeywords failed: %s", e)
        await message.reply(f"❌ Ошибка: {e}", parse_mode=None)
        return
    if not rows:
        await message.reply("ℹ️ Список keyword-watch пуст.", parse_mode=None)
        return
    lines = ["<b>Keyword-watch фразы:</b>"]
    for i, kw in enumerate(rows, 1):
        suffix = " 🌙ban" if kw.ban_in_night_mode else ""
        lines.append(f"{i}. <code>{html.escape(kw.phrase)}</code>{suffix}")
    await message.reply("\n".join(lines), parse_mode="HTML")


@router.message(F.chat.type == "private", Command("setmodchat"))
async def cmd_setmodchat(message: types.Message) -> None:
    """v4.8.0: !setmodchat <chat_id> — назначить чат как modchat.

    SU-only. Принимает chat_id числом. 0 = сбросить (снять modchat).
    Проверяет что чат не является уже report_chat (взаимоисключение).
    """
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.reply(
            "📋 Формат: !setmodchat <chat_id>\n"
            "0 = сбросить modchat.",
            parse_mode=None,
        )
        return
    try:
        target_chat_id = int(parts[1])
    except ValueError:
        await message.reply("❌ chat_id должен быть числом", parse_mode=None)
        return

    if target_chat_id == 0:
        # Сброс — снимаем is_mod_chat со всех чатов, чистим mod_chat_id.
        async with async_session() as session:
            mods = (await session.execute(
                select(ChatSettings).where(ChatSettings.is_mod_chat.is_(True))
            )).scalars().all()
            for cs in mods:
                cs.is_mod_chat = False
            # Также чистим mod_chat_id во всех чатах.
            all_cs = (await session.execute(
                select(ChatSettings).where(ChatSettings.mod_chat_id.is_not(None))
            )).scalars().all()
            for cs in all_cs:
                cs.mod_chat_id = None
            await session.commit()
        await message.reply("✅ Modchat сброшен.", parse_mode=None)
        return

    # Проверяем что чат существует и бот в нём состоит.
    try:
        chat_info = await message.bot.get_chat(chat_id=target_chat_id)
    except TelegramAPIError as e:
        await message.reply(
            f"❌ Бот не может найти чат {target_chat_id}.\n"
            f"Возможно, бот не добавлен в чат или чат удалён.\n"
            f"Ошибка: {e}",
            parse_mode=None,
        )
        return

    # Проверяем взаимоисключение с report_chat.
    async with async_session() as session:
        # Если этот чат уже является report_chat — отказ.
        target_cs = (await session.execute(
            select(ChatSettings).where(ChatSettings.chat_id == target_chat_id)
        )).scalar_one_or_none()
        if target_cs and target_cs.is_report_chat:
            await message.reply(
                f"❌ Чат «{chat_info.title or target_chat_id}» уже является "
                f"репорт-чатом. Назначить его как modchat нельзя "
                f"(взаимоисключение).",
                parse_mode=None,
            )
            return
        # Снимаем is_mod_chat со всех других чатов.
        mods = (await session.execute(
            select(ChatSettings).where(ChatSettings.is_mod_chat.is_(True))
        )).scalars().all()
        for cs in mods:
            cs.is_mod_chat = False
        # Назначаем новый modchat.
        if target_cs is None:
            target_cs = ChatSettings(chat_id=target_chat_id)
            session.add(target_cs)
        target_cs.is_mod_chat = True
        target_cs.title = chat_info.title or target_cs.title
        # Также устанавливаем mod_chat_id для global default (chat_id=0).
        default_cs = (await session.execute(
            select(ChatSettings).where(ChatSettings.chat_id == 0)
        )).scalar_one_or_none()
        if default_cs is None:
            default_cs = ChatSettings(chat_id=0)
            session.add(default_cs)
        default_cs.mod_chat_id = target_chat_id
        await session.commit()
    await message.reply(
        f"✅ Modchat назначен: «{chat_info.title or target_chat_id}» "
        f"({target_chat_id}).",
        parse_mode=None,
    )


# ═══════════════════════════════════════════════════════════════════════════
# v4.8.5: !idea <текст> — отправка идеи в GitHub Issue + Project v2
# ═══════════════════════════════════════════════════════════════════════════

# Лимит длины текста идеи. Краткость — сестра таланта.
_IDEA_MAX_LEN = 200

# Текущая версия бота — для записи в idea_log.bot_version.
# (читаем лениво, чтобы не тянуть круговый import web_app → bot_handlers.)
def _bot_version() -> str:
    try:
        import web_app
        return getattr(web_app, "APP_VERSION", "v4.8.5")
    except Exception:
        return "v4.8.5"


async def _resolve_sender_web_user(user_id: int) -> WebUser | None:
    """Ищем активный WebUser по tg_user_id. None если нет/деактивирован."""
    async with async_session() as session:
        wu = (await session.execute(
            select(WebUser).where(WebUser.tg_user_id == user_id)
        )).scalar_one_or_none()
        if wu is None or not wu.is_active:
            return None
        return wu


def _build_display_name(wu: WebUser, fallback_user: types.User) -> str:
    """Имя для алерта SU в DM: приоритет — first+last из WebUser,
    затем first+last из Telegram, затем @username, в крайнем случае — tg_user_id.
    """
    if wu.tg_first_name:
        name = wu.tg_first_name
        if wu.tg_last_name:
            name = f"{name} {wu.tg_last_name}"
        if wu.tg_username:
            name = f"{name} (@{wu.tg_username})"
        return name
    # Fallback — данные из TG message.
    if fallback_user.first_name:
        name = fallback_user.first_name
        if fallback_user.last_name:
            name = f"{name} {fallback_user.last_name}"
        if fallback_user.username:
            name = f"{name} (@{fallback_user.username})"
        return name
    if fallback_user.username:
        return f"@{fallback_user.username}"
    return f"tg_user_id={fallback_user.id}"


async def _load_github_settings() -> GithubSettings | None:
    """Загружает singleton-настройку GitHub (id=1). None если нет записи
    или интеграция выключена (is_active=False)."""
    async with async_session() as session:
        gs = (await session.execute(
            select(GithubSettings).where(GithubSettings.id == 1)
        )).scalar_one_or_none()
        return gs


async def _send_idea_alert_to_su(
    bot: types.Bot, sender_display: str, idea_text: str,
    issue_url: str | None, error_msg: str | None,
) -> None:
    """Отправляет DM каждому SU с алертом о новой идее.

    Если issue_url=None (создание упало) — алерт содержит текст ошибки и
    текст идеи (чтобы SU мог пересоздать Issue вручную).

    Доставляемость best-effort: если DM конкретному SU не доставляется
    (нет приватного чата, юзер заблокировал бота) — логируем warning
    и идём к следующему. Не падаем.
    """
    # Собираем список SU tg_user_id: env ADMIN_IDS + WebUser role='su'.
    su_tg_ids: set[int] = set(ADMIN_IDS)
    async with async_session() as session:
        su_wus = (await session.execute(
            select(WebUser).where(WebUser.role == "su", WebUser.is_active.is_(True))
        )).scalars().all()
        for wu in su_wus:
            if wu.tg_user_id:
                su_tg_ids.add(wu.tg_user_id)

    if not su_tg_ids:
        logger.warning(
            "send_idea_alert_to_su: no SU configured (ADMIN_IDS empty, "
            "no WebUser role='su'). Idea alert lost: sender=%s text=%r",
            sender_display, idea_text[:80],
        )
        return

    if issue_url:
        text = (
            f"💡 <b>Новая идея от</b> <code>{html.escape(sender_display)}</code>.\n"
            f"Загляни в проект: {issue_url}"
        )
    else:
        # Провал — включаем в алерт сам текст идеи.
        text = (
            f"⚠️ <b>Идея от</b> <code>{html.escape(sender_display)}</code> "
            f"<b>не дошла до GitHub</b>.\n"
            f"Ошибка: <code>{html.escape(error_msg or 'неизвестна')}</code>\n"
            f"Текст идеи: {html.escape(idea_text)}"
        )

    for su_id in su_tg_ids:
        try:
            await bot.send_message(chat_id=su_id, text=text, parse_mode="HTML")
        except TelegramAPIError as e:
            logger.warning(
                "send_idea_alert_to_su: failed to DM su_id=%s: %s",
                su_id, e,
            )
        except Exception as e:
            logger.warning(
                "send_idea_alert_to_su: unexpected error for su_id=%s: %s",
                su_id, e,
            )


async def _process_idea_submission(
    message: types.Message, idea_text: str, source: str,
    source_chat_id: int | None,
) -> None:
    """Общая логика обработки `!idea` для DM и modchat.

    Args:
        message: исходное сообщение (для reply).
        idea_text: уже валидированный текст (1..200 символов).
        source: 'dm' | 'modchat'.
        source_chat_id: для modchat — ID чата, для dm — message.chat.id.
    """
    user = message.from_user
    user_id = user.id

    # Стелс: проверяем что юзер — активный WebUser (SU/admin/moderator).
    wu = await _resolve_sender_web_user(user_id)
    if wu is None:
        # Посторонний или деактивированный — молчим, как в /help.
        return

    # Загружаем настройки GitHub.
    gs = await _load_github_settings()
    if gs is None or not gs.is_active:
        # Интеграция не настроена — не пугаем отправителя, но пишем в лог.
        logger.info(
            "idea: GitHub integration not configured (is_active=False or no row). "
            "user_id=%s idea=%r", user_id, idea_text[:80],
        )
        await message.reply(
            "❌ Интеграция с GitHub ещё не настроена. "
            "Скажи SU, чтобы завёл PAT в веб-панели.",
            parse_mode=None,
        )
        return

    # Расшифровываем PAT.
    try:
        pat = _decrypt_pat(gs.pat_encrypted)
    except Exception as e:
        logger.error("idea: failed to decrypt PAT: %s", e)
        await message.reply(
            "❌ Не удалось расшифровать PAT. Скажи SU, чтобы перезавёл токен "
            "в веб-панели.",
            parse_mode=None,
        )
        return

    # Готовим метаданные для лога.
    display_name = _build_display_name(wu, user)
    bot_version = _bot_version()

    # Создаём Issue + добавляем в Project.
    issue_url: str | None = None
    issue_number: int | None = None
    project_item_id: str | None = None
    error_msg: str | None = None

    try:
        from github_client import (
            GithubApiError,
            add_issue_to_project,
            create_issue,
            set_item_status_by_name,
        )
        # 1. REST: создаём Issue (title = idea_text, без body).
        issue_ref = await create_issue(
            pat=pat, owner=gs.repo_owner, repo=gs.repo_name,
            title=idea_text,
        )
        issue_url = issue_ref.url
        issue_number = issue_ref.number

        # 2. GraphQL: добавляем Issue в Project v2.
        # Нужен node ID Issue. create_issue возвращает его, но иногда
        # (для нового формата Global ID) он уже подходит. На всякий случай
        # используем issue_ref.node_id напрямую — он должен быть валидным.
        if gs.project_node_id:
            try:
                project_item_id = await add_issue_to_project(
                    pat=pat,
                    project_node_id=gs.project_node_id,
                    issue_node_id=issue_ref.node_id,
                )
            except GithubApiError as e:
                # Issue создан, но не добавился в Project — не критично.
                # Логируем warning, но считаем идею доставленной.
                logger.warning(
                    "idea: Issue #%s created but add_to_project failed: %s "
                    "(project_node_id=%s)",
                    issue_number, e, gs.project_node_id,
                )
                project_item_id = None

            # v4.8.5.3: выставляем Status = 'Предложено' для новой карточки.
            # Best-effort — если упадёт, Issue уже в Project, просто без
            # Status (пользователь перетащит руками).
            if project_item_id:
                status_name = (
                    gs.project_status_option_name
                    or "Предложено"
                )
                try:
                    ok = await set_item_status_by_name(
                        pat=pat,
                        project_node_id=gs.project_node_id,
                        item_id=project_item_id,
                        status_name=status_name,
                    )
                    if not ok:
                        logger.warning(
                            "idea: Issue #%s added to Project %s but Status "
                            "not set (option '%s' missing or field missing).",
                            issue_number, gs.project_node_id, status_name,
                        )
                except Exception as e:
                    # Defensive: set_item_status_by_name сам ловит
                    # GithubApiError внутри, но на всякий случай.
                    logger.warning(
                        "idea: set_item_status_by_name unexpected error "
                        "for Issue #%s: %s",
                        issue_number, e,
                    )
    except GithubApiError as e:
        error_msg = str(e)
        logger.error(
            "idea: create_issue failed for user_id=%s: %s",
            user_id, e,
        )
    except Exception as e:
        error_msg = f"unexpected: {e}"
        logger.exception("idea: unexpected error for user_id=%s", user_id)

    # Логируем в idea_log (в любом случае — успех или провал).
    try:
        async with async_session() as session:
            session.add(IdeaLog(
                tg_user_id=user_id,
                tg_username=wu.tg_username or user.username,
                tg_display_name=display_name,
                source=source,
                source_chat_id=source_chat_id,
                idea_text=idea_text,
                github_issue_url=issue_url,
                github_issue_number=issue_number,
                github_project_item_id=project_item_id,
                error_message=error_msg,
                bot_version=bot_version,
            ))
            await session.commit()
    except Exception as e:
        logger.exception("idea: failed to write idea_log: %s", e)

    # Ответ отправителю.
    if issue_url is not None:
        try:
            await message.reply("Спасибо за идею. Передал.", parse_mode=None)
        except TelegramAPIError as e:
            logger.warning("idea: reply to sender failed: %s", e)
    else:
        try:
            await message.reply(
                "❌ Не получилось передать идею, уже чиним.",
                parse_mode=None,
            )
        except TelegramAPIError as e:
            logger.warning("idea: reply to sender failed: %s", e)

    # Алерт SU в DM.
    await _send_idea_alert_to_su(
        bot=message.bot,
        sender_display=display_name,
        idea_text=idea_text,
        issue_url=issue_url,
        error_msg=error_msg,
    )


async def _is_modchat_chat(chat_id: int) -> bool:
    """Проверяет, является ли chat_id модераторским чатом.

    Использует _get_mod_chat_id из modchat.py: если для этого chat_id
    резолвится mod_chat_id == chat_id (то есть он сам и есть modchat),
    возвращаем True.
    """
    try:
        from modchat import _get_mod_chat_id
        async with async_session() as session:
            mod_chat_id = await _get_mod_chat_id(session, chat_id)
            return mod_chat_id is not None and mod_chat_id == chat_id
    except Exception as e:
        logger.warning("_is_modchat_chat: lookup failed for chat_id=%s: %s",
                       chat_id, e)
        return False


# !idea <текст> в ЛС боту.
# v4.8.5.2: prefix="!/" — ловит и !idea, и /idea. Раньше (v4.8.5) был
# default prefix="/", который НЕ ловил !idea → бот молчал в ответ на !idea
# (сообщение падало в stealth_catchall_private).
@router.message(F.chat.type == "private", Command("idea", prefix="!/"))
async def cmd_idea_dm(message: types.Message) -> None:
    """v4.8.5: !idea <текст> — отправить идею в GitHub Issue + Project.

    Доступ: SU/admin/moderator (через WebUser). Посторонние — молчим (стелс).
    Канал: только DM (этот handler). Отдельный handler покрывает modchat.

    Лимит: 200 символов на текст идеи.
    Без cooldown, без модерации.

    Ответ отправителю: "Спасибо за идею. Передал."
    Алерт SU в DM: "Новая идея от <имя>. Загляни в проект!" + ссылка.
    """
    # Извлекаем текст после /idea.
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply(
            "💡 Формат: !idea <текст идеи>\n"
            f"Максимум {_IDEA_MAX_LEN} символов.",
            parse_mode=None,
        )
        return

    idea_text = parts[1].strip()
    if len(idea_text) > _IDEA_MAX_LEN:
        await message.reply(
            f"❌ Идея слишком длинная: {len(idea_text)} символов. "
            f"Максимум {_IDEA_MAX_LEN}.",
            parse_mode=None,
        )
        return

    await _process_idea_submission(
        message=message, idea_text=idea_text,
        source="dm", source_chat_id=message.chat.id,
    )


# !idea <текст> в групповом чате (срабатывает ТОЛЬКО если это modchat).
# Стоит раньше остальных group-обработчиков, но проверка modchat отсечёт
# все обычные чаты. Не падает, не пишет в чат, если это не modchat.
# v4.8.5.2: prefix="!/" — ловит и !idea, и /idea.
@router.message(F.chat.type != "private", Command("idea", prefix="!/"))
async def cmd_idea_modchat(message: types.Message) -> None:
    """v4.8.5: !idea <текст> в modchat — отправить идею в GitHub Issue + Project.

    Срабатывает в любом групповом чате, но логика выполняется только если
    чат является modchat (см. _is_modchat_chat). В обычных чатах — молчим
    и не трогаем сообщение.
    """
    if not await _is_modchat_chat(message.chat.id):
        # Не modchat — молча игнорируем. Не удаляем сообщение, не отвечаем.
        return

    # Извлекаем текст.
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        # В modchat можно подсказать формат — тут только свои.
        try:
            await message.reply(
                f"💡 Формат: !idea <текст идеи> (макс. {_IDEA_MAX_LEN} символов)",
                parse_mode=None,
            )
        except TelegramAPIError as e:
            logger.warning("cmd_idea_modchat: format hint failed: %s", e)
        return

    idea_text = parts[1].strip()
    if len(idea_text) > _IDEA_MAX_LEN:
        try:
            await message.reply(
                f"❌ Идея слишком длинная: {len(idea_text)} символов. "
                f"Максимум {_IDEA_MAX_LEN}.",
                parse_mode=None,
            )
        except TelegramAPIError as e:
            logger.warning("cmd_idea_modchat: too-long reply failed: %s", e)
        return

    await _process_idea_submission(
        message=message, idea_text=idea_text,
        source="modchat", source_chat_id=message.chat.id,
    )


@router.message(F.chat.type == "private", Command("cas"))
async def cmd_cas(message: types.Message) -> None:
    """v4.5.2 (#2): /cas chat_id on|off — включить/выключить CAS-проверку."""
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        await message.reply(
            "📋 Формат: /cas chat_id on|off\n"
            "💡 on — проверять новых участников через api.cas.chat, банить если в базе",
            parse_mode=None,
        )
        return

    try:
        chat_id = int(parts[1])
    except ValueError:
        await message.reply("❌ chat_id должен быть числом", parse_mode=None)
        return

    flag = parts[2].lower().strip()
    if flag not in ("on", "off", "1", "0", "true", "false", "yes", "no"):
        await message.reply("❌ Должно быть on/off", parse_mode=None)
        return
    enabled = flag in ("on", "1", "true", "yes")

    async with async_session() as session:
        settings = await _get_chat_settings(session, chat_id)
        settings.cas_check_enabled = enabled
        await session.commit()

    status = "включена" if enabled else "выключена"
    await message.reply(
        f"✅ CAS-проверка в чате {chat_id}: <b>{status}</b>",
        parse_mode="HTML",
    )


@router.message(F.chat.type == "private", Command("nightmode"))
async def cmd_nightmode(message: types.Message) -> None:
    """v4.5.3: расширенная настройка ночного режима.

    Поддерживаемые формы:
      /nightmode chat_id <start> <end> [strict|text_only|none]   — базовая настройка
      /nightmode chat_id off                                     — выключить
      /nightmode chat_id tz <Europe/Moscow>                      — сменить часовой пояс
      /nightmode chat_id weekend <start> <end>                   — расписание на сб/вс
      /nightmode chat_id weekend off                             — сбросить (использовать будничное)
      /nightmode chat_id notify on [custom_enter_text]           — включить уведомления
      /nightmode chat_id notify off                              — выключить уведомления
      /nightmode chat_id notify_text enter <text>                — только текст входа
      /nightmode chat_id notify_text exit <text>                 — только текст выхода
      /nightmode chat_id custom <perm>=0|1 <perm>=0|1 ...        — точечные права

    Алиасы perms для custom: msgs, audios, docs, photos, videos,
      vnotes, voices, polls, other, links.
    """
    if message.from_user.id not in ADMIN_IDS:
        return
    # Не ограничиваем split — для notify с кастомным текстом может быть много слов.
    raw = message.text or ""
    parts = raw.split(maxsplit=3)
    if len(parts) < 3:
        await message.reply(
            "📋 Формат:\n"
            "  /nightmode chat_id <start> <end> [strict|text_only|none]\n"
            "  /nightmode chat_id off\n"
            "  /nightmode chat_id tz <Europe/Moscow>\n"
            "  /nightmode chat_id weekend <start> <end> | off\n"
            "  /nightmode chat_id notify on [custom_text] | off\n"
            "  /nightmode chat_id notify_text enter|exit <text>\n"
            "  /nightmode chat_id custom <perm>=0|1 ...\n"
            "  /nightmode chat_id slowmode [day_sec] [night_sec] | off\n"
            "💡 perms: msgs, audios, docs, photos, videos, vnotes, voices, polls, other, links\n"
            "💡 slowmode: 0..36400 сек; day — днём, night — ночью; 0=выкл",
            parse_mode=None,
        )
        return

    try:
        chat_id = int(parts[1])
    except ValueError:
        await message.reply("❌ chat_id должен быть числом", parse_mode=None)
        return

    arg2 = parts[2].lower().strip()

    # ── SUBCOMMAND: off ──────────────────────────────────────────────
    if arg2 == "off":
        async with async_session() as session:
            settings = await _get_chat_settings(session, chat_id)
            settings.night_mode_enabled = False
            # v4.7.2: если сейчас активен — выходим из ночного режима,
            # восстанавливаем права из snapshot. Иначе чат зависнет в night-правах.
            if settings.night_mode_currently_active:
                try:
                    # v4.8.9: late import через app_state вместо `from bot import`
                    # (см. 03_TASK_v4.8.9.md §3 — удаление sys.modules хака).
                    from app_state import get_exit_night_mode
                    _exit_night_mode = get_exit_night_mode()
                    # Re-fetch из сессии чтобы _exit_night_mode работал
                    await _exit_night_mode(settings)
                    await session.refresh(settings)
                except Exception as e:
                    logger.warning("nightmode off: exit failed for chat %s: %s", chat_id, e)
                    settings.night_mode_currently_active = False
            await session.commit()
        await message.reply(
            f"✅ Ночной режим в чате {chat_id}: <b>выключен</b>",
            parse_mode="HTML",
        )
        return

    # ── SUBCOMMAND: on (v4.7.2) ─────────────────────────────────────
    # Включает функцию night_mode_enabled=True, не меняя настроек.
    # Полезно после миграции v4.7.2 (где все toggles сбрасываются в off).
    if arg2 == "on":
        async with async_session() as session:
            settings = await _get_chat_settings(session, chat_id)
            settings.night_mode_enabled = True
            await session.commit()
            start = settings.night_mode_start or "23:00"
            end = settings.night_mode_end or "07:00"
            tz = settings.night_mode_tz or "Europe/Moscow"
        await message.reply(
            f"✅ Ночной режим в чате {chat_id}: <b>функция включена</b>\n"
            f"⏰ {start} → {end} ({tz})\n"
            "Бот будет автоматически применять ночные ограничения в заданное время.",
            parse_mode="HTML",
        )
        return

    # ── SUBCOMMAND: tz ───────────────────────────────────────────────
    # v4.7.2: tz/weekend/notify/preset/status — это настройки режима.
    # Если night_mode_enabled=False, всё равно можно их менять (настройки
    # сохраняются, но не активны). Это упрощает миграцию и не требует
    # включать режим чтобы настроить. Однако tick не будет применять
    # ничего пока enabled=False.
    if arg2 == "tz":
        if len(parts) < 4:
            async with async_session() as session:
                settings = await _get_chat_settings(session, chat_id)
                current_tz = settings.night_mode_tz or "Europe/Moscow"
            await message.reply(
                f"🌍 Текущий tz чата {chat_id}: <b>{current_tz}</b>\n"
                "💡 /nightmode chat_id tz Europe/Moscow\n"
                "💡 /nightmode chat_id tz Asia/Yekaterinburg\n"
                "Полный список: https://en.wikipedia.org/wiki/List_of_tz_database_time_zones",
                parse_mode="HTML",
            )
            return
        tz_name = parts[3].strip()
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(tz_name)  # validate
        except (ValueError, KeyError):
            await message.reply(
                f"❌ Некорректный tz: '{tz_name}'\n"
                "💡 Примеры: Europe/Moscow, Europe/Kaliningrad, Asia/Yekaterinburg, Asia/Novosibirsk",
                parse_mode=None,
            )
            return
        async with async_session() as session:
            settings = await _get_chat_settings(session, chat_id)
            settings.night_mode_tz = tz_name
            await session.commit()
        await message.reply(
            f"✅ Часовой пояс чата {chat_id}: <b>{tz_name}</b>",
            parse_mode="HTML",
        )
        return

    # ── SUBCOMMAND: weekend ──────────────────────────────────────────
    if arg2 == "weekend":
        if len(parts) < 4:
            await message.reply(
                "📋 Формат:\n"
                "  /nightmode chat_id weekend <start> <end>\n"
                "  /nightmode chat_id weekend off\n"
                "💡 /nightmode chat_id weekend 02:00 10:00",
                parse_mode=None,
            )
            return
        arg3 = parts[3].lower().strip()
        if arg3 == "off":
            async with async_session() as session:
                settings = await _get_chat_settings(session, chat_id)
                settings.night_mode_weekend_start = None
                settings.night_mode_weekend_end = None
                await session.commit()
            await message.reply(
                f"✅ Чат {chat_id}: выходное расписание сброшено (используется будничное)",
                parse_mode=None,
            )
            return
        # Парсим "start end" — но parts[3] уже содержит "start", нужно ещё и end.
        # parts делится с maxsplit=3, поэтому parts[3] может содержать "start end extra".
        sub = (raw.split(maxsplit=4))
        if len(sub) < 5:
            await message.reply(
                "❌ Нужно указать start и end\n"
                "💡 /nightmode chat_id weekend 02:00 10:00",
                parse_mode=None,
            )
            return
        wknd_start = sub[3].strip()
        wknd_end = sub[4].strip()
        for label, t in (("start", wknd_start), ("end", wknd_end)):
            tparts = t.split(":")
            if len(tparts) != 2:
                await message.reply(f"❌ {label} должен быть HH:MM (получили '{t}')", parse_mode=None)
                return
            try:
                h, m = int(tparts[0]), int(tparts[1])
                if not (0 <= h <= 23 and 0 <= m <= 59):
                    raise ValueError()
            except ValueError:
                await message.reply(f"❌ {label} некорректное время: '{t}'", parse_mode=None)
                return
        async with async_session() as session:
            settings = await _get_chat_settings(session, chat_id)
            settings.night_mode_weekend_start = wknd_start
            settings.night_mode_weekend_end = wknd_end
            await session.commit()
        await message.reply(
            f"✅ Чат {chat_id}: выходные <b>{wknd_start} → {wknd_end}</b>",
            parse_mode="HTML",
        )
        return

    # ── SUBCOMMAND: notify_text ──────────────────────────────────────
    if arg2 == "notify_text":
        # /nightmode chat_id notify_text enter <text>
        # /nightmode chat_id notify_text exit <text>
        sub = raw.split(maxsplit=4)
        if len(sub) < 5:
            await message.reply(
                "📋 Формат:\n"
                "  /nightmode chat_id notify_text enter <текст>\n"
                "  /nightmode chat_id notify_text exit <текст>\n"
                "💡 В тексте можно использовать плейсхолдеры: {chat_id}, {start}, {end}\n"
                "💡 '/nightmode chat_id notify_text enter default' — вернуть дефолтный шаблон",
                parse_mode=None,
            )
            return
        which = sub[3].lower().strip()
        if which not in ("enter", "exit"):
            await message.reply(
                "❌ Первый аргумент notify_text должен быть 'enter' или 'exit'",
                parse_mode=None,
            )
            return
        text_value = sub[4].strip()
        if text_value.lower() == "default":
            text_value = None
        elif not text_value:
            await message.reply("❌ Текст не может быть пустым (используйте 'default' для сброса)", parse_mode=None)
            return
        async with async_session() as session:
            settings = await _get_chat_settings(session, chat_id)
            if which == "enter":
                settings.night_mode_notify_enter_msg = text_value
            else:
                settings.night_mode_notify_exit_msg = text_value
            await session.commit()
        label = "входа" if which == "enter" else "выхода"
        status = "<b>дефолтный шаблон</b>" if text_value is None else f"кастомный: <code>{html.escape(text_value[:80])}</code>"
        await message.reply(
            f"✅ Текст уведомления {label} для чата {chat_id}: {status}",
            parse_mode="HTML",
        )
        return

    # ── SUBCOMMAND: notify ───────────────────────────────────────────
    if arg2 == "notify":
        # v4.5.3: parse with maxsplit=4 to separate "on|off" from custom text.
        # sub = ['/nightmode', chat_id, 'notify', 'on'|'off', 'custom text...']
        sub = raw.split(maxsplit=4)
        if len(sub) < 4:
            async with async_session() as session:
                settings = await _get_chat_settings(session, chat_id)
                current = bool(settings.night_mode_notify)
            await message.reply(
                f"🔔 Уведомления ночного режима для чата {chat_id}: "
                f"<b>{'включены' if current else 'выключены'}</b>\n"
                "💡 /nightmode chat_id notify on [custom_text]\n"
                "💡 /nightmode chat_id notify off",
                parse_mode="HTML",
            )
            return
        arg3 = sub[3].lower().strip()
        if arg3 not in ("on", "off"):
            await message.reply(
                "❌ notify: ожидается 'on' или 'off'",
                parse_mode=None,
            )
            return
        # Если on + есть кастомный текст — сохраняем его как enter_msg и exit_msg.
        # Это сокращение для типичного юзкейса: одинаковый текст для входа и выхода.
        custom_text: str | None = None
        if arg3 == "on" and len(sub) >= 5 and sub[4].strip().lower() != "default":
            custom_text = sub[4].strip()
        async with async_session() as session:
            settings = await _get_chat_settings(session, chat_id)
            settings.night_mode_notify = (arg3 == "on")
            if arg3 == "on" and custom_text is not None:
                settings.night_mode_notify_enter_msg = custom_text
                settings.night_mode_notify_exit_msg = custom_text
            await session.commit()
        status = "включены" if arg3 == "on" else "выключены"
        await message.reply(
            f"✅ Уведомления ночного режима для чата {chat_id}: <b>{status}</b>",
            parse_mode="HTML",
        )
        return

    # ── SUBCOMMAND: custom ───────────────────────────────────────────
    if arg2 == "custom":
        # /nightmode chat_id custom <perm>=0|1 <perm>=0|1 ...
        # perm — алиас или полное имя. Алиасы см. _NIGHT_PERM_ALIASES.
        sub = raw.split()
        # sub = ['/nightmode', chat_id, 'custom', 'msgs=1', 'photos=0', ...]
        if len(sub) < 4:
            await message.reply(
                "❌ Укажите хотя бы один override\n"
                "💡 /nightmode chat_id custom msgs=1 photos=0 videos=0\n"
                "💡 Алиасы: msgs, audios, docs, photos, videos, vnotes, voices, polls, other, links",
                parse_mode=None,
            )
            return
        overrides: dict[str, bool] = {}
        for token in sub[3:]:
            if "=" not in token:
                await message.reply(
                    f"❌ Неверный формат: '{token}' (нужно <perm>=0|1)",
                    parse_mode=None,
                )
                return
            key, _, val = token.partition("=")
            key = key.strip().lower()
            val = val.strip()
            if val not in ("0", "1"):
                await message.reply(
                    f"❌ Значение должно быть 0 или 1 (получили '{val}' для '{key}')",
                    parse_mode=None,
                )
                return
            full_name = _NIGHT_PERM_ALIASES.get(key, key)
            if full_name not in _PERM_FIELDS:
                await message.reply(
                    f"❌ Неизвестный perm: '{key}'\n"
                    "💡 Алиасы: msgs, audios, docs, photos, videos, vnotes, voices, polls, other, links",
                    parse_mode=None,
                )
                return
            overrides[full_name] = (val == "1")
        # Базовый preset — текущий сохранённый preset или text_only.
        async with async_session() as session:
            settings = await _get_chat_settings(session, chat_id)
            current_perms_json = settings.night_mode_permissions
            base_preset = "text_only"
            if current_perms_json:
                try:
                    data = json.loads(current_perms_json)
                    # Распознаём preset по значению can_send_messages
                    if all(data.get(k, False) for k in _PERM_FIELDS):
                        base_preset = "none"
                    elif not any(data.get(k, False) for k in (
                        "can_send_messages", "can_send_audios", "can_send_documents",
                        "can_send_photos", "can_send_videos", "can_send_video_notes",
                        "can_send_voice_notes", "can_send_polls", "can_send_other_messages",
                    )):
                        base_preset = "strict"
                except (ValueError, TypeError):
                    pass
            perms = _build_custom_night_permissions(base_preset, overrides)
            perms_json = json.dumps({k: bool(getattr(perms, k, False)) for k in _PERM_FIELDS})
            settings.night_mode_permissions = perms_json
            await session.commit()
        summary = ", ".join(f"{k}={'1' if v else '0'}" for k, v in overrides.items())
        await message.reply(
            f"✅ Точечные права для чата {chat_id} применены (base=<b>{base_preset}</b>):\n"
            f"<code>{html.escape(summary)}</code>",
            parse_mode="HTML",
        )
        return

    # ── SUBCOMMAND: slowmode (v4.7.16) ──────────────────────────────
    # /nightmode chat_id slowmode              — показать текущие настройки
    # /nightmode chat_id slowmode <day> <night> — установить day/night slow_mode (сек)
    # /nightmode chat_id slowmode off          — выключить slow_mode changes (0/0)
    # Telegram: 0 <= slow_mode_delay <= 36400. 0 = выкл.
    if arg2 == "slowmode":
        # Перепарсим с большим maxsplit чтобы получить day/night аргументы.
        sub = raw.split(maxsplit=5)
        # sub = ['/nightmode', chat_id_str, 'slowmode', arg3?, arg4?]
        if len(sub) < 4:
            # Show current
            async with async_session() as session:
                settings = await _get_chat_settings(session, chat_id)
                day_s = int(settings.day_slow_mode_delay or 0)
                night_s = int(settings.night_mode_slow_mode_delay or 0)
                saved_s = settings.night_mode_saved_slow_mode_delay
            await message.reply(
                f"🐌 Slow mode для чата {chat_id}:\n"
                f"  ☀️ Day:   <b>{day_s}</b> сек ({'выкл' if day_s == 0 else 'включён'})\n"
                f"  🌙 Night: <b>{night_s}</b> сек ({'выкл' if night_s == 0 else 'включён'})\n"
                + (f"  💾 Saved: {int(saved_s)} сек (snapshot для restore)"
                   if saved_s is not None else ""),
                parse_mode="HTML",
            )
            return
        arg3 = sub[3].strip().lower()
        if arg3 == "off":
            # Disable both
            async with async_session() as session:
                settings = await _get_chat_settings(session, chat_id)
                settings.day_slow_mode_delay = 0
                settings.night_mode_slow_mode_delay = 0
                # НЕ трогаем night_mode_saved_slow_mode_delay — если night mode
                # сейчас активен, при выходе restore вернёт snapshot. Иначе
                # snapshot не нужен (его очистит _exit_night_mode при следующем
                # цикле). Просто сбрасываем настройки.
                await session.commit()
            await message.reply(
                f"✅ Slow mode changes для чата {chat_id}: <b>выключены</b>\n"
                "Текущий slow_mode в чате не меняется. "
                "При следующем входе/выходе из night mode бот не будет трогать slow_mode.",
                parse_mode="HTML",
            )
            return
        # Parse <day_sec> <night_sec>
        if len(sub) < 5:
            await message.reply(
                "❌ Укажите оба значения: <day_sec> <night_sec>\n"
                "💡 /nightmode chat_id slowmode 10 60\n"
                "💡 0 = выкл для этой стороны (например '0 60' — днём не трогать, ночью 60с)\n"
                "💡 /nightmode chat_id slowmode off — полностью выключить",
                parse_mode=None,
            )
            return
        try:
            day_v = int(sub[3])
            night_v = int(sub[4])
        except ValueError:
            await message.reply(
                "❌ Значения должны быть целыми числами (секунды)\n"
                "💡 /nightmode chat_id slowmode 10 60",
                parse_mode=None,
            )
            return
        # Telegram limit: 0..36400. 0 = disabled.
        for label, v in (("day", day_v), ("night", night_v)):
            if v < 0 or v > 36400:
                await message.reply(
                    f"❌ {label}_sec={v} вне диапазона (0..36400)\n"
                    "💡 0 = выкл, типичные значения: day=10, night=30..60",
                    parse_mode=None,
                )
                return
        async with async_session() as session:
            settings = await _get_chat_settings(session, chat_id)
            settings.day_slow_mode_delay = day_v
            settings.night_mode_slow_mode_delay = night_v
            await session.commit()
        # Человекочитаемое описание
        def _sm_desc(v: int) -> str:
            if v == 0:
                return "выкл (не меняется)"
            if v < 60:
                return f"{v}с"
            m, s = divmod(v, 60)
            if s == 0:
                return f"{m}мин"
            return f"{m}мин {s}с"
        await message.reply(
            f"✅ Slow mode для чата {chat_id} настроен:\n"
            f"  ☀️ Day:   <b>{_sm_desc(day_v)}</b>\n"
            f"  🌙 Night: <b>{_sm_desc(night_v)}</b>\n"
            "Применится автоматически при следующем входе/выходе из night mode. "
            "Если night mode сейчас активен — изменения вступят в силу при следующем тике.",
            parse_mode="HTML",
        )
        return

    # ── БАЗОВАЯ ФОРМА: /nightmode chat_id <start> <end> [preset] ─────
    # arg2 = start (HH:MM). Нужно ещё и end.
    # Перепарсим с большим maxsplit чтобы получить preset.
    sub = raw.split(maxsplit=4)
    # sub = ['/nightmode', chat_id_str, start, end, preset?]
    if len(sub) < 4:
        await message.reply(
            "❌ Нужно указать start и end\n"
            "💡 /nightmode chat_id 23:00 07:00 [strict|text_only|none]",
            parse_mode=None,
        )
        return
    start = sub[2].strip()
    end = sub[3].strip()
    # Валидация HH:MM
    for label, t in (("start", start), ("end", end)):
        tparts = t.split(":")
        if len(tparts) != 2:
            await message.reply(f"❌ {label} должен быть в формате HH:MM (получили '{t}')", parse_mode=None)
            return
        try:
            h, m = int(tparts[0]), int(tparts[1])
            if not (0 <= h <= 23 and 0 <= m <= 59):
                raise ValueError()
        except ValueError:
            await message.reply(f"❌ {label} некорректное время: '{t}'", parse_mode=None)
            return

    preset = "text_only"
    if len(sub) >= 5:
        preset = sub[4].lower().strip()
        if preset not in ("strict", "text_only", "none"):
            await message.reply(
                f"❌ permissions должен быть strict/text_only/none (получили '{preset}')",
                parse_mode=None,
            )
            return

    # Сохраняем permissions в JSON
    perms = _night_mode_permissions_preset(preset)
    perms_json = json.dumps({k: bool(getattr(perms, k, False)) for k in _PERM_FIELDS})

    async with async_session() as session:
        settings = await _get_chat_settings(session, chat_id)
        settings.night_mode_enabled = True
        settings.night_mode_start = start
        settings.night_mode_end = end
        settings.night_mode_permissions = perms_json
        await session.commit()
        tz_name = settings.night_mode_tz or "Europe/Moscow"

    await message.reply(
        f"✅ Ночной режим в чате {chat_id}: <b>включён</b>\n"
        f"⏰ {start} → {end} ({html.escape(tz_name)})\n"
        f"🔒 Permissions: <b>{preset}</b>",
        parse_mode="HTML",
    )


@router.message(F.chat.type == "private", Command("warndecay"))
async def cmd_warndecay(message: types.Message) -> None:
    """v4.5.2 (#45): /warndecay chat_id <days> — установить срок действия варна.

    days=0 — отключить decay (варны копятся вечно).
    """
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        await message.reply(
            "📋 Формат: /warndecay chat_id <days>\n"
            "💡 0 — отключить decay (варны копятся вечно)\n"
            "    30 — варны старше 30 дней не учитываются в счётчике",
            parse_mode=None,
        )
        return

    try:
        chat_id = int(parts[1])
        days = int(parts[2])
    except ValueError:
        await message.reply("❌ chat_id и days должны быть числами", parse_mode=None)
        return
    if days < 0:
        await message.reply("❌ days должен быть >= 0", parse_mode=None)
        return

    async with async_session() as session:
        settings = await _get_chat_settings(session, chat_id)
        settings.warn_decay_days = days
        await session.commit()

    status = f"{days} дней" if days > 0 else "отключён"
    await message.reply(
        f"✅ Warn decay в чате {chat_id}: <b>{status}</b>",
        parse_mode="HTML",
    )


@router.message(F.chat.type == "private", Command("sanitary"))
async def cmd_sanitary(message: types.Message) -> None:
    """v4.5.4: Управление санитарными днями чата.

    В санитарный день чат переводится в полный lockdown (ChatPermissions
    → all False). Модераторов это не касается — их Telegram admin rights
    (выданные через promote_chat_member) override'ят chat-level perms.
    Ночной режим в санитарный день пропускается (не дёргает права).

    v4.7.2: добавлены подкоманды on/off — явный toggle функции.
    Если sanitary_days_enabled=False, бот не обрабатывает add/toggle/
    remove (только on/off/list/clear). Это убирает лишние варнинги в
    чатах где функция выключена.

    Поддерживаемые формы:
      /sanitary chat_id                                  — показать список
      /sanitary chat_id on                               — включить функцию
      /sanitary chat_id off                              — выключить функцию
      /sanitary chat_id add <YYYY-MM-DD>                 — добавить один день
      /sanitary chat_id add <YYYY-MM-DD>:<YYYY-MM-DD>    — добавить диапазон
      /sanitary chat_id remove <YYYY-MM-DD>              — удалить день/диапазон,
                                                            содержащий эту дату
      /sanitary chat_id clear                            — очистить весь список
      /sanitary chat_id toggle                           — вручную войти/выйти
                                                            (для тестирования)
    """
    if message.from_user.id not in ADMIN_IDS:
        return
    raw = message.text or ""
    parts = raw.split(maxsplit=3)
    if len(parts) < 2:
        await message.reply(
            "📋 Формат:\n"
            "  /sanitary chat_id\n"
            "  /sanitary chat_id on\n"
            "  /sanitary chat_id off\n"
            "  /sanitary chat_id add <YYYY-MM-DD>\n"
            "  /sanitary chat_id add <YYYY-MM-DD>:<YYYY-MM-DD>\n"
            "  /sanitary chat_id remove <YYYY-MM-DD>\n"
            "  /sanitary chat_id clear\n"
            "  /sanitary chat_id toggle\n"
            "💡 Санитарный день = полный lockdown чата (модераторы не страдают).",
            parse_mode=None,
        )
        return
    try:
        chat_id = int(parts[1])
    except ValueError:
        await message.reply("❌ chat_id должен быть числом", parse_mode=None)
        return

    sub = parts[2].lower().strip() if len(parts) >= 3 else ""

    async with async_session() as session:
        settings = await _get_chat_settings(session, chat_id)

        # ── LIST (no subcommand) ───────────────────────────────────────
        if not sub:
            pairs = parse_sanitary_days_json(settings.sanitary_days)
            if not pairs:
                state = "включена" if settings.sanitary_days_enabled else "выключена"
                await message.reply(
                    f"📋 Санитарные дни чата {chat_id}: <b>пусто</b>\n"
                    f"Функция: <b>{state}</b>",
                    parse_mode="HTML",
                )
                return
            lines = [f"📋 Санитарные дни чата {chat_id}:"]
            for i, (s, e) in enumerate(pairs, 1):
                if s == e:
                    lines.append(f"  {i}. {s}")
                else:
                    lines.append(f"  {i}. {s} → {e}")
            status = " ● АКТИВЕН" if settings.sanitary_days_currently_active else ""
            state = "включена" if settings.sanitary_days_enabled else "ВЫКЛЮЧЕНА (даты сохранены, не активны)"
            lines.append(f"Статус: {'🔒 lockdown' if settings.sanitary_days_currently_active else '⚪ не активен'}{status}")
            lines.append(f"Функция: {state}")
            await message.reply(
                "\n".join(lines),
                parse_mode="HTML",
            )
            return

        # ── v4.7.2: ON / OFF (явный toggle) ───────────────────────────
        if sub == "on":
            settings.sanitary_days_enabled = True
            await session.commit()
            await message.reply(
                f"✅ Санитарные дни чата {chat_id}: <b>функция включена</b>\n"
                "Бот будет автоматически входить в lockdown в заданные даты.",
                parse_mode="HTML",
            )
            return
        if sub == "off":
            settings.sanitary_days_enabled = False
            # Если сейчас активен — выходим из sanitary day.
            if settings.sanitary_days_currently_active:
                try:
                    # v4.8.9: app_state вместо `from bot import`
                    from app_state import get_exit_sanitary_day
                    _exit_sanitary_day = get_exit_sanitary_day()
                    await _exit_sanitary_day(settings)
                except Exception as e:
                    logger.warning("sanitary off: exit failed for chat %s: %s", chat_id, e)
            await session.commit()
            await message.reply(
                f"✅ Санитарные дни чата {chat_id}: <b>функция выключена</b>\n"
                "Даты сохранены, но бот не будет применять lockdown.",
                parse_mode="HTML",
            )
            return

        # ── v4.7.2: gate на enabled для add/remove/toggle ─────────────
        if sub in ("add", "remove", "toggle") and not settings.sanitary_days_enabled:
            await message.reply(
                f"⚠️ Функция санитарных дней выключена для чата {chat_id}.\n"
                f"Сначала включите: <code>/sanitary {chat_id} on</code>",
                parse_mode="HTML",
            )
            return

        # ── CLEAR ──────────────────────────────────────────────────────
        if sub == "clear":
            settings.sanitary_days = None
            settings.sanitary_days_saved_permissions = None
            settings.sanitary_days_currently_active = False
            await session.commit()
            await message.reply(
                f"✅ Санитарные дни чата {chat_id}: <b>очищены</b>",
                parse_mode="HTML",
            )
            return

        # ── TOGGLE (manual enter/exit for testing) ─────────────────────
        if sub == "toggle":
            if not settings.sanitary_days_currently_active:
                # v4.8.9: app_state вместо `from bot import`
                from app_state import get_enter_sanitary_day
                _enter_sanitary_day = get_enter_sanitary_day()
                await _enter_sanitary_day(settings)
                await message.reply(
                    f"🔒 Чат {chat_id}: <b>санитарный день включён вручную</b>\n"
                    "Чат в lockdown. Модераторы могут писать.",
                    parse_mode="HTML",
                )
            else:
                from app_state import get_exit_sanitary_day
                _exit_sanitary_day = get_exit_sanitary_day()
                await _exit_sanitary_day(settings)
                await message.reply(
                    f"🔓 Чат {chat_id}: <b>санитарный день снят вручную</b>\n"
                    "Права чата восстановлены из снапшота.",
                    parse_mode="HTML",
                )
            return

        # ── ADD / REMOVE ───────────────────────────────────────────────
        if sub not in ("add", "remove"):
            await message.reply(
                "❌ Неизвестная подкоманда. Доступно: add, remove, clear, toggle, "
                "(пусто — показать список).",
                parse_mode=None,
            )
            return
        if len(parts) < 4:
            await message.reply(
                f"❌ Укажите дату для '{sub}'\n"
                "💡 /sanitary chat_id add 2026-08-01\n"
                "💡 /sanitary chat_id add 2026-08-01:2026-08-03\n"
                "💡 /sanitary chat_id remove 2026-08-01",
                parse_mode=None,
            )
            return

        date_arg = parts[3].strip()
        # Парсим "start:end" или "start - end" или одну дату.
        sep = None
        for cand in (" - ", " to ", " — ", " – ", ":"):
            if cand in date_arg:
                sep = cand
                break
        if sep:
            sp = date_arg.split(sep, 1)
            ds = _parse_sanitary_date(sp[0].strip())
            de = _parse_sanitary_date(sp[1].strip())
            if ds is None or de is None:
                await message.reply(
                    f"❌ Невалидный диапазон: '{date_arg}'\n"
                    "💡 Формат: YYYY-MM-DD:YYYY-MM-DD",
                    parse_mode=None,
                )
                return
            if de < ds:
                de = ds
            target_pairs = [[ds.isoformat(), de.isoformat()]]
            target_label = ds.isoformat() if ds == de else f"{ds.isoformat()} → {de.isoformat()}"
        else:
            d = _parse_sanitary_date(date_arg)
            if d is None:
                await message.reply(
                    f"❌ Невалидная дата: '{date_arg}' (нужен YYYY-MM-DD)",
                    parse_mode=None,
                )
                return
            target_pairs = [[d.isoformat(), d.isoformat()]]
            target_label = d.isoformat()

        current_pairs = parse_sanitary_days_json(settings.sanitary_days)

        if sub == "add":
            # Дедуп: не добавляем если точно такая же пара уже есть.
            for s, e in current_pairs:
                if s == target_pairs[0][0] and e == target_pairs[0][1]:
                    await message.reply(
                        f"⚠️ Диапазон {target_label} уже есть в списке",
                        parse_mode=None,
                    )
                    return
            current_pairs.append(target_pairs[0])
            # Сортируем по start дате для удобства.
            current_pairs.sort(key=lambda p: p[0])
            settings.sanitary_days = json.dumps(current_pairs)
            await session.commit()
            await message.reply(
                f"✅ Добавлен санитарный день: <b>{target_label}</b>\n"
                f"Всего записей: {len(current_pairs)}",
                parse_mode="HTML",
            )
            return

        if sub == "remove":
            # Удаляем все пары, которые содержат указанную дату (или весь
            # диапазон целиком если передан диапазон и точно совпадает).
            if sep:
                # Точное совпадение пары.
                before = len(current_pairs)
                current_pairs = [
                    [s, e] for s, e in current_pairs
                    if not (s == target_pairs[0][0] and e == target_pairs[0][1])
                ]
                removed = before - len(current_pairs)
            else:
                # Удаляем все пары, содержащие указанную дату.
                d = _parse_sanitary_date(target_pairs[0][0])
                before = len(current_pairs)
                current_pairs = [
                    [s, e] for s, e in current_pairs
                    if not (
                        _parse_sanitary_date(s) <= d <= _parse_sanitary_date(e)
                    )
                ]
                removed = before - len(current_pairs)
            if removed == 0:
                await message.reply(
                    f"⚠️ Не найдено записей, содержащих {target_label}",
                    parse_mode=None,
                )
                return
            settings.sanitary_days = json.dumps(current_pairs) if current_pairs else None
            await session.commit()
            await message.reply(
                f"✅ Удалено записей: {removed}\n"
                f"Осталось: {len(current_pairs)}",
                parse_mode=None,
            )
            return


# ── v4.7.9: Тексты /help разделены по ролям ────────────────────────────────
# Полный текст — для ADMIN_IDS env, WebUser с role='su' или role='admin'.
# Сокращённый текст — для WebUser с role='moderator' и is_active=True:
# только групповые модераторские команды + ссылка на веб-панель.
# Все остальные (посторонние) — молчим (стелс сохраняется).
#
# v4.8.2: актуализация под реформу команд v4.8.1 — разбивка на громкие
# (публичное сообщение, причина обязательна) и тихие (стелс, ephemeral
# только модератору, причина необязательна). Удалены упоминания word_filter
# команд /addword//delword//listwords (заменены на KeywordWatch). Добавлен
# !alarm (v4.7.20b, не был описан). Добавлена ссылка на /admin/bans.
#
# v4.8.3.2: ПОЛНЫЙ РЕДИЗАЙН /help через Rich Message (InputRichMessage).
# Причина: _HELP_FULL_TEXT был 4621 символ, лимит Telegram — 4096 → SU и
# admin получали BadRequest "message is too long" и НЕ получали /help.
# Сокращённая _HELP_MODERATOR_TEXT (2477) вписывалась, модераторы видели.
# Теперь: обе версии — Rich Message со сворачиваемыми Details-блоками для
# длинных секций настроек. Без иконок, без intro, лаконичные списки.
# Footer: ссылка на веб-панель + версия бота.
#
# Архитектура: _build_help_full_rich() / _build_help_moderator_rich() —
# чистые функции, возвращающие InputRichMessage. cmd_help() выбирает по
# роли и вызывает bot.send_rich_message. Без fallback на HTML (риск что
# Rich Message не поддерживается — принят; Telegram clients обновлены).

def _help_code(text: str) -> RichTextCode:
    """Шорткат для inline-моноширинного кода в List-пунктах."""
    return RichTextCode(text=text)


def _help_list_item(code_text: str, description: str) -> InputRichBlockListItem:
    """List-пункт вида: <code>!cmd</code> — описание.

    code_text — команда с аргументами (моноширинно).
    description — что делает (после тире, обычный текст).
    """
    return InputRichBlockListItem(
        blocks=[InputRichBlockParagraph(text=[
            _help_code(code_text),
            f" — {description}",
        ])]
    )


def _help_section(
    heading: str,
    items: list[tuple[str, str]],
) -> list:
    """Секция: SectionHeading(size=2) + Divider + List.

    items — список пар (code_text, description).
    Возвращает список блоков (3 шт) для добавления в blocks[].
    """
    return [
        InputRichBlockSectionHeading(text=heading, size=2),
        InputRichBlockDivider(),
        InputRichBlockList(items=[
            _help_list_item(code, desc) for code, desc in items
        ]),
    ]


def _help_details(
    summary: str,
    items: list[tuple[str, str]],
    note: list | None = None,
) -> InputRichBlockDetails:
    """Сворачиваемый Details-блок с List команд внутри.

    summary — заголовок сворачиваемого блока (виден всегда).
    items — список пар (code_text, description) для List.
    note — опциональный параграф (список inline-элементов) после List,
            например предупреждение про KeywordWatch.
    """
    details_blocks: list = [
        InputRichBlockDivider(),
        InputRichBlockList(items=[
            _help_list_item(code, desc) for code, desc in items
        ]),
    ]
    if note is not None:
        details_blocks.append(InputRichBlockParagraph(text=note))
    return InputRichBlockDetails(
        summary=summary,
        is_open=False,
        blocks=details_blocks,
    )


def _build_help_full_rich() -> InputRichMessage:
    """Полная версия /help для SU и admin — через Rich Message.

    Структура:
      1. H1 заголовок + Divider
      2. Громкие команды (3 шт) — раскрыто
      3. Тихие команды (3 шт) — раскрыто
      4. Снятие наказаний / Прочее (6 шт) — раскрыто
      5. Details «Настройки чатов (8 команд)»
      6. Details «Фильтры (7 команд)»
      7. Details «Ночной режим (9 команд)»
      8. Details «Санитарные дни (6 команд)»
      9. Details «Прочее (1 команда)»
      10. Footer: веб-ссылка + версия бота
    """
    # Lazy import — избегаем циклического web_app ↔ bot_handlers.
    try:
        from web_app import APP_VERSION
        version_str = APP_VERSION
    except Exception:
        version_str = "v4.8.5"
    web_url = WEB_PUBLIC_URL or "https://degraban.bothost.tech"

    blocks: list = []

    # H1 + Divider
    blocks.append(InputRichBlockSectionHeading(
        text="Дедушка Вобжак — список команд", size=1,
    ))
    blocks.append(InputRichBlockDivider())

    # 2. Громкие команды (раскрыто)
    blocks.extend(_help_section(
        "Громкие команды (reply на нарушителя)",
        [
            ("!mute <длит> <причина>", "мьют. Длительность: 1d2h, 30м, 2h"),
            ("!warn <причина>", "варн (1 поинт). Сообщение нарушителя удаляется"),
            ("!ban <причина>", "бан. Если reply на стикер — пак автодобавляется"),
        ],
    ))

    # 3. Тихие команды (раскрыто)
    blocks.extend(_help_section(
        "Тихие команды (стелс, ephemeral модератору)",
        [
            ("!smute <длит> [причина]", "мьют без публичного сообщения"),
            ("!swarn [причина]", "варн. Нарушитель видит причину"),
            ("!sban [причина]", "бан без публичного сообщения"),
        ],
    ))

    # 4. Снятие наказаний / Прочее (раскрыто)
    blocks.extend(_help_section(
        "Снятие наказаний / Прочее",
        [
            ("!unmute / !unban", "снять ограничения (reply)"),
            ("!unwarn [N]", "снять N последних варнов (по умолчанию 1)"),
            ("!warns", "показать активные варны (в личку)"),
            ("!resetwarns", "обнулить варны (только админы)"),
            ("!resetmc [@user|tgid]", "обнулить счётчик автомьютов (только админы)"),
            ("!alarm on [длит] / !alarm off", "режим тревоги (усиленные ограничения)"),
            ("!idea <текст>", "предложить идею → GitHub Issue (только ЛС/modchat)"),
        ],
    ))

    # 5. Details: Настройки чатов (8 команд)
    blocks.append(_help_details(
        "Настройки чатов (в ЛС) — 8 команд",
        [
            ("/settings chat_id", "показать настройки чата"),
            ("/sethashtag chat_id #tag", "хэштег чата"),
            ("/setreport chat_id [report_chat_id]", "чат для отчётов (0 = сброс)"),
            ("/warns_mute chat_id N", "порог варнов до автомьюта"),
            ("/warns_ban chat_id N", "порог варнов до автобана"),
            ("/mute_duration chat_id 1d2h", "длительность мьюта по умолчанию"),
            ("/addadmin chat_id user_id", "добавить админа чата"),
            ("/deladmin chat_id user_id", "удалить админа чата"),
        ],
    ))

    # 6. Details: Фильтры (7 команд)
    blocks.append(_help_details(
        "Фильтры (в ЛС) — 7 команд",
        [
            ("/bansticker <pack|link> [delete|warn|mute|ban] [dur]", "забанить стикерпак"),
            ("/liststickers [chat_id]", "список забаненных стикерпаков"),
            ("/delsticker <pack> [chat_id]", "удалить стикерпак из бан-листа"),
            ("/linkfilter chat_id on|off", "фильтр ссылок"),
            ("/linkallow chat_id|global <domain>", "allowlist домена"),
            ("/linkallowlist [chat_id]", "показать allowlist"),
            ("/cas chat_id on|off", "CAS-проверка новых юзеров"),
        ],
        note=[
            RichTextBold(text="Word filter"),
            " удалён в v4.8.1 — используйте KeywordWatch через веб-панель ",
            RichTextUrl(text="/admin/keywords", url=f"{web_url}/admin/keywords"),
            " или групповые команды ",
            RichTextCode(text="!addkeyword"),
            " / ",
            RichTextCode(text="!delkeyword"),
            " / ",
            RichTextCode(text="!listkeywords"),
            ".",
        ],
    ))

    # 7. Details: Ночной режим (9 команд)
    blocks.append(_help_details(
        "Ночной режим (в ЛС) — 9 команд",
        [
            ("/nightmode chat_id <start> <end> [strict|text_only|none|custom]", "включить ночной режим"),
            ("/nightmode chat_id off", "выключить"),
            ("/nightmode chat_id tz <Europe/Moscow>", "часовой пояс"),
            ("/nightmode chat_id weekend <start> <end>", "расписание на сб/вс"),
            ("/nightmode chat_id weekend off", "сбросить (использовать будничное)"),
            ("/nightmode chat_id notify on|off [custom_text]", "уведомления входа/выхода"),
            ("/nightmode chat_id custom <perm>=0|1 ...", "точечные права (msgs, audios, docs, ...)"),
            ("/nightmode chat_id slowmode <day_sec> <night_sec>", "slow mode (0=выкл, 0..36400)"),
            ("/nightmode chat_id slowmode off", "выключить slow mode changes"),
        ],
    ))

    # 8. Details: Санитарные дни (6 команд)
    blocks.append(_help_details(
        "Санитарные дни (в ЛС) — 6 команд",
        [
            ("/sanitary chat_id", "показать список"),
            ("/sanitary chat_id add <YYYY-MM-DD>", "добавить день"),
            ("/sanitary chat_id add <start>:<end>", "добавить диапазон"),
            ("/sanitary chat_id remove <YYYY-MM-DD>", "удалить день/диапазон"),
            ("/sanitary chat_id clear", "очистить список"),
            ("/sanitary chat_id toggle", "вручную войти/выйти (для теста)"),
        ],
        note=[
            "Lockdown чата (модераторы не страдают); ночной режим пропускается.",
        ],
    ))

    # 9. Details: Прочее (1 команда)
    blocks.append(_help_details(
        "Прочее (в ЛС) — 1 команда",
        [
            ("/warndecay chat_id <days>", "срок действия варна (0 = отключено)"),
        ],
    ))

    # 10. Footer: веб-ссылка + версия бота
    blocks.append(InputRichBlockDivider())
    blocks.append(InputRichBlockFooter(text=[
        RichTextUrl(text=web_url, url=web_url),
        f"  {version_str}",
    ]))

    return InputRichMessage(blocks=blocks)


def _build_help_moderator_rich() -> InputRichMessage:
    """Сокращённая версия /help для moderator — через Rich Message.

    Только то, что модератор может использовать:
      • Громкие команды (!mute, !warn, !ban)
      • Тихие команды (!smute, !swarn, !sban)
      • Снятие наказаний (!unmute, !unban, !unwarn, !warns)
    Без !resetwarns (только админы) и !alarm (только админы).
    Без всех Details-блоков настроек — модератор не может их вызывать.
    Footer: веб-ссылка + версия + «Логин и пароль выдаёт SU».
    """
    try:
        from web_app import APP_VERSION
        version_str = APP_VERSION
    except Exception:
        version_str = "v4.8.5"
    web_url = WEB_PUBLIC_URL or "https://degraban.bothost.tech"

    blocks: list = []

    # H1 + Divider
    blocks.append(InputRichBlockSectionHeading(
        text="Дедушка Вобжак — команды модератора", size=1,
    ))
    blocks.append(InputRichBlockDivider())

    # Громкие команды
    blocks.extend(_help_section(
        "Громкие команды (reply на нарушителя)",
        [
            ("!mute <длит> <причина>", "мьют. Длительность: 1d2h, 30м, 2h"),
            ("!warn <причина>", "варн (1 поинт). Сообщение нарушителя удаляется"),
            ("!ban <причина>", "бан. Если reply на стикер — пак автодобавляется"),
        ],
    ))

    # Тихие команды
    blocks.extend(_help_section(
        "Тихие команды (стелс, ephemeral модератору)",
        [
            ("!smute <длит> [причина]", "мьют без публичного сообщения"),
            ("!swarn [причина]", "варн. Нарушитель видит причину"),
            ("!sban [причина]", "бан без публичного сообщения"),
        ],
    ))

    # Снятие наказаний
    blocks.extend(_help_section(
        "Снятие наказаний",
        [
            ("!unmute / !unban", "снять ограничения (reply)"),
            ("!unwarn [N]", "снять N последних варнов (по умолчанию 1)"),
            ("!warns", "показать активные варны (в личку)"),
        ],
    ))

    # Идеи
    blocks.extend(_help_section(
        "Предложить идею",
        [
            ("!idea <текст>", "отправить идею в GitHub Issue (только ЛС/modchat, до 200 символов)"),
        ],
    ))

    # Footer: веб-ссылка + версия + подсказка про SU
    blocks.append(InputRichBlockDivider())
    blocks.append(InputRichBlockFooter(text=[
        RichTextUrl(text=web_url, url=web_url),
        f"  {version_str}",
    ]))
    blocks.append(InputRichBlockParagraph(text=[
        "Логин и пароль выдаёт SU при создании аккаунта. ",
        "Наказания нельзя применять к себе и к другим модераторам этого чата.",
    ]))

    return InputRichMessage(blocks=blocks)


@router.message(F.chat.type == "private", Command("help"))
async def cmd_help(message: types.Message) -> None:
    """Показывает список команд через Rich Message.

    v4.8.3.2: полный редизайн. Раньше _HELP_FULL_TEXT был 4621 символ
    (лимит Telegram — 4096) → SU/admin получали BadRequest "message is too long"
    и НЕ получали /help вообще. Теперь: Rich Message со сворачиваемыми
    Details-блоками для длинных секций настроек.

    Логика выбора версии (без изменений):
      • ADMIN_IDS env → полный help
      • WebUser role='su' или 'admin', is_active=True → полный help
      • WebUser role='moderator', is_active=True → сокращённый help
        (только групповые модераторские команды + ссылка на веб-панель)
      • Все остальные (посторонние) — молчим (стелс сохраняется)

    Без fallback на HTML — Rich Message поддерживается во всех
    современных клиентах Telegram. Если send_rich_message упадёт —
    логируем warning, пользователь не получит ответ.
    """
    user_id = message.from_user.id

    # 1. Глобальные супер-админы из env — всегда полный help
    if user_id in ADMIN_IDS:
        try:
            await message.bot.send_rich_message(
                chat_id=message.chat.id,
                rich_message=_build_help_full_rich(),
            )
        except TelegramAPIError as e:
            logger.warning("cmd_help: send_rich_message (full, ADMIN_IDS) failed for user_id=%s: %s",
                           user_id, e)
        return

    # 2. Ищем веб-профиль по tg_user_id
    async with async_session() as session:
        wu = (await session.execute(
            select(WebUser).where(WebUser.tg_user_id == user_id)
        )).scalar_one_or_none()

    # Посторонний или деактивированный — молчим (стелс)
    if wu is None or not wu.is_active:
        return

    # 3. SU / admin → полный help
    if wu.role in ("su", "admin"):
        try:
            await message.bot.send_rich_message(
                chat_id=message.chat.id,
                rich_message=_build_help_full_rich(),
            )
        except TelegramAPIError as e:
            logger.warning("cmd_help: send_rich_message (full, role=%s) failed for user_id=%s: %s",
                           wu.role, user_id, e)
        return

    # 4. Moderator → сокращённый help
    if wu.role == "moderator":
        try:
            await message.bot.send_rich_message(
                chat_id=message.chat.id,
                rich_message=_build_help_moderator_rich(),
            )
        except TelegramAPIError as e:
            logger.warning("cmd_help: send_rich_message (moderator) failed for user_id=%s: %s",
                           user_id, e)
        return

    # Неизвестная роль — safe default, молчим.


# ═══════════════════════════════════════════════════════════════════════════
# СТЕЛС: Catch-all — молча игнорируем ВСЕ сообщения от не-админов
# Эти обработчики стоят ПОСЛЕ всех остальных, поэтому срабатывают
# только если ни один специфичный хэндлер не подошёл.
# ═══════════════════════════════════════════════════════════════════════════

@router.message(F.chat.type == "private", Command("start"))
async def private_start_handler(message: types.Message) -> None:
    """v4.7.0: /start в ЛС боту — активирует pending WebUser.

    Логика:
      1. Ищем WebUser по tg_user_id = message.from_user.id.
      2. Если найден и is_pending=True:
         - Генерируем пароль (16 символов base64url).
         - password_hash = _hash_password(password).
         - is_active = True, is_pending = False.
         - Скачиваем аватарку (best-effort).
         - Шлём DM с credentials (login + password под спойлером).
      3. Если найден и is_active=True (уже активирован):
         - Шлём "ты уже активен, логин: X" (без пароля — он знает).
      4. Если найден и is_active=False (деактивирован SU):
         - Молчим (стелс — не выдаём существование бота).
      5. Если не найден:
         - Молчим (стелс — не даём посторонним знать что бот умеет /start).

    Безопасность: бот НИКОГДА не отвечает посторонним. Только pending/active
    WebUser получают ответ. Это сохраняет стелс-режим бота.
    """
    if not message.from_user:
        return
    tg_uid = message.from_user.id

    async with async_session() as session:
        wu = (await session.execute(
            select(WebUser).where(WebUser.tg_user_id == tg_uid)
        )).scalar_one_or_none()

        if wu is None:
            # Посторонний — молчим.
            return

        if wu.is_su:
            # SU не должен активироваться через /start (его учётка создаётся
            # из env при первом запуске). Молчим — он уже знает свой доступ.
            return

        if not wu.is_pending and not wu.is_active:
            # Деактивирован SU — молчим.
            return

        if wu.is_active and not wu.is_pending:
            # Уже активирован — напомним логин (без пароля).
            login = wu.username
            web_url = WEB_PUBLIC_URL or "https://degraban.bothost.tech"
            web_root_url = web_url + "/"
            greeting_name = ""
            if wu.tg_first_name:
                greeting_name = f", {wu.tg_first_name}"
            try:
                await message.bot.send_rich_message(
                    chat_id=tg_uid,
                    rich_message=InputRichMessage(blocks=[
                        InputRichBlockSectionHeading(
                            text=f"👋 Уже активны{greeting_name}", size=2,
                        ),
                        InputRichBlockParagraph(text=[
                            "Ваша учётка уже активирована. ",
                            "Веб-панель: ",
                            RichTextUrl(text=web_root_url, url=web_root_url),
                        ]),
                        InputRichBlockParagraph(text=[
                            "Логин: ", RichTextBold(text=login),
                        ]),
                        InputRichBlockParagraph(text=[
                            "Если забыли пароль — используйте ",
                            RichTextBold(text="«Сменить пароль»"),
                            " в разделе Profile (если ещё помните текущий) ",
                            "либо попросите SU сбросить пароль через /admin/users.",
                        ]),
                        InputRichBlockFooter(
                            text=f"⏱ {datetime.now(MSK).strftime('%d.%m.%Y %H:%M')} МСК"
                        ),
                    ]),
                )
            except TelegramAPIError as e:
                logger.warning(
                    "private_start: send already-active DM failed for tg_uid=%s: %s",
                    tg_uid, e,
                )
            return

        # is_pending=True → активируем.
        password = secrets.token_urlsafe(12)[:16]
        wu.password_hash = _hash_password(password)
        wu.is_active = True
        wu.is_pending = False
        wu.last_login_at = datetime.now(timezone.utc)
        await session.commit()

        login = wu.username
        role = wu.role or "moderator"
        first_name = wu.tg_first_name

        # Скачиваем аватарку (best-effort). Импортируем тут, чтобы избежать
        # циклического импорта с web_app на верхнем уровне.
        try:
            from web_app import _fetch_and_save_avatar
            # v4.8.7: strong ref — аватарка скачивается в фоне, GC не убьёт.
            _spawn_background_task(
                _fetch_and_save_avatar(message.bot, tg_uid),
                label="fetch_avatar",
            )
        except ImportError:
            pass

        logger.info(
            "private_start: activated pending WebUser tg_uid=%s login=%s role=%s",
            tg_uid, login, role,
        )

    # Шлём DM с credentials (вне сессии БД).
    web_url = WEB_PUBLIC_URL or "https://degraban.bothost.tech"
    web_root_url = web_url + "/"
    greeting_name = f", {first_name}" if first_name else ""

    if role == "moderator":
        heading = f"🔎 Доступ к веб-панели (модератор){greeting_name}"
        rights_line = (
            "Ваши права: только просмотр логов нарушителей (раздел Dashboard). "
            "Управление админами, чатами и модераторами недоступно."
        )
    else:
        heading = f"🎉 Доступ к веб-панели (админ){greeting_name}"
        rights_line = (
            "Ваши права: управление модераторами чатов и настройками чатов "
            "(хэштег, пороги варнов), а также просмотр логов."
        )

    try:
        await message.bot.send_rich_message(
            chat_id=tg_uid,
            rich_message=InputRichMessage(blocks=[
                InputRichBlockSectionHeading(text=heading, size=2),
                InputRichBlockParagraph(text=[
                    "Вас добавили в систему «Дедушка Вобжак» (авто-обнаружение "
                    "по админ-правам в чате). ",
                    "Веб-панель: ",
                    RichTextUrl(text=web_root_url, url=web_root_url),
                ]),
                InputRichBlockParagraph(text=rights_line),
                InputRichBlockParagraph(text="Данные для входа (скрыты под спойлером):"),
                InputRichBlockParagraph(
                    text=RichTextSpoiler(
                        text=[
                            "Логин: ", RichTextBold(text=login), "\n",
                            "Пароль: ", RichTextBold(text=password),
                        ]
                    )
                ),
                InputRichBlockParagraph(text=[
                    "🔐 После первого входа смените пароль: раздел ",
                    RichTextBold(text="Profile"),
                    " → блок ",
                    RichTextBold(text="Change my password"),
                    ".",
                ]),
                InputRichBlockFooter(
                    text=f"⏱ {datetime.now(MSK).strftime('%d.%m.%Y %H:%M')} МСК"
                ),
            ]),
        )
    except TelegramAPIError as e:
        logger.warning(
            "private_start: send credentials DM failed for tg_uid=%s: %s",
            tg_uid, e,
        )


@router.message(F.chat.type == "private")
async def stealth_catchall_private(message: types.Message) -> None:
    """Стелс: молча игнорируем ВСЕ сообщения в личке от не-админов.
    Ни /start, ни /help, ни любой другой текст — бот не реагирует.
    Если это админ, но команда не распознана — тоже молчим (нет подсказок).
    """
    # Просто return — бот НИКОГДА не отвечает обычным юзерам.
    # Даже если это админ отправил неизвестную команду — молчим,
    # чтобы случайно не выдать существование бота.
    return


# ═══════════════════════════════════════════════════════════════════════════
# v4.5.2: Автоматические фильтры (CAS, sticker pack, word, link)
# ──────────────────────────────────────────────────────────────────────────
# Эти хэндлеры стоят ПЕРЕД stealth_catchall_group, чтобы перехватывать
# сообщения до того, как их проигнорирует catchall. Каждый хэндлер
# возвращает управление (return) если фильтр не сработал, давая шанс
# следующему. Если фильтр сработал — сообщение удаляется и применяется
# наказание, дальше обработка не идёт.
# ═══════════════════════════════════════════════════════════════════════════


@router.message(F.chat.type.in_(["group", "supergroup"]), F.new_chat_members)
async def handle_new_members(message: types.Message) -> None:
    """v4.5.2 (#2): CAS-проверка новых участников чата.

    Срабатывает когда в чат заходит новый юзер (или несколько). Если для
    чата включена CAS-проверка (cas_check_enabled=True) — для каждого
    нового юзера делается запрос к api.cas.chat. Если юзер в базе —
    автоматический бан + удаление join-сообщения.

    Fail-open: при сетевой ошибке / недоступности CAS — пропускаем юзера
    (лучше пропустить спамера, чем заблокировать вход при сбое сервиса).
    """
    try:
        async with async_session() as session:
            settings = await _get_chat_settings(session, message.chat.id)
            cas_enabled = settings.cas_check_enabled if settings else False
    except Exception as e:
        logger.warning("handle_new_members: DB error: %s (fail-open)", e)
        return

    if not cas_enabled:
        return  # CAS не включён для этого чата — пропускаем

    for member in message.new_chat_members:
        # Не проверяем ботов (они не бывают в CAS-базе, а проверка лишняя)
        if member.is_bot:
            continue
        # v4.7.30: exempt модераторов/админов от CAS-проверки.
        # Сценарий: модератор был ранее замечен в CAS-базе (в других чатах,
        # давно), но в нашем чате он теперь полноценный модератор через
        # ChatAdmin или ADMIN_IDS. Без exempt бот его забанит при входе.
        # Проверка best-effort — если БД лежит, fail-open (пропускаем, не банним).
        try:
            async with async_session() as admin_session:
                member_is_adm = await _is_admin(admin_session, message.chat.id, member.id)
        except Exception as e:
            logger.warning(
                "CAS check: _is_admin lookup failed for new member %s in chat %s: %s "
                "(fail-open — will skip CAS check for this user)",
                member.id, message.chat.id, e,
            )
            member_is_adm = True  # fail-open: лучше пропустить, чем забанить
        if member_is_adm:
            logger.info(
                "CAS check: exempt admin/mod %s joining chat %s (skipping CAS check)",
                member.id, message.chat.id,
            )
            continue
        is_banned, reason = await _cas_check_user(member.id)
        if not is_banned:
            continue
        # Юзер в CAS-базе — банним
        try:
            # v4.8.7: tg_safe_call — ретраит при 429/RetryAfter.
            await tg_safe_call(
                lambda: message.bot.ban_chat_member(
                    chat_id=message.chat.id, user_id=member.id,
                ),
                label="CAS_auto_ban",
            )
            # v4.7.27: помечаем бан от бота — для дедупликации в on_chat_member_updated
            _mark_bot_ban(message.chat.id, member.id)
            logger.info(
                "CAS auto-ban: user_id=%s in chat %s (reason: %s)",
                member.id, message.chat.id, reason,
            )
            # Сохраняем запись о бане
            async with async_session() as session:
                await _upsert_user(session, member.id, member.username,
                                   member.first_name, member.last_name)
                # mod_id=0 — системная запись (бот)
                await _upsert_moderator(session, 0, None, "CAS System")
                await _save_punishment(
                    session, member.id, 0, message.chat.id,
                    "ban", None, f"CAS auto-ban: {reason}", None,
                )
            # Уведомляем в репорт-чат
            await _send_report(
                bot=message.bot, chat_id=message.chat.id, target=member,
                action_type="ban", reason=f"CAS auto-ban: {reason}",
                mod=None,
            )
        except TelegramAPIError as e:
            logger.error("CAS ban failed for user %s: %s", member.id, e)
    # Удаляем join-сообщение в любом случае (если CAS включён — чище чат)
    try:
        await message.delete()
    except TelegramAPIError:
        pass


@router.message(F.chat.type.in_(["group", "supergroup"]), F.sticker)
async def handle_sticker_message(message: types.Message) -> None:
    """v4.5.2 (#15): Проверка стикеров по бан-листу стикерпаков.

    Если у стикера есть ``set_name`` (он из пака, не анонимный) и пак
    находится в BannedStickerPack для этого чата (или global) —
    применяется настроенное наказание (delete/warn/mute/ban) и сообщение
    удаляется. Анонимные стикеры (без set_name) не проверяются.

    v4.7.30: модераторы/админы чата exempt от наказания за забаненные
    стикерпаки (Баг #4 аудита v4.7.30). Без этого модератор, тестирующий
    фильтр, мог быть забанен/замьючен собственным ботом. Сообщение
    модератора тоже НЕ удаляется — чтобы он видел что стикер вообще
    отправился. Логируем warning для аудита.
    """
    sticker = message.sticker
    if not sticker or not sticker.set_name:
        return  # анонимный стикер без пака — пропускаем

    chat_id = message.chat.id
    try:
        async with async_session() as session:
            pack = await _check_banned_sticker(session, chat_id, sticker.set_name)
    except Exception as e:
        logger.warning("handle_sticker_message: DB error: %s (fail-open)", e)
        return

    if pack is None:
        return  # пак не в бан-листе — пропускаем к catchall

    target = message.from_user

    # v4.7.30: exempt модераторов/админов (Баг #4 аудита v4.7.30).
    # Аналогично _check_via_bot_filter — админам можно всё.
    # ВАЖНО: проверяем ДО удаления сообщения — иначе модератор не увидит
    # что его стикер вообще дошёл, и подумает что бот сломан.
    try:
        async with async_session() as session:
            is_adm = await _is_admin(session, chat_id, target.id)
    except Exception as e:
        logger.warning(
            "handle_sticker_message: _is_admin check failed for user %s in chat %s: %s "
            "(fail-open — will apply punishment)",
            target.id, chat_id, e,
        )
        is_adm = False
    if is_adm:
        logger.info(
            "Sticker filter: exempt admin/mod %s in chat %s (pack='%s' is banned "
            "but user has moderator rights — skipping punishment and deletion)",
            target.id, chat_id, sticker.set_name,
        )
        return  # не удаляем, не наказываем — пропускаем к catchall

    target_content = f"🎭 [Стикер из пака: {sticker.set_name}]"

    # Удаляем сообщение со стикером
    try:
        await message.delete()
    except TelegramAPIError as e:
        logger.warning("Cannot delete sticker message: %s", e)

    # Применяем наказание
    punishment = pack.punishment or "delete"
    if punishment == "delete":
        # Просто удаление — уже сделали выше. Логируем для аудита.
        logger.info(
            "Sticker pack '%s' deleted in chat %s (user %s, punishment=delete)",
            sticker.set_name, chat_id, target.id,
        )
        return

    # Для warn/mute/ban — нужен mod_id (используем 0 = system)
    async with async_session() as session:
        await _upsert_user(session, target.id, target.username,
                           target.first_name, target.last_name)
        await _upsert_moderator(session, 0, None, "Sticker Filter")
        await session.commit()  # v4.5.2: фиксируем user/moderator до новой сессии

    if punishment == "warn":
        async with async_session() as session:
            await _save_punishment(
                session, target.id, 0, chat_id,
                "warn", 1, f"Banned sticker pack: {sticker.set_name}",
                target_content,
            )
        logger.info(
            "Sticker pack '%s' warn issued in chat %s (user %s)",
            sticker.set_name, chat_id, target.id,
        )
        return

    if punishment == "mute":
        mute_dur = pack.mute_duration or 3600
        # v4.8.4: прогрессивный автомьют — base + (count * 60 сек).
        async with async_session() as session:
            auto_count = await _get_automute_count(session, chat_id, target.id)
        mute_dur = mute_dur + (auto_count * 60)
        until_date = int(datetime.now(timezone.utc).timestamp()) + mute_dur
        try:
            # v4.8.7: tg_safe_call — ретраит при 429/RetryAfter.
            await tg_safe_call(
                lambda: message.bot.restrict_chat_member(
                    chat_id=chat_id, user_id=target.id,
                    permissions=_mute_permissions(),
                    until_date=until_date,
                ),
                label="sticker_auto_mute",
            )
            async with async_session() as session:
                await _save_punishment(
                    session, target.id, 0, chat_id,
                    "mute", mute_dur,
                    f"Banned sticker pack: {sticker.set_name}",
                    target_content,
                )
                # v4.8.4: инкремент счётчика автомьютов.
                new_count = await _increment_automute_count(session, chat_id, target.id)
                await session.commit()
            logger.info(
                "Sticker pack '%s' mute issued in chat %s (user %s, %s, "
                "automute_count %d→%d)",
                sticker.set_name, chat_id, target.id, _format_duration(mute_dur),
                auto_count, new_count,
            )
        except TelegramAPIError as e:
            logger.error("Sticker mute failed: %s", e)
        return

    if punishment == "ban":
        try:
            # v4.8.7: tg_safe_call — ретраит при 429/RetryAfter.
            await tg_safe_call(
                lambda: message.bot.ban_chat_member(chat_id=chat_id, user_id=target.id),
                label="sticker_auto_ban",
            )
            # v4.7.27: помечаем бан от бота — для дедупликации в on_chat_member_updated
            _mark_bot_ban(chat_id, target.id)
            async with async_session() as session:
                await _save_punishment(
                    session, target.id, 0, chat_id,
                    "ban", None,
                    f"Banned sticker pack: {sticker.set_name}",
                    target_content,
                )
            logger.info(
                "Sticker pack '%s' ban issued in chat %s (user %s)",
                sticker.set_name, chat_id, target.id,
            )
        except TelegramAPIError as e:
            logger.error("Sticker ban failed: %s", e)
        return


async def _check_via_bot_filter(message: types.Message, chat_id: int) -> bool:
    """v4.7.24: Via-bot rate-limit filter.

    Возвращает True если сообщение обработано (delete + mute) и обработку
    следует остановить. False — можно продолжать к word/link filter.

    Логика:
      • message.via_bot is None → False (не our case)
      • message.from_user is None → False (edge case — channel post)
      • filter disabled → False
      • user is admin → False (админам можно)
      • запись в _via_bot_rate_limit свежее rate_limit секунд → True (block)
      • иначе → False (allow, обновляем timestamp)

    Rate-limit — per (chat_id, user_id, bot_id). Юзер может отправить
    1 сообщение @Bot1 + 1 сообщение @Bot2 в одном окне. Это более
    user-friendly, чем «1 сообщение всем ботам суммарно».

    Stealth: сообщение молча удаляется, юзер мутичится без уведомления.
    """
    vb = message.via_bot
    fu = message.from_user
    if vb is None or fu is None:
        return False

    try:
        async with async_session() as session:
            settings = await _get_chat_settings(session, chat_id)
            if not settings or not settings.via_bot_filter_enabled:
                return False
            # Админам — всегда можно
            if await _is_admin(session, chat_id, fu.id):
                return False
            rate_limit = settings.via_bot_rate_limit_seconds or 300
            mute_min = settings.via_bot_mute_minutes or 10
    except Exception as e:
        logger.warning("Via-bot filter: DB error: %s (fail-open)", e)
        return False

    bot_id = vb.id
    bot_username = (vb.username or "").lower() or "unknown"

    _via_bot_rate_limit_cleanup()
    key = (chat_id, fu.id, bot_id)
    now = datetime.now(timezone.utc)
    last = _via_bot_rate_limit.get(key)

    if last is None or (now - last).total_seconds() >= rate_limit:
        # Разрешаем, обновляем timestamp
        _via_bot_rate_limit[key] = now
        logger.debug(
            "Via-bot filter: allowed @%s in chat %s (user %s, last=%s, gap=%ss)",
            bot_username, chat_id, fu.id, last,
            int((now - last).total_seconds()) if last else -1,
        )
        return False

    # Превышение rate-limit — delete + mute + save punishment
    gap_sec = int((now - last).total_seconds()) if last else 0
    reason = (
        f"Via-bot filter: @{bot_username} (rate-limit {rate_limit}s exceeded, "
        f"last message {gap_sec}s ago)"
    )
    target_content = (message.text or message.caption or "")[:500] or None

    try:
        await message.delete()
    except TelegramAPIError as e:
        logger.warning("Via-bot filter: cannot delete message: %s", e)

    dur = max(mute_min, 1) * 60
    # v4.8.4: прогрессивный автомьют — base + (count * 60 сек).
    async with async_session() as session:
        auto_count = await _get_automute_count(session, chat_id, fu.id)
    dur = dur + (auto_count * 60)
    until_date = int(now.timestamp()) + dur
    try:
        # v4.8.7: tg_safe_call — ретраит при 429/RetryAfter.
        await tg_safe_call(
            lambda: message.bot.restrict_chat_member(
                chat_id=chat_id, user_id=fu.id,
                permissions=_mute_permissions(),
                until_date=until_date,
            ),
            label="via_bot_filter_auto_mute",
        )
        async with async_session() as session:
            await _upsert_user(session, fu.id, fu.username,
                               fu.first_name, fu.last_name)
            await _upsert_moderator(session, 0, None, "Via-bot Filter")
            await session.commit()
        async with async_session() as session:
            await _save_punishment(
                session, fu.id, 0, chat_id,
                "mute", dur, reason, target_content,
            )
            # v4.8.4: инкремент счётчика автомьютов (после успешного мьюта).
            new_count = await _increment_automute_count(session, chat_id, fu.id)
            await session.commit()
        logger.info(
            "Via-bot filter (mute %s) in chat %s (user %s, bot @%s): "
            "rate-limit exceeded (last=%ss ago, limit=%ss, "
            "automute_count %d→%d)",
            _format_duration(dur), chat_id, fu.id, bot_username,
            gap_sec, rate_limit, auto_count, new_count,
        )
        # ── v4.8.1: публичное сообщение в чат (фиксированный текст) ───
        # Формат: «Пользователь "<display_name>" задолбал срать в чат
        # и был замутан на "<duration>"». Без указания причины — она
        # техническая (rate-limit details) и чату неинтересна.
        try:
            display_name = _user_display_name(fu)
            name_safe = html.escape(display_name, quote=False)
            dur_safe = html.escape(_format_duration(dur), quote=False)
            await message.bot.send_message(
                chat_id=chat_id,
                text=(
                    f'Пользователь "<b>{name_safe}</b>" задолбал срать в чат '
                    f'и был замутан на "<b>{dur_safe}</b>"'
                ),
                parse_mode="HTML",
            )
        except TelegramAPIError as pub_e:
            logger.warning(
                "Via-bot filter: public notice failed (chat=%s user=%s): %s",
                chat_id, fu.id, pub_e,
            )
    except TelegramAPIError as e:
        logger.error("Via-bot filter mute failed: %s", e)

    return True


@router.message(F.chat.type.in_(["group", "supergroup"]))
async def handle_content_filters(message: types.Message) -> None:
    """v4.5.2 (#7, #8): Link filter для текстовых сообщений + keyword-watch.

    Срабатывает на текстовых сообщениях (и caption у медиа). Если сработал
    link filter — применяется chat_settings.link_filter_action.

    v4.8.1: Word filter удалён (deprecated в v4.8.0, заменён на keyword-watch
    как notify-механизм). Теперь здесь только link filter.

    v4.7.24: ПЕРВЫМ проверяется via-bot rate-limit filter (для сообщений
    с message.via_bot is not None). Если rate-limit превышен — delete + mute,
    link filter не запускается. Если в пределах grace-окна —
    пропускаем к link filter (сообщение может нарушить и его).

    v4.7.30: модераторы/админы чата exempt от word/link фильтров (Баг #4
    аудита v4.7.30). Без этого модератор, тестирующий фильтр запрещённым
    словом или ссылкой, мог быть забанен/замьючен собственным ботом.
    Проверка делается ПОСЛЕ via-bot фильтра (он уже имеет свой exempt)
    и ДО word/link проверки — чтобы лишний раз не дёргать БД если юзер
    не модератор.

    v4.8.0: добавлен keyword-watch (замена word_filter). Keyword-watch
    проверяется ПОСЛЕ link_filter. Принцип:
      • День: только notify в modchat (не банит, не удаляет).
      • Ночь (active night mode): если ban_in_night_mode=True — автобан.
        Иначе — notify в modchat.
      • Exempt: модераторы/админы/SU пропускаются.
    Keyword-watch НЕ удаляет сообщение и НЕ применяет warn/mute —
    это чисто notify-механизм (с опциональным автобаном ночью).

    v4.8.1: WordFilter удалён (deprecated в v4.8.0). Link filter остаётся
    как единственный «действующий» content-filter (в дополнение к
    keyword-watch как notify-механизму).

    Если ни один фильтр не сработал — возвращаем управление (return),
    давая шанс stealth_catchall_group.
    """
    chat_id = message.chat.id

    # v4.7.24: via-bot rate-limit filter — ПЕРВЫЙ (до text-check, т.к.
    # via_bot может быть на медиа-сообщениях без text/caption).
    if await _check_via_bot_filter(message, chat_id):
        return  # Сообщение удалено, юзер замучен — стоп.

    text = message.text or message.caption or ""
    if not text:
        return

    # v4.7.30: exempt модераторов/админов от word/link фильтров (Баг #4).
    # _check_via_bot_filter уже имеет свой exempt, но он работает только
    # для via-bot сообщений. Здесь покрываем обычные текстовые сообщения.
    target_for_exempt = message.from_user
    is_adm = False
    if target_for_exempt is not None:
        try:
            async with async_session() as session:
                is_adm = await _is_admin(session, chat_id, target_for_exempt.id)
        except Exception as e:
            logger.warning(
                "handle_content_filters: _is_admin check failed for user %s in chat %s: %s "
                "(fail-open — will apply filters)",
                target_for_exempt.id, chat_id, e,
            )
            is_adm = False
        if is_adm:
            # Модератору можно — пропускаем word/link проверку.
            # Не логируем на INFO (часто — модераторы пишут в чат нормально),
            # но debug-уровень оставляем для отладки.
            logger.debug(
                "Content filter: exempt admin/mod %s in chat %s (skipping word/link check)",
                target_for_exempt.id, chat_id,
            )
            # v4.8.0: keyword-watch тоже exempt для модераторов.
            return

    try:
        async with async_session() as session:
            settings = await _get_chat_settings(session, chat_id)
            # v4.8.1: word_filter удалён (deprecated в v4.8.0, заменён на
            # KeywordWatch). Здесь остаётся только link filter.
            link_filter_on = settings.link_filter_enabled if settings else False
            link_filter_action = settings.link_filter_action if settings else "delete"
            if link_filter_on:
                has_blocked, blocked_domains = await _link_filter_check(session, chat_id, text)
            else:
                has_blocked, blocked_domains = False, []
    except Exception as e:
        logger.warning("handle_content_filters: DB error: %s (fail-open)", e)
        return

    # ── v4.8.0: Keyword-watch ───────────────────────────────────────────
    # Запускается всегда (если есть активные фразы в БД). Не заменяет
    # word/link filter — работает параллельно. Если word/link сработали —
    # keyword-watch тоже сработает (но сообщение уже удалено — это OK).
    #
    # Day mode: только notify в modchat.
    # Night mode: если ban_in_night_mode=True — автобан + notify.
    #             Иначе — notify.
    try:
        from db import async_session as _kw_session
        from modchat import (
            _check_keyword_rate_limit as _kw_rl,
        )
        from modchat import (
            _keyword_watch_match as _kw_match,
        )
        from modchat import (
            _send_keyword_notify_to_modchat as _kw_notify,
        )
        async with _kw_session() as kw_session:
            kw_matches = await _kw_match(kw_session, text)
            # Проверяем night mode (для автобана).
            cs_kw = await _get_chat_settings(kw_session, chat_id)
            is_night = bool(cs_kw and cs_kw.night_mode_currently_active)
        if kw_matches:
            # Multiplexing: если несколько совпадений — отправляем одно
            # уведомление. Rate-limit проверяется per-phrase.
            allowed_phrases: list = []
            suppressed_counts: dict[str, int] = {}
            for kw in kw_matches:
                phrase_lower = kw.phrase.lower()
                allowed, suppressed = _kw_rl(chat_id, phrase_lower)
                if allowed:
                    allowed_phrases.append(kw)
                if suppressed > 0:
                    suppressed_counts[kw.phrase] = suppressed
            if allowed_phrases:
                try:
                    await _kw_notify(
                        bot=message.bot, source_chat_id=chat_id,
                        message=message, matches=allowed_phrases,
                        suppressed_counts=suppressed_counts or None,
                    )
                except Exception as e:
                    logger.debug("Keyword notify failed: %s", e)
            # Автобан ночью для фраз с ban_in_night_mode=True.
            if is_night:
                target = message.from_user
                if target is not None:
                    ban_phrases = [kw for kw in kw_matches if kw.ban_in_night_mode]
                    if ban_phrases:
                        phrases_str = ", ".join(f"«{kw.phrase}»" for kw in ban_phrases)
                        reason = f"Keyword-watch (night mode auto-ban): {phrases_str}"
                        try:
                            # v4.8.7: tg_safe_call — ретраит при 429/RetryAfter.
                            await tg_safe_call(
                                lambda: message.bot.ban_chat_member(
                                    chat_id=chat_id, user_id=target.id,
                                ),
                                label="keyword_watch_auto_ban",
                            )
                            # v4.7.27: помечаем бан от бота — для дедупликации.
                            _mark_bot_ban(chat_id, target.id)
                            async with async_session() as session:
                                await _save_punishment(
                                    session, target.id, 0, chat_id,
                                    "ban", None, reason, text[:500] if text else None,
                                )
                            logger.info(
                                "Keyword-watch auto-ban (night mode) in chat %s (user %s): %s",
                                chat_id, target.id, reason,
                            )
                        except TelegramAPIError as e:
                            logger.error(
                                "Keyword-watch auto-ban failed in chat %s: %s",
                                chat_id, e,
                            )
    except Exception as e:
        logger.warning("handle_content_filters: keyword-watch error: %s (continuing)", e)

    # Если ничего не сработало — выходим, даст шанс catchall
    if not has_blocked:
        return

    target = message.from_user

    # ── Определяем, какое действие применить ──
    # v4.8.1: word_filter удалён. Действие — только из link_filter_action
    # (delete/warn/mute/ban). mute_dur — None (link filter не задаёт свою
    # длительность мьюта; используем дефолт 3600 сек как и раньше).
    action = link_filter_action
    reason = f"Link filter: blocked domains: {', '.join(blocked_domains[:3])}"
    mute_dur = None

    # Удаляем сообщение (для всех действий кроме бан — бан и так кикает)
    try:
        await message.delete()
    except TelegramAPIError as e:
        logger.warning("Cannot delete filtered message: %s", e)

    target_content = text[:500] if text else None

    # Сохраняем пользователя и системного модератора
    async with async_session() as session:
        await _upsert_user(session, target.id, target.username,
                           target.first_name, target.last_name)
        await _upsert_moderator(session, 0, None, "Content Filter")
        await session.commit()  # v4.5.2: фиксируем до новой сессии

    if action == "delete":
        logger.info("Content filter (delete) in chat %s: %s", chat_id, reason)
        return

    if action == "warn":
        async with async_session() as session:
            await _save_punishment(
                session, target.id, 0, chat_id,
                "warn", 1, reason, target_content,
            )
        logger.info("Content filter (warn) in chat %s (user %s): %s",
                    chat_id, target.id, reason)
        return

    if action == "mute":
        dur = mute_dur or 3600
        # v4.8.4: прогрессивный автомьют — base + (count * 60 сек).
        async with async_session() as session:
            auto_count = await _get_automute_count(session, chat_id, target.id)
        dur = dur + (auto_count * 60)
        until_date = int(datetime.now(timezone.utc).timestamp()) + dur
        try:
            # v4.8.7: tg_safe_call — ретраит при 429/RetryAfter.
            await tg_safe_call(
                lambda: message.bot.restrict_chat_member(
                    chat_id=chat_id, user_id=target.id,
                    permissions=_mute_permissions(),
                    until_date=until_date,
                ),
                label="content_filter_auto_mute",
            )
            async with async_session() as session:
                await _save_punishment(
                    session, target.id, 0, chat_id,
                    "mute", dur, reason, target_content,
                )
                # v4.8.4: инкремент счётчика автомьютов.
                new_count = await _increment_automute_count(session, chat_id, target.id)
                await session.commit()
            logger.info(
                "Content filter (mute %s) in chat %s (user %s): %s "
                "(automute_count %d→%d)",
                _format_duration(dur), chat_id, target.id, reason,
                auto_count, new_count,
            )
        except TelegramAPIError as e:
            logger.error("Content filter mute failed: %s", e)
        return

    if action == "ban":
        try:
            # v4.8.7: tg_safe_call — ретраит при 429/RetryAfter.
            await tg_safe_call(
                lambda: message.bot.ban_chat_member(chat_id=chat_id, user_id=target.id),
                label="content_filter_auto_ban",
            )
            # v4.7.27: помечаем бан от бота — для дедупликации в on_chat_member_updated
            _mark_bot_ban(chat_id, target.id)
            async with async_session() as session:
                await _save_punishment(
                    session, target.id, 0, chat_id,
                    "ban", None, reason, target_content,
                )
            logger.info("Content filter (ban) in chat %s (user %s): %s",
                        chat_id, target.id, reason)
        except TelegramAPIError as e:
            logger.error("Content filter ban failed: %s", e)
        return


@router.message(F.chat.type.in_(["group", "supergroup"]))
async def stealth_catchall_group(message: types.Message) -> None:
    """Стелс: молча игнорируем все сообщения в группах,
    которые не были обработаны модераторскими командами.
    Сюда попадают: обычные сообщения, /start, /help и т.д.

    v4.4.7: Побочный эффект — при первом сообщении в чате создаём
    chat_settings (если ещё нет) и уведомляем SU. Это надёжнее, чем
    my_chat_member, т.к. Telegram не всегда присылает my_chat_member
    при добавлении бота (зависит от прав).
    """
    # v4.4.7: создаём chat_settings для чата, если ещё нет
    try:
        async with async_session() as session:
            settings, created = await _ensure_chat_settings(
                session,
                chat_id=message.chat.id,
                title=message.chat.title,
            )
            if created:
                await session.commit()
                _new_chat_id = settings.chat_id
                _new_chat_title = settings.title or message.chat.title
                # Уведомляем SU вне сессии (best-effort)
                _spawn_background_task(
                    _notify_su_about_chat(message.bot, _new_chat_id, _new_chat_title),
                    label="notify_su_group",
                )
                logger.info(
                    "Auto-detected new chat: id=%s title='%s' — notified SU",
                    _new_chat_id, _new_chat_title,
                )
    except Exception as e:
        logger.warning("stealth_catchall_group: ensure_chat_settings failed: %s", e)
    return


# ── v4.4.7: my_chat_member — обработка добавления/удаления бота ──────────
@router.my_chat_member()
async def on_my_chat_member(event: types.ChatMemberUpdated) -> None:
    """Срабатывает, когда бота добавляют/удаляют из чата, повышают/понижают права.

    Используется для авто-обнаружения чатов: при добавлении бота в чат
    создаём chat_settings и уведомляем SU. my_chat_member более надёжен,
    чем stealth_catchall_group, т.к. срабатывает сразу при добавлении,
    а не при первом сообщении.
    """
    new_status = event.new_chat_member.status if event.new_chat_member else None
    old_status = event.old_chat_member.status if event.old_chat_member else None

    # Был добавлен в чат (или повышен с left/member до administrator)
    if new_status in ("member", "administrator") and new_status != old_status:
        try:
            async with async_session() as session:
                settings, created = await _ensure_chat_settings(
                    session,
                    chat_id=event.chat.id,
                    title=event.chat.title,
                )
                if created:
                    await session.commit()
                    _spawn_background_task(
                        _notify_su_about_chat(event.bot, event.chat.id, event.chat.title),
                        label="notify_su_my_chat_member",
                    )
                    logger.info(
                        "my_chat_member: bot added to chat id=%s title='%s'",
                        event.chat.id, event.chat.title,
                    )
        except Exception as e:
            logger.warning("on_my_chat_member: failed: %s", e)


# ── v4.7.27: chat_member — обработка ручных банов админами ─────────────────
@router.chat_member()
async def on_chat_member_updated(event: types.ChatMemberUpdated) -> None:
    """v4.7.27: Срабатывает на изменения статуса участников чата (не самого бота).

    Главный use-case — детектирование ручных банов, выдаваемых админами через
    Telegram-клиент (правой кнопкой на сообщении → «Заблокировать» или через
    профиль юзера). Telegram присылает ``ChatMemberUpdated`` с
    ``new_chat_member.status == "kicked"``.

    Без дедупликации бот отправил бы второй отчёт в reporting chat для каждого
    бана, выполненного самим ботом через ``!ban`` / autoban / CAS / sticker-pack /
    content-filter — т.к. Telegram присылает ``ChatMemberUpdated`` и для ботов,
    и для ручных банов одинаково.

    Решение состоит из ДВУХ уровней защиты (v4.7.28 — добавлен 2-й уровень):

    1. **PERSISTENT-проверка ``event.from_user.is_bot``** (основная, v4.7.28):
       когда бот сам вызывает ``bot.ban_chat_member()``, Telegram присылает
       ``ChatMemberUpdated`` с ``from_user`` == сам бот (``is_bot=True``).
       Эта проверка НЕ зависит от in-memory состояния и переживает рестарт
       бота — когда ``_recent_bot_bans`` dict теряется. Также отсекает баны
       от ДРУГИХ ботов (если в чате есть второй модератор-бот, который банит
       независимо) — это тоже не «ручной бан админом через клиент».

    2. **TTL-дедупликация через ``_consume_bot_ban()``** (backup, v4.7.27):
       каждый ``bot.ban_chat_member()`` в кодовой базе помечается через
       ``_mark_bot_ban(chat_id, user_id)`` — timestamp в in-memory dict
       ``_recent_bot_bans``. Здесь вызываем ``_consume_bot_ban()`` — если
       запись есть (т.е. бот сам банил в последние 10 сек), она удаляется
       и handler молча выходит. Осталась как backup на случай, если Telegram
       когда-то решит присылать ``from_user`` без ``is_bot=True``.

    Если ни одна из проверок не сработала — это честный ручной бан админом
    через клиент, отправляем компактный отчёт через ``_send_manual_ban_report``.

    Прочие изменения статуса (member→administrator, kicked→left через unban,
    restrict_chat_member и т.д.) пока НЕ обрабатываются — только баны.
    """
    new_status = event.new_chat_member.status if event.new_chat_member else None
    old_status = event.old_chat_member.status if event.old_chat_member else None

    # ── Ручной бан: new_status == "kicked" ─────────────────────
    if new_status != "kicked":
        return  # не бан — игнорируем (member/admin/left/restricted)

    # На всякий случай: если old_status уже был "kicked" — это не новый бан
    # (возможно, Telegram присылает дубль апдейта при каких-то операциях).
    if old_status == "kicked":
        return

    target_user = event.new_chat_member.user if event.new_chat_member else None
    if target_user is None:
        # Невозможно, но safety net — выходим
        return

    chat_id = event.chat.id
    user_id = target_user.id

    # ── v4.7.28: PERSISTENT-дедупликация бот-собственных банов ──
    # Когда бот сам вызывает `bot.ban_chat_member()` (через !ban / autoban / CAS /
    # sticker-pack / content-filter), Telegram присылает ChatMemberUpdated с
    # `from_user` == сам бот. Это НАДЁЖНЫЙ способ узнать «свой» бан — он не
    # зависит от TTL и переживает рестарт бота (когда `_recent_bot_bans`
    # in-memory dict теряется). Без этой проверки, если бот перезапустится в
    # момент между вызовом `ban_chat_member` и приходом ChatMemberUpdated —
    # бот отправил бы ложный отчёт «ручной бан» для своего же бана.
    #
    # Также отсекаем баны от ДРУГИХ ботов (если в чате есть ещё один
    # модератор-бот, который банит независимо) — это тоже не «ручной бан
    # админом через клиент», и репортить его как ручной было бы некорректно.
    actor = event.from_user
    if actor is not None and actor.is_bot:
        logger.debug(
            "on_chat_member_updated: bot-issued ban ignored (actor is bot) "
            "chat=%s user=%s actor_bot_id=%s",
            chat_id, user_id, actor.id,
        )
        return

    # ── TTL-дедупликация (backup) ─────────────────────────────
    # Осталась как backup на случай, если Telegram когда-то решит присылать
    # `from_user` без `is_bot=True` (или вообще без from_user) для ботовских
    # банов. В нормальном flow основная проверка выше уже отфильтровала баны
    # ботов — здесь мы ловим только edge-case'ы.
    if _consume_bot_ban(chat_id, user_id):
        # Бот сам забанил — отчёт уже отправлен в обычном flow (!ban / autoban /
        # CAS / sticker / content-filter). Молча выходим, не дублируем.
        logger.debug(
            "on_chat_member_updated: bot-own ban deduplicated (TTL backup) for chat=%s user=%s",
            chat_id, user_id,
        )
        return

    # ── Это ручной бан от админа через Telegram-клиент ─────────
    # Проверяем, что для этого чата задан reporting chat — иначе молча выходим
    # (как и ``_send_report`` делает).
    try:
        async with async_session() as session:
            report_dest = await _get_report_chat_id(session, chat_id)
            settings = await _get_chat_settings(session, chat_id)
            hashtag = settings.hashtag if settings else ""
    except Exception as e:
        logger.warning(
            "on_chat_member_updated: DB error for chat=%s user=%s: %s (skipping manual-ban report)",
            chat_id, user_id, e,
        )
        return

    if not report_dest:
        # Репорт-чат не задан — молча пропускаем (как _send_report).
        return

    # event.from_user — админ, который выполнил действие (если это действие
    # через клиент). Может быть None в редких случаях (например, если бан
    # выполнил сам владелец чата через какие-то legacy-механизмы Telegram).
    admin = event.from_user

    logger.info(
        "on_chat_member_updated: manual ban detected — chat=%s user=%s admin=%s",
        chat_id, user_id,
        (admin.id if admin and not admin.is_bot else "unknown/bot"),
    )

    # upsert'им юзера в БД (для веб-панели — чтобы профиль был доступен)
    try:
        async with async_session() as session:
            await _upsert_user(
                session, target_user.id, target_user.username,
                target_user.first_name, target_user.last_name,
            )
    except Exception as e:
        logger.warning(
            "on_chat_member_updated: failed to upsert user %s: %s",
            user_id, e,
        )

    # Отправляем компактный отчёт в reporting chat
    await _send_manual_ban_report(
        bot=event.bot,
        chat_id=chat_id,
        target=target_user,
        admin=admin,
        report_dest=report_dest,
        hashtag=hashtag or "",
    )
