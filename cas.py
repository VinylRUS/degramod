"""cas.py — v5.3.2: ночная CAS+LOLS проверка уже сидящих в чате.

Дизайн: docs/superpowers/plans/2026-08-30-cas-lols-nightly.md (v2, 2026-08-30).

Ключевые ограничения и решения:
  • Telegram Bot API не умеет перечислять участников чата — «сидящие»
    известны боту только по тем, кто писал: таблица chat_members_seen,
    которую наполняет MembersSeenMiddleware (только чаты с
    cas_check_enabled=True). Люркеры невидимы — платформенный потолок;
    спамер безвреден, пока молчит: первое же сообщение попадает в проверку.
  • CAS (api.cas.chat) — per-id запросы, только ночью (окно 01:00–05:00
    МСК), ≤5 rps, fail-open (CAS лежит → юзер проходит).
  • LOLS (lols.bot) — раз в сутки скачивается bulk-лист scammers.json
    (~10k user_id) → вердикты мгновенно из памяти, 0 запросов к LOLS.
  • Вердикт юзера: CAS banned OR LOLS banned.
  • Авто-бан без подтверждений; ложное срабатывание → разбан → юзер
    автоматически попадает в cas_ignore (хук в revoke_user_ban) и больше
    не банился ночным свипом.
  • Отчётность: один дайджест в 05:00 МСК в репорт-чат — без поюзерных
    простыней (решение владельца 29.08.2026).

Расписание (одна фоновая таска cas_sweep_loop, тик 10 минут, время по МСК):
  01:00–01:59  ночной свип всех чатов с cas_check_enabled (раз в сутки)
  05:00–05:59  дайджест в репорт-чат (раз в сутки)
  санитарный день у чата → свип этого чата раз в час (усиленный режим)

День/ночь считаются по МСК (UTC+3, в РФ нет DST).
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

import aiohttp
from aiogram import BaseMiddleware
from sqlalchemy import select

from db import (
    CasIgnore,
    CasVerdict,
    ChatAdmin,
    ChatMemberSeen,
    ChatSettings,
    async_session,
)

logger = logging.getLogger("shadow_logger.cas")

# ── Константы ──────────────────────────────────────────────────────────────

# Дней с момента последней проверки юзера, прежде чем проверять снова.
CAS_VERDICT_TTL_DAYS = 30

# Сколько дней назад юзер должен был писать, чтобы попасть в ночной свип.
# 0 = свип выключен.
CAS_SWEEP_SEEN_DAYS = max(0, int(os.getenv("CAS_SWEEP_SEEN_DAYS", "90")))

# Целевой RPS к api.cas.chat во время ночного свипа.
_CAS_RPS = 5.0

# LOLS bulk-лист: свежесть, после которой перекачиваем (санитарные свипы).
_LOLS_MAX_AGE_HOURS = 6

# Таймаут одного CAS-запроса (тот же смысл, что в bot_handlers._cas_check_user).
_CAS_TIMEOUT = aiohttp.ClientTimeout(total=3.0)

_CAS_API_URL = "https://api.cas.chat/v1/status"
_LOLS_BULK_URL = "https://lols.bot/scammers.json"

# Часовой пояс расписания: МСК = UTC+3 (в РФ нет DST — фиксированный сдвиг).
_TZ_OFFSET = timedelta(hours=3)

# ── Состояние (in-memory; после рестарта свип отработает повторно —
# идемпотентно за счёт cas_verdicts: свежий кэш пропускается) ───────────────

# LOLS bulk: set(user_id) + момент загрузки.
_lols_set: set[int] = set()
_lols_loaded_at: datetime | None = None

# chat_id -> момент последнего санитарного свипа (усиленный режим).
_last_sanitary_sweep: dict[int, datetime] = {}

# Даты (МСК) последнего ночного свипа и дайджеста — guards «раз в сутки».
_last_nightly_date: str | None = None
_last_digest_date: str | None = None

# chat_id чатов с cas_check_enabled=True — обновляется loop'ом каждые 10 мин.
_cas_enabled_chat_ids: set[int] = set()

# Суточный аккумулятор для дайджеста: {checked, banned, ids}.
_day_stats: dict = {"checked": 0, "banned": 0, "ids": []}


def _now_msk() -> datetime:
    """Текущее время по МСК (UTC+3, без DST)."""
    return datetime.now(timezone.utc) + _TZ_OFFSET


# ── LOLS bulk ──────────────────────────────────────────────────────────────

async def refresh_lols_list() -> int:
    """Скачивает scammers.json → in-memory Set(user_id). Возвращает размер.

    Один запрос в сутки (ночное окно) + освежение при санитарных свипах,
    если список старше _LOLS_MAX_AGE_HOURS. Ошибка — старый Set сохраняется
    (лучше устаревший список, чем никакого).
    """
    global _lols_set, _lols_loaded_at
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=60),
        ) as http:
            async with http.get(_LOLS_BULK_URL) as resp:
                if resp.status != 200:
                    logger.warning(
                        "LOLS bulk download failed: HTTP %s (keeping old set)",
                        resp.status,
                    )
                    return len(_lols_set)
                data = await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
        logger.warning("LOLS bulk download failed: %s (keeping old set)", e)
        return len(_lols_set)

    ids: set[int] = set()
    if isinstance(data, list):
        for entry in data:
            if isinstance(entry, dict) and isinstance(entry.get("user_id"), int):
                ids.add(entry["user_id"])
    _lols_set = ids
    _lols_loaded_at = datetime.now(timezone.utc)
    logger.info("LOLS bulk list refreshed: %d user_ids", len(ids))
    return len(ids)


def _lols_is_banned(user_id: int) -> bool:
    """Мгновенный LOLS-вердикт из памяти (bulk-лист последних суток)."""
    return user_id in _lols_set


# ── Кэш вердиктов ──────────────────────────────────────────────────────────

async def _cached_verdict(user_id: int) -> tuple[bool, str, str | None] | None:
    """Свежий (≤30 дней) вердикт из cas_verdicts, иначе None."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=CAS_VERDICT_TTL_DAYS)
    async with async_session() as session:
        row = (await session.execute(
            select(CasVerdict).where(CasVerdict.user_id == user_id)
        )).scalar_one_or_none()
    if row is None:
        return None
    # SQLite отдаёт naive datetime — приводим к UTC-aware перед сравнением.
    checked = row.checked_at
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=timezone.utc)
    if checked < cutoff:
        return None
    return row.is_banned, row.source, row.reason


async def _store_verdict(user_id: int, source: str,
                         is_banned: bool, reason: str | None) -> None:
    """Upsert вердикта в кэш. Ошибки глотаются — кэш не критичен."""
    try:
        async with async_session() as session:
            row = (await session.execute(
                select(CasVerdict).where(CasVerdict.user_id == user_id)
            )).scalar_one_or_none()
            if row is None:
                session.add(CasVerdict(
                    user_id=user_id, source=source,
                    is_banned=is_banned, reason=reason,
                ))
            else:
                row.checked_at = datetime.now(timezone.utc)
                row.source = source
                row.is_banned = is_banned
                row.reason = reason
            await session.commit()
    except Exception as e:
        logger.warning("cas verdict store failed for %s: %s", user_id, e)


async def _is_ignored(user_id: int) -> bool:
    """Юзер в cas_ignore? (ложное срабатывание — не баним снова)."""
    async with async_session() as session:
        row = (await session.execute(
            select(CasIgnore).where(CasIgnore.user_id == user_id)
        )).scalar_one_or_none()
    return row is not None


async def add_to_ignore(user_id: int, added_by: int | None,
                        comment: str | None) -> None:
    """Добавляет юзера в cas_ignore (вызывается из revoke_user_ban-хука)."""
    async with async_session() as session:
        row = (await session.execute(
            select(CasIgnore).where(CasIgnore.user_id == user_id)
        )).scalar_one_or_none()
        if row is None:
            session.add(CasIgnore(user_id=user_id, added_by=added_by,
                                  comment=comment))
        else:
            row.added_by = added_by
            row.comment = comment
        await session.commit()


# ── chat_members_seen ──────────────────────────────────────────────────────

async def touch_member_seen(chat_id: int, user_id: int) -> None:
    """Upsert last_seen (MembersSeenMiddleware, каждое сообщение CAS-чата)."""
    now = datetime.now(timezone.utc)
    async with async_session() as session:
        row = (await session.execute(
            select(ChatMemberSeen).where(
                ChatMemberSeen.chat_id == chat_id,
                ChatMemberSeen.user_id == user_id,
            )
        )).scalar_one_or_none()
        if row is None:
            session.add(ChatMemberSeen(chat_id=chat_id, user_id=user_id,
                                       first_seen_at=now, last_seen_at=now))
        else:
            row.last_seen_at = now
        await session.commit()


async def refresh_enabled_chats() -> set[int]:
    """Обновляет in-memory набор chat_id с cas_check_enabled=True."""
    global _cas_enabled_chat_ids
    async with async_session() as session:
        rows = (await session.execute(
            select(ChatSettings.chat_id).where(
                ChatSettings.cas_check_enabled.is_(True),
                ChatSettings.is_enabled.is_(True),
            )
        )).scalars().all()
    _cas_enabled_chat_ids = set(rows)
    return _cas_enabled_chat_ids


class MembersSeenMiddleware(BaseMiddleware):
    """v5.3.2: обновляет chat_members_seen на каждом сообщении CAS-чата.

    Дёшево: для не-CAS чатов — чистый passthrough (одна проверка set);
    для CAS-чатов — один upsert в SQLite (WAL). Сам CAS днём не дёргается:
    вердикты собирает ночной свип.
    """

    async def __call__(self, handler, event, data):
        chat = getattr(event, "chat", None)
        user = getattr(event, "from_user", None)
        if (chat is not None
                and chat.type in ("group", "supergroup")
                and user is not None
                and not user.is_bot
                and chat.id in _cas_enabled_chat_ids):
            try:
                await touch_member_seen(chat.id, user.id)
            except Exception as e:
                logger.warning("touch_member_seen failed (chat=%s user=%s): %s",
                               chat.id, user.id, e)
        return await handler(event, data)


# ── Свип ───────────────────────────────────────────────────────────────────

async def _sweep_chat(bot, cs: ChatSettings) -> dict:
    """Прогон одного чата: chat_members_seen → вердикты → автобаны.

    Пропускает: cas_ignore, свежий кэш (≤30 дней — юзер уже известен),
    админов/модов чата, ADMIN_IDS, ботов. Баны перманентные, через
    tg_safe_call; каждая санкция — _save_punishment (mod_id=0, CAS System).
    """
    from bot_handlers import (  # lazy — против circular import
        ADMIN_IDS,
        TelegramAPIError,
        _cas_check_user,
        _mark_bot_ban,
        _save_punishment,
        _upsert_moderator,
        _upsert_user,
        tg_safe_call,
    )

    stats: dict = {"chat_id": cs.chat_id, "checked": 0,
                   "banned": 0, "users": []}
    cutoff = datetime.now(timezone.utc) - timedelta(days=CAS_SWEEP_SEEN_DAYS)

    async with async_session() as session:
        user_ids = (await session.execute(
            select(ChatMemberSeen.user_id).where(
                ChatMemberSeen.chat_id == cs.chat_id,
                ChatMemberSeen.last_seen_at >= cutoff,
            )
        )).scalars().all()
        chat_admin_ids = set((await session.execute(
            select(ChatAdmin.user_id).where(ChatAdmin.chat_id == cs.chat_id)
        )).scalars().all())

    for user_id in user_ids:
        # Свои — exempt (v4.7.30): админы чата, глобальные SU, боты.
        if user_id in chat_admin_ids or user_id in ADMIN_IDS:
            continue
        if await _is_ignored(user_id):
            continue

        # Кэш-первый: свежий вердикт (любой) → пропускаем юзера целиком.
        fresh = await _cached_verdict(user_id)
        if fresh is not None:
            continue

        # LOLS — мгновенно из памяти; CAS — per-id с троттлингом.
        if _lols_is_banned(user_id):
            is_banned, source, reason = True, "lols", "LOLS banlist"
        else:
            is_banned, reason = await _cas_check_user(user_id)
            source = "cas"
            stats["checked"] += 1
            await asyncio.sleep(1.0 / _CAS_RPS)
        await _store_verdict(user_id, source, is_banned, reason)

        if not is_banned:
            continue

        stats["banned"] += 1
        stats["users"].append(user_id)
        try:
            await tg_safe_call(
                lambda: bot.ban_chat_member(
                    chat_id=cs.chat_id, user_id=user_id,
                ),
                label="CAS_nightly_sweep",
            )
            _mark_bot_ban(cs.chat_id, user_id)
            async with async_session() as session:
                await _upsert_user(session, user_id, None, None, None)
                await _upsert_moderator(session, 0, None, "CAS System")
                await _save_punishment(
                    session, user_id, 0, cs.chat_id,
                    "ban", None,
                    f"CAS nightly sweep ({source}): {reason}", None,
                )
            logger.info(
                "CAS nightly sweep: banned user_id=%s in chat %s (source=%s, reason=%s)",
                user_id, cs.chat_id, source, reason,
            )
        except TelegramAPIError as e:
            logger.error(
                "CAS nightly ban failed for user %s in chat %s: %s",
                user_id, cs.chat_id, e,
            )
    return stats


async def _nightly_sweep_all(bot) -> None:
    """Ночной свип всех чатов с cas_check_enabled=True."""
    await refresh_lols_list()
    for chat_id in sorted(_cas_enabled_chat_ids):
        async with async_session() as session:
            cs = (await session.execute(
                select(ChatSettings).where(ChatSettings.chat_id == chat_id)
            )).scalar_one_or_none()
        if cs is None:
            continue
        stats = await _sweep_chat(bot, cs)
        _accumulate(stats)
        logger.info("CAS nightly sweep chat %s: %s", chat_id, stats)


async def _sanitary_boost(bot, now: datetime) -> None:
    """Усиленный режим санитарного дня: свип CAS-чата раз в час.

    Санитарный день = чат под прицелом модерации; прирост юзеров маленький
    (права ограничены), свип быстрый. LOLS-лист освежается, если старше 6 ч.
    """
    if not _cas_enabled_chat_ids:
        return
    if (_lols_loaded_at is None
            or (datetime.now(timezone.utc) - _lols_loaded_at)
            > timedelta(hours=_LOLS_MAX_AGE_HOURS)):
        await refresh_lols_list()

    async with async_session() as session:
        chats = (await session.execute(
            select(ChatSettings).where(
                ChatSettings.cas_check_enabled.is_(True),
                ChatSettings.is_enabled.is_(True),
                ChatSettings.sanitary_days_currently_active.is_(True),
            )
        )).scalars().all()

    for cs in chats:
        last = _last_sanitary_sweep.get(cs.chat_id)
        if last is not None and (now - last) < timedelta(minutes=55):
            continue
        _last_sanitary_sweep[cs.chat_id] = now
        stats = await _sweep_chat(bot, cs)
        _accumulate(stats)
        logger.info("CAS sanitary boost sweep chat %s: %s", cs.chat_id, stats)


# ── Дайджест ───────────────────────────────────────────────────────────────

def _digest_text() -> str:
    """Одна строка за сутки — живой индикатор ночного дежурства."""
    checked = _day_stats.get("checked", 0)
    banned = _day_stats.get("banned", 0)
    ids = _day_stats.get("ids", [])
    if banned:
        shown = ", ".join(str(u) for u in ids[:5])
        more = f" (+{banned - len(ids)})" if banned > len(ids) else ""
        return (f"🛡️ CAS/LOLS за сутки: проверено {checked}, "
                f"забанено {banned} (id: {shown}{more})")
    return f"🛡️ CAS/LOLS за сутки: проверено {checked}, забанено 0"


def _accumulate(stats: dict) -> None:
    _day_stats["checked"] += stats.get("checked", 0)
    _day_stats["banned"] += stats.get("banned", 0)
    _day_stats["ids"].extend(stats.get("users", []))


def _reset_day_stats() -> None:
    _day_stats["checked"] = 0
    _day_stats["banned"] = 0
    _day_stats["ids"].clear()


async def _send_daily_digest(bot) -> None:
    """Дайджест в репорт-чат каждого CAS-чата (дедуп по resolved chat_id)."""
    from bot_handlers import _get_report_chat_id  # lazy

    if not _cas_enabled_chat_ids:
        return
    text = _digest_text()
    targets: set[int] = set()
    async with async_session() as session:
        for chat_id in sorted(_cas_enabled_chat_ids):
            try:
                rid = await _get_report_chat_id(session, chat_id)
                if rid:
                    targets.add(rid)
            except Exception as e:
                logger.warning(
                    "digest: report chat resolve failed for %s: %s", chat_id, e,
                )
    for rid in sorted(targets):
        try:
            await bot.send_message(chat_id=rid, text=text, parse_mode=None)
        except Exception as e:
            logger.warning("digest send failed to %s: %s", rid, e)
    _reset_day_stats()


# ── Фоновая таска ──────────────────────────────────────────────────────────

async def cas_sweep_loop(bot) -> None:
    """v5.3.2: ночное окно 01:00–05:00 МСК + санитарный буст + дайджест.

    Тик 10 минут. Guards «раз в сутки» по дате (МСК). После рестарта свип
    может отработать повторно — идемпотентно: cas_verdicts кэширует
    вердикты на 30 дней, свежие пропускаются.
    """
    global _last_nightly_date, _last_digest_date
    await asyncio.sleep(20)  # дать стартовать остальным таскам
    try:
        await refresh_enabled_chats()
        await refresh_lols_list()
    except Exception as e:
        logger.warning("cas_sweep_loop: startup refresh failed: %s", e)

    while True:
        try:
            now = _now_msk()
            today = now.date().isoformat()
            await refresh_enabled_chats()

            # Ночной свип: 01:00–01:59 МСК, раз в сутки.
            if now.hour == 1 and _last_nightly_date != today:
                _last_nightly_date = today
                logger.info(
                    "CAS nightly sweep started (chats: %d)",
                    len(_cas_enabled_chat_ids),
                )
                await _nightly_sweep_all(bot)

            # Дайджест: 05:00–05:59 МСК, раз в сутки.
            if now.hour == 5 and _last_digest_date != today:
                _last_digest_date = today
                await _send_daily_digest(bot)

            # Санитарный буст: усиленный свип раз в час днём.
            if now.hour not in (1, 2, 5):
                await _sanitary_boost(bot, now)

        except Exception as e:
            logger.error("cas_sweep_loop tick error: %s", e)
        await asyncio.sleep(600)


# ── Регистрация middleware ─────────────────────────────────────────────────
# bot.py импортирует cas ПОСЛЕ bot_handlers — роутер уже собран; aiogram
# применяет middleware динамически, поэтому порядок не важен.
from bot_handlers import router as _bh_router

_bh_router.message(MembersSeenMiddleware())
