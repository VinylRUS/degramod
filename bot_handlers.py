"""
bot_handlers.py — Обработка модераторских команд: !mute, !warn, !ban, !unmute.
Бот удаляет сообщение с командой, снимает слепок пермишенов и логирует санкцию.
"""

from __future__ import annotations

import json
import os
import re
import logging
from datetime import datetime, timezone

from aiogram import Router, types
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import select, desc

from db import async_session, User, Moderator, Punishment

logger = logging.getLogger("shadow_logger.bot_handlers")

router = Router()

# ── ADMIN_IDS из окружения ─────────────────────────────────────────────────
_raw_admins = os.getenv("ADMIN_IDS", "")
ADMIN_IDS: set[int] = {int(x.strip()) for x in _raw_admins.split(",") if x.strip()}

# ── Парсинг длительности: 1d, 2h, 30m, 1d12h ──────────────────────────────
_DURATION_RE = re.compile(
    r"(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?", re.IGNORECASE
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


# ── Команды ─────────────────────────────────────────────────────────────────
_CMD_MUTE = re.compile(r"^!mute\s+(\S+)\s+(.+)$", re.IGNORECASE)
_CMD_WARN = re.compile(r"^!warn\s+(\d+)\s+(.+)$", re.IGNORECASE)
_CMD_BAN = re.compile(r"^!ban\s+(.+)$", re.IGNORECASE)
_CMD_UNMUTE = re.compile(r"^!unmute\s*$", re.IGNORECASE)

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


# ── Helpers ─────────────────────────────────────────────────────────────────
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
                           permissions_snapshot: str | None = None) -> None:
    punishment = Punishment(
        user_id=user_id,
        mod_id=mod_id,
        chat_id=chat_id,
        action_type=action_type,
        duration_seconds=duration_seconds,
        reason=reason,
        message_text=message_text,
        permissions_snapshot=permissions_snapshot,
    )
    session.add(punishment)
    await session.commit()


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


# ── Главный обработчик ─────────────────────────────────────────────────────
@router.message()
async def handle_mod_command(message: types.Message) -> None:
    # Только reply-сообщения от ADMIN_IDS
    if message.reply_to_message is None:
        return
    if message.from_user.id not in ADMIN_IDS:
        return

    text = message.text
    if not text:
        return

    target: types.User = message.reply_to_message.from_user
    chat_id = message.chat.id
    mod = message.from_user
    target_msg_text = message.reply_to_message.text or message.reply_to_message.caption

    # Удаляем сообщение модератора с командой (независимо от исхода парсинга)
    try:
        await message.delete()
    except TelegramBadRequest:
        logger.warning("Не удалось удалить сообщение модератора %s в чате %s",
                       mod.id, chat_id)

    # ── !mute ───────────────────────────────────────────────────────────
    m = _CMD_MUTE.match(text)
    if m:
        dur_str, reason = m.group(1), m.group(2)
        duration_seconds = _parse_duration(dur_str)
        if duration_seconds is None:
            return

        # Снимаем слепок пермишенов ДО мьюта
        perm_snapshot = None
        try:
            member = await message.bot.get_chat_member(chat_id=chat_id, user_id=target.id)
            perm_snapshot = _snapshot_permissions(member)
        except TelegramBadRequest as e:
            logger.warning("get_chat_member before mute failed: %s", e)

        # Restrict пользователя
        until_date = int(datetime.now(timezone.utc).timestamp()) + duration_seconds
        try:
            await message.bot.restrict_chat_member(
                chat_id=chat_id,
                user_id=target.id,
                can_send_messages=False,
                until_date=until_date,
            )
        except TelegramBadRequest as e:
            logger.error("restrict_chat_member failed: %s", e)
            return
        async with async_session() as session:
            await _upsert_user(session, target.id, target.username,
                               target.first_name, target.last_name)
            await _upsert_moderator(session, mod.id, mod.username, mod.first_name)
            await _save_punishment(
                session, target.id, mod.id, chat_id,
                "mute", duration_seconds, reason, target_msg_text,
                permissions_snapshot=perm_snapshot,
            )
        return

    # ── !warn ───────────────────────────────────────────────────────────
    m = _CMD_WARN.match(text)
    if m:
        points, reason = int(m.group(1)), m.group(2)
        async with async_session() as session:
            await _upsert_user(session, target.id, target.username,
                               target.first_name, target.last_name)
            await _upsert_moderator(session, mod.id, mod.username, mod.first_name)
            await _save_punishment(
                session, target.id, mod.id, chat_id,
                "warn", points, reason, target_msg_text,
            )
        return

    # ── !ban ────────────────────────────────────────────────────────────
    m = _CMD_BAN.match(text)
    if m:
        reason = m.group(1)

        # Снимаем слепок пермишенов ДО бана
        perm_snapshot = None
        try:
            member = await message.bot.get_chat_member(chat_id=chat_id, user_id=target.id)
            perm_snapshot = _snapshot_permissions(member)
        except TelegramBadRequest as e:
            logger.warning("get_chat_member before ban failed: %s", e)

        try:
            await message.bot.ban_chat_member(chat_id=chat_id, user_id=target.id)
        except TelegramBadRequest as e:
            logger.error("ban_chat_member failed: %s", e)
            return
        async with async_session() as session:
            await _upsert_user(session, target.id, target.username,
                               target.first_name, target.last_name)
            await _upsert_moderator(session, mod.id, mod.username, mod.first_name)
            await _save_punishment(
                session, target.id, mod.id, chat_id,
                "ban", None, reason, target_msg_text,
                permissions_snapshot=perm_snapshot,
            )
        return

    # ── !unmute ─────────────────────────────────────────────────────────
    if _CMD_UNMUTE.match(text):
        # Получаем снапшот пермишенов из последнего mute/ban
        restored = False
        async with async_session() as session:
            snapshot_json = await _fetch_last_snapshot(session, target.id, chat_id)

        if snapshot_json:
            try:
                perms = _restore_permissions(snapshot_json)
                await message.bot.restrict_chat_member(
                    chat_id=chat_id,
                    user_id=target.id,
                    permissions=perms,
                )
                restored = True
            except (TelegramBadRequest, json.JSONDecodeError) as e:
                logger.warning("Restore from snapshot failed, falling back to defaults: %s", e)

        # Fallback: выдать все пермишены, если снапшота нет или он повреждён
        if not restored:
            try:
                await message.bot.restrict_chat_member(
                    chat_id=chat_id,
                    user_id=target.id,
                    permissions=types.ChatPermissions(
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
                    ),
                )
            except TelegramBadRequest as e:
                logger.error("unmute restrict failed: %s", e)
                return

        async with async_session() as session:
            await _upsert_user(session, target.id, target.username,
                               target.first_name, target.last_name)
            await _upsert_moderator(session, mod.id, mod.username, mod.first_name)
            await _save_punishment(
                session, target.id, mod.id, chat_id,
                "unmute", None, None, target_msg_text,
            )
        return
