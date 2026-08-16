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

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from aiogram import types
from aiogram.exceptions import TelegramAPIError
from sqlalchemy import select

from bot_handlers import (
    _CMD_BAN,
    _add_banned_sticker_pack,
    _mark_bot_ban,
    _save_punishment,
    _send_ephemeral,
    _send_public_punishment_notice,
    _send_report,
    _snapshot_permissions,
    _upsert_moderator,
    _upsert_user,
    tg_safe_call,
)
from db import BannedStickerPack, async_session

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


# ── Dispatcher ──────────────────────────────────────────────────────────────
# v4.8.9: пока только "ban". По мере переноса других команд (cmd_sban, cmd_mute,
# cmd_smute, cmd_warn, cmd_swarn) — добавлять сюда.
# Остальные 11 команд (!unmute, !unban, !unwarn, !warns, !resetwarns, !resetmc,
# !sban, !mute, !smute, !warn, !swarn) пока обрабатываются inline в
# handle_group_command — TODO v4.9.0.

COMMANDS: dict[str, callable] = {
    "ban": cmd_ban,
}
