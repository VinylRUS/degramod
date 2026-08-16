# Degramod: стабилизация и синхронизация roadmap — план внедрения

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Убрать причины, по которым бот регулярно ломается, и привести `roadmap.md` в соответствие с фактическим состоянием кода — чтобы дальнейшие фичи ставились на проверяемый фундамент.

**Architecture:** Работа идёт снизу вверх. Сначала появляется инструментарий (uv, pytest, ruff, CI) — без него любую правку невозможно проверить. Затем чинятся рантайм-баги, которые роняют бота в проде. Затем закрывается веб-панель. Декомпозиция гигантских функций идёт последней, уже под защитой тестов, и только для тех участков, которые реально трогаем.

**Tech Stack:** Python 3.14, aiogram 3.30, FastAPI, SQLAlchemy 2.x (async) + aiosqlite, Jinja2, uv, pytest + pytest-asyncio, ruff.

**Spec:** `roadmap.md` (в части будущих версий) + аудит расхождений в разделе «Состояние roadmap» ниже.

## Global Constraints

- Python 3.14 — версия, на которой ведётся работа над репозиторием сейчас. 3.11 в `Dockerfile` достался от первоначальной сборки проекта и никакими зависимостями не обусловлен, поэтому выравнивание идёт вверх, на актуальную версию, а не вниз. Совместимость проверена на 3.14.6 — все зависимости из `requirements.txt` в запиненных версиях ставятся, все модули импортируются, `init_db()` отрабатывает без `DeprecationWarning`, приложение FastAPI поднимается со всеми 54 роутами.
- Язык комментариев и пользовательских строк — русский.
- Инварианты режимов чата из `chat_modes.py:1-36` и `roadmap.md` §2 не нарушаются ни одной задачей.
- Схема БД меняется только через миграцию, которая идёт **до** любого ORM-запроса к затронутой таблице (`init_db`).
- Ни одна задача не меняет поведение команд бота, кроме тех, где это явно заявлено.
- Каждая задача заканчивается коммитом в ветке `dev`.

---

## Состояние roadmap: что реализовано, что нет

Сверка `roadmap.md` с кодом на 16 августа 2026. `roadmap.md` отстал примерно на две версии — он датирован компиляцией 16 августа, но описывает состояние на 13-е.

| Пункт roadmap | Статус в roadmap | Фактически в коде | Доказательство |
|---|---|---|---|
| v4.8.1 – v4.8.4 | ✅ релизнуты | ✅ подтверждено | `templates/base.html:760-825` |
| **v4.8.5** `!idea` → GitHub | 📋 запланировано | ✅ **реализовано** + 4 хотфикса | `github_client.py`, `db.py:665` `IdeaLog`, `db.py:700` `GithubSettings`, `bot_handlers.py:6530` `cmd_idea_dm`, `web_app.py:3339-3465` |
| **v4.8.6** финальная чистка | 📋 запланировано | ⚠️ **реализовано частично** | `APP_VERSION = "v4.8.6"`, `web_app.py:161` |
| └ удаление `WordFilter` | запланировано | ❌ **решение отменено** | `db.py:357` модель активна, `web_app.py:3732` CRUD живой, `base.html:655` «модель активна, не deprecated» |
| └ удаление stub `/addword` и др. | запланировано | ✅ сделано | `web_app.py:4066` |
| └ чистка Settings UI | запланировано | ✅ сделано | `base.html:644` |
| └ удаление debug-`print` | запланировано | ❌ остался один | `db.py:1324` `print(logger_info)` |
| └ полный регресс тестов | запланировано | ❌ **невозможно** | каталог `scripts/` удалён из git, коммит `351b6d8` |
| v4.9.0 §1.1 перенос `_alarm_auto_off_tick` | ⏳ | ❌ не сделано | `bot.py:218`, открыт Issue #15 |
| v4.9.0 §1.2 behavioral-тесты | ⏳ | ❌ не сделано | тестов в репозитории нет, открыт Issue #16 |
| v4.9.0 §2.1 `/mywarns` | ⏳ | ❌ не сделано | 0 совпадений в коде, открыт Issue #6 |
| **v5.0.0-01** блок «Информация о боте» | 📋 | ⚠️ **сделано досрочно** в v4.8.6 | `web_app.py:3100` `_bot_info()`: uptime, RSS, версия Python, размер БД, бейдж Online |
| **v5.0.0-07** `/healthz` | 📋 | ⚠️ **частично, под другим именем** | `web_app.py:652` `/health` отдаёт только `status/service/version/time`; нет memory, `telegram_connected`, latency, кода 503 |
| v5.0.0-02..06, 08 | 📋 | ❌ не сделано | `bothost_agent.py` отсутствует |
| B2-бэкапы (`backup.py`) | отложено | ❌ не сделано — соответствует roadmap | файла нет |

Три расхождения требуют не кода, а решения:

1. **`WordFilter` не удалён, и это осознанный разворот.** Issue #7 «Финальная чистка кода и удаление WordFilter» закрыт, но модель осталась активной и управляется через `/admin/presets`. Пункт roadmap §8.2 и строка §0 «Модели … без `WordFilter` после v4.8.1» описывают несуществующее состояние.
2. **Тесты, на которые ссылается roadmap, удалены из репозитория.** `roadmap.md` перечисляет `scripts/test_v481_*.py`, `test_v484_*.py` и ещё ~30 файлов; changelog v4.8.6 утверждает «14/14 тестов проходят». В git их нет. Либо они восстанавливаются из локальной копии друга, либо все ссылки на них из roadmap убираются как недостоверные.
3. **Схема деплоя в roadmap §12 не описывает реальность.** Там zip-архивы в `/home/z/my-project/download/`; фактически деплой — git + `Dockerfile`. Этот путь — артефакт среды, в которой roadmap генерировался.

---

## File Structure

Новые файлы:

- `pyproject.toml` — метаданные проекта, зависимости, конфигурация ruff и pytest. Заменяет `requirements.txt`.
- `uv.lock` — лок-файл полного дерева зависимостей (генерируется, коммитится).
- `.python-version` — `3.11`.
- `tests/conftest.py` — общие фикстуры: изолированная БД, `TestClient`, аутентифицированный клиент.
- `tests/test_web_auth.py`, `tests/test_web_bans.py`, `tests/test_telegram_resilience.py`, `tests/test_db_migrations.py` — по одному файлу на предметную область.
- `tg_retry.py` — единая обёртка над вызовами Bot API с обработкой `TelegramRetryAfter`.
- `task_registry.py` — хранение ссылок на фоновые задачи, чтобы их не собирал GC.
- `.github/workflows/ci.yml` — линт и тесты на push/PR.

Изменяемые файлы перечислены в каждой задаче.

---

## Phase 0 — Фундамент

Без этой фазы остальные не проверяемы. Порядок внутри фазы обязателен.

### Task 1: Перевод на uv и pyproject ✅ ВЫПОЛНЕНО

**Статус:** закрыт 16 августа 2026, коммит `fc81aa3`. Все семь шагов пройдены, включая сборку и запуск образа. Три вещи всплыли по ходу и внесены в текст ниже: `[tool.uv] package = false`, настройка ruff под кириллицу и `--no-sync` в `CMD`.

**Files:**
- Create: `pyproject.toml`, `.python-version`
- Delete: `requirements.txt`
- Modify: `Dockerfile:1-9`

**Interfaces:**
- Produces: воспроизводимое окружение; команда `uv run pytest` доступна всем последующим задачам.

- [x] **Step 1: Создать `.python-version`**

Патч указывается точно, а не маской `3.14`. С маской uv берёт любой уже установленный 3.14.x — на практике он выбрал давно скачанный 3.14.0, тогда как образ `python:3.14-slim` тянет свежий патч. Расхождение dev/prod возвращается, просто уровнем ниже.

```
3.14.7
```

- [x] **Step 2: Создать `pyproject.toml`**

Версии зависимостей взяты из текущего `requirements.txt`. Три незапиненные (`Pillow`, `rlottie-python`, `cryptography`) получают нижнюю границу как было — точные версии зафиксирует `uv.lock`.

```toml
[project]
name = "degramod"
version = "4.8.6"
description = "Telegram-бот-модератор с веб-панелью"
requires-python = ">=3.14,<3.15"
dependencies = [
    "aiogram==3.30.0",
    "aiosqlite==0.20.0",
    "sqlalchemy[asyncio]==2.0.36",
    "fastapi==0.115.6",
    "uvicorn[standard]==0.34.0",
    "jinja2==3.1.5",
    "python-multipart==0.0.18",
    "aiohttp==3.13.3",
    "Pillow>=10.0.0",
    "rlottie-python>=1.0.0",
    "cryptography>=42.0.0",
]

[tool.uv]
# Приложение, а не библиотека: модули лежат в корне (bot.py, db.py, ...),
# устанавливать проект в venv не нужно — только его зависимости.
package = false

[dependency-groups]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.24",
    "httpx>=0.27",
    "ruff>=0.6",
]

[tool.ruff]
line-length = 100
target-version = "py314"

[tool.ruff.lint]
select = ["E", "F", "W", "B", "ASYNC", "RUF"]
ignore = [
    "E501",
    # RUF001-003 ругаются на кириллицу, похожую на латиницу. В русскоязычной
    # кодовой базе это 1296 срабатываний подряд и ноль полезного сигнала.
    "RUF001",
    "RUF002",
    "RUF003",
    # B008 запрещает вызов функции в значении аргумента по умолчанию, но
    # Depends(...) в сигнатуре — это основная идиома FastAPI, а не ошибка.
    "B008",
]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

- [x] **Step 3: Сгенерировать лок и окружение**

Run: `uv python install 3.14.7 && uv sync`
Expected: создаётся `uv.lock` и `.venv` на Python 3.14.7. Фактически — 52 пакета, лок на 187 КБ.

- [x] **Step 4: Проверить, что бот поднимается**

Проверяются три уровня: импорт модулей, реальная работа SQLAlchemy с миграциями и сборка приложения FastAPI. `DeprecationWarning` как ошибка — чтобы поймать то, что 3.14 пометил к удалению.

```bash
BOT_TOKEN=1:test WEB_PASSWORD=x SESSION_SECRET=y DB_PATH=/tmp/degramod-check.db \
  uv run python -c "
import asyncio, warnings
warnings.simplefilter('error', DeprecationWarning)
import db, web_app, bot_handlers, chat_modes, modchat, github_client, sticker_cache

async def main():
    await db.init_db()
    await db.engine.dispose()

asyncio.run(main())
app = web_app.create_app(bot=None)
print('OK, роутов:', len(app.routes))
"
```

Expected: `OK, роутов: 54`.

Фактически: `init_db()` проходит начисто, приложение поднимается. Но со строгим фильтром всплывает `DeprecationWarning` из FastAPI 0.115.6 — см. Task 17, это не блокер, а дата истечения срока годности зависимости.

- [x] **Step 5: Обновить `Dockerfile`**

```dockerfile
FROM python:3.14.7-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

RUN mkdir -p /app/data && chmod 777 /app/data

COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev

COPY . .

EXPOSE 3000

CMD ["uv", "run", "--no-sync", "python", "bot.py"]
```

`--no-sync` в `CMD` обязателен: зависимости уже установлены слоем выше, и без флага `uv run` при каждом старте контейнера идёт сверять окружение с локом.

- [x] **Step 6: Проверить сборку образа**

Run: `docker build -t degramod:uv-test .`
Expected: сборка проходит, слой `uv sync` кэшируется отдельно от кода.

Риск сборки из исходников был снят до самой сборки — чтением лока: у всех 13 пакетов с нативным кодом (`aiohttp`, `Pillow`, `cryptography`, `rlottie-python`, `uvloop`, `httptools`, `multidict`, `yarl`, `frozenlist`, `propcache`, `pyyaml`, `watchfiles`, `websockets`) есть готовые manylinux-колёса под x86_64/cp314 либо стабильный ABI. Компилятор в slim-образе не нужен.

- [x] **Step 6a: Прогнать проверку из Step 4 внутри образа**

Run: `docker run --rm -e BOT_TOKEN=1:test -e WEB_PASSWORD=x -e SESSION_SECRET=y -e DB_PATH=/app/data/check.db degramod:uv-test uv run python -c "import db, web_app; print('ok')"`
Expected: `ok`.

- [x] **Step 7: Удалить `requirements.txt` и закоммитить**

```bash
git rm requirements.txt
git add pyproject.toml uv.lock .python-version Dockerfile
git commit -m "build: переход на uv, Python 3.14.7 и лок зависимостей"
```

Сделано коммитом `fc81aa3`.

### Task 2: Тестовый харнесс

**Files:**
- Create: `tests/conftest.py`, `tests/test_smoke.py`

**Interfaces:**
- Produces: фикстуры `test_db` (изолированная файловая SQLite с прогнанным `init_db`), `client` (FastAPI `TestClient`), `su_client` (клиент с валидной SU-сессией). Все последующие тестовые задачи их используют.

Файловая, а не in-memory БД: `init_db` открывает несколько независимых соединений через `engine.begin()`, а `:memory:` у каждого соединения своя.

- [ ] **Step 1: Написать `tests/conftest.py`**

```python
"""Общие фикстуры. БД — временный файл: init_db открывает несколько
соединений, а in-memory SQLite у каждого соединения своя."""
import importlib
import os
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def test_env(monkeypatch):
    """Изолированное окружение: своя БД, известные секреты."""
    tmpdir = tempfile.mkdtemp()
    db_path = str(Path(tmpdir) / "test.db")
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("BOT_TOKEN", "1:test")
    monkeypatch.setenv("WEB_PASSWORD", "test-su-password")
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret")
    monkeypatch.setenv("ADMIN_IDS", "")
    monkeypatch.setenv("GITHUB_PAT_ENC_KEY", "")
    return {"db_path": db_path, "tmpdir": tmpdir}


@pytest.fixture
async def test_db(test_env):
    """Модуль db перезагружается, чтобы engine подхватил DB_PATH."""
    import db as db_module
    importlib.reload(db_module)
    await db_module.init_db()
    yield db_module
    await db_module.engine.dispose()


@pytest.fixture
def client(test_db):
    from fastapi.testclient import TestClient
    import web_app as web_app_module
    importlib.reload(web_app_module)
    app = web_app_module.create_app(bot=None)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def su_client(client):
    """Клиент с активной SU-сессией."""
    resp = client.post(
        "/login",
        data={"username": "su", "password": "test-su-password"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, f"login не прошёл: {resp.status_code}"
    return client
```

- [ ] **Step 2: Написать smoke-тест**

```python
def test_health_endpoint_отвечает(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_dashboard_без_логина_редиректит(client):
    resp = client.get("/dashboard", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_su_логинится_и_видит_дашборд(su_client):
    resp = su_client.get("/dashboard")
    assert resp.status_code == 200
```

- [ ] **Step 3: Запустить**

Run: `uv run pytest tests/test_smoke.py -v`
Expected: 3 passed. Если `create_app(bot=None)` падает — зафиксировать, где именно нужен живой `bot`, и передать заглушку через `unittest.mock.AsyncMock`.

- [ ] **Step 4: Прогнать линтер**

Run: `uv run ruff check --statistics .`
Expected: 78 замечаний по легаси — это замеренный базовый уровень на момент Task 1. Ошибки в новых файлах чинятся сразу; легаси в этой задаче не трогается.

Замер независимо подтвердил две находки плана: `RUF006 asyncio-dangling-task` ровно 5 штук — те же `create_task` из Task 5, и `ASYNC240`/`ASYNC230` восемь штук — блокирующий ввод-вывод в async-функциях из Task 6. Остальное — мелочь на разовую чистку: 26 `F541` (f-строки без подстановок), 18 `F401` (неиспользуемые импорты), 10 `E402`, 3 `B904`.

- [ ] **Step 5: Коммит**

```bash
git add tests/
git commit -m "test: тестовый харнесс — изолированная БД, TestClient, SU-сессия"
```

### Task 3: CI

**Files:**
- Create: `.github/workflows/ci.yml`

- [ ] **Step 1: Написать workflow**

```yaml
name: CI

on:
  push:
    branches: [main, dev]
  pull_request:

jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
        with:
          enable-cache: true
      - run: uv sync --frozen
      - name: Lint
        run: uv run ruff check .
      - name: Tests
        run: uv run pytest -v
```

- [ ] **Step 2: Проверить локально теми же командами**

Run: `uv sync --frozen && uv run ruff check . && uv run pytest -v`
Expected: тесты зелёные, ruff падает на 78 легаси-замечаниях (замер из Task 2).

Порядок такой: сначала этой же задачей чинятся 47 автоисправимых (`uv run ruff check --fix .` — это `F541`, `F401`, `W291` и мелкие `RUF`), затем прогоняется Step 4 из Task 1 для проверки, что ничего не сломалось. Оставшиеся ~31 (`E402`, `ASYNC240`, `RUF006`, `B904`) закрываются задачами Phase 1 — до тех пор CI по линту не блокирует, а только сообщает: добавить `continue-on-error: true` на шаг Lint и снять его, когда счётчик дойдёт до нуля.

- [ ] **Step 3: Коммит**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: линт и тесты на push и pull request"
```

---

## Phase 1 — Стабильность рантайма

Здесь чинится то, из-за чего бот падает в проде. Каждая задача сначала воспроизводит проблему тестом.

### Task 4: Обработка flood control Telegram

**Files:**
- Create: `tg_retry.py`, `tests/test_telegram_resilience.py`
- Modify: `bot_handlers.py` (вызовы `restrict_chat_member`, `ban_chat_member`, `delete_message`, `send_message`), `chat_modes.py:324` (`_apply_chat_permissions`)

**Interfaces:**
- Produces: `async def tg_call(coro_factory, *, attempts: int = 3) -> Any` — принимает фабрику корутины (не корутину: при ретрае нужен свежий awaitable), возвращает результат вызова.

- [ ] **Step 1: Написать падающий тест**

```python
import asyncio
import pytest
from aiogram.exceptions import TelegramRetryAfter


async def test_повтор_после_flood_control():
    from tg_retry import tg_call

    calls = []

    async def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise TelegramRetryAfter(method=None, message="Too Many Requests", retry_after=0)
        return "ok"

    assert await tg_call(lambda: flaky()) == "ok"
    assert len(calls) == 2


async def test_сдаётся_после_исчерпания_попыток():
    from tg_retry import tg_call

    async def always_flooded():
        raise TelegramRetryAfter(method=None, message="Too Many Requests", retry_after=0)

    with pytest.raises(TelegramRetryAfter):
        await tg_call(lambda: always_flooded(), attempts=2)
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `uv run pytest tests/test_telegram_resilience.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tg_retry'`.

- [ ] **Step 3: Написать `tg_retry.py`**

```python
"""Единая обёртка над вызовами Telegram Bot API.

Aiogram сам не ретраит flood control: при 429 он бросает TelegramRetryAfter
с полем retry_after. Без обработки хендлер просто падает в лог, и действие
модератора теряется. Обёртка ждёт указанное время и повторяет вызов.

Принимает фабрику корутины, а не корутину: повторно awaitнуть один и тот же
объект нельзя, для каждой попытки нужен свежий.
"""
import asyncio
import logging
from typing import Any, Callable, Coroutine

from aiogram.exceptions import TelegramRetryAfter

logger = logging.getLogger("shadow_logger")

MAX_SLEEP = 60


async def tg_call(
    coro_factory: Callable[[], Coroutine[Any, Any, Any]],
    *,
    attempts: int = 3,
) -> Any:
    """Вызывает Bot API, переживая flood control.

    Ждём ровно столько, сколько просит Telegram, но не дольше MAX_SLEEP —
    иначе один залипший вызов держит хендлер минутами.
    """
    for attempt in range(1, attempts + 1):
        try:
            return await coro_factory()
        except TelegramRetryAfter as e:
            if attempt == attempts:
                logger.error("tg_call: flood control не отпустил за %d попыток", attempts)
                raise
            delay = min(e.retry_after, MAX_SLEEP)
            logger.warning(
                "tg_call: flood control, ждём %ss (попытка %d/%d)",
                delay, attempt, attempts,
            )
            await asyncio.sleep(delay)
```

- [ ] **Step 4: Запустить тесты**

Run: `uv run pytest tests/test_telegram_resilience.py -v`
Expected: 2 passed.

- [ ] **Step 5: Применить обёртку к операциям модерации**

Оборачиваются только те вызовы, потеря которых означает несработавшую модерацию: `restrict_chat_member`, `ban_chat_member`, `unban_chat_member`, `set_chat_permissions` в `_apply_chat_permissions`. Отправка уведомлений и удаление сообщений — не оборачиваются, они не критичны и уже под `try/except`.

Шаблон замены:

```python
# было
await message.bot.restrict_chat_member(chat_id, user_id, permissions=perms, until_date=until)
# стало
await tg_call(lambda: message.bot.restrict_chat_member(
    chat_id, user_id, permissions=perms, until_date=until,
))
```

- [ ] **Step 6: Прогнать весь набор тестов**

Run: `uv run pytest -v`
Expected: все зелёные, регрессий нет.

- [ ] **Step 7: Коммит**

```bash
git add tg_retry.py tests/test_telegram_resilience.py bot_handlers.py chat_modes.py
git commit -m "fix: переживать flood control Telegram при операциях модерации"
```

### Task 5: Фоновые задачи не теряются

**Files:**
- Create: `task_registry.py`
- Modify: `bot_handlers.py:3273, 4727, 7962, 8692, 8728`
- Test: `tests/test_task_registry.py`

**Interfaces:**
- Consumes: ничего.
- Produces: `def spawn(coro, name: str) -> asyncio.Task` — создаёт задачу, держит на неё сильную ссылку до завершения и логирует исключения.

- [ ] **Step 1: Написать падающий тест**

```python
import asyncio


async def test_задача_живёт_до_завершения():
    from task_registry import spawn, _tasks

    done = []

    async def work():
        await asyncio.sleep(0)
        done.append(True)

    task = spawn(work(), name="test-work")
    assert task in _tasks, "ссылка на задачу не удержана — её может собрать GC"
    await task
    assert done == [True]
    assert task not in _tasks, "завершённая задача должна убираться из реестра"


async def test_исключение_логируется_и_не_всплывает(caplog):
    from task_registry import spawn

    async def boom():
        raise ValueError("сломалось")

    task = spawn(boom(), name="test-boom")
    await asyncio.gather(task, return_exceptions=True)
    assert "test-boom" in caplog.text
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `uv run pytest tests/test_task_registry.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'task_registry'`.

- [ ] **Step 3: Написать `task_registry.py`**

```python
"""Реестр фоновых задач.

asyncio держит на задачу только слабую ссылку. Если результат create_task
никуда не сохранён, сборщик мусора может уничтожить её на середине —
именно поэтому эфемерные сообщения удалялись через раз. Реестр держит
сильную ссылку до завершения и логирует исключения, которые иначе
проглатывались бы молча.
"""
import asyncio
import logging
from typing import Any, Coroutine

logger = logging.getLogger("shadow_logger")

_tasks: set[asyncio.Task] = set()


def spawn(coro: Coroutine[Any, Any, Any], name: str) -> asyncio.Task:
    """Создаёт фоновую задачу, которую не потеряет сборщик мусора."""
    task = asyncio.create_task(coro, name=name)
    _tasks.add(task)

    def _done(t: asyncio.Task) -> None:
        _tasks.discard(t)
        if not t.cancelled() and t.exception() is not None:
            logger.exception("Фоновая задача %s упала", t.get_name(), exc_info=t.exception())

    task.add_done_callback(_done)
    return task
```

- [ ] **Step 4: Запустить тесты**

Run: `uv run pytest tests/test_task_registry.py -v`
Expected: 2 passed.

- [ ] **Step 5: Заменить все пять вызовов**

```python
# было (bot_handlers.py:3273)
asyncio.create_task(_del_ephemeral())
# стало
spawn(_del_ephemeral(), name=f"ephemeral-delete-{chat_id}")
```

Остальные четыре — по тому же образцу, имя задачи осмысленное: `msg-delete`, `avatar-fetch`, и т. д.

- [ ] **Step 6: Проверить, что необработанных `create_task` не осталось**

Run: `grep -n 'asyncio.create_task' bot_handlers.py modchat.py`
Expected: пусто (в `bot.py` остаётся `tg.create_task` внутри `TaskGroup` — это корректно).

- [ ] **Step 7: Коммит**

```bash
git add task_registry.py tests/test_task_registry.py bot_handlers.py modchat.py
git commit -m "fix: удерживать ссылки на фоновые задачи и логировать их падения"
```

### Task 6: Убрать блокировку event loop в веб-панели

**Files:**
- Modify: `web_app.py:143` (`_wal_checkpoint`), `3139`, `3249-3283` (backup, vacuum), `3582-3707` (cleanup), `db.py:47`
- Test: `tests/test_web_maintenance.py`

Синхронный `sqlite3`, `shutil.copy2` и `VACUUM` выполняются прямо в async-роутах. Пока SU жмёт «Backup», встаёт весь процесс — бот не отвечает ни в одном чате.

- [ ] **Step 1: Написать тест, что backup не блокирует loop**

```python
import asyncio
import time


async def test_backup_не_блокирует_event_loop(su_client):
    """Пока идёт бэкап, loop обязан обслуживать другие корутины."""
    ticks = []

    async def heartbeat():
        for _ in range(20):
            await asyncio.sleep(0.01)
            ticks.append(time.monotonic())

    hb = asyncio.create_task(heartbeat())
    await asyncio.to_thread(su_client.post, "/admin/settings/backup", follow_redirects=False)
    await hb

    gaps = [b - a for a, b in zip(ticks, ticks[1:])]
    assert max(gaps) < 0.2, f"event loop вставал на {max(gaps):.2f}s — блокирующий вызов"
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `uv run pytest tests/test_web_maintenance.py -v`
Expected: FAIL — максимальный разрыв заметно больше порога.

- [ ] **Step 3: Вынести синхронную работу в тред**

Тела обработчиков не переписываются — синхронная часть выделяется в отдельную функцию и вызывается через `asyncio.to_thread`:

```python
def _do_backup_sync(db_path: str, backup_path: str) -> None:
    """Синхронная часть бэкапа. Вызывать только через asyncio.to_thread."""
    _wal_checkpoint()
    shutil.copy2(db_path, backup_path)


# в роуте
await asyncio.to_thread(_do_backup_sync, DB_PATH, backup_path)
```

Тот же приём для `VACUUM`, `_cleanup_counts` и счётчиков в `_bot_info`.

- [ ] **Step 4: Добавить `busy_timeout` движку**

```python
# db.py:47
engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    connect_args={"timeout": 30},
)
```

- [ ] **Step 5: Запустить тесты**

Run: `uv run pytest -v`
Expected: все зелёные, включая тест на блокировку.

- [ ] **Step 6: Коммит**

```bash
git add web_app.py db.py tests/test_web_maintenance.py
git commit -m "fix: обслуживание БД не блокирует event loop, busy_timeout 30s"
```

---

## Phase 2 — Безопасность веб-панели

### Task 7: Срок жизни сессии

**Files:**
- Modify: `web_app.py:179-204`
- Test: `tests/test_web_auth.py`

`_make_token` кладёт время выдачи в поле `t`, но `_verify_token` его никогда не читает — токен действует вечно.

- [ ] **Step 1: Написать падающий тест**

```python
import json
import time


def test_протухший_токен_отклоняется(client):
    import web_app

    payload = {"u": "su", "s": 1, "r": "su", "t": int(time.time()) - 8 * 86400, "n": "aa"}
    raw = json.dumps(payload, separators=(",", ":"))
    old_token = f"{raw}:{web_app._sign(raw)}"

    assert web_app._verify_token(old_token) is None, "токен старше 7 дней должен быть невалиден"


def test_свежий_токен_принимается(client):
    import web_app

    token = web_app._make_token("su", is_su=True, role="su")
    assert web_app._verify_token(token) is not None
```

- [ ] **Step 2: Запустить, убедиться что первый падает**

Run: `uv run pytest tests/test_web_auth.py -v`
Expected: FAIL на `test_протухший_токен_отклоняется`.

- [ ] **Step 3: Проверять возраст токена**

```python
SESSION_MAX_AGE = 7 * 86400  # совпадает с max_age куки


def _verify_token(token: str) -> dict | None:
    """Возвращает payload (dict) или None если токен невалиден или протух."""
    try:
        raw, signature = token.rsplit(":", 1)
        expected = _sign(raw)
        if not hmac.compare_digest(signature, expected):
            return None
        payload = json.loads(raw)
        if not {"u", "s", "t"} <= set(payload.keys()):
            return None
        # max_age куки задаёт клиент — проверяем возраст на сервере.
        issued = payload.get("t")
        if not isinstance(issued, (int, float)) or time.time() - issued > SESSION_MAX_AGE:
            return None
        return payload
    except (ValueError, json.JSONDecodeError):
        return None
```

- [ ] **Step 4: Запустить тесты**

Run: `uv run pytest tests/test_web_auth.py -v`
Expected: 2 passed.

- [ ] **Step 5: Коммит**

```bash
git add web_app.py tests/test_web_auth.py
git commit -m "fix: серверная проверка срока жизни сессионного токена"
```

### Task 8: Rate-limit логина не обходится заголовком

**Files:**
- Modify: `web_app.py:134`, `685-707`
- Test: `tests/test_web_auth.py`

`_client_ip` безусловно доверяет `X-Forwarded-For`, поэтому лимит в 5 попыток снимается подстановкой произвольного значения. Плюс пароль SU сравнивается оператором `!=`.

- [ ] **Step 1: Написать падающий тест**

```python
def test_подделка_xff_не_обходит_лимит(client):
    for i in range(12):
        client.post(
            "/login",
            data={"username": "su", "password": "неверный"},
            headers={"X-Forwarded-For": f"10.0.0.{i}"},
        )
    resp = client.post(
        "/login",
        data={"username": "su", "password": "неверный"},
        headers={"X-Forwarded-For": "10.0.0.99"},
    )
    assert resp.status_code == 429, "лимит обошли подстановкой X-Forwarded-For"
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `uv run pytest tests/test_web_auth.py::test_подделка_xff_не_обходит_лимит -v`
Expected: FAIL — получаем 200, лимит не сработал.

- [ ] **Step 3: Доверять заголовку только от известного прокси**

```python
# Список доверенных прокси. Пусто (дефолт) — заголовок игнорируется целиком,
# берётся реальный адрес соединения. Заполняется только если панель реально
# стоит за reverse proxy: TRUSTED_PROXIES=10.0.0.1,10.0.0.2
_TRUSTED_PROXIES = {
    ip.strip() for ip in os.getenv("TRUSTED_PROXIES", "").split(",") if ip.strip()
}


def _client_ip(request: Request) -> str:
    """IP клиента. X-Forwarded-For учитывается только от доверенного прокси —
    иначе любой запрос мог бы назначить себе произвольный адрес и обойти
    rate-limit на /login."""
    peer = request.client.host if request.client else "unknown"
    if peer in _TRUSTED_PROXIES:
        xff = request.headers.get("X-Forwarded-For", "")
        if xff:
            return xff.split(",")[0].strip()
    return peer
```

- [ ] **Step 4: Сделать сравнение пароля SU постоянного времени**

```python
if not WEB_PASSWORD or not hmac.compare_digest(password, WEB_PASSWORD):
```

- [ ] **Step 5: Запустить тесты**

Run: `uv run pytest tests/test_web_auth.py -v`
Expected: все зелёные.

- [ ] **Step 6: Отметить новую переменную в CLAUDE.md**

В таблицу переменных окружения добавляется строка `TRUSTED_PROXIES`.

- [ ] **Step 7: Коммит**

```bash
git add web_app.py tests/test_web_auth.py CLAUDE.md
git commit -m "fix: X-Forwarded-For только от доверенного прокси, постоянное сравнение пароля SU"
```

### Task 9: CSRF-токены на изменяющих запросах

**Files:**
- Modify: `web_app.py` (зависимость + 34 POST-роута), `templates/base.html` (макрос скрытого поля), все шаблоны с формами
- Test: `tests/test_web_csrf.py`

Сейчас единственная защита — `SameSite=lax`. Она закрывает основной вектор в актуальных браузерах, но это одна линия обороны, и она отпадёт, если куку когда-нибудь переведут на `SameSite=none`.

- [ ] **Step 1: Написать падающий тест**

```python
def test_post_без_csrf_токена_отклоняется(su_client):
    resp = su_client.post("/admin/settings/vacuum", follow_redirects=False)
    assert resp.status_code == 403


def test_post_с_валидным_токеном_проходит(su_client):
    page = su_client.get("/admin/settings")
    import re
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    resp = su_client.post(
        "/admin/settings/vacuum",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 303
```

- [ ] **Step 2: Запустить, убедиться что первый падает**

Run: `uv run pytest tests/test_web_csrf.py -v`
Expected: FAIL — POST без токена проходит.

- [ ] **Step 3: Добавить выдачу и проверку токена**

Токен привязывается к сессии тем же HMAC, что и сессионная кука, — отдельного хранилища не нужно.

```python
def _csrf_token(auth: AuthUser) -> str:
    """Токен, привязанный к пользователю. Отдельное хранилище не нужно —
    подпись тем же секретом, что и сессия."""
    return _sign(f"csrf:{auth.username}")


async def require_csrf(request: Request, auth: AuthUser = Depends(require_auth)) -> AuthUser:
    form = await request.form()
    supplied = form.get("csrf_token", "")
    if not hmac.compare_digest(supplied, _csrf_token(auth)):
        raise HTTPException(status_code=403, detail="CSRF token missing or invalid")
    return auth
```

Токен кладётся в контекст всех шаблонов через общий хелпер контекста, а зависимость `require_csrf` заменяет `require_auth` / `require_admin` / `require_su` в POST-роутах (проверка роли при этом сохраняется — `require_csrf` вызывает `require_auth`, роль проверяется отдельной строкой в теле роута либо вторым `Depends`).

- [ ] **Step 4: Добавить скрытое поле во все формы**

```html
<input type="hidden" name="csrf_token" value="{{ csrf_token }}">
```

- [ ] **Step 5: Запустить тесты**

Run: `uv run pytest -v`
Expected: все зелёные. Каждая форма, забытая на шаге 4, проявится как падающий тест — по одному на роут.

- [ ] **Step 6: Коммит**

```bash
git add web_app.py templates/ tests/test_web_csrf.py
git commit -m "feat: CSRF-токены на всех изменяющих запросах веб-панели"
```

---

## Phase 3 — Декомпозиция

Начинается только после того, как Phase 0–2 зелёные в CI. Правило: файл трогается — под него сначала пишется тест на текущее поведение, потом он режется.

### Task 10: Разделить `create_app`

`web_app.py:588` — одна функция на 4070 строк, все роуты в замыкании на `bot`.

Роуты выносятся в `routers/` по предметным областям (`auth`, `dashboard`, `users`, `chats`, `presets`, `keywords`, `bans`, `settings`), `bot` передаётся через `app.state.bot` вместо замыкания. `create_app` остаётся сборщиком на ~80 строк. Каждый вынесенный роутер получает тест на «страница отвечает 200 и содержит ключевой элемент» до переноса.

### Task 11: Разделить `handle_group_command`

`bot_handlers.py:3791` — 1090 строк. Режется по естественным швам, которые уже видны в коде: резолв цели, проверка прав, применение наказания, отправка отчёта, публичное уведомление. Перед разрезанием пишутся тесты на каждую из шести команд `!ban/!sban/!warn/!swarn/!mute/!smute`.

### Task 12: Alembic вместо ручных миграций

660 строк `PRAGMA` + `ALTER TABLE` в `init_db` заменяются на alembic: текущая схема снимается как baseline-ревизия, `init_db` сводится к `alembic upgrade head`. Тест проверяет апгрейд с реальной прод-БД, снятой бэкапом.

---

## Phase 4 — Roadmap и новые функции

### Task 13: Привести `roadmap.md` в соответствие с кодом

По таблице расхождений выше: v4.8.5 и v4.8.6 отмечаются как релизнутые, пункт об удалении `WordFilter` помечается как отменённый с указанием причины, строка §0 «без `WordFilter`» исправляется, ссылки на удалённые `scripts/test_*.py` убираются либо тесты восстанавливаются, схема деплоя §12 переписывается под git + Docker. Issues #14 и #13 уже закрыты корректно; #7 закрыт при отменённом решении — в нём стоит оставить комментарий с объяснением.

### Task 14: `/mywarns` (Issue #6, roadmap v4.9.0 §2.1)

Дизайн в roadmap готов, но три вопроса помечены как «нужно подтвердить у пользователя»: синтаксис (`/mywarns` или `!mywarns`), формат вывода, поведение в группе. Задача берётся в работу после ответов.

### Task 15: Перенос `_alarm_auto_off_tick` (Issue #15, roadmap v4.9.0 §1.1)

Механический перенос из `bot.py:218` в `bot_handlers.py`. Делать после Task 11, заодно с устранением костыля `sys.modules.setdefault("bot", ...)` — оба про одну и ту же связанность.

### Task 16: Довести `/health` до `/healthz` (roadmap v5.0.0-07)

Текущий `/health` отдаёт четыре поля. По roadmap нужны `memory_mb`, `memory_percent`, `telegram_connected`, `telegram_api_latency_ms`, коды 200/503 и градации `ok/degraded/down`. Половина данных уже собирается в `_bot_info()` — задача сводится к сборке ответа и добавлению замера latency. Роут остаётся публичным и не должен зависеть от БД.

### Task 17: Обновить FastAPI

Обнаружено при выполнении Task 1. На Python 3.14 сборка приложения выдаёт:

```
DeprecationWarning: 'asyncio.iscoroutinefunction' is deprecated and slated for
removal in Python 3.16; use inspect.iscoroutinefunction() instead
```

Источник — `fastapi/routing.py:233` в запиненной версии 0.115.6. Сейчас это
предупреждение, а не ошибка: приложение поднимается со всеми 54 роутами. Но в
Python 3.16 вызов исчезнет, и FastAPI 0.115.6 перестанет работать.

Актуальная версия — 0.141.1, и она тянет Starlette 1.6.0, то есть смену мажорной
версии. Поэтому апгрейд не влезал в Task 1: смысл лока в том, чтобы зафиксировать
ровно текущее поведение, а не менять его заодно с системой сборки. Делать после
Task 2, когда есть чем поймать регрессию: поднять версию, прогнать весь набор
тестов, глазами проверить рендер всех страниц панели.

Срочности нет — 3.16 выйдет не раньше конца 2027 года. Но и тянуть до последнего
не стоит: чем больше разрыв, тем болезненнее переход.

**Порядок Phase 4:** Task 13 идёт первой и не зависит ни от чего — это чистка документа. Task 16 самая дешёвая из кода. Task 14 ждёт ответов. Task 15 ждёт Task 11.

**Не берётся в этот план:** v5.0.0-02..06 и 08 (self-healing, рестарт, логи, авто-webhook). Они требуют доступа к Bothost Agent API, которого нет в репозитории, и self-update из веб-панели опасен до закрытия Phase 2 — панель с правом переписывать собственный код должна сначала перестать быть уязвимой.

---

## Self-Review

**Покрытие спеки.** Все пункты roadmap со статусом «не сделано» разнесены по Task 13–16 либо явно исключены с обоснованием. Все находки аудита кода закрыты в Phase 0–3.

**Плейсхолдеры.** Phase 0–2 содержат исполнимый код на каждый шаг. Phase 3 (Task 10–12) описан на уровне подхода намеренно: точные швы декомпозиции видны только после того, как тесты Phase 0–2 зафиксируют текущее поведение, и писать сейчас конкретные сигнатуры для 4000 строк роутов означало бы выдумывать их. Перед началом Phase 3 на неё пишется отдельный детальный план.

**Согласованность типов.** `tg_call(coro_factory, *, attempts)` и `spawn(coro, name)` используются в Task 4–5 ровно с теми сигнатурами, что объявлены. `_csrf_token(auth)` и `require_csrf` согласованы с существующими `_sign` и `require_auth`.
