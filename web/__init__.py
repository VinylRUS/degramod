"""
web/__init__.py — v4.8.9: package для декомпозиции create_app().

Цель: разбить 4893-строчный create_app() с 54 роутами на отдельные
FastAPI Router'ы (см. 03_TASK_v4.8.9.md §2).

v4.8.9: создана инфраструктура (web/ package + web/deps.py + web/health.py +
web/auth.py). Перенесены 2 роута как proof-of-concept (/health и /logout).
Остальные 52 роута остаются в create_app() — TODO v4.9.0.

Структура (план v4.9.0):
  web/__init__.py    ← этот файл
  web/deps.py        ← require_auth, require_csrf_*, AuthUser, COOKIE_NAME
  web/auth.py        ← /login, /logout
  web/me.py          ← /dashboard, /profile, /me/*
  web/admin.py       ← /admin, /admin/settings, /admin/cleanup
  web/admin_bans.py  ← /admin/bans*, /api/unban
  web/admin_chats.py ← /admin/chats*
  web/admin_keywords.py ← /admin/keywords*
  web/admin_presets.py  ← /admin/presets*
  web/api.py         ← /api/* (JSON endpoints)

В create_app() останется только:
  app = FastAPI(...)
  app.include_router(auth.router)
  app.include_router(me.router)
  ...
  return app
"""
