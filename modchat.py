"""
modchat.py — v4.8.0: модераторский чат + keyword-watch.

Содержит:
  • `_get_mod_chat_id(session, chat_id)` — аналогично `_get_report_chat_id`,
    но для modchat.
  • `_send_to_modchat(bot, chat_id, text)` — отправка простого текста в modchat.
  • `_send_alarm_event_to_modchat(bot, chat_id, event_type, ...)` — отправка
    alarm-событий (on/off/auto-off/продление) с консолидацией продлений.
  • `_keyword_watch_match(session, text)` — matcher: substring для фраз с
    пробелом, word-boundary для одиночных слов.
  • `_send_keyword_notify_to_modchat(bot, chat_id, message, matches)` —
    rich-уведомление о срабатывании keyword-watch.

Принцип:
  • Modchat — это отдельный чат для оперативных оповещений модераторам.
    В отличие от report_chat (журнал санкций с rich-превью), modchat —
    краткий текстовый формат.
  • Keyword-watch — замена word_filter. День: только notify в modchat.
    Ночь: фразы с ban_in_night_mode=True → автобан, без флага → notify.

См. ROADMAP_v4.8.0.md пункт #10 для полного описания.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from aiogram import types
from aiogram.exceptions import TelegramAPIError
from sqlalchemy import select

if TYPE_CHECKING:
    from db import KeywordWatch

logger = logging.getLogger("shadow_logger")


# ── Константы ───────────────────────────────────────────────────────────────

# Консолидация продлений alarm: если за это окно было несколько продлений,
# шлём одно консолидированное сообщение.
_ALARM_EXTEND_CONSOLIDATE_WINDOW_SECONDS: int = 60

# Rate-limit для keyword-watch: не чаще одного notify на (chat_id, keyword)
# в течение этого окна. Если за окно было N срабатываний — шлём одно со
# счётчиком "ещё N-1 за последнюю минуту".
_KEYWORD_RATE_LIMIT_SECONDS: int = 60


# ── In-memory state ─────────────────────────────────────────────────────────
# Консолидация продлений: {chat_id: {"first_started_by": int, "first_until": dt,
#                                     "extends": [(mod_id, delta_str, ts_str)],
#                                     "last_until": dt, "task": asyncio.Task}}
_alarm_extend_state: dict[int, dict] = {}

# Rate-limit: {(chat_id, phrase_lower): {"last_notify_ts": float, "count": int}}
_keyword_rate_limit: dict[tuple[int, str], dict] = {}


# ── Get modchat ID ──────────────────────────────────────────────────────────
async def _get_mod_chat_id(session, chat_id: int) -> int | None:
    """v4.8.0: возвращает ID чата для модераторских оповещений.

    Приоритет:
      1. Per-chat override (ChatSettings.mod_chat_id для данного chat_id)
      2. Любой чат с is_mod_chat=True (первый попавшийся)
      3. Глобальный default (ChatSettings.mod_chat_id для chat_id=0)
      4. None (модераторский чат не задан — оповещения отключены)
    """
    from db import ChatSettings

    # 1. Per-chat override
    settings = await _get_chat_settings(session, chat_id)
    if settings.mod_chat_id is not None and settings.mod_chat_id != 0:
        return settings.mod_chat_id

    # 2. Любой чат с is_mod_chat=True
    rc = (await session.execute(
        select(ChatSettings.chat_id).where(
            ChatSettings.is_mod_chat.is_(True),
            ChatSettings.chat_id != 0,
        ).limit(1)
    )).scalars().first()
    if rc is not None:
        return rc

    # 3. Глобальный default (chat_id=0)
    default_settings = await _get_chat_settings(session, 0)
    if default_settings.mod_chat_id is not None and default_settings.mod_chat_id != 0:
        return default_settings.mod_chat_id

    # 4. Disabled
    return None


async def _get_chat_settings(session, chat_id: int):
    """Хелпер: получить ChatSettings (использует кеш bot_handlers если есть)."""
    from db import ChatSettings
    return (await session.execute(
        select(ChatSettings).where(ChatSettings.chat_id == chat_id)
    )).scalar_one_or_none()


# ── Простая отправка в modchat ──────────────────────────────────────────────
async def _send_to_modchat(
    bot: types.Bot, source_chat_id: int, text: str,
) -> bool:
    """Отправляет простое текстовое сообщение в modchat.

    Args:
      bot: aiogram Bot instance.
      source_chat_id: ID чата, к которому относится оповещение (для
        определения mod_chat_id через per-chat override).
      text: текст сообщения (HTML).

    Returns:
      True при успехе, False если modchat не задан или отправка не удалась.
    """
    try:
        from db import async_session
        async with async_session() as session:
            mod_chat_id = await _get_mod_chat_id(session, source_chat_id)
        if mod_chat_id is None:
            return False
        await bot.send_message(
            chat_id=mod_chat_id,
            text=text,
            parse_mode="HTML",
        )
        return True
    except TelegramAPIError as e:
        logger.warning(
            "Send to modchat failed (source_chat=%s): %s",
            source_chat_id, e,
        )
        return False
    except Exception as e:
        logger.warning("Send to modchat unexpected error: %s", e)
        return False


# ── Alarm-события в modchat ─────────────────────────────────────────────────

async def _send_alarm_event_to_modchat(
    bot: types.Bot,
    chat_id: int,
    event_type: str,
    *,
    mod_user: types.User | None = None,
    mod_id: int | None = None,
    duration_str: str | None = None,
    active_until: datetime | None = None,
    prev_mod_display: str | None = None,
    reason: str | None = None,
) -> None:
    """Отправляет alarm-событие в modchat.

    Поддерживаемые event_type:
      • "on"          — !alarm on (новый alarm).
      • "off"         — !alarm off (ручное снятие).
      • "auto_off"    — auto-off по таймауту.
      • "off_by_mode" — снятие входом в night mode / sanitary day.
      • "extend"      — !alarm on когда уже активен (продление).

    Для "extend" — события консолидируются: если за 60 сек было несколько
    продлений, шлём одно консолидированное сообщение с историей "кто, когда,
    на сколько". On/off/auto_off/off_by_mode — всегда мгновенно, без
    консолидации.

    Args:
      bot: aiogram Bot instance.
      chat_id: ID чата, где произошло событие.
      event_type: тип события (см. выше).
      mod_user: объект User модератора (если есть) — для mention.
      mod_id: ID модератора (если mod_user нет, fallback на id:NNN).
      duration_str: длительность в человекочитаемом виде ("30 мин").
      active_until: datetime окончания alarm (для "on" и "extend").
      prev_mod_display: строка с инфо о предыдущем модераторе (для "extend").
      reason: причина (для "auto_off" и "off_by_mode").
    """
    if event_type == "extend":
        # Консолидируем — добавляем в очередь и планируем отправку.
        await _enqueue_alarm_extend(
            bot=bot, chat_id=chat_id,
            mod_user=mod_user, mod_id=mod_id,
            duration_str=duration_str, active_until=active_until,
            prev_mod_display=prev_mod_display,
        )
        return

    # Не-extend события — отправляем мгновенно.
    try:
        chat_info = await bot.get_chat(chat_id=chat_id)
        chat_title = chat_info.title or f"id:{chat_id}"
    except TelegramAPIError:
        chat_title = f"id:{chat_id}"

    mod_mention = "неизвестно"
    if mod_user is not None:
        mod_mention = _user_mention_html(mod_user)
    elif mod_id is not None:
        mod_mention = f"id:{mod_id}"

    text_parts: list[str] = []
    if event_type == "on":
        text_parts.append(f"🚨 <b>В чате «{chat_title}» поставлен режим тревоги</b>")
        text_parts.append(f"Модератор: {mod_mention}")
        if duration_str:
            text_parts.append(f"Длительность: {duration_str}")
        if active_until:
            text_parts.append(f"До: {_format_dt_msk(active_until)}")
    elif event_type == "off":
        text_parts.append(f"✅ <b>В чате «{chat_title}» снят режим тревоги</b>")
        text_parts.append(f"Модератор: {mod_mention}")
    elif event_type == "auto_off":
        text_parts.append(f"⏱ <b>В чате «{chat_title}» автоматически снят режим тревоги</b>")
        if reason:
            text_parts.append(f"Причина: {reason}")
    elif event_type == "off_by_mode":
        text_parts.append(f"🔒 <b>В чате «{chat_title}» снят режим тревоги</b>")
        if reason:
            text_parts.append(f"Причина: {reason}")
    else:
        text_parts.append(f"⚠️ Alarm event ({event_type}) в чате «{chat_title}»")

    text = "\n".join(text_parts)
    await _send_to_modchat(bot, chat_id, text)


async def _enqueue_alarm_extend(
    *,
    bot: types.Bot,
    chat_id: int,
    mod_user: types.User | None,
    mod_id: int | None,
    duration_str: str | None,
    active_until: datetime | None,
    prev_mod_display: str | None,
) -> None:
    """Добавляет продление в очередь консолидации.

    Если очередь пуста — создаём запись и планируем отправку через 60 сек.
    Если уже есть — добавляем в extends и обновляем last_until.
    """
    # v4.8.9: ruff поймал unused `now_ts = time.time()` — убрали. В коде
    # ниже используется `datetime.now(timezone.utc)` для форматирования.
    mod_mention = "неизвестно"
    if mod_user is not None:
        mod_mention = _user_mention_html(mod_user)
    elif mod_id is not None:
        mod_mention = f"id:{mod_id}"

    state = _alarm_extend_state.get(chat_id)
    if state is None:
        # Первое продление в окне — создаём запись.
        _alarm_extend_state[chat_id] = {
            "first_started_by_display": prev_mod_display or mod_mention,
            "first_until": active_until,
            "extends": [(mod_mention, duration_str or "?", _format_dt_msk_short(datetime.now(timezone.utc)))],
            "last_until": active_until,
            "task": asyncio.create_task(
                _flush_alarm_extend(bot, chat_id, delay=_ALARM_EXTEND_CONSOLIDATE_WINDOW_SECONDS)
            ),
        }
    else:
        # Уже есть — добавляем в extends.
        state["extends"].append(
            (mod_mention, duration_str or "?", _format_dt_msk_short(datetime.now(timezone.utc)))
        )
        state["last_until"] = active_until


async def _flush_alarm_extend(bot: types.Bot, chat_id: int, *, delay: int) -> None:
    """Отправляет консолидированное сообщение о продлениях через delay секунд."""
    await asyncio.sleep(delay)
    state = _alarm_extend_state.pop(chat_id, None)
    if state is None:
        return

    try:
        chat_info = await bot.get_chat(chat_id=chat_id)
        chat_title = chat_info.title or f"id:{chat_id}"
    except TelegramAPIError:
        chat_title = f"id:{chat_id}"

    extends_list = state["extends"]
    last_until = state["last_until"]
    first_started_by = state["first_started_by_display"]

    text_parts: list[str] = []
    text_parts.append(f"⏱ <b>Alarm в чате «{chat_title}» продлён</b>")
    text_parts.append(f"Изначально поставил: {first_started_by}")
    if len(extends_list) == 1:
        mod_mention, dur, ts = extends_list[0]
        text_parts.append(f"Продление: {mod_mention} (+{dur}, {ts})")
    else:
        text_parts.append(f"Продления ({len(extends_list)}):")
        for mod_mention, dur, ts in extends_list:
            text_parts.append(f"• {mod_mention} (+{dur}, {ts})")
    if last_until:
        text_parts.append(f"Текущее окончание: {_format_dt_msk(last_until)}")

    text = "\n".join(text_parts)
    await _send_to_modchat(bot, chat_id, text)


# ── Keyword-watch matcher ───────────────────────────────────────────────────

# Регулярное выражение для word-boundary matching в Unicode (кириллица).
# \b в Python re работает плохо для Unicode, поэтому используем custom pattern.
# Граница слова = (?:^|[^\\w]) перед фразой и (?:$|[^\\w]) после.
# Это не идеально для всех языков, но для русского/английского работает.
_WORD_BOUNDARY_RE_CACHE: dict[str, re.Pattern] = {}


def _compile_word_boundary(phrase_lower: str) -> re.Pattern:
    """Компилирует regex для word-boundary match одиночного слова."""
    if phrase_lower in _WORD_BOUNDARY_RE_CACHE:
        return _WORD_BOUNDARY_RE_CACHE[phrase_lower]
    # Экранируем спецсимволы regex в фразе.
    escaped = re.escape(phrase_lower)
    # Граница слова: не-буква/цифра или начало/конец строки.
    # Используем lookbehind/lookahead чтобы не захватывать границу.
    pattern = re.compile(
        r"(?:(?<=^)|(?<=[^\w]))" + escaped + r"(?:(?=$)|(?=[^\w]))",
        re.IGNORECASE | re.UNICODE,
    )
    _WORD_BOUNDARY_RE_CACHE[phrase_lower] = pattern
    return pattern


async def _keyword_watch_match(
    session, text: str,
) -> list["KeywordWatch"]:
    """Проверяет текст по keyword_watch для всех чатов (глобальный список).

    Возвращает список совпавших KeywordWatch объектов (может быть пустым).

    Match logic:
      • Если фраза содержит пробел → case-insensitive substring.
      • Если фраза — одно слово → word-boundary match (Unicode-aware).

    Multiplexing: возвращает ВСЕ совпавшие фразы, вызывающий код отправляет
    ОДНО уведомление со списком.
    """
    if not text:
        return []
    from db import KeywordWatch

    text_lower = text.lower()
    rows = (await session.execute(
        select(KeywordWatch).where(
            KeywordWatch.chat_id == 0,  # глобальный список
            KeywordWatch.is_active.is_(True),
        )
    )).scalars().all()

    matches: list[KeywordWatch] = []
    for kw in rows:
        phrase_lower = kw.phrase.lower()
        if " " in kw.phrase:
            # Фраза с пробелом → substring match.
            if phrase_lower in text_lower:
                matches.append(kw)
        else:
            # Одиночное слово → word-boundary match.
            pattern = _compile_word_boundary(phrase_lower)
            if pattern.search(text):
                matches.append(kw)
    return matches


def _check_keyword_rate_limit(chat_id: int, phrase_lower: str) -> tuple[bool, int]:
    """Проверяет rate-limit для (chat_id, phrase).

    Returns:
      (allowed, suppressed_count) — allowed=True если можно слать notify,
                                    suppressed_count = сколько срабатываний
                                    было suppressed с последнего notify.
    """
    key = (chat_id, phrase_lower)
    now_ts = time.time()
    state = _keyword_rate_limit.get(key)
    if state is None:
        # Первое срабатывание — слать.
        _keyword_rate_limit[key] = {"last_notify_ts": now_ts, "count": 0}
        return True, 0
    elapsed = now_ts - state["last_notify_ts"]
    if elapsed >= _KEYWORD_RATE_LIMIT_SECONDS:
        # Окно прошло — слать, обнуляем счётчик.
        suppressed = state["count"]
        _keyword_rate_limit[key] = {"last_notify_ts": now_ts, "count": 0}
        return True, suppressed
    # Внутри окна — suppress, увеличиваем счётчик.
    state["count"] += 1
    return False, state["count"]


# ── v5.3.0: удалённое сообщение канала ──────────────────────────────────────

# Rate-limit нотификаций об удалении: {(chat_id, channel_id): last_notify_ts}.
# Канал, который спамит, шлёт сообщения пачками — модчату хватит одного
# оповещения на канал за окно.
_channel_delete_rate_limit: dict[tuple[int, int], float] = {}
_CHANNEL_DELETE_RATE_LIMIT_SECONDS: int = 60


async def _send_channel_deleted_to_modchat(
    bot: types.Bot,
    source_chat_id: int,
    sender_chat,
    content_desc: str | None,
) -> bool:
    """Оповещает модчат об удалённом сообщении от имени чужого канала.

    В чате бот молчит (стелс-режим), поэтому ложное срабатывание фильтра
    иначе никак не заметить. Поэтому же в тексте — готовая подсказка, как
    внести канал в белый список: реплаем, без похода в веб-панель.

    Rate-limit: не чаще одного оповещения на (чат, канал) в минуту —
    спамящий канал шлёт сообщения пачками.

    Returns: True при успехе, False если modchat не задан, отправка не
    удалась или сработал rate-limit.
    """
    channel_id = getattr(sender_chat, "id", None)
    if channel_id is None:
        return False

    key = (source_chat_id, channel_id)
    now_ts = time.time()
    last = _channel_delete_rate_limit.get(key)
    if last is not None and now_ts - last < _CHANNEL_DELETE_RATE_LIMIT_SECONDS:
        return False
    _channel_delete_rate_limit[key] = now_ts

    title = getattr(sender_chat, "title", None) or "(без названия)"
    username = getattr(sender_chat, "username", None)
    lines = [
        "📢 <b>Удалено сообщение от имени канала</b>",
        f"<b>Канал:</b> {html.escape(title, quote=False)}",
    ]
    if username:
        lines.append(f"<b>Username:</b> @{html.escape(username, quote=False)}")
    lines.append(f"<b>ID канала:</b> <code>{channel_id}</code>")
    if content_desc:
        preview = content_desc[:300]
        lines.append(f"<b>Текст:</b> {html.escape(preview, quote=False)}")
    lines.append(
        "\n💡 Если удалено зря — ответьте на сообщение канала командой "
        "<code>/channelallow</code>, либо "
        f"<code>/channelallow {source_chat_id} {channel_id}</code> в личке бота."
    )
    return await _send_to_modchat(bot, source_chat_id, "\n".join(lines))


# ── Keyword-watch notify (rich format) ──────────────────────────────────────

async def _send_keyword_notify_to_modchat(
    bot: types.Bot,
    source_chat_id: int,
    message: types.Message,
    matches: list["KeywordWatch"],
    suppressed_counts: dict[str, int] | None = None,
) -> bool:
    """Отправляет rich-уведомление о срабатывании keyword-watch в modchat.

    Формат (rich-блоки для единообразия с репортами):
      • SectionHeading — «👀 Ключевое слово в чате «X»»
      • Divider
      • List — нарушитель (имя кликабельно → tg://user?id=...), время МСК,
        сработавшая фраза (или список фраз, если несколько совпадений).
      • Divider
      • Footer — текст + suppressed count (если >0).

    Использует обычный HTML (не rich-блоки) — упрощённая версия. В будущем
    можно расширить до rich-блоков как в _send_report.

    Returns:
      True при успехе, False если modchat не задан или отправка не удалась.
    """
    try:
        from db import async_session
        async with async_session() as session:
            mod_chat_id = await _get_mod_chat_id(session, source_chat_id)
        if mod_chat_id is None:
            return False
    except Exception as e:
        logger.warning("Keyword notify: cannot get modchat: %s", e)
        return False

    # Получаем title чата.
    try:
        chat_info = await bot.get_chat(chat_id=source_chat_id)
        chat_title = chat_info.title or f"id:{source_chat_id}"
    except TelegramAPIError:
        chat_title = f"id:{source_chat_id}"

    user = message.from_user
    user_mention = "неизвестно"
    if user is not None:
        user_mention = _user_mention_html(user)

    text_content = message.text or message.caption or ""
    text_preview = text_content[:500]
    if len(text_content) > 500:
        text_preview += "..."

    phrases_str = ", ".join(f"«{html_escape(kw.phrase)}»" for kw in matches)

    # Suppressed count (если было больше срабатываний за rate-limit окно).
    suppressed_total = 0
    if suppressed_counts:
        suppressed_total = sum(suppressed_counts.values())

    text_parts: list[str] = []
    text_parts.append(f"👀 <b>Ключевое слово в чате «{html_escape(chat_title)}»</b>")
    text_parts.append(f"Юзер: {user_mention}")
    text_parts.append(f"Время: {_format_dt_msk(datetime.now(timezone.utc))}")
    text_parts.append(f"Совпадение: {phrases_str}")
    if suppressed_total > 0:
        text_parts.append(f"Ещё {suppressed_total} за последнюю минуту")
    text_parts.append("")
    text_parts.append("<b>Текст сообщения:</b>")
    text_parts.append(f"<i>{html_escape(text_preview)}</i>")
    # Кнопка «Перейти к сообщению» — через URL.
    msg_link = _get_message_link(message)
    if msg_link:
        text_parts.append(f'\n<a href="{msg_link}">Перейти к сообщению →</a>')

    text = "\n".join(text_parts)
    try:
        await bot.send_message(
            chat_id=mod_chat_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return True
    except TelegramAPIError as e:
        logger.warning(
            "Keyword notify send failed (source_chat=%s): %s",
            source_chat_id, e,
        )
        return False


def _get_message_link(message: types.Message) -> str | None:
    """Возвращает URL сообщения для кнопки «Перейти к сообщению».

    Для приватных чатов: https://t.me/c/{internal_id}/{message_id}
    Для публичных: https://t.me/{username}/{message_id}
    Для приватных без username: возвращаем None.
    """
    try:
        return message.link
    except Exception:
        return None


# ── Хелперы форматирования ──────────────────────────────────────────────────

def _user_mention_html(user: types.User) -> str:
    """HTML-mention юзера для модераторских уведомлений."""
    name = (user.first_name or "") + (
        f" {user.last_name}" if user.last_name else ""
    )
    name = name.strip() or f"id:{user.id}"
    if user.username:
        return f'{html_escape(name)} (<a href="t.me/{user.username}">@{user.username}</a>, id:{user.id})'
    return f'<a href="tg://user?id={user.id}">{html_escape(name)}</a> (id:{user.id})'


def _html_escape_unsafe(s: str) -> str:
    """HTML-escape для безопасной вставки в HTML-сообщение."""
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
    )


# Используем html.escape из stdlib (не было в импортах).
def html_escape(s: str) -> str:
    """HTML-escape через stdlib."""
    import html as _html
    return _html.escape(s, quote=False)


def _format_dt_msk(dt: datetime) -> str:
    """Форматирует datetime в МСК-строку 'YYYY-MM-DD HH:MM МСК'."""
    if dt is None:
        return "N/A"
    try:
        from zoneinfo import ZoneInfo
        msk = ZoneInfo("Europe/Moscow")
        dt_msk = dt.astimezone(msk) if dt.tzinfo else dt.replace(tzinfo=timezone.utc).astimezone(msk)
        return dt_msk.strftime("%Y-%m-%d %H:%M МСК")
    except Exception:
        return dt.strftime("%Y-%m-%d %H:%M UTC")


def _format_dt_msk_short(dt: datetime) -> str:
    """Короткий формат: 'HH:MM'."""
    if dt is None:
        return "?"
    try:
        from zoneinfo import ZoneInfo
        msk = ZoneInfo("Europe/Moscow")
        dt_msk = dt.astimezone(msk) if dt.tzinfo else dt.replace(tzinfo=timezone.utc).astimezone(msk)
        return dt_msk.strftime("%H:%M")
    except Exception:
        return dt.strftime("%H:%M")
