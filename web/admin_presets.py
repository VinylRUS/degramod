"""
web/admin_presets.py — управление пресетами прав + word filter + link allowlist (#10).

v4.9.0 (Task 10): вынесены GET /admin/presets, POST /admin/presets/create,
POST /admin/presets/{id}/edit, POST /admin/presets/{id}/delete,
POST /admin/presets/words/add, POST /admin/presets/words/{id}/delete,
POST /admin/presets/links/add и POST /admin/presets/links/{id}/delete из
create_app(). Единая страница /admin/presets управляет тремя глобальными
списками модерации: пресетами прав чата для day/night/sanitary (v4.6.0),
word filter — ban words (v4.7.5) и link allowlist (v4.7.5).

v5.1.0 (Task 8): + POST /admin/presets/bots/add и POST
/admin/presets/bots/{wl_id}/delete — вайтлист ботов (`db.BotWhitelist`,
Task 6), веб-паритет с командами /botallow, /botunallow, /botallowlist
(Task 7). Структура скопирована с link allowlist: chat_id=0 = global,
hard delete, тот же require_csrf_admin.

Хелперы web_app (`_req_logger`, `APP_VERSION`, `APP_RELEASE_DATE`) берутся
через модуль (`web_app._helper`), а не импортом имён: тесты патчат атрибуты
модуля, и при `from web_app import ...` патч промахнулся бы мимо уже
связанного имени.

Три роута (create/edit/delete пресета) делают late `import bot as
_bot_module` внутри тела функции для инвалидации кеша `_DAY_DEFAULT_CACHE`
при изменении пресета "Day default". Импорт остаётся внутри функции — это
защита от циклической зависимости (bot.py импортирует web_app на верхнем
уровне), поднимать его на уровень модуля нельзя.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

import web_app
from db import (
    BotWhitelist,
    ChatSettings,
    LinkAllowlist,
    PermissionPreset,
    WordFilter,
    async_session,
)
from web.deps import APP_VERSION, AuthUser, get_templates, require_admin, require_csrf_admin

router = APIRouter()


# ── v4.6.0: Permission Presets ────────────────────────────────────
@router.get("/admin/presets", response_class=HTMLResponse)
async def admin_presets_page(
    request: Request,
    flash: str = "",
    _auth: AuthUser = Depends(require_admin),
    templates: Jinja2Templates = Depends(get_templates),
):
    """v4.6.0: страница управления пресетами прав для day/night/sanitary.

    v4.7.5: добавлены секции Word filter (ban words) и Link allowlist.
    Теперь страница — единое место для всех «глобальных списков» модерации.

    Доступ: SU + admin (moderator не имеет доступа — ему нечего тут делать).
    """
    async with async_session() as session:
        presets = (await session.execute(
            select(PermissionPreset).order_by(
                PermissionPreset.scope, PermissionPreset.name
            )
        )).scalars().all()

        # v4.7.5: Word filter — все активные паттерны, отсортированы
        # по chat_id (global=0 первым), затем по created_at desc.
        words = (await session.execute(
            select(WordFilter)
            .where(WordFilter.is_active.is_(True))
            .order_by(WordFilter.chat_id.asc(), WordFilter.created_at.desc())
        )).scalars().all()

        # v4.7.5: Link allowlist — все домены, аналогичная сортировка.
        links = (await session.execute(
            select(LinkAllowlist)
            .order_by(LinkAllowlist.chat_id.asc(), LinkAllowlist.created_at.asc())
        )).scalars().all()

        # v4.7.5: список чатов для dropdown в add-формах
        # (chat_id=0 — global; реальные чаты для per-chat правил).
        chats = (await session.execute(
            select(ChatSettings)
            .where(ChatSettings.chat_id != 0)
            .order_by(ChatSettings.title.asc())
        )).scalars().all()

        # v5.1.0: Вайтлист ботов — паритет с /botallow, /botallowlist.
        bot_whitelist = (await session.execute(
            select(BotWhitelist)
            .order_by(BotWhitelist.chat_id.asc(), BotWhitelist.bot_username.asc())
        )).scalars().all()

    # Группируем по scope для UI.
    grouped = {"day": [], "night": [], "sanitary": []}
    for p in presets:
        if p.scope in grouped:
            grouped[p.scope].append(p)

    return templates.TemplateResponse(
        "admin_presets.html",
        {
            "request": request,
            "presets_by_scope": grouped,
            "word_filters": words,
            "link_allowlist": links,
            "bot_whitelist": bot_whitelist,
            "chats": chats,
            "flash": flash,
            "app_version": APP_VERSION,
            "app_release_date": web_app.APP_RELEASE_DATE,
            "auth_user": _auth,
        },
    )


@router.post("/admin/presets/create")
async def admin_presets_create(
    name: str = Form(""),
    scope: str = Form(""),
    perm_can_send_messages: str = Form(""),
    perm_can_send_audios: str = Form(""),
    perm_can_send_documents: str = Form(""),
    perm_can_send_photos: str = Form(""),
    perm_can_send_videos: str = Form(""),
    perm_can_send_video_notes: str = Form(""),
    perm_can_send_voice_notes: str = Form(""),
    perm_can_send_polls: str = Form(""),
    perm_can_send_other_messages: str = Form(""),
    perm_can_add_web_page_previews: str = Form(""),
    perm_can_change_info: str = Form(""),
    perm_can_invite_users: str = Form(""),
    perm_can_pin_messages: str = Form(""),
    # v4.7.16: slow_mode_delay (chat-level, separate from ChatPermissions).
    # Empty/blank = None (не менять slow_mode при применении пресета).
    # 0 = выкл. >0 = N сек. Telegram limit: 0..36400.
    slow_mode_delay: str = Form(""),
    _auth: AuthUser = Depends(require_csrf_admin),
):
    """v4.6.0: создать новый пользовательский пресет. v4.7.16: + slow_mode_delay."""
    name = (name or "").strip()
    if not name or len(name) > 64:
        return RedirectResponse(
            url="/admin/presets?flash=Invalid+preset+name+(1-64+chars)",
            status_code=303,
        )
    if scope not in ("day", "night", "sanitary"):
        return RedirectResponse(
            url="/admin/presets?flash=Invalid+scope",
            status_code=303,
        )

    # v4.7.16: парсим slow_mode_delay. Empty = None (не менять).
    slow_mode_raw = (slow_mode_delay or "").strip()
    slow_mode_value: int | None = None
    if slow_mode_raw:
        try:
            slow_mode_value = int(slow_mode_raw)
        except ValueError:
            return RedirectResponse(
                url="/admin/presets?flash=Invalid+slow_mode_delay+(must+be+integer)",
                status_code=303,
            )
        if slow_mode_value < 0 or slow_mode_value > 36400:
            return RedirectResponse(
                url="/admin/presets?flash=slow_mode_delay+must+be+0..36400",
                status_code=303,
            )

    perms = {
        "can_send_messages":          perm_can_send_messages == "on",
        "can_send_audios":            perm_can_send_audios == "on",
        "can_send_documents":         perm_can_send_documents == "on",
        "can_send_photos":            perm_can_send_photos == "on",
        "can_send_videos":            perm_can_send_videos == "on",
        "can_send_video_notes":       perm_can_send_video_notes == "on",
        "can_send_voice_notes":       perm_can_send_voice_notes == "on",
        "can_send_polls":             perm_can_send_polls == "on",
        "can_send_other_messages":   perm_can_send_other_messages == "on",
        "can_add_web_page_previews": perm_can_add_web_page_previews == "on",
        "can_change_info":            perm_can_change_info == "on",
        "can_invite_users":           perm_can_invite_users == "on",
        "can_pin_messages":           perm_can_pin_messages == "on",
    }

    async with async_session() as session:
        # Уникальность name.
        existing = (await session.execute(
            select(PermissionPreset).where(PermissionPreset.name == name)
        )).scalar_one_or_none()
        if existing is not None:
            return RedirectResponse(
                url=f"/admin/presets?flash=Preset+name+already+exists:+{name.replace(' ', '+')}",
                status_code=303,
            )
        preset = PermissionPreset(
            name=name, scope=scope,
            permissions=json.dumps(perms),
            slow_mode_delay=slow_mode_value,
            is_system=False,
        )
        session.add(preset)
        await session.commit()
        web_app._req_logger.info(
            "presets_create: name=%r scope=%s slow_mode=%s by=%s",
            name, scope, slow_mode_value, _auth.username,
        )

    # v4.7.20: если создали пресет с name="Day default" (маловероятно т.к.
    # системный уже существует, но на всякий случай) — инвалидируем кеш.
    # Lazy import чтобы избежать circular import (bot.py импортирует web_app).
    if name == "Day default" and scope == "day":
        try:
            import bot as _bot_module
            _bot_module._invalidate_day_default_cache()
        except Exception:
            pass  # бот может быть не загружен в тестовом окружении

    return RedirectResponse(
        url=f"/admin/presets?flash=Preset+{name.replace(' ', '+')}+created",
        status_code=303,
    )


# ── v4.7.17: редактирование пресетов ──────────────────────────────
# Раньше пресеты можно было только создать и удалить. Если опечатка в имени,
# лишний чекбокс или забыли slow_mode — единственный путь был удалить и
# пересоздать. Теперь — Edit: меняет name/scope/permissions/slow_mode in-place.
# Системные пресеты нельзя редактировать (как и удалять) — это гарантия того,
# что «Full lockdown» / «Text only» / «Day default» всегда остаются каноничными.
# Уникальность name проверяется с исключением текущего пресета (иначе нельзя
# сохранить пресет, не меняя имя).
# Замечание про chats: чаты, привязанные к пресету, хранят КОПИЮ JSON в
# ChatSettings (day_permissions / night_mode_permissions / ...). Поэтому
# редактирование пресета НЕ затрагивает уже настроенные чаты — это by design,
# как и для удаления. Чтобы обновить права в конкретном чате, нужно
# перенастроить его на странице /admin/chats (или дождаться следующего
# входа/выхода из night mode).
@router.post("/admin/presets/{preset_id:int}/edit")
async def admin_presets_edit(
    preset_id: int,
    name: str = Form(""),
    scope: str = Form(""),
    perm_can_send_messages: str = Form(""),
    perm_can_send_audios: str = Form(""),
    perm_can_send_documents: str = Form(""),
    perm_can_send_photos: str = Form(""),
    perm_can_send_videos: str = Form(""),
    perm_can_send_video_notes: str = Form(""),
    perm_can_send_voice_notes: str = Form(""),
    perm_can_send_polls: str = Form(""),
    perm_can_send_other_messages: str = Form(""),
    perm_can_add_web_page_previews: str = Form(""),
    perm_can_change_info: str = Form(""),
    perm_can_invite_users: str = Form(""),
    perm_can_pin_messages: str = Form(""),
    slow_mode_delay: str = Form(""),
    _auth: AuthUser = Depends(require_csrf_admin),
):
    """v4.7.17: редактировать пресет (name/scope/permissions/slow_mode_delay).

    Системные пресеты редактировать нельзя (как и удалять).
    Уникальность name проверяется с исключением текущего preset_id.
    Валидация полей — идентична admin_presets_create.
    """
    name = (name or "").strip()
    if not name or len(name) > 64:
        return RedirectResponse(
            url="/admin/presets?flash=Invalid+preset+name+(1-64+chars)",
            status_code=303,
        )
    if scope not in ("day", "night", "sanitary"):
        return RedirectResponse(
            url="/admin/presets?flash=Invalid+scope",
            status_code=303,
        )

    # v4.7.16: парсим slow_mode_delay. Empty = None (не менять).
    slow_mode_raw = (slow_mode_delay or "").strip()
    slow_mode_value: int | None = None
    if slow_mode_raw:
        try:
            slow_mode_value = int(slow_mode_raw)
        except ValueError:
            return RedirectResponse(
                url="/admin/presets?flash=Invalid+slow_mode_delay+(must+be+integer)",
                status_code=303,
            )
        if slow_mode_value < 0 or slow_mode_value > 36400:
            return RedirectResponse(
                url="/admin/presets?flash=slow_mode_delay+must+be+0..36400",
                status_code=303,
            )

    perms = {
        "can_send_messages":          perm_can_send_messages == "on",
        "can_send_audios":            perm_can_send_audios == "on",
        "can_send_documents":         perm_can_send_documents == "on",
        "can_send_photos":            perm_can_send_photos == "on",
        "can_send_videos":            perm_can_send_videos == "on",
        "can_send_video_notes":       perm_can_send_video_notes == "on",
        "can_send_voice_notes":       perm_can_send_voice_notes == "on",
        "can_send_polls":             perm_can_send_polls == "on",
        "can_send_other_messages":   perm_can_send_other_messages == "on",
        "can_add_web_page_previews": perm_can_add_web_page_previews == "on",
        "can_change_info":            perm_can_change_info == "on",
        "can_invite_users":           perm_can_invite_users == "on",
        "can_pin_messages":           perm_can_pin_messages == "on",
    }

    async with async_session() as session:
        preset = (await session.execute(
            select(PermissionPreset).where(PermissionPreset.id == preset_id)
        )).scalar_one_or_none()
        if preset is None:
            return RedirectResponse(
                url="/admin/presets?flash=Preset+not+found",
                status_code=303,
            )
        if preset.is_system:
            return RedirectResponse(
                url="/admin/presets?flash=System+presets+cannot+be+edited",
                status_code=303,
            )

        # Уникальность name — исключаем текущий preset_id.
        existing = (await session.execute(
            select(PermissionPreset).where(
                PermissionPreset.name == name,
                PermissionPreset.id != preset_id,
            )
        )).scalar_one_or_none()
        if existing is not None:
            return RedirectResponse(
                url=f"/admin/presets?flash=Preset+name+already+exists:+{name.replace(' ', '+')}",
                status_code=303,
            )

        old_name = preset.name
        old_scope = preset.scope
        preset.name = name
        preset.scope = scope
        preset.permissions = json.dumps(perms)
        preset.slow_mode_delay = slow_mode_value
        # updated_at апдейтится автоматически через onupdate=lambda.
        await session.commit()
        web_app._req_logger.info(
            "presets_edit: id=%d name=%r->%r scope=%s->%s slow_mode=%s by=%s",
            preset_id, old_name, name, old_scope, scope,
            slow_mode_value, _auth.username,
        )

    # v4.7.20: если изменился name/scope/permissions пресета с name="Day default"
    # (или newName="Day default") — инвалидируем кеш _DAY_DEFAULT_CACHE.
    # Системные пресеты редактировать нельзя, но на всякий случай.
    if (old_name == "Day default" or name == "Day default") and (old_scope == "day" or scope == "day"):
        try:
            import bot as _bot_module
            _bot_module._invalidate_day_default_cache()
        except Exception:
            pass

    return RedirectResponse(
        url=f"/admin/presets?flash=Preset+{name.replace(' ', '+')}+updated",
        status_code=303,
    )


@router.post("/admin/presets/{preset_id:int}/delete")
async def admin_presets_delete(
    preset_id: int,
    _auth: AuthUser = Depends(require_csrf_admin),
):
    """v4.6.0: удалить пользовательский пресет. Системные неудаляемы."""
    async with async_session() as session:
        preset = (await session.execute(
            select(PermissionPreset).where(PermissionPreset.id == preset_id)
        )).scalar_one_or_none()
        if preset is None:
            return RedirectResponse(
                url="/admin/presets?flash=Preset+not+found",
                status_code=303,
            )
        if preset.is_system:
            return RedirectResponse(
                url="/admin/presets?flash=System+presets+cannot+be+deleted",
                status_code=303,
            )
        name = preset.name
        scope = preset.scope
        await session.delete(preset)
        await session.commit()
        web_app._req_logger.info(
            "presets_delete: id=%d name=%r by=%s",
            preset_id, name, _auth.username,
        )

    # v4.7.20: если удалили пресет с name="Day default" (пользовательский,
    # не системный) — инвалидируем кеш _DAY_DEFAULT_CACHE.
    if name == "Day default" and scope == "day":
        try:
            import bot as _bot_module
            _bot_module._invalidate_day_default_cache()
        except Exception:
            pass

    return RedirectResponse(
        url=f"/admin/presets?flash=Preset+{name.replace(' ', '+')}+deleted",
        status_code=303,
    )


# ── v4.7.5: Word filter (ban words) CRUD ─────────────────────────
# v4.8.6: stub bot-команды /addword /delword /listwords удалены.
# Word filter теперь управляется только через этот web UI.
# chat_id=0 — глобальный паттерн (применяется ко всем чатам).
# is_regex=True — pattern интерпретируется как re.search, иначе — case-insensitive substring.
# action: delete|warn|mute|ban.
# ──────────────────────────────────────────────────────────────────
@router.post("/admin/presets/words/add")
async def admin_presets_words_add(
    chat_id: str = Form("0"),
    pattern: str = Form(""),
    is_regex: str = Form(""),
    action: str = Form("delete"),
    _auth: AuthUser = Depends(require_csrf_admin),
):
    """v4.7.5: добавить паттерн в word filter через веб-панель.

    Валидация:
      • pattern — непустой, 1-255 символов.
      • is_regex=True → pattern должен компилироваться re.compile без ошибок.
      • action ∈ {delete, warn, mute, ban}.
      • chat_id — число (0 для global).
      • Дубликат (chat_id + pattern + is_active=True) — обновляем action/is_regex.
    """
    # Парсим chat_id
    chat_id = (chat_id or "0").strip()
    try:
        chat_id_int = int(chat_id)
    except ValueError:
        return RedirectResponse(
            url="/admin/presets?flash=Invalid+chat_id+(must+be+number+or+0+for+global)",
            status_code=303,
        )

    pattern = (pattern or "").strip()
    if not pattern or len(pattern) > 255:
        return RedirectResponse(
            url="/admin/presets?flash=Invalid+pattern+(1-255+chars)",
            status_code=303,
        )

    is_regex_bool = is_regex == "on"

    if action not in ("delete", "warn", "mute", "ban"):
        return RedirectResponse(
            url="/admin/presets?flash=Invalid+action",
            status_code=303,
        )

    if is_regex_bool:
        import re as _re
        try:
            _re.compile(pattern)
        except _re.error as e:
            return RedirectResponse(
                url=f"/admin/presets?flash=Invalid+regex:+{str(e).replace(' ', '+')[:80]}",
                status_code=303,
            )

    async with async_session() as session:
        existing = (await session.execute(
            select(WordFilter).where(
                WordFilter.chat_id == chat_id_int,
                WordFilter.pattern == pattern,
                WordFilter.is_active.is_(True),
            )
        )).scalar_one_or_none()
        if existing:
            existing.action = action
            existing.is_regex = is_regex_bool
            await session.commit()
            web_app._req_logger.info(
                "wordfilter_add (update existing): chat_id=%d pattern=%r by=%s",
                chat_id_int, pattern, _auth.username,
            )
            return RedirectResponse(
                url=f"/admin/presets?flash=Pattern+updated:+{pattern[:40].replace(' ', '+')}",
                status_code=303,
            )
        session.add(WordFilter(
            chat_id=chat_id_int,
            pattern=pattern,
            is_regex=is_regex_bool,
            action=action,
            created_by=_auth.tg_user_id,
        ))
        await session.commit()
        web_app._req_logger.info(
            "wordfilter_add: chat_id=%d pattern=%r action=%s regex=%s by=%s",
            chat_id_int, pattern, action, is_regex_bool, _auth.username,
        )

    return RedirectResponse(
        url=f"/admin/presets?flash=Pattern+added:+{pattern[:40].replace(' ', '+')}",
        status_code=303,
    )


@router.post("/admin/presets/words/{word_id:int}/delete")
async def admin_presets_words_delete(
    word_id: int,
    _auth: AuthUser = Depends(require_csrf_admin),
):
    """v4.7.5: удалить паттерн из word filter (soft-delete — is_active=False).

    Soft-delete выбран для паритета с /delword (команда ботa тоже ставит
    is_active=False). Это сохраняет историю добавлений для аудита.
    """
    async with async_session() as session:
        wf = (await session.execute(
            select(WordFilter).where(WordFilter.id == word_id)
        )).scalar_one_or_none()
        if wf is None:
            return RedirectResponse(
                url="/admin/presets?flash=Pattern+not+found",
                status_code=303,
            )
        if not wf.is_active:
            return RedirectResponse(
                url="/admin/presets?flash=Pattern+already+deleted",
                status_code=303,
            )
        wf.is_active = False
        await session.commit()
        web_app._req_logger.info(
            "wordfilter_delete: id=%d pattern=%r chat_id=%d by=%s",
            word_id, wf.pattern, wf.chat_id, _auth.username,
        )

    return RedirectResponse(
        url=f"/admin/presets?flash=Pattern+deleted:+{wf.pattern[:40].replace(' ', '+')}",
        status_code=303,
    )


# ── v4.7.5: Link allowlist CRUD ──────────────────────────────────
# Паритет с командами /linkallow, /linkallowlist.
# chat_id=0 — глобальный allowlist. Сравнение по подстроке домена.
# ──────────────────────────────────────────────────────────────────
@router.post("/admin/presets/links/add")
async def admin_presets_links_add(
    chat_id: str = Form("0"),
    domain: str = Form(""),
    _auth: AuthUser = Depends(require_csrf_admin),
):
    """v4.7.5: добавить домен в link allowlist через веб-панель.

    Валидация (паритет с /linkallow):
      • domain — непустой, после нормализации должен содержать точку.
      • Убирается scheme и path если есть.
      • Дубликат (chat_id + domain) — отказ с flash.
    """
    chat_id = (chat_id or "0").strip()
    try:
        chat_id_int = int(chat_id)
    except ValueError:
        return RedirectResponse(
            url="/admin/presets?flash=Invalid+chat_id",
            status_code=303,
        )

    domain = (domain or "").strip().lower()
    # Нормализация: убираем scheme если есть
    if "://" in domain:
        try:
            from urllib.parse import urlparse
            parsed = urlparse(domain)
            domain = parsed.netloc.lower()
        except ValueError:
            pass
    domain = domain.lstrip("@").strip("/")
    if not domain or "." not in domain:
        return RedirectResponse(
            url="/admin/presets?flash=Invalid+domain+(must+contain+dot,+e.g.+t.me)",
            status_code=303,
        )

    async with async_session() as session:
        existing = (await session.execute(
            select(LinkAllowlist).where(
                LinkAllowlist.chat_id == chat_id_int,
                LinkAllowlist.domain == domain,
            )
        )).scalar_one_or_none()
        if existing:
            return RedirectResponse(
                url=f"/admin/presets?flash=Domain+already+in+allowlist:+{domain}",
                status_code=303,
            )
        session.add(LinkAllowlist(
            chat_id=chat_id_int,
            domain=domain,
            created_by=_auth.tg_user_id,
        ))
        await session.commit()
        web_app._req_logger.info(
            "linkallowlist_add: chat_id=%d domain=%r by=%s",
            chat_id_int, domain, _auth.username,
        )

    return RedirectResponse(
        url=f"/admin/presets?flash=Domain+added:+{domain}",
        status_code=303,
    )


@router.post("/admin/presets/links/{link_id:int}/delete")
async def admin_presets_links_delete(
    link_id: int,
    _auth: AuthUser = Depends(require_csrf_admin),
):
    """v4.7.5: удалить домен из link allowlist (hard delete).

    Hard delete — потому что в таблице link_allowlist нет is_active флага
    (в отличие от WordFilter). Это паритет с тем, как если бы строку
    удалили через прямой SQL.
    """
    async with async_session() as session:
        link = (await session.execute(
            select(LinkAllowlist).where(LinkAllowlist.id == link_id)
        )).scalar_one_or_none()
        if link is None:
            return RedirectResponse(
                url="/admin/presets?flash=Domain+not+found",
                status_code=303,
            )
        domain = link.domain
        chat_id = link.chat_id
        await session.delete(link)
        await session.commit()
        web_app._req_logger.info(
            "linkallowlist_delete: id=%d domain=%r chat_id=%d by=%s",
            link_id, domain, chat_id, _auth.username,
        )

    return RedirectResponse(
        url=f"/admin/presets?flash=Domain+removed:+{domain}",
        status_code=303,
    )


# ── v5.1.0: Вайтлист ботов (Task 8) ──────────────────────────────
# Паритет с командами /botallow, /botunallow, /botallowlist (Task 7).
# chat_id=0 — глобальный вайтлист. bot_id заполняется ботом оппортунистически
# при первой встрече в message.via_bot — из веба его не задают.
# ──────────────────────────────────────────────────────────────────
@router.post("/admin/presets/bots/add")
async def admin_presets_bots_add(
    chat_id_str: str = Form("0"),
    bot_username: str = Form(""),
    note: str = Form(""),
    _auth: AuthUser = Depends(require_csrf_admin),
):
    """v5.1.0: добавить бота в вайтлист via-bot фильтра.

    chat_id_str = "0" означает global — паритет с link allowlist.
    """
    try:
        chat_id_int = int((chat_id_str or "0").strip())
    except ValueError:
        return RedirectResponse(
            url="/admin/presets?flash=Invalid+chat_id", status_code=303,
        )

    username = (bot_username or "").strip().lstrip("@").lower()
    if not username:
        return RedirectResponse(
            url="/admin/presets?flash=Bot+username+required", status_code=303,
        )

    async with async_session() as session:
        existing = (await session.execute(
            select(BotWhitelist).where(
                BotWhitelist.chat_id == chat_id_int,
                func.lower(BotWhitelist.bot_username) == username,
            )
        )).scalar_one_or_none()
        if existing:
            return RedirectResponse(
                url=f"/admin/presets?flash=Bot+already+whitelisted:+{username}",
                status_code=303,
            )
        session.add(BotWhitelist(
            chat_id=chat_id_int,
            bot_username=username,
            note=(note or "").strip() or None,
            added_by_mod_id=_auth.tg_user_id,
        ))
        await session.commit()
        web_app._req_logger.info(
            "bot_whitelist_add: chat_id=%d bot=%r by=%s",
            chat_id_int, username, _auth.username,
        )

    return RedirectResponse(
        url=f"/admin/presets?flash=Bot+whitelisted:+{username}", status_code=303,
    )


@router.post("/admin/presets/bots/{wl_id:int}/delete")
async def admin_presets_bots_delete(
    wl_id: int,
    _auth: AuthUser = Depends(require_csrf_admin),
):
    """v5.1.0: убрать бота из вайтлиста (hard delete, как у link allowlist)."""
    async with async_session() as session:
        row = (await session.execute(
            select(BotWhitelist).where(BotWhitelist.id == wl_id)
        )).scalar_one_or_none()
        if row is None:
            return RedirectResponse(
                url="/admin/presets?flash=Bot+not+found", status_code=303,
            )
        username = row.bot_username
        await session.delete(row)
        await session.commit()
        web_app._req_logger.info(
            "bot_whitelist_delete: id=%d bot=%r by=%s",
            wl_id, username, _auth.username,
        )

    return RedirectResponse(
        url=f"/admin/presets?flash=Bot+removed:+{username}", status_code=303,
    )
