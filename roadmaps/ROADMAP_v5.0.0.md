# Roadmap / Backlog — v5.0.0

> Скелет roadmap'а для следующего MAJOR-релиза после v4.9.0.
> v5.0.0 — MAJOR bump. Запускается после завершения v4.9.0.
>
> **Пока НЕ заполняется деталями** — решение пользователя 9 августа 2026.
> Здесь только скелет, тема для обсуждения и перенесённый бэклог.
>
> Предыдущие ROADMAP'ы:
> - `ROADMAP_v4.8.0.md` (закрыт, v4.8.0 релизнут 8 августа 2026).
> - `ROADMAP_v4.8.x.md` (активный для патчей v4.8.1 - v4.8.5, bugfixes + minor).
> - `ROADMAP_v4.9.0.md` (активный для MINOR-цикла, по запросам модераторов).

---

## ⚠️ АРХИТЕКТУРНЫЕ ИНВАРИАНТЫ — НЕ НАРУШАТЬ

Перенесено из `ROADMAP_v4.8.0.md` (без изменений). 6 правил про alarm/night/
sanitary/snapshot/restore/use_independent_chat_permissions. См.
`ROADMAP_v4.8.0.md` раздел «АРХИТЕКТУРНЫЕ ИНВАРИАНТЫ».

При реализации любого пункта v5.0.0 — сверяться с этими правилами. MAJOR bump
**не повод** ломать работающее поведение.

---

## Статус

📋 **СКЕЛЕТ + план деплоя** — v5.0.0 не в работе, но план деплоя уже
сформулирован (см. ниже). Сначала завершаем v4.8.x, потом v4.9.0 (по запросам
модераторов, после совещания). v5.0.0 запускается после v4.9.0.

---

## Главная тема v5.0.0 — облегчение деплоя

**Источник:** запрос пользователя 9 августа 2026: «Давай подумаем как облегчить
деплой бота».

**Подтверждённая цель v5.0.0 по деплою (решение пользователя 9 августа 2026):**
перевести релизный цикл с ручной сборки zip-архивов + ручной загрузки на Bothost
на **нативный GitHub webhook Bothost**. Бот уже привязан к приватному GitHub-репо,
тариф Pro (максимальный), Bothost автоматически пересобирает Docker-образ при
пуше в репо.

### Архитектура деплоя (подтверждена)

**Как сейчас работает Bothost webhook** (по логам деплоя, предоставленным
пользователем 9 августа 2026):

1. GitHub отправляет webhook-событие на Bothost URL:
   `http://nl9.bothost.ru/api/webhooks/github?token=<webhook-token>`.
2. Bothost клонирует/пуллит **весь репозиторий** (без настройки путей — тянет
   корень репо).
3. Bothost собирает Docker-образ по встроенному Dockerfile:
   ```dockerfile
   FROM python:3.11-slim
   WORKDIR /app
   RUN mkdir -p /app/data && chmod 777 /app/data
   COPY requirements.txt .
   RUN pip install --no-cache-dir -r requirements.txt
   COPY . .
   EXPOSE 3000
   CMD ["python", "bot.py"]
   ```
4. Volume `/app/data` создаётся явно (chmod 777) — это **точка персистентности**
   между деплоями. Всё, что вне `/app/data`, затирается при `COPY . .`.
5. Контейнер перезапускается с новым образом.

**Ключевое наблюдение:** Dockerfile здесь — встроенный в Bothost, пользователь
его НЕ контролирует. Мы не кладём свой Dockerfile в репо. Контролируем только
содержимое репо (`requirements.txt`, `bot.py`, остальные `.py`-файлы, шаблоны).

**Что НЕ нужно делать:**
- ❌ Писать свой Dockerfile — Bothost использует свой.
- ❌ Писать CI/CD pipeline (GitHub Actions) для сборки zip — Bothost сам собирает.
- ❌ Настраивать webhook-приёмник в боте — Bothost сам принимает webhook от GitHub.
- ❌ Пушить `.env` в репо — Bothost подставляет env из своей панели управления.
- ❌ Пушить `bot.db` в репо — БД живёт в volume `/app/data`, не в репо.

**Что нужно сделать (см. детали ниже):**
- ✅ Привести репо в порядок: `.gitignore`, `.env.example`, `VERSION`-файл.
- ✅ Настроить GitHub webhook на Bothost URL с правильным триггером.
- ✅ Изменить `web_app.py`: `APP_VERSION` из `VERSION`-файла (не хардкод).
- ✅ Убедиться, что `bot.db` лежит в `/app/data` (volume), а не в `/app`.
- ✅ Сохранить `requirements.txt` актуальным (Bothost тянет зависимости оттуда).
- ✅ Удалить старые тестовые файлы из репо (пользователь сделает сам).

### План деплоя v5.0.0 (детальный)

#### Шаг 1. Репозиторий — структура и вспомогательные файлы

**Новые файлы в репо:**

**`.gitignore`** (NEW):
```gitignore
# Secrets & env
.env
.env.*
!.env.example

# Database
*.db
*.db-journal
*.db-wal
*.db-shm

# Python
__pycache__/
*.py[cod]
*$py.class
*.so
.Python
venv/
.venv/
env/

# Tests & build artifacts
.pytest_cache/
htmlcov/
.coverage
*.zip
*_delta_*.zip
*_tests.zip
*_CHANGES.md

# Logs
*.log
logs/

# IDE
.idea/
.vscode/
*.swp
*.swo

# OS
.DS_Store
Thumbs.db
```

**`.env.example`** (NEW) — шаблон всех env-переменных, которые Bothost ожидает
увидеть в панели управления. Не содержит реальных секретов. Используется
только как документация: что нужно прописать в Bothost → Env vars.

```env
# Telegram
BOT_TOKEN=<get from @BotFather>
ADMIN_IDS=123456789,987654321

# Web panel
SU_PASSWORD=<strong-password>
SESSION_SECRET=<random-32-bytes-hex>
WEBHOOK_URL=https://<bot-subdomain>.bothost.ru/
WEBHOOK_PORT=3000

# Database (внутри контейнера, /app/data/bot.db)
DATABASE_URL=sqlite+aiosqlite:///app/data/bot.db

# Bothost agent
BOTHOST_API_URL=http://agent:8000

# Logging
LOG_LEVEL=INFO

# (опционально, для v5.0.0 фич)
# GITHUB_RULES_TOKEN=
# GITHUB_RULES_REPO=
# GITHUB_RULES_BRANCH=main
# GITHUB_RULES_PATH=index.html
# GITHUB_WEBHOOK_SECRET=
# SU_TOTP_SECRET=
```

**`VERSION`** (NEW) — текстовый файл с одной строкой, без `v`-префикса:
```
5.0.0
```

Файл лежит в корне репо. Обновляется одним commit'ом в момент релиза.
Бот читает его при старте и подставляет в `APP_VERSION`.

**`requirements.txt`** (уже есть, проверяем актуальность):
- Должен содержать все runtime-зависимости (`aiogram`, `fastapi`, `sqlalchemy`,
  `aiosqlite`, `jinja2`, `uvicorn`, `httpx` и т.д.).
- Не должен содержать dev-зависимости (`pytest`, `pytest-asyncio` — они нужны
  только локально, в контейнере Bothost тесты не запускаются).
- Пинить версии — обязательно (`aiogram==3.x.y`, не `aiogram>=3.0`).

**Удалить из репо (пользователь сделает сам):**
- Старые тестовые файлы (`test_*.py` в корне или в `tests/`), которые уже
  неактуальны. Перед удалением — проверить, что они не referenced в
  `build_v48X_release.sh`.
- Любые `*.db` файлы, если случайно закоммичены (`.gitignore` не ретроактивен).
- Любые `.env` файлы, если случайно закоммичены (то же).

#### Шаг 2. APP_VERSION в web_app.py — читать из VERSION-файла

**Сейчас** (v4.8.x):
```python
APP_VERSION = "v4.8.2"
```
Хардкод. Забываем bump'нуть → рассинхрон с changelog в `base.html`.

**В v5.0.0:**
```python
from pathlib import Path

_VERSION_FILE = Path(__file__).resolve().parent / "VERSION"


def _read_version() -> str:
    """Read version from VERSION file. Falls back to 'v0.0.0-unknown' if missing."""
    try:
        raw = _VERSION_FILE.read_text(encoding="utf-8").strip()
        if not raw:
            return "v0.0.0-unknown"
        return f"v{raw}" if not raw.startswith("v") else raw
    except (FileNotFoundError, OSError):
        return "v0.0.0-unknown"


APP_VERSION = _read_version()
```

**Почему так, а не `git describe --tags`:**
- В Docker-контейнере Bothost нет `.git/` директории (только `COPY . .`, без
  `.git/`). `git describe` не сработает.
- `VERSION`-файл — прост, читается за O(1), не требует зависимостей.
- При релизе: bump `VERSION` одним commit'ом → Bothost пересобирает → бот
  стартует с новой версией → `/admin/system` и `base.html` показывают её.

**В `templates/base.html`** — заменить хардкод версии на `APP_VERSION`
(через контекст FastAPI, как уже сделано для других шаблонов).

#### Шаг 3. БД в `/app/data` — проверить персистентность

**Гипотеза (по логам Bothost):** `bot.db` лежит в `/app/data/bot.db`, что
является volume'ом — БД переживает деплой. Если же `bot.db` лежит в `/app/bot.db`,
то при каждом `COPY . .` он затирается (если только Bothost не исключает его
из copy — неочевидно).

**Что нужно проверить (после первого деплоя v5.0.0):**
1. Зайти в Bothost → File manager или через `POST /api/bots/logs` посмотреть
   логи старта. Должна быть строка про открытие БД по пути `/app/data/bot.db`.
2. Если путь другой — изменить `DATABASE_URL` в env (на стороне Bothost):
   ```
   DATABASE_URL=sqlite+aiosqlite:////app/data/bot.db
   ```
   (4 слеша для абсолютного пути в SQLite URL).
3. После первого деплоя v5.0.0 — сделать рестарт и убедиться, что данные
   (модераторы, баны, настройки чатов) на месте.

**Если сейчас БД лежит в `/app/bot.db` (не в volume):**
- До v5.0.0 — миграция: остановить бота, скачать `bot.db` из Bothost file
  manager, поправить `DATABASE_URL`, залить `bot.db` обратно в `/app/data/`,
  запустить.
- Альтернатива: просто не трогать до v5.0.0 — при первом деплое v5.0.0 БД
  будет «как есть», потом разово мигрируем.

#### Шаг 4. GitHub webhook — настройка триггера

**Webhook URL (Bothost):**
```
http://nl9.bothost.ru/api/webhooks/github?token=<webhook-token>
```
`<webhook-token>` — берётся из панели Bothost → Webhooks → GitHub.

**На стороне GitHub:**
- Repository → Settings → Webhooks → Add webhook.
- Payload URL: Bothost webhook URL (см. выше).
- Content type: `application/json`.
- Secret: можно оставить пустым (Bothost token в query string уже служит
  авторизацией), но лучше — задать и на Bothost, и на GitHub (двойная защита).
- Events: **customize** → выбрать только нужные.

**Триггер — release vs push (рекомендация): release**

| Триггер | Что происходит | Плюс | Минус |
|---------|---------------|------|------|
| `push` to `main` | Любой commit в main → деплой | Быстрый feedback | Любой WIP commit уходит в прод |
| `release` (published) | Только публикация release → деплой | Контроль: ничего не уходит без явного действия | Лишний шаг (создать release) |

**Рекомендация:** использовать `release` (событие `release.published`).
- Пользователь — единственный разработчик, но даже единственный разработчик
  может по ошибке запушить сломанный код в main.
- Release — это и есть «релизный цикл»: тегнули `v5.0.0` → GitHub создал
  release → Bothost подхватил → бот пересобрался.
- Откат: удалить release (или отметить как draft) → Bothost не откатит
  автоматически, но можно запустить старый release повторно.

**Альтернатива (если релиз слишком формален):** `push` to `main` + ветка
`dev` для WIP-коммитов. Pull request из `dev` в `main` = релиз. Bothost
деплоит только при merge в `main`.

#### Шаг 5. Health-check после деплоя

**В `bot.py` (после старта):**
- `/health` endpoint уже есть — Bothost agent пингует его периодически.
- Дополнительно: после первого запроса к Telegram API при старте — записать
  в лог строку `STARTUP_OK version=<APP_VERSION> webhook=<URL>`.

**В Bothost панели:**
- Раздел «Логи» — после деплоя открыть логи, убедиться, что:
  - Контейнер стартовал без ошибок импорта.
  - Webhook зарегистрирован в Telegram API (ответ 200 от `setWebhook`).
  - Первый `getMe` вернул корректные данные о боте.

**Алёрт при падении после деплоя (опционально, через exit1.dev — см. §7.2):**
- exit1.dev пингует `/health` каждые 5 минут.
- Если 3 пинга подряд упали → webhook в modchat → модератор видит, что бот упал
  после релиза → может откатить.

#### Шаг 6. Rollback стратегия

**Rollback через GitHub release:**
1. Зайти в GitHub → Releases.
2. Найти предыдущий стабильный release (например `v4.9.0`).
3. Bothost webhook сработал только на `release.published` — повторной
  публикации не будет. **Rollback через release не работает автоматически.**

**Ручной rollback (через Bothost панель):**
1. Bothost → Deploy → выбрать предыдущий коммит (если панель показывает историю
   коммитов) → «Deploy this commit».
2. Если Bothost не умеет — `git revert` в репо, push, Bothost пересоберёт.
3. В крайнем случае — `git reset --hard <stable-sha>` + force push (рискованно,
   только если совсем сломалось).

**Что нужно от пользователя:** уточнить в Bothost docs, есть ли в панели
«Deploy from specific commit» — это самый чистый rollback. Если нет — фиксируем
процедуру `git revert + push`.

#### Шаг 7. Миграция с zip-деплоя на webhook-деплой

**Последовательность перехода (не в v5.0.0, а ЗА НЕСКОЛЬКО ДНЕЙ ДО v5.0.0):**

1. **Подготовка репо (за 1-2 дня до v5.0.0):**
   - Добавить `.gitignore`, `.env.example`, `VERSION` (содержимое `4.8.2`).
   - Проверить, что `requirements.txt` полный и без dev-зависимостей.
   - Пользователь удаляет старые тесты из репо.
   - Закоммитить → запушить в `main` (пока без webhook — Bothost не деплоит).

2. **Проверка совместимости (в день v5.0.0):**
   - Убедиться, что текущий прод (v4.8.2) собирается из этого репо: Bothost
     → Deploy from Git → выбрать HEAD `main` → задеплоить.
   - Если Bothost собрал и бот стартовал — отлично, репо готов к webhook'у.
   - Если упало — фиксы в репо (чаще всего: забытый импорт, не та версия
     aiogram в `requirements.txt`).

3. **Включение webhook (в день v5.0.0):**
   - GitHub → Settings → Webhooks → Add webhook (см. Шаг 4).
   - Bothost → Webhooks → GitHub → скопировать token.
   - Создать release `v5.0.0` на GitHub (tag `v5.0.0`, title `v5.0.0`,
     description = changelog).
   - Bothost получает webhook → собирает → деплоит.

4. **Smoke-test после деплоя v5.0.0:**
   - `/health` отвечает 200.
   - `/help` в Telegram — версия `v5.0.0`.
   - `/admin/system` в веб-панели — версия `v5.0.0`.
   - Тест: выдать `/swarn` тестовому юзеру → проверка наказания.
   - Тест: добавить слово через `/admin/keywords` → проверка записи в БД.

5. **Декомиссия zip-деплоя:**
   - После 3-5 дней стабильной работы v5.0.0 через webhook — перестать
     собирать `ded-vobzhak-X.Y.Z.zip` архивы.
   - `scripts/build_v48X_release.sh` — больше не нужен, можно оставить
     как историческую справку.
   - `/home/z/my-project/download/` — архивы предыдущих версий оставляем
     как бэкап (не удаляем).

### Идеи для обсуждения (помимо деплоя)

Деплой — основная, но не единственная тема v5.0.0. Ниже — идеи, которые стоит
рассмотреть после запуска webhook-деплоя (черновик, всё TBD):

- **Миграции БД.** Сейчас схема SQLite эволюционирует «в коде» — никаких
  alembic-миграций, прирост колонок делается через `ALTER TABLE` вручную или
  через `IF NOT EXISTS`. Для v5.0.0 — alembic или аналог, с automatic rollback
  при неудачной миграции.
- **Env-менеджмент.** Сейчас 15+ env-переменных, часть обязательна, часть нет,
  часть устаревает между версиями. В v5.0.0 — `config.py` с валидацией,
  deprecation-warnings для устаревших vars, единый источник правды.
- **Webhook re-registration при деплое.** При смене домена / хостинга —
  перерегистрация webhook сейчас ручная. Автоматизировать: при старте бот
  проверяет `WEBHOOK_URL` и при необходимости перерегистрирует.
- **Health-check + auto-restart.** Сейчас `/health` есть, но за рестарт отвечает
  Bothost agent. В v5.0.0 — встроенный watchdog, который при падении underlying
  service (TG API, БД) сам рестартит бота или алертит. **Пересекается с §7.2
  exit1.dev и §6 BH-8.**
- **Single-command deploy.** После перехода на webhook — почти не нужно: push
  в main = деплой. Остаётся только `make release VERSION=5.0.1` — создать
  release на GitHub (через `gh` CLI).

### Что нужно от пользователя

- ✅ Подтвердить: триггер `release.published` (рекомендация) или `push` to `main`?
  (рекомендация — release).
- ✅ Подтвердить: миграция `bot.db` в `/app/data` (если ещё не там) — делать
  до v5.0.0 или в момент v5.0.0?
- ✅ Проверить в Bothost docs: есть ли «Deploy from specific commit» для
  rollback?
- ⏸ Решение по миграциям (alembic vs самопис vs ничего) — после v5.0.0.
- ⏸ Решение по staging: не нужно (1 разработчик, SQLite без миграций, откат
  через release) — подтверждено 9 августа 2026.
- ⏸ Бюджет времени на v5.0.0 (major bump — это недели, не дни).

---

## Перенесённый бэклог (из `ROADMAP_v4.9.0.md` и `ROADMAP_v4.8.x.md`)

Все крупные задачи, ранее запланированные на v4.9.0, перенесены сюда
(решение пользователя 9 августа 2026). v4.9.0 теперь зарезервирован
исключительно под запросы модераторов (+ рефакторинг, перенесённый из
v4.8.4 11 августа 2026).

Дополнительно:
- B2-бэкап (#7.1) перенесён из `ROADMAP_v4.8.x.md` v4.8.3
  (решение пользователя 11 августа 2026) — инфраструктурная задача, лучше
  делать в контексте v5.0.0 «облегчение деплоя».
- §10 «Импорт участников и банов через userbot» — новая задача
  (зарегистрирована 11 августа 2026 после обсуждения с пользователем),
  статус ⚠️ **ПОДУМАТЬ НАД РЕАЛИЗАЦИЕЙ**.

Эти задачи — **кандидаты** на v5.0.0, не финальный план. После запуска v5.0.0
пересматриваем приоритеты с учётом темы «облегчение деплоя».

### 0. Авто-бэкап SQLite в Backblaze B2 (#7.1) — перенесён из v4.8.3

**Статус:** 📋 запланировано (перенесено 11 августа 2026).

**Цель:** защитить БД от потери при падении Bothost.

**Что делаем:**
- Новый модуль `backup.py` — фоновая задача раз в сутки (ночью).
- Копировать `bot.db` → `bot_backup_YYYYMMDD.db`.
- Загружать в B2 через `boto3` или `aiobotocore`.
- Хранить 7 последних бэкапов, старые удалять.
- Виджет последнего бэкапа на Dashboard (или на `/admin/system`).
- Таблица `backup_history` для аудита.

**Что нужно от пользователя:**
- **KeyID** приложения B2 (создаётся в B2 dashboard → App Keys).
- **applicationKey** (secret) — показывается один раз при создании key.
- **Bucket name** (например `ded-vobzhak-backups`).
- Опционально: регион bucket'а (B2 по умолчанию US, можно выбрать EU).
- Уточнить: хочет ли хранить бэкапы дольше 7 дней.

**env vars (новые):**
- `B2_KEY_ID` — KeyID.
- `B2_APPLICATION_KEY` — secret.
- `B2_BUCKET_NAME` — имя bucket'а.
- `B2_BACKUP_RETENTION_DAYS` — `7` (default).

**Файлы, затронутые:**
- `backup.py` (NEW) — клиент B2, фоновая задача.
- `bot.py` — запуск фоновой задачи в `lifespan`.
- `web_app.py` — виджет на Dashboard (или на `/admin/system`).
- `db.py` — таблица `backup_history`.
- `scripts/test_v500_b2_backup.py` (NEW) — mock S3 client, retry, retention cleanup.

**Сложность:** ~1 день.

**Связь с темой деплоя v5.0.0:** бэкап — часть инфраструктуры, как и
миграции БД и rollback. Логично делать в одном цикле с настройкой
webhook-деплоя: при первом webhook-релизе бэкап уже должен работать
(на случай если webhook-деплой что-то сломает в БД).

---

### 1. Join requests (заявки на вступление в чат)

**Статус:** ⏸ отложено до решения владельца чата («Бабай»).

Пользователь опрашивает других модераторов — собирает пожелания. Ждём результата
опроса, после чего сформулируем конкретные требования.

**Возможные сценарии (черновик):**
- Бот присылает админам уведомление о новой заявке.
- Бот автоматически approve/reject по заданным критериям.
- Бот ведёт лог заявок в веб-панели.

Решение примем после разговора с Бабай и сбора фидбэка от модераторов.

---

### 2. Интеграция банов со стрим-платформ (Twitch + GoodGame)

**Статус:** ➡️ перенесено в отдельный проект.

**Перенос (12 августа 2026):** стрим-интеграция вынесена из v5.0.0 в
**отдельный проект «Дедушка Стримов»** (streambot). Решение пользователя:
развивать независимо, в отдельном чате разработки, начиная с v0.0.1.

**Полный план:** см. [`ROADMAP_streambot.md`](./ROADMAP_streambot.md).

**Краткое описание:** отдельный бот (свой BOT_TOKEN, свой контейнер на
Bothost, своя веб-панель на отдельном поддомене), который только читает
события банов/мутов с Twitch (EventSub) и GoodGame (WebSocket), пишет в
общую PostgreSQL (shared с основным ботом, separate `stream_*` таблицы),
показывает в веб-панели карточки нарушителей и опционально постит в
отдельный stream-log TG-чат.

**Ключевые принципы (фиксированные пользователем):**
- Только логирование, без команд модераторам стрима.
- Без `/link` привязки аккаунтов.
- Модераторы стрима ≠ модераторы TG (отдельная таблица `stream_moderators`,
  отдельные роли `stream_admin` / `stream_moderator`).
- Секреты (OAuth-токены Twitch, GG) — в БД, зашифрованные Fernet.
- Stream-log TG-чат — отдельный, настраивает `stream_admin` / `su`.

**Связь с основным ботом v5.0.0:**
- Общая PostgreSQL на `node1.pghost.ru` (Bothost managed).
- Стрим-бот читает `users` (read-only) для попытки сопоставить
  `platform_username` → `tg_user_id`.
- Основной бот НЕ знает о стрим-боте (полная изоляция сбоев).
- Миграция SQLite → PostgreSQL — общая задача, см. §1 v5.0.0 (этап 0).

---

### 5. Расширение настроек SU в веб-панели

**Статус:** 💡 ждём выбора пользователя — какие группы/пункты включить.

**Контекст:** сейчас SU-меню состоит только из вкладки Admins (`/admin/users`).
Хочется добавить больше настроек.

**Предлагаемые группы:**

**Группа 1 — Управление чатами:**
- **A1.** Список подключённых чатов с возможностью: включить/выключить бота,
  изменить хэштег, изменить `report_chat_id`.
- **A2.** Редактирование порогов `warns_to_mute`, `mute_duration_seconds`,
  `warns_to_ban` для каждого чата через UI.
- **A3.** Sync admins per-chat — кнопка «синхронизировать админов из TG».

**Группа 2 — Word/Link фильтры (глобально):**
- **B1.** Управление глобальным word-filter (после v4.8.x — keyword-watch).
- **B2.** Управление link allowlist.
- **B3.** Включение/выключение link-filter per-chat.

**Группа 3 — Безопасность и аудит:**
- **C1.** Лог действий веб-юзеров.
- **C2.** Активные сессии с возможностью отозвать.
- **C3.** Журнал входов (кто и когда логинился, неудачные попытки).

**Группа 4 — Системные настройки:**
- **D1.** Просмотр env-конфигурации (read-only, BOT_TOKEN замаскирован).
- **D2.** Управление SU-паролем.
- **D3.** Управление SESSION_SECRET (ротация).
- **D4.** Системная информация (версия бота, uptime, размер БД, кол-во записей).

**Группа 5 — Бэкап и обслуживание БД:**
- **E1.** Экспорт БД.
- **E2.** Очистка старых записей.
- **E3.** Просмотр размера таблиц.

**Группа 6 — Управление модераторами TG:**
- **F1.** Список всех модераторов с привязкой к чатам.
- **F2.** Ручное добавление TG-модератора.

**Оценка сложности:**
| Группа | Сложность | Оценка |
|--------|-----------|--------|
| G1 (чаты) | средняя | 1.5 дня |
| G2 (фильтры) | средняя | 1.5 дня |
| G3 (аудит) | средняя-высокая | 2 дня |
| G4 (системные) | низкая | 0.5 дня |
| G5 (бэкап) | низкая-средняя | 1 день |
| G6 (TG-модераторы) | средняя | 1 день |

---

### 6. Bothost API — расширение SU-панели

**Статус:** 📋 запланировано. **Перед реализацией каждого пункта — отдельное
подтверждение пользователя.**

**Документация:** https://bothost.ru/docs/api-reference

**Принцип:** использовать данные, доступные через Bothost API агента
(`http://agent:8000`).

**API endpoints (через `http://agent:8000`):**
- `POST /api/bots/self/restart` — самоперезапуск.
- `POST /api/bots/restart` — рестарт по `bot_id`+`user_id`.
- `POST /api/bots/stop` — остановка бота.
- `POST /api/bots/start` — запуск бота.
- `POST /api/bots/logs` — получение логов (`lines: N`).
- `GET /api/bots/{bot_id}/stats` — CPU%, memory, uptime.

**Варианты (каждый требует отдельного подтверждения):**

| # | Идея | Сложность | Оценка |
|---|------|-----------|--------|
| **BH-1** | Кнопка «Перезапустить бота» в веб-панели | низкая | ~0.5 дня |
| **BH-2** | Просмотр логов бота (`/admin/logs`) | низкая-средняя | ~1 день |
| **BH-3** | Системная статистика контейнера (`/admin/system`) | средняя | ~1.5 дня |
| **BH-4** | Управление состоянием бота (stop/start) | низкая | ~0.5 дня |
| **BH-5** | Метаданные хостинга в System Info | тривиальная | ~0.2 дня |
| **BH-6** | Live-стрим логов через polling | средняя | ~1 день |
| **BH-7** | История перезапусков | низкая-средняя | ~0.5 дня |
| **BH-8** | Авто-проверка здоровья (health check) с алёртом | средняя | ~1 день |
| **BH-9** | Кнопка «Обновить с GitHub» (webhook-деплой trigger) | низкая | ~0.5 дня |
| **BH-10** | Версия бота + последний commit в System Info | тривиальная | ~0.2 дня |

#### BH-9: «Обновить с GitHub» — отдельное обсуждение

**Вопрос пользователя 12 августа 2026:** может ли бот внутри Docker-контейнера
выполнять команды на перезапуск/обновление с GitHub?

**Краткий ответ:** технически — да, но в инфраструктуре Bothost это надо
делать НЕ через выполнение команд внутри контейнера, а через **agent API
Bothost**. См. анализ ниже.

##### Анализ: 3 способа реализовать «обновить бота с GitHub»

**Способ A. Бот сам дёргает `git pull` + `pip install` + рестарт внутри
контейнера.**

- Реализация: `subprocess.run(["git", "pull"], cwd="/app")`,
  затем `subprocess.run(["pip", "install", "-r", "requirements.txt"])`,
  затем `os.execv(...)` для рестарта процесса.
- **Почему НЕ работает в Bothost:**
  1. Bothost Dockerfile использует `COPY . .` (см. §«Архитектура деплоя»).
     В контейнере **нет `.git/` папки** — `git pull` не из чего pull'ить.
  2. Даже если бы папка была — `pip install` modifies `/usr/local/lib/python3.11/...`,
     что **перезатирается** при следующем `COPY . .` / пересборке образа.
  3. `os.execv()` перезапускает Python-процесс, но **контейнер остаётся** —
     если зависла зависимость или сломался import, бот упадёт и не поднимется
     (Docker restart policy может не сработать, если exit code быстрый).
- **Вердикт:** ❌ не использовать.

**Способ B. Бот делает HTTP-запрос к Bothost agent API → agent пересобирает
образ через webhook GitHub.**

- Реализация: `POST /api/bots/update` (или эквивалент) на
  `http://agent:8000` с `bot_id` + `user_id`.
- Agent Bothost:
  1. Дёргает GitHub webhook trigger (или сразу pull'ит репо).
  2. Пересобирает Docker-образ с нуля (`COPY . .` + `pip install`).
  3. Перезапускает контейнер с новым образом.
- **Плюсы:**
  - Использует штатный механизм Bothost (тот же, что и при ручном деплое
    через Their Panel).
  - Никаких хаков внутри контейнера.
  - Образ всегда чистый, состояние контейнера предсказуемое.
  - Логи обновления видны в Bothost Panel (а не только в логах бота).
- **Минусы:**
  - Нужен `BOT_ID` + `USER_ID` в env (или hardcoded).
  - Нужен Bearer token для авторизации на agent API.
- **Вердикт:** ✅ рекомендуемый способ. Реализуется как BH-9.

**Способ C. SU нажимает кнопку в веб-панели → GitHub webhook manually.**

- Реализация: в веб-панели кнопка «Open GitHub release page» со ссылкой
  на `https://github.com/<user>/<repo>/releases/new`.
- SU сам создаёт release на GitHub → GitHub шлёт webhook → Bothost
  пересобирает.
- **Плюсы:** вообще ничего не делаем в коде (только кнопка-ссылка).
- **Минусы:** лишний клик, SU должен иметь доступ к GitHub.
- **Вердикт:** ⚠️ fallback, если agent API не работает / нет токена.

##### Рекомендация

Реализовать **BH-9 через способ B** (agent API). Алгоритм:

1. SU открывает `/admin/system` (или новую вкладку `/admin/deploy`).
2. Видит текущую версию (из `VERSION`-файла, см. §«Шаг 2» плана деплоя).
3. Видит последний commit на GitHub (через GitHub API `GET /repos/.../commits/main`).
4. Если локальная версия < последнего commit'а — кнопка «Обновить» активна.
5. SU нажимает «Обновить» → бот делает `POST /api/bots/update` на agent.
6. Веб-панель показывает прогресс (polling `/api/bots/status`).
7. После рестарта — бот присылает SU уведомление в репорт-чат:
   «✅ Обновлено с v4.8.3 до v5.0.0».

**Что нужно от пользователя для BH-9:**
- ✅ Подтвердить, что agent API на `http://agent:8000` (или
  `http://nl9.bothost.ru`) доступен из контейнера бота (проверить через
  `curl` из контейнера или просто попробовать).
- ⏸ Получить `BOT_ID` (в Bothost Panel → bot info) и `USER_ID` (ваш
  Bothost account ID).
- ⏸ Получить Bearer token для agent API (в Bothost Panel → API keys
  или похожий раздел). Если такого нет — попросить support Bothost.
- ⏸ Подтвердить: триггерим update через `POST /api/bots/update` (если
  такой endpoint существует) или через ручной GitHub webhook
  (способ C, без agent API).

##### Связь с темой v5.0.0 «облегчение деплоя»

BH-9 — логичное продолжение webhook-деплоя (см. §«План деплоя v5.0.0»):
после того как webhook настроен, кнопка «Обновить» в веб-панели даёт SU
возможность обновлять бота **без захода в GitHub / Bothost Panel**. Один
интерфейс для всего.

BH-10 (версия + последний commit) — естественное дополнение: SU видит,
есть ли что обновлять.

**NB:** BH-1, BH-4, BH-6, BH-7, BH-9, BH-10 пересекаются с темой
«облегчение деплоя» — возможно, реализовать раньше, в рамках deploy-theme
задач v5.0.0.

#### BH-9 Resilience: что работает при частичном падении Bothost

**Вопрос пользователя 12 августа 2026:** «А если повторится такая ситуация
как сейчас с веб панелью хостинга — получится ли дёргать за api или ничего
не получится?»

**Сценарии падения Bothost:**

| Что лежит | BH-9 через agent API | BH-9 через Plan C (git pull) | Бот работает? |
|---|---|---|---|
| Billing panel (RU nodes), agent на ноде с ботом жив | ⚠️ МОЖЕТ работать | ✅ Работает | ✅ Да |
| Agent на ноде с ботом упал | ❌ Не работает | ✅ Работает | ⚠️ Возможно |
| Bothost целиком (все ноды) | ❌ Не работает | ✅ Работает (пока контейнер жив) | ⚠️ Возможно |
| Только GitHub лежит | ❌ (webhook не дойдёт) | ❌ git pull не сработает | ✅ Да |
| Контейнер бота упал | ❌ | ❌ | ❌ Нет |

**Вывод:** agent API — **НЕ 100% guarantee**. Plan C (bot self-update via
`git pull` + `os.execv`) — единственный способ, который работает при
лежащем Bothost billing/agent, пока жив контейнер бота.

#### BH-9 Plan C: bot self-update via git pull + os.execv

**Источник:** анализ resilience 12 августа 2026. Пользователь подтвердил,
что Dockerfile контролируется им (лежит в репо `/home/z/my-project/v4.5/Dockerfile`),
а не встроенный в Bothost.

##### Архитектура Plan C

```
[SU нажимает «Обновить» в веб-панели]
        ↓
[bot.py: handler /api/deploy/update]
        ↓
1. git fetch origin main
2. git checkout main && git reset --hard origin/main
        ↓
3. Pre-flight check:
   python -c "import bot_handlers"  # синтаксис OK?
        ↓ если OK
4. Если requirements.txt изменился:
   pip install -r requirements.txt
        ↓
5. Запуск миграций БД (если есть alembic):
   alembic upgrade head
        ↓
6. os.execv(sys.executable, [sys.executable, "bot.py"] + sys.argv)
   — текущий Python-процесс заменяется новым
        ↓
7. Бот стартует с новым кодом → шлёт уведомление в репорт-чат:
   «✅ Обновлено с v4.8.3 до v5.0.0»
```

**Если pre-flight check упал (шаг 3):**
- `git reset --hard HEAD~1` — откат к предыдущему commit'у.
- `os.execv` — рестарт со старым кодом.
- Уведомление SU: «❌ Обновление не удалось, откатились. Ошибка: ...».

##### Изменение Dockerfile

Текущий Dockerfile (подтверждён пользователем 12 августа 2026):

```dockerfile
FROM python:3.11-slim
WORKDIR /app
RUN mkdir -p /app/data && chmod 777 /app/data
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 3000
CMD ["python", "bot.py"]
```

Новый Dockerfile для Plan C (минимальные изменения):

```dockerfile
FROM python:3.11-slim

WORKDIR /app

# v5.0.0 Plan C: git для self-update (bot git pull + os.execv)
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# Создаём директорию для SQLite и даём права на запись
RUN mkdir -p /app/data && chmod 777 /app/data

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 3000
CMD ["python", "bot.py"]
```

**Изменения:** +1 layer `apt-get install git` (~30 МБ к размеру образа).
Никаких других изменений — `COPY . .` остаётся, build процесс не меняется.

##### Критический вопрос про `.git/` в образе

`COPY . .` копирует **всё** из build context (репо), включая `.git/`,
**ЕСЛИ** нет `.dockerignore`, который его исключает.

- В текущем репо `/home/z/my-project/v4.5/` нет `.dockerignore` →
  если Bothost клонирует репо через `git clone` во временную папку и
  собирает образ оттуда, `.git/` попадёт в образ → `git pull` сработает.
- Если Bothost использует `git archive` / `tarball` — `.git/` отрезается,
  `git pull` упадёт с «not a git repository».

**Что нужно проверить у Bothost support:**
1. Как именно Bothost готовит build context при webhook-деплое?
   - `git clone` → `.git/` есть в образе ✅
   - `git archive` / `tarball` → `.git/` нет ❌
2. Если `.git/` нет — можно ли использовать в Dockerfile
   `RUN git clone https://<token>@github.com/<user>/<repo>.git /app`
   вместо `COPY . .`? Это требует GitHub PAT как build arg.

##### Альтернатива: git clone в Dockerfile (сверх-надёжный вариант)

```dockerfile
FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# v5.0.0: clone репо прямо в образ (нужен GitHub PAT как build arg)
# PAT хранится в .git/config внутри образа — образ приватный (Bothost
# не публикует), риск минимальный.
ARG GITHUB_PAT
RUN git clone https://${GITHUB_PAT}@github.com/<user>/<repo>.git /app

RUN mkdir -p /app/data && chmod 777 /app/data
RUN pip install --no-cache-dir -r requirements.txt

EXPOSE 3000
CMD ["python", "bot.py"]
```

**Плюсы:** `.git/` гарантированно есть, `git pull` сработает.
**Минусы:** PAT в образе, нужно настроить Bothost для передачи build arg.

##### Реализация в коде (после решения по Dockerfile)

Новый модуль `deploy.py`:

```python
# deploy.py
import os
import sys
import subprocess
import logging
from pathlib import Path

logger = logging.getLogger("shadow_logger.deploy")

REPO_DIR = Path("/app")


async def self_update() -> dict:
    """Plan C: git pull + pre-flight check + os.execv.

    Returns:
        {"ok": True, "old": "...", "new": "..."} если обновление началось
        (процесс вот-вот заменится).
        {"ok": False, "error": "..."} если что-то пошло не так.

    NB: при успешном обновлении функция НЕ возвращает управление —
    os.execv заменяет процесс.
    """
    # 1. Текущая версия
    old_version = _read_version()

    # 2. git fetch + reset --hard origin/main
    try:
        subprocess.run(["git", "fetch", "origin", "main"],
                       cwd=REPO_DIR, check=True, capture_output=True, timeout=30)
        subprocess.run(["git", "reset", "--hard", "origin/main"],
                       cwd=REPO_DIR, check=True, capture_output=True, timeout=10)
    except subprocess.CalledProcessError as e:
        return {"ok": False, "error": f"git: {e.stderr.decode()[:200]}"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "git: timeout"}

    # 3. Pre-flight check
    pre = subprocess.run(
        [sys.executable, "-c", "import bot_handlers; import bot; import web_app; import db"],
        cwd=REPO_DIR, capture_output=True, timeout=15,
    )
    if pre.returncode != 0:
        # Откат
        subprocess.run(["git", "reset", "--hard", "HEAD~1"],
                       cwd=REPO_DIR, capture_output=True, timeout=10)
        return {"ok": False, "error": f"pre-flight: {pre.stderr.decode()[:200]}"}

    # 4. pip install если requirements изменился
    # (проверяем через git diff)
    diff = subprocess.run(
        ["git", "diff", "HEAD~1", "HEAD", "--name-only"],
        cwd=REPO_DIR, capture_output=True, timeout=10,
    )
    if b"requirements.txt" in diff.stdout:
        logger.info("requirements.txt changed, running pip install...")
        subprocess.run([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"],
                       cwd=REPO_DIR, check=False, timeout=120)

    # 5. Миграции (если есть alembic)
    if (REPO_DIR / "alembic.ini").exists():
        subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"],
                       cwd=REPO_DIR, check=False, timeout=60)

    # 6. os.execv — рестарт
    new_version = _read_version()
    logger.info("Self-update: %s → %s, restarting...", old_version, new_version)
    os.execv(sys.executable, [sys.executable, "bot.py"] + sys.argv[1:])
    # NOTREACHED


def _read_version() -> str:
    v = REPO_DIR / "VERSION"
    if v.exists():
        return v.read_text().strip()
    return "(unknown)"
```

Новый route в `web_app.py`:

```python
@app.post("/admin/deploy/update")
async def admin_deploy_update(current: WebUser = Depends(require_su)):
    from deploy import self_update
    try:
        result = await asyncio.wait_for(self_update(), timeout=180)
        if not result["ok"]:
            return JSONResponse(result, status_code=500)
        # Если дошли сюда — os.execv не сработал (не должен)
        return JSONResponse({"ok": False, "error": "execv did not replace process"})
    except asyncio.TimeoutError:
        return JSONResponse({"ok": False, "error": "timeout"}, status_code=504)
```

Новый template `templates/admin_deploy.html`:
- Текущая версия (из `VERSION`).
- Последний commit на GitHub (через GitHub API).
- Кнопка «Обновить» → POST `/admin/deploy/update` → после ответа
  polling `/health` каждые 2 сек, как только ответит — редирект на
  `/admin/system` с сообщением «Обновлено».

##### Риски Plan C и mitigations

1. **`pip install` не выполнится, если нет новых зависимостей**
   - Mitigation: проверяем `git diff requirements.txt` (см. шаг 4).
   - Если изменился — запускаем `pip install`, ждём до 120 сек.

2. **Сломанный код убивает бота после `os.execv`**
   - Mitigation: pre-flight check (шаг 3) — `python -c "import bot_handlers"`.
   - Если упал — `git reset --hard HEAD~1` + `os.execv` со старым кодом.
   - Если старый код тоже сломан (что почти невозможно) — бот упадёт,
     Docker restart policy подхватит через несколько секунд.

3. **Миграции БД ломают совместимость**
   - Mitigation: миграции запускаются ОТДЕЛЬЬНО перед `os.execv` (шаг 5).
   - Если миграция упала — не рестартуем, возвращаем ошибку SU.
   - Нужен rollback-strategy для миграций (если alembic — `alembic downgrade -1`).

4. **Конкурентные запросы на обновление**
   - Mitigation: глобальный `asyncio.Lock()` в `self_update()`.
   - Второй запрос получает 409 Conflict.

5. ** PAT в образе (если выберем git clone в Dockerfile)**
   - Mitigation: образ приватный (Bothost не публикует).
   - После настройки — сменить PAT на GitHub (старый станет недействительным,
     но образ уже собран с ним).
   - Альтернатива: использовать deploy keys (SSH) вместо PAT.

##### Что нужно от пользователя для Plan C

- ⏸ Подтвердить у Bothost support: `git clone` или `git archive` при
  webhook-деплое? Если `git clone` — минимальный Dockerfile работает.
  Если `git archive` — нужен `RUN git clone` в Dockerfile + GitHub PAT.
- ⏸ Если нужен PAT: создать GitHub Personal Access Token с правом
  `repo` (classic) или `Contents: Read` (fine-grained).
- ⏸ Подтвердить подход: pre-flight check + auto-rollback на HEAD~1
  если импорт падает.
- ⏸ Решить: используем alembic (§12 шаг 4) — если да, шаг 5 в Plan C
  запускает миграции автоматически. Если нет — пропускаем шаг 5.

##### Связь с другими задачами v5.0.0

- **§12 PostgreSQL migration**: если используем alembic, Plan C шаг 5
  автоматически накатит миграции. Без alembic — миграции вручную перед
  обновлением.
- **§«План деплоя v5.0.0»**: Plan C **дополняет** webhook-деплой, не
  заменяет. Webhook — основной способ (обычные релизы). Plan C —
  резервный (когда Bothost billing лежит, но бот работает).
- **BH-10**: версия бота + последний commit нужны для Plan C, чтобы
  показывать SU «есть что обновлять».

##### Сложность Plan C

~1.5 дня:
- Dockerfile изменения + проверка Bothost build context — 0.3 дня.
- `deploy.py` модуль (self_update с pre-flight + rollback) — 0.5 дня.
- `web_app.py` route + `templates/admin_deploy.html` — 0.3 дня.
- Тесты (mock subprocess, pre-flight failure, rollback scenarios) — 0.4 дня.

**Приоритет Plan C:** высокий. Это единственный способ обновлять бота
при лежащем Bothost billing. Делать в одном цикле с webhook-деплоем
(тема v5.0.0).

---

### 7.2. Uptime-мониторинг — exit1.dev

**Статус:** 📋 запланировано (пользователь выбрал exit1.dev).

**Сервис:** exit1.dev — облачный uptime-мониторинг, made in EU (Дания).

**Бесплатный тариф:**
- 5 мониторов, 5-минутный интервал проверок.
- 1 webhook интеграция, 10 emails в месяц.
- 1 публичная status page, 60 дней хранения истории.

**Интеграция с ботом:**
- exit1.dev пингует существующий `/health` endpoint бота.
- При падении — webhook на Telegram Bot API URL (отдельный алерт-чат SU).

**Что нужно от пользователя:**
- Узнать у Bothost публичный URL веб-панели бота.
- Зарегистрироваться на exit1.dev, создать monitors и webhook-канал.
- **Никаких изменений в коде бота не требуется** — `/health` уже работает.

**Сложность:** ~0.2 дня (только настройка на стороне exit1.dev + проверка).

**Примечание:** можно сделать в любой момент, не привязано к версии.

---

### 7.3. TOTP 2FA для SU

**Статус:** 📋 запланировано.

**Зачем:** SU-аккаунт — единственный кто может создавать/удалять других
админов. Если SU-пароль утёк — полный контроль над ботом. 2FA закрывает это.

**Реализация:**
- При логине SU → после ввода пароля → запрос TOTP-кода (6 цифр из Google
  Authenticator / Authy / 1Password).
- Секрет хранится в БД (зашифрованный) или в env `SU_TOTP_SECRET`.
- На странице настроек SU — QR-код для привязки к приложению.
- Recovery codes на случай потери телефона.

**Сложность:** ~1 день (`pyotp` + QR-генерация + recovery codes).

**Файлы:** `web_app.py`, `templates/login.html` (новый шаг 2FA), `db.py`
(новые поля у `WebUser` или отдельная таблица `totp_secrets`).

---

### 8. Миграция на собственный домен mod.degradach.ru

**Статус:** 📋 отложено.

**Контекст:** у пользователя есть домен `degradach.ru`, планируется поддомен
`mod.degradach.ru` для веб-панели бота.

**Зачем:**
- Короткий и запоминающийся URL.
- Брендинг — домен совпадает с названием чата.
- Независимость от Bothost — при миграции на другой хостинг домен сохранится.
- SSL-сертификат через Let's Encrypt / Cloudflare.

**Что нужно сделать:**
1. DNS-настройка: A-запись или CNAME на сервер Bothost.
2. На стороне Bothost: узнать, поддерживает ли Bothost custom domains.
3. SSL: Let's Encrypt (если Bothost автоматически) или Cloudflare proxy.
4. Webhook Telegram: обновить `WEBHOOK_URL` env, перерегистрировать webhook.
5. Веб-панель: проверить относительные пути в шаблонах, cookie domain.
6. Telegram Login Widget (если будет): добавить `mod.degradach.ru` в
   @BotFather → Bot Settings → Domain.
7. exit1.dev monitor: обновить URL (см. пункт 7.2).

**Сложность:** низкая, ~0.5 дня (DNS + проверка + перерегистрация webhook).

**Риски:**
- Если Bothost не поддерживает custom domains — миграция на VPS.
- Несколько минут простоя при переключении webhook (фоллбэк на long polling).

---

### 9. GitHub sync сайта правил (#11)

**Статус:** 📋 запланировано.

**Источник:** перенесён из `ROADMAP_v4.8.x.md` v4.8.2 (решение пользователя
9 августа 2026), затем из `ROADMAP_v4.9.0.md` в v5.0.0 (решение пользователя
9 августа 2026, реструктуризация roadmap'ов).

**Цель:** связать keyword-watch список с сайтом правил на GitHub.

#### Принцип работы

**Добавление фразы (через веб-панель):**
1. Модератор в форме добавления фразы выбирает секцию на сайте (дропдаун:
   «не публиковать на сайте» / «Раковые форсы» / будущие секции) + вводит фразу.
2. Бот сохраняет фразу в `keyword_watch` (БД) с пометкой `rules_section`.
3. Бот дёргает GitHub API:
   - `GET /repos/{owner}/{repo}/contents/index.html` → текущий SHA + base64-контент.
   - Декодирует, парсит через `BeautifulSoup`, находит `<details>` по тексту
     `<summary>` «Что считается раковыми форсами».
   - Добавляет `<li>{phrase}</li>` перед закрывающим `</ul>`.
   - Кодирует обратно в base64.
   - `PUT /repos/.../contents/index.html` с `{message, content, sha, branch}`.
4. При 409 Conflict — один retry: снова GET → patch → PUT. Второй 409 — ошибка
   модератору.
5. При успехе — коммит `bot: add banned phrase "{phrase}" (via web panel by @{moderator})`.
6. GitHub Pages перестраивает сайт за 2-5 минут.

**Удаление фразы:** обратная операция.

**Webhook обратно в бота (опционально, рекомендуется):**
- При пуше — бот скачивает новый `index.html`, парсит все `<details>`, сравнивает
  с БД. При расхождении — уведомление в modchat.

#### Безопасность

- **PAT (Personal Access Token):** fine-grained, scope `contents: write` только
  на этот repo. Срок — 1 год. Хранится в env `GITHUB_RULES_TOKEN`.
- **Webhook secret:** env `GITHUB_WEBHOOK_SECRET`. Без него webhook от любого
  — security risk.
- **Commit author:** «Дед Вобжак Bot <noreply@degradach.ru>».
- **Логирование:** каждый commit пишем в worklog + в modchat.

#### Изменения в БД

В модель `KeywordWatch` (уже есть в v4.8.0):
- Поле `rules_section` (str, nullable) — ID секции. `null` = не публиковать.

Возможно, отдельная таблица `RulesSection`:
- `section_id` (str, PK) — `"rakovye_forsy"`.
- `summary_text` (str) — точный текст `<summary>` для поиска блока.
- `repo_path` (str) — путь к файлу (`"index.html"`).
- `display_name` (str) — для дропдауна в веб-панели.

#### Изменения в коде

- `github_sync.py` (NEW):
  - `async def github_add_phrase_to_rules(phrase, section_id, mod_user)`.
  - `async def github_remove_phrase_from_rules(phrase, section_id)`.
  - `async def github_pull_rules()` — для webhook-синхронизации.
- `web_app.py`:
  - Расширить `POST /admin/keywords/add` полем `rules_section`.
  - (Опционально) `POST /api/github/webhook` — приём push event'ов.
- `templates/admin_keywords.html` — дропдаун «Опубликовать на сайте правил».
- `db.py` — поле `rules_section` (уже есть), опционально таблица `RulesSection`.

#### env vars (новые)

- `GITHUB_RULES_TOKEN` — PAT (обязательно).
- `GITHUB_RULES_REPO` — `{owner}/{repo}`.
- `GITHUB_RULES_BRANCH` — `main` (default).
- `GITHUB_RULES_PATH` — `index.html` (default).
- `GITHUB_WEBHOOK_SECRET` (опционально, рекомендуется).

#### Тесты

`scripts/test_v500_github_rules_sync.py`:
- Mock GitHub API: GET возвращает тестовый HTML + SHA, PUT принимает.
- Добавление фразы в существующую секцию.
- Удаление фразы.
- 409 Conflict → retry → успех.
- 409 Conflict → retry → второй 409 → ошибка модератору.
- HTML edge cases: пустой `<ul>`, `<details>` без `<summary>`.
- Webhook: push event с валидной подписью → синхронизация БД.
- Webhook: push event с неверной подписью → 403.
- Фраза с `rules_section=null` → на сайт не идёт.
- HTML formatting preserved (BeautifulSoup не переформатирует весь файл).

#### Сложность

~2 дня.

#### Что нужно от пользователя (настройка, не обсуждение)

- `GITHUB_RULES_TOKEN` — fine-grained PAT, scope `contents:write` на repo.
- `GITHUB_RULES_REPO` — например `pepegovich/degradach-rules`.
- `GITHUB_RULES_BRANCH` — `main` или `master`.
- `GITHUB_WEBHOOK_SECRET` (опционально).

---

<!-- Сюда дописывать новые идеи для v5.0.0 после обсуждения деплоя и завершения
     v4.9.0. Пока НЕ заполнять. -->

---

### 10. Импорт участников и банов из Telegram через userbot — ⚠️ ПОДУМАТЬ НАД РЕАЛИЗАЦИЕЙ

**Статус:** 🤔 **ПОДУМАТЬ НАД РЕАЛИЗАЦИЕЙ** — задача-кандидат на v5.0.0,
но архитектура требует проработки. Зарегистрирована 11 августа 2026 после
обсуждения с пользователем.

**Источник:** вопрос пользователя, 11 августа 2026:
> «А как думаешь, сложно будет просканировать всех пользователей чата и
> занести их в БД? И еще бы хотелось просканировать забаненных и так же
> внести их в БД (чтобы была возможность разбанить через веб-панель).
> Сложно такое реализовать и какие права нужны боту?»

**Кратко:** сейчас таблица `users` копится инкрементально — только те, кто
написал сообщение или получил санкцию от наших модераторов. Юзеры, которые
молчат или были забанены через UI Telegram другим админом, в БД отсутствуют.
Из-за этого `/admin/bans` показывает только наши баны, а в `users` нет
большой части аудитории чата.

#### Почему НЕ Bot API

Telegram Bot API не даёт нужных методов:
- `get_chat_member(chat_id, user_id)` — один конкретный юзер по ID.
- `get_chat_administrators(chat_id)` — только список админов.
- `get_chat_member_count(chat_id)` — только счётчик.

**`get_chat_members` (получить всех участников чата) и `get_banned_users`
(список банов) — в Bot API НЕ СУЩЕСТВУЕТ.** Это осознанное ограничение
Telegram — боты не могут массово сканировать чаты.

#### Решение — userbot (Pyrogram или Telethon)

Второй клиент под обычным Telegram-аккаунтом (не ботом), который админ
в целевом чате. Использует MTProto — без ограничений Bot API.

**Что даёт Pyrogram:**
- `get_chat_members(chat_id)` — все участники супергруппы (постранично,
  ~200 за запрос).
- `get_chat_members(chat_id, filter=ChatMembersFilter.BANNED)` — список
  забаненных.
- `get_chat_members(chat_id, filter=ChatMembersFilter.RESTRICTED)` —
  список замьюченных.

**Оценка скорости:** для чата ~19k участников → ~95 запросов, при лимите
~30 req/sec → **3-5 минут** полного скана. Для банов — быстрее, ~1-2 минуты.

#### Что нужно для реализации

| Что | Где взять | Сложность |
|-----|-----------|-----------|
| `api_id` + `api_hash` | https://my.telegram.org → API development tools (бесплатно, 5 мин) | тривиально |
| Session string | Один раз залогиниться аккаунтом-админом, сохранить сессию | 5 мин |
| Аккаунт-админ в чате | Любой из админов Бабая, с правом `can_restrict_members` | уже есть |
| Pyrogram | `pip install pyrogram tgcrypto` (~10MB) | тривиально |
| Backfill-скрипт | ~150-200 строк Python | ~1-2 дня |

#### Права

- **Бот:** уже админ с `can_restrict_members` — для разбана через веб-панель
  хватит (он уже умеет `unban_chat_member` через `/api/unban` из v4.8.1).
- **Userbot-аккаунт:** должен быть админом в целевом чате с
  `can_restrict_members` — иначе Telegram не отдаст список банов (это
  приватная информация).

#### Сложность

| Часть | Оценка |
|-------|--------|
| Pyrogram setup + session string | ~0.3 дня |
| Backfill-скрипт (участники + баны) | ~0.7 дня |
| Edge cases (rate limits, retries, частичные сбои, дубликаты) | ~0.5 дня |
| Интеграция в веб-панель (кнопка «Import bans from Telegram» в `/admin/bans`) | ~0.5 дня |
| Тесты | ~0.5 дня |
| **Итого** | **~2.5 дня** |

#### Почему v5.0.0, не v4.9.0

По правилам в `ROADMAP_v4.9.0.md`:
> **Что НЕ подходит для v4.9.0 (идёт в v5.0.0):** Интеграции, требующие
> env-секретов от пользователя.

`api_id` + `api_hash` + session string = явные env-секреты. Плюс Pyrogram
— новая асинхронная библиотека в одном контейнере с aiogram, потенциально
конфликтует по event loop. Это архитектурная задача, не патч-уровень.

#### Архитектурные вопросы — НУЖНО ПОДУМАТЬ

**1. Continuous или one-shot?**

- **Вариант A. One-time backfill (простой):**
  - Запускаем скрипт один раз, DB заполняется.
  - Бот продолжает инкрементально копить новых юзеров через `_upsert_user`.
  - Баны через `!ban` от наших модераторов логируются автоматически.
  - **Проблема:** баны, которые другие админы ставят через UI Telegram,
    не попадают в DB. Если это критично — нужен вариант B.

- **Вариант B. Periodic sync (каждую неделю):**
  - Userbot-клиент живёт в том же контейнере, раз в неделю запускает
    `get_chat_members(filter=BANNED)`.
  - Diff с таблицей `punishments` → новые баны добавляются, снятые —
    помечаются `is_revoked=True`.
  - +~50MB RAM для второго клиента, +~200ms CPU на каждый sync.

- **Вариант C. Continuous (слушает события):**
  - Userbot подписывается на `ChatBanned` события через `updates`.
  - Мгновенная синхронизация, но самый сложный в поддержке (handles для
    всех edge cases, реконнектов и т.д.).
  - Не рекомендуется — overhead не стоит того.

**2. Один контейнер или два?**

- **В одном контейнере с ботом:** проще деплой, но риски event loop
  конфликтов между aiogram и pyrogram. Обе библиотеки async, обе
  используют `asyncio.run()` — надо проверить совместимость.
- **В отдельном контейнере/процессе:** чище архитектура, но Bothost
  тариф Pro позволяет только один контейнер на бот. Придётся либо
  запускать скрипт по cron через `subprocess`, либо делать отдельный
  "sidecar" бот-аккаунт (что сложнее).

**3. Как хранить баны «не от наших модераторов»?**

Сейчас `Punishment.mod_id` — `NOT NULL` (ForeignKey на `moderators`).
Баны от Telegram UI не имеют модератора в нашей БД. Варианты:
- Использовать системный `mod_id=0` (но это нарушит FK).
- Создать специальную запись `Moderator(mod_id=0, username='telegram_ui')`.
- Сделать `mod_id` nullable (миграция схемы, рискованно).
- Использовать `added_via='telegram_import'` поле (добавить колонку
  в `Punishment`).

**4. Что показывать в веб-панели?**

- Баны от userbot-импорта должны быть помечены в `/admin/bans`
  (бейдж «Telegram import» или «External ban»).
- Кнопка «Unban» должна работать (вызывает `unban_chat_member` через
  бота — это уже работает в v4.8.1).
- При unban'е внешнего бана — создавать новую `Punishment(action_type='unban')`
  запись с `mod_id=<web user>` (как уже делается в `/api/unban`).

**5. Приватность и лимиты Telegram**

- Pyrogram использует session string — если она утечёт, злоумышленник
  получит полный доступ к аккаунту-админу. Хранить только в env var,
  никогда не логировать.
- Telegram может забанить аккаунт за «подозрительную активность» если
  сканить слишком часто. Рекомендация: не чаще раза в сутки для полного
  скана, или раз в неделю для diff-sync.
- 19k участников — ок. Если чат вырастет до 100k+, скан займёт ~30 мин,
  потребуется chunking и retry logic.

**6. Что делать с юзерами, которые уже были в БД, но сменили username?**

- `user_id` — immutable, первичный ключ.
- `username`, `first_name`, `last_name` — обновляем через `_upsert_user`
  при каждом скане (как уже делается для активных юзеров).
- Не затираем `last_seen` — оставляем последнее реальное сообщение.

#### Файлы, затронутые (предварительно)

- `userbot_client.py` (NEW) — Pyrogram client, init from session string.
- `backfill_users.py` (NEW) — скрипт: scan members → bulk upsert в `users`.
- `backfill_bans.py` (NEW) — скрипт: scan banned → upsert в `users` +
  создать `Punishment(action_type='ban', is_revoked=False, added_via='telegram_import')`.
- `db.py` — миграция: добавить колонку `added_via` в `Punishment`
  (или системный Moderator(mod_id=0, username='telegram_ui')).
- `web_app.py` — новый endpoint `/admin/bans/import` (SU-only) — запускает
  backfill, показывает прогресс, результат.
- `templates/admin_bans.html` — бейдж «Telegram import» для внешних банов,
  кнопка «Import from Telegram».
- `requirements.txt` — `pyrogram`, `tgcrypto`.
- `scripts/test_v500_userbot_import.py` (NEW) — тесты.
- `.env.example` — `TG_API_ID`, `TG_API_HASH`, `TG_SESSION_STRING`.

#### Что нужно от пользователя

- **Подтвердить** выбор варианта (A one-shot / B periodic / C continuous).
- **Предоставить** `api_id`, `api_hash` (https://my.telegram.org).
- **Предоставить** session string (один раз залогиниться, сохранить).
- **Уточнить** какой аккаунт-админ будет userbot'ом (личный аккаунт
  Бабая? отдельный аккаунт?).
- **Обсудить** вопросы 1-6 выше (особенно #2 — один контейнер или два,
  и #3 — как хранить баны без модератора).

#### Сложность

~2.5 дня (минимальная оценка, без учёта архитектурных решений). После
ответов на вопросы 1-6 — может вырасти до 3-4 дней.

**Приоритет:** средний. Зависит от того, насколько критична полнота
данных в `/admin/bans` для работы модераторов. Сейчас (v4.8.x) банов
от других админов в БД нет, но это не блокирует работу — модераторы
видят их в UI Telegram напрямую.

---

### 11. Slow_mode bug — диагностика и фикс (перенесено из плана v4.8.3.2)

**Статус:** 📋 перенесено в v5.0.0 (решение пользователя 12 августа 2026).

**Источник:** логи продакшена от 2026-08-10 и 2026-08-11. Полное
расследование зафиксировано в `worklog.md` Task ID `v4.8.3.2-investigation`.

**Симптомы:**

- Бот меняет **другие права** (`can_send_messages`, `can_send_media_messages`,
  и т.д.) во **всех** чатах успешно.
- `setChatSlowModeDelay` НЕ меняется **ни в одном** чате.
- В логах:
  ```
  2026-08-10 20:00:28 │ shadow_logger │ WARNING │ Night mode: set_chat_slow_mode_delay(30s) failed for chat -1001342267142: Telegram server says - Not Found
  2026-08-10 20:00:28 │ shadow_logger │ WARNING │ Night mode: set_chat_slow_mode_delay(30s) failed for chat -1002476691161: Telegram server says - Not Found
  ```
  При этом в обоих чатах `Night mode ON ... perms applied, snapshot saved` —
  то есть права применились, snapshot сохранён, упал **только slow_mode**.
- `!alarm on` тоже не меняет slow_mode (там `_ALARM_SLOW_MODE_DELAY = 30`).
- Версия в проде: 4.8.3 (4.8.3.1 не задеплоен из-за техработ Bothost).

**Расследование — что проверено и подтверждено (не причина):**

1. **Кастомный класс `SetChatSlowModeDelay`** (`bot_handlers.py:150-163`)
   корректен: `__api_method__ = "setChatSlowModeDelay"`, наследник
   `TelegramMethod[bool]`, поля `chat_id`/`slow_mode_delay`.
2. **aiogram 3.30** не имеет встроенного `SetChatSlowModeDelay` (ImportError)
   — используется только наш кастомный класс.
3. **URL формируется правильно:**
   `https://api.telegram.org/bot<token>/setChatSlowModeDelay`
4. **Form fields корректны:** `chat_id=-1001342267142`, `slow_mode_delay=30`.
5. **Endpoint существует на Telegram** — проверено с фейк-токеном: даже
   несуществующий `foobarbaz` возвращает 401 Unauthorized, значит Telegram
   проверяет токен РАНЬШЕ существования метода. С валидным токеном на
   существующий endpoint Telegram вернёт 200/400/403, но **не 404**.
6. **Права `can_restrict_members` есть** — `setChatPermissions` работает
   (требует тех же прав).
7. **Код НЕ переопределяет** `bot.session.api`, `proxy`, `trust_env`,
   `BOT_API_URL`. `AiohttpSession` создаёт `ClientSession` без `trust_env`
   → env-переменные `HTTPS_PROXY` не подхватываются.
8. **Локальный тест** (`scripts/test_slowmode_url.py`, можно запустить из
   песочницы): с фейк-токеном на `api.telegram.org` — `TelegramUnauthorizedError`
   (ожидаемо). Значит wrapper и aiogram-интеграция работают корректно.

**Гипотеза H6 (инфраструктурная — основная):**

С валидным токеном Telegram **физически не может** вернуть 404 на
существующий endpoint `/bot<token>/setChatSlowModeDelay`. Значит, "Not Found"
формируется **не Telegram**, а чем-то между ботом и Telegram в
инфраструктуре Bothost:

- **H6a.** Local BotAPI server в Bothost с устаревшим whitelist методов
  (setChatSlowModeDelay забыт или не обновлён).
- **H6b.** Прозрачный HTTP-proxy / DPI на исходящий трафик к
  `api.telegram.org`, фильтрующий методы по имени URL (например, по
  совпадению с pattern'ом "slowmode" / "restrict").
- **H6c.** Local BotAPI server на очень старой версии (< 2019, когда
  добавили setChatSlowModeDelay). Маловероятно.

**План v5.0.0 (этапы):**

#### Этап 1. Диагностическое логирование (НЕ меняет поведение)

В `bot.py` (`_enter_night_mode` ~L456-470, `_restore_day_state` ~L734-758)
и `bot_handlers.py` (`handle_alarm_command` ~L4581-4590) обернуть вызов
`SetChatSlowModeDelay` так, чтобы при ошибке логировался:

- URL запроса (`bot.session.api.api_url(token, method)`)
- Полный HTTP-ответ: `status_code`, `headers`, `body`

По заголовку `Server:` сразу видно, кто отвечает:
- `Server: nginx` → Telegram (значит гипотеза H6 неверна, ищем дальше)
- Любой другой сервер → local BotAPI server / proxy Bothost

Реализация: перехват через `try/except TelegramAPIError`, затем повторный
raw-запрос через `aiohttp` на тот же URL и логирование ответа.

Новый helper: `bot_handlers.py:_log_slow_mode_http_details()` — общий
helper для диагностики HTTP-ответа при ошибке `setChatSlowModeDelay`.

#### Этап 2. Альтернативный способ (если H6 подтвердится)

Если Bothost local BotAPI server не поддерживает `setChatSlowModeDelay`:

- **(a) MTProto через Pyrogram/Telethon** — overkill, не рекомендуется.
  Поддерживает ВСЕ методы, но требует отдельной сессии, +1 зависимость,
  усложнение деплоя.
- **(b) Попросить Bothost обновить local BotAPI server** — предпочтительно,
  но зависит от реакции support'а.
- **(c) Использовать официальный cloud BotAPI** (если Bothost позволяет
  настроить `BOT_API_URL` env) — но это может ломать другие вещи
  (rate limits cloud BotAPI строже local).

Без подтверждения H6 этап 2 не начинается.

#### Этап 3. Запрос в Bothost support

- «Используете ли вы local BotAPI server? Какая версия?»
- «Есть ли ограничения на исходящие HTTPS-запросы к `api.telegram.org`?»
- «Поддерживает ли ваша инфраструктура метод `setChatSlowModeDelay`?»
- «Прозрачный proxy или DPI на исходящий трафик?»

#### Файлы, затронутые (предварительно, для этапа 1)

- `bot.py` — diagnostic logging в `_enter_night_mode`, `_restore_day_state`.
- `bot_handlers.py` — diagnostic logging в `handle_alarm_command`, новый
  helper `_log_slow_mode_http_details()`.
- `scripts/test_v500_slow_mode_diagnostic.py` (NEW) — проверка что
  diagnostic-логирование работает.

#### Сложность

~0.5 дня на этап 1 (диагностические логи). Этап 2 — зависит от того, что
найдём на этапе 1 и что ответит Bothost.

**Приоритет:** средний. Баг не критичный (slow_mode — удобство, не
безопасность), но неприятный (night mode без slow_mode теряет смысл для
режима «медленный чат ночью»). После миграции на PostgreSQL (§12) и
webhook-деплой — разберёмся.

**Связь с другими задачами v5.0.0:**
- §0 B2-бэкап — тоже инфраструктурная задача, делать вместе.
- §12 Миграция SQLite → PostgreSQL — отдельная задача, но если Bothost
  действительно использует local BotAPI server, переход на managed
  PostgreSQL позволит лучше понять архитектуру хостинга (одна инфра
  задача → общий контекст).

---

### 12. Миграция SQLite → PostgreSQL (managed Bothost DB)

**Статус:** 📋 запланировано (добавлено 12 августа 2026).

**Источник:** решение пользователя 12 августа 2026 — перенести БД из
Docker-контейнера (`/app/data/bot.db` SQLite) на managed Bothost PostgreSQL.

> ⚠️ **БЕЗОПАСНОСТЬ КРЕДИТОВ:** connection string ниже содержит реальный
> production-пароль. НЕ коммитить roadmap с этим паролем в публичный
> репозиторий. После настройки env на Bothost — сменить пароль в Bothost
> panel. В репо класть только `DATABASE_URL=postgresql+asyncpg://...`
> плейсхолдер (см. `.env.example`).

**Цель:** отделить БД от Docker-контейнера. Преимущества:

- БД переживает удаление/пересоздание контейнера (webhook-деплой v5.0.0).
- Резервное копирование через Bothost managed DB (отдельная инфра).
- Возможность подключиться к БД извне (psql, DBeaver) для отладки.
- Подготовка к стрим-боту (`ROADMAP_streambot.md` — общая PostgreSQL,
  отдельные `stream_*` таблицы).
- Снимает ограничение SQLite на одновременные writes (хотя для 1 бота
  это не критично сейчас).

**Connection string (managed Bothost PostgreSQL):**

```
postgresql://bothost_db_354b7ede1bee:J-6XwZixivVU_kbiicBQy4xxQio7UMlSuoL028ozt2c@node1.pghost.ru:15977/bothost_db_354b7ede1bee
```

- **Хост:** `node1.pghost.ru` (Bothost managed PostgreSQL node).
- **Порт:** `15977`.
- **БД:** `bothost_db_354b7ede1bee` (имя, выданное Bothost).
- **Юзер:** `bothost_db_354b7ede1bee` (совпадает с именем БД).
- **Пароль:** `J-6XwZixivVU_kbiicBQy4xxQio7UMlSuoL028ozt2c`.

**URL для SQLAlchemy async (с asyncpg):**

```
DATABASE_URL=postgresql+asyncpg://bothost_db_354b7ede1bee:J-6XwZixivVU_kbiicBQy4xxQio7UMlSuoL028ozt2c@node1.pghost.ru:15977/bothost_db_354b7ede1bee
```

> ℹ️ SQLAlchemy async требует `postgresql+asyncpg://` (не `postgresql://`).
> Драйвер `asyncpg` нужно добавить в `requirements.txt`.

#### Что делаем

##### Шаг 1. Подготовка схемы PostgreSQL

- Установить `psql` локально (или через DBeaver / pgAdmin).
- Подключиться к managed PostgreSQL:
  ```bash
  psql "postgresql://bothost_db_354b7ede1bee:J-6XwZixivVU_kbiicBQy4xxQio7UMlSuoL028ozt2c@node1.pghost.ru:15977/bothost_db_354b7ede1bee"
  ```
- Проверить, что БД пустая (Bothost создаёт БД пустой):
  ```sql
  \dt
  SELECT current_database();
  ```
- Если есть остаточные таблицы — очистить (ОСТОРОЖНО: только если БД новая).

##### Шаг 2. Миграция данных из SQLite

- Скачать текущий `bot.db` из Bothost file manager (или через `bot.py`
  endpoint выгрузки).
- Написать скрипт `scripts/migrate_sqlite_to_postgres.py`:
  - Читает все таблицы из `bot.db` через `sqlite3`.
  - Пишет в PostgreSQL через `asyncpg` или `psycopg2`.
  - Таблицы: `users`, `moderators`, `punishments`, `chat_admins`,
    `chat_settings`, `permission_presets`, `web_users`, `keyword_watch`,
    `link_allowlist`, `banned_sticker_packs`.
  - Сохраняет все существующие записи (id, FK, timestamps).
- Прогнать на копии `bot.db` локально, проверить целостность (row count
  per table должен совпадать).

##### Шаг 3. Изменение кода

- `requirements.txt`: добавить `asyncpg==0.30.0` (или новее).
- `db.py`: изменить `create_async_engine` — читать `DATABASE_URL` из env.
  Если env не задан — fallback на SQLite (для dev/тестов):
  ```python
  DATABASE_URL = os.getenv("DATABASE_URL") or "sqlite+aiosqlite:///app/data/bot.db"
  engine = create_async_engine(DATABASE_URL, echo=False, ...)
  ```
- **SQL-диалектные отличия** (главное, что меняется при миграции SQLite → PostgreSQL):
  - `INTEGER PRIMARY KEY AUTOINCREMENT` → `SERIAL` / `BIGSERIAL` (или
    `IDENTITY` для PostgreSQL 10+). SQLAlchemy `Integer, primary_key=True`
    абстрагирует — но нужно проверить, что нет сырых SQL с `AUTOINCREMENT`.
  - `TEXT` без длины — работает в обоих, ОК.
  - `DATETIME` — SQLAlchemy `DateTime(timezone=True)` для PostgreSQL
    (с таймзоной), в SQLite хранится как TEXT. Проверить миграцию
    timestamp'ов.
  - `JSON` поля (`night_mode_saved_permissions`, `alarm_saved_permissions`,
    `day_permissions`) — SQLAlchemy `JSON` работает в обоих, но PostgreSQL
    использует нативный `JSONB` (быстрее). Можно оставить `JSON` для
    совместимости, либо мигрировать на `JSONB`.
  - `BOOLEAN` — в SQLite хранится как 0/1, в PostgreSQL как настоящий
    bool. SQLAlchemy абстрагирует.
  - Case-sensitivity: PostgreSQL fold'ит незакавыченные идентификаторы в
    lowercase. SQLAlchemy генерирует закавыченные — ОК.
- **Проверить сырой SQL** в коде (grep `text(` / `execute(` / `RAW SQL`):
  все ли запросы dialect-agnostic? Если есть SQLite-specific (например,
  `strftime`, `datetime()`, `||` для конкатенации строк) — заменить на
  SQLAlchemy cross-dialect выражения.

##### Шаг 4. Alembic миграции (опционально, но рекомендуется)

- Сейчас схема SQLite эволюционирует «в коде» через `ALTER TABLE` вручную
  (см. «Идеи для обсуждения» в плане деплоя v5.0.0).
- После перехода на PostgreSQL — завести Alembic для управления схемой:
  - `alembic init migrations`
  - Создать baseline-миграцию из текущей схемы `db.py`.
  - Все будущие изменения схемы — через `alembic revision --autogenerate`.
- Это закроет вопрос «как накатить схему на новой БД» — `alembic upgrade head`.
- **Сложность:** ~0.5 дня на настройку + обучение процессу.

##### Шаг 5. Настройка env на Bothost

- В Bothost panel управления ботом → env vars → добавить:
  ```
  DATABASE_URL=postgresql+asyncpg://bothost_db_354b7ede1bee:J-6XwZixivVU_kbiicBQy4xxQio7UMlSuoL028ozt2c@node1.pghost.ru:15977/bothost_db_354b7ede1bee
  ```
- Убедиться, что контейнер Bothost имеет сетевой доступ к
  `node1.pghost.ru:15977` (Bothost managed DB должен быть доступен по
  умолчанию, но проверить).
- Оставить fallback SQLite env закомментированным в `.env.example` для
  dev-окружения.

##### Шаг 6. Тестирование и деплой

- Локально (dev): прогнать тесты с `DATABASE_URL=postgresql+asyncpg://...`
  against тестовой PostgreSQL БД (можно поднять локальный `docker run
  postgres:16`).
- Прогнать регрессию на всех существующих тестах (`scripts/test_v*.py`).
- На Bothost: задеплоить бота с новым `DATABASE_URL`, проверить:
  - Бот стартует без ошибок.
  - Лог содержит: `Database: postgresql+asyncpg://...@node1.pghost.ru:...`
    (новая строка в startup).
  - Команды работают: `!ban`, `!warn`, `!mute`, `!alarm`, `/admin/bans`,
    `/admin/users`, и т.д.
  - В БД появляются новые записи (проверить через `psql` подключение).
- Откат: если что-то сломалось — переключить `DATABASE_URL` обратно на
  SQLite, перезапустить контейнер.

#### Файлы, затронутые

- `db.py` — `create_async_engine` с env-driven URL, возможные
  dialect-agnostic адаптации.
- `requirements.txt` — добавить `asyncpg==0.30.0`.
- `.env.example` (NEW) — плейсхолдер `DATABASE_URL=...`.
- `bot.py` — startup-лог: какая БД используется (PostgreSQL vs SQLite).
- `scripts/migrate_sqlite_to_postgres.py` (NEW) — миграция данных.
- `scripts/test_v500_postgres_compat.py` (NEW) — тесты SQL-диалектной
  совместимости (запустить against PostgreSQL).
- `migrations/` (NEW, если Alembic) — Alembic migrations directory.

#### Связь с другими задачами

- **§0 B2-бэкап:** после миграции на managed PostgreSQL — Backblaze B2
  бэкап `bot.db` становится не нужен (Bothost сам бэкапит managed DB).
  Либо, как дополнение: B2-бэкап для архивных целей (отдельная
  ежедневная копия). Решить после миграции.
- **§11 Slow_mode bug:** если Bothost действительно использует local
  BotAPI server / DPI (гипотеза H6), миграция на managed PostgreSQL
  косвенно помогает диагностировать — это даст больше понимания, что
  Bothost действительно делает с трафиком.
- **Stream-bot (`ROADMAP_streambot.md`):** отдельный проект будет
  использовать ту же managed PostgreSQL с префиксом `stream_*` для своих
  таблиц. Миграция основного бота на PostgreSQL — prerequisite для
  стрим-бота.

#### Что нужно от пользователя

- ✅ Connection string предоставлен (см. выше).
- ⏸ Подтвердить: БД `bothost_db_354b7ede1bee` уже создана на Bothost
  managed PostgreSQL (через Bothost panel → Databases → PostgreSQL)?
- ⏸ Подтвердить: сетевой доступ от Docker-контейнера бота к
  `node1.pghost.ru:15977` работает (можно проверить через `psql` из
  контейнера, если он там установлен, или после первого деплоя с
  новым env).
- ⏸ Решить: оставляем B2-бэкап (§0) как дополнение к managed DB backups,
  или удаляем эту задачу из v5.0.0?
- ⏸ Решить: заводим Alembic (шаг 4) или оставляем «в коде» как сейчас?

#### Сложность

~2 дня (без Alembic) / ~2.5 дня (с Alembic):
- Шаг 1 (подготовка PostgreSQL) — 0.5 дня.
- Шаг 2 (миграция данных) — 0.5 дня.
- Шаг 3 (изменение кода) — 0.5 дня.
- Шаг 4 (Alembic, опционально) — 0.5 дня.
- Шаг 5 (env на Bothost) — 0.2 дня.
- Шаг 6 (тесты и деплой) — 0.3 дня.

**Приоритет:** высокий. Связан с темой v5.0.0 «облегчение деплоя»:
managed DB = одна точка отказа меньше, проще webhook-деплой (БД не
зависит от пересоздания контейнера). Prerequisite для стрим-бота.

---

---

## Описание бота (для справки)

**Дедушка Вобжак** — Telegram-модератор-бот для чата Бабая. Бот + веб-панель
в одном контейнере. Хостится на Bothost.

### Архитектура (актуально после v4.8.x)
- **Точка входа:** `bot.py` — FastAPI + Aiogram.
- **Бот:** `bot_handlers.py` — обработка команд и сообщений.
- **Веб-панель:** `web_app.py` (FastAPI) + Jinja2-шаблоны.
- **БД:** SQLite (`db.py`), SQLAlchemy async.
- **Модели:** `User`, `Moderator`, `Punishment`, `ChatAdmin`, `ChatSettings`,
  `PermissionPreset`, `WebUser`, `KeywordWatch`, `LinkAllowlist`,
  `BannedStickerPack`, `Base` (после v4.8.1 — без `WordFilter`).
- **Модули v4.8.x:** `chat_modes.py` (унификация режимов), `modchat.py`
  (modchat + keyword-watch), `backup.py` (после v4.8.3).

### Стек
- Python 3.11+, aiogram 3.x, FastAPI, SQLAlchemy 2.x (async), Jinja2.
- Хостинг: Bothost (Docker-контейнер, agent API на `http://agent:8000`).

### Кодстайл
См. `ROADMAP_v4.8.0.md` раздел «Кодстайл» — без изменений.

### Версионирование
Формат: `v{MAJOR}.{MINOR}.{PATCH}`. v5.0.0 — MAJOR bump после v4.9.x.
После v5.0.0 патчи идут v5.0.1 → v5.0.2 → ..., потом v5.1.0.

---

## Деплой v5.0.0

**См. раздел «План деплоя v5.0.0 (детальный)» выше** — это и есть формат
деплоя v5.0.0. Краткая сводка:

- **Механизм:** GitHub webhook → Bothost → авто-сборка Docker-образа → рестарт.
- **Триггер:** `release.published` на GitHub (рекомендация, ждет подтверждения).
- **Архивы `ded-vobzhak-5.0.0*.zip`:** собираться **не будут** — Bothost
  пересобирает сам, zip-доставка больше не нужна. Старые архивы (до v5.0.0)
  остаются в `/home/z/my-project/download/` как бэкап.
- **Changelog `ded-vobzhak-5.0.0_CHANGES.md`:** сохраняется, кладётся в
  `/home/z/my-project/download/` (история релизов вне репо).
- **Version bump:** обновить `VERSION`-файл в репо одним commit'ом. Не хардкодить
  в `web_app.py`.

В `web_app.py`:
```python
# Читается из VERSION-файла, не хардкод
APP_VERSION = _read_version()  # → "v5.0.0" после релиза
```

**Миграция на новый формат деплоя:** см. «Шаг 7. Миграция с zip-деплоя на
webhook-деплой» выше. Начинается за 1-2 дня до v5.0.0 (подготовка репо),
завершается в день v5.0.0 (включение webhook + первый релиз).

---

## Прочее (плейсхолдер для будущих идей)

<!-- Сюда дописывать новые идеи после обсуждения с пользователем и модераторами.
     Пока НЕ заполнять. -->
