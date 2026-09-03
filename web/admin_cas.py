"""
web/admin_cas.py — v5.5.0: вкладка «На карандаше» + пороги каскада CAS/LOLS.

GET  /admin/cas            — пороги (форма) + watch-таблица потенциальных
POST /admin/cas/thresholds — сохранить пороги (su/admin)
POST /admin/cas/action     — ban/ignore для потенциального (su/admin)

Каскад /account НЕ выдаёт санкций: тир — метка для этой вкладки, решает
модератор (решение владельца 30.08.2026: потенциальных не баним и не
мьютим). Бан только по подтверждённым источникам (verified/hot/CAS).

Хелперы cas.py импортируются лениво внутри функций — против circular
import (тот же паттерн, что в web/admin_bans.py).
"""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from db import (
    CasIgnore,
    CasSettings,
    CasVerdict,
    ChatMemberSeen,
    User,
    async_session,
)
from web.deps import (
    APP_VERSION,
    AuthUser,
    get_bot,
    get_templates,
    require_auth,
    require_csrf_admin,
)

router = APIRouter()


async def _load_settings(session) -> CasSettings:
    """Singleton cas_settings (id=1): создаёт с дефолтами, если нет."""
    row = (await session.execute(
        select(CasSettings).where(CasSettings.id == 1)
    )).scalar_one_or_none()
    if row is None:
        row = CasSettings(id=1)
        session.add(row)
        await session.commit()
    return row


async def _watch_rows(session) -> list[dict]:
    """Потенциальные (Tier C) без cas_ignore, с никами и чатами присутствия."""
    rows = (await session.execute(
        select(CasVerdict, User.username, User.first_name)
        .outerjoin(User, User.user_id == CasVerdict.user_id)
        .outerjoin(CasIgnore, CasIgnore.user_id == CasVerdict.user_id)
        .where(
            CasVerdict.is_banned.is_(False),
            CasVerdict.tier.like("C%"),
            CasVerdict.source == "lols",
            CasIgnore.user_id.is_(None),
        )
        .order_by(CasVerdict.tier, CasVerdict.spam_factor.desc())
        .limit(500)
    )).all()

    seen = (await session.execute(select(ChatMemberSeen))).scalars().all()
    chats_by_user: dict[int, list[str]] = {}
    for cm in seen:
        chats_by_user.setdefault(cm.user_id, []).append(str(cm.chat_id))

    out = []
    for v, username, first_name in rows:
        out.append({
            "user_id": v.user_id,
            "tier": v.tier or "?",
            "spam_factor": v.spam_factor,
            "offenses": v.offenses,
            "scammer": bool(v.scammer),
            "reason": v.reason or "",
            "checked_at": v.checked_at,
            "username": username or "",
            "first_name": first_name or "",
            "chats": ", ".join(chats_by_user.get(v.user_id, [])) or "—",
        })
    return out


# ── GET /admin/cas ──────────────────────────────────────────────────────────
@router.get("/admin/cas", response_class=HTMLResponse)
async def admin_cas_page(
    request: Request,
    flash: str = "",
    _auth: AuthUser = Depends(require_auth),
    templates: Jinja2Templates = Depends(get_templates),
):
    """v5.5.0: вкладка «На карандаше» + пороги каскада."""
    async with async_session() as session:
        cfg = await _load_settings(session)
        watch = await _watch_rows(session)
    return templates.TemplateResponse("admin_cas.html", {
        "request": request,
        "auth_user": _auth,
        "app_version": APP_VERSION,
        "flash": flash or None,
        "cfg": cfg,
        "watch": watch,
    })


# ── POST /admin/cas/thresholds (su/admin) ──────────────────────────────────
@router.post("/admin/cas/thresholds")
async def cas_thresholds_post(
    request: Request,
    spamfactor_ban: str = Form(""),
    spamfactor_mute: str = Form(""),
    offenses_mute: str = Form(""),
    _auth: AuthUser = Depends(require_csrf_admin),
    templates: Jinja2Templates = Depends(get_templates),
):
    """Сохраняет пороги каскада. Каскад санкций не выдаёт — пороги влияют
    только на тир-метки в «На карандаше»."""
    def _num(v, default):
        try:
            return float(v) if default.__class__ is float else int(float(v))
        except (TypeError, ValueError):
            return default

    sf_ban = _num(spamfactor_ban, 60.0)
    sf_mute = _num(spamfactor_mute, 30.0)
    off_mute = _num(offenses_mute, 10)
    if sf_mute > sf_ban:
        sf_mute = sf_ban  # порядок порогов не даём сломать формой

    async with async_session() as session:
        cfg = await _load_settings(session)
        cfg.spamfactor_ban = sf_ban
        cfg.spamfactor_mute = sf_mute
        cfg.offenses_mute = int(off_mute)
        cfg.updated_by = getattr(_auth, "username", None) or "web"
        await session.commit()

    return RedirectResponse(
        f"/admin/cas?flash={quote('Пороги сохранены: ban ≥ %g, mute ≥ %g, offenses ≥ %d' % (sf_ban, sf_mute, off_mute))}",
        status_code=303,
    )


# ── POST /admin/cas/action (su/admin): ручной бан / игнор ──────────────────
@router.post("/admin/cas/action")
async def cas_action_post(
    request: Request,
    action: str = Form(...),
    user_id: int = Form(...),
    _auth: AuthUser = Depends(require_csrf_admin),
    bot=Depends(get_bot),
    templates: Jinja2Templates = Depends(get_templates),
):
    """Ручные действия над потенциальным из «На карандаше».

    ban — бан во всех чатах присутствия + punishment (mod = веб-юзер);
    ignore — в cas_ignore (LOLS-метка считается ложной, свип пропустит).
    Оба действия убирают юзера из «На карандаше».
    """
    from bot_handlers import (  # lazy — против circular import
        TelegramAPIError,
        _save_punishment,
        _upsert_moderator,
        _upsert_user,
        tg_safe_call,
    )

    mod_id = getattr(_auth, "tg_user_id", None) or 0
    mod_name = getattr(_auth, "username", None) or "web"

    if action == "ban":
        banned_chats: list[int] = []
        errors: list[str] = []
        async with async_session() as session:
            chat_ids = (await session.execute(
                select(ChatMemberSeen.chat_id).where(
                    ChatMemberSeen.user_id == user_id,
                )
            )).scalars().all()
        for chat_id in sorted(set(chat_ids)):
            try:
                await tg_safe_call(
                    lambda: bot.ban_chat_member(chat_id=chat_id, user_id=user_id),
                    label="CAS_watchlist_ban",
                )
                banned_chats.append(chat_id)
                async with async_session() as session:
                    await _upsert_user(session, user_id, None, None, None)
                    await _upsert_moderator(session, mod_id, None, mod_name)
                    await _save_punishment(
                        session, user_id, mod_id, chat_id,
                        "ban", None,
                        "Ручной бан из «На карандаше» (потенциальный по LOLS)",
                        None,
                    )
            except TelegramAPIError as e:
                errors.append(f"chat {chat_id}: {e}")

        if errors:
            flash = "Бан частично не удался: " + "; ".join(errors)
        else:
            flash = f"Забанен в {len(banned_chats)} чатах" if banned_chats else \
                "Юзер не найден ни в одном чате присутствия — нечего банить"
        return RedirectResponse(
            f"/admin/cas?flash={quote(flash)}", status_code=303,
        )

    if action == "ignore":
        async with async_session() as session:
            row = (await session.execute(
                select(CasIgnore).where(CasIgnore.user_id == user_id)
            )).scalar_one_or_none()
            if row is None:
                session.add(CasIgnore(
                    user_id=user_id, added_by=mod_id,
                    comment="manual: watchlist ignore",
                ))
            else:
                row.added_by = mod_id
                row.comment = "manual: watchlist ignore"
            await session.commit()
        return RedirectResponse(
            f"/admin/cas?flash={quote(f'Юзер {user_id} добавлен в cas_ignore')}",
            status_code=303,
        )

    return RedirectResponse(
        f"/admin/cas?flash={quote('Неизвестное действие')}", status_code=303,
    )
