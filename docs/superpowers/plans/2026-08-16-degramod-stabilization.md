# Degramod: стабилизация и синхронизация roadmap — план внедрения

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Убрать причины, по которым бот регулярно ломается, и привести `roadmap.md` в соответствие с фактическим состоянием кода — чтобы дальнейшие фичи ставились на проверяемый фундамент.

**Architecture:** Работа идёт снизу вверх. Сначала появляется инструментарий (uv, pytest, ruff, CI) — без него любую правку невозможно проверить. Затем чинятся рантайм-баги, которые роняют бота в проде. Затем закрывается веб-панель. Декомпозиция гигантских функций идёт последней, уже под защитой тестов, и только для тех участков, которые реально трогаем.

**Tech Stack:** Python 3.14, aiogram 3.30, FastAPI, SQLAlchemy 2.x (async) + aiosqlite, Jinja2, uv, pytest + pytest-asyncio, ruff.

**Spec:** `roadmap.md` (в части будущих версий) + аудит расхождений в разделе «Состояние roadmap» ниже.

---

## Статус выполнения (обновлено 17.08.2026)

План писался 16.08 по состоянию `main` на тот момент. Параллельно в `main`
влили v4.8.9–v4.8.10 (декомпозиция, Alembic, CSRF, XFF, `tg_safe_call`), поэтому
часть задач закрылась не этой веткой, а тем релизом. В колонке «кем» это
различие зафиксировано — иначе непонятно, что проверять.

| # | Задача | Статус | Версия | Коммит |
|---|---|---|---|---|
| 1 | Перевод на uv и pyproject | ✅ сделано | — | `27ad8e9` |
| 2 | Тестовый харнесс | ✅ **перевыполнено** | — | `d4185bc` → `919fd26` |
| 3 | CI | ✅ **перевыполнено** | — | `d4185bc` |
| 4 | Обработка flood control | ✅ сделано | v4.8.7 / v4.10.3 | `2d41e1b`, — |
| 5 | Фоновые задачи не теряются | ✅ сделано | v4.8.7 | `2d41e1b` |
| 6 | Убрать блокировку event loop | ✅ сделано | v4.8.7 / v4.10.3 | `2d41e1b`, — |
| 7 | Срок жизни сессии | ✅ сделано | v4.8.7 | `2d41e1b` |
| 8 | Rate-limit логина не обходится заголовком | ✅ сделано | v4.8.8 | `2d41e1b` |
| 9 | CSRF-токены | ✅ сделано | v4.8.8 | `2d41e1b` |
| 10 | Разделить `create_app` | ✅ сделано | v4.10.0 | `535cf74..a7c7e53` |
| 11 | Разделить `handle_group_command` | ✅ сделано | v4.8.9 / v4.8.10 | `b6fe6eb`, `786d9ae` |
| 12 | Alembic вместо ручных миграций | ❌ откачено | v4.8.9 | `b6fe6eb` |
| 13 | Привести `roadmap.md` в соответствие | ✅ сделано | — | `c71997f`, `d729ff6` |
| 14 | `/mywarns` | ✅ сделано + починено | v4.8.10 / v4.8.11 / v4.9.0 | `786d9ae`, `461d9e3`, `7fe046d` |
| 15 | Перенос `_alarm_auto_off_tick` | ✅ сделано | v4.8.9 | `b6fe6eb` |
| 16 | `/health` → `/healthz` | ✅ сделано | v4.10.2 | — |
| 17 | Обновить FastAPI | ✅ сделано | v4.8.12 | `44e8c18` |
| 18 | Сюита: проверки, не роняющие pytest | ✅ сделано | v4.10.0 / v4.10.1 | `3bd1c0d`, `174e88a..bb482be` |

### Как читать колонку «коммит»

Привязка восстановлена по git 19.08.2026. Две особенности истории, без которых
таблица вводит в заблуждение:

**1. До v4.8.10 включительно разработка шла вне git.** Версии заливались
архивами через веб-интерфейс GitHub, поэтому в истории они выглядят как
череда коммитов «Add files via upload» без внятных сообщений. Один такой
коммит = один релиз целиком, а не одна задача. Отсюда `2d41e1b` в шести
строках подряд: v4.8.7 и v4.8.8 приехали **одним** коммитом (`APP_VERSION`
в нём сразу `v4.8.8`, отдельного коммита для v4.8.7 не существует, хотя
в changelog версия описана отдельно).

Сопоставление upload-коммитов с версиями сделано по `APP_VERSION` в
`web_app.py` каждого коммита:

| Коммит | Дата | `APP_VERSION` | Что принёс |
|---|---|---|---|
| `2180fce` | 15.08 | v4.8.6 | чистка Settings UI |
| `2d41e1b` | 16.08 | v4.8.8 | Task 4–9: `tg_safe_call`, TTL, XFF, CSRF (+428 строк в `web_app.py`) |
| `b6fe6eb` | 16.08 | v4.8.9 | Task 10–12, 15: `app_state.py`, `mod_commands.py`, `web/`, Alembic |
| `786d9ae` | 16.08 | v4.8.10 | Task 11, 14: завершение декомпозиции, `!mywarns`, `roadmaps/` |

**2. Task 1 раньше ссылался на `fc81aa3` — этого коммита нет в `dev`.**
В репозитории лежат три копии одного и того же коммита («переход на uv»):
`fc81aa3`, `9ab0e45` и `27ad8e9`. Из ветки `dev` достижим только последний,
остальные — висячие, от заливок через веб-интерфейс. То же касается
`105528c`/`6bf9f97`/`2b34b3c` (аудит roadmap) и `f484c8f`/`7d01459`/`9691286`
(переписанный Task 8). В таблице оставлены версии, достижимые из `dev`.

### Где перевыполнили

**Task 2 (тестовый харнесс).** План просил `tests/conftest.py` и
`tests/test_smoke.py` — то есть фикстуры и один дымовой тест с нуля.
Фактически: сюита из **64 файлов и 1329 тестов**, восстановленная из истории
(`351b6d8^`) и приведённая в рабочий вид. Она была прибита к путям чужой
песочницы (`/home/z/my-project/`, 39 файлов из 65) и не запускалась нигде;
пути вычисляются от `__file__`, добавлен раннер `tools/run_tests.py`
(файл = процесс, иначе конфликтует модульное состояние) и baseline
`known_failing.txt`, который сейчас **пуст** — все 64 файла зелёные.

Попутно из истории восстановлена утилита `tools/cleanup_test_data.py`: при
заливке сюиты в `main` приложили только тест к ней, а сам скрипт нет.

**Task 3 (CI).** План просил workflow с ruff. Фактически ещё и прогон всей
сюиты, и `docker build` — то есть сборка прод-образа проверяется на каждый
push. Это же закрыло вопрос «не сломает ли uv деплой» фактом, а не спором.

### Что осталось и почему

**Task 4 — частично.** `tg_safe_call` существует, но покрывает 22 call site
при сотнях вызовов Telegram API. Правило «оборачивай новые критичные вызовы»
записано в `CLAUDE.md`; сплошное покрытие — отдельная задача.

**Task 6 — частично.** `sqlite3`, `VACUUM` и `shutil.copy2` вынесены в
`asyncio.to_thread`, но блокирующий `open()` в `web_app.py:738, 3341` остался
(ruff `ASYNC230`, вынесен в `per-file-ignores` с пометкой).

**Task 12 — откачено.** Alembic добавлен, но дважды уронил прод на auto-stamp
существующей БД; работает через `DB_USE_LEGACY_MIGRATIONS=1`, то есть старый
`init_db()`. `migrations/` и `alembic.ini` лежат мёртвым грузом. Включать
только после проверки на staging.

**Task 16 — сделано в v4.10.2.** Добавлен `/healthz` со всеми полями из
роадмапа и градацией `ok/degraded/down` (коды 200/503). Старый `/health`
оставлен без изменений — его может опрашивать мониторинг Bothost.

Два решения сверх буквы роадмапа. Во-первых, `telegram_connected` и
`telegram_api_latency_ms` собирает фоновый пробник (`health_probe.py`,
`getMe` раз в минуту), а роут читает готовый снимок: роадмап требовал, чтобы
эндпоинт «не требовал TG», и одновременно просил эти поля — активный вызов
на каждый запрос упёрся бы в rate limit при мониторинге раз в полминуты.
Во-вторых, `memory_percent` отдаётся `null`, если лимит контейнера не найден
в cgroup: считать процент от памяти хоста бессмысленно — 300 МБ от 32 ГБ
дают 1%, и порог не сработал бы никогда.

### Сверх плана

- `/mywarns` (v4.8.10) починен: приватность при таймауте, утечка памяти в
  реестре таймаутов, отсутствие `tg_safe_call`, английский месяц в дате.
- Разбан из веб-панели требует привязанного Telegram (был `tg_user_id or -1`,
  заводивший фантомного модератора `-1`).
- `CLAUDE.md` приведён в соответствие с кодом: пять ключевых утверждений
  устарели после v4.8.9 и вводили в заблуждение.
- Добавлены `.dockerignore`, `.gitignore` под проект, `.env.example`.

---


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

> **Таблица — снимок на 16 августа, а не текущий статус.** Три пункта v4.9.0 закрылись уже после сверки, в v4.8.9–v4.8.11, и отмечены ниже как «сделано после сверки». Актуальный статус задач плана — в таблице «Статус выполнения» выше; актуальный статус версий — в `roadmap.md` §13.

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
| v4.9.0 §1.1 перенос `_alarm_auto_off_tick` | ⏳ | ✅ **сделано после сверки** (v4.8.9) | переехал в `chat_modes.py`, а не в `bot_handlers.py`: режимы чата — его домен. Issue #15 можно закрывать |
| v4.9.0 §1.2 behavioral-тесты | ⏳ | ✅ **сделано после сверки** | сюита 64 файла / 1329 тестов, покрыты `/admin/bans`, `/admin/keywords`, `/api/unban`, toggle-эндпоинты. Issue #16 можно закрывать |
| v4.9.0 §2.1 `/mywarns` | ⏳ | ✅ **сделано после сверки** (v4.8.10) | вышло как `!mywarns`, не `/mywarns`; четыре дефекта починены в v4.8.11, `tests/test_v4811_mywarns.py`. Issue #6 можно закрывать |
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

**Статус:** закрыт 16 августа 2026, коммит `27ad8e9` (в `dev`; `fc81aa3` и `9ab0e45` — его висячие копии от заливок через веб-интерфейс, см. «Как читать колонку „коммит“» выше). Все семь шагов пройдены, включая сборку и запуск образа. Три вещи всплыли по ходу и внесены в текст ниже: `[tool.uv] package = false`, настройка ruff под кириллицу и `--no-sync` в `CMD`.

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

Сделано коммитом `27ad8e9`.

### Task 2: Тестовый харнесс ✅ ПЕРЕВЫПОЛНЕНО (`d4185bc` → `919fd26`)

> Вместо conftest + одного smoke-теста — сюита из 64 файлов (1329 тестов),
> восстановленная из истории и починенная, плюс раннер `tools/run_tests.py`.
> Шаги ниже описывают исходный, более скромный замысел.

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

### Task 3: CI ✅ ПЕРЕВЫПОЛНЕНО (`d4185bc`)

> Помимо ruff workflow гоняет всю сюиту и `docker build`.

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

### Task 4: Обработка flood control Telegram ✅ ВЫПОЛНЕНО (v4.8.7 `2d41e1b` / v4.10.3)

> `tg_safe_call` есть; в v4.10.3 закрыты все критичные call site и добавлен
> тест-сторож.

**Закрыто в v4.10.3.** Пересчёт показал, что «сотни непокрытых вызовов» —
неверная оценка: почти все наказания были обёрнуты ещё в v4.8.7, а из
оставшихся двухсот `await` подавляющее большинство это `reply`/`send_message`
(ответы модератору) и вызовы SQLAlchemy `execute`/`commit`, к Bot API
отношения не имеющие. Голых критичных вызовов оказалось три:

- `chat_modes.py` — `set_chat_permissions` в `_apply_chat_permissions`:
  ядро режимов чата. Потерянный при 429 вызов оставлял права предыдущего
  режима, и следующий тик снимал snapshot с испорченного состояния;
- `mod_commands.py` — `delete_message` эфемерного уведомления о варнах:
  таска одноразовая, повторить некому, сообщение висело бы вечно;
- `web/admin_chats.py` — `leave_chat` при удалении чата из панели: бот
  оставался в чате, которого в панели уже нет.

Ответы намеренно не оборачивались: их потеря видна сразу и лечится повтором
команды, а дифф на двести мест без разбора смысла никто не вычитает. Решение
соответствует формулировке в `CLAUDE.md`: «Новый критичный вызов оборачивай».

Главный результат — не три правки, а `tests/test_v4103_critical_calls_wrapped.py`:
сторож разбирает исходники через AST, отсекает упоминания методов в
докстроках и падает на любом новом критичном вызове без обёртки. Плюс
поведенческая проверка, что `_apply_chat_permissions` переживает 429 и
сохраняет `use_independent_chat_permissions` при ретрае.

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

### Task 5: Фоновые задачи не теряются ✅ ВЫПОЛНЕНО (v4.8.7, `2d41e1b`)

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

### Task 6: Убрать блокировку event loop в веб-панели ✅ ВЫПОЛНЕНО (v4.8.7 `2d41e1b` / v4.10.3)

> sqlite3/VACUUM/copy2 вынесены в `to_thread` в v4.8.7; последний блокирующий
> `open()` закрыт в v4.10.3, игноры ASYNC230 сняты.

**Закрыто в v4.10.3.** К этому моменту оставался один блокирующий вызов —
запись файла аватарки в `_fetch_and_save_avatar`. Второй, чтение
`/proc/self/status`, перестал быть нарушением сам: в v4.10.2 он переехал в
`health_probe.memory_rss_bytes()`, синхронную функцию, где правило ASYNC230
неприменимо.

Запись вынесена в `asyncio.to_thread`. Оба `per-file-ignores` с ASYNC230
удалены — для `web_app.py` и осиротевший для `web/admin_settings.py`.
Это важнее самой правки: пока игнор висел, ruff молчал и о будущих
нарушениях в тех же файлах. Проверено подсадкой: блокирующий `open()` в
async-роуте теперь роняет линтер.

Тест проверяет поведение, а не текст: во время записи двух мегабайт
параллельная корутина продолжает тикать. Вернуть запись в основной поток —
и счётчик тиков упадёт.

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

### Task 7: Срок жизни сессии ✅ ВЫПОЛНЕНО (v4.8.7, `2d41e1b`)

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

### Task 8: Rate-limit логина не обходится заголовком ✅ ВЫПОЛНЕНО (v4.8.8, `2d41e1b`)

> Реализовано через `TRUSTED_PROXIES`, а не через разбор в uvicorn.

**Files:**
- Modify: `web_app.py:120-141` (`_check_login_rate_limit`, `_client_ip`), `web_app.py:685-707` (роут `/login`), `bot.py:1584` (`uvicorn.run`)
- Test: `tests/test_web_auth.py`

**Interfaces:**
- Produces: `_check_login_rate_limit(ip: str, username: str) -> bool` — сигнатура меняется, добавляется второй аргумент.

#### Постановка

`_client_ip` безусловно доверяет `X-Forwarded-For`, поэтому лимит в 5 попыток снимается подстановкой произвольного значения. Плюс пароль SU сравнивается оператором `!=`.

Первая версия этой задачи предлагала завести свой список `TRUSTED_PROXIES` и разбирать заголовок вручную. От неё отказались по двум причинам, вскрывшимся при разборе.

**Первая: разбор брал не ту сторону заголовка.** Каждый прокси дописывает `X-Forwarded-For` справа, поэтому правая часть заголовка написана нашим прокси и клиентом не подделывается, а левую клиент контролирует целиком. `xff.split(",")[0]` берёт именно левый элемент — то есть проверка доверенного прокси добавлялась бы, а данные всё равно брались бы у атакующего.

**Вторая: это уже реализовано в uvicorn, и реализовано правильно.** `ProxyHeadersMiddleware` включён по умолчанию (`proxy_headers=True`) и подменяет `scope["client"]`, если peer входит в `forwarded_allow_ips`. Его алгоритм (`uvicorn/middleware/proxy_headers.py:125`) идёт по заголовку в обратном порядке и возвращает первый недоверенный адрес:

```python
# Note: each proxy appends to the header list so check it in reverse order
for host in reversed(x_forwarded_for_hosts):
    if host not in self:
        return host
```

Поэтому правильное решение — не дублировать middleware, а **убрать разбор из `web_app.py` и настроить uvicorn**.

#### Риск, из-за которого задача переделана

Если приложение стоит за обратным прокси (у Bothost это Traefik — упоминается в `bot.py:7`), а `forwarded_allow_ips` настроен неверно, то `request.client.host` будет одинаковым для всех. При ключе лимита по одному только IP пяти неудачных попыток случайного человека хватит, чтобы **заблокировать вход всем администраторам на пять минут**. Сейчас такого нет: лимит просто не работает. То есть наивная починка меняет дыру на отказ в обслуживании.

Поэтому лимит переводится на ключ `(ip, username)` — тогда неверная настройка прокси деградирует до «можно заблокировать один атакуемый аккаунт» вместо «можно заблокировать всех». Топология перестаёт быть вопросом жизни и смерти.

- [ ] **Step 1: Выяснить реальный peer-адрес**

Значение `forwarded_allow_ips` не угадывается и не запрашивается у поддержки Bothost: в `X-Forwarded-For` лежат адреса клиентов, а нужен адрес того, кто устанавливает TCP-соединение с uvicorn. Он виден в логах.

Временно добавить первой строкой в обработчик `/login`:

```python
_req_logger.info(
    "DIAG peer=%s xff=%r",
    request.client.host if request.client else None,
    request.headers.get("X-Forwarded-For"),
)
```

Задеплоить, открыть `/login`, прочитать логи контейнера. Ожидаемо `peer` окажется `127.0.0.1` (прокси в том же контейнере) либо адресом контейнерной сети вида `172.x.x.x`. Записать значение, строку убрать.

- [ ] **Step 2: Написать падающие тесты**

Три сценария: заголовок не влияет на лимит, перебор одного логина не блокирует остальных, перебор логинов с одного адреса упирается в общий потолок.

```python
def test_подделка_xff_не_обходит_лимит(client):
    """_client_ip не смотрит в заголовок, поэтому разные значения
    X-Forwarded-For не дают атакующему лишних попыток."""
    for i in range(5):
        client.post("/login",
                    data={"username": "su", "password": "неверный"},
                    headers={"X-Forwarded-For": f"10.0.0.{i}"})
    resp = client.post("/login",
                       data={"username": "su", "password": "неверный"},
                       headers={"X-Forwarded-For": "10.0.0.99"})
    assert resp.status_code == 429, "лимит обошли подстановкой X-Forwarded-For"


def test_лимит_не_блокирует_другие_аккаунты(client):
    """Ключ — пара (ip, username). Даже когда адрес у всех общий (панель
    за прокси), перебор одного логина не должен закрывать вход остальным."""
    for _ in range(6):
        client.post("/login", data={"username": "su", "password": "неверный"})

    resp = client.post("/login",
                       data={"username": "moderator1", "password": "неверный"})
    assert resp.status_code != 429, "перебор одного аккаунта заблокировал другой"


def test_перебор_логинов_с_одного_адреса_упирается_в_потолок(client):
    """Общий счётчик по адресу закрывает перебор имён пользователей."""
    for i in range(20):
        client.post("/login", data={"username": f"user{i}", "password": "неверный"})
    resp = client.post("/login", data={"username": "ещё_один", "password": "неверный"})
    assert resp.status_code == 429
```

- [ ] **Step 3: Запустить, убедиться что падают**

Run: `uv run pytest tests/test_web_auth.py -v -k "лимит or xff"`
Expected: FAIL. Первый — потому что заголовок сейчас читается; второй — потому что ключ сейчас только по IP; третий — потому что общего потолка нет.

- [ ] **Step 4: Убрать разбор заголовка из `web_app.py`**

```python
def _client_ip(request: Request) -> str:
    """Адрес клиента.

    X-Forwarded-For здесь намеренно НЕ разбирается. Этим занимается
    ProxyHeadersMiddleware в uvicorn: он идёт по заголовку справа налево
    и берёт первый недоверенный адрес. Правую часть дописывает наш прокси,
    и подделать её клиент не может — в отличие от левой, которую он
    контролирует полностью. Разбор здесь дублировал бы middleware, а взяв
    левый элемент (как было до v4.9), сводил бы защиту на нет.

    Кого считать прокси — задаётся через forwarded_allow_ips в bot.py.
    """
    return request.client.host if request.client else "unknown"
```

- [ ] **Step 5: Перевести лимит на ключ `(ip, username)`**

```python
_LOGIN_RATELIMIT_MAX = 5          # попыток на пару (ip, username)
_LOGIN_RATELIMIT_IP_MAX = 20      # попыток с одного адреса по всем логинам
_LOGIN_RATELIMIT_WINDOW = 300     # 5 минут

_login_attempts: dict[tuple[str, str], list[float]] = {}
_login_attempts_by_ip: dict[str, list[float]] = {}


def _check_login_rate_limit(ip: str, username: str) -> bool:
    """True — попытка разрешена, False — лимит исчерпан.

    Ключ — пара (ip, username), а не один ip. Если панель окажется за
    прокси с неверно настроенным forwarded_allow_ips, все запросы придут
    с одного адреса; при ключе только по ip пяти неудачных попыток хватило
    бы, чтобы заблокировать вход всем администраторам сразу. С парой
    блокируется лишь атакуемый аккаунт.

    Второй счётчик, по одному ip на все логины, закрывает перебор имён.
    Его срабатывание логируется: если он выбивается при нормальной
    нагрузке — это признак того, что адреса схлопнулись в прокси.
    """
    now = time.time()

    def _fresh(bucket: list[float]) -> list[float]:
        return [t for t in bucket if now - t < _LOGIN_RATELIMIT_WINDOW]

    key = (ip, username)
    per_user = _fresh(_login_attempts.get(key, []))
    per_ip = _fresh(_login_attempts_by_ip.get(ip, []))
    _login_attempts[key] = per_user
    _login_attempts_by_ip[ip] = per_ip

    if len(per_user) >= _LOGIN_RATELIMIT_MAX:
        return False

    if len(per_ip) >= _LOGIN_RATELIMIT_IP_MAX:
        _req_logger.warning(
            "login: исчерпан общий лимит для ip=%s (%d попыток за %ds). "
            "Если это один и тот же адрес для всех пользователей — проверьте "
            "forwarded_allow_ips в bot.py",
            ip, len(per_ip), _LOGIN_RATELIMIT_WINDOW,
        )
        return False

    per_user.append(now)
    per_ip.append(now)
    return True
```

- [ ] **Step 6: Подвинуть проверку лимита после разбора формы**

Сейчас `_check_login_rate_limit(ip)` вызывается до `await request.form()`. Новой сигнатуре нужен `username`, поэтому порядок в роуте `/login` меняется: сначала форма, затем проверка лимита.

```python
form = await request.form()
username = (form.get("username") or "").strip().lower()
password = form.get("password", "")

ip = _client_ip(request)
if not _check_login_rate_limit(ip, username):
    _req_logger.warning("login rate-limited for ip=%s username=%s", ip, username)
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "error": True,
            "error_msg": "Too many login attempts. Try again in 5 minutes.",
        },
        status_code=429,
    )
```

- [ ] **Step 7: Настроить uvicorn**

В `bot.py`, в вызове `uvicorn.run`:

```python
uvicorn.run(
    app,
    host="0.0.0.0",
    port=PORT,
    log_level="info",
    timeout_keep_alive=30,
    # Кому доверяем X-Forwarded-For. Дефолт uvicorn — "127.0.0.1"; если
    # Traefik у Bothost стоит отдельным контейнером, сюда нужен адрес его
    # подсети. Реальное значение получено в Step 1 из логов.
    forwarded_allow_ips=os.getenv("FORWARDED_ALLOW_IPS", "127.0.0.1"),
)
```

Значение `"*"` не использовать: оно велит доверять заголовку от кого угодно, и дыра возвращается ровно в исходном виде.

- [ ] **Step 8: Сделать сравнение пароля SU постоянного времени**

```python
if not WEB_PASSWORD or not hmac.compare_digest(password, WEB_PASSWORD):
```

- [ ] **Step 9: Запустить тесты**

Run: `uv run pytest tests/test_web_auth.py -v`
Expected: все зелёные, включая тесты срока жизни сессии из Task 7.

- [ ] **Step 10: Отметить новую переменную в CLAUDE.md**

В таблицу переменных окружения добавляется `FORWARDED_ALLOW_IPS` — кому доверять `X-Forwarded-For`, по умолчанию `127.0.0.1`, значение выясняется по логам (Step 1).

- [ ] **Step 11: Коммит**

```bash
git add web_app.py bot.py tests/test_web_auth.py CLAUDE.md
git commit -m "fix: разбор X-Forwarded-For отдан uvicorn, лимит логина по паре (ip, username)"
```

### Task 9: CSRF-токены на изменяющих запросах ✅ ВЫПОЛНЕНО (v4.8.8, `2d41e1b`)

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

### Task 10: Разделить `create_app` ✅ ВЫПОЛНЕНО (v4.10.0, `535cf74..a7c7e53`)

> Все 47 роутов вынесены из `create_app()` в 11 модулей `web/`. Внутри
> `create_app()` не осталось ни одного роута — только сборка приложения.

`web_app.py:772` (на момент замера v4.9.0, `4e3d5b2`) — одна функция на 4135
строк, все роуты в замыкании на `bot` и `templates`. После декомпозиции
`create_app()` — 243 строки, 0 роутов; `web_app.py` целиком — 999 строк
(было 4906).

Роуты вынесены в `web/` по предметным областям, `bot` и `templates`
передаются через `app.state` + `Depends` вместо замыкания. Каждый вынесенный
роутер получил тест на «страница отвечает 200 и содержит ключевой элемент»
до переноса; вся сюита (67 файлов) прошла без правок в поведении.

Спека (`docs/superpowers/specs/2026-08-19-web-app-decomposition-design.md`)
уточнила план в трёх местах, где он был написан «на уровне подхода», и все
три уточнения подтвердились реализацией:

- каталог — `web/`, а не `routers/`: пакет уже существовал с v4.8.9, и в нём
  лежали 7 вынесенных на тот момент роутов;
- вместо восьми доменов из плана — одиннадцать модулей: `admin/settings` и
  `admin/cleanup` разведены (настройки тянут GitHub-интеграцию и `_bot_info`),
  а `dashboard` не выделен в отдельный модуль, а входит в `me.py`;
- применено правило позднего связывания хелперов (`import web_app` вместо
  `from web_app import ...`) — иначе ломаются тесты, патчащие атрибуты
  модуля `web_app` (например, `web_app._fetch_and_save_avatar` в
  `test_v45_dashboard.py`).

Попутно вскрылись и починены две дыры в самой сюите: `test_06` в
`test_v480_web_ui_modchat_keywords.py` проходил вхолостую (не находил
функций и не выполнял assert), а `t01` в `test_v4810_web_behavioral.py`
считал роуты наивным обходом `app.routes` и не видел роуты, вынесенные в
подключённые роутеры.

### Task 11: Разделить `handle_group_command` ✅ ВЫПОЛНЕНО (v4.8.9 `b6fe6eb` / v4.8.10 `786d9ae`)

> Команды живут в `mod_commands.py`. Важно для тестов: модуль импортирует
> хелперы по именам, поэтому патчить надо `mod_commands.X`.

`bot_handlers.py:3791` — 1090 строк. Режется по естественным швам, которые уже видны в коде: резолв цели, проверка прав, применение наказания, отправка отчёта, публичное уведомление. Перед разрезанием пишутся тесты на каждую из шести команд `!ban/!sban/!warn/!swarn/!mute/!smute`.

### Task 12: Alembic вместо ручных миграций ❌ ОТКАЧЕНО (v4.8.9, `b6fe6eb`)

> Дважды уронил прод на auto-stamp. Работает `DB_USE_LEGACY_MIGRATIONS=1`.

660 строк `PRAGMA` + `ALTER TABLE` в `init_db` заменяются на alembic: текущая схема снимается как baseline-ревизия, `init_db` сводится к `alembic upgrade head`. Тест проверяет апгрейд с реальной прод-БД, снятой бэкапом.

---

## Phase 4 — Roadmap и новые функции

### Task 13: Привести `roadmap.md` в соответствие с кодом ✅ ВЫПОЛНЕНО (`c71997f`, `d729ff6`)

По таблице расхождений выше: v4.8.5 и v4.8.6 отмечаются как релизнутые, пункт об удалении `WordFilter` помечается как отменённый с указанием причины, строка §0 «без `WordFilter`» исправляется, ссылки на удалённые `scripts/test_*.py` убираются либо тесты восстанавливаются, схема деплоя §12 переписывается под git + Docker. Issues #14 и #13 уже закрыты корректно; #7 закрыт при отменённом решении — в нём стоит оставить комментарий с объяснением.

### Task 14: `/mywarns` (Issue #6, roadmap v4.9.0 §2.1) ✅ ВЫПОЛНЕНО (v4.8.10 `786d9ae`) + ПОЧИНЕНО (v4.8.11 `461d9e3`, v4.9.0 `7fe046d`)

> Реализована как `!mywarns`. В v4.8.11 исправлены четыре дефекта:
> приватность при таймауте, утечка памяти, отсутствие `tg_safe_call`,
> английский месяц в дате. Покрыта 13 тестами.

Дизайн в roadmap готов, но три вопроса помечены как «нужно подтвердить у пользователя»: синтаксис (`/mywarns` или `!mywarns`), формат вывода, поведение в группе. Задача берётся в работу после ответов.

### Task 15: Перенос `_alarm_auto_off_tick` (Issue #15, roadmap v4.9.0 §1.1) ✅ ВЫПОЛНЕНО (v4.8.9, `b6fe6eb`)

> Переехала в `chat_modes.py`, а не в `bot_handlers.py` — это её домен.
> Хак `sys.modules.setdefault` снят там же, заменён на `app_state.py`.

Механический перенос из `bot.py:218` в `bot_handlers.py`. Делать после Task 11, заодно с устранением костыля `sys.modules.setdefault("bot", ...)` — оба про одну и ту же связанность.

### Task 16: Довести `/health` до `/healthz` (roadmap v5.0.0-07) ✅ ВЫПОЛНЕНО (v4.10.2)

Текущий `/health` отдаёт четыре поля. По roadmap нужны `memory_mb`, `memory_percent`, `telegram_connected`, `telegram_api_latency_ms`, коды 200/503 и градации `ok/degraded/down`. Половина данных уже собирается в `_bot_info()` — задача сводится к сборке ответа и добавлению замера latency. Роут остаётся публичным и не должен зависеть от БД.

### Task 17: Обновить FastAPI ✅ ВЫПОЛНЕНО (v4.8.12, `44e8c18`)

> 0.115.6 → 0.141.1, Starlette 0.41.3 → 1.6.0. Сюита поймала два ломающих
> изменения: сигнатуру `TemplateResponse` (15 файлов, вся веб-панель) и
> пустые значения `Form(...)`, ставшие «отсутствующими» (сырой 422 вместо
> понятной ошибки на семи роутах).

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

### Task 18: Сюита — проверки, не роняющие pytest ✅ ВЫПОЛНЕНО (v4.10.0 / v4.10.1)

Часть файлов сюиты проходит вхолостую: `uv run python tools/run_tests.py`
печатает PASS, хотя внутри файла реальная проверка не выполняется или
падает молча. Два независимых механизма:

**(а) Проверки лежат в функциях, которые pytest не собирает.** Имена
функций не начинаются с `test_` (`t01_...`, `check(...)` вместо `assert`),
сами вызовы спрятаны под `if __name__ == "__main__": sys.exit(main())`. Под
`pytest tests/test_x.py` файл импортируется, ни одна функция не выполняется,
pytest не находит ни одного теста и возвращает `rc=5`. Раннер (`tools/run_tests.py`)
трактует `rc=5` как успех скриптового файла — по его собственному
обоснованию: «модуль импортировался и ни один assert не сработал». Для
файлов этой категории это неверно: assert'ы не сработали не потому что
прошли, а потому что не запускались вовсе.

Затронуты: `test_v4831_regex_check`, `test_v4851_project_node_id`,
`test_v4852_command_prefix`, `test_v4853_status_field`, `test_v488_smoke`.

`test_v4853_status_field` попал сюда не сразу: в v4.10.0 ему переадресовали
греп T13 на `web/admin_settings.py`, и в первой редакции этого раздела файл
числился вылеченным. Проверка показала обратное — сам греп теперь верный, но
исполняется только при прямом запуске скрипта: все его функции `t1_..t20_`
по-прежнему без префикса `test_`, и pytest собирает из файла ноль тестов.

**(б) Проверки обёрнуты в try/except с самодельным репортером.** Функции
собираются pytest'ом (или исполняются на уровне модуля), но `check(...)`/
`_fail(...)` внутри только печатает ✗ и увеличивает счётчик — не бросает.
Настоящий `sys.exit(1)` по накопленному счётчику стоит в `main()`, а `main()`
опять же под `if __name__`. Под pytest файл честно импортируется и
выполняется, но провал проверки никак не всплывает наружу — тест зелёный
независимо от результата.

Затронуты: `test_v484_progressive_automutes`, `test_v4854_migration_order`,
`test_v485_idea`, `test_v487_async_routes`.

**Закрыто в v4.10.1.** Оживлено 63 проверки в четырёх файлах категории (а):
`test_v4851_project_node_id` (13), `test_v4852_command_prefix` (10),
`test_v4853_status_field` (20), плюс `test_v4831_regex_check` (11 тестов,
12 subtest'ов) — он был не тестом, а диагностическим скриптом без единой
проверки, причём regex в нём лежали копиями из v4.8.3 и уже разошлись с
рабочими; теперь импортирует их из `bot_handlers`. `test_v488_smoke`
переписан с print'ов на assert'ы (шаги 4-7 после декомпозиции считали роуты
наивным обходом `app.routes` и грепали CSRF в `web_app.py`, откуда роуты
уехали).

В категории (б) репортер сделан бросающим у `test_v484_progressive_automutes`
и `test_v485_idea`. Оживление вскрыло четыре устаревшие проверки, все
починены по смыслу: прибитая `APP_VERSION == "v4.8.4"` → сравнение через
`tests/_version.ver()`; грep секции `# ── !mute` в `bot_handlers.py` →
`# ── cmd_mute` в `mod_commands.py` (ветки мод-команд уехали туда в
v4.8.9/v4.8.10); требование импорта ORM-модели `IdeaLog` в
`web/admin_settings.py`, который читает `idea_log` сырым SQL → проверка
перенесена в `bot_handlers.py`, где модель и используется; чтение
`requirements.txt`, удалённого в v4.8.9 при переходе на uv → `pyproject.toml`.

Два файла, числившиеся в категории (б), оказались исправны: у
`test_v4854_migration_order` и `test_v487_async_routes` есть итоговый
`assert not failures` на уровне модуля, их провалы всплывают. У второго
задокументирована ловушка: прямой запуск без `conftest.py` даёт три ложных
провала на POST-роутах, потому что CSRF-шим ставится только под pytest.

Каждая починка проверена инъекцией регресса: возврат старой формы
`_CMD_SWARN` роняет тест regex, отключение трёх `include_router` роняет
smoke, снятие `require_csrf_su` роняет сторож CSRF.

Это долг проекта, возникший до и независимо от декомпозиции веб-панели —
декомпозиция его не создала, только сделала заметнее (несколько грепов внутри
этих файлов остались указывать на `web_app.py` после переезда роутов в
`web/`, что и вскрыло проблему при финальном ревью). Два самых ценных файла
почищены здесь же, в v4.10.0: `test_v4810_web_behavioral.py` (14
поведенческих проверок перенесённых роутов — переведён на `test_0N_...` +
настоящий `assert`) и `test_v488_verify_csrf.py` (единственный сторож
CSRF-инварианта — источник сканирования перенесён на `web/*.py`, добавлен
`assert`). Оставшиеся файлы закрыты в v4.10.1 (см. выше). Полный переход сюиты на
общие pytest-фикстуры по-прежнему за рамками: файлы остаются
самостоятельными скриптами, каждый со своим окружением, и раннер по-прежнему
даёт каждому свой процесс.

**Порядок Phase 4:** Task 13 идёт первой и не зависит ни от чего — это чистка документа. Task 16 самая дешёвая из кода. Task 14 ждёт ответов. Task 15 ждёт Task 11.

**Не берётся в этот план:** v5.0.0-02..06 и 08 (self-healing, рестарт, логи, авто-webhook). Они требуют доступа к Bothost Agent API, которого нет в репозитории, и self-update из веб-панели опасен до закрытия Phase 2 — панель с правом переписывать собственный код должна сначала перестать быть уязвимой.

---

## Self-Review

**Покрытие спеки.** Все пункты roadmap со статусом «не сделано» разнесены по Task 13–16 либо явно исключены с обоснованием. Все находки аудита кода закрыты в Phase 0–3.

**Плейсхолдеры.** Phase 0–2 содержат исполнимый код на каждый шаг. Phase 3 (Task 10–12) описан на уровне подхода намеренно: точные швы декомпозиции видны только после того, как тесты Phase 0–2 зафиксируют текущее поведение, и писать сейчас конкретные сигнатуры для 4000 строк роутов означало бы выдумывать их. Перед началом Phase 3 на неё пишется отдельный детальный план.

**Согласованность типов.** `tg_call(coro_factory, *, attempts)` и `spawn(coro, name)` используются в Task 4–5 ровно с теми сигнатурами, что объявлены. `_csrf_token(auth)` и `require_csrf` согласованы с существующими `_sign` и `require_auth`.
