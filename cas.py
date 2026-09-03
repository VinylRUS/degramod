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
  • LOLS (lols.bot) — три bulk-списка: verified scammers (ночь, Tier A),
    баны последнего часа (каждый час, Tier B), полный банлист (ночь,
    Tier C — потенциальные).
  • Вердикт: бан ТОЛЬКО подтверждённым — LOLS verified/hot или CAS banned.
    Потенциальные скамеры (полный банлист LOLS без подтверждения) —
    помечаются в cas_verdicts и считаются в дайджесте, но НЕ банимся
    (решение владельца 30.08.2026).
  • Авто-бан без подтверждений; ложное срабатывание → разбан → юзер
    автоматически попадает в cas_ignore (хук в revoke_user_ban) и больше
    не банился ночным свипом.
  • Отчётность: один дайджест в 05:00 МСК в репорт-чат — без поюзерных
    простыней (решение владельца 29.08.2026).

Расписание (одна фоновая таска cas_sweep_loop, тик 10 минут, время по МСК):
  каждый час   banlist-1h (~10 КБ) — Tier B: дневное покрытие без per-id
  01:00–01:59  ночной свип всех чатов с cas_check_enabled (раз в сутки)
  05:00–05:59  дайджест в репорт-чат (раз в сутки)
  санитарный день у чата → свип этого чата раз в час (усиленный режим)

День/ночь считаются по МСК (UTC+3, в РФ нет DST).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone

import aiohttp
from aiogram import BaseMiddleware
from sqlalchemy import select

from db import (
    CasIgnore,
    CasSettings,
    CasVerdict,
    ChatAdmin,
    ChatMemberSeen,
    ChatSettings,
    WebUser,
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
_LOLS_BULK_URL = "https://lols.bot/scammers.json"          # Tier A: verified
_LOLS_HOT_URL = "https://lols.bot/spam/banlist-1h.json"    # Tier B: баны за час (~10 КБ)
_LOLS_FULL_URL = "https://lols.bot/spam/banlist.json"      # Tier C: весь банлист (43 МБ)

# Часовой пояс расписания: МСК = UTC+3 (в РФ нет DST — фиксированный сдвиг).
_TZ_OFFSET = timedelta(hours=3)

# ── Состояние (in-memory; после рестарта свип отработает повторно —
# идемпотентно за счёт cas_verdicts: свежий кэш пропускается) ───────────────

# LOLS verified (scammers.json): set(user_id) + момент загрузки.
# v5.4.0: единственный LOLS-список, по которому БАНИМ из bulk-источников.
_lols_set: set[int] = set()
_lols_loaded_at: datetime | None = None

# LOLS banlist-1h: баны последнего часа (Tier B, обновляется каждый час).
_lols_hot_set: set[int] = set()
_lols_hot_at: datetime | None = None

# LOLS полный банлист (Tier C): потенциальные скамеры — только помечаем.
_lols_full_set: set[int] = set()
_lols_full_at: datetime | None = None

# chat_id -> момент последнего санитарного свипа (усиленный режим).
_last_sanitary_sweep: dict[int, datetime] = {}

# Даты (МСК) последнего ночного свипа и дайджеста — guards «раз в сутки».
_last_nightly_date: str | None = None
_last_digest_date: str | None = None

# chat_id чатов с cas_check_enabled=True — обновляется loop'ом каждые 10 мин.
_cas_enabled_chat_ids: set[int] = set()

# Суточный аккумулятор для дайджеста: {checked, banned, ids}.
_day_stats: dict = {"checked": 0, "banned": 0, "marked": 0, "ids": []}


def _now_msk() -> datetime:
    """Текущее время по МСК (UTC+3, без DST)."""
    return datetime.now(timezone.utc) + _TZ_OFFSET


# ── LOLS bulk ──────────────────────────────────────────────────────────────

_INT_RE = re.compile(rb"-?\d+")


def _parse_lols_payload(raw: bytes, label: str) -> set[int]:
    """Тело bulk-листа LOLS → set(user_id). Кривая форма → пустой set.

    v5.4.0 FIX: у lols.bot ДВЕ разных формы ответа, и парсер знал только
    первую — banlist-1h.json и banlist.json разбирались в пустой набор,
    то есть Tier B и Tier C не работали вовсе (тесты подсовывали
    `_lols_hot_set`/`_lols_full_set` напрямую и промаха не видели):
      • scammers.json — [{"user_id": 123, "names": [...], ...}, ...]
      • banlist*.json — [123, -100456, ...] — плоский список чисел.

    Плоский список разбираем регуляркой по сырому телу, а не json.loads:
    в banlist.json 4.1 млн чисел, и промежуточный list[int] добавляет
    ~200 МБ к пиковому RSS сверх самого set'а.
    """
    head = raw[:64].lstrip()
    if not head.startswith(b"["):
        logger.warning("LOLS %s: unexpected response shape", label)
        return set()
    if head[1:].lstrip()[:1] != b"{":
        return {int(m.group()) for m in _INT_RE.finditer(raw)}

    try:
        data = json.loads(raw)
    except ValueError as e:
        logger.warning("LOLS %s: bad JSON (%s)", label, e)
        return set()
    ids: set[int] = set()
    for entry in data:
        if isinstance(entry, dict) and isinstance(entry.get("user_id"), int):
            ids.add(entry["user_id"])
    return ids


async def _fetch_lols_ids(url: str, label: str) -> set[int] | None:
    """Скачивает bulk-лист LOLS → set(user_id).

    None — сбой сети/HTTP (вызывающий решает, сохранять ли старый набор);
    пустой set — ответ пришёл, но разобрать нечего.
    """
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=180),
        ) as http:
            async with http.get(url) as resp:
                if resp.status != 200:
                    logger.warning(
                        "LOLS %s download failed: HTTP %s", label, resp.status,
                    )
                    return None
                raw = await resp.read()
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
        logger.warning("LOLS %s download failed: %s", label, e)
        return None

    return _parse_lols_payload(raw, label)


async def refresh_lols_list() -> int:
    """Verified-список (scammers.json, Tier A) → _lols_set. Возвращает размер.

    Один запрос в сутки (ночное окно) + освежение при санитарных свипах,
    если список старше _LOLS_MAX_AGE_HOURS. Ошибка/пустой ответ — старый
    Set сохраняется (лучше устаревший список, чем никакого).
    """
    global _lols_set, _lols_loaded_at
    ids = await _fetch_lols_ids(_LOLS_BULK_URL, "verified")
    if ids:
        _lols_set = ids
        _lols_loaded_at = datetime.now(timezone.utc)
    logger.info("LOLS verified list refreshed: %d user_ids", len(_lols_set))
    return len(_lols_set)


async def refresh_lols_hot() -> int:
    """Banlist-1h (Tier B: баны LOLS за последний час) → _lols_hot_set.

    ~10 КБ, вызывается каждый час (день и ночь) — дневное покрытие без
    per-id запросов. Часовой список можно спокойно обнулять: он и есть
    «бан за последний час».
    """
    global _lols_hot_set, _lols_hot_at
    ids = await _fetch_lols_ids(_LOLS_HOT_URL, "hot")
    if ids is None:
        # Сбой сети: держим предыдущий набор (он максимум на час старее),
        # время загрузки не штампуем — следующий тик повторит попытку.
        logger.warning(
            "LOLS hot banlist: refresh failed, keeping %d ids",
            len(_lols_hot_set),
        )
        return len(_lols_hot_set)
    _lols_hot_set = ids
    _lols_hot_at = datetime.now(timezone.utc)
    logger.info("LOLS hot banlist refreshed: %d user_ids", len(ids))
    return len(ids)


async def refresh_lols_full() -> int:
    """Полный банлист (Tier C, 43 МБ) → _lols_full_set. Только ночью.

    Потенциальные скамеры: юзер в этом списке, но не в verified/hot и не
    подтверждён CAS — помечается (cas_verdicts, is_banned=False), НЕ банится.
    """
    global _lols_full_set, _lols_full_at
    ids = await _fetch_lols_ids(_LOLS_FULL_URL, "full")
    if ids:
        _lols_full_set = ids
        _lols_full_at = datetime.now(timezone.utc)
    logger.info("LOLS full banlist refreshed: %d user_ids", len(ids))
    return len(ids)


def _release_lols_full() -> None:
    """Освобождает полный банлист после ночного свипа.

    4.1 млн id — это ~134 МБ резидентной памяти в контейнере, который
    делит процесс с ботом и веб-панелью. Список нужен только внутри
    ночного окна (пометка потенциальных), днём в памяти остаются лишь
    verified + hot — как и заявлено в шапке модуля.
    """
    global _lols_full_set, _lols_full_at
    _lols_full_set = set()
    _lols_full_at = None


def _lols_is_confirmed(user_id: int) -> bool:
    """Подтверждённый LOLS-спамер: verified (Tier A) или бан за последний час (Tier B)."""
    return user_id in _lols_set or user_id in _lols_hot_set


async def _cas_thresholds() -> tuple[float, float, int]:
    """Пороги каскада из cas_settings (правятся в панели). Дефолты 60/30/10."""
    async with async_session() as session:
        row = (await session.execute(
            select(CasSettings).where(CasSettings.id == 1)
        )).scalar_one_or_none()
    if row is None:
        return 60.0, 30.0, 10
    return (float(row.spamfactor_ban), float(row.spamfactor_mute),
            int(row.offenses_mute))


async def _lols_account(user_id: int) -> dict:
    """GET api.lols.bot/account?id= → метрики юзера.

    Возвращает dict с banned/offenses/spam_factor/scammer или {} при сбое
    (fail-open: потенциальный без метрик уйдёт в C3_watch).
    """
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=8),
        ) as http:
            async with http.get(
                f"https://api.lols.bot/account?id={user_id}"
            ) as resp:
                if resp.status != 200:
                    logger.warning(
                        "LOLS account probe %s: HTTP %s", user_id, resp.status,
                    )
                    return {}
                data = await resp.json(content_type=None)
                return data if isinstance(data, dict) else {}
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
        logger.warning("LOLS account probe %s failed: %s", user_id, e)
        return {}


def _lols_tier(acc: dict, sf_ban: float, sf_mute: float,
               off_mute: int) -> tuple[str, str]:
    """Тир по метрикам LOLS: (C1_ban | C2_mute | C3_watch | clean, detail).

    Санкций здесь НЕТ: потенциальных не баним и не мьютим (решение
    владельца 30.08.2026) — тир это метка для «На карандаше» панели,
    где модератор решает вручную.
    """
    if not acc.get("banned"):
        return "clean", ""
    try:
        sf_val = float(acc.get("spam_factor") or 0.0)
    except (TypeError, ValueError):
        sf_val = 0.0
    try:
        off_val = int(acc.get("offenses") or 0)
    except (TypeError, ValueError):
        off_val = 0
    scammer = bool(acc.get("scammer"))
    detail = f"spam_factor={sf_val:g}, offenses={off_val}, scammer={scammer}"
    if scammer or sf_val >= sf_ban:
        return "C1_ban", detail
    if sf_val >= sf_mute or off_val >= off_mute:
        return "C2_mute", detail
    return "C3_watch", detail


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
                         is_banned: bool, reason: str | None,
                         *, spam_factor: float | None = None,
                         offenses: int | None = None,
                         scammer: bool | None = None,
                         tier: str | None = None) -> None:
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
                    spam_factor=spam_factor, offenses=offenses,
                    scammer=scammer, tier=tier,
                ))
            else:
                row.checked_at = datetime.now(timezone.utc)
                row.source = source
                row.is_banned = is_banned
                row.reason = reason
                row.spam_factor = spam_factor
                row.offenses = offenses
                row.scammer = scammer
                row.tier = tier
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

    Пропускает: cas_ignore, свежий кэш (≤30 дней — юзер уже известен;
    вердикт «чист» не отменяет проверку по LOLS-тирам, см. ниже),
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
                   "banned": 0, "marked": 0, "users": []}
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
        # v5.3.2 fix: chat_admins покрывает только синкнутых модеров этого
        # чата — su/admin веб-панели (CLAUDE.md: WebUser.role действует
        # во всех чатах) тоже exempt, даже если не привязаны к chat_admins.
        web_admin_ids = set((await session.execute(
            select(WebUser.tg_user_id).where(
                WebUser.role.in_(("su", "admin")),
                WebUser.is_active.is_(True),
                WebUser.tg_user_id.is_not(None),
            )
        )).scalars().all())

    sf_ban, sf_mute, off_mute = await _cas_thresholds()

    for user_id in user_ids:
        # Свои — exempt (v4.7.30): админы чата, глобальные SU/admin, боты.
        if (user_id in chat_admin_ids or user_id in ADMIN_IDS
                or user_id in web_admin_ids):
            continue
        if await _is_ignored(user_id):
            continue

        # Кэш вердиктов существует ради экономии per-id запросов к CAS.
        # v5.4.0 FIX: он больше не отменяет бесплатную (set в памяти)
        # проверку по подтверждённым LOLS-тирам. Со старым порядком
        # «кэш-первый» Tier B не работал для сидящих в принципе: у них с
        # прошлого свипа лежит вердикт «чист» на 30 дней, и юзер
        # пропускался целиком, ни разу не сверившись с горячим списком —
        # то есть попасть в banlist-1h успевали только те, кого бот вообще
        # ещё не проверял. Свежий бан-вердикт по-прежнему прекращает
        # разбор: второй раз того же юзера не банимся.
        fresh = await _cached_verdict(user_id)
        if fresh is not None and fresh[0]:
            continue

        # 1) Подтверждённые LOLS-тиры (v5.4.0): verified scammer (Tier A)
        #    или бан LOLS за последний час (Tier B) — CAS не проверяем.
        confirmed = _lols_is_confirmed(user_id)
        if fresh is not None and not confirmed:
            continue

        if confirmed:
            if user_id in _lols_set:
                source, reason, tier = "lols", "verified scammer (LOLS)", "A_verified"
            else:
                source, reason, tier = "lols", "banned by LOLS in the last hour", "B_hot"
            is_banned = True
            await _store_verdict(user_id, source, is_banned, reason, tier=tier)
        else:
            # 2) CAS — подтверждающий фактор (в т.ч. для потенциальных:
            #    CAS banned = подтверждение → бан).
            is_banned, reason = await _cas_check_user(user_id)
            source = "cas"
            stats["checked"] += 1
            await asyncio.sleep(1.0 / _CAS_RPS)
            # 3) Потенциальный скамер (v5.5.0): в полном банлисте LOLS, но
            #    не подтверждён никем — каскад /account ставит ТИР, а не
            #    санкцию: потенциальных не баним и не мьютим (решение
            #    владельца 30.08.2026). Тир уходит в cas_verdicts и в
            #    «На карандаше» панели.
            potential = False
            acc: dict = {}
            if not is_banned and user_id in _lols_full_set:
                potential = True
                acc = await _lols_account(user_id)
                tier, detail = _lols_tier(acc, sf_ban, sf_mute, off_mute)
                source = "lols"
                reason = f"potential ({tier}): {detail}"
                stats["marked"] += 1
            await _store_verdict(
                user_id, source, is_banned, reason,
                spam_factor=(float(acc.get("spam_factor") or 0.0)
                             if potential else None),
                offenses=(int(acc.get("offenses") or 0)
                          if potential else None),
                scammer=(bool(acc.get("scammer"))
                         if potential else None),
                tier=tier if potential else None,
            )

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
    await refresh_lols_full()
    try:
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
    finally:
        _release_lols_full()


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
    marked = _day_stats.get("marked", 0)
    ids = _day_stats.get("ids", [])
    if banned:
        shown_ids = ids[:5]
        shown = ", ".join(str(u) for u in shown_ids)
        # v5.3.2 fix: сравнивали banned с длиной ПОЛНОГО ids (а не
        # отображённых 5) — banned == len(ids) всегда (по одному id на
        # бан в _accumulate), поэтому "+N ещё" не показывался никогда.
        more = (f" (+{banned - len(shown_ids)})"
                if banned > len(shown_ids) else "")
        tail = f" (id: {shown}{more})"
    else:
        tail = ""
    marked_part = f", на карандаше {marked}" if marked else ""
    return (f"🛡️ CAS/LOLS за сутки: проверено {checked}, "
            f"забанено {banned}{tail}{marked_part}")


def _accumulate(stats: dict) -> None:
    _day_stats["checked"] += stats.get("checked", 0)
    _day_stats["banned"] += stats.get("banned", 0)
    _day_stats["marked"] += stats.get("marked", 0)
    _day_stats["ids"].extend(stats.get("users", []))


def _reset_day_stats() -> None:
    _day_stats["checked"] = 0
    _day_stats["banned"] = 0
    _day_stats["marked"] = 0
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

            # Tier B: горячий банлист LOLS каждый час (~10 КБ) — дневное
            # покрытие без per-id запросов (решение владельца 30.08.2026).
            if _cas_enabled_chat_ids and (
                _lols_hot_at is None
                or (datetime.now(timezone.utc) - _lols_hot_at)
                >= timedelta(minutes=55)
            ):
                await refresh_lols_hot()

            # Ночной свип: 01:00–01:59 МСК, раз в сутки.
            if now.hour == 1 and _last_nightly_date != today:
                _last_nightly_date = today
                logger.info(
                    "CAS nightly sweep started (chats: %d)",
                    len(_cas_enabled_chat_ids),
                )
                await _nightly_sweep_all(bot)

            # Дайджест: 05:00+ МСК, раз в сутки. v5.3.2 fix: `now` для этой
            # проверки пересчитан отдельно, а условие — `>=`, не `==`.
            # Ночной свип при большом бэклоге (CAS ≤5 rps) может занять
            # часы; со старым `now.hour == 5`, взятым в начале тика,
            # свип, доехавший до хвоста после 05:59, застревал бы на
            # `now.hour` вроде 1 в этом тике, а следующий тик мог уже
            # увидеть час 6+ — и дайджест не отправлялся бы весь день.
            # `today` — день начала цикла (не день, в который случайно
            # доехал свип), чтобы дайджест не задвоился при переходе
            # через полночь.
            if _now_msk().hour >= 5 and _last_digest_date != today:
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
# v5.3.2 fix: `.message(...)` — декоратор регистрации хендлера, а не
# middleware; без .outer_middleware(...) объект просто отбрасывался, и
# chat_members_seen никогда не наполнялся.
from bot_handlers import router as _bh_router

_bh_router.message.outer_middleware(MembersSeenMiddleware())
