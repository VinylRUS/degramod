"""
mod_commands.py — v4.8.9: декомпозиция handle_group_command.

Контекст: в bot_handlers.py была одна функция handle_group_command() на 1121
строку, обрабатывающая 12 мод-команд (!ban, !sban, !mute, !smute, !warn,
!swarn, !unmute, !unban, !unwarn, !warns, !resetwarns, !resetmc). Это
невозможно тестировать изолированно, любой багфикс — редактирование 1000+
строк.

v4.8.9: создан mod_commands.py с паттерном "диспетчер + отдельные cmd_X
функции + ModContext dataclass для общего state". Пока перенесена только
cmd_ban как proof-of-concept (см. CHANGES_v4.8.9.md). Остальные 11 команд
остаются в handle_group_command — перенос отложен на v4.9.0.

Архитектура:
  • ModContext — dataclass с общими переменными (chat_id, mod, target,
    reason, target_content, text). Заполняется dispatcher'ом в шапке
    handle_group_command, передаётся в cmd_X.
  • cmd_ban(message, ctx) — вынесенная функция для !ban.
  • COMMANDS — dict[str, Callable], маппинг имени команды → функция.
    Пока содержит только "ban". По мере переноса других команд —
    заполняется.

Приёмка (см. 03_TASK_v4.8.9.md §1):
  - 1121-строчная функция → ~50-строчный dispatcher + cmd_ban (~130 строк).
  - 1/12 функций перенесена (cmd_ban). Остальные — TODO v4.9.0.
  - Все 17 call sites tg_safe_call сохранились (cmd_ban использует tg_safe_call).
  - Регрессия v4.8.7 + v4.8.8 проходит.
"""
from __future__ import annotations

import asyncio
import html
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from aiogram import types
from aiogram.exceptions import TelegramAPIError
from sqlalchemy import select

from bot_handlers import (
    _CMD_BAN,
    _CMD_MUTE,
    _CMD_SBAN,
    _CMD_SMUTE,
    _CMD_SWARN,
    _CMD_UNWARN,
    _CMD_WARN,
    _EPHEMERAL_DELETE_SEM,
    ADMIN_IDS,
    _add_banned_sticker_pack,
    _check_warn_threshold,
    _count_warns,
    _format_duration,
    _get_chat_settings,
    _get_web_user_role,
    _mark_bot_ban,
    _mute_permissions,
    _parse_duration,
    _reset_automute_count,
    _revoke_last_action,
    _revoke_last_warns,
    _save_punishment,
    _send_audit_to_report,
    _send_ephemeral,
    _send_public_punishment_notice,
    _send_report,
    _send_user_warn_notification,
    _snapshot_permissions,
    _spawn_background_task,
    _upsert_moderator,
    _upsert_user,
    _user_mention_html,
    revoke_user_ban,
    tg_safe_call,
)
from db import BannedStickerPack, Punishment, async_session

if TYPE_CHECKING:
    pass

logger = logging.getLogger("shadow_logger")


# ── ModContext ──────────────────────────────────────────────────────────────


@dataclass
class ModContext:
    """Общий state для всех мод-команд.

    Заполняется dispatcher'ом (handle_group_command) в шапке после парсинга
    команды и резолва target. Передаётся в cmd_X функции.

    Поля:
      chat_id: ID чата, где идёт модерация.
      mod: User — модератор, вызвавший команду (message.from_user).
      target: User — цель наказания (резолвится из reply/@username/TGID).
      target_content: содержимое сообщения нарушителя (для _save_punishment).
      text: полный текст команды (message.text or message.caption).
    """

    chat_id: int
    mod: types.User
    target: types.User
    target_content: str | None
    text: str


# ── cmd_ban ─────────────────────────────────────────────────────────────────


async def cmd_ban(message: types.Message, ctx: ModContext) -> None:
    """!ban [@username|TGID] [reason] — публичный бан нарушителя.

    Перенесена из handle_group_command (bot_handlers.py:4411-4535) в v4.8.9
    как proof-of-concept декомпозиции. См. mod_commands.py docstring.

    Логика:
      1. Снимает snapshot прав нарушителя (для возможного restore при unban).
      2. Банит через tg_safe_call (ретраит при 429/RetryAfter).
      3. Помечает бан как bot-initiated (_mark_bot_ban) для дедупликации
         в ChatMemberUpdated handler.
      4. Если забанили за стикер — автодобавляет пак в BannedStickerPack.
      5. Отправляет отчёт в modchat (_send_report).
      6. Сохраняет наказание в БД (_save_punishment).
      7. Публичное сообщение в чат (_send_public_punishment_notice).
      8. Удаляет сообщение нарушителя (если был reply).
    """
    m = _CMD_BAN.match(ctx.text)
    if m is None:
        # Не !ban — не должно происходить, dispatcher вызвал не ту функцию.
        logger.warning("cmd_ban called with non-ban command: %s", ctx.text[:50])
        return
    reason = m.group("reason")

    chat_id = ctx.chat_id
    mod = ctx.mod
    target = ctx.target
    target_content = ctx.target_content

    # ── Snapshot прав (для restore при unban) ──────────────────────────
    perm_snapshot = None
    try:
        member = await message.bot.get_chat_member(chat_id=chat_id, user_id=target.id)
        perm_snapshot = _snapshot_permissions(member)
    except TelegramAPIError as e:
        logger.warning("get_chat_member before ban failed: %s", e)

    # ── Бан через tg_safe_call (ретраит при 429) ───────────────────────
    try:
        await tg_safe_call(
            lambda: message.bot.ban_chat_member(chat_id=chat_id, user_id=target.id),
            label="!ban",
        )
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

    # v4.7.27: помечаем что бан выполнил сам бот — чтобы ChatMemberUpdated
    # handler не отправил второй отчёт о «ручном бане» (дедупликация).
    _mark_bot_ban(chat_id, target.id)

    # ── Автодобавление стикерпака если бан за стикер ───────────────────
    # v4.5.2: если забанили за стикер — автоматически добавляем пак в
    # BannedStickerPack (per-chat, punishment=ban — чтобы следующий юзер
    # с этим же паком тоже был забанен автоматически).
    # v4.8.3: отслеживаем был ли стикерпак newly_added — для sticker_pack_info.
    sticker = getattr(message.reply_to_message, "sticker", None) if message.reply_to_message else None
    sticker_pack_info: tuple[str, bool] | None = None
    if sticker and sticker.set_name:
        try:
            # Проверяем — был ли стикерпак уже в бан-листе ДО добавления.
            async with async_session() as session:
                existing_pack = (
                    await session.execute(
                        select(BannedStickerPack).where(
                            BannedStickerPack.chat_id == chat_id,
                            BannedStickerPack.pack_name == sticker.set_name,
                            BannedStickerPack.is_active.is_(True),
                        )
                    )
                ).scalar_one_or_none()
            was_newly_added = (existing_pack is None)

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
            sticker_pack_info = (sticker.set_name, was_newly_added)
        except Exception as e:
            logger.warning(
                "auto-add sticker pack '%s' failed: %s", sticker.set_name, e
            )

    # ── Отчёт в modchat ────────────────────────────────────────────────
    await _send_report(
        bot=message.bot, chat_id=chat_id, target=target,
        action_type="ban", reason=reason, mod=mod,
        reply_to_message=message.reply_to_message,
        sticker_pack_info=sticker_pack_info,
        moderator_screenshot=message if message.photo else None,
    )

    # ── Сохранение наказания в БД ──────────────────────────────────────
    async with async_session() as session:
        await _upsert_user(session, target.id, target.username,
                           target.first_name, target.last_name)
        await _upsert_moderator(session, mod.id, mod.username, mod.first_name)
        await _save_punishment(
            session, target.id, mod.id, chat_id,
            "ban", None, reason, target_content,
            permissions_snapshot=perm_snapshot,
        )

    # ── Публичное сообщение в чат (v4.8.1) ────────────────────────────
    await _send_public_punishment_notice(
        bot=message.bot, chat_id=chat_id, target=target,
        action="ban", reason=reason,
    )

    # ── Удаление сообщения нарушителя ──────────────────────────────────
    # v4.7.15: удаляем ПОСЛЕ всех операций.
    # v4.8.3: если reply нет (бан по @username/TGID) — пропускаем.
    if message.reply_to_message is not None:
        try:
            await message.reply_to_message.delete()
        except TelegramAPIError as e:
            logger.warning(
                "Не удалось удалить сообщение нарушителя %s в чате %s: %s",
                target.id, chat_id, e,
            )


# ── cmd_mute ────────────────────────────────────────────────────────────────


async def cmd_mute(message: types.Message, ctx: ModContext) -> None:
    """!mute <dur> [reason] — публичный мьют нарушителя.

    Перенесена из handle_group_command (bot_handlers.py:4091-4175) в v4.8.10.
    """
    m = _CMD_MUTE.match(ctx.text)
    if m is None:
        return
    dur_str = m.group("dur")
    reason = m.group("reason")
    chat_id = ctx.chat_id
    mod = ctx.mod
    target = ctx.target
    target_content = ctx.target_content

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
        await tg_safe_call(
            lambda: message.bot.restrict_chat_member(
                chat_id=chat_id, user_id=target.id,
                permissions=_mute_permissions(),
                until_date=until_date,
            ),
            label="!mute",
        )
    except TelegramAPIError as e:
        logger.error("restrict_chat_member failed: %s", e)
        try:
            await message.bot.send_message(chat_id=mod.id, text=f"❌ Мут не удался: {e}")
        except TelegramAPIError:
            pass
        return

    await _send_report(
        bot=message.bot, chat_id=chat_id, target=target,
        action_type="mute", reason=reason, mod=mod,
        duration_seconds=duration_seconds,
        reply_to_message=message.reply_to_message,
        moderator_screenshot=message if message.photo else None,
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

    await _send_public_punishment_notice(
        bot=message.bot, chat_id=chat_id, target=target,
        action="mute", reason=reason, duration=duration_seconds,
    )

    if message.reply_to_message is not None:
        try:
            await message.reply_to_message.delete()
        except TelegramAPIError as e:
            logger.warning("Не удалось удалить сообщение нарушителя %s в чате %s: %s",
                           target.id, chat_id, e)


# ── cmd_smute ───────────────────────────────────────────────────────────────


async def cmd_smute(message: types.Message, ctx: ModContext) -> None:
    """!smute [dur] [reason] — тихий мьют (stealth).

    Перенесена из handle_group_command (bot_handlers.py:4184-4289) в v4.8.10.
    """
    m = _CMD_SMUTE.match(ctx.text)
    if m is None:
        return
    dur_str = m.group("dur")
    chat_id = ctx.chat_id
    mod = ctx.mod
    target = ctx.target
    target_content = ctx.target_content

    if dur_str is None:
        try:
            await _send_ephemeral(
                bot=message.bot, chat_id=chat_id, recipient=mod,
                text=(
                    "❌ Не указана длительность мьюта.\n"
                    "💡 Формат: <code>!smute 1d [причина]</code>, "
                    "<code>!smute @user 2h Причина</code>, "
                    "<code>!smute 30m</code>.\n"
                    "Поддерживаемые единицы: <b>м/m</b> (минуты), "
                    "<b>ч/h</b> (часы), <b>д/d</b> (дни). "
                    "Комбинированные: <code>1d12h30m</code>."
                ),
            )
        except Exception:
            pass
        return
    reason = m.group("reason") or "(без причины)"
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
        logger.warning("get_chat_member before smute failed: %s", e)

    until_date = int(datetime.now(timezone.utc).timestamp()) + duration_seconds
    try:
        await tg_safe_call(
            lambda: message.bot.restrict_chat_member(
                chat_id=chat_id, user_id=target.id,
                permissions=_mute_permissions(),
                until_date=until_date,
            ),
            label="!smute",
        )
    except TelegramAPIError as e:
        logger.error("restrict_chat_member failed (smute): %s", e)
        try:
            await message.bot.send_message(chat_id=mod.id, text=f"❌ Мут не удался: {e}")
        except TelegramAPIError:
            pass
        return

    await _send_report(
        bot=message.bot, chat_id=chat_id, target=target,
        action_type="mute", reason=reason, mod=mod,
        duration_seconds=duration_seconds,
        reply_to_message=message.reply_to_message,
        moderator_screenshot=message if message.photo else None,
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

    if message.reply_to_message is not None:
        try:
            await message.reply_to_message.delete()
        except TelegramAPIError as e:
            logger.warning("Не удалось удалить сообщение нарушителя %s в чате %s: %s",
                           target.id, chat_id, e)


# ── cmd_warn ────────────────────────────────────────────────────────────────


async def cmd_warn(message: types.Message, ctx: ModContext) -> None:
    """!warn [reason] — публичный варн.

    Перенесена из handle_group_command (bot_handlers.py:4295-4343) в v4.8.10.
    """
    m = _CMD_WARN.match(ctx.text)
    if m is None:
        return
    reason = m.group("reason")
    chat_id = ctx.chat_id
    mod = ctx.mod
    target = ctx.target
    target_content = ctx.target_content

    async with async_session() as session:
        await _upsert_user(session, target.id, target.username,
                           target.first_name, target.last_name)
        await _upsert_moderator(session, mod.id, mod.username, mod.first_name)
        await _save_punishment(
            session, target.id, mod.id, chat_id,
            "warn", 1, reason, target_content,
        )

    await _send_report(
        bot=message.bot, chat_id=chat_id, target=target,
        action_type="warn", reason=reason, warn_points=1, mod=mod,
        reply_to_message=message.reply_to_message,
        moderator_screenshot=message if message.photo else None,
    )

    await _send_public_punishment_notice(
        bot=message.bot, chat_id=chat_id, target=target,
        action="warn", reason=reason,
    )

    await _check_warn_threshold(
        bot=message.bot, chat_id=chat_id,
        target=target, mod=mod,
    )

    if message.reply_to_message is not None:
        try:
            await message.reply_to_message.delete()
        except TelegramAPIError as e:
            logger.warning("Не удалось удалить сообщение нарушителя %s в чате %s: %s",
                           target.id, chat_id, e)


# ── cmd_swarn ───────────────────────────────────────────────────────────────


async def cmd_swarn(message: types.Message, ctx: ModContext) -> None:
    """!swarn [reason] — тихий варн + уведомление нарушителю.

    Перенесена из handle_group_command (bot_handlers.py:4350-4404) в v4.8.10.
    """
    m = _CMD_SWARN.match(ctx.text)
    if m is None:
        return
    reason = m.group("reason") or "(без причины)"
    chat_id = ctx.chat_id
    mod = ctx.mod
    target = ctx.target
    target_content = ctx.target_content

    async with async_session() as session:
        await _upsert_user(session, target.id, target.username,
                           target.first_name, target.last_name)
        await _upsert_moderator(session, mod.id, mod.username, mod.first_name)
        await _save_punishment(
            session, target.id, mod.id, chat_id,
            "warn", 1, reason, target_content,
        )
        total_warns_now = await _count_warns(session, target.id, chat_id)
        chat_settings = await _get_chat_settings(session, chat_id)

    await _send_report(
        bot=message.bot, chat_id=chat_id, target=target,
        action_type="warn", reason=reason, warn_points=1, mod=mod,
        reply_to_message=message.reply_to_message,
        moderator_screenshot=message if message.photo else None,
    )

    await _send_user_warn_notification(
        bot=message.bot, chat_id=chat_id, target=target,
        reason=reason, total_warns=total_warns_now,
        settings=chat_settings,
    )

    reason_safe = html.escape(reason, quote=False) if reason and reason != "(без причины)" else ""
    await _send_ephemeral(
        bot=message.bot, chat_id=chat_id, recipient=mod,
        text=(
            f"✅ Варн выдан {_user_mention_html(target)}"
            + (f" за: {reason_safe}" if reason_safe else "")
            + f". Варнов всего: {total_warns_now}"
        ),
    )

    await _check_warn_threshold(
        bot=message.bot, chat_id=chat_id,
        target=target, mod=mod,
    )

    if message.reply_to_message is not None:
        try:
            await message.reply_to_message.delete()
        except TelegramAPIError as e:
            logger.warning("Не удалось удалить сообщение нарушителя %s в чате %s: %s",
                           target.id, chat_id, e)


# ── cmd_sban ────────────────────────────────────────────────────────────────


async def cmd_sban(message: types.Message, ctx: ModContext) -> None:
    """!sban [reason] — тихий бан (stealth).

    Перенесена из handle_group_command (bot_handlers.py:4431-4547) в v4.8.10.
    """
    m = _CMD_SBAN.match(ctx.text)
    if m is None:
        return
    reason = m.group("reason") or "(без причины)"
    chat_id = ctx.chat_id
    mod = ctx.mod
    target = ctx.target
    target_content = ctx.target_content

    perm_snapshot = None
    try:
        member = await message.bot.get_chat_member(chat_id=chat_id, user_id=target.id)
        perm_snapshot = _snapshot_permissions(member)
    except TelegramAPIError as e:
        logger.warning("get_chat_member before sban failed: %s", e)

    try:
        await tg_safe_call(
            lambda: message.bot.ban_chat_member(chat_id=chat_id, user_id=target.id),
            label="!sban",
        )
    except TelegramAPIError as e:
        logger.error("ban_chat_member failed (sban): %s", e)
        try:
            await message.bot.send_message(chat_id=mod.id, text=f"❌ Бан не удался: {e}")
        except TelegramAPIError:
            pass
        return

    _mark_bot_ban(chat_id, target.id)

    sticker = getattr(message.reply_to_message, "sticker", None) if message.reply_to_message else None
    sticker_pack_info: tuple[str, bool] | None = None
    if sticker and sticker.set_name:
        try:
            async with async_session() as session:
                existing_pack = (
                    await session.execute(
                        select(BannedStickerPack).where(
                            BannedStickerPack.chat_id == chat_id,
                            BannedStickerPack.pack_name == sticker.set_name,
                            BannedStickerPack.is_active.is_(True),
                        )
                    )
                ).scalar_one_or_none()
            was_newly_added = (existing_pack is None)

            async with async_session() as session:
                await _add_banned_sticker_pack(
                    session,
                    chat_id=chat_id,
                    pack_name=sticker.set_name,
                    punishment="ban",
                    reason=f"Auto-added via !sban by mod {mod.id}: {reason}",
                    added_by_mod_id=mod.id,
                    added_via="auto_ban",
                )
            logger.info(
                "v4.5.2 auto-banned sticker pack '%s' in chat %s (via !sban by mod %s)",
                sticker.set_name, chat_id, mod.id,
            )
            await _send_ephemeral(
                bot=message.bot, chat_id=chat_id, recipient=mod,
                text=(
                    f"🎭 Стикерпак <code>{sticker.set_name}</code> "
                    f"автодобавлен в бан-лист (punishment=ban)."
                ),
            )
            sticker_pack_info = (sticker.set_name, was_newly_added)
        except Exception as e:
            logger.warning(
                "auto-add sticker pack '%s' failed (sban): %s", sticker.set_name, e
            )

    await _send_report(
        bot=message.bot, chat_id=chat_id, target=target,
        action_type="ban", reason=reason, mod=mod,
        reply_to_message=message.reply_to_message,
        sticker_pack_info=sticker_pack_info,
        moderator_screenshot=message if message.photo else None,
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

    reason_safe = html.escape(reason, quote=False) if reason and reason != "(без причины)" else ""
    await _send_ephemeral(
        bot=message.bot, chat_id=chat_id, recipient=mod,
        text=(
            f"✅ Забанен {_user_mention_html(target)}"
            + (f" за: {reason_safe}" if reason_safe else "")
            + "."
        ),
    )

    if message.reply_to_message is not None:
        try:
            await message.reply_to_message.delete()
        except TelegramAPIError as e:
            logger.warning("Не удалось удалить сообщение нарушителя %s в чате %s: %s",
                           target.id, chat_id, e)


# ── cmd_unmute ──────────────────────────────────────────────────────────────


async def cmd_unmute(message: types.Message, ctx: ModContext) -> None:
    """!unmute — снимает мьют, восстанавливая дефолтные права чата.

    Перенесена из handle_group_command (bot_handlers.py:4553-4637) в v4.8.10.
    """
    chat_id = ctx.chat_id
    mod = ctx.mod
    target = ctx.target
    target_content = ctx.target_content

    try:
        chat_info = await message.bot.get_chat(chat_id=chat_id)
        chat_perms = chat_info.permissions
        if chat_perms is None:
            chat_perms = types.ChatPermissions(can_send_messages=True)
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
        )
    except TelegramAPIError as e:
        logger.error("get_chat for unmute failed: %s", e)
        try:
            await message.bot.send_message(chat_id=mod.id, text=f"❌ Размут не удался (get_chat): {e}")
        except TelegramAPIError:
            pass
        return

    try:
        await tg_safe_call(
            lambda: message.bot.restrict_chat_member(
                chat_id=chat_id, user_id=target.id,
                permissions=chat_perms,
            ),
            label="!unmute",
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
        await _revoke_last_action(
            session, target.id, chat_id, "mute", revoked_by_mod_id=mod.id,
        )
        await _save_punishment(
            session, target.id, mod.id, chat_id,
            "unmute", None, None, target_content,
        )

    await _send_ephemeral(
        bot=message.bot, chat_id=chat_id, recipient=mod,
        text=f"✅ Размьючен {_user_mention_html(target)}.",
    )

    await _send_audit_to_report(
        bot=message.bot, chat_id=chat_id, mod=mod, target=target,
        action_label="мьют", detail="команда !unmute",
    )


# ── cmd_unban ───────────────────────────────────────────────────────────────


async def cmd_unban(message: types.Message, ctx: ModContext) -> None:
    """!unban — снимает бан (через revoke_user_ban, паритет с веб-панелью).

    Перенесена из handle_group_command (bot_handlers.py:4643-4680) в v4.8.10.
    """
    chat_id = ctx.chat_id
    mod = ctx.mod
    target = ctx.target

    result = await revoke_user_ban(
        bot=message.bot, chat_id=chat_id, user_id=target.id,
        mod_id=mod.id, reason=None, target_user=target,
    )
    if not result.get("ok"):
        try:
            await message.bot.send_message(
                chat_id=mod.id,
                text=f"❌ Разбан не удался: {result.get('error', 'unknown')}",
            )
        except TelegramAPIError:
            pass
        return

    await _send_report(
        bot=message.bot, chat_id=chat_id, target=target,
        action_type="unban", mod=mod,
        reply_to_message=message.reply_to_message,
    )

    await _send_ephemeral(
        bot=message.bot, chat_id=chat_id, recipient=mod,
        text=f"✅ Разбанен {_user_mention_html(target)}.",
    )

    await _send_audit_to_report(
        bot=message.bot, chat_id=chat_id, mod=mod, target=target,
        action_label="бан", detail="команда !unban",
    )


# ── cmd_unwarn ──────────────────────────────────────────────────────────────


async def cmd_unwarn(message: types.Message, ctx: ModContext) -> None:
    """!unwarn [N] — снимает N варнов (по умолчанию 1).

    Перенесена из handle_group_command (bot_handlers.py:4687-4735) в v4.8.10.
    """
    m = _CMD_UNWARN.match(ctx.text)
    if m is None:
        return
    n_str = m.group(1)
    n = int(n_str) if n_str else 1
    if n < 1:
        n = 1

    chat_id = ctx.chat_id
    mod = ctx.mod
    target = ctx.target

    async with async_session() as session:
        await _upsert_user(session, target.id, target.username,
                           target.first_name, target.last_name)
        await _upsert_moderator(session, mod.id, mod.username, mod.first_name)
        current_warns = await _count_warns(session, target.id, chat_id)
        n_effective = min(n, max(current_warns, 1))
        revoked_count = await _revoke_last_warns(
            session, target.id, chat_id, n_effective, revoked_by_mod_id=mod.id,
        )
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

    await _send_ephemeral(
        bot=message.bot, chat_id=chat_id, recipient=mod,
        text=(
            f"✅ Снято {revoked_count} варн(а/ов) с {_user_mention_html(target)}."
            f" Варнов всего: {total_warns_now}"
        ),
    )

    await _send_audit_to_report(
        bot=message.bot, chat_id=chat_id, mod=mod, target=target,
        action_label="варн(а/ов)", detail="команда !unwarn",
        count=revoked_count,
    )


# ── cmd_warns ───────────────────────────────────────────────────────────────


async def cmd_warns(message: types.Message, ctx: ModContext) -> None:
    """!warns — показывает количество варнов у цели (модератору в ЛС).

    Перенесена из handle_group_command (bot_handlers.py:4738-4785) в v4.8.10.
    """
    chat_id = ctx.chat_id
    target = ctx.target

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
        sent = await message.bot.send_message(
            chat_id=chat_id,
            text=f"⚠️ {target.first_name or target.id}: {total_warns} варнов",
        )
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
        _spawn_background_task(_del_msg(), label="del_msg")
    try:
        await message.delete()
    except TelegramAPIError:
        pass


# ── cmd_resetwarns ──────────────────────────────────────────────────────────


async def cmd_resetwarns(message: types.Message, ctx: ModContext) -> None:
    """!resetwarns — полный сброс варнов (SU/Admin only).

    Перенесена из handle_group_command (bot_handlers.py:4795-4871) в v4.8.10.
    """
    chat_id = ctx.chat_id
    mod = ctx.mod
    target = ctx.target

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


# ── cmd_resetmc ─────────────────────────────────────────────────────────────


async def cmd_resetmc(message: types.Message, ctx: ModContext) -> None:
    """!resetmc — сброс счётчика автомьютов (SU/Admin only).

    Перенесена из handle_group_command (bot_handlers.py:4877-4934) в v4.8.10.
    """
    chat_id = ctx.chat_id
    mod = ctx.mod
    target = ctx.target

    async with async_session() as session:
        mod_role = await _get_web_user_role(session, mod.id)
    is_privileged = (mod.id in ADMIN_IDS) or (mod_role in ("su", "admin"))
    if not is_privileged:
        try:
            await _send_ephemeral(
                bot=message.bot, chat_id=chat_id, recipient=mod,
                text=(
                    "❌ !resetmc доступен только SU/Admin. "
                    "Сброс счётчика автомьютов — привилегия администратора."
                ),
            )
        except Exception:
            pass
        try:
            await message.delete()
        except TelegramAPIError:
            pass
        return

    async with async_session() as session:
        await _upsert_user(session, target.id, target.username,
                           target.first_name, target.last_name)
        await _upsert_moderator(session, mod.id, mod.username, mod.first_name)
        old_count = await _reset_automute_count(session, chat_id, target.id)
        await session.commit()

    try:
        await message.bot.send_message(
            chat_id=message.from_user.id,
            text=(
                f"✅ Счётчик автомьютов обнулён: "
                f"{target.first_name or ''}"
                f"{' ' + target.last_name if target.last_name else ''}"
                f"{' @' + target.username if target.username else ''}"
                f" (было {old_count})"
            ),
        )
    except TelegramAPIError:
        pass

    await _send_audit_to_report(
        bot=message.bot, chat_id=chat_id, mod=mod, target=target,
        action_label="счётчик автомьютов — сброс",
        detail=f"команда !resetmc (было {old_count})",
        count=old_count,
    )

    try:
        await message.delete()
    except TelegramAPIError:
        pass


# ── Dispatcher ──────────────────────────────────────────────────────────────
# v4.8.10: все 12 команд перенесены. handle_group_command теперь ~50-строчный
# dispatcher: парсит команду → вызывает COMMANDS[cmd](message, ctx).

COMMANDS: dict[str, callable] = {
    "ban": cmd_ban,
    "sban": cmd_sban,
    "mute": cmd_mute,
    "smute": cmd_smute,
    "warn": cmd_warn,
    "swarn": cmd_swarn,
    "unmute": cmd_unmute,
    "unban": cmd_unban,
    "unwarn": cmd_unwarn,
    "warns": cmd_warns,
    "resetwarns": cmd_resetwarns,
    "resetmc": cmd_resetmc,
}
