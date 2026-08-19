"""
web/admin_keywords.py — управление keyword-watch списком (#5).

v4.9.0 (Task 5): вынесены GET /admin/keywords, POST /admin/keywords/add,
POST /admin/keywords/{id}/delete и POST /admin/keywords/{id}/toggle-ban-night
из create_app(). Роуты позволяют SU просматривать, добавлять, удалять и
переключать флаг ban_in_night_mode у фраз из таблицы keyword_watch —
веб-эквивалент команд бота !addkeyword / !delkeyword / !listkeywords.

Хелперы web_app (`_req_logger`, `APP_RELEASE_DATE`) берутся через модуль
(`web_app._helper`), а не импортом имён: тесты патчат атрибуты модуля, и
при `from web_app import ...` патч промахнулся бы мимо уже связанного
имени. Межмодульного импорта из других web/-роутеров этот домен не требует.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

import web_app
from db import KeywordWatch, async_session
from web.deps import APP_VERSION, AuthUser, get_templates, require_csrf_su, require_su

router = APIRouter()


# ──────────────────────────────────────────────────────────────────
#  /admin/keywords — v4.8.0: управление списком keyword-watch фраз.
#
#  Страница позволяет SU просматривать, добавлять, редактировать и
#  удалять фразы из таблицы keyword_watch. Эквивалентна командам бота
#  !addkeyword / !delkeyword / !listkeywords, но через веб-панель.
#
#  Доступ: SU-only (по аналогии с /admin/users — глобальная настройка).
# ──────────────────────────────────────────────────────────────────
@router.get("/admin/keywords", response_class=HTMLResponse)
async def admin_keywords_page(
    request: Request,
    flash: str = "",
    _auth: AuthUser = Depends(require_su),
    templates: Jinja2Templates = Depends(get_templates),
):
    """v4.8.0: страница управления keyword-watch списком.

    Показывает все активные фразы (is_active=True) с флагом
    ban_in_night_mode, даёт добавить/удалить/переключить флаг.
    """
    async with async_session() as session:
        keywords = (await session.execute(
            select(KeywordWatch)
            .where(KeywordWatch.is_active.is_(True))
            .order_by(KeywordWatch.created_at.desc())
        )).scalars().all()

        # Также покажем отключённые (для аудита), но визуально отделёнными.
        inactive_keywords = (await session.execute(
            select(KeywordWatch)
            .where(KeywordWatch.is_active.is_(False))
            .order_by(KeywordWatch.created_at.desc())
            .limit(50)
        )).scalars().all()

    return templates.TemplateResponse(
        "admin_keywords.html",
        {
            "request": request,
            "keywords": keywords,
            "inactive_keywords": inactive_keywords,
            "flash": flash,
            "app_version": APP_VERSION,
            "app_release_date": web_app.APP_RELEASE_DATE,
            "auth_user": _auth,
        },
    )


@router.post("/admin/keywords/add")
async def admin_keywords_add(
    phrase: str = Form(""),
    ban_in_night_mode: str = Form(""),
    _auth: AuthUser = Depends(require_csrf_su),
):
    """v4.8.0: добавить фразу в keyword-watch список.

    phrase — 1..255 символов, trim + collapse internal whitespace.
    ban_in_night_mode — чекбокс (presence = True).
    Если фраза уже существует (case-insensitive, и активна, и
    неактивна) — реактивируем и обновляем флаг ban_in_night_mode.
    """
    # Нормализация: trim, collapse internal whitespace, макс 255.
    phrase_clean = " ".join((phrase or "").split())
    if not phrase_clean:
        return RedirectResponse(
            url="/admin/keywords?flash=Phrase+cannot+be+empty",
            status_code=303,
        )
    if len(phrase_clean) > 255:
        return RedirectResponse(
            url="/admin/keywords?flash=Phrase+too+long+(max+255+chars)",
            status_code=303,
        )
    ban_flag = bool(ban_in_night_mode)

    async with async_session() as session:
        # Поиск существующей записи (case-insensitive) — SQLite LOWER.
        existing = (await session.execute(
            select(KeywordWatch).where(
                KeywordWatch.phrase.ilike(phrase_clean)
            )
        )).scalars().first()

        if existing:
            # Реактивируем и обновляем флаг.
            existing.is_active = True
            existing.ban_in_night_mode = ban_flag
            msg = (
                f"Keyword+%27{phrase_clean[:60].replace(' ', '+')}%27"
                f"+updated+(reactivated)"
            )
        else:
            kw = KeywordWatch(
                chat_id=0,  # глобальный список
                phrase=phrase_clean,
                ban_in_night_mode=ban_flag,
                is_active=True,
            )
            session.add(kw)
            msg = (
                f"Keyword+%27{phrase_clean[:60].replace(' ', '+')}%27"
                f"+added"
            )
        await session.commit()

    web_app._req_logger.info(
        "admin_keywords_add: phrase=%r ban_night=%s by=%s",
        phrase_clean, ban_flag, _auth.username,
    )
    return RedirectResponse(
        url=f"/admin/keywords?flash={msg}",
        status_code=303,
    )


@router.post("/admin/keywords/{keyword_id:int}/delete")
async def admin_keywords_delete(
    keyword_id: int,
    _auth: AuthUser = Depends(require_csrf_su),
):
    """v4.8.0: soft-delete фразу из keyword-watch списка.

    Устанавливает is_active=False (audit history preserved).
    """
    async with async_session() as session:
        kw = (await session.execute(
            select(KeywordWatch).where(KeywordWatch.id == keyword_id)
        )).scalar_one_or_none()
        if kw is None:
            return RedirectResponse(
                url="/admin/keywords?flash=Keyword+not+found",
                status_code=303,
            )
        phrase_log = kw.phrase
        kw.is_active = False
        await session.commit()

    web_app._req_logger.info(
        "admin_keywords_delete: id=%s phrase=%r by=%s",
        keyword_id, phrase_log, _auth.username,
    )
    return RedirectResponse(
        url="/admin/keywords?flash=Keyword+deleted",
        status_code=303,
    )


@router.post("/admin/keywords/{keyword_id:int}/toggle-ban-night")
async def admin_keywords_toggle_ban_night(
    keyword_id: int,
    _auth: AuthUser = Depends(require_csrf_su),
):
    """v4.8.0: переключить флаг ban_in_night_mode для фразы."""
    async with async_session() as session:
        kw = (await session.execute(
            select(KeywordWatch).where(KeywordWatch.id == keyword_id)
        )).scalar_one_or_none()
        if kw is None:
            return RedirectResponse(
                url="/admin/keywords?flash=Keyword+not+found",
                status_code=303,
            )
        kw.ban_in_night_mode = not kw.ban_in_night_mode
        new_state = kw.ban_in_night_mode
        await session.commit()

    web_app._req_logger.info(
        "admin_keywords_toggle_ban_night: id=%s new=%s by=%s",
        keyword_id, new_state, _auth.username,
    )
    state_str = "ON" if new_state else "OFF"
    return RedirectResponse(
        url=f"/admin/keywords?flash=Ban-night+{state_str}",
        status_code=303,
    )
