# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Что это

Telegram-бот-модератор («Дедушка Вобжак») + веб-панель администрирования в одном
процессе. Селфхост, обслуживает несколько чатов. Один контейнер: FastAPI (веб-панель
+ webhook-эндпоинт) и Aiogram (бот) делят общий event loop и одну SQLite-базу.

Язык кода и комментариев — русский. Соблюдай это в новом коде.

## Команды

Зависимостями управляет **uv**. Python пришпилен к 3.14.7 в `.python-version` и в
`Dockerfile` — локально, в CI и в проде один и тот же интерпретатор.

```bash
uv sync                  # поднять окружение из uv.lock
uv run python bot.py     # запуск: единственная точка входа, поднимает и бота, и веб-панель
uv run ruff check .      # линт
uv run ruff check --fix . # починить автоисправимое

# Docker (так деплоится в прод)
docker build -t degramod .
docker run --env-file .env -p 3000:3000 -v ./data:/app/data degramod
```

`pyproject.toml` содержит `[tool.uv] package = false` — модули лежат в корне, а не в
пакете, и без этого `uv sync` пытается собрать проект как библиотеку.

В конфиге ruff отключены `RUF001–003` (ругаются на кириллицу, похожую на латиницу —
в русскоязычном коде это 1296 срабатываний и ноль сигнала) и `B008` (запрещает вызов
в аргументе по умолчанию, то есть `Depends(...)` — основную идиому FastAPI).
Базовый уровень на момент перехода — **78 замечаний** по легаси, из них 47
автоисправимы.

### Тесты

```bash
uv run python tools/run_tests.py          # вся сюита, 67 файлов
uv run python tools/run_tests.py -k alarm # подмножество по имени файла
uv run pytest tests/test_v487_sanity.py -q  # один файл
```

**Запускать только через раннер, не `pytest tests` напрямую.** Сюита писалась
как набор самостоятельных скриптов: каждый файл поднимает своё окружение при
импорте, и в одном процессе они конфликтуют — повторный `dp.include_router`
даёт `Router is already attached`, а `web_app`/`db` замораживают `WEB_PASSWORD`
и `DB_PATH` на первом импортировавшем файле. Раннер даёт каждому файлу свой
процесс. `pytest tests` напрямую покажет 3 ошибки сбора — это ожидаемо.

`tests/known_failing.txt` — список временно отложенных файлов. Сейчас **пуст**:
все 67 файлов зелёные. Пока файл в списке, его падение не роняет сборку, но как
только он начинает проходить, раннер требует убрать строку.

Около 40 тестов помечены `@unittest.skip` — это проверки удалённых фич
(`/addword`, word filter) и текстового `/help` до перехода на Rich Message.
Причина всегда в тексте skip'а.

CI (`.github/workflows/ci.yml`) на каждый push и PR гоняет ruff, сюиту и
`docker build`.

## Переменные окружения

| Переменная | Обязательна | Назначение |
|---|---|---|
| `BOT_TOKEN` | **да** — иначе `RuntimeError` на старте | токен Telegram-бота |
| `WEB_PASSWORD` | де-факто да | пароль SU для веб-панели (логин `su`), сравнивается с env напрямую, не хешируется |
| `ADMIN_IDS` | да | TG ID глобальных супер-админов через запятую; обходят все проверки прав |
| `SESSION_SECRET` | нет, но нужна | без неё генерируется случайная при старте → все сессии слетают при рестарте |
| `DB_PATH` | нет | по умолчанию `/app/data/shadow_logs.db` |
| `WEBHOOK_URL` | нет | если задан и вебхук установился → webhook-режим, иначе long polling |
| `WEBHOOK_SECRET` | нет | проверяется в `X-Telegram-Bot-Api-Secret-Token`; без env генерируется случайный |
| `CHAT_HASHTAGS` | нет | `-100123:Тег,-100456:Тег2` — принудительно создаёт `chat_settings` для этих чатов |
| `WEB_PUBLIC_URL` | нет | база для ссылок в отчётах; дефолт захардкожен на прод-домен |
| `PORT` | нет | дефолт 3000 |
| `TRUSTED_PROXIES` | нет | прокси, которым можно верить в `X-Forwarded-For`; пусто = не верить никому (v4.8.8) |
| `DB_USE_LEGACY_MIGRATIONS` | **на проде да** | `1` → миграции через `init_db()` вместо Alembic |
| `WEB_SESSION_TTL_SECONDS` | нет | срок жизни сессии, дефолт 604800 (7 дней) |
| `WEB_COOKIE_SECURE` | нет | дефолт `1`; `0` для локальной разработки по http |
| `WEB_ALLOW_NO_SECRET` | нет | `1` разрешает старт без `SESSION_SECRET` (тесты) |

Пример со всеми переменными и пояснениями — `.env.example`.

Есть и не-env секрет: ключ шифрования GitHub PAT (`db.py:_load_or_create_enc_key`)
берётся из env или файла рядом с БД, при отсутствии генерируется с правами 0600.

## Архитектура

### Точка входа и цикл импортов

`bot.py` запускается как `__main__`, а другим модулям нужны его функции. Раньше
это решалось хаком `sys.modules.setdefault("bot", _self_module)` — иначе Python
грузил `bot.py` второй раз как модуль `bot`, повторно выполнял
`dp.include_router(...)` и падал с `RuntimeError: Router is already attached`.

**v4.8.9 хак убран.** Вместо него — service locator `app_state.py`: `bot.py` при
старте регистрирует свои функции, потребители достают их геттерами.

```python
# в bot.py при старте
register(exit_night_mode=_exit_night_mode, ...)

# в bot_handlers.py / web_app.py вместо `from bot import _exit_night_mode`
from app_state import get_exit_night_mode
_exit_night_mode = get_exit_night_mode()
```

Не возвращай `from bot import ...`. И учти при тестах: патчить надо
`app_state.get_*`, а не атрибут модуля `bot` — до реального вызова он не достаёт.

### Карта модулей

- `bot.py` (1.5k) — точка входа, `lifespan`, фоновые тики режимов, webhook-роут.
- `bot_handlers.py` (8.4k) — хендлеры и бизнес-логика модерации.
- `mod_commands.py` (1.1k) — 12 мод-команд, вынесенных из `handle_group_command`
  в v4.8.9/v4.8.10. Модуль импортирует хелперы из `bot_handlers` по именам,
  поэтому в тестах патчить надо `mod_commands.X`, а не `bot_handlers.X`.
- `web_app.py` (999 строк) — конфигурация, авторизация, module-level
  хелперы и `create_app()` как сборщик (~243 строки, 0 роутов внутри).
  В `web/` всего 54 роута: 7 вынесены раньше (v4.8.9/v4.8.10 — `/health`,
  `/logout`, `/`, `/avatar/{id}`, `/api/presets`, `/api/automute-count`),
  ещё 47 — в этот раунд (Task 1–11).
- `web/` — 11 модулей с роутами по предметным областям: `auth`, `me`,
  `api`, `health`, `admin_bans`, `admin_chats`, `admin_cleanup`,
  `admin_keywords`, `admin_presets`, `admin_settings`, `admin_users`,
  плюс `deps.py` с общими зависимостями (`require_auth`, `get_bot`,
  `get_templates` и др.). `bot` и `templates` приходят через `app.state`
  + `Depends`, не через замыкание. Декомпозиция завершена в v4.10.0.
- `app_state.py` — service locator, заменивший хак с `sys.modules`.
- `db.py` (1.6k) — модели SQLAlchemy + `init_db()`.
- `chat_modes.py` — snapshot/restore/apply прав чата (v4.8.0) и
  `_alarm_auto_off_tick` (переехал сюда из `bot.py` в v4.8.9).
- `modchat.py` — отправка в модчат, keyword-watch, rate-limit алармов.
- `github_client.py` — GitHub REST + GraphQL (Issues + Projects v2).
- `sticker_cache.py` — конвертация стикеров (webp/tgs/webm) в PNG через BytesIO.
- `tools/` — `run_tests.py` (раннер сюиты), `cleanup_test_data.py` (очистка
  тестовых данных из БД), `legacy/` (одноразовые кодмоды v4.8.8, не запускать).

### Режимы чата — главный инвариант проекта

Три режима меняют **chat-default permissions** (`set_chat_permissions`), а не
персональные рестрикты. Инварианты описаны в docstring `chat_modes.py:1-36` и в
`roadmap.md` §2. Нарушать нельзя:

1. Режимы не трогают per-user рестрикты. `!alarm off` не снимает персональные мьюты.
2. Snapshot берётся из `chat.permissions`, не из members.
3. `use_independent_chat_permissions=True` обязателен — централизован в
   `_apply_chat_permissions`.
4. Snapshot хранится в БД как JSON из 13 полей `_PERM_FIELDS`. Список полей
   продублирован в `chat_modes.py` и `bot_handlers.py` — при правке синхронизируй оба.
5. **Порядок тиков в `_night_mode_loop` (`bot.py`): alarm → sanitary → night.**
   Иначе sanitary снимет snapshot с alarm-состояния и права «залипнут».
6. Приоритет режимов: `sanitary > night > alarm > day`.

Восстановление дневных прав — четырёхуровневый fallback:
`day_permissions` пресет → сохранённый snapshot → системный пресет `Day default`
→ хардкод `_DAY_DEFAULT_HARDCODED` (продублирован в `bot.py` и `chat_modes.py`).

### Права доступа

Две независимые системы, которые нужно держать в голове одновременно:

- **В боте** — `_is_admin(session, chat_id, user_id)` (`bot_handlers.py:914`):
  `ADMIN_IDS` env → выключенный чат → `WebUser.role` (`su`/`admin` везде,
  `moderator` только при записи в `chat_admins`) → fallback на TG-only модератора
  из `chat_admins`. Деактивированный `WebUser` закрывает доступ окончательно.
- **В веб-панели** — зависимости `require_auth` / `require_admin` / `require_su`.
  Роль перечитывается из БД на каждый запрос, токен только подтверждает вход.

### База данных

SQLite + `aiosqlite`, WAL, `foreign_keys=ON` через listener на `connect`.
Миграции — рукописные, в `init_db()` (`db.py:743`, ~660 строк): для каждой колонки
`PRAGMA table_info(...)` → `ALTER TABLE ... ADD COLUMN`. Alembic нет.

При добавлении колонки: (1) поле в модель, (2) идемпотентный блок миграции в
`init_db()`, (3) **блок миграции обязан идти до любого ORM-запроса к этой таблице** —
ORM подставляет в SELECT все колонки модели и падает на старой БД. Именно этот
порядок ломал старт бота в v4.8.5.4.

### Changelog

Живёт в `templates/base.html` (модалка в футере, ~строки 640–1200) и это фактический
источник истины по релизам — он подробнее `roadmap.md` и обычно свежее его.
Версия — `APP_VERSION` в `web_app.py`.

### Кто отчитывается о наказаниях (решение, а не недоделка)

Отчёт в репорт-чат (`_send_report`) шлёт **только** `_check_warn_threshold` —
автомьют/бан по порогу варнов. Автоматические наказания молчат:

- `_check_via_bot_filter` (автомьют за via-bot флуд) — не отчитывается;
- `handle_content_filters` (автомьют за слова/ссылки) — не отчитывается;
  в модчат оттуда уходит только keyword-watch notify, но не само наказание.

Это проверялось и подтверждено владельцем 17.08.2026: репорт-чат ведёт
действия, начатые модератором, а автоматика в него не сыпет. Наказания
автоматики видны в веб-панели (`_save_punishment` вызывается везде) и в логах.

Не «чини» это, увидев асимметрию в коде — вопрос уже задавали.

### Разбан из веб-панели требует привязанного Telegram

`api_unban` пишет `mod_id = _auth.tg_user_id`. Учётки веб-панели заводятся
только через привязку в боте (sync-admins → `/start` → пароль), поэтому
обычный юзер без `tg_user_id` — нарушение инварианта: запрос отклоняется
с 400, а не выполняется.

Единственное исключение — встроенный `su`: он создаётся сидом `init_db`
(`db.py:1372`) и логинится по `WEB_PASSWORD`, TG ID у него нет по построению.
Его разбаны пишутся на `_SU_WEB_MOD_ID = -1`, а имя учётки уходит в текст
причины — иначе автор действия терялся бы совсем.

До v4.8.11 здесь стоял `tg_user_id or -1`: разбан от кого угодно проходил,
`_upsert_moderator` заводил несуществующего модератора `-1`, и на него
вешались все такие записи.

## Известные ловушки

- **Flood control обрабатывается только там, где вызов обёрнут.** Есть
  `tg_safe_call(factory, label=...)` — retry на `TelegramRetryAfter`. Но покрыты
  им не все вызовы: в `bot_handlers.py` +`mod_commands.py` около 20 call sites
  при сотнях `message.answer`/`reply`. Новый критичный вызов оборачивай.
  `factory` — callable, возвращающий **новую** корутину: после исключения
  корутину переиспользовать нельзя.
- **`asyncio.create_task` только через `_spawn_background_task`**
  (`bot_handlers.py:214`). Голый `create_task` без сохранения ссылки GC может
  собрать на середине. Инвариант сторожит `tests/test_v487_sanity.py` [9].
- **Блокирующий I/O в async — под запретом линтером.** `sqlite3`, `VACUUM`,
  `shutil.copy2` (v4.8.7) и запись файлов (v4.10.3) вынесены в
  `asyncio.to_thread`. Правило ruff `ASYNC230` включено без исключений:
  бот и веб-панель делят один event loop, и синхронная операция в роуте
  останавливает обработку сообщений во всех чатах.
- Хелперы `_user_mention_html` и `_get_chat_settings` продублированы в
  `bot_handlers.py` и `modchat.py` — правь обе копии.
- **Alembic есть, но выключен.** `migrations/` и `alembic.ini` в репозитории,
  однако прод работает через `DB_USE_LEGACY_MIGRATIONS=1` → старый `init_db()`.
  Попытка включить Alembic в v4.8.9 дважды уронила прод (auto-stamp на
  существующей БД). Не включай без проверки на staging.
- **`Form(...)` не принимает пустую строку.** После обновления стека (FastAPI
  0.115.6 → 0.141.1, Starlette 0.41 → 1.6, Task 17) пустое текстовое поле
  формы валидация считает отсутствующим значением и отсекает запрос ещё до
  хендлера сырым 422 (`{"detail":[{"type":"missing",...}]}`) — раньше `""`
  доходила до обработчика, и тот сам отвечал понятной ошибкой. Поэтому
  текстовые поля, которые пользователь заполняет руками, объявляются как
  `Form("")`, а не `Form(...)`; числовых (`punishment_id`, `user_id`,
  `chat_id`) это не касается — они приходят из сгенерированных форм. Контракт
  закреплён в `tests/test_v4812_empty_form_fields.py`.
- **Роутеры `web/` импортируются только внутри `create_app()`.** Top-level
  импорт `web.X` в `web_app.py` даёт цикл: `web_app → web.X → web.deps →
  web_app` (модули `web/` сами обращаются к `web_app` за хелперами).
- **Модули `web/` зовут хелперы `web_app` через модуль** (`web_app._helper(...)`),
  а не `from web_app import _helper`. Тесты патчат хелперы как атрибуты
  модуля `web_app`; именной импорт фиксирует значение на момент импорта, и
  патч в тестах перестаёт действовать.

## Стиль

- Комментарии-версии (`# v4.7.26: FIX ...`) — сложившаяся практика: правки
  помечаются версией с объяснением, что было сломано. Их ~250. При новом фиксе
  придерживайся формата, но не превращай комментарий в пересказ changelog.
- Ошибки Telegram лови как `TelegramAPIError` (базовый класс), а не
  `TelegramBadRequest` — `TelegramNotFound` и `TelegramForbiddenError` от него не
  наследуются, и это уже приводило к падению фоновых тиков.
