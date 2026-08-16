"""
chat_modes.py — v4.8.0: унифицированная логика режимов чата.

Раньше (до v4.8.0) snapshot прав и восстановление были размазаны по
трём режимам (alarm, night, sanitary) с тремя копиями похожей логики:

  • `_enter_night_mode` (bot.py) — snapshot через bot.get_chat().permissions,
    с поддержкой day_permissions preset.
  • `_enter_sanitary_day` (bot.py) — то же самое, копия логики.
  • `handle_alarm_command` (bot_handlers.py) — snapshot БЕЗ preset support,
    упрощённая копия.

Восстановление прав — аналогично:
  • `_deactivate_alarm` (bot_handlers.py) — 4-уровневый fallback
    (day_permissions → alarm_saved → system_default → hardcoded),
    но БЕЗ использования `_resolve_day_perms` из bot.py.
  • `_restore_day_state` (bot.py) — использует `_resolve_day_perms`,
    но только для night/sanitary, не для alarm.

В v4.8.0 унифицировано через:
  • `_snapshot_chat_permissions(bot, chat_id, day_permissions=None)`
    — единая функция snapshot для всех трёх режимов.
  • `_resolve_restore_perms(session, cs)` — единая логика выбора прав
    для восстановления (4-уровневый fallback).
  • `_apply_chat_permissions(bot, chat_id, perms)` — обёртка над
    set_chat_permissions с use_independent_chat_permissions=True.

Архитектурные инварианты (см. ROADMAP_v4.8.0.md):
  1. Alarm / Night / Sanitary работают с chat-default permissions,
     НЕ с per-user overrides.
  2. Snapshot берётся из `chat.permissions`, не из individual members.
  3. use_independent_chat_permissions=True — обязательно.
  4. Snapshot хранится в JSON (13 полей из _PERM_FIELDS).
  5. Порядок tick'ов: alarm → sanitary → night.
  6. Приоритет режимов: sanitary > night > alarm > day.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from aiogram import types
from aiogram.exceptions import TelegramAPIError
from sqlalchemy import select

from db import ChatSettings, async_session

if TYPE_CHECKING:
    from aiogram import Bot

logger = logging.getLogger("shadow_logger")


# ── Поля ChatPermissions (13 шт) ────────────────────────────────────────────
# Должно совпадать с _PERM_FIELDS в bot_handlers.py.
_PERM_FIELDS: tuple[str, ...] = (
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
)


# ── Hardcoded Day default (fallback последнего эшелона) ─────────────────────
# Совпадает с _DAY_DEFAULT_HARDCODED в bot.py.
_DAY_DEFAULT_HARDCODED: dict[str, bool] = {
    "can_send_messages": True,
    "can_send_audios": True,
    "can_send_photos": True,
    "can_send_videos": True,
    "can_send_other_messages": True,
    "can_send_documents": False,
    "can_send_video_notes": False,
    "can_send_voice_notes": False,
    "can_send_polls": False,
    "can_add_web_page_previews": False,
    "can_change_info": False,
    "can_invite_users": False,
    "can_pin_messages": False,
}


# ── Snapshot ────────────────────────────────────────────────────────────────
async def _snapshot_chat_permissions(
    bot: types.Bot,
    chat_id: int,
    day_permissions: str | None = None,
) -> tuple[str | None, int]:
    """v4.8.0: делает snapshot прав чата для любого режима.

    Возвращает (snapshot_json, slow_mode_delay).

    Приоритет источника snapshot'а:
      1. Если `day_permissions` (JSON-строка preset'а) передан —
         используем его. Это правильно, потому что day_permissions —
         это то, что ДОЛЖНО быть после выхода из режима, а не то, что
         СЕЙЧАС в чате (которое может быть уже изменено предыдущим
         режимом).
      2. Иначе — текущие права чата через bot.get_chat().permissions.
         Это старое поведение (backward compat) для чатов без preset.

    slow_mode_delay берётся из текущего chat.slow_mode_delay (chat-level
    свойство, не входит в ChatPermissions).

    Args:
      bot: aiogram Bot instance.
      chat_id: ID чата.
      day_permissions: опционально — JSON preset'а дневных прав.

    Returns:
      (snapshot_json, slow_mode_delay). snapshot_json может быть None
      если chat_info.permissions is None (ганона для групп, но
      defensive). slow_mode_delay — int (0 если не задано).

    Raises:
      TelegramAPIError — если bot.get_chat упал (чат удалён, бот кикнут).
                        Вызывающий код решает, что делать.
    """
    chat_info = await bot.get_chat(chat_id=chat_id)

    # ── Права для snapshot'а ───────────────────────────────────────────
    snapshot_data: dict[str, bool]
    if day_permissions:
        try:
            data = json.loads(day_permissions)
            # Гарантируем что все 13 полей присутствуют.
            snapshot_data = {k: bool(data.get(k, False)) for k in _PERM_FIELDS}
        except (ValueError, TypeError) as e:
            logger.warning(
                "Snapshot: bad day_permissions JSON for chat %s: %s — "
                "falling back to current chat permissions",
                chat_id, e,
            )
            current_perms = chat_info.permissions
            snapshot_data = {
                field: bool(getattr(current_perms, field, True)) if current_perms else True
                for field in _PERM_FIELDS
            }
    else:
        current_perms = chat_info.permissions
        snapshot_data = {
            field: bool(getattr(current_perms, field, True)) if current_perms else True
            for field in _PERM_FIELDS
        }

    snapshot_json = json.dumps(snapshot_data, ensure_ascii=False)

    # ── slow_mode_delay ────────────────────────────────────────────────
    slow_mode = getattr(chat_info, "slow_mode_delay", 0) or 0
    if not isinstance(slow_mode, int):
        # v4.7.20: защита от MagicMock в тестах / None в реальности.
        slow_mode = 0

    return snapshot_json, slow_mode


# ── Restore ─────────────────────────────────────────────────────────────────
def _restore_permissions_from_json(snapshot_json: str) -> types.ChatPermissions:
    """v4.8.0: восстанавливает ChatPermissions из JSON snapshot'а.

    Унифицированная версия. Используется всеми тремя режимами при
    восстановлении из своего *_saved_permissions поля.

    Raises:
      ValueError, TypeError — если JSON битый.
    """
    data = json.loads(snapshot_json)
    return types.ChatPermissions(
        **{k: bool(data.get(k, False)) for k in _PERM_FIELDS}
    )


def _hardcoded_day_default() -> types.ChatPermissions:
    """v4.8.0: hardcoded fallback (последний эшелон).

    Используется когда:
      • day_permissions preset не задан.
      • System preset «Day default» не найден в БД.
      • snapshot сохранённых прав пустой или битый.

    Никогда не выдаёт admin-права (can_change_info, can_invite_users,
    can_pin_messages) — это safety net.
    """
    return types.ChatPermissions(
        **{k: _DAY_DEFAULT_HARDCODED[k] for k in _PERM_FIELDS}
    )


def _resolve_restore_perms_sync(
    cs: "ChatSettings",
    saved_permissions_field: str | None,
    saved_source_name: str = "snapshot",
) -> tuple[types.ChatPermissions, str]:
    """v4.8.0: синхронная логика выбора прав для восстановления.

    Приоритет:
      1. cs.day_permissions (явный preset чата) → "day_permissions preset"
      2. saved_permissions_field (snapshot ДО режима) → "{saved_source_name}"
      3. Hardcoded default → "hardcoded default"

    Args:
      cs: объект ChatSettings.
      saved_permissions_field: значение поля *_saved_permissions
                              (alarm_saved_permissions / night_mode_saved_permissions
                              / sanitary_days_saved_permissions).
      saved_source_name: имя источника для логирования ("alarm snapshot",
                       "night snapshot", "sanitary snapshot").

    Returns:
      (ChatPermissions, source_str) — права и строка-описание источника.

    Note: системный пресет «Day default» тут НЕ проверяется — для этого
    нужен async вариант `_resolve_restore_perms_async` (ниже), т.к. требует
    обращения к БД. Эта sync-версия используется в `_deactivate_alarm`
    (bot_handlers.py), где уже есть session, но логика специально упрощена
    чтобы избежать лишних запросов.
    """
    # 1. Явный preset чата.
    if cs.day_permissions:
        try:
            perms = _restore_permissions_from_json(cs.day_permissions)
            return perms, "day_permissions preset"
        except (ValueError, TypeError):
            logger.warning(
                "Restore: bad day_permissions JSON for chat %s — falling back",
                cs.chat_id,
            )

    # 2. Snapshot режима.
    if saved_permissions_field:
        try:
            perms = _restore_permissions_from_json(saved_permissions_field)
            return perms, saved_source_name
        except (ValueError, TypeError):
            logger.warning(
                "Restore: bad %s JSON for chat %s — falling back",
                saved_source_name, cs.chat_id,
            )

    # 3. Hardcoded default.
    return _hardcoded_day_default(), "hardcoded default"


async def _resolve_restore_perms_async(
    session,
    cs: "ChatSettings",
    saved_permissions_field: str | None,
    saved_source_name: str = "snapshot",
) -> tuple[types.ChatPermissions, str]:
    """v4.8.0: async-вариант с поддержкой системного пресета «Day default».

    Приоритет:
      1. cs.day_permissions (явный preset чата) → "day_permissions preset"
      2. Системный пресет «Day default» из БД → "system default"
      3. saved_permissions_field (snapshot ДО режима) → "{saved_source_name}"
      4. Hardcoded default → "hardcoded default"

    Note: системный пресет имеет ПРИОРИТЕТ над snapshot'ом режима. Это
    правильно, потому что day_permissions / system default — это
    «то что должно быть днём», а snapshot — «то что было до режима»
    (что может отличаться, если админ вручную менял права).

    Используется в bot.py (_restore_day_state для night/sanitary).
    """
    # 1. Явный preset чата.
    if cs.day_permissions:
        try:
            perms = _restore_permissions_from_json(cs.day_permissions)
            return perms, "day_permissions preset"
        except (ValueError, TypeError):
            logger.warning(
                "Restore: bad day_permissions JSON for chat %s — falling back",
                cs.chat_id,
            )

    # 2. Системный пресет «Day default».
    try:
        from sqlalchemy import select

        from db import PermissionPreset
        preset = (await session.execute(
            select(PermissionPreset).where(
                PermissionPreset.name == "Day default",
                PermissionPreset.scope == "day",
            )
        )).scalar_one_or_none()
        if preset and preset.permissions:
            try:
                data = json.loads(preset.permissions)
                perms = types.ChatPermissions(
                    **{k: bool(data.get(k, False)) for k in _PERM_FIELDS}
                )
                return perms, "system default"
            except (ValueError, TypeError):
                logger.warning(
                    "Restore: bad system Day default JSON — falling back",
                )
    except Exception as e:
        logger.warning("Restore: failed to load system Day default: %s", e)

    # 3. Snapshot режима.
    if saved_permissions_field:
        try:
            perms = _restore_permissions_from_json(saved_permissions_field)
            return perms, saved_source_name
        except (ValueError, TypeError):
            logger.warning(
                "Restore: bad %s JSON for chat %s — falling back",
                saved_source_name, cs.chat_id,
            )

    # 4. Hardcoded default.
    return _hardcoded_day_default(), "hardcoded default"


# ── Apply ───────────────────────────────────────────────────────────────────
async def _apply_chat_permissions(
    bot: types.Bot,
    chat_id: int,
    perms: types.ChatPermissions,
) -> bool:
    """v4.8.0: обёртка над set_chat_permissions с independent-режимом.

    use_independent_chat_permissions=True — критично (см. архитектурный
    инвариант #3 в ROADMAP). Без этого Telegram работает в legacy-режиме,
    где can_send_other_messages=True неявно подтягивает
    can_send_video_notes=True и др.

    Returns:
      True при успехе, False при ошибке (сетевая / права бота / чат удалён).
    """
    try:
        await bot.set_chat_permissions(
            chat_id=chat_id,
            permissions=perms,
            use_independent_chat_permissions=True,
        )
        return True
    except TelegramAPIError as e:
        logger.error(
            "Apply permissions: set_chat_permissions failed for chat %s: %s",
            chat_id, e,
        )
        return False


# ── Приоритет режимов ───────────────────────────────────────────────────────
# Задокументирован явно в коде (а не только в комментариях tick'ов).
# sanitary > night > alarm > day
#
# Это значит:
#   • Если sanitary active — night и alarm не входят (снимаются если были).
#   • Если night active — alarm не входит (снимается если был).
#   • Day — это «никакой режим не активен».
#
# Порядок tick'ов в _night_mode_loop (bot.py):
#   1. _alarm_auto_off_tick — снимает alarm'ы с истёкшим timeout
#   2. _sanitary_day_tick — входит/выходит из sanitary
#   3. _night_mode_tick — входит/выходит из night
#
# При входе в night mode — снимаем активный alarm (если есть).
# При входе в sanitary day — снимаем активные alarm AND night mode.
# (см. _enter_night_mode и _enter_sanitary_day в bot.py)


def _mode_priority(cs: "ChatSettings") -> str:
    """v4.8.0: возвращает имя текущего активного режима по приоритету.

    Используется для логирования и отладки. Не используется для принятия
    решений (tick'и и так знают порядок).

    Returns:
      "sanitary" | "night" | "alarm" | "day"
    """
    if cs.sanitary_days_currently_active:
        return "sanitary"
    if cs.night_mode_currently_active:
        return "night"
    if cs.alarm_currently_active:
        return "alarm"
    return "day"


def _active_modes(cs: "ChatSettings") -> list[str]:
    """v4.8.0: возвращает список всех активных режимов (для логирования).

    В норме список должен содержать 0 или 1 элемент. Если больше —
    это инвариант-нарушение (логируем как warning в вызывающем коде).
    """
    modes: list[str] = []
    if cs.sanitary_days_currently_active:
        modes.append("sanitary")
    if cs.night_mode_currently_active:
        modes.append("night")
    if cs.alarm_currently_active:
        modes.append("alarm")
    return modes


# ── Alarm auto-off (перенесено из bot.py в v4.8.9) ─────────────────────────
# v4.8.9: функция перенесена сюда из bot.py — это её домен (см. план v4.8.9 §5).
# Раньше лежала в bot.py:218 как часть _night_mode_loop, что создавало
# путаницу: night/sanitary/alarm — всё в chat_modes.py, а alarm auto-off
# почему-то отдельно. Теперь все tick'и alarm'а — в одном модуле.


async def _alarm_auto_off_tick(bot: "Bot") -> None:
    """v4.7.30: проверяет ВСЕ чаты с активным alarm и истёкшим alarm_active_until.

    v4.8.9: перенесена из bot.py в chat_modes.py — это её домен.

    Вынесено из _night_mode_tick в отдельную функцию — Баг #1 аудита v4.7.30:
    раньше auto-off работал только для чатов с night_mode_enabled=True
    (т.к. был встроен в query _night_mode_tick). Чаты без night_mode вообще
    не проверялись, alarm зависал навсегда даже при указанной длительности.

    Теперь: отдельный query по всем чатам с alarm_currently_active=True и
    alarm_active_until IS NOT NULL. Если now >= alarm_active_until —
    вызываем _deactivate_alarm(reason="auto_off_timeout").

    Вызывается из _night_mode_loop (bot.py) ПЕРЕД _sanitary_day_tick и
    _night_mode_tick — чтобы alarm снялся до любых других манипуляций
    с правами чата.

    Args:
        bot: экземпляр aiogram.Bot — нужен для _deactivate_alarm (восстанавливает
            права через bot.set_chat_permissions) и для отправки modchat-события.
    """
    now = datetime.now(timezone.utc)
    try:
        async with async_session() as session:
            stmt = select(ChatSettings).where(
                ChatSettings.alarm_currently_active.is_(True),
                ChatSettings.alarm_active_until.is_not(None),
                ChatSettings.chat_id != 0,  # пропускаем global default
                ChatSettings.is_enabled.is_(True),  # чат активен
            )
            chats = (await session.execute(stmt)).scalars().all()
    except Exception as e:
        logger.warning("Alarm auto-off tick: DB error loading chats: %s", e)
        return

    for cs in chats:
        if cs.alarm_active_until is None or now < cs.alarm_active_until:
            continue  # не вышло время (двойная проверка на случай race)
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
                    # Сохраняем started_by ДО деактивации — для modchat-события.
                    started_by = alarm_cs.alarm_started_by
                    # Используем _deactivate_alarm из bot_handlers.
                    # Восстанавливает права из snapshot/preset.
                    # Lazy import — чтобы избежать циклической зависимости
                    # (bot_handlers импортирует из chat_modes).
                    from bot_handlers import _deactivate_alarm
                    await _deactivate_alarm(
                        alarm_session, alarm_cs, bot,
                        cs.chat_id, reason="auto_off_timeout",
                    )
                    # Синхронизируем cs в памяти (на случай если кто-то
                    # дальше в этом тике будет читать — defensive).
                    cs.alarm_currently_active = False
                    cs.alarm_saved_permissions = None
                    cs.alarm_saved_slow_mode_delay = None
                    cs.alarm_active_until = None
                    # v4.8.0: отправляем событие в modchat.
                    try:
                        from modchat import _send_alarm_event_to_modchat
                        await _send_alarm_event_to_modchat(
                            bot=bot, chat_id=cs.chat_id, event_type="auto_off",
                            mod_id=started_by,
                            reason="истёк таймаут",
                        )
                    except Exception as modchat_e:
                        logger.debug(
                            "Modchat alarm auto-off event failed for chat %s: %s",
                            cs.chat_id, modchat_e,
                        )
        except Exception as e:
            logger.error(
                "Alarm auto-off failed for chat %s: %s",
                cs.chat_id, e,
            )
