# Декомпозиция `create_app()` — план внедрения (Task 10)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** вынести 47 роутов из `create_app()` в предметные модули `web/`, заменив
замыкание на `bot`/`templates` передачей через `app.state` + `Depends`.

**Architecture:** `create_app()` кладёт `app.state.templates` и `app.state.bot`;
`web/deps.py` отдаёт их провайдерами `get_templates`/`get_bot`; роуты объявляют
их как зависимости. Тела роутов переносятся дословно. `create_app()` ужимается
с 4135 строк до ~137 и становится сборщиком.

**Tech Stack:** Python 3.14.7, FastAPI 0.141.1, Starlette 1.6.0, aiogram 3.30.0,
SQLAlchemy async + aiosqlite, uv, ruff, unittest + собственный раннер
`tools/run_tests.py`.

**Spec:** `docs/superpowers/specs/2026-08-19-web-app-decomposition-design.md`

---

## Global Constraints

Действуют в каждой задаче, повторно не проговариваются:

- **Язык кода и комментариев — русский** (требование `CLAUDE.md`).
- **Тела роутов переносятся дословно.** Никаких «улучшений» по дороге:
  ни переименований, ни рефакторинга, ни исправления замеченных багов.
  Замечено — записывается отдельно, чинится отдельным коммитом после Task 10.
- **База данных не трогается.** Ни модели, ни `init_db()`, ни миграции, ни
  схема. Task 10 не создаёт миграций и не требует перерегистрации данных.
- **Импорты роутеров — только внутри `create_app()`.** Top-level импорт
  `web.*` в `web_app.py` даёт цикл `web_app → web.X → web.deps → web_app` и
  падает с `ImportError: cannot import name 'APP_VERSION' from 'web_app'`
  (проверено экспериментально 19.08.2026).
- **Хелперы `web_app` вызываются через модуль:** `import web_app` +
  `web_app._helper(...)`, никогда `from web_app import _helper`. Иначе
  ломаются тесты, патчащие `web_app._fetch_and_save_avatar`.
- **Прогон после каждой задачи:** `uv run python tools/run_tests.py` (65
  файлов, ~6 минут) и `uv run ruff check .`. Обе команды должны быть
  зелёными до коммита.
- **`known_failing.txt` остаётся пустым.** Красный тест чинится, а не
  заносится в список.
- **Версия не бампается** до завершения всех задач — бамп в Task 12.

### Эталонные числа (на `4e3d5b2`, v4.9.0)

- Роутов в приложении: **54** (47 в `create_app`, 7 в `web/`).
- Строк в `create_app()`: **4135**, из них переезжает **3998**.
- Сюита: **65 файлов, 65 PASS**, `known_failing.txt` пуст.
- `ruff check .`: **All checks passed**.

### Как считать роуты (обязательно этим способом)

В Starlette 1.6 `include_router` **не** разворачивает роуты в `app.routes` —
там появляется `fastapi.routing._IncludedRouter`, а роуты лежат в его
`original_router.routes`. Наивный подсчёт даёт 47 и будет падать с каждой
задачей, изображая потерю роутов.

```python
from starlette.routing import Route

def walk(routes):
    for r in routes:
        if isinstance(r, Route):
            yield r
        elif hasattr(r, "original_router"):
            yield from walk(r.original_router.routes)

pairs = {(r.path, m) for r in walk(app.routes)
         for m in (r.methods or ()) if m != "HEAD"}
assert len(pairs) == 54
```

### Тесты, грепающие исходник `web_app.py` — особый случай

Три файла сюиты читают `web_app.py` как текст и ищут в нём подстроки. При
переносе кода такие проверки краснеют **законно**: строка никуда не пропала,
она в другом файле.

| Файл | Что ищет | Ломается в задаче |
|---|---|---|
| `test_v478_login_500_fix.py` | `login: failed to update su.last_login_at`, `login: failed to update %s.last_login_at` | Task 2 (auth) |
| `test_v487_sanity.py` [4] | `re.search(r"hmac\.compare_digest\(password, WEB_PASSWORD\)")` | Task 2 (auth) |
| `test_v487_sanity.py` [11] | `asyncio.to_thread` ≥ 7 вхождений | Task 4, 8 |
| `test_v475_wordfilter_linkallowlist_ui.py` | пути `/admin/presets/words/*`, `/links/*`, `WordFilter`, `LinkAllowlist` | Task 10 (presets) |
| `test_v476_sanitary_ui_cleanup.py` | `/sanitary/add`, `admin_chats_sanitary_add`, … | Task 11 (chats) |

**Правило:** это единственное исключение из «краснеет тест — чини код». Такой
тест правится так, чтобы искать в новом файле, и **только так** — смысл
проверки сохраняется, меняется адрес. Ослаблять или удалять проверку нельзя.

---

## File Structure

| Файл | Ответственность | Задача |
|---|---|---|
| `web/deps.py` | + `get_templates`, `get_bot` | 1 |
| `web_app.py` | `create_app` как сборщик; module-level хелперы остаются | 1–12 |
| `web/auth.py` | `/login` (GET, POST) + существующий `/logout` | 2 |
| `web/admin_bans.py` | `/admin/bans` | 3 |
| `web/admin_cleanup.py` | `/admin/cleanup` (GET, POST) + `_cleanup_counts` | 4 |
| `web/admin_keywords.py` | `/admin/keywords*` (4 роута) | 5 |
| `web/api.py` | + `/api/dashboard`, `/api/search`, `/api/unban`, `/api/reset-automute-count` | 6 |
| `web/me.py` | + `/dashboard`, `/user/{id}`, `/me`, `/me/password`, `/me/avatar/refresh` | 7 |
| `web/admin_settings.py` | `/admin/settings*` (6 роутов) + `_bot_info`, `_load_github_settings_row` | 8 |
| `web/admin_users.py` | `/admin/users*` (8 роутов) | 9 |
| `web/admin_presets.py` | `/admin/presets*` (8 роутов) | 10 |
| `web/admin_chats.py` | `/admin/chats*` (7 роутов) | 11 |

---

## Task 1: Провайдеры зависимостей и `app.state`

Инфраструктура без переноса роутов: после задачи всё работает ровно как
раньше, но появляется механика, на которой поедут задачи 2–11.

**Files:**
- Modify: `web/deps.py`
- Modify: `web_app.py:772-900` (тело `create_app`, до первого роута)
- Test: `tests/test_v490_decomposition.py` (создать)

**Interfaces:**
- Produces: `web.deps.get_templates(request) -> Jinja2Templates`,
  `web.deps.get_bot(request) -> Bot | None` — их потребляют задачи 2–11.

- [ ] **Step 1: Написать падающий тест**

Создать `tests/test_v490_decomposition.py`:

```python
"""
test_v490_decomposition.py — инварианты декомпозиции create_app() (Task 10).

Проверяет механику, на которой держится вынос роутов в web/:
  1. app.state.templates и app.state.bot проставлены;
  2. get_templates / get_bot достают их из request;
  3. get_bot возвращает None, когда create_app вызван без бота
     (тесты и часть роутов рассчитывают на 503, а не на падение);
  4. общее число роутов приложения не меняется при переносе.

Пункт 4 — страховка от потери роута при копировании. Считать нужно с
обходом _IncludedRouter: в Starlette 1.6 include_router не разворачивает
роуты в app.routes.
"""
from __future__ import annotations

import os
import sys
import unittest

from _paths import _P

sys.path.insert(0, _P())

os.environ.setdefault("BOT_TOKEN", "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw")
os.environ.setdefault("ADMIN_IDS", "111111111")
os.environ.setdefault("WEB_PASSWORD", "test_pw")
os.environ.setdefault("WEB_ALLOW_NO_SECRET", "1")

from starlette.routing import Route  # noqa: E402

import web_app  # noqa: E402
from web.deps import get_bot, get_templates  # noqa: E402

# Эталон на v4.9.0 (4e3d5b2): 47 роутов в create_app + 7 в web/.
_EXPECTED_ROUTES = 54


def _walk(routes):
    """Разворачивает вложенные роутеры.

    FastAPI 0.141 кладёт в app.routes объект _IncludedRouter, а сами роуты
    прячет в его original_router.routes. Без обхода счётчик покажет только
    роуты, объявленные внутри create_app, и будет уменьшаться с каждым
    вынесенным доменом.
    """
    for r in routes:
        if isinstance(r, Route):
            yield r
        elif hasattr(r, "original_router"):
            yield from _walk(r.original_router.routes)


def _route_pairs(app):
    return {
        (r.path, m)
        for r in _walk(app.routes)
        for m in (r.methods or ())
        if m != "HEAD"
    }


class _FakeRequest:
    """Минимальный объект с .app — провайдерам больше ничего не нужно."""

    def __init__(self, app):
        self.app = app


class TestAppState(unittest.TestCase):

    def test_templates_in_app_state(self):
        """create_app кладёт templates в app.state — роутеры берут его оттуда."""
        app = web_app.create_app()
        self.assertTrue(hasattr(app.state, "templates"))

    def test_get_templates_returns_state_object(self):
        """get_templates отдаёт тот же объект, что лежит в state.

        Важно, что именно тот же: на нём висит CSRF-обёртка над
        TemplateResponse, собранная в create_app.
        """
        app = web_app.create_app()
        self.assertIs(get_templates(_FakeRequest(app)), app.state.templates)

    def test_get_bot_returns_none_without_bot(self):
        """create_app() без бота → get_bot даёт None, а не падает.

        На это рассчитывают роуты, отвечающие 503 при bot is None, и вся
        сюита: она зовёт create_app() без аргументов.
        """
        app = web_app.create_app()
        self.assertIsNone(get_bot(_FakeRequest(app)))

    def test_get_bot_returns_passed_bot(self):
        """Переданный бот доезжает до провайдера."""
        sentinel = object()
        app = web_app.create_app(bot=sentinel)
        self.assertIs(get_bot(_FakeRequest(app)), sentinel)


class TestRouteInventory(unittest.TestCase):

    def test_route_count_unchanged(self):
        """Число роутов не меняется — ловит потерю роута при переносе."""
        app = web_app.create_app()
        self.assertEqual(len(_route_pairs(app)), _EXPECTED_ROUTES)

    def test_no_duplicate_routes(self):
        """Один и тот же (путь, метод) не зарегистрирован дважды.

        При копировании легко оставить роут и в create_app, и в новом
        модуле: FastAPI не ругается, просто первый выигрывает.
        """
        app = web_app.create_app()
        pairs = [
            (r.path, m)
            for r in _walk(app.routes)
            for m in (r.methods or ())
            if m != "HEAD"
        ]
        self.assertEqual(len(pairs), len(set(pairs)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
```

- [ ] **Step 2: Запустить, убедиться что падает**

```bash
uv run pytest tests/test_v490_decomposition.py -q
```

Expected: FAIL — `ImportError: cannot import name 'get_templates' from 'web.deps'`.
Если вместо этого падает на числе роутов — значит эталон 54 не сошёлся,
разбираться до продолжения.

- [ ] **Step 3: Добавить провайдеры в `web/deps.py`**

В конец файла, до `__all__`:

```python
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
```

Импорты в шапку `web/deps.py`:

```python
from fastapi import Request
from fastapi.templating import Jinja2Templates
```

Дописать в `__all__`: `"get_templates"`, `"get_bot"`.

- [ ] **Step 4: Проставить `app.state` в `create_app`**

В `web_app.py`, сразу после блока создания `AVATARS_DIR` и **до** секции
`# ── v4.8.9: Routers из web/ package ──`:

```python
    # ── v4.9.0 (Task 10): зависимости для роутеров из web/ ──────────────
    # Роутеры не могут замкнуться на локальные переменные create_app, поэтому
    # templates и bot кладутся в состояние приложения, а web/deps.py отдаёт
    # их провайдерами get_templates/get_bot.
    # Именно app.state, а не модульные синглтоны: сюита зовёт create_app()
    # многократно в одном процессе (test_v460_granular_perms.py — 15 раз),
    # и глобальное состояние текло бы между экземплярами.
    app.state.templates = templates
    app.state.bot = bot
```

- [ ] **Step 5: Запустить тест — должен проходить**

```bash
uv run pytest tests/test_v490_decomposition.py -q
```

Expected: 6 passed.

- [ ] **Step 6: Прогнать всю сюиту и линтер**

```bash
uv run python tools/run_tests.py
uv run ruff check .
```

Expected: 66 файлов, 66 PASS (новый файл добавился к 65); ruff — All checks passed.

- [ ] **Step 7: Коммит**

```bash
git add web/deps.py web_app.py tests/test_v490_decomposition.py
git commit -m "refactor(web): провайдеры get_templates/get_bot через app.state

Инфраструктура для выноса роутов из create_app (Task 10). Поведение не
меняется: роуты по-прежнему объявлены в create_app и используют замыкание.

app.state, а не модульные синглтоны: сюита зовёт create_app многократно в
одном процессе, глобальное состояние текло бы между экземплярами.

6 тестов на механику: state проставлен, провайдеры достают объекты,
get_bot даёт None без бота, число роутов (54) не изменилось и дублей нет.
Счётчик роутов обходит _IncludedRouter — в Starlette 1.6 include_router не
разворачивает роуты в app.routes, и наивный подсчёт занижал бы их до 47."
```

---

## Task 2: Домен `auth` — `/login`

Первый настоящий перенос. Два роута, 94 строки, обкатка `get_templates`.

**Files:**
- Modify: `web/auth.py` (добавить 2 роута)
- Modify: `web_app.py:909-1002` (удалить оба роута)
- Modify: `tests/test_v478_login_500_fix.py:188-195` (переадресовать grep)
- Modify: `tests/test_v487_sanity.py:79-94` (переадресовать grep)

**Interfaces:**
- Consumes: `web.deps.get_templates` (Task 1).
- Produces: ничего для следующих задач; `web/auth.py` становится образцом.

- [ ] **Step 1: Убедиться, что сюита зелёная до начала**

```bash
uv run python tools/run_tests.py
```

Expected: 66/66 PASS. Начинать перенос на красной сюите нельзя — потом не
отличить своё падение от чужого.

- [ ] **Step 2: Перенести роуты в `web/auth.py`**

Скопировать `web_app.py:909-1002` (от `@app.get("/login"` до конца
`login_submit`, не включая комментарий про logout) в `web/auth.py` **до**
существующих роутов `/logout`.

Заменить в скопированном:
- `@app.get(` → `@router.get(`, `@app.post(` → `@router.post(`;
- убрать 4 пробела отступа со всех строк;
- `templates.TemplateResponse(...)` → `templates.TemplateResponse(...)`,
  где `templates` приходит параметром (см. ниже);
- в сигнатуры добавить зависимость.

Итоговые сигнатуры:

```python
@router.get("/login", response_class=HTMLResponse)
async def login_page(
    request: Request,
    templates: Jinja2Templates = Depends(get_templates),
):
```

```python
@router.post("/login")
async def login_submit(
    request: Request,
    templates: Jinja2Templates = Depends(get_templates),
):
```

Обращения к хелперам и константам `web_app` — через модуль:

| Было | Стало |
|---|---|
| `_client_ip(request)` | `web_app._client_ip(request)` |
| `_check_login_rate_limit(ip)` | `web_app._check_login_rate_limit(ip)` |
| `_req_logger` | `web_app._req_logger` |
| `WEB_PASSWORD` | `web_app.WEB_PASSWORD` |
| `_make_token(...)` | `web_app._make_token(...)` |
| `_COOKIE_SECURE` | `web_app._COOKIE_SECURE` |
| `_verify_password(...)` | `web_app._verify_password(...)` |

`COOKIE_NAME` уже импортируется в `web/auth.py` из `web.deps` — оставить как есть.

Шапка `web/auth.py` после правки:

```python
"""
web/auth.py — роуты входа и выхода.

v4.8.9: вынесены /logout (POST и GET) как proof-of-concept декомпозиции.
v4.9.0 (Task 10): добавлены GET и POST /login.

Хелперы и константы берутся через модуль web_app (web_app._client_ip и
т.д.), а не импортом имён: тесты патчат атрибуты модуля, и при
`from web_app import ...` патч промахнулся бы мимо уже связанного имени.
"""
from __future__ import annotations

import hmac
from datetime import datetime, timezone

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

import web_app
from db import WebUser, async_session
from web.deps import COOKIE_NAME, get_templates

router = APIRouter()
```

(`APIRouter` уже импортирован в файле — не дублировать.)

- [ ] **Step 3: Удалить роуты из `create_app`**

Вырезать `web_app.py:909-1002` — оба роута целиком. На их месте оставить
комментарий в стиле уже существующих пометок:

```python
    # ── GET /login и POST /login — v4.9.0 перенесены в web/auth.py ──────
    # Раньше тут были inline @app.get("/login") и @app.post("/login").
    # Теперь — в web/auth.py, подключены через app.include_router выше.
```

- [ ] **Step 4: Запустить сюиту — увидеть ожидаемые падения**

```bash
uv run python tools/run_tests.py -k login
uv run pytest tests/test_v487_sanity.py -q
```

Expected: краснеют ровно два файла —
`test_v478_login_500_fix.py` (не находит строки в `web_app.py`) и
`test_v487_sanity.py` (не находит `hmac.compare_digest`). Функциональные
тесты логина при этом зелёные: роут работает, переехал только адрес кода.

Если краснеет что-то ещё — это регрессия переноса, разбираться до
продолжения.

- [ ] **Step 5: Переадресовать grep-проверки**

В `tests/test_v478_login_500_fix.py` заменить тело `test_07`:

```python
    def test_07_login_handler_has_try_except_around_last_login_at(self):
        """login handler содержит try/except вокруг last_login_at update.

        v4.9.0 (Task 10): роут переехал из web_app.py в web/auth.py,
        проверка ищет там же. Смысл прежний — обновление метрики не
        должно ронять логин (см. v4.7.8).
        """
        with open(_P("web/auth.py")) as f:
            content = f.read()
        self.assertIn("login: failed to update su.last_login_at", content)
        self.assertIn("login: failed to update %s.last_login_at", content)
```

В `tests/test_v487_sanity.py` пункт [4] — читать исходник `web/auth.py`:

```python
# 4. SU password uses hmac.compare_digest
print("\n[4] web/auth.py: SU password compare_digest...")
# v4.9.0 (Task 10): роут /login переехал из web_app.py в web/auth.py.
with open(_P("web/auth.py")) as f:
    auth_src = f.read()
assert re.search(r"hmac\.compare_digest\s*\(\s*password\s*,\s*web_app\.WEB_PASSWORD\s*\)", auth_src), \
    "SU login should use hmac.compare_digest(password, web_app.WEB_PASSWORD)"
src_lines = auth_src.split("\n")
violations = []
for i, line in enumerate(src_lines, 1):
    stripped = line.split("#", 1)[0].strip()
    if "password != web_app.WEB_PASSWORD" in stripped:
        violations.append((i, line))
assert not violations, f"found `password != WEB_PASSWORD` at lines: {violations}"
print("    ✓ SU login uses hmac.compare_digest (no != comparison)")
```

- [ ] **Step 6: Прогнать всю сюиту и линтер**

```bash
uv run python tools/run_tests.py
uv run ruff check .
```

Expected: 66/66 PASS; ruff — All checks passed. Тест из Task 1 подтверждает,
что роутов по-прежнему 54.

- [ ] **Step 7: Коммит**

```bash
git add web/auth.py web_app.py tests/test_v478_login_500_fix.py tests/test_v487_sanity.py
git commit -m "refactor(web): /login вынесен в web/auth.py

Первый домен Task 10. Оба роута перенесены дословно, templates приходит
через Depends(get_templates) вместо замыкания.

Две grep-проверки переадресованы на web/auth.py: они ищут строки в
исходнике, а строки законно переехали. Смысл проверок не тронут —
try/except вокруг last_login_at (v4.7.8) и hmac.compare_digest в SU-логине
(v4.8.7) проверяются там же, где теперь живёт код."
```

---

## Задачи 3–11: остальные домены

Все девять идут по одному шаблону — он отработан в Task 2. Ниже для каждой
задачи дан только её специфический состав: что переносить, что сломается,
на что смотреть. Шаги внутри задачи одинаковы:

1. убедиться, что сюита зелёная;
2. перенести роуты в модуль (декоратор `@app.` → `@router.`, снять отступ,
   зависимости через `Depends`, хелперы через `web_app.`);
3. удалить из `create_app`, оставить комментарий-пометку;
4. прогнать сюиту, увидеть ожидаемые падения (если они предсказаны);
5. переадресовать grep-проверки, если предсказаны;
6. полная сюита + ruff зелёные;
7. коммит.

Новый модуль начинается с докстроки в стиле `web/auth.py`: что за домен,
когда вынесен, и напоминание про `import web_app` вместо `from web_app import`.

### Task 3: `web/admin_bans.py`

**Files:** Create `web/admin_bans.py`; Modify `web_app.py:4581-4714`.

| Метод | Путь | Функция | Строки | Зависимости |
|---|---|---|---|---|
| GET | `/admin/bans` | `admin_bans_page` | 4581–4714 (134) | `templates` |

Grep-проверок нет. Самая простая задача после Task 2: один роут, одна
зависимость. Покрытие — `test_v4810_web_behavioral.py`, `test_v481_web_unban.py`.

### Task 4: `web/admin_cleanup.py`

**Files:** Create `web/admin_cleanup.py`; Modify `web_app.py:3148-3167` (хелпер),
`web_app.py:3817-3979` (роуты).

| Метод | Путь | Функция | Строки | Зависимости |
|---|---|---|---|---|
| GET | `/admin/cleanup` | `admin_cleanup_page_legacy` | 3817–3830 (14) | — |
| POST | `/admin/cleanup` | `admin_cleanup_apply` | 3831–3979 (149) | `_cleanup_counts`, `_wal_checkpoint` |

Вложенный хелпер `_cleanup_counts` (`web_app.py:3148`, 20 строк) переезжает
сюда и становится модульной функцией — её импортируют задачи 5 и 8.
`_wal_checkpoint` остаётся в `web_app.py`, зовётся как `web_app._wal_checkpoint`.

**Ожидаемое падение:** `test_v487_sanity.py` [11] считает `asyncio.to_thread`
в `web_app.py` и требует ≥ 7. Часть вызовов уезжает сюда. Проверку заменить
на сумму по файлам:

```python
# v4.9.0 (Task 10): вызовы разъехались по web/ вместе с роутами.
sources = [_P("web_app.py"), _P("web/admin_cleanup.py"), _P("web/admin_settings.py")]
to_thread_count = sum(
    len(re.findall(r"asyncio\.to_thread\s*\(", open(p).read()))
    for p in sources if os.path.exists(p)
)
assert to_thread_count >= 7, f"expected ≥7 asyncio.to_thread calls, got {to_thread_count}"
```

### Task 5: `web/admin_keywords.py`

**Files:** Create `web/admin_keywords.py`; Modify `web_app.py:2971-3147`.

| Метод | Путь | Функция | Строки | Зависимости |
|---|---|---|---|---|
| GET | `/admin/keywords` | `admin_keywords_page` | 2971–3009 (39) | `templates` |
| POST | `/admin/keywords/add` | `admin_keywords_add` | 3010–3075 (66) | — |
| POST | `/admin/keywords/{keyword_id:int}/delete` | `admin_keywords_delete` | 3076–3106 (31) | — |
| POST | `/admin/keywords/{keyword_id:int}/toggle-ban-night` | `admin_keywords_toggle_ban_night` | 3107–3147 (61) | `_cleanup_counts` |

Первый межмодульный импорт: `from web.admin_cleanup import _cleanup_counts`.
Цикла не возникает — `admin_cleanup` ни от кого из `web/` не зависит.

### Task 6: `web/api.py` (+4 роута)

**Files:** Modify `web/api.py`; Modify `web_app.py:1217-1326`, `4715-4906`.

| Метод | Путь | Функция | Строки | Зависимости |
|---|---|---|---|---|
| GET | `/api/dashboard` | `api_dashboard` | 1217–1295 (79) | — |
| GET | `/api/search` | `api_search` | 1296–1326 (31) | — |
| POST | `/api/unban` | `api_unban` | 4715–4845 (131) | `bot` |
| POST | `/api/reset-automute-count` | `api_reset_automute_count` | 4846–4906 (61) | — |

Обкатка `get_bot`. `api_unban` несёт инвариант из `CLAUDE.md`: разбан требует
привязанного Telegram, встроенный `su` пишется на `_SU_WEB_MOD_ID = -1`.
Логика переносится дословно, покрытие — `test_v481_web_unban.py`.

### Task 7: `web/me.py` (+5 роутов)

**Files:** Modify `web/me.py`; Modify `web_app.py:1003-1216`, `3168-3324`.

| Метод | Путь | Функция | Строки | Зависимости |
|---|---|---|---|---|
| GET | `/dashboard` | `dashboard` | 1003–1149 (147) | `templates` |
| GET | `/user/{user_id:int}` | `user_page` | 1150–1216 (67) | `templates` |
| POST | `/me/password` | `me_change_password` | 3168–3237 (70) | — |
| GET | `/me` | `me_profile` | 3238–3276 (39) | `templates` |
| POST | `/me/avatar/refresh` | `me_avatar_refresh` | 3277–3324 (126) | `bot`, `_bot_info`, `_fetch_and_save_avatar` |

**Главный риск задачи.** `tests/test_v45_dashboard.py:547,563` патчат
`web_app._fetch_and_save_avatar` и дёргают `/me/avatar/refresh`. Вызов
обязан быть `web_app._fetch_and_save_avatar(...)` — при `from web_app import`
патч промахнётся, тест полезет в настоящий Telegram и упадёт.

`_bot_info` к этому моменту живёт в `web/admin_settings.py` (Task 8), поэтому
**Task 7 выполняется после Task 8** либо `_bot_info` временно вызывается из
`web_app`. Проще: поменять эти две задачи местами. Порядок в плане оставлен
как в спеке; при исполнении — сначала Task 8, затем Task 7.

### Task 8: `web/admin_settings.py`

**Files:** Create `web/admin_settings.py`; Modify `web_app.py:3325-3402` (хелпер
`_bot_info`), `3403-3574` (роуты), `3575-3588` (хелпер
`_load_github_settings_row`), `3589-3816` (роуты).

| Метод | Путь | Функция | Строки | Зависимости |
|---|---|---|---|---|
| GET | `/admin/settings` | `admin_settings_page` | 3403–3489 (87) | `templates`, `_bot_info`, `_cleanup_counts` |
| POST | `/admin/settings/backup` | `admin_settings_backup` | 3490–3525 (36) | `_wal_checkpoint` |
| POST | `/admin/settings/vacuum` | `admin_settings_vacuum` | 3526–3588 (63) | `_load_github_settings_row` |
| GET | `/admin/settings/github` | `admin_settings_github_get` | 3589–3615 (27) | `_load_github_settings_row` |
| POST | `/admin/settings/github` | `admin_settings_github_post` | 3616–3714 (99) | `_load_github_settings_row` |
| POST | `/admin/settings/github/test` | `admin_settings_github_test` | 3715–3816 (102) | `_load_github_settings_row` |

Два вложенных хелпера переезжают сюда: `_bot_info` (78 строк) и
`_load_github_settings_row` (14 строк). `_cleanup_counts` импортируется из
`web/admin_cleanup.py`.

**Ruff:** внутри `_bot_info` — блокирующий `open("/proc/self/status")`
(`web_app.py:3358`). Ruff увидит `ASYNC230` в новом файле. Добавить в
`pyproject.toml`:

```toml
# ASYNC230 — blocking open("/proc/self/status") в _bot_info. Долг из той же
# серии, что чинил v4.8.7; переехал сюда вместе с хелпером в v4.9.0.
# Снимается в Task 6 плана стабилизации.
"web/admin_settings.py" = ["ASYNC230"]
```

**Ожидаемое падение:** `test_v486_settings_render.py` читает `web_app.py` —
проверить, какие подстроки ищет, и переадресовать по правилу выше.

### Task 9: `web/admin_users.py`

**Files:** Create `web/admin_users.py`; Modify `web_app.py:1327-1888`.

| Метод | Путь | Функция | Строки | Зависимости |
|---|---|---|---|---|
| GET | `/admin/users` | `admin_users_page` | 1327–1407 (81) | `templates` |
| POST | `/admin/users/create` | `admin_users_create` | 1408–1601 (194) | `bot`, `_fetch_and_save_avatar` |
| POST | `/admin/users/{user_id:int}/toggle` | `admin_users_toggle` | 1602–1618 (17) | — |
| POST | `/admin/users/{user_id:int}/reset` | `admin_users_reset` | 1619–1637 (19) | — |
| POST | `/admin/users/{user_id:int}/role` | `admin_users_change_role` | 1638–1689 (52) | — |
| POST | `/admin/users/{user_id:int}/edit-chats` | `admin_users_edit_chats` | 1690–1753 (64) | — |
| POST | `/admin/users/{user_id:int}/bind-tg` | `admin_users_bind_tg` | 1754–1849 (96) | `bot`, `_fetch_and_save_avatar` |
| POST | `/admin/users/{user_id:int}/delete` | `admin_users_delete` | 1850–1888 (39) | — |

`_fetch_and_save_avatar` — снова через `web_app.` (см. Task 7).

### Task 10: `web/admin_presets.py`

**Files:** Create `web/admin_presets.py`; Modify `web_app.py:3980-4580`.

| Метод | Путь | Функция | Строки | Зависимости |
|---|---|---|---|---|
| GET | `/admin/presets` | `admin_presets_page` | 3980–4042 (63) | `templates` |
| POST | `/admin/presets/create` | `admin_presets_create` | 4043–4163 (121) | late `import bot` (4140) |
| POST | `/admin/presets/{preset_id:int}/edit` | `admin_presets_edit` | 4164–4292 (129) | late `import bot` (4283) |
| POST | `/admin/presets/{preset_id:int}/delete` | `admin_presets_delete` | 4293–4342 (50) | late `import bot` (4326) |
| POST | `/admin/presets/words/add` | `admin_presets_words_add` | 4343–4432 (90) | — |
| POST | `/admin/presets/words/{word_id:int}/delete` | `admin_presets_words_delete` | 4433–4472 (40) | — |
| POST | `/admin/presets/links/add` | `admin_presets_links_add` | 4473–4538 (66) | — |
| POST | `/admin/presets/links/{link_id:int}/delete` | `admin_presets_links_delete` | 4539–4580 (42) | — |

Три места делают `import bot as _bot_module` внутри функции и зовут
`_bot_module._invalidate_day_default_cache()`. Переносятся дословно, наверх
файла не поднимаются — это защита от цикла `bot.py` → `web_app.py`.

**Ожидаемое падение:** `test_v475_wordfilter_linkallowlist_ui.py` грепает
`web_app.py` на пути `/admin/presets/words/*`, `/links/*` и имена
`WordFilter`, `LinkAllowlist`. Переадресовать на `web/admin_presets.py`.

### Task 11: `web/admin_chats.py`

**Files:** Create `web/admin_chats.py`; Modify `web_app.py:1889-2970`.

| Метод | Путь | Функция | Строки | Зависимости |
|---|---|---|---|---|
| GET | `/admin/chats` | `admin_chats_page` | 1889–1962 (74) | `templates` |
| POST | `/admin/chats/{chat_id_str}/update` | `admin_chats_update` | 1963–2290 (328) | late `app_state` (2355, 2377) |
| POST | `/admin/chats/{chat_id_str}/toggle` | `admin_chats_toggle` | 2291–2474 (184) | `bot` |
| POST | `/admin/chats/{chat_id_str}/delete` | `admin_chats_delete` | 2475–2593 (119) | `bot` |
| POST | `/admin/chats/{chat_id_str}/sync-admins` | `admin_chats_sync_admins` | 2594–2843 (250) | `bot` |
| POST | `/admin/chats/{chat_id_str}/sanitary/add` | `admin_chats_sanitary_add` | 2844–2910 (67) | — |
| POST | `/admin/chats/{chat_id_str}/sanitary/{idx_str}/delete` | `admin_chats_sanitary_delete` | 2911–2970 (60) | — |

Самый крупный модуль (1082 строки) и самый нагруженный тестами (17 файлов
упоминают `/admin/chats`). `admin_chats_update` — 328 строк, крупнейший роут
проекта; внутри late-импорты `from app_state import get_exit_night_mode` и
`get_exit_sanitary_day`, переносятся как есть.

**Ожидаемое падение:** `test_v476_sanitary_ui_cleanup.py` грепает `web_app.py`
на `/sanitary/add`, `/sanitary/{idx_str}/delete`, `admin_chats_sanitary_add`,
`admin_chats_sanitary_delete`. Переадресовать на `web/admin_chats.py`.

---

## Task 12: Финализация

**Files:**
- Modify: `web/__init__.py` (докстрока)
- Modify: `CLAUDE.md` (карта модулей)
- Modify: `docs/superpowers/plans/2026-08-16-degramod-stabilization.md` (Task 10 → выполнено)
- Modify: `web_app.py`, `pyproject.toml`, `templates/base.html` (версия + changelog)

- [ ] **Step 1: Проверить размер `create_app`**

```bash
python3 -c "
import re, pathlib
src = pathlib.Path('web_app.py').read_text(encoding='utf-8').splitlines()
start = next(i for i,l in enumerate(src) if l.startswith('def create_app'))
end = next((i for i in range(start+1, len(src)) if src[i] and not src[i][0].isspace()), len(src))
print('create_app:', end-start, 'строк')
print('роутов внутри:', sum(1 for l in src[start:end] if re.match(r'^    @app\.\w+\(', l)))
"
```

Expected: ~137 строк, 0 роутов.

- [ ] **Step 2: Обновить докстроку `web/__init__.py`**

Убрать план на будущее и «TODO v4.9.0», описать фактическую структуру: 12
модулей, что в каком лежит, правило `import web_app`, правило late-импорта
роутеров в `create_app`.

- [ ] **Step 3: Обновить карту модулей в `CLAUDE.md`**

Заменить строки про `web_app.py (4.9k)` и `web/`:

```markdown
- `web_app.py` — конфигурация, авторизация, module-level хелперы и
  `create_app()` как сборщик (~137 строк). Роуты — в `web/`.
- `web/` — 12 модулей с роутами по предметным областям: `auth`, `me`,
  `api`, `health`, `admin_bans`, `admin_chats`, `admin_cleanup`,
  `admin_keywords`, `admin_presets`, `admin_settings`, `admin_users`,
  плюс `deps.py` с зависимостями. `bot` и `templates` приходят через
  `app.state` + `Depends`, не через замыкание.
```

Добавить в «Известные ловушки»:

```markdown
- **Роутеры импортируются только внутри `create_app()`.** Top-level импорт
  `web.*` в `web_app.py` даёт цикл `web_app → web.X → web.deps → web_app`.
- **Модули `web/` зовут хелперы как `web_app._helper(...)`.** Через
  `from web_app import _helper` ломаются тесты, патчащие атрибут модуля.
```

- [ ] **Step 4: Отметить Task 10 выполненным в плане стабилизации**

Статус в таблице → `✅ сделано`, версия `v4.10.0`, коммиты — диапазон.
Заголовок `### Task 10` — снять `⚠️ ЧАСТИЧНО`.

- [ ] **Step 5: Версия и changelog**

`APP_VERSION = "v4.10.0"` в `web_app.py`, `version = "4.10.0"` в
`pyproject.toml`, `uv lock`, запись в `templates/base.html`.

MINOR, а не PATCH: меняется внутренняя архитектура, хотя поведение то же.

- [ ] **Step 6: Финальная проверка**

```bash
uv run python tools/run_tests.py
uv run ruff check .
docker build -t degramod-test .
```

Expected: 66/66 PASS; All checks passed; образ собирается.

- [ ] **Step 7: Коммит**

```bash
git add -A
git commit -m "docs: Task 10 закрыт — create_app стал сборщиком

47 роутов вынесены в 10 модулей web/, create_app ужался с 4135 строк до
~137. Поведение не менялось: вся сюита прошла без правок, кроме пяти
grep-проверок, переадресованных на новые файлы."
```

---

## Self-Review

**Покрытие спеки.** §2 (что вынесено) — контекст, задач не требует. §3
(инвентаризация) → задачи 2–11, все 47 роутов распределены. §4 (архитектура)
→ Task 1. §5.1 (позднее связывание) → Global Constraints + Task 7. §5.2
(module-level хелперы) → Global Constraints. §5.3 (вложенные хелперы) →
задачи 4 и 8. §5.4 (CSRF не трогается) → Task 1 кладёт готовый объект в
state. §5.5 (late imports) → задачи 10, 11 + Global Constraints. §5.6
(порядок роутеров) → сохраняется, отдельной задачи не требует. §5.7 (ruff) →
Task 8. §6 (порядок) → задачи 2–11. §7 (проверка) → Task 1 даёт счётчик,
каждая задача гоняет сюиту. §8 (риски) → Global Constraints. §9 (вне объёма)
→ ничего из этого в плане нет.

**Расхождение со спекой, внесённое сознательно.** Спека ставит `me` перед
`admin_settings`; план меняет их местами при исполнении, потому что
`/me/avatar/refresh` зовёт `_bot_info`, который переезжает в
`admin_settings`. Отмечено в Task 7.

**Дополнение к спеке.** Раздел про grep-тесты — находка, сделанная при
написании плана: три файла сюиты читают `web_app.py` как текст. Спека этого
не содержит; правило «краснеет тест — чини код» для них не работает, нужна
переадресация. Внести в спеку отдельной правкой.

**Плейсхолдеры.** Отсутствуют: у каждой задачи точные пути, номера строк,
имена функций и сигнатуры. Единственное место, где план говорит «проверить,
какие подстроки ищет» — Task 8 про `test_v486_settings_render.py`; это
осознанно, файл читает исходник, но конкретные проверки зависят от того,
что к тому моменту останется в `web_app.py`.

**Согласованность имён.** `get_templates` / `get_bot` объявлены в Task 1 и
используются в задачах 2–11 с теми же именами. `_cleanup_counts` создаётся в
Task 4, потребляется в 5 и 8. `_bot_info` создаётся в Task 8, потребляется в
7. `_walk` / `_route_pairs` — только внутри тестового файла Task 1.
