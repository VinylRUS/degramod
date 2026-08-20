"""
web/admin_bans.py — страница активных банов (#3).

v4.9.0 (Task 3): вынесен GET /admin/bans из create_app().

Хелперы и константы берутся через модуль web_app (web_app._helper и
т.д.), а не импортом имён: тесты патчат атрибуты модуля, и при
`from web_app import ...` патч промахнулся бы мимо уже связанного имени.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from db import Punishment, async_session
from web.deps import APP_VERSION, AuthUser, get_templates, require_auth

router = APIRouter()


# ════════════════════════════════════════════════════════════════════
# v4.8.1: Web unban (#3) — список активных банов + API разбана
# ════════════════════════════════════════════════════════════════════

# ── GET /admin/bans ─────────────────────────────────────────────────
@router.get("/admin/bans", response_class=HTMLResponse)
async def admin_bans_page(
    request: Request,
    chat_id: str = "",
    q: str = "",
    limit: int = 200,
    flash: str = "",
    _auth: AuthUser = Depends(require_auth),
    templates: Jinja2Templates = Depends(get_templates),
):
    """v4.8.1: страница активных банов (#3).

    Показывает все активные баны (action_type='ban', is_revoked=False)
    по всем чатам, с фильтрами по chat_id и поиску по юзеру.

    Доступ: все аутентифицированные веб-юзеры (admin + SU + moderator).
    Кнопка разбана через POST /api/unban.
    """
    # Нормализуем параметры.
    try:
        limit_int = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        limit_int = 200
    chat_id_int: int | None = None
    if chat_id:
        try:
            chat_id_int = int(chat_id)
        except ValueError:
            pass  # игнорим некорректный — показываем без фильтра
    q_lower = (q or "").strip().lower()

    bans = []
    total_count = 0
    async with async_session() as session:
        # Базовый запрос: только активные баны (is_revoked=False).
        # JOIN с User и Moderator для подстановки имён/юзернеймов.
        from sqlalchemy import func as _f
        base_filter = [
            Punishment.action_type == "ban",
            Punishment.is_revoked.is_(False),
        ]
        if chat_id_int is not None:
            base_filter.append(Punishment.chat_id == chat_id_int)

        # Считаем total_count (без LIMIT).
        count_q = select(_f.count(Punishment.id)).where(*base_filter)
        total_count = (await session.execute(count_q)).scalar_one()

        # Поиск по юзеру (user_id, username, first_name) — добавляем к фильтру.
        if q_lower:
            # Простая подстрока. user_id — числовое поле, проверяем если
            # q — число. Иначе — ILIKE по username/first_name.
            from db import Moderator as _M
            from db import User as _U
            user_cond = []
            try:
                q_int = int(q_lower)
                user_cond.append(_U.user_id == q_int)
            except ValueError:
                pass
            user_cond.append(_U.username.ilike(f"%{q_lower}%"))
            user_cond.append(_U.first_name.ilike(f"%{q_lower}%"))
            from sqlalchemy import or_ as _or

            main_q = (
                select(
                    Punishment,
                    _U.user_id.label("u_user_id"),
                    _U.username.label("u_username"),
                    _U.first_name.label("u_first_name"),
                    _U.last_name.label("u_last_name"),
                    _M.mod_id.label("m_mod_id"),
                    _M.username.label("m_username"),
                    _M.first_name.label("m_first_name"),
                )
                .select_from(Punishment)
                .outerjoin(_U, Punishment.user_id == _U.user_id)
                .outerjoin(_M, Punishment.mod_id == _M.mod_id)
                .where(*base_filter)
                .where(_or(*user_cond))
                .order_by(Punishment.created_at.desc())
                .limit(limit_int)
            )
        else:
            from db import Moderator as _M
            from db import User as _U
            main_q = (
                select(
                    Punishment,
                    _U.user_id.label("u_user_id"),
                    _U.username.label("u_username"),
                    _U.first_name.label("u_first_name"),
                    _U.last_name.label("u_last_name"),
                    _M.mod_id.label("m_mod_id"),
                    _M.username.label("m_username"),
                    _M.first_name.label("m_first_name"),
                )
                .select_from(Punishment)
                .outerjoin(_U, Punishment.user_id == _U.user_id)
                .outerjoin(_M, Punishment.mod_id == _M.mod_id)
                .where(*base_filter)
                .order_by(Punishment.created_at.desc())
                .limit(limit_int)
            )
        rows = (await session.execute(main_q)).all()
        for row in rows:
            p = row[0]
            bans.append({
                "id": p.id,
                "user_id": p.user_id,
                "chat_id": p.chat_id,
                "mod_id": p.mod_id,
                "reason": p.reason,
                "created_at": p.created_at,
                "user_username": row.u_username,
                "user_first_name": row.u_first_name,
                "user_last_name": row.u_last_name,
                "mod_username": row.m_username,
                "mod_first_name": row.m_first_name,
            })

    return templates.TemplateResponse("admin_bans.html", {
        "request": request,
        "auth_user": _auth,
        "app_version": APP_VERSION,
        "flash": flash or None,
        "bans": bans,
        "total_count": total_count,
        "filters": {
            "chat_id": chat_id,
            "q": q,
            "limit": limit_int,
        },
    })
