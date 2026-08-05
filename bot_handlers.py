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
  !mute <1d/2h/30m> <причина>  — замьютить (полный мьют — все виды отправки)
  !warn <причина>               — выдать варн (1 поинт) + удалить сообщение нарушителя
  !ban <причина>                — забанить (v4.5.2: если reply на стикер — пак автодобавляется в BannedStickerPack)
  !unmute                       — размьютить (выдаёт текущие права чата)
  !unban                        — разбанить (only_if_banned — безопасный)
  !unwarn [N]                   — снять N последних варнов (по умолчанию 1; cap = текущее кол-во)
  !warns                        — показать текущее кол-во варнов юзера (в личку админу)
  !resetwarns                   — обнулить варны юзера

Команды в личке (только для ADMIN_IDS):
  /addadmin chat_id user_id      — добавить админа в чат
  /deladmin chat_id user_id      — убрать админа
  /sethashtag chat_id #хэштег   — установить хэштег чата
  /setreport chat_id report_chat_id — задать чат для отчётов (0 = сбросить, использовать default)
  /warns_mute chat_id число      — варнов до авто-мьюта (0 = выкл)
  /warns_ban chat_id число       — варнов до авто-бана (0 = выкл)
  /mute_duration chat_id 1d/2h/30m — длительность мьюта
  /settings chat_id              — показать текущие настройки

v4.5.2 — новые команды в личке (только для ADMIN_IDS):
  /bansticker <pack_name_or_link> [punishment] [duration]
      — добавить стикерпак в бан-лист. punishment: delete|warn|mute|ban (default: delete).
        Для mute — длительность в формате 1d/2h/30m.
        pack_name_or_link может быть как именем пака, так и ссылкой https://t.me/addstickers/<name>.
        Если punishment не указан — берётся из глобального default (delete).
  /liststickers [chat_id]        — показать забаненные стикерпаки (все или для чата)
  /delsticker <pack_name> [chat_id] — убрать стикерпак из бан-листа (без chat_id — из global)
  /addword <chat_id> <pattern> [action] [is_regex]
      — добавить слово/паттерн в word filter. action: delete|warn|mute|ban (default: delete).
        is_regex: 0 или 1 (default: 0 — простая подстрока, case-insensitive).
  /delword <chat_id> <pattern>   — убрать слово из word filter
  /listwords [chat_id]           — показать список забаненных слов
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
import os
import re
import logging
import secrets
from datetime import datetime, timezone, timedelta, date
from urllib.parse import urlparse

import aiohttp
from aiogram import Router, types, F, BaseMiddleware
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import (
    InputRichMessage,
    InputRichBlockSectionHeading,
    InputRichBlockParagraph,
    InputRichBlockBlockQuotation,
    InputRichBlockDetails,
    InputRichBlockFooter,
    InputRichBlockPhoto,
    InputRichBlockVideo,
    InputRichBlockAnimation,
    InputRichBlockAudio,
    InputRichBlockVoiceNote,
    InputRichBlockList,
    InputRichBlockListItem,
    InputRichBlockDivider,
    InputMediaPhoto,
    InputMediaVideo,
    InputMediaAnimation,
    InputMediaAudio,
    InputMediaVoiceNote,
    RichTextUrl,
    RichTextBold,
    RichTextCode,
    RichTextSpoiler,
)
from sqlalchemy import select, desc, func

from db import (
    async_session, User, Moderator, Punishment, ChatAdmin, ChatSettings, WebUser,
    WordFilter, LinkAllowlist, BannedStickerPack,
    _hash_password,
)

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
_CMD_MUTE = re.compile(r"^!mute\s+(\S+)(?:\s+(.+))?$", re.IGNORECASE)
_CMD_WARN = re.compile(r"^!warn\s+(.+)$", re.IGNORECASE)
_CMD_BAN = re.compile(r"^!ban\s+(.+)$", re.IGNORECASE)
_CMD_UNMUTE = re.compile(r"^!unmute\s*$", re.IGNORECASE)
_CMD_UNBAN = re.compile(r"^!unban\s*$", re.IGNORECASE)
_CMD_UNWARN = re.compile(r"^!unwarn(?:\s+(\d+))?\s*$", re.IGNORECASE)
_CMD_WARNS = re.compile(r"^!warns\s*$", re.IGNORECASE)
_CMD_RESETWARNS = re.compile(r"^!resetwarns\s*$", re.IGNORECASE)
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
_ALL_MOD_COMMANDS: tuple[re.Pattern, ...] = (
    _CMD_MUTE, _CMD_WARN, _CMD_BAN,
    _CMD_UNMUTE, _CMD_UNBAN, _CMD_UNWARN,
    _CMD_WARNS, _CMD_RESETWARNS,
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


async def _deactivate_alarm(
    session,
    cs: ChatSettings,
    bot: types.Bot,
    chat_id: int,
    *,
    reason: str = "manual",
) -> bool:
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

    Возвращает True если успешно, False если что-то упало.
    Логирует все шаги.
    """
    if not cs.alarm_currently_active:
        logger.info("Alarm deactivate: chat %s not active (reason=%s)", chat_id, reason)
        return False

    # Шаг 1: определяем права для восстановления
    perms_to_restore: types.ChatPermissions | None = None
    perms_source = ""

    if cs.day_permissions:
        try:
            perms_to_restore = _restore_permissions(cs.day_permissions)
            perms_source = "day_permissions preset"
        except (ValueError, TypeError) as e:
            logger.warning("Alarm deactivate: bad day_permissions JSON for chat %s: %s", chat_id, e)

    if perms_to_restore is None and cs.alarm_saved_permissions:
        try:
            perms_to_restore = _restore_permissions(cs.alarm_saved_permissions)
            perms_source = "alarm_saved_permissions snapshot"
        except (ValueError, TypeError) as e:
            logger.warning("Alarm deactivate: bad alarm_saved_permissions JSON for chat %s: %s", chat_id, e)

    if perms_to_restore is None:
        # Fallback: всё разрешено (текст + медиа), без админских полей.
        # Не используем _resolve_day_perms из bot.py (там зависимость от сессии)
        # — здесь проще явно задать дефолт.
        perms_to_restore = types.ChatPermissions(
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
        )
        perms_source = "hardcoded default (all allowed)"

    # Шаг 2: определяем slow_mode для восстановления
    slow_to_restore: int = 0
    slow_source = "default 0"
    if cs.day_slow_mode_delay and cs.day_slow_mode_delay > 0:
        slow_to_restore = cs.day_slow_mode_delay
        slow_source = f"day_slow_mode_delay={slow_to_restore}"
    elif cs.alarm_saved_slow_mode_delay is not None:
        slow_to_restore = cs.alarm_saved_slow_mode_delay
        slow_source = f"alarm_saved_slow_mode_delay={slow_to_restore}"

    # Шаг 3: применяем права
    try:
        # v4.7.25: use_independent_chat_permissions=True — иначе в legacy-режиме
        # can_send_messages=True в _alarm_permissions() может неявно подтянуть
        # другие права через правило импликации. См. подробности в bot.py
        # (_restore_day_state).
        await bot.set_chat_permissions(
            chat_id=chat_id,
            permissions=perms_to_restore,
            use_independent_chat_permissions=True,
        )
        logger.info(
            "Alarm deactivate: chat %s perms restored from %s",
            chat_id, perms_source,
        )
    except TelegramAPIError as e:
        logger.error(
            "Alarm deactivate: set_chat_permissions failed for chat %s: %s",
            chat_id, e,
        )
        return False

    # Шаг 4: применяем slow_mode
    # v4.7.22: SetChatSlowModeDelay определён в bot_handlers.py (top-level),
    # импорт не нужен. Раньше был late import `from bot import SetChatSlowModeDelay`,
    # но это вызывало повторный import bot.py как отдельный модуль (bot.py запускается
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
    cs.alarm_currently_active = False
    cs.alarm_saved_permissions = None
    cs.alarm_saved_slow_mode_delay = None
    cs.alarm_active_until = None
    cs.alarm_started_by = None
    await session.commit()

    logger.info(
        "Alarm deactivated: chat %s reason=%s started_by=%s",
        chat_id, reason, cs.alarm_started_by,
    )
    return True


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
async def _word_filter_match(
    session, chat_id: int, text: str,
) -> tuple[WordFilter | None, str | None]:
    """Проверяет текст сообщения по word_filter для чата.

    Возвращает (matching_filter, matched_word) или (None, None).
    Проверяются паттерны конкретного чата + глобальные (chat_id=0).
    is_regex=True — re.search; иначе — case-insensitive substring.
    Первый совпавший паттерн выигрывает (порядок: per-chat, потом global).
    """
    if not text:
        return (None, None)
    text_lower = text.lower()
    # per-chat (chat_id != 0) имеет приоритет над global (chat_id=0).
    from sqlalchemy import case
    stmt = (
        select(WordFilter)
        .where(
            WordFilter.chat_id.in_([0, chat_id]),
            WordFilter.is_active.is_(True),
        )
        .order_by(
            case((WordFilter.chat_id == 0, 1), else_=0),  # per-chat first
            WordFilter.created_at.asc(),
        )
    )
    rows = (await session.execute(stmt)).scalars().all()
    for wf in rows:
        if wf.is_regex:
            try:
                m = re.search(wf.pattern, text, re.IGNORECASE)
                if m:
                    return (wf, m.group(0))
            except re.error:
                # битый regex — логируем и пропускаем
                logger.warning("WordFilter id=%s has invalid regex: %s", wf.id, wf.pattern)
                continue
        else:
            pattern_lower = wf.pattern.lower()
            if pattern_lower in text_lower:
                return (wf, wf.pattern)
    return (None, None)


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
                          и фото/видео/гиф из него. По умолчанию скрыт (защита от
                          шок-контента), разворачивается по тапу.
      6. Divider        — разделитель
      7. Details        — «Доп. инфо» (чат/длительность/варнов всего) — сворачиваемо
      8. Divider        — разделитель
      9. Footer         — время МСК + хэштег чата + кликабельное имя модератора
                          (без приписки «Модератор:», просто имя).

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
    sticker_file_id: str | None = None
    if reply_to_message is not None:
        text_content = reply_to_message.text or reply_to_message.caption
        media_block = _build_media_block(reply_to_message)
        if reply_to_message.sticker is not None:
            sticker_file_id = reply_to_message.sticker.file_id
        if media_block is None and text_content is None:
            desc = _get_message_content_desc(reply_to_message)
            if desc:
                text_content = desc

    # ── Список блоков (v4.4.10 редизайн) ───────────────────────
    blocks: list = []
    blocks.append(InputRichBlockSectionHeading(text=action_label, size=2))
    blocks.append(InputRichBlockDivider())

    # ── List: нарушитель / причина / веб-профиль ───────────────
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

    # Пункт 3: веб-профиль — короткий текст вместо длинного URL
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
    # гиф) обёрнуты в сворачиваемый Details «📎 Сообщение юзера».
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
        blocks.append(InputRichBlockDivider())
        blocks.append(
            InputRichBlockDetails(
                summary="📎 Сообщение юзера",
                is_open=False,
                blocks=media_details_blocks,
            )
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

    # ── Стикер: отправляем отдельным сообщением после rich-отчёта ──
    # Rich Messages не имеют inline-блока для стикеров, поэтому крепим его
    # отдельным send_sticker. Стикеры редко бывают шок-контентом, поэтому
    # без has_spoiler — но всё равно после основного отчёта, чтобы не
    # заслонять его превью в списке сообщений.
    if sticker_file_id:
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

    asyncio.create_task(_del_ephemeral())


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
                await bot.ban_chat_member(chat_id=chat_id, user_id=target.id)
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
            until_date = int(datetime.now(timezone.utc).timestamp()) + mute_dur
            try:
                await bot.restrict_chat_member(
                    chat_id=chat_id,
                    user_id=target.id,
                    permissions=_mute_permissions(),
                    until_date=until_date,
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
                logger.info(
                    "Auto-mute: marked %d warns as consumed_by_action=auto_mute "
                    "for user %s in chat %s",
                    consumed, target.id, chat_id,
                )
                await _send_report(bot, chat_id, target, "mute",
                                   f"Автомьют: {total_warns} варнов",
                                   mod=mod,
                                   duration_seconds=mute_dur)
                logger.info("Auto-mute triggered for user %s in chat %s (%d warns, %s)",
                            target.id, chat_id, total_warns, _format_duration(mute_dur))
                # ── Ephemeral-уведомление модератору (видно только ему) ────
                # Нарушитель НЕ уведомляется — стелс-режим бота сохраняется.
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
# Обработчики команд в ГРУППАХ
# ═══════════════════════════════════════════════════════════════════════════

@router.message(F.chat.type.in_(["group", "supergroup"]), F.reply_to_message)
async def handle_group_command(message: types.Message) -> None:
    """Обрабатывает !mute, !warn, !ban, !unmute, !unban, !unwarn в группах."""
    text = message.text
    if not text:
        return

    # ── v4.4.8 FIX: не трогаем сообщения модератора, если это не команда ──
    # Раньше бот удалял ЛЮБОЙ ответ модератора в чате (т.к. удаление шло
    # ДО проверки на соответствие команде). Теперь сначала проверяем, что
    # текст реально является одной из модераторских команд — и только тогда
    # удаляем. Обычные ответы модератора больше не исчезают.
    if not _is_moderation_command(text):
        return

    # Проверяем, что отправитель — админ
    chat_id = message.chat.id
    async with async_session() as session:
        is_adm = await _is_admin(session, chat_id, message.from_user.id)
    if not is_adm:
        return

    target: types.User = message.reply_to_message.from_user
    mod = message.from_user
    target_content = _get_message_content_desc(message.reply_to_message)

    # ── v4.5.1: защита от самонаказания и friendly-fire ────────────────
    # Запрещаем применять !warn / !mute / !ban к:
    #   1. Самому себе (mod == target) — модератор не должен наказывать себя.
    #   2. Другому модератору/админу в этом же чате — чтобы не было конфликта
    #      интересов и случайных autoban-ов на коллег.
    # Для снятия (!unmute / !unban / !unwarn / !resetwarns) и просмотра (!warns)
    # эти ограничения НЕ действуют — там нет вреда, только восстановление.
    #
    # Проверяем только если команда — одна из наказательных.
    is_punitive_cmd = bool(
        _CMD_MUTE.match(text) or _CMD_WARN.match(text) or _CMD_BAN.match(text)
    )
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

    # ── !mute ───────────────────────────────────────────────────────────
    m = _CMD_MUTE.match(text)
    if m:
        dur_str = m.group(1)
        reason = m.group(2) or "(без причины)"
        duration_seconds = _parse_duration(dur_str)
        if duration_seconds is None:
            try:
                await message.bot.send_message(
                    chat_id=mod.id,
                    text=f"❌ Не удалось распознать длительность: {dur_str}\n"
                         f"💡 Формат: 1m, 30м, 1d, 2ч, 1d12h30m (рус/англ)",
                )
            except TelegramAPIError:
                pass
            return

        perm_snapshot = None
        try:
            member = await message.bot.get_chat_member(chat_id=chat_id, user_id=target.id)
            perm_snapshot = _snapshot_permissions(member)
        except TelegramAPIError as e:
            logger.warning("get_chat_member before mute failed: %s", e)

        until_date = int(datetime.now(timezone.utc).timestamp()) + duration_seconds
        try:
            await message.bot.restrict_chat_member(
                chat_id=chat_id, user_id=target.id,
                permissions=_mute_permissions(),
                until_date=until_date,
            )
        except TelegramAPIError as e:
            logger.error("restrict_chat_member failed: %s", e)
            try:
                await message.bot.send_message(
                    chat_id=mod.id,
                    text=f"❌ Мут не удался: {e}",
                )
            except TelegramAPIError:
                pass
            return

        await _send_report(
            bot=message.bot, chat_id=chat_id, target=target,
            action_type="mute", reason=reason, mod=mod,
            duration_seconds=duration_seconds,
            reply_to_message=message.reply_to_message,
        )

        async with async_session() as session:
            await _upsert_user(session, target.id, target.username,
                               target.first_name, target.last_name)
            await _upsert_moderator(session, mod.id, mod.username, mod.first_name)
            await _save_punishment(
                session, target.id, mod.id, chat_id,
                "mute", duration_seconds, reason, target_content,
                permissions_snapshot=perm_snapshot,
            )

        # ── Ephemeral-подтверждение модератору (видно только ему) ────
        # Нарушитель НЕ уведомляется — стелс-режим бота сохраняется.
        reason_safe = html.escape(reason, quote=False) if reason and reason != "(без причины)" else ""
        await _send_ephemeral(
            bot=message.bot, chat_id=chat_id, recipient=mod,
            text=(
                f"✅ Замьютил {_user_mention_html(target)} на "
                f"{_format_duration(duration_seconds)}"
                + (f" за: {reason_safe}" if reason_safe else "")
                + "."
            ),
        )

        # v4.7.15: удаляем сообщение нарушителя ПОСЛЕ отчёта. _send_report
        # читает атрибуты из Python-объекта reply_to_message (text, photo,
        # file_id, sticker) — они остаются в памяти после удаления. Но мы
        # перестраховываемся: сначала отчёт уходит в репорт-чат (с текстом
        # и медиа), и только потом оригинал удаляется из модерируемого чата.
        try:
            await message.reply_to_message.delete()
        except TelegramAPIError as e:
            logger.warning("Не удалось удалить сообщение нарушителя %s в чате %s: %s",
                           target.id, chat_id, e)

        return

    # ── !warn ───────────────────────────────────────────────────────────
    m = _CMD_WARN.match(text)
    if m:
        reason = m.group(1)

        # Сначала сохраняем наказание — тогда _count_warns внутри _send_report
        # и здесь будет учитывать только что выданный варн.
        async with async_session() as session:
            await _upsert_user(session, target.id, target.username,
                               target.first_name, target.last_name)
            await _upsert_moderator(session, mod.id, mod.username, mod.first_name)
            await _save_punishment(
                session, target.id, mod.id, chat_id,
                "warn", 1, reason, target_content,
            )
            total_warns_now = await _count_warns(session, target.id, chat_id)
            # Подтягиваем настройки чата — нужны пороги для уведомления нарушителю
            chat_settings = await _get_chat_settings(session, chat_id)

        # Теперь отчёт — в нём будет правильный счётчик варнов
        await _send_report(
            bot=message.bot, chat_id=chat_id, target=target,
            action_type="warn", reason=reason, warn_points=1, mod=mod,
            reply_to_message=message.reply_to_message,
        )

        # ── v4.4.9: Уведомление НАРУШИТЕЛЮ (видно только ему) ────────
        # Раньше варн был невидим для нарушителя — бесполезная санкция.
        # Теперь через receiver_user_id=target.id отправляем ему ephemeral
        # с причиной + текущим кол-вом варнов + порогами мьюта/бана.
        # Остальные участники чата этого сообщения НЕ видят.
        await _send_user_warn_notification(
            bot=message.bot, chat_id=chat_id, target=target,
            reason=reason, total_warns=total_warns_now,
            settings=chat_settings,
        )

        # ── Ephemeral-подтверждение модератору (видно только ему) ────
        reason_safe = html.escape(reason, quote=False) if reason else ""
        await _send_ephemeral(
            bot=message.bot, chat_id=chat_id, recipient=mod,
            text=(
                f"✅ Варн выдан {_user_mention_html(target)}"
                + (f" за: {reason_safe}" if reason_safe else "")
                + f". Варнов всего: {total_warns_now}"
            ),
        )

        # Проверяем порог варнов (тоже использует обновлённый счётчик)
        await _check_warn_threshold(
            bot=message.bot, chat_id=chat_id,
            target=target, mod=mod,
        )

        # v4.7.15: удаляем сообщение нарушителя ПОСЛЕ всех операций с ним.
        # Раньше удалялось ДО _send_report — это работало (потому что
        # _send_report читает атрибуты из Python-объекта, который остаётся
        # в памяти), но логически было хрупким. Теперь удаление — последняя
        # операция, после того как отчёт ушел и пороги проверены.
        try:
            await message.reply_to_message.delete()
        except TelegramAPIError as e:
            logger.warning("Не удалось удалить сообщение нарушителя %s в чате %s: %s",
                           target.id, chat_id, e)

        return

    # ── !ban ────────────────────────────────────────────────────────────
    m = _CMD_BAN.match(text)
    if m:
        reason = m.group(1)

        perm_snapshot = None
        try:
            member = await message.bot.get_chat_member(chat_id=chat_id, user_id=target.id)
            perm_snapshot = _snapshot_permissions(member)
        except TelegramAPIError as e:
            logger.warning("get_chat_member before ban failed: %s", e)

        try:
            await message.bot.ban_chat_member(chat_id=chat_id, user_id=target.id)
        except TelegramAPIError as e:
            logger.error("ban_chat_member failed: %s", e)
            try:
                await message.bot.send_message(
                    chat_id=mod.id,
                    text=f"❌ Бан не удался: {e}",
                )
            except TelegramAPIError:
                pass
            return

        await _send_report(
            bot=message.bot, chat_id=chat_id, target=target,
            action_type="ban", reason=reason, mod=mod,
            reply_to_message=message.reply_to_message,
        )

        async with async_session() as session:
            await _upsert_user(session, target.id, target.username,
                               target.first_name, target.last_name)
            await _upsert_moderator(session, mod.id, mod.username, mod.first_name)
            await _save_punishment(
                session, target.id, mod.id, chat_id,
                "ban", None, reason, target_content,
                permissions_snapshot=perm_snapshot,
            )

        # ── v4.5.2: если забанили за стикер — автоматически добавляем пак ──
        # в BannedStickerPack (per-chat, punishment=ban — чтобы следующий
        # юзер с этим же паком тоже был забанен автоматически). Это избавляет
        # модератора от необходимости отдельно выполнять !bansticker.
        # v4.5.3: используем getattr для безопасности (mock objects в тестах
        # могут не иметь атрибута 'sticker' — это нормально, просто пропустим).
        # v4.7.15: читаем sticker ДО удаления сообщения — для надёжности
        # (хотя Python-объект остаётся в памяти после delete, мы явно
        # сохраняем ссылку заранее).
        sticker = getattr(message.reply_to_message, "sticker", None)
        if sticker and sticker.set_name:
            try:
                async with async_session() as session:
                    await _add_banned_sticker_pack(
                        session,
                        chat_id=chat_id,
                        pack_name=sticker.set_name,
                        punishment="ban",
                        reason=f"Auto-added via !ban by mod {mod.id}: {reason}",
                        added_by_mod_id=mod.id,
                        added_via="auto_ban",
                    )
                logger.info(
                    "v4.5.2 auto-banned sticker pack '%s' in chat %s (via !ban by mod %s)",
                    sticker.set_name, chat_id, mod.id,
                )
                await _send_ephemeral(
                    bot=message.bot, chat_id=chat_id, recipient=mod,
                    text=(
                        f"🎭 Стикерпак <code>{sticker.set_name}</code> "
                        f"автодобавлен в бан-лист (punishment=ban)."
                    ),
                )
            except Exception as e:
                logger.warning(
                    "auto-add sticker pack '%s' failed: %s", sticker.set_name, e
                )

        # ── Ephemeral-подтверждение модератору (видно только ему) ────
        # Нарушитель НЕ уведомляется — стелс-режим бота сохраняется.
        reason_safe = html.escape(reason, quote=False) if reason else ""
        await _send_ephemeral(
            bot=message.bot, chat_id=chat_id, recipient=mod,
            text=(
                f"✅ Забанен {_user_mention_html(target)}"
                + (f" за: {reason_safe}" if reason_safe else "")
                + "."
            ),
        )

        # v4.7.15: удаляем сообщение нарушителя ПОСЛЕ всех операций.
        # Сначала — отчёт в репорт-чат (с текстом и медиа), потом
        # auto-add стикерпака (если бан за стикер), потом ephemeral
        # модератору, и только в самом конце — удаление оригинала.
        try:
            await message.reply_to_message.delete()
        except TelegramAPIError as e:
            logger.warning("Не удалось удалить сообщение нарушителя %s в чате %s: %s",
                           target.id, chat_id, e)

        return

    # ── !unmute ─────────────────────────────────────────────────────────
    # Размут выдаёт ТЕКУЩИЕ дефолтные права чата (Chat.permissions),
    # а не индивидуальный снапшот — так ночной режим и прочие
    # ограничения чата не ломаются при размуте посреди ночи.
    if _CMD_UNMUTE.match(text):
        try:
            chat_info = await message.bot.get_chat(chat_id=chat_id)
            chat_perms = chat_info.permissions
            if chat_perms is None:
                # Группа с супер-админом без дефолтных прав — даём всё
                chat_perms = types.ChatPermissions(can_send_messages=True)
            # v4.7.14: зануляем админские права — они НЕ должны
            # восстанавливаться через restrict_chat_member. Если у чата
            # в дефолтных правах вдруг есть can_change_info/can_invite_users/
            # can_pin_messages (что бывает, если владелец открыл их для всех),
            # то без этой очистки unmute мог бы "случайно" раздать админ-права
            # размьюченному. Админ-права выдаются только через promote.
            chat_perms = types.ChatPermissions(
                can_send_messages=getattr(chat_perms, "can_send_messages", False),
                can_send_audios=getattr(chat_perms, "can_send_audios", False),
                can_send_documents=getattr(chat_perms, "can_send_documents", False),
                can_send_photos=getattr(chat_perms, "can_send_photos", False),
                can_send_videos=getattr(chat_perms, "can_send_videos", False),
                can_send_video_notes=getattr(chat_perms, "can_send_video_notes", False),
                can_send_voice_notes=getattr(chat_perms, "can_send_voice_notes", False),
                can_send_polls=getattr(chat_perms, "can_send_polls", False),
                can_send_other_messages=getattr(chat_perms, "can_send_other_messages", False),
                can_add_web_page_previews=getattr(chat_perms, "can_add_web_page_previews", False),
                # Админские права — НЕ передаются (Telegram проигнорирует,
                # но мы явно не ставим их в True).
            )
        except TelegramAPIError as e:
            logger.error("get_chat for unmute failed: %s", e)
            try:
                await message.bot.send_message(chat_id=mod.id, text=f"❌ Размут не удался (get_chat): {e}")
            except TelegramAPIError:
                pass
            return

        try:
            await message.bot.restrict_chat_member(
                chat_id=chat_id, user_id=target.id,
                permissions=chat_perms,
            )
        except TelegramAPIError as e:
            logger.error("unmute restrict failed: %s", e)
            try:
                await message.bot.send_message(chat_id=mod.id, text=f"❌ Размут не удался: {e}")
            except TelegramAPIError:
                pass
            return

        await _send_report(
            bot=message.bot, chat_id=chat_id, target=target,
            action_type="unmute", mod=mod,
            reply_to_message=message.reply_to_message,
        )

        async with async_session() as session:
            await _upsert_user(session, target.id, target.username,
                               target.first_name, target.last_name)
            await _upsert_moderator(session, mod.id, mod.username, mod.first_name)
            # Пометим последний активный мьют как снятый (для истории/веб-панели)
            await _revoke_last_action(
                session, target.id, chat_id, "mute", revoked_by_mod_id=mod.id,
            )
            await _save_punishment(
                session, target.id, mod.id, chat_id,
                "unmute", None, None, target_content,
            )

        # ── Ephemeral-подтверждение модератору (видно только ему) ────
        # Нарушитель НЕ уведомляется — стелс-режим бота сохраняется.
        await _send_ephemeral(
            bot=message.bot, chat_id=chat_id, recipient=mod,
            text=f"✅ Размьючен {_user_mention_html(target)}.",
        )

        # v4.5.1: audit в репорт-чат (кратко: кто что снял вручную)
        await _send_audit_to_report(
            bot=message.bot, chat_id=chat_id, mod=mod, target=target,
            action_label="мьют", detail="команда !unmute",
        )

        return

    # ── !unban — разбанить юзера (reply) ──────────────────────────────
    # Telegram: unban_chat_member снимает бан. Если юзер не забанен —
    # команда безопасна (ничего не делает, но Bot API может вернуть ошибку,
    # которую мы просто логируем).
    if _CMD_UNBAN.match(text):
        try:
            # only_if_banned=True — безопасный разбан: не разбанит того,
            # кто не забанен (иначе можно было бы использовать для обхода кика)
            await message.bot.unban_chat_member(
                chat_id=chat_id, user_id=target.id, only_if_banned=True,
            )
        except TelegramAPIError as e:
            logger.error("unban_chat_member failed: %s", e)
            try:
                await message.bot.send_message(
                    chat_id=mod.id,
                    text=f"❌ Разбан не удался: {e}",
                )
            except TelegramAPIError:
                pass
            return

        await _send_report(
            bot=message.bot, chat_id=chat_id, target=target,
            action_type="unban", mod=mod,
            reply_to_message=message.reply_to_message,
        )

        async with async_session() as session:
            await _upsert_user(session, target.id, target.username,
                               target.first_name, target.last_name)
            await _upsert_moderator(session, mod.id, mod.username, mod.first_name)
            # Пометим последний активный бан как снятый (для истории/веб-панели)
            await _revoke_last_action(
                session, target.id, chat_id, "ban", revoked_by_mod_id=mod.id,
            )
            await _save_punishment(
                session, target.id, mod.id, chat_id,
                "unban", None, None, target_content,
            )

        # ── Ephemeral-подтверждение модератору ────
        await _send_ephemeral(
            bot=message.bot, chat_id=chat_id, recipient=mod,
            text=f"✅ Разбанен {_user_mention_html(target)}.",
        )

        # v4.5.1: audit в репорт-чат
        await _send_audit_to_report(
            bot=message.bot, chat_id=chat_id, mod=mod, target=target,
            action_label="бан", detail="команда !unban",
        )

        return

    # ── !unwarn [N] — снять N последних варнов (reply) ────────────────
    # По умолчанию N=1. Снятые варны помечаются is_revoked=True и больше
    # не учитываются в _count_warns / _check_warn_threshold.
    # v4.5.1: cap = текущее количество активных варнов на юзере (раньше было 100).
    # Если попросили снять больше, чем есть, — снимаем сколько есть, без ошибки.
    m = _CMD_UNWARN.match(text)
    if m:
        n_str = m.group(1)
        n = int(n_str) if n_str else 1
        if n < 1:
            n = 1

        async with async_session() as session:
            await _upsert_user(session, target.id, target.username,
                               target.first_name, target.last_name)
            await _upsert_moderator(session, mod.id, mod.username, mod.first_name)
            # v4.5.1: cap = текущее количество активных (не снятых, не погашенных) варнов.
            # Так !unwarn 999 у юзера с 3 варнами снимет 3, а не упадёт.
            current_warns = await _count_warns(session, target.id, chat_id)
            n_effective = min(n, max(current_warns, 1))
            revoked_count = await _revoke_last_warns(
                session, target.id, chat_id, n_effective, revoked_by_mod_id=mod.id,
            )
            # Сохраняем запись о снятии (как отдельную "санкцию" типа unwarn)
            await _save_punishment(
                session, target.id, mod.id, chat_id,
                "unwarn", revoked_count, f"Снято {revoked_count} варн(а/ов)", None,
            )
            total_warns_now = await _count_warns(session, target.id, chat_id)

        await _send_report(
            bot=message.bot, chat_id=chat_id, target=target,
            action_type="unwarn", reason=f"Снято {revoked_count} варн(а/ов)",
            warn_points=revoked_count, mod=mod,
            reply_to_message=message.reply_to_message,
        )

        # ── Ephemeral-подтверждение модератору ────
        await _send_ephemeral(
            bot=message.bot, chat_id=chat_id, recipient=mod,
            text=(
                f"✅ Снято {revoked_count} варн(а/ов) с {_user_mention_html(target)}."
                f" Варнов всего: {total_warns_now}"
            ),
        )

        # v4.5.1: audit в репорт-чат
        await _send_audit_to_report(
            bot=message.bot, chat_id=chat_id, mod=mod, target=target,
            action_label="варн(а/ов)", detail="команда !unwarn",
            count=revoked_count,
        )

        return

    # ── !warns — показать текущее количество варнов юзера ─────────────
    if _CMD_WARNS.match(text):
        async with async_session() as session:
            total_warns = await _count_warns(session, target.id, chat_id)
            settings = await _get_chat_settings(session, chat_id)

        warn_mute = settings.warns_to_mute if settings.warns_to_mute > 0 else None
        warn_ban = settings.warns_to_ban if settings.warns_to_ban > 0 else None

        info_parts = [f"⚠️ Варнов: {total_warns}"]
        if warn_mute:
            info_parts.append(f"Автомьют при {warn_mute}")
        if warn_ban:
            info_parts.append(f"Автобан при {warn_ban}")

        try:
            await message.bot.send_message(
                chat_id=message.from_user.id,
                text=f"👤 {target.first_name or ''}{' ' + target.last_name if target.last_name else ''}"
                     f"{' @' + target.username if target.username else ''}\n"
                     + "\n".join(info_parts),
            )
        except TelegramAPIError:
            # Если бот не может написать в личку — ответ в чат (будет удалён)
            sent = await message.bot.send_message(
                chat_id=chat_id,
                text=f"⚠️ {target.first_name or target.id}: {total_warns} варнов",
            )
            # Удаляем ответ через 30 секунд
            # v4.7.3: Semaphore(100) — тот же лимит что и для ephemeral,
            # т.к. это тоже auto-delete фоновой задачей.
            async def _del_msg():
                try:
                    async with _EPHEMERAL_DELETE_SEM:
                        await asyncio.sleep(30)
                        try:
                            await message.bot.delete_message(
                                chat_id=chat_id, message_id=sent.message_id,
                            )
                        except TelegramAPIError:
                            pass
                except asyncio.CancelledError:
                    raise
            asyncio.create_task(_del_msg())
        try:
            await message.delete()
        except TelegramAPIError:
            pass
        return

    # ── !resetwarns — обнулить варны юзера ────────────────────────────
    # v4.5.1: переписано. Раньше занулял duration_seconds (ломало фильтр
    # active/revoked в веб-панели, не писало audit, не закрывало варны
    # корректно для !unwarn). Теперь помечаем все активные варны юзера
    # is_revoked=True с revoked_by_mod_id и шлём audit в репорт-чат.
    # Доступ: только ADMIN_IDS env или WebUser с role su/admin. Рядовой
    # модератор не может обнулить чужие варны (включая свои собственные —
    # слишком легко заметать следы).
    if _CMD_RESETWARNS.match(text):
        # ── Role check ──
        async with async_session() as session:
            mod_role = await _get_web_user_role(session, mod.id)
        is_privileged = (mod.id in ADMIN_IDS) or (mod_role in ("su", "admin"))
        if not is_privileged:
            try:
                await _send_ephemeral(
                    bot=message.bot, chat_id=chat_id, recipient=mod,
                    text=(
                        "❌ !resetwarns доступен только SU/Admin. "
                        "Используйте !unwarn N для снятия отдельных варнов."
                    ),
                )
            except Exception:
                pass
            return

        async with async_session() as session:
            await _upsert_user(session, target.id, target.username,
                               target.first_name, target.last_name)
            await _upsert_moderator(session, mod.id, mod.username, mod.first_name)

            # Помечаем все активные warn-записи юзера в чате как снятые
            stmt = (
                select(Punishment)
                .where(
                    Punishment.user_id == target.id,
                    Punishment.chat_id == chat_id,
                    Punishment.action_type == "warn",
                    Punishment.is_revoked.is_(False),
                )
            )
            result = await session.execute(stmt)
            warns = result.scalars().all()
            now = datetime.now(timezone.utc)
            for w in warns:
                w.is_revoked = True
                w.revoked_at = now
                w.revoked_by_mod_id = mod.id
            if warns:
                await session.commit()
            total_reset = len(warns)

            # Сохраняем запись о сбросе (как отдельную "санкцию" типа unwarn)
            if total_reset > 0:
                await _save_punishment(
                    session, target.id, mod.id, chat_id,
                    "unwarn", total_reset,
                    f"Полный сброс варнов ({total_reset} шт.)", None,
                )

        try:
            await message.bot.send_message(
                chat_id=message.from_user.id,
                text=f"✅ Варны обнулены: {target.first_name or ''}"
                     f"{' ' + target.last_name if target.last_name else ''}"
                     f"{' @' + target.username if target.username else ''}"
                     f" (сброшено {total_reset} записей)",
            )
        except TelegramAPIError:
            pass

        # v4.5.1: audit в репорт-чат
        if total_reset > 0:
            await _send_audit_to_report(
                bot=message.bot, chat_id=chat_id, mod=mod, target=target,
                action_label="варн(а/ов) — полный сброс",
                detail="команда !resetwarns",
                count=total_reset,
            )

        try:
            await message.delete()
        except TelegramAPIError:
            pass
        return


# ═══════════════════════════════════════════════════════════════════════════
# v4.7.20: !alarm on/off — отдельный handler (не требует reply)
# ═══════════════════════════════════════════════════════════════════════════

@router.message(F.chat.type.in_(["group", "supergroup"]))
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
    """
    text = message.text
    if not text:
        return

    m = _CMD_ALARM.match(text)
    if not m:
        return  # Не команда !alarm — пусть другие handler'ы разбираются

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
            ok = await _deactivate_alarm(session, cs, message.bot, chat_id, reason="manual")
        if ok:
            await _send_alarm_dm(
                bot=message.bot, user_id=mod.id,
                text=(
                    f"✅ Alarm снят в чате.\n"
                    f"Права и slow_mode восстановлены из сохранённого snapshot."
                ),
            )
        else:
            await _send_alarm_dm(
                bot=message.bot, user_id=mod.id,
                text=(
                    f"⚠️ Alarm не удалось снять — ошибка при восстановлении прав. "
                    f"Проверьте логи и при необходимости восстановите права вручную."
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
                    f"🌙 Сейчас активен ночной режим — !alarm включить нельзя.\n"
                    f"Ночной режим уже ограничивает права чата (медиа отключены, "
                    f"slow_mode активен). Alarm будет избыточен.\n"
                    f"Дождитесь окончания ночного режима или отключите его."
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
                    f"🚫 Сейчас активен санитарный день — !alarm включить нельзя.\n"
                    f"Санитарный день уже ограничивает права чата (полный локдаун). "
                    f"Alarm будет избыточен."
                ),
            )
            return

        # ── Если alarm уже активен — продлеваем/обновляем duration ────────
        if cs.alarm_currently_active:
            old_started_by = cs.alarm_started_by
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
                        f"Alarm active until: {new_until.isoformat()}."
                    ),
                )
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
                        f"Теперь alarm будет активен до ручного !alarm off."
                    ),
                )
            return

        # ── Включаем alarm с нуля: snapshot → apply ───────────────────────
        # Шаг 1: snapshot текущих прав чата
        snapshot_perms: str | None = None
        snapshot_slow: int = 0
        try:
            chat_info = await message.bot.get_chat(chat_id)
            # chat_info.permissions — это ChatPermissions объекта чата.
            # Для групп это "default permissions" — то что нам нужно.
            if chat_info.permissions:
                perms = chat_info.permissions
                data = {field: bool(getattr(perms, field, False)) for field in _PERM_FIELDS}
                snapshot_perms = json.dumps(data, ensure_ascii=False)
            snapshot_slow = getattr(chat_info, "slow_mode_delay", 0) or 0
            # v4.7.20: валидация типа (см. BUG#5 — MagicMock в тестах / None в реальности)
            if not isinstance(snapshot_slow, int):
                snapshot_slow = 0
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

        # Шаг 2: применяем alarm_permissions
        try:
            # v4.7.25: use_independent_chat_permissions=True — иначе в legacy-режиме
            # can_send_messages=True в _alarm_permissions() может неявно подтянуть
            # другие права через правило импликации. См. bot.py (_restore_day_state).
            await message.bot.set_chat_permissions(
                chat_id=chat_id,
                permissions=_alarm_permissions(),
                use_independent_chat_permissions=True,
            )
        except TelegramAPIError as e:
            logger.error(
                "Alarm on: set_chat_permissions failed for chat %s: %s",
                chat_id, e,
            )
            await _send_alarm_dm(
                bot=message.bot, user_id=mod.id,
                text=(
                    f"❌ Не удалось применить ограничения alarm: {e}\n"
                    f"У бота нет прав администратора в чате?"
                ),
            )
            return

        # Шаг 3: применяем slow_mode 30s
        # v4.7.22: SetChatSlowModeDelay определён в bot_handlers.py (top-level),
        # импорт не нужен. Раньше был late import `from bot import SetChatSlowModeDelay`,
        # но это вызывало повторный import bot.py как отдельный модуль (bot.py запускается
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


@router.message(F.chat.type == "private", Command("addword"))
async def cmd_addword(message: types.Message) -> None:
    """v4.5.2 (#7): /addword chat_id <pattern> [action] [is_regex]

    action: delete|warn|mute|ban (default: delete).
    is_regex: 0/1 (default: 0 — case-insensitive substring).
    """
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = message.text.split(maxsplit=4)
    if len(parts) < 3:
        await message.reply(
            "📋 Формат: /addword chat_id <pattern> [delete|warn|mute|ban] [is_regex 0/1]\n"
            "💡 chat_id=0 — глобальный паттерн (для всех чатов)\n"
            "💡 is_regex=1 — паттерн интерпретируется как regex",
            parse_mode=None,
        )
        return

    try:
        chat_id = int(parts[1])
    except ValueError:
        await message.reply("❌ chat_id должен быть числом (0 для global)", parse_mode=None)
        return

    pattern = parts[2]
    action = "delete"
    is_regex = False
    if len(parts) >= 4:
        action = parts[3].lower().strip()
        if action not in ("delete", "warn", "mute", "ban"):
            await message.reply("❌ action должен быть delete/warn/mute/ban", parse_mode=None)
            return
    if len(parts) >= 5:
        is_regex_val = parts[4].strip()
        if is_regex_val in ("1", "true", "yes", "regex"):
            is_regex = True
        elif is_regex_val in ("0", "false", "no", "plain"):
            is_regex = False
        else:
            await message.reply("❌ is_regex должен быть 0 или 1", parse_mode=None)
            return

    # Валидация regex если is_regex=True
    if is_regex:
        try:
            re.compile(pattern)
        except re.error as e:
            await message.reply(f"❌ Битый regex: {e}", parse_mode=None)
            return

    async with async_session() as session:
        # Проверяем дубликат
        existing = (await session.execute(
            select(WordFilter).where(
                WordFilter.chat_id == chat_id,
                WordFilter.pattern == pattern,
                WordFilter.is_active.is_(True),
            )
        )).scalar_one_or_none()
        if existing:
            existing.action = action
            existing.is_regex = is_regex
            await session.commit()
            await message.reply(
                f"✅ Обновлён существующий паттерн <code>{pattern}</code> "
                f"[chat {chat_id}] → action={action}, is_regex={is_regex}",
                parse_mode="HTML",
            )
            return
        wf = WordFilter(
            chat_id=chat_id,
            pattern=pattern,
            is_regex=is_regex,
            action=action,
            created_by=message.from_user.id,
        )
        session.add(wf)
        await session.commit()

    scope = "global" if chat_id == 0 else f"chat {chat_id}"
    await message.reply(
        f"✅ Паттерн <code>{pattern}</code> добавлен [{scope}]\n"
        f"action: <b>{action}</b>, is_regex: {is_regex}",
        parse_mode="HTML",
    )


@router.message(F.chat.type == "private", Command("delword"))
async def cmd_delword(message: types.Message) -> None:
    """v4.5.2 (#7): /delword chat_id <pattern> — убрать слово из фильтра."""
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        await message.reply("📋 Формат: /delword chat_id <pattern>", parse_mode=None)
        return

    try:
        chat_id = int(parts[1])
    except ValueError:
        await message.reply("❌ chat_id должен быть числом", parse_mode=None)
        return

    pattern = parts[2]
    async with async_session() as session:
        wf = (await session.execute(
            select(WordFilter).where(
                WordFilter.chat_id == chat_id,
                WordFilter.pattern == pattern,
                WordFilter.is_active.is_(True),
            )
        )).scalar_one_or_none()
        if wf is None:
            await message.reply(
                f"⚠️ Паттерн <code>{pattern}</code> не найден [chat {chat_id}]",
                parse_mode="HTML",
            )
            return
        wf.is_active = False
        await session.commit()

    await message.reply(
        f"✅ Паттерн <code>{pattern}</code> убран из фильтра [chat {chat_id}]",
        parse_mode="HTML",
    )


@router.message(F.chat.type == "private", Command("listwords"))
async def cmd_listwords(message: types.Message) -> None:
    """v4.5.2 (#7): /listwords [chat_id] — показать список забаненных слов."""
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
        stmt = select(WordFilter).where(WordFilter.is_active.is_(True))
        if chat_filter is not None:
            stmt = stmt.where(WordFilter.chat_id == chat_filter)
        stmt = stmt.order_by(WordFilter.chat_id.asc(), WordFilter.created_at.desc())
        wfs = (await session.execute(stmt)).scalars().all()

    if not wfs:
        await message.reply("📭 Нет забаненных слов.", parse_mode=None)
        return

    lines = ["📝 <b>Список забаненных слов</b>:\n"]
    for wf in wfs:
        scope = "global" if wf.chat_id == 0 else f"chat {wf.chat_id}"
        regex_tag = " (regex)" if wf.is_regex else ""
        lines.append(
            f"  • <code>{wf.pattern}</code> [{scope}] — {wf.action}{regex_tag}"
        )
    await message.reply("\n".join(lines), parse_mode="HTML")


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
                    from bot import _exit_night_mode
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
                    from bot import _exit_sanitary_day
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
                # Импортируем _enter_sanitary_day из bot.py (avoid circular).
                # Поскольку bot.py импортирует bot_handlers, делаем lazy import.
                from bot import _enter_sanitary_day, _exit_sanitary_day
                await _enter_sanitary_day(settings)
                await message.reply(
                    f"🔒 Чат {chat_id}: <b>санитарный день включён вручную</b>\n"
                    "Чат в lockdown. Модераторы могут писать.",
                    parse_mode="HTML",
                )
            else:
                from bot import _enter_sanitary_day, _exit_sanitary_day
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

_HELP_FULL_TEXT = (
    "📖 <b>Дедушка Вобжак — список команд</b>\n\n"
    "<b>В группах (reply на сообщение):</b>\n"
    "  !mute [длительность] [причина] — мьют (формат: 1d2h, 30м; без аргументов — перманент)\n"
    "  !warn [причина] — варн (сообщение нарушителя удаляется)\n"
    "  !ban [причина] — бан (если reply на стикер — пак автодобавляется в бан-лист)\n"
    "  !unmute / !unban — снять ограничения\n"
    "  !unwarn [N] — снять N последних варнов (по умолчанию 1)\n"
    "  !warns / !resetwarns — показать / обнулить варны\n\n"
    "<b>В личке (настройки чатов):</b>\n"
    "  💡 Большинство настроек доступно в веб-панели: /admin/chats\n"
    "  /settings chat_id — показать настройки\n"
    "  /sethashtag chat_id #tag — хэштег чата\n"
    "  /setreport chat_id [report_chat_id] — чат для отчётов (0 = сброс)\n"
    "  /warns_mute chat_id N / /warns_ban chat_id N — пороги\n"
    "  /mute_duration chat_id 1d2h — длительность мьюта\n"
    "  /addadmin chat_id user_id / /deladmin chat_id user_id\n\n"
    "<b>Фильтры (в личке):</b>\n"
    "  /bansticker &lt;pack|link&gt; [delete|warn|mute|ban] [dur] — забанить стикерпак\n"
    "  /liststickers [chat_id] / /delsticker &lt;pack&gt; [chat_id]\n"
    "  /addword chat_id &lt;слово&gt; [action] [is_regex 0/1]\n"
    "  /delword chat_id &lt;слово&gt; / /listwords [chat_id]\n"
    "  /linkfilter chat_id on|off — фильтр ссылок\n"
    "  /linkallow chat_id|global &lt;domain&gt; / /linkallowlist [chat_id]\n"
    "  /cas chat_id on|off — CAS-проверка новых юзеров\n\n"
    "<b>Ночной режим (в личке):</b>\n"
    "  /nightmode chat_id &lt;start&gt; &lt;end&gt; [strict|text_only|none|custom]\n"
    "  /nightmode chat_id off — выключить\n"
    "  /nightmode chat_id tz &lt;Europe/Moscow&gt; — часовой пояс\n"
    "  /nightmode chat_id weekend &lt;start&gt; &lt;end&gt; — расписание на сб/вс\n"
    "  /nightmode chat_id weekend off — сбросить (использовать будничное)\n"
    "  /nightmode chat_id notify on|off [custom_text] — уведомления входа/выхода\n"
    "  /nightmode chat_id custom &lt;perm&gt;=0|1 ... — точечные права\n"
    "    perms: msgs, audios, docs, photos, videos, vnotes, voices, polls, other, links\n"
    "  /nightmode chat_id slowmode &lt;day_sec&gt; &lt;night_sec&gt; — slow mode (0=выкл, 0..36400)\n"
    "  /nightmode chat_id slowmode off — выключить slow mode changes\n\n"
    "<b>Санитарные дни (в личке):</b>\n"
    "  /sanitary chat_id — показать список\n"
    "  /sanitary chat_id add &lt;YYYY-MM-DD&gt; — добавить день\n"
    "  /sanitary chat_id add &lt;start&gt;:&lt;end&gt; — добавить диапазон\n"
    "  /sanitary chat_id remove &lt;YYYY-MM-DD&gt; — удалить день/диапазон\n"
    "  /sanitary chat_id clear — очистить список\n"
    "  /sanitary chat_id toggle — вручную войти/выйти (для теста)\n"
    "  💡 Lockdown чата (модераторы не страдают); ночной режим пропускается\n\n"
    "<b>Прочее:</b>\n"
    "  /warndecay chat_id &lt;days&gt; — срок действия варна (0 = отключено)\n"
)

_HELP_MODERATOR_TEXT = (
    "📖 <b>Дедушка Вобжак — команды модератора</b>\n\n"
    "<b>В группах (reply на сообщение нарушителя):</b>\n\n"
    "  <code>!mute [длительность] [причина]</code>\n"
    "    Мьют нарушителя. Длительность в формате <code>1d2h</code> (1 день 2 часа),\n"
    "    <code>30м</code>, <code>2h</code>. Без аргументов — перманент.\n"
    "    Пример: <code>!mute 1d спам</code> → мьют на 1 день за спам.\n\n"
    "  <code>!warn [причина]</code>\n"
    "    Вынести варн. Сообщение нарушителя удаляется.\n"
    "    При достижении порога <code>warns_to_mute</code> — автомьют.\n"
    "    Пример: <code>!warn мат</code>.\n\n"
    "  <code>!ban [причина]</code>\n"
    "    Бан нарушителя. Если reply на стикер — пак автодобавляется в бан-лист.\n"
    "    Пример: <code>!ban реклама</code>.\n\n"
    "  <code>!unmute</code> / <code>!unban</code>\n"
    "    Снять мьют или бан с нарушителя (reply на его сообщение).\n\n"
    "  <code>!unwarn [N]</code>\n"
    "    Снять N последних варнов (по умолчанию 1).\n"
    "    Пример: <code>!unwarn 2</code> → снимает 2 варна.\n\n"
    "  <code>!warns</code>\n"
    "    Показать количество активных варнов у нарушителя.\n"
    "    Бот пришлёт статистику в личку, если сможет.\n\n"
    "<b>Веб-панель:</b>\n"
    "  🔗 <a href=\"https://degraban.bothost.tech\">degraban.bothost.tech</a>\n"
    "  Логин и пароль выдаёт SU при создании аккаунта.\n"
    "  Если забыли — напишите SU в Telegram.\n\n"
    "<b>Важно:</b>\n"
    "  • Наказания нельзя применять к себе и к другим модераторам этого чата.\n"
    "  • <code>!resetwarns</code> доступен только администраторам.\n"
    "  • Команды настройки чатов (<code>/sethashtag</code>, <code>/nightmode</code>,\n"
    "    <code>/sanitary</code>, фильтры и т.д.) — только для админов.\n"
    "    Запросите доступ у SU, если нужно менять настройки чата.\n"
)


@router.message(F.chat.type == "private", Command("help"))
async def cmd_help(message: types.Message) -> None:
    """Показывает список команд.

    v4.7.9: раньше /help видели только ADMIN_IDS (env). Теперь:
      • ADMIN_IDS env → полный help (как раньше)
      • WebUser role='su' или 'admin', is_active=True → полный help
      • WebUser role='moderator', is_active=True → сокращённый help
        (только групповые модераторские команды + ссылка на веб-панель)
      • Все остальные — молчим (стелс сохраняется).

    Это закрывает проблему: модераторы, привязанные к чату через chat_admins,
    могли использовать !mute/!warn в группах, но не видели /help — им было
    неоткуда узнать синтаксис команд.
    """
    user_id = message.from_user.id

    # 1. Глобальные супер-админы из env — всегда полный help
    if user_id in ADMIN_IDS:
        await message.reply(_HELP_FULL_TEXT, parse_mode="HTML")
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
        await message.reply(_HELP_FULL_TEXT, parse_mode="HTML")
        return

    # 4. Moderator → сокращённый help
    if wu.role == "moderator":
        await message.reply(_HELP_MODERATOR_TEXT, parse_mode="HTML")
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
            asyncio.create_task(_fetch_and_save_avatar(message.bot, tg_uid))
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
        is_banned, reason = await _cas_check_user(member.id)
        if not is_banned:
            continue
        # Юзер в CAS-базе — банним
        try:
            await message.bot.ban_chat_member(
                chat_id=message.chat.id, user_id=member.id,
            )
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
        until_date = int(datetime.now(timezone.utc).timestamp()) + mute_dur
        try:
            await message.bot.restrict_chat_member(
                chat_id=chat_id, user_id=target.id,
                permissions=_mute_permissions(),
                until_date=until_date,
            )
            async with async_session() as session:
                await _save_punishment(
                    session, target.id, 0, chat_id,
                    "mute", mute_dur,
                    f"Banned sticker pack: {sticker.set_name}",
                    target_content,
                )
            logger.info(
                "Sticker pack '%s' mute issued in chat %s (user %s, %s)",
                sticker.set_name, chat_id, target.id, _format_duration(mute_dur),
            )
        except TelegramAPIError as e:
            logger.error("Sticker mute failed: %s", e)
        return

    if punishment == "ban":
        try:
            await message.bot.ban_chat_member(chat_id=chat_id, user_id=target.id)
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
    until_date = int(now.timestamp()) + dur
    try:
        await message.bot.restrict_chat_member(
            chat_id=chat_id, user_id=fu.id,
            permissions=_mute_permissions(),
            until_date=until_date,
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
        logger.info(
            "Via-bot filter (mute %s) in chat %s (user %s, bot @%s): "
            "rate-limit exceeded (last=%ss ago, limit=%ss)",
            _format_duration(dur), chat_id, fu.id, bot_username,
            gap_sec, rate_limit,
        )
    except TelegramAPIError as e:
        logger.error("Via-bot filter mute failed: %s", e)

    return True


@router.message(F.chat.type.in_(["group", "supergroup"]))
async def handle_content_filters(message: types.Message) -> None:
    """v4.5.2 (#7, #8): Word filter + Link filter для текстовых сообщений.

    Срабатывает на текстовых сообщениях (и caption у медиа). Если сработал
    word filter — применяется его action. Если сработал link filter —
    применяется chat_settings.link_filter_action. Первым проверяется word
    filter (он более специфичный), потом link filter.

    v4.7.24: ПЕРВЫМ проверяется via-bot rate-limit filter (для сообщений
    с message.via_bot is not None). Если rate-limit превышен — delete + mute,
    word/link filter не запускается. Если в пределах grace-окна —
    пропускаем к word/link filter (сообщение может нарушить и их).

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

    try:
        async with async_session() as session:
            settings = await _get_chat_settings(session, chat_id)
            # Проверка word filter (включён всегда если есть паттерны —
            # пользователь просил "off by default" = нет паттернов = off)
            wf_match, matched_word = await _word_filter_match(session, chat_id, text)
            link_filter_on = settings.link_filter_enabled if settings else False
            link_filter_action = settings.link_filter_action if settings else "delete"
            if link_filter_on:
                has_blocked, blocked_domains = await _link_filter_check(session, chat_id, text)
            else:
                has_blocked, blocked_domains = False, []
    except Exception as e:
        logger.warning("handle_content_filters: DB error: %s (fail-open)", e)
        return

    # Если ничего не сработало — выходим, даст шанс catchall
    if wf_match is None and not has_blocked:
        return

    target = message.from_user

    # ── Определяем, какое действие применить ──
    # Приоритет: word filter (он более специфичный). Если word filter
    # сработал — используем его action. Иначе — link filter action.
    if wf_match is not None:
        action = wf_match.action
        reason = f"Word filter: '{matched_word}' (pattern: {wf_match.pattern})"
        mute_dur = wf_match.mute_duration
    else:
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
        until_date = int(datetime.now(timezone.utc).timestamp()) + dur
        try:
            await message.bot.restrict_chat_member(
                chat_id=chat_id, user_id=target.id,
                permissions=_mute_permissions(),
                until_date=until_date,
            )
            async with async_session() as session:
                await _save_punishment(
                    session, target.id, 0, chat_id,
                    "mute", dur, reason, target_content,
                )
            logger.info("Content filter (mute %s) in chat %s (user %s): %s",
                        _format_duration(dur), chat_id, target.id, reason)
        except TelegramAPIError as e:
            logger.error("Content filter mute failed: %s", e)
        return

    if action == "ban":
        try:
            await message.bot.ban_chat_member(chat_id=chat_id, user_id=target.id)
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
                asyncio.create_task(
                    _notify_su_about_chat(message.bot, _new_chat_id, _new_chat_title)
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
                    asyncio.create_task(
                        _notify_su_about_chat(event.bot, event.chat.id, event.chat.title)
                    )
                    logger.info(
                        "my_chat_member: bot added to chat id=%s title='%s'",
                        event.chat.id, event.chat.title,
                    )
        except Exception as e:
            logger.warning("on_my_chat_member: failed: %s", e)
