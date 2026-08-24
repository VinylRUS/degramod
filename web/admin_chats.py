"""
web/admin_chats.py — управление настройками чатов (/admin/chats*), v4.4.7+.

v4.9.0 (Task 11): вынесены семь роутов из create_app() в web_app.py:
GET /admin/chats, POST /admin/chats/{chat_id_str}/update,
POST /admin/chats/{chat_id_str}/toggle, POST /admin/chats/{chat_id_str}/delete,
POST /admin/chats/{chat_id_str}/sync-admins,
POST /admin/chats/{chat_id_str}/sanitary/add,
POST /admin/chats/{chat_id_str}/sanitary/{idx_str}/delete.

Крупнейший домен проекта: настройки чата (хэштег, репорт-чат, пороги
варнов, link filter, via-bot filter), режимы (night mode / sanitary days —
snapshot/restore прав описан в chat_modes.py), синхронизация TG-админов
чата в WebUser/chat_admins, CRUD санитарных периодов.

`_req_logger` берётся через модуль (`web_app._req_logger`), а не импортом
имени — тесты патчят его как атрибут `web_app` (см. CLAUDE.md, «Обращения
к web_app»). `async_session` импортируется напрямую из `db` (как и в
остальных вынесенных модулях) — это отдельный символ от `web_app.async_session`,
и тесты, поднимающие тестовую БД, патчат его именно на этом модуле
(`web.admin_chats.async_session`).

`admin_chats_toggle` содержит late-импорты `from app_state import
get_exit_night_mode` и `get_exit_sanitary_day` внутри тела функции —
service locator, введённый в v4.8.9 взамен хака с `sys.modules` (см.
`app_state.py`). Перенесены дословно, наверх модуля не подняты.
"""
from __future__ import annotations

import json
import re as _re
from datetime import datetime, timezone

from aiogram.exceptions import TelegramBadRequest
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

import web_app
from db import ChatAdmin, ChatSettings, PermissionPreset, Punishment, WebUser, async_session
from web.deps import AuthUser, get_bot, get_templates, require_admin, require_csrf_admin, require_csrf_su

router = APIRouter()


# ──────────────────────────────────────────────────────────────────
#  /admin/chats — управление настройками чатов (v4.4.7)
#
#  v4.4.7: добавлены toggles для is_enabled / is_private / is_report_chat.
#  Чаты создаются автоматически (ботом при добавлении в чат), здесь —
#  только редактирование настроек.
#
#  Доступ: SU + admin (require_admin). Moderator → redirect на /dashboard.
# ──────────────────────────────────────────────────────────────────
@router.get("/admin/chats", response_class=HTMLResponse)
async def admin_chats_page(
    request: Request,
    flash: str = "",
    _auth: AuthUser = Depends(require_admin),
    templates: Jinja2Templates = Depends(get_templates),
):
    """Страница управления настройками чатов (v4.4.7)."""
    async with async_session() as session:
        stmt = (
            select(ChatSettings)
            .order_by(ChatSettings.chat_id.asc())
        )
        rows = (await session.execute(stmt)).scalars().all()

        # Статистика наказаний
        stats: dict[int, int] = {}
        if rows:
            chat_ids = [r.chat_id for r in rows if r.chat_id != 0]
            if chat_ids:
                stat_rows = (await session.execute(
                    select(Punishment.chat_id, func.count(Punishment.id))
                    .where(Punishment.chat_id.in_(chat_ids))
                    .group_by(Punishment.chat_id)
                )).all()
                stats = {cid: cnt for cid, cnt in stat_rows}

        # Кол-во модераторов (chat_admins) на каждый чат
        mod_counts: dict[int, int] = {}
        if rows:
            chat_ids = [r.chat_id for r in rows if r.chat_id != 0]
            if chat_ids:
                mc_rows = (await session.execute(
                    select(ChatAdmin.chat_id, func.count(ChatAdmin.id))
                    .where(ChatAdmin.chat_id.in_(chat_ids))
                    .group_by(ChatAdmin.chat_id)
                )).all()
                mod_counts = {cid: cnt for cid, cnt in mc_rows}

        # v4.5.1: список доступных репорт-чатов (is_report_chat=True)
        # — для dropdown в шаблоне admin_chats.html вместо свободного
        # текстового поля. Так SU не может ввести несуществующий chat_id
        # и удивляться, что отчёты не приходят.
        report_chat_options = [
            {
                "chat_id": r.chat_id,
                "title": r.title or f"(id {r.chat_id})",
                "hashtag": r.hashtag,
            }
            for r in rows
            if r.is_report_chat and r.chat_id != 0
        ]

        # v4.6.0: список всех пресетов для dropdown в admin_chats.html.
        presets_rows = (await session.execute(
            select(PermissionPreset).order_by(
                PermissionPreset.scope, PermissionPreset.name
            )
        )).scalars().all()
        presets_by_scope = {"day": [], "night": [], "sanitary": []}
        for p in presets_rows:
            if p.scope in presets_by_scope:
                presets_by_scope[p.scope].append(p)

    return templates.TemplateResponse("admin_chats.html", {
        "request": request,
        "chats": rows,
        "stats": stats,
        "mod_counts": mod_counts,
        "report_chat_options": report_chat_options,
        "presets_by_scope": presets_by_scope,
        "auth_user": _auth,
        "flash": flash or None,
    })


@router.post("/admin/chats/{chat_id_str}/update")
async def admin_chats_update(
    chat_id_str: str,
    hashtag: str = Form(""),
    report_chat_id: str = Form(""),
    warns_to_mute: str = Form(""),
    mute_duration_seconds: str = Form(""),
    warns_to_ban: str = Form(""),
    warn_decay_days: str = Form(""),
    link_filter_action: str = Form("delete"),
    # v4.5.3: расширенная настройка ночного режима.
    night_mode_start: str = Form("23:00"),
    night_mode_end: str = Form("07:00"),
    night_mode_tz: str = Form("Europe/Moscow"),
    night_mode_weekend_start: str = Form(""),
    night_mode_weekend_end: str = Form(""),
    night_mode_notify: str = Form(""),
    night_mode_notify_enter_msg: str = Form(""),
    night_mode_notify_exit_msg: str = Form(""),
    # v4.5.4: sanitary days textarea. Multiline-текст, одна запись на
    # строку ('YYYY-MM-DD' или 'YYYY-MM-DD - YYYY-MM-DD').
    sanitary_days_text: str = Form(""),
    # v4.7.24: via-bot rate-limit filter (настройки в разделе «Наказания»).
    # via_bot_rate_limit_seconds — grace-окно (по умолчанию 300 = 5 мин).
    # via_bot_mute_minutes — длительность мьюта при превышении (по умолчанию 10).
    via_bot_rate_limit_seconds: str = Form("300"),
    via_bot_mute_minutes: str = Form("10"),
    # v4.6.1: пресеты прав — только выбор из dropdown. Custom grids убраны,
    # свои наборы прав создаются на странице /admin/presets.
    # preset_id="" или "__none__" → NULL (старое поведение, через snapshot).
    # preset_id="__lockdown__" → all False (только для sanitary, default).
    # preset_id=<int> → берём permissions из пресета.
    # v4.7.16: из пресета также копируется slow_mode_delay (если задан).
    day_preset_id: str = Form(""),
    night_preset_id: str = Form(""),
    sanitary_preset_id: str = Form("__lockdown__"),
    # v5.1.0: своя ссылка на правила для /rules. Пусто → RULES_URL_DEFAULT.
    rules_url: str = Form(""),
    _auth: AuthUser = Depends(require_csrf_admin),
):
    """Обновляет настройки чата (включая v4.5.2: warn decay, link filter, night mode)."""
    try:
        chat_id = int(chat_id_str)
    except (ValueError, TypeError):
        return RedirectResponse(
            url=f"/admin/chats?flash=Invalid+chat_id+%27{chat_id_str}%27",
            status_code=303,
        )

    def _parse_int(raw: str, field_name: str, min_val: int = 0) -> int | None:
        raw = (raw or "").strip()
        if field_name == "report_chat_id" and raw == "":
            return None
        try:
            v = int(raw)
        except (ValueError, TypeError):
            raise ValueError(f"{field_name} must be a number")
        if v < min_val:
            raise ValueError(f"{field_name} must be >= {min_val}")
        return v

    try:
        wtm = _parse_int(warns_to_mute, "warns_to_mute", 0)
        mdb = _parse_int(mute_duration_seconds, "mute_duration_seconds", 0)
        wtb = _parse_int(warns_to_ban, "warns_to_ban", 0)
        rc = _parse_int(report_chat_id, "report_chat_id", -10**15)
        decay = _parse_int(warn_decay_days, "warn_decay_days", 0)
        # v4.7.24: via-bot rate-limit settings (1..86400 sec / 1..1440 min)
        vb_rl = _parse_int(via_bot_rate_limit_seconds, "via_bot_rate_limit_seconds", 1)
        vb_mm = _parse_int(via_bot_mute_minutes, "via_bot_mute_minutes", 1)
    except ValueError as e:
        return RedirectResponse(
            url=f"/admin/chats?flash={e}",
            status_code=303,
        )

    # v4.7.24: sanity-clip — rate-limit не больше 24h, mute не больше 24h
    if vb_rl is not None and vb_rl > 86400:
        vb_rl = 86400
    if vb_mm is not None and vb_mm > 1440:
        vb_mm = 1440

    # v4.5.2: валидация link_filter_action.
    # v4.6.1: night_mode_preset валидация убрана — presetId из БД валидируется ниже.
    if link_filter_action not in ("delete", "warn", "mute", "ban"):
        return RedirectResponse(
            url="/admin/chats?flash=Invalid+link_filter_action",
            status_code=303,
        )

    # v5.1.0 (фикс финального ревью): валидация rules_url. Без неё кривое
    # значение (без схемы, случайный текст) сохранялось молча — Telegram
    # отвергал ссылку при отправке, _send_ephemeral глушил ошибку, и /rules
    # тихо переставал работать без единого объяснения в интерфейсе.
    rules_url_stripped = (rules_url or "").strip()
    if rules_url_stripped and not rules_url_stripped.startswith(("http://", "https://")):
        return RedirectResponse(
            url="/admin/chats?flash=rules_url+must+start+with+http%3A%2F%2F+or+https%3A%2F%2F",
            status_code=303,
        )

    # v4.5.2: валидация HH:MM для night mode
    _hhmm_re = _re.compile(r"^([01][0-9]|2[0-3]):([0-5][0-9])$")
    nm_start = (night_mode_start or "").strip()
    nm_end = (night_mode_end or "").strip()
    if not _hhmm_re.match(nm_start):
        return RedirectResponse(
            url="/admin/chats?flash=Invalid+night_mode_start+(use+HH:MM)",
            status_code=303,
        )
    if not _hhmm_re.match(nm_end):
        return RedirectResponse(
            url="/admin/chats?flash=Invalid+night_mode_end+(use+HH:MM)",
            status_code=303,
        )

    # v4.5.3: валидация tz (IANA timezone).
    nm_tz = (night_mode_tz or "Europe/Moscow").strip()
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(nm_tz)
    except (ValueError, KeyError):
        return RedirectResponse(
            url="/admin/chats?flash=Invalid+night_mode_tz+(use+IANA+name+like+Europe/Moscow)",
            status_code=303,
        )

    # v4.5.3: валидация weekend schedule (опционально).
    # Если одно из полей задано — оба обязательны.
    nm_wknd_start = (night_mode_weekend_start or "").strip()
    nm_wknd_end = (night_mode_weekend_end or "").strip()
    if (nm_wknd_start or nm_wknd_end) and not (nm_wknd_start and nm_wknd_end):
        return RedirectResponse(
            url="/admin/chats?flash=Weekend+schedule+requires+both+start+and+end",
            status_code=303,
        )
    if nm_wknd_start and not _hhmm_re.match(nm_wknd_start):
        return RedirectResponse(
            url="/admin/chats?flash=Invalid+night_mode_weekend_start+(use+HH:MM)",
            status_code=303,
        )
    if nm_wknd_end and not _hhmm_re.match(nm_wknd_end):
        return RedirectResponse(
            url="/admin/chats?flash=Invalid+night_mode_weekend_end+(use+HH:MM)",
            status_code=303,
        )

    ht = (hashtag or "").strip()
    if ht and not ht.startswith("#"):
        ht = "#" + ht
    if len(ht) > 64:
        return RedirectResponse(
            url="/admin/chats?flash=Hashtag+too+long+(max+64)",
            status_code=303,
        )

    # v4.6.1: night_mode_permissions теперь берётся только из night_preset_id.
    # Старый dropdown night_mode_preset (text_only/strict/none/custom) и custom grid
    # (perm_can_send_*) убраны из UI — свои наборы прав создаются на /admin/presets.
    # Изначально night_perms_json = None (NULL в БД = night mode не меняет права).
    # Если night_preset_id указывает на валидный пресет — берём его permissions.
    night_perms_json: str | None = None

    # v4.5.4 / v4.6.0: парсим sanitary_days.
    # v4.6.1: monthly_sanitary_days_json убран — UI шлёт только sanitary_days_text
    # (textarea). Парсинг остаётся тем же.
    try:
        from bot_handlers import (
            parse_sanitary_days_textarea,
            serialize_sanitary_days_monthly,
        )
    except ImportError:
        return RedirectResponse(
            url="/admin/chats?flash=Server+error+(bot_handlers+import+failed)",
            status_code=303,
        )

    # v4.6.1: UI присылает только textarea (sanitary_days_text).
    # Парсим и группируем по месяцам автоматически.
    # v4.7.11: парсер теперь возвращает entries длиной 2/3/4 (с опциональным
    # временем). Группировка должна сохранять время, иначе round-trip
    # format→parse→serialize терял данные и валидация падала на строках
    # вида '2026-07-31 23:00 - 2026-08-03 09:00'.
    san_pairs, san_errors = parse_sanitary_days_textarea(sanitary_days_text)
    if san_errors:
        first_err = san_errors[0].replace(" ", "+")
        return RedirectResponse(
            url=f"/admin/chats?flash=Sanitary+days:+{first_err}",
            status_code=303,
        )
    if san_pairs:
        grouped: dict[str, list[list[str]]] = {}
        for entry in san_pairs:
            # entry: [start_iso, end_iso] / [s, e, st] / [s, e, st, et]
            mk = entry[0][:7]  # YYYY-MM
            grouped.setdefault(mk, []).append(entry)
        sanitary_days_json = serialize_sanitary_days_monthly(grouped)
    else:
        sanitary_days_json = None

    # v4.6.1: пресеты прав — только выбор из dropdown, без custom grids.
    # Загружаем все пресеты одним запросом для валидации.
    async with async_session() as _ps:
        preset_records = (await _ps.execute(
            select(PermissionPreset)
        )).scalars().all()
    preset_by_id = {p.id: p for p in preset_records}

    _ALL_PERM_KEYS = (
        "can_send_messages", "can_send_audios", "can_send_documents",
        "can_send_photos", "can_send_videos", "can_send_video_notes",
        "can_send_voice_notes", "can_send_polls", "can_send_other_messages",
        "can_add_web_page_previews", "can_change_info", "can_invite_users",
        "can_pin_messages",
    )

    def _resolve_perms(preset_id_field: str, scope: str) -> tuple[str | None, int | None]:
        """v4.6.1: Возвращает (JSON-строка permissions, slow_mode_delay) для ChatSettings.

        Логика (custom grids убраны — только выбор пресета):
          • preset_id_field == "__none__" или "" → (None, None) (старое поведение / snapshot)
          • preset_id_field == "__lockdown__" → (all False, None) (default для sanitary)
          • preset_id_field == int (валидный ID) → берём из preset_by_id
            (с проверкой scope). v4.7.16: slow_mode_delay тоже из пресета.
          • невалидный ID или несоответствие scope → (None, None) (safe fallback)

        v4.7.16: slow_mode_delay — None = не менять, 0 = выкл, >0 = N сек.
        Копируется в ChatSettings.day_slow_mode_delay / night_mode_slow_mode_delay.
        """
        pid = (preset_id_field or "").strip()
        if pid in ("", "__none__"):
            return None, None
        if pid == "__lockdown__":
            return json.dumps({k: False for k in _ALL_PERM_KEYS}), None
        try:
            pid_int = int(pid)
        except (ValueError, TypeError):
            return None, None
        preset = preset_by_id.get(pid_int)
        if preset is None or preset.scope != scope:
            return None, None
        return preset.permissions, preset.slow_mode_delay

    day_perms_json, day_slow = _resolve_perms(day_preset_id, "day")
    sanitary_perms_json, _sanitary_slow = _resolve_perms(sanitary_preset_id, "sanitary")
    # v4.6.1: night_perms_json — только из night_preset_id.
    night_slow: int | None = None
    if night_preset_id and night_preset_id not in ("", "__none__"):
        night_resolved, night_slow_candidate = _resolve_perms(night_preset_id, "night")
        if night_resolved is not None:
            night_perms_json = night_resolved
            night_slow = night_slow_candidate

    async with async_session() as session:
        cs = (await session.execute(
            select(ChatSettings).where(ChatSettings.chat_id == chat_id)
        )).scalar_one_or_none()
        if cs is None:
            return RedirectResponse(
                url=f"/admin/chats?flash=Chat+{chat_id}+not+found",
                status_code=303,
            )
        # v4.5.1: валидация report_chat_id — должен указывать на чат,
        # помеченный is_report_chat=True (либо None для сброса).
        if rc is not None:
            rc_target = (await session.execute(
                select(ChatSettings).where(ChatSettings.chat_id == rc)
            )).scalar_one_or_none()
            if rc_target is None or not rc_target.is_report_chat:
                return RedirectResponse(
                    url=(
                        f"/admin/chats?flash=Report+chat+{rc}+is+not+marked+"
                        "as+report+chat.+Use+the+%E2%98%86+Make+report+button+"
                        "on+that+chat+first."
                    ),
                    status_code=303,
                )
        cs.hashtag = ht or None
        cs.report_chat_id = rc
        cs.warns_to_mute = wtm if wtm is not None else 0
        cs.mute_duration_seconds = mdb if mdb is not None else 3600
        cs.warns_to_ban = wtb if wtb is not None else 0
        # v4.5.2: новые поля
        cs.warn_decay_days = decay if decay is not None else 0
        cs.link_filter_action = link_filter_action
        cs.night_mode_start = nm_start
        cs.night_mode_end = nm_end
        cs.night_mode_permissions = night_perms_json
        # v4.5.3: расширенная настройка ночного режима.
        cs.night_mode_tz = nm_tz
        cs.night_mode_weekend_start = nm_wknd_start or None
        cs.night_mode_weekend_end = nm_wknd_end or None
        cs.night_mode_notify = (night_mode_notify == "on")
        enter_msg = (night_mode_notify_enter_msg or "").strip()
        exit_msg = (night_mode_notify_exit_msg or "").strip()
        cs.night_mode_notify_enter_msg = enter_msg or None
        cs.night_mode_notify_exit_msg = exit_msg or None
        # v4.5.4 / v4.6.0: sanitary days. Сохраняем monthly JSON (или None).
        cs.sanitary_days = sanitary_days_json
        # v4.6.0: гранулярные права.
        cs.day_permissions = day_perms_json
        cs.sanitary_days_permissions = sanitary_perms_json
        # v4.7.16: slow_mode копируется из пресета (как permissions).
        # None = пресет не выбран → 0 (не менять slow_mode, backward compat).
        # 0 = выкл. >0 = N сек. См. PermissionPreset.slow_mode_delay.
        cs.day_slow_mode_delay = day_slow if day_slow is not None else 0
        cs.night_mode_slow_mode_delay = night_slow if night_slow is not None else 0
        # v4.7.24: via-bot rate-limit settings (toggle ставится отдельно
        # через /toggle поле=via_bot_filter).
        cs.via_bot_rate_limit_seconds = vb_rl if vb_rl is not None else 300
        cs.via_bot_mute_minutes = vb_mm if vb_mm is not None else 10
        # v5.1.0: пусто → дефолт из RULES_URL_DEFAULT (решается на чтении).
        cs.rules_url = rules_url_stripped or None
        cs.updated_at = datetime.now(timezone.utc)
        await session.commit()

    web_app._req_logger.info(
        "admin_chats_update: chat_id=%s updated by=%s (hashtag=%s, "
        "report_chat_id=%s, warns_to_mute=%s, mute_dur=%s, warns_to_ban=%s, "
        "warn_decay=%s, link_filter_action=%s, night=%s-%s [%s], tz=%s, "
        "weekend=%s-%s, notify=%s, sanitary=%s, day_perms=%s, san_perms=%s, "
        "night_preset_id=%s, day_slow=%s, night_slow=%s, "
        "via_bot_rl=%ss, via_bot_mute=%smin)",
        chat_id, _auth.username, ht, rc, wtm, mdb, wtb,
        decay, link_filter_action, nm_start, nm_end,
        night_preset_id or "(none)",
        nm_tz, nm_wknd_start or "-", nm_wknd_end or "-",
        night_mode_notify == "on",
        sanitary_days_json or "(none)",
        "yes" if day_perms_json else "no",
        "yes" if sanitary_perms_json else "no",
        night_preset_id or "(none)",
        day_slow if day_slow is not None else "(unchanged)",
        night_slow if night_slow is not None else "(unchanged)",
        vb_rl if vb_rl is not None else 300,
        vb_mm if vb_mm is not None else 10,
    )
    return RedirectResponse(
        url=f"/admin/chats?flash=Chat+{chat_id}+settings+updated",
        status_code=303,
    )


@router.post("/admin/chats/{chat_id_str}/toggle")
async def admin_chats_toggle(
    chat_id_str: str,
    request: Request,
    _auth: AuthUser = Depends(require_csrf_admin),
    bot=Depends(get_bot),
):
    """v4.4.7: переключает is_enabled / is_report_chat для чата.
    v4.5.2: добавлены toggle для cas, link_filter, night_mode.
    v4.7.2: добавлен toggle для sanitary_days.
    v4.7.6: упразднён toggle 'private' (система private/non-private удалена).
    v4.7.24: добавлен toggle для via_bot_filter (rate-limit «via @Bot»).
    v4.8.0: добавлен toggle для mod_chat (взаимоисключение с report_chat).
    v5.3.0: добавлен toggle для channel_filter (удаление сообщений от имени
    чужих каналов). Свои — сама группа и связанный канал — этим тумблером
    не управляются, они защищены безусловно (_channel_guard_reason).

    Поле form: field=enabled|report_chat|cas|link_filter|night_mode|sanitary_days|via_bot_filter|mod_chat|channel_filter — что переключать.
    """
    try:
        chat_id = int(chat_id_str)
    except (ValueError, TypeError):
        return RedirectResponse(
            url="/admin/chats?flash=Invalid+chat_id",
            status_code=303,
        )
    form = await request.form()
    field = (form.get("field") or "").strip().lower()
    valid_fields = {"enabled", "report_chat", "cas", "link_filter", "night_mode",
                    "sanitary_days", "via_bot_filter", "mod_chat",
                    # v5.3.0: удаление сообщений от имени чужих каналов.
                    "channel_filter"}
    if field not in valid_fields:
        return RedirectResponse(
            url="/admin/chats?flash=Invalid+toggle+field",
            status_code=303,
        )

    async with async_session() as session:
        cs = (await session.execute(
            select(ChatSettings).where(ChatSettings.chat_id == chat_id)
        )).scalar_one_or_none()
        if cs is None:
            return RedirectResponse(
                url=f"/admin/chats?flash=Chat+{chat_id}+not+found",
                status_code=303,
            )
        if field == "enabled":
            cs.is_enabled = not cs.is_enabled
            msg = f"Chat+{chat_id}+{'enabled' if cs.is_enabled else 'disabled'}"
        elif field == "cas":
            cs.cas_check_enabled = not cs.cas_check_enabled
            msg = f"Chat+{chat_id}+CAS+{'enabled' if cs.cas_check_enabled else 'disabled'}"
        elif field == "channel_filter":
            # v5.3.0: удаление сообщений от имени чужих каналов.
            # Свои (сама группа, связанный канал) защищены безусловно и
            # этим тумблером не управляются — см. _channel_guard_reason.
            cs.delete_channel_messages = not cs.delete_channel_messages
            msg = (f"Chat+{chat_id}+Channel+filter+"
                   f"{'enabled' if cs.delete_channel_messages else 'disabled'}")
        elif field == "link_filter":
            cs.link_filter_enabled = not cs.link_filter_enabled
            msg = f"Chat+{chat_id}+Link+filter+{'enabled' if cs.link_filter_enabled else 'disabled'}"
        elif field == "night_mode":
            cs.night_mode_enabled = not cs.night_mode_enabled
            if not cs.night_mode_enabled:
                # v4.7.2: при выключении — снимаем active, но НЕ выходим
                # через _exit_night_mode (это требует bot instance и может
                # затормозить). Tick сам увидит enabled=False и не тронет.
                # Если режим сейчас активен, восстановление прав произойдёт
                # через _exit_night_mode при следующем tick (он проверяет
                # enabled и пропустит, оставив active=True). Поэтому тут
                # явно сбрасываем active=False чтобы UI был консистентен,
                # но права в TG останутся night до вмешательства SU.
                # Лучше: дёрнуть _exit_night_mode если бот доступен.
                if cs.night_mode_currently_active and bot is not None:
                    try:
                        # v4.8.9: app_state вместо `from bot import`
                        from app_state import get_exit_night_mode
                        _exit_night_mode = get_exit_night_mode()
                        await _exit_night_mode(cs)
                        # Re-fetch т.к. _exit_night_mode коммитил.
                        await session.refresh(cs)
                    except Exception as e:
                        web_app._req_logger.warning(
                            "toggle night_mode off: exit failed for chat %s: %s",
                            chat_id, e,
                        )
                        cs.night_mode_currently_active = False
                else:
                    cs.night_mode_currently_active = False
            msg = f"Chat+{chat_id}+Night+mode+{'enabled' if cs.night_mode_enabled else 'disabled'}"
        elif field == "sanitary_days":
            # v4.7.2: явный toggle для санитарных дней.
            cs.sanitary_days_enabled = not cs.sanitary_days_enabled
            if not cs.sanitary_days_enabled and cs.sanitary_days_currently_active:
                # Выходим из sanitary day если он сейчас активен.
                if bot is not None:
                    try:
                        # v4.8.9: app_state вместо `from bot import`
                        from app_state import get_exit_sanitary_day
                        _exit_sanitary_day = get_exit_sanitary_day()
                        await _exit_sanitary_day(cs)
                        await session.refresh(cs)
                    except Exception as e:
                        web_app._req_logger.warning(
                            "toggle sanitary off: exit failed for chat %s: %s",
                            chat_id, e,
                        )
                        cs.sanitary_days_currently_active = False
                else:
                    cs.sanitary_days_currently_active = False
            msg = f"Chat+{chat_id}+Sanitary+days+{'enabled' if cs.sanitary_days_enabled else 'disabled'}"
        elif field == "via_bot_filter":
            # v4.7.24: toggle for via-bot rate-limit filter.
            # Включает/выключает фильтр «via @Bot» сообщений. Настройки
            # (rate_limit, mute_minutes) сохраняются в /update отдельно.
            cs.via_bot_filter_enabled = not cs.via_bot_filter_enabled
            msg = (
                f"Chat+{chat_id}+Via-bot+filter+"
                f"{'enabled' if cs.via_bot_filter_enabled else 'disabled'}"
            )
        elif field == "report_chat":
            if cs.is_report_chat:
                cs.is_report_chat = False
                msg = f"Chat+{chat_id}+no+longer+report+chat"
            elif cs.is_mod_chat:
                # v4.8.0: взаимоисключение — нельзя быть report_chat и
                # mod_chat одновременно. Если чат сейчас mod_chat — отказ.
                msg = (
                    f"Chat+{chat_id}+is+mod+chat"
                    f"%3B+cannot+be+report+chat+too"
                )
            else:
                # Снимаем флаг с других чатов (репорт-чат может быть только один)
                others = (await session.execute(
                    select(ChatSettings).where(
                        ChatSettings.is_report_chat.is_(True),
                        ChatSettings.chat_id != chat_id,
                    )
                )).scalars().all()
                for o in others:
                    o.is_report_chat = False
                cs.is_report_chat = True
                msg = f"Chat+{chat_id}+is+now+the+report+chat"
        # v4.8.0: mod_chat toggle — взаимоисключение с report_chat.
        # Нельзя быть одновременно report_chat и mod_chat (разные цели:
        # report_chat — журнал санкций с rich-превью, modchat — оперативные
        # оповещения для дежурного модератора в кратком формате).
        # Если чат уже report_chat — отказ (UI должен скрывать кнопку,
        # но проверка на бэке обязательна для безопасности).
        if field == "mod_chat":
            if cs.is_report_chat:
                msg = (
                    f"Chat+{chat_id}+is+report+chat"
                    f"%3B+cannot+be+mod+chat+too"
                )
            elif cs.is_mod_chat:
                cs.is_mod_chat = False
                cs.mod_chat_id = None
                msg = f"Chat+{chat_id}+no+longer+mod+chat"
            else:
                # Снимаем флаг с других чатов (modchat тоже может быть
                # только один — по аналогии с report_chat).
                others = (await session.execute(
                    select(ChatSettings).where(
                        ChatSettings.is_mod_chat.is_(True),
                        ChatSettings.chat_id != chat_id,
                    )
                )).scalars().all()
                for o in others:
                    o.is_mod_chat = False
                    o.mod_chat_id = None
                cs.is_mod_chat = True
                cs.mod_chat_id = chat_id
                msg = f"Chat+{chat_id}+is+now+the+mod+chat"
        cs.updated_at = datetime.now(timezone.utc)
        await session.commit()
    web_app._req_logger.info(
        "admin_chats_toggle: chat_id=%s field=%s by=%s",
        chat_id, field, _auth.username,
    )
    return RedirectResponse(url=f"/admin/chats?flash={msg}", status_code=303)


# ──────────────────────────────────────────────────────────────────
#  /admin/chats/{chat_id}/delete — v4.4.8: удалить чат полностью.
#
#  Бот ЛИВАЕТ из чата через bot.leave_chat (best-effort — если бот уже
#  не в чате,_telegram вернёт ошибку, мы её просто логируем).
#  Из БД удаляются:
#    • chat_settings — настройки чата
#    • chat_admins — связи модераторов с этим чатом
#    • punishments — история наказаний в этом чате
#
#  Ограничения:
#    • Нельзя удалить chat_id=0 (глобальные дефолтные настройки).
#    • Доступ: require_admin (как и остальные /admin/chats/* маршруты).
# ──────────────────────────────────────────────────────────────────
@router.post("/admin/chats/{chat_id_str}/delete")
async def admin_chats_delete(
    chat_id_str: str,
    _auth: AuthUser = Depends(require_csrf_admin),
    bot=Depends(get_bot),
):
    """v4.4.8: полностью удаляет чат. Бот ливает, записи из БД чистятся."""
    try:
        chat_id = int(chat_id_str)
    except (ValueError, TypeError):
        return RedirectResponse(
            url="/admin/chats?flash=Invalid+chat_id",
            status_code=303,
        )

    # Защита: chat_id=0 — это глобальный дефолт, его нельзя удалять.
    if chat_id == 0:
        return RedirectResponse(
            url="/admin/chats?flash=Cannot+delete+default+settings+(chat_id=0)",
            status_code=303,
        )

    # Считаем что будем удалять (для лога и флэша).
    async with async_session() as session:
        cs = (await session.execute(
            select(ChatSettings).where(ChatSettings.chat_id == chat_id)
        )).scalar_one_or_none()
        if cs is None:
            return RedirectResponse(
                url=f"/admin/chats?flash=Chat+{chat_id}+not+found",
                status_code=303,
            )

        pun_count = (await session.execute(
            select(func.count(Punishment.id)).where(Punishment.chat_id == chat_id)
        )).scalar() or 0
        ca_count = (await session.execute(
            select(func.count(ChatAdmin.id)).where(ChatAdmin.chat_id == chat_id)
        )).scalar() or 0

        chat_title = cs.title or "(no title)"

        # 1. Удаляем punishments для этого чата.
        if pun_count:
            await session.execute(
                Punishment.__table__.delete().where(Punishment.chat_id == chat_id)
            )
        # 2. Удаляем chat_admins для этого чата.
        if ca_count:
            await session.execute(
                ChatAdmin.__table__.delete().where(ChatAdmin.chat_id == chat_id)
            )
        # 3. Удаляем саму chat_settings.
        await session.execute(
            ChatSettings.__table__.delete().where(ChatSettings.chat_id == chat_id)
        )
        await session.commit()

    # 4. Лучше-эффорт: бот ливает из чата.
    #    Если бот уже не в чате / нет прав / chat_id невалидный —
    #    Telegram вернёт BadRequest, мы его просто логируем.
    leave_msg = ""
    if bot is not None:
        try:
            # v4.10.3 (Task 4): через tg_safe_call — при 429 бот остался бы
            # в чате, хотя из панели чат уже удалён: состояние разъезжается,
            # а повторить операцию неоткуда, кнопка уже отработала.
            from bot_handlers import tg_safe_call
            await tg_safe_call(
                lambda: bot.leave_chat(chat_id=chat_id),
                label="admin_chats_leave",
            )
            leave_msg = "+bot+left"
            web_app._req_logger.info("admin_chats_delete: bot left chat_id=%s", chat_id)
        except TelegramBadRequest as e:
            web_app._req_logger.warning(
                "admin_chats_delete: bot.leave_chat(%s) failed: %s",
                chat_id, e,
            )
            leave_msg = "+bot+leave+failed+(already+not+in+chat?)"
        except Exception as e:
            web_app._req_logger.warning(
                "admin_chats_delete: bot.leave_chat(%s) unexpected error: %s",
                chat_id, e,
            )
            leave_msg = "+bot+leave+error"
    else:
        leave_msg = "+no+bot+instance"

    msg = (
        f"Chat+{chat_id}+({chat_title.replace(' ', '+')})+deleted+"
        f"({pun_count}+punishments,+{ca_count}+admins){leave_msg}"
    )
    web_app._req_logger.info(
        "admin_chats_delete: chat_id=%s title='%s' by=%s "
        "(punishments=%s, chat_admins=%s, leave=%s)",
        chat_id, chat_title, _auth.username, pun_count, ca_count, leave_msg,
    )
    return RedirectResponse(url=f"/admin/chats?flash={msg}", status_code=303)


# ──────────────────────────────────────────────────────────────────
#  /admin/chats/{chat_id}/sync-admins — v4.7.0: авто-обнаружение
#  TG-админов чата и создание/обновление WebUser.
#
#  Логика sync (per-chat, по кнопке SU):
#    1. Получаем TG-админов через bot.get_chat_administrators.
#    2. Пропускаем ботов и анонимных (если есть).
#    3. Для каждого TG-админа:
#       a. Если уже есть WebUser с этим tg_user_id:
#          - Если is_pending=True → оставляем как есть (ждёт /start).
#          - Если is_active=True → проверяем роль и chat_admins.
#            Обновляем role: can_promote_members → admin, иначе moderator.
#            (SU-override роль не трогаем — SU всегда SU.)
#          - Гарантируем наличие chat_admins записи (для moderator).
#       b. Если нет WebUser → создаём pending (is_active=False,
#          is_pending=True, без пароля, auto_discovered=True).
#          Логин: @username (если есть) или tg<TGID>.
#          Role: admin если can_promote_members, иначе moderator.
#    4. Для каждого существующего активного WebUser-moderator, привязанного
#       к этому чату через chat_admins, но НЕ найденного среди текущих
#       TG-админов → is_active=False (полная деактивация по решению SU).
#       Admin-роль не понижаем (он может быть админом в других чатах).
#
#  Ограничения:
#    • is_report_chat чаты игнорируются (репорт-чат не модерируется).
#    • Только SU (кнопка доступна только SU в UI).
# ──────────────────────────────────────────────────────────────────
@router.post("/admin/chats/{chat_id_str}/sync-admins")
async def admin_chats_sync_admins(
    chat_id_str: str,
    _auth: AuthUser = Depends(require_csrf_su),
    bot=Depends(get_bot),
):
    """v4.7.0: sync TG-admins of a chat → WebUser (pending or activate)."""
    try:
        chat_id = int(chat_id_str)
    except (ValueError, TypeError):
        return RedirectResponse(
            url="/admin/chats?flash=Invalid+chat_id",
            status_code=303,
        )
    if chat_id == 0:
        return RedirectResponse(
            url="/admin/chats?flash=Cannot+sync+default+settings",
            status_code=303,
        )
    if bot is None:
        return RedirectResponse(
            url="/admin/chats?flash=Bot+instance+not+available",
            status_code=303,
        )

    async with async_session() as session:
        cs = (await session.execute(
            select(ChatSettings).where(ChatSettings.chat_id == chat_id)
        )).scalar_one_or_none()
        if cs is None:
            return RedirectResponse(
                url=f"/admin/chats?flash=Chat+{chat_id}+not+found",
                status_code=303,
            )
        if cs.is_report_chat:
            return RedirectResponse(
                url="/admin/chats?flash=Report+chat+ignored+(no+admins+to+sync)",
                status_code=303,
            )

    # 1. Получаем TG-админов.
    try:
        tg_admins = await bot.get_chat_administrators(chat_id=chat_id)
    except TelegramBadRequest as e:
        web_app._req_logger.warning(
            "sync_admins: get_chat_administrators(%s) failed: %s",
            chat_id, e,
        )
        return RedirectResponse(
            url=f"/admin/chats?flash=Telegram+error:+{str(e).replace(' ', '+')[:200]}",
            status_code=303,
        )
    except Exception as e:
        web_app._req_logger.warning(
            "sync_admins: get_chat_administrators(%s) unexpected: %s",
            chat_id, e,
        )
        return RedirectResponse(
            url=f"/admin/chats?flash=Unexpected+error:+{str(e).replace(' ', '+')[:200]}",
            status_code=303,
        )

    # Фильтруем ботов (is_bot=True) — у нас нет смысла создавать учётки для ботов.
    tg_admins = [a for a in tg_admins if not getattr(a.user, "is_bot", False)]

    # 2. Словарь tg_user_id → (can_promote, tg_user_obj) для удобства.
    tg_admin_map: dict[int, tuple[bool, object]] = {}
    for a in tg_admins:
        uid = getattr(a.user, "id", None)
        if uid is None:
            continue
        # can_promote_members есть и у creator, и у administrator с этим правом.
        can_promote = bool(getattr(a, "can_promote_members", False)) or \
                      getattr(a, "status", "") == "creator"
        tg_admin_map[uid] = (can_promote, a.user)

    # 3. Существующие WebUser по tg_user_id (одним запросом).
    tg_ids = list(tg_admin_map.keys())
    existing_wus: dict[int, WebUser] = {}
    if tg_ids:
        async with async_session() as session:
            rows = (await session.execute(
                select(WebUser).where(WebUser.tg_user_id.in_(tg_ids))
            )).scalars().all()
            for wu in rows:
                existing_wus[wu.tg_user_id] = wu

    # 4. Существующие chat_admins для этого чата (для деактивации отсутствующих).
    async with async_session() as session:
        existing_ca_rows = (await session.execute(
            select(ChatAdmin).where(ChatAdmin.chat_id == chat_id)
        )).scalars().all()
    existing_ca_uids: set[int] = {ca.user_id for ca in existing_ca_rows}

    # 5. Считаем что сделали — для флэша и лога.
    created_pending = 0
    created_admin = 0
    created_moderator = 0
    updated_role = 0
    already_ok = 0
    deactivated = 0

    async with async_session() as session:
        # 5a. Обработка найденных TG-админов.
        for uid, (can_promote, tg_user) in tg_admin_map.items():
            desired_role = "admin" if can_promote else "moderator"
            wu = existing_wus.get(uid)
            if wu is None:
                # Создаём pending.
                tg_username = getattr(tg_user, "username", None)
                if tg_username:
                    login = tg_username.strip().lstrip("@").lower()
                else:
                    login = f"tg{uid}"
                # Гарантируем уникальность логина (если вдруг занят).
                base_login = login
                suffix = 1
                while True:
                    exists = (await session.execute(
                        select(WebUser.id).where(WebUser.username == login)
                    )).first()
                    if not exists:
                        break
                    suffix += 1
                    login = f"{base_login}{suffix}"
                new_wu = WebUser(
                    username=login,
                    password_hash=None,
                    is_su=False,
                    is_active=False,
                    is_pending=True,
                    auto_discovered=True,
                    role=desired_role,
                    tg_user_id=uid,
                    tg_first_name=getattr(tg_user, "first_name", None),
                    tg_last_name=getattr(tg_user, "last_name", None),
                    tg_username=tg_username,
                )
                session.add(new_wu)
                if desired_role == "admin":
                    created_admin += 1
                else:
                    created_moderator += 1
                created_pending += 1
                # Гарантируем chat_admins для moderator.
                if desired_role == "moderator":
                    ca = ChatAdmin(
                        chat_id=chat_id,
                        user_id=uid,
                        added_by=None,
                    )
                    session.add(ca)
            else:
                # WebUser уже есть.
                if wu.is_pending:
                    # Ждёт /start — не трогаем.
                    already_ok += 1
                    # Но роль можем обновить (если изменилась).
                    if not wu.is_su and wu.role != desired_role:
                        wu.role = desired_role
                        updated_role += 1
                    # И chat_admins гарантия.
                    if desired_role == "moderator" and uid not in existing_ca_uids:
                        session.add(ChatAdmin(
                            chat_id=chat_id, user_id=uid, added_by=None,
                        ))
                        existing_ca_uids.add(uid)
                    continue
                if not wu.is_active:
                    # Не pending и не active — деактивирован ранее. Пропускаем
                    # (SU должен сам реактивировать через change-role/deactivate).
                    already_ok += 1
                    continue
                # Активный WebUser.
                if wu.is_su:
                    # SU не трогаем.
                    already_ok += 1
                    continue
                # Обновляем роль если нужно.
                if wu.role != desired_role:
                    wu.role = desired_role
                    updated_role += 1
                    # При повышении moderator→admin — чистим chat_admins
                    # (админу они не нужны).
                    if desired_role == "admin":
                        for ca in (await session.execute(
                            select(ChatAdmin).where(ChatAdmin.user_id == uid)
                        )).scalars().all():
                            await session.delete(ca)
                        existing_ca_uids.discard(uid)
                # Гарантируем chat_admins для moderator.
                if desired_role == "moderator" and uid not in existing_ca_uids:
                    session.add(ChatAdmin(
                        chat_id=chat_id, user_id=uid, added_by=None,
                    ))
                    existing_ca_uids.add(uid)
                already_ok += 1

        # 5b. Деактивация отсутствующих: для каждого uid в existing_ca_uids,
        # которого нет среди текущих TG-админов → если есть WebUser с role=moderator
        # → is_active=False (по решению SU "всегда deact").
        for uid in list(existing_ca_uids):
            if uid in tg_admin_map:
                continue  # всё ещё админ — не трогаем
            wu = (await session.execute(
                select(WebUser).where(WebUser.tg_user_id == uid)
            )).scalar_one_or_none()
            if wu is None or wu.is_su:
                continue
            if wu.role == "moderator" and wu.is_active:
                wu.is_active = False
                deactivated += 1
            # Удаляем chat_admins запись для этого чата (он больше не админ тут).
            for ca in (await session.execute(
                select(ChatAdmin).where(
                    ChatAdmin.chat_id == chat_id,
                    ChatAdmin.user_id == uid,
                )
            )).scalars().all():
                await session.delete(ca)
            existing_ca_uids.discard(uid)

        await session.commit()

    msg_parts = [
        f"created={created_pending}",
        f"(admin={created_admin},mod={created_moderator})",
        f"updated_role={updated_role}",
        f"deactivated={deactivated}",
        f"already_ok={already_ok}",
    ]
    msg = "+".join(msg_parts)
    web_app._req_logger.info(
        "sync_admins: chat_id=%s by=%s — %s",
        chat_id, _auth.username, msg,
    )
    return RedirectResponse(
        url=f"/admin/chats?flash=Sync+{chat_id_str}+done:+{msg.replace(' ', '+')}",
        status_code=303,
    )


# ──────────────────────────────────────────────────────────────────
#  /admin/chats/{chat_id}/sanitary/add — v4.7.6: добавить период
#  санитарных дней через UI (date+time picker).
#
#  Поля формы: start_date (YYYY-MM-DD), end_date (YYYY-MM-DD),
#  start_time (HH:MM, опционально), end_time (HH:MM, опционально).
#  Если время не задано — период full-day (старое поведение).
#
#  Доступ: require_admin (как и другие /admin/chats/*).
# ──────────────────────────────────────────────────────────────────
@router.post("/admin/chats/{chat_id_str}/sanitary/add")
async def admin_chats_sanitary_add(
    chat_id_str: str,
    request: Request,
    _auth: AuthUser = Depends(require_csrf_admin),
):
    """v4.7.6: добавить период санитарных дней."""
    try:
        chat_id = int(chat_id_str)
    except (ValueError, TypeError):
        return RedirectResponse(
            url="/admin/chats?flash=Invalid+chat_id",
            status_code=303,
        )
    form = await request.form()
    start_date = (form.get("start_date") or "").strip()
    end_date = (form.get("end_date") or "").strip()
    start_time = (form.get("start_time") or "").strip() or None
    end_time = (form.get("end_time") or "").strip() or None

    try:
        from bot_handlers import add_sanitary_period
    except ImportError:
        return RedirectResponse(
            url="/admin/chats?flash=Server+error+(bot_handlers+import)",
            status_code=303,
        )

    async with async_session() as session:
        cs = (await session.execute(
            select(ChatSettings).where(ChatSettings.chat_id == chat_id)
        )).scalar_one_or_none()
        if cs is None:
            return RedirectResponse(
                url=f"/admin/chats?flash=Chat+{chat_id}+not+found",
                status_code=303,
            )
        new_json, err = add_sanitary_period(
            cs.sanitary_days, start_date, end_date, start_time, end_time,
        )
        if err:
            return RedirectResponse(
                url=f"/admin/chats?flash=Sanitary+add+failed:+{err.replace(' ', '+')}",
                status_code=303,
            )
        cs.sanitary_days = new_json
        cs.updated_at = datetime.now(timezone.utc)
        await session.commit()

    web_app._req_logger.info(
        "sanitary_add: chat_id=%s by=%s start=%s%s end=%s%s",
        chat_id, _auth.username,
        start_date, f" {start_time}" if start_time else "",
        end_date, f" {end_time}" if end_time else "",
    )
    return RedirectResponse(
        url=f"/admin/chats?flash=Sanitary+period+added+for+chat+{chat_id}",
        status_code=303,
    )


# ──────────────────────────────────────────────────────────────────
#  /admin/chats/{chat_id}/sanitary/{idx}/delete — v4.7.6: удалить период
#  санитарных дней по глобальному индексу.
#
#  idx = позиция в плоском list от parse_sanitary_days_json.
#  Доступ: require_admin.
# ──────────────────────────────────────────────────────────────────
@router.post("/admin/chats/{chat_id_str}/sanitary/{idx_str}/delete")
async def admin_chats_sanitary_delete(
    chat_id_str: str,
    idx_str: str,
    _auth: AuthUser = Depends(require_csrf_admin),
):
    """v4.7.6: удалить период санитарных дней по индексу."""
    try:
        chat_id = int(chat_id_str)
        idx = int(idx_str)
    except (ValueError, TypeError):
        return RedirectResponse(
            url="/admin/chats?flash=Invalid+chat_id+or+index",
            status_code=303,
        )
    try:
        from bot_handlers import delete_sanitary_period
    except ImportError:
        return RedirectResponse(
            url="/admin/chats?flash=Server+error+(bot_handlers+import)",
            status_code=303,
        )

    async with async_session() as session:
        cs = (await session.execute(
            select(ChatSettings).where(ChatSettings.chat_id == chat_id)
        )).scalar_one_or_none()
        if cs is None:
            return RedirectResponse(
                url=f"/admin/chats?flash=Chat+{chat_id}+not+found",
                status_code=303,
            )
        new_json, err = delete_sanitary_period(cs.sanitary_days, idx)
        if err:
            return RedirectResponse(
                url=f"/admin/chats?flash=Sanitary+delete+failed:+{err.replace(' ', '+')}",
                status_code=303,
            )
        cs.sanitary_days = new_json
        cs.updated_at = datetime.now(timezone.utc)
        await session.commit()

    web_app._req_logger.info(
        "sanitary_delete: chat_id=%s by=%s idx=%s",
        chat_id, _auth.username, idx,
    )
    return RedirectResponse(
        url=f"/admin/chats?flash=Sanitary+period+deleted+for+chat+{chat_id}",
        status_code=303,
    )
