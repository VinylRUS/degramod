"""
web/deps.py — v4.8.9: общие FastAPI dependencies для web/ routers.

Пока что просто реэкспортирует symbols из web_app.py (где они определены).
В будущем (v4.9.0) — определения перенесутся сюда, а web_app.py будет
их импортировать.

Это позволяет web/auth.py и другим router'ам использовать require_auth,
require_csrf_*, COOKIE_NAME, APP_VERSION без циклических зависимостей.
"""
from __future__ import annotations

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
]
