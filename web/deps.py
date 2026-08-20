"""
web/deps.py — v4.8.9: общие FastAPI dependencies для web/ routers.

Пока что просто реэкспортирует symbols из web_app.py (где они определены).
В будущем (v4.9.0) — определения перенесутся сюда, а web_app.py будет
их импортировать.

Это позволяет web/auth.py и другим router'ам использовать require_auth,
require_csrf_*, COOKIE_NAME, APP_VERSION без циклических зависимостей.
"""
from __future__ import annotations

from fastapi import Request
from fastapi.templating import Jinja2Templates

# Реэкспортируем из web_app.py — там они определены.
# В v4.9.0 эти определения переедут сюда.
from web_app import (
    APP_VERSION,
    COOKIE_NAME,
    AuthUser,
    require_admin,
    require_auth,
    require_csrf_admin,
    require_csrf_auth,
    require_csrf_su,
    require_su,
)


def get_templates(request: Request) -> Jinja2Templates:
    """Jinja2Templates, собранный в create_app().

    v4.9.0 (Task 10): роутеры из web/ больше не замыкаются на локальную
    переменную create_app — берут объект из app.state. На нём уже висит
    обёртка, прокидывающая csrf_token в контекст каждого шаблона.
    """
    return request.app.state.templates


def get_bot(request: Request):
    """Экземпляр aiogram.Bot или None.

    None — штатная ситуация: вся сюита зовёт create_app() без бота, и
    роуты, которым бот нужен, отвечают 503. Поэтому getattr с дефолтом,
    а не обращение к атрибуту напрямую.
    """
    return getattr(request.app.state, "bot", None)


__all__ = [
    "AuthUser",
    "APP_VERSION",
    "COOKIE_NAME",
    "require_auth",
    "require_su",
    "require_admin",
    "require_csrf_auth",
    "require_csrf_su",
    "require_csrf_admin",
    "get_templates",
    "get_bot",
]
