"""
web/admin_users.py — управление веб-пользователями (/admin/users*), v4.4.7.

v4.9.0 (Task 9): вынесены восемь роутов из create_app() в web_app.py:
GET /admin/users, POST /admin/users/create, POST /admin/users/{id}/toggle,
POST /admin/users/{id}/reset, POST /admin/users/{id}/role,
POST /admin/users/{id}/edit-chats, POST /admin/users/{id}/bind-tg,
POST /admin/users/{id}/delete.

Объединяет бывш. Admins + Moderators: одна сущность WebUser с ролью
admin/moderator; модераторам при создании можно сразу назначить чаты
(мультивыбор). Доступ — только SU.

Хелперы и константы web_app (`_req_logger`, `_verify_flash`, `_sign_flash`,
`_generate_password`, `_send_admin_welcome`, `_fetch_and_save_avatar`)
берутся через модуль (`web_app._helper`), а не импортом имён — тесты
патчут `web_app._fetch_and_save_avatar` как атрибут модуля
(`tests/test_v45_dashboard.py:547,563`), и при `from web_app import
_fetch_and_save_avatar` патч промахнулся бы мимо уже связанного имени.
`_hash_password` и `async_session` берутся напрямую из `db` — они там и
определены, `web_app` их лишь реэкспортирует (см. `web/me.py`).
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

import web_app
from db import ChatAdmin, ChatSettings, WebUser, _hash_password, async_session
from web.deps import AuthUser, get_bot, get_templates, require_csrf_su, require_su

router = APIRouter()


# ──────────────────────────────────────────────────────────────────
#  /admin/users — управление всеми пользователями (v4.4.7)
#
#  v4.4.7: Объединяет бывш. Admins + Moderators. Создаётся одна
#  сущность WebUser с ролью admin/moderator; модераторам при
#  создании можно сразу назначить чаты (мультивыбор).
#
#  Доступ: только SU. (Admin не может создавать других юзеров.)
# ──────────────────────────────────────────────────────────────────
@router.get("/admin/users", response_class=HTMLResponse)
async def admin_users_page(
    request: Request,
    flash: str = "",
    created: str = "",
    _auth: AuthUser = Depends(require_su),
    templates: Jinja2Templates = Depends(get_templates),
):
    created_info = None
    if created:
        payload = web_app._verify_flash(created, max_age_seconds=180)
        if payload and "u" in payload and "p" in payload:
            created_info = {
                "username": payload["u"],
                "password": payload["p"],
                "tg_user_id": payload.get("tg"),
                "welcome_sent": bool(payload.get("w", 0)),
            }

    async with async_session() as session:
        users = (await session.execute(
            select(WebUser).order_by(WebUser.is_su.desc(), WebUser.created_at.asc())
        )).scalars().all()

        # Для каждого moderator-юзера подгружаем список привязанных чатов
        user_chats: dict[int, list[tuple[int, str | None, str | None]]] = {}
        # {web_user.tg_user_id: [(chat_id, chat_title, hashtag), ...]}
        if users:
            tg_ids = [u.tg_user_id for u in users if u.tg_user_id]
            if tg_ids:
                rows = (await session.execute(
                    select(
                        ChatAdmin.user_id,
                        ChatAdmin.chat_id,
                        ChatSettings.title,
                        ChatSettings.hashtag,
                    )
                    .outerjoin(ChatSettings, ChatAdmin.chat_id == ChatSettings.chat_id)
                    .where(ChatAdmin.user_id.in_(tg_ids))
                    .order_by(ChatAdmin.user_id, ChatAdmin.chat_id)
                )).all()
                for r in rows:
                    user_chats.setdefault(r[0], []).append((r[1], r[2], r[3]))

        # Список всех чатов для мультивыбора (исключая default chat_id=0)
        all_chats = (await session.execute(
            select(ChatSettings.chat_id, ChatSettings.title, ChatSettings.hashtag)
            .where(ChatSettings.chat_id != 0)
            .order_by(ChatSettings.title.asc(), ChatSettings.chat_id.asc())
        )).all()

        # Также подгружаем TG-only модераторов (chat_admins без веб-аккаунта)
        tg_only_moderators = []
        if users:
            web_tg_ids = {u.tg_user_id for u in users if u.tg_user_id}
            ca_rows = (await session.execute(
                select(
                    ChatAdmin.id,
                    ChatAdmin.chat_id,
                    ChatAdmin.user_id,
                    ChatAdmin.created_at,
                    ChatSettings.title.label("chat_title"),
                    ChatSettings.hashtag.label("chat_hashtag"),
                )
                .outerjoin(ChatSettings, ChatAdmin.chat_id == ChatSettings.chat_id)
                .order_by(ChatAdmin.chat_id.asc(), ChatAdmin.user_id.asc())
            )).all()
            for r in ca_rows:
                if r[2] not in web_tg_ids:
                    tg_only_moderators.append(r)

    return templates.TemplateResponse("admin.html", {
        "request": request,
        "web_users": users,
        "user_chats": user_chats,
        "all_chats": all_chats,
        "tg_only_moderators": tg_only_moderators,
        "auth_user": _auth,
        "flash": flash or None,
        "created_info": created_info,
    })


@router.post("/admin/users/create")
async def admin_users_create(
    request: Request,
    tg_user_id: str = Form(""),
    role: str = Form("admin"),
    chat_ids: list[str] = Form(None),
    _auth: AuthUser = Depends(require_csrf_su),
    bot=Depends(get_bot),
):
    """v4.4.7: создаёт веб-пользователя по Telegram ID с ролью admin/moderator.

    role=moderator: дополнительно принимает список chat_ids (мультивыбор) —
    для каждого выбранного чата создаётся запись в chat_admins, что даёт
    модератору право использовать !warn/!mute/!ban в этих чатах.
    role=admin: chat_ids игнорируются (админ имеет права во всех публичных
    чатах автоматически).
    """
    # ── 0. Валидация role ──────────────────────────────────────────
    role = (role or "admin").strip().lower()
    if role not in ("admin", "moderator"):
        return RedirectResponse(
            url="/admin/users?flash=Invalid+role.+Must+be+%27admin%27+or+%27moderator%27",
            status_code=303,
        )

    # ── 1. Валидация TGID ───────────────────────────────────────────
    tg_raw = (tg_user_id or "").strip()
    try:
        tg_id = int(tg_raw)
    except (ValueError, TypeError):
        return RedirectResponse(
            url="/admin/users?flash=Telegram+ID+must+be+a+number",
            status_code=303,
        )
    if tg_id <= 0:
        return RedirectResponse(
            url="/admin/users?flash=Telegram+ID+must+be+positive",
            status_code=303,
        )

    # ── 2. bot должен быть передан ──────────────────────────────────
    if bot is None:
        web_app._req_logger.error("admin_users_create: bot is None — create_app called without bot?")
        return RedirectResponse(
            url="/admin/users?flash=Bot+instance+not+available",
            status_code=303,
        )

    # ── 3. bot.get_chat ─────────────────────────────────────────────
    try:
        chat = await bot.get_chat(chat_id=tg_id)
    except Exception as e:
        web_app._req_logger.warning("admin_users_create: bot.get_chat(%s) failed: %s", tg_id, e)
        return RedirectResponse(
            url="/admin/users?flash=Cannot+fetch+user+from+Telegram.+"
                "The+user+must+have+interacted+with+the+bot+at+least+once+"
                "(sent+%2Fstart+or+any+message).",
            status_code=303,
        )

    # ── 4. Профиль из Chat ──────────────────────────────────────────
    tg_username = getattr(chat, "username", None)
    tg_first_name = getattr(chat, "first_name", None)
    tg_last_name = getattr(chat, "last_name", None)
    if not tg_username:
        return RedirectResponse(
            url=f"/admin/users?flash=User+{tg_id}+has+no+%40username+in+Telegram.+"
                "Cannot+create+login+without+it.",
            status_code=303,
        )
    login = tg_username.strip().lstrip("@").lower()
    if login == "su":
        return RedirectResponse(
            url="/admin/users?flash=%27su%27+is+reserved",
            status_code=303,
        )
    if not (5 <= len(login) <= 32):
        return RedirectResponse(
            url=f"/admin/users?flash=Telegram+username+%27{login}%27+has+invalid+length+"
                f"(must+be+5-32+chars)",
            status_code=303,
        )

    # ── 5. Парсим выбранные чаты (только для moderator) ─────────────
    chosen_chat_ids: list[int] = []
    if role == "moderator" and chat_ids:
        for raw in chat_ids:
            raw = (raw or "").strip()
            if not raw:
                continue
            try:
                cid = int(raw)
                if cid != 0 and cid not in chosen_chat_ids:
                    chosen_chat_ids.append(cid)
            except (ValueError, TypeError):
                pass

    # ── 6. Генерация пароля ─────────────────────────────────────────
    password = web_app._generate_password()

    # ── 7. Сохранение ───────────────────────────────────────────────
    async with async_session() as session:
        existing_by_tg = (await session.execute(
            select(WebUser).where(WebUser.tg_user_id == tg_id)
        )).scalar_one_or_none()
        if existing_by_tg:
            return RedirectResponse(
                url=f"/admin/users?flash=Telegram+ID+{tg_id}+already+bound+to+"
                    f"admin+%27{existing_by_tg.username}%27",
                status_code=303,
            )
        existing_by_login = (await session.execute(
            select(WebUser).where(WebUser.username == login)
        )).scalar_one_or_none()
        if existing_by_login:
            return RedirectResponse(
                url=f"/admin/users?flash=Admin+with+username+%27{login}%27+already+exists",
                status_code=303,
            )

        session.add(WebUser(
            username=login,
            password_hash=_hash_password(password),
            is_su=False,
            is_active=True,
            created_by=_auth.username,
            tg_user_id=tg_id,
            tg_first_name=tg_first_name,
            tg_last_name=tg_last_name,
            tg_username=login,
            role=role,
        ))
        await session.flush()

        # Для moderator — создаём записи в chat_admins
        if role == "moderator" and chosen_chat_ids:
            for cid in chosen_chat_ids:
                # Проверяем что ещё нет такой записи
                existing_ca = (await session.execute(
                    select(ChatAdmin).where(
                        ChatAdmin.chat_id == cid,
                        ChatAdmin.user_id == tg_id,
                    )
                )).scalars().first()
                if existing_ca is None:
                    session.add(ChatAdmin(
                        chat_id=cid,
                        user_id=tg_id,
                        added_by=None,
                    ))
        await session.commit()

    web_app._req_logger.info(
        "admin_users_create: created web_user username=%s tg_user_id=%s role=%s chats=%s by=%s",
        login, tg_id, role, chosen_chat_ids if role == "moderator" else "(all)",
        _auth.username,
    )

    # ── 8. v4.5: Скачиваем аватарку (best-effort) ───────────────────
    avatar_ok = await web_app._fetch_and_save_avatar(bot, tg_id)
    if avatar_ok:
        async with async_session() as session:
            wu_for_avatar = (await session.execute(
                select(WebUser).where(WebUser.tg_user_id == tg_id)
            )).scalar_one_or_none()
            if wu_for_avatar is not None:
                wu_for_avatar.tg_photo_updated_at = datetime.now(timezone.utc)
                await session.commit()
        web_app._req_logger.info("admin_users_create: avatar saved for tg_user_id=%s", tg_id)
    else:
        web_app._req_logger.info("admin_users_create: no avatar for tg_user_id=%s (skipped)", tg_id)

    # ── 9. Welcome-DM ───────────────────────────────────────────────
    welcome_ok, welcome_err = await web_app._send_admin_welcome(
        bot=bot, tg_user_id=tg_id, login=login, password=password,
        first_name=tg_first_name, role=role,
    )
    if welcome_ok:
        web_app._req_logger.info(
            "admin_users_create: welcome sent to tg_user_id=%s (login=%s)",
            tg_id, login,
        )
    else:
        web_app._req_logger.warning(
            "admin_users_create: welcome FAILED for tg_user_id=%s (login=%s): %s",
            tg_id, login, welcome_err,
        )

    flash_token = web_app._sign_flash({
        "u": login, "p": password, "tg": tg_id,
        "t": int(time.time()),
        "w": 1 if welcome_ok else 0,
    })
    return RedirectResponse(url=f"/admin/users?created={flash_token}", status_code=303)


@router.post("/admin/users/{user_id:int}/toggle")
async def admin_users_toggle(
    user_id: int,
    _auth: AuthUser = Depends(require_csrf_su),
):
    async with async_session() as session:
        wu = (await session.execute(
            select(WebUser).where(WebUser.id == user_id)
        )).scalar_one_or_none()
        if wu is None:
            return RedirectResponse(url="/admin/users", status_code=303)
        if wu.is_su:
            return RedirectResponse(url="/admin/users", status_code=303)
        wu.is_active = not wu.is_active
        await session.commit()
    return RedirectResponse(url="/admin/users", status_code=303)


@router.post("/admin/users/{user_id:int}/reset")
async def admin_users_reset(
    request: Request,
    user_id: int,
    password: str = Form(""),
    _auth: AuthUser = Depends(require_csrf_su),
):
    if len(password) < 6:
        return RedirectResponse(url="/admin/users?flash=Password+must+be+at+least+6+chars", status_code=303)
    async with async_session() as session:
        wu = (await session.execute(
            select(WebUser).where(WebUser.id == user_id)
        )).scalar_one_or_none()
        if wu is None or wu.is_su:
            return RedirectResponse(url="/admin/users", status_code=303)
        wu.password_hash = _hash_password(password)
        await session.commit()
    return RedirectResponse(url="/admin/users", status_code=303)


@router.post("/admin/users/{user_id:int}/role")
async def admin_users_change_role(
    user_id: int,
    request: Request,
    _auth: AuthUser = Depends(require_csrf_su),
):
    """v4.4.7: меняет роль пользователя admin↔moderator.

    При повышении moderator→admin: удаляем все его записи из chat_admins
    (они больше не нужны — админ имеет права во всех публичных чатах).
    При понижении admin→moderator: записи в chat_admins не трогаем —
    модератор должен будет сам указать чаты через edit-chats (или останется
    с теми чатами, что успел получить до понижения; если их не было —
    он не сможет модерировать ничего, пока SU не привяжет его к чатам).
    """
    form = await request.form()
    new_role = (form.get("role") or "").strip().lower()
    if new_role not in ("admin", "moderator"):
        return RedirectResponse(
            url="/admin/users?flash=Invalid+role",
            status_code=303,
        )
    async with async_session() as session:
        wu = (await session.execute(
            select(WebUser).where(WebUser.id == user_id)
        )).scalar_one_or_none()
        if wu is None or wu.is_su:
            return RedirectResponse(url="/admin/users", status_code=303)
        old_role = wu.role
        if old_role == new_role:
            return RedirectResponse(url="/admin/users", status_code=303)
        wu.role = new_role
        # Повышение moderator→admin: чистим chat_admins
        if old_role == "moderator" and new_role == "admin" and wu.tg_user_id:
            await session.execute(
                select(ChatAdmin).where(ChatAdmin.user_id == wu.tg_user_id)
            )
            # Удаляем все записи chat_admins для этого юзера
            for ca in (await session.execute(
                select(ChatAdmin).where(ChatAdmin.user_id == wu.tg_user_id)
            )).scalars().all():
                await session.delete(ca)
        await session.commit()
    web_app._req_logger.info(
        "admin_users_change_role: user_id=%s %s→%s by=%s",
        user_id, old_role, new_role, _auth.username,
    )
    return RedirectResponse(
        url=f"/admin/users?flash=Role+changed%3A+{old_role}+%E2%86%92+{new_role}",
        status_code=303,
    )


@router.post("/admin/users/{user_id:int}/edit-chats")
async def admin_users_edit_chats(
    user_id: int,
    request: Request,
    _auth: AuthUser = Depends(require_csrf_su),
):
    """v4.4.7: редактирует список чатов, к которым привязан модератор.

    Принимает form-поле chat_ids (множественный выбор). Все ранее привязанные
    чаты, которых нет в новом списке, удаляются. Все новые — добавляются.
    Работает только для пользователей с role=moderator (для admin/SU игнорируется).
    """
    form = await request.form()
    # Получаем список выбранных chat_ids (Form может прийти как list или scalar)
    raw_chats = form.getlist("chat_ids")
    chosen: set[int] = set()
    for raw in raw_chats:
        raw = (raw or "").strip()
        if not raw:
            continue
        try:
            cid = int(raw)
            if cid != 0:
                chosen.add(cid)
        except (ValueError, TypeError):
            pass

    async with async_session() as session:
        wu = (await session.execute(
            select(WebUser).where(WebUser.id == user_id)
        )).scalar_one_or_none()
        if wu is None or wu.is_su:
            return RedirectResponse(url="/admin/users", status_code=303)
        if wu.role != "moderator" or not wu.tg_user_id:
            return RedirectResponse(
                url="/admin/users?flash=Edit+chats+only+for+moderator+role",
                status_code=303,
            )
        # Текущие привязки
        existing = (await session.execute(
            select(ChatAdmin).where(ChatAdmin.user_id == wu.tg_user_id)
        )).scalars().all()
        existing_cids = {ca.chat_id for ca in existing}

        # Удалить те, которых нет в новом списке
        for ca in existing:
            if ca.chat_id not in chosen:
                await session.delete(ca)
        # Добавить новые
        for cid in chosen:
            if cid not in existing_cids:
                session.add(ChatAdmin(
                    chat_id=cid, user_id=wu.tg_user_id, added_by=None,
                ))
        await session.commit()
    web_app._req_logger.info(
        "admin_users_edit_chats: user_id=%s new_chats=%s by=%s",
        user_id, sorted(chosen), _auth.username,
    )
    return RedirectResponse(
        url=f"/admin/users?flash=Chats+updated+for+user+{wu.username}",
        status_code=303,
    )


@router.post("/admin/users/{user_id:int}/bind-tg")
async def admin_users_bind_tg(
    user_id: int,
    request: Request,
    _auth: AuthUser = Depends(require_csrf_su),
    bot=Depends(get_bot),
):
    """v4.4.7: привязывает/отвязывает TG ID для существующего пользователя.

    Используется SU чтобы привязать свой собственный TG ID к SU-аккаунту
    (для получения DM о новых чатах), либо чтобы перепривязать TG ID
    другого пользователя (если сменил аккаунт).
    """
    form = await request.form()
    tg_raw = (form.get("tg_user_id") or "").strip()
    if not tg_raw:
        # Пустое значение = отвязать
        async with async_session() as session:
            wu = (await session.execute(
                select(WebUser).where(WebUser.id == user_id)
            )).scalar_one_or_none()
            if wu is None:
                return RedirectResponse(url="/admin/users", status_code=303)
            if wu.is_su and wu.role == "su":
                # SU можно отвязать (но тогда он не будет получать DM)
                pass
            wu.tg_user_id = None
            await session.commit()
        return RedirectResponse(
            url="/admin/users?flash=TG+ID+unbound",
            status_code=303,
        )
    try:
        tg_id = int(tg_raw)
    except (ValueError, TypeError):
        return RedirectResponse(
            url="/admin/users?flash=TG+ID+must+be+a+number",
            status_code=303,
        )
    if tg_id <= 0:
        return RedirectResponse(
            url="/admin/users?flash=TG+ID+must+be+positive",
            status_code=303,
        )

    async with async_session() as session:
        wu = (await session.execute(
            select(WebUser).where(WebUser.id == user_id)
        )).scalar_one_or_none()
        if wu is None:
            return RedirectResponse(url="/admin/users", status_code=303)
        # Проверка что TG ID не занят другим пользователем
        other = (await session.execute(
            select(WebUser).where(
                WebUser.tg_user_id == tg_id,
                WebUser.id != user_id,
            )
        )).scalar_one_or_none()
        if other:
            return RedirectResponse(
                url=f"/admin/users?flash=TG+ID+{tg_id}+already+bound+to+"
                    f"%27{other.username}%27",
                status_code=303,
            )
        wu.tg_user_id = tg_id
        # Best-effort: обновляем профиль из TG
        if bot is not None:
            try:
                chat = await bot.get_chat(chat_id=tg_id)
                wu.tg_first_name = getattr(chat, "first_name", None)
                wu.tg_last_name = getattr(chat, "last_name", None)
                tg_un = getattr(chat, "username", None)
                if tg_un:
                    wu.tg_username = tg_un.strip().lstrip("@").lower()
            except Exception as e:
                web_app._req_logger.info("bind-tg: bot.get_chat failed (non-critical): %s", e)
        await session.commit()
    # v4.5: скачиваем аватарку для привязанного TG ID (best-effort)
    avatar_ok = await web_app._fetch_and_save_avatar(bot, tg_id)
    if avatar_ok:
        async with async_session() as session:
            wu_for_avatar = (await session.execute(
                select(WebUser).where(WebUser.id == user_id)
            )).scalar_one_or_none()
            if wu_for_avatar is not None:
                wu_for_avatar.tg_photo_updated_at = datetime.now(timezone.utc)
                await session.commit()
        web_app._req_logger.info("admin_users_bind_tg: avatar saved for tg_id=%s", tg_id)
    web_app._req_logger.info(
        "admin_users_bind_tg: user_id=%s tg_id=%s by=%s",
        user_id, tg_id, _auth.username,
    )
    return RedirectResponse(
        url=f"/admin/users?flash=TG+ID+{tg_id}+bound+to+{wu.username}",
        status_code=303,
    )


@router.post("/admin/users/{user_id:int}/delete")
async def admin_users_delete(
    user_id: int,
    _auth: AuthUser = Depends(require_csrf_su),
):
    async with async_session() as session:
        wu = (await session.execute(
            select(WebUser).where(WebUser.id == user_id)
        )).scalar_one_or_none()
        if wu is None or wu.is_su:
            return RedirectResponse(url="/admin/users", status_code=303)
        # v4.5.1: чистим chat_admins для tg_user_id удаляемого юзера.
        # Раньше запись оставалась в БД, и _is_admin через fallback
        # продолжал давать доступ «TG-only модератору» — фактически
        # удалённый аккаунт сохранял модеративные права.
        if wu.tg_user_id:
            cas = (await session.execute(
                select(ChatAdmin).where(ChatAdmin.user_id == wu.tg_user_id)
            )).scalars().all()
            for ca in cas:
                await session.delete(ca)
        await session.delete(wu)
        await session.commit()
    web_app._req_logger.info(
        "admin_users_delete: deleted web_user id=%s by=%s "
        "(also cleared chat_admins for tg_user_id=%s)",
        user_id, _auth.username, wu.tg_user_id,
    )
    return RedirectResponse(url="/admin/users", status_code=303)
