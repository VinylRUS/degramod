"""
web/me.py — роуты личного профиля пользователя.

Домен: / (root redirect), /avatar/{tg_user_id}, /dashboard, /user/{user_id},
/me, /me/password, /me/avatar/refresh — путь пользователя от входа до
дашборда, карточки конкретного нарушителя и собственного профиля.

v4.8.10: перенесены / (root) и /avatar/{tg_user_id}.
v4.9.0 (Task 8): перенесены /dashboard, /user/{user_id}, /me, /me/password,
/me/avatar/refresh. Хелперы и константы web_app (`_avatar_url`,
`_fetch_and_save_avatar`, `_req_logger`, `PAGE_SIZE`, `WEB_PUBLIC_URL`)
берутся через модуль (`web_app._helper`), а не импортом имён — тесты
(`tests/test_v45_dashboard.py:547,563`) патчат `web_app._fetch_and_save_avatar`,
и при `from web_app import _fetch_and_save_avatar` патч промахнулся бы мимо
уже связанного имени.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func, select

import web_app
from db import ChatSettings, Moderator, Punishment, User, WebUser, _hash_password, _verify_password, async_session
from web.deps import APP_VERSION, AuthUser, get_bot, get_templates, require_auth, require_csrf_auth
from web_app import _avatar_path

router = APIRouter()


@router.get("/")
async def root():
    """Root → редирект на /login.

    v4.8.10: перенесён из create_app() в web/me.py.
    """
    return RedirectResponse(url="/login", status_code=302)


@router.get("/avatar/{tg_user_id:int}")
async def get_avatar(tg_user_id: int, _auth: AuthUser = Depends(require_auth)):
    """Отдаёт файл аватарки <AVATARS_DIR>/<tg_user_id>.jpg.

    v4.5.1: добавлена проверка require_auth — чтобы посторонние не могли
    перебирать tg_user_id и тащить аватарки.

    v4.8.10: перенесён из create_app() в web/me.py.
    """
    path = _avatar_path(tg_user_id)
    if not os.path.exists(path):
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="image/jpeg")


# ── GET /dashboard ──────────────────────────────────────────────────
# v4.5: урезанный дашборд. Только:
#   • Search user (поиск нарушителя)
#   • 4 stat-карточки: Total / Mutes / Warns / Bans
#   • Recent sanctions — лог с 4 фильтрами (All/Mute/Warn/Ban)
# Убрано: top offenders/moderators, chat-settings (дублировал /admin/chats),
# change-pw (переехал в /me), anchor-nav (нечего навигировать).
#
# v4.9.0 (Task 8): перенесён из create_app() в web/me.py.
@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    page: int = 1,
    action: str = "",
    rev: str = "",          # "all" / "active" / "revoked"; по умолчанию ""
    sort: str = "new",      # "new" / "old" / "type" / "user"
    pw_msg: str = "",       # legacy: оставлен для редиректов от старой /me/password
    _auth: AuthUser = Depends(require_auth),
    templates: Jinja2Templates = Depends(get_templates),
):
    offset = (page - 1) * web_app.PAGE_SIZE

    async with async_session() as session:
        # ── Общая статистика (все время, только активные) ───────────
        total_stmt = (
            select(Punishment.action_type, func.count(Punishment.id))
            .where(Punishment.is_revoked.is_(False))
            .group_by(Punishment.action_type)
        )
        total_result = await session.execute(total_stmt)
        total_stats = {row[0]: row[1] for row in total_result.all()}
        total_all = sum(total_stats.values())

        # ── Лог санкций: базовый запрос + фильтры ────────────────────
        base = (
            select(Punishment, User, Moderator)
            .join(User, Punishment.user_id == User.user_id)
            .join(Moderator, Punishment.mod_id == Moderator.mod_id)
        )

        # Action filter (v4.5: в UI осталось 4 кнопки — All/Mute/Warn/Ban.
        # URL с action=unmute/unwarn/unban по-прежнему работает для прямых
        # ссылок, но в дашборде кнопок для этого нет.)
        if action in ("mute", "warn", "ban", "unmute", "unwarn", "unban"):
            base = base.where(Punishment.action_type == action)

        # Revoked filter: по умолчанию показываем всё ("")
        if rev == "active":
            base = base.where(Punishment.is_revoked.is_(False))
        elif rev == "revoked":
            base = base.where(Punishment.is_revoked.is_(True))

        # Sorting
        if sort == "old":
            base = base.order_by(Punishment.created_at.asc())
        elif sort == "type":
            base = base.order_by(Punishment.action_type.asc(),
                                 Punishment.created_at.desc())
        elif sort == "user":
            base = base.order_by(User.username.asc().nullslast(),
                                 Punishment.created_at.desc())
        else:  # "new" / default
            base = base.order_by(Punishment.created_at.desc())

        # ── Count total для пагинации (с теми же фильтрами) ─────────
        count_base = (
            select(func.count(Punishment.id))
            .join(User, Punishment.user_id == User.user_id)
        )
        if action in ("mute", "warn", "ban", "unmute", "unwarn", "unban"):
            count_base = count_base.where(Punishment.action_type == action)
        if rev == "active":
            count_base = count_base.where(Punishment.is_revoked.is_(False))
        elif rev == "revoked":
            count_base = count_base.where(Punishment.is_revoked.is_(True))
        total_row_count = (await session.execute(count_base)).scalar() or 0
        total_pages = max(1, (total_row_count + web_app.PAGE_SIZE - 1) // web_app.PAGE_SIZE)

        rows = (await session.execute(
            base.offset(offset).limit(web_app.PAGE_SIZE)
        )).all()

        # ── v4.6.0: Warnings card ────────────────────────────────────
        # Собираем предупреждения со всех чатов, сортируем по важности:
        # 1. critical (нет chat_settings — неожиданное) — приоритет 0
        # 2. no_sanitary_next_month — приоритет 10
        # 3. no_bot_rights — приоритет 20
        # 4. other (e.g. chat disabled) — приоритет 30
        warnings = []
        from datetime import datetime as _dt
        from datetime import timezone as _tz
        from zoneinfo import ZoneInfo as _ZI
        try:
            now_msk = _dt.now(_ZI("Europe/Moscow"))
        except Exception:
            now_msk = _dt.now(_tz.utc)
        current_month = now_msk.strftime("%Y-%m")
        # Следующий месяц (для warning "нет дат на след. месяц").
        if now_msk.month == 12:
            next_month = f"{now_msk.year + 1}-01"
        else:
            next_month = f"{now_msk.year}-{now_msk.month + 1:02d}"
        day_of_month = now_msk.day
        # Получаем все чаты.
        all_chats = (await session.execute(
            select(ChatSettings).where(ChatSettings.chat_id != 0).order_by(ChatSettings.chat_id)
        )).scalars().all()
        for chat in all_chats:
            chat_label = chat.title or f"chat {chat.chat_id}"
            # 1. Sanitary days warning — после 20-го числа, если на след. месяц нет дат
            # и в этом месяце ещё не было сан. дня (last_sanitary_month != current).
            if day_of_month >= 20 and chat.last_sanitary_month != current_month:
                try:
                    sd_data = json.loads(chat.sanitary_days) if chat.sanitary_days else {}
                except (ValueError, TypeError):
                    sd_data = {}
                next_month_pairs = sd_data.get(next_month, []) if isinstance(sd_data, dict) else []
                if not next_month_pairs:
                    warnings.append({
                        "priority": 10,
                        "level": "warn",
                        "chat_id": chat.chat_id,
                        "chat_label": chat_label,
                        "title": "Нет санитарных дней на следующий месяц",
                        "detail": f"Месяц {next_month} не имеет дат санитарных дней. Настройте в /admin/chats → Sanitary days.",
                    })
            # 2. Chat disabled — friendly warning.
            if not chat.is_enabled:
                warnings.append({
                    "priority": 30,
                    "level": "info",
                    "chat_id": chat.chat_id,
                    "chat_label": chat_label,
                    "title": "Чат отключён",
                    "detail": "Бот игнорирует все команды в этом чате. Включите через /admin/chats → ⚙ → is_enabled.",
                })
        # Сортировка по priority.
        warnings.sort(key=lambda w: (w["priority"], w["chat_label"]))

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "rows": rows,
        "total_stats": total_stats,
        "total_all": total_all,
        "page": page,
        "total_pages": total_pages,
        "page_size": web_app.PAGE_SIZE,
        "action_filter": action,
        "rev_filter": rev,
        "sort": sort,
        "auth_user": _auth,
        "app_version": APP_VERSION,
        "pw_msg": pw_msg or None,
        "warnings": warnings,
    })


# ── GET /user/<user_id> ─────────────────────────────────────────────
# v4.9.0 (Task 8): перенесён из create_app() в web/me.py.
@router.get("/user/{user_id:int}", response_class=HTMLResponse)
async def user_page(
    request: Request,
    user_id: int,
    action: str = "",
    rev: str = "",
    _auth: AuthUser = Depends(require_auth),
    templates: Jinja2Templates = Depends(get_templates),
):
    async with async_session() as session:
        user = (await session.execute(
            select(User).where(User.user_id == user_id)
        )).scalar_one_or_none()
        if user is None:
            raise HTTPException(status_code=404)

        # Счётчики: только активные (is_revoked=False)
        count_stmt = (
            select(Punishment.action_type, func.count(Punishment.id))
            .where(
                Punishment.user_id == user_id,
                Punishment.is_revoked.is_(False),
            )
            .group_by(Punishment.action_type)
        )
        counters = {
            row[0]: row[1]
            for row in (await session.execute(count_stmt)).all()
        }

        # Текущие варны (только активные)
        warn_sum_stmt = (
            select(func.coalesce(func.sum(Punishment.duration_seconds), 0))
            .where(
                Punishment.user_id == user_id,
                Punishment.action_type == "warn",
                Punishment.is_revoked.is_(False),
            )
        )
        current_warns = int((await session.execute(warn_sum_stmt)).scalar() or 0)

        # История
        punishment_stmt = (
            select(Punishment, Moderator)
            .join(Moderator, Punishment.mod_id == Moderator.mod_id)
            .where(Punishment.user_id == user_id)
            .order_by(desc(Punishment.created_at))
        )
        if action in ("mute", "warn", "ban", "unmute", "unwarn", "unban"):
            punishment_stmt = punishment_stmt.where(Punishment.action_type == action)
        if rev == "active":
            punishment_stmt = punishment_stmt.where(Punishment.is_revoked.is_(False))
        elif rev == "revoked":
            punishment_stmt = punishment_stmt.where(Punishment.is_revoked.is_(True))
        punishments = (await session.execute(punishment_stmt)).all()

    return templates.TemplateResponse("user.html", {
        "request": request,
        "user": user,
        "counters": counters,
        "current_warns": current_warns,
        "punishments": punishments,
        "action_filter": action,
        "rev_filter": rev,
        "auth_user": _auth,
    })


# ──────────────────────────────────────────────────────────────────
#  /me/password — смена своего пароля (v4.4 → v4.5: переехал из /dashboard в /me)
#  Доступен всем авторизованным, но SU пароль хранится в env — ему форма
#  показывает предупреждение и ничего не делает.
#
#  v4.9.0 (Task 8): перенесён из create_app() в web/me.py.
# ──────────────────────────────────────────────────────────────────
@router.post("/me/password")
async def me_change_password(
    request: Request,
    old_password: str = Form(""),
    new_password: str = Form(""),
    confirm: str = Form(""),
    _auth: AuthUser = Depends(require_csrf_auth),
):
    # SU пароль в env — менять через /me нельзя.
    if _auth.is_su:
        return RedirectResponse(
            url="/me?pw_msg=SU+password+is+managed+via+WEB_PASSWORD+env+variable",
            status_code=303,
        )

    # Валидация
    if len(new_password) < 6:
        return RedirectResponse(
            url="/me?pw_msg=New+password+must+be+at+least+6+chars",
            status_code=303,
        )
    if new_password != confirm:
        return RedirectResponse(
            url="/me?pw_msg=New+password+and+confirmation+do+not+match",
            status_code=303,
        )

    async with async_session() as session:
        wu = (await session.execute(
            select(WebUser).where(WebUser.username == _auth.username)
        )).scalar_one_or_none()
        if wu is None or not wu.is_active or not wu.password_hash:
            return RedirectResponse(
                url="/me?pw_msg=Account+not+found",
                status_code=303,
            )
        # Проверяем старый пароль
        if not _verify_password(old_password, wu.password_hash):
            return RedirectResponse(
                url="/me?pw_msg=Current+password+is+incorrect",
                status_code=303,
            )
        # Проверяем, что новый пароль отличается от старого
        if _verify_password(new_password, wu.password_hash):
            return RedirectResponse(
                url="/me?pw_msg=New+password+must+differ+from+current",
                status_code=303,
            )
        wu.password_hash = _hash_password(new_password)
        await session.commit()

    web_app._req_logger.info(
        "me_change_password: user=%s changed own password",
        _auth.username,
    )
    return RedirectResponse(
        url="/me?pw_msg=Password+changed+successfully",
        status_code=303,
    )


# ──────────────────────────────────────────────────────────────────
#  v4.5: /me — личный профиль пользователя
#
#  Показывает:
#    • Аватарку из TG (большая, 96×96) + кнопку Refresh
#    • Инфу об аккаунте: логин, роль, TG ID, дата создания, последний логин
#    • Для admin/moderator — форму смены пароля
#    • Для SU — предупреждение что пароль в env WEB_PASSWORD
#    • Для moderator — дополнительно инструкцию по смене пароля через DM боту
#
#  v4.9.0 (Task 8): перенесён из create_app() в web/me.py.
# ──────────────────────────────────────────────────────────────────
@router.get("/me", response_class=HTMLResponse)
async def me_profile(
    request: Request,
    pw_msg: str = "",
    _auth: AuthUser = Depends(require_auth),
    templates: Jinja2Templates = Depends(get_templates),
):
    async with async_session() as session:
        wu = (await session.execute(
            select(WebUser).where(WebUser.username == _auth.username)
        )).scalar_one_or_none()
        if wu is None:
            # Сессия валидна, но юзер пропал — редирект на логин
            return RedirectResponse(url="/login", status_code=303)
        # Создаём snapshot со всеми нужными полями (wu может быть удалён из сессии)
        profile = {
            "username": wu.username,
            "role": wu.role or ("su" if wu.is_su else "admin"),
            "is_su": wu.is_su,
            "is_active": wu.is_active,
            "tg_user_id": wu.tg_user_id,
            "tg_first_name": wu.tg_first_name,
            "tg_last_name": wu.tg_last_name,
            "tg_username": wu.tg_username,
            "created_at": wu.created_at,
            "last_login_at": wu.last_login_at,
            "created_by": wu.created_by,
            "avatar_url": web_app._avatar_url(wu.tg_user_id, wu.tg_photo_updated_at),
            "photo_updated_at": wu.tg_photo_updated_at,
        }

    return templates.TemplateResponse("profile.html", {
        "request": request,
        "auth_user": _auth,
        "app_version": APP_VERSION,
        "profile": profile,
        "pw_msg": pw_msg or None,
        "web_public_url": web_app.WEB_PUBLIC_URL,
    })


# v4.9.0 (Task 8): перенесён из create_app() в web/me.py.
@router.post("/me/avatar/refresh")
async def me_avatar_refresh(
    request: Request,
    _auth: AuthUser = Depends(require_csrf_auth),
    bot=Depends(get_bot),
):
    """v4.5: принудительно скачивает аватарку из TG и обновляет timestamp."""
    if not _auth.tg_user_id:
        return RedirectResponse(
            url="/me?pw_msg=No+TG+ID+bound+to+your+account",
            status_code=303,
        )
    if bot is None:
        return RedirectResponse(
            url="/me?pw_msg=Bot+instance+not+available",
            status_code=303,
        )
    ok = await web_app._fetch_and_save_avatar(bot, _auth.tg_user_id)
    if ok:
        async with async_session() as session:
            wu = (await session.execute(
                select(WebUser).where(WebUser.username == _auth.username)
            )).scalar_one_or_none()
            if wu is not None:
                wu.tg_photo_updated_at = datetime.now(timezone.utc)
                await session.commit()
        web_app._req_logger.info(
            "me_avatar_refresh: avatar updated for user=%s tg_id=%s",
            _auth.username, _auth.tg_user_id,
        )
        return RedirectResponse(
            url="/me?pw_msg=Avatar+updated+successfully",
            status_code=303,
        )
    else:
        return RedirectResponse(
            url="/me?pw_msg=Could+not+fetch+avatar+(no+profile+photo+or+API+error)",
            status_code=303,
        )
