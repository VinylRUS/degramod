"""
web/__init__.py — package с роутами веб-панели, вынесенными из create_app().

v4.8.9–v4.10.0 (Task 1–12 плана декомпозиции): все 47 роутов, которые раньше
жили в 4135-строчном create_app() как замыкание над `bot`, разъехались по
11 модулям. create_app() в web_app.py теперь только собирает FastAPI-
приложение и подключает роутеры — роутов внутри него не осталось.

Модули и их зона ответственности:
  web/deps.py            — общие FastAPI dependencies: require_auth,
                            require_csrf_*, AuthUser, COOKIE_NAME, get_bot,
                            get_templates.
  web/health.py           — GET /health.
  web/auth.py             — GET/POST /login, /logout.
  web/me.py                — личный профиль: /, /avatar/{tg_user_id},
                            /dashboard, /user/{user_id}, /me, /me/password,
                            /me/avatar/refresh.
  web/api.py               — /api/* JSON-эндпоинты (dashboard, search,
                            unban, reset-automute-count и др.).
  web/admin_bans.py        — GET /admin/bans.
  web/admin_chats.py       — /admin/chats* (настройки чата, режимы,
                            sync-admins, sanitary periods).
  web/admin_cleanup.py     — /admin/cleanup (очистка тестовых данных).
  web/admin_keywords.py    — /admin/keywords* (keyword-watch список).
  web/admin_presets.py     — /admin/presets* (пресеты прав, word filter,
                            link allowlist).
  web/admin_settings.py    — /admin/settings* (системные настройки, GitHub
                            интеграция).
  web/admin_users.py       — /admin/users* (веб-пользователи).

Два правила, обязательных для новых модулей:

1. Роутеры импортируются только внутри `create_app()`, не на верхнем
   уровне `web_app.py`. Top-level `from web.X import router` в web_app.py
   даёт цикл импорта: web_app → web.X → web.deps → web_app.
2. Модули `web/` обращаются к хелперам `web_app` через модуль
   (`web_app._helper(...)`), а не через `from web_app import _helper`.
   Тесты патчат хелперы как атрибуты модуля `web_app`; именной импорт
   привязывается к значению на момент импорта и патч не подхватывает.
"""
