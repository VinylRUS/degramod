# Roadmap — Stream Bot (рабочее название «Дедушка Стримов»)

> **Отдельный проект**, развиваемый параллельно с основным ботом
> «Дедушка Вобжак». Стартовая версия: **v0.0.1** (skeleton + planning).
>
> **Родительский проект:** `ROADMAP_v5.0.0.md` (основной бот).
> Стрим-интеграция изначально была пунктом §2 в roadmap v5.0.0, вынесена
> в отдельный проект решением пользователя 12 августа 2026 — чтобы
> развивать независимо, в отдельном чате разработки, не смешивая с
> циклом патчей основного бота.
>
> **Принцип:** стрим-бот — **только логирование**. Никаких команд из TG,
> никакой двусторонней синхронизации банов, никакого `/link`. Только
> чтение событий с Twitch / GoodGame и красивое отображение в веб-панели
> + опционально логирование в отдельный TG-чат.

---

## Контекст

Бабай стримит на Twitch и GoodGame. Сейчас баны со стрима логируются
вручную в Discord-канале `#BAN` (1600+ сообщений) — модераторы делают
скриншоты, ищут по датам, ведут «карточки юзеров» руками. Хочется
автоматизировать: бот сам слушает события банов/мутов со стрим-платформ,
ведёт базу, показывает в веб-панели в виде карточек с историей.

**Ключевое требование пользователя (12 августа 2026):**

> Переучивать модераторов стрима у меня нет никакого желания.

Поэтому:
- ✅ Бот только читает события со стрим-платформ
- ❌ Бот не даёт команд модераторам стрима
- ❌ Бот не умеет банить на стриме
- ❌ Бот не требует от юзеров `/link`-привязки аккаунтов

---

## Архитектурные решения (фиксированные)

### 1. Отдельный бот, отдельный проект, отдельный чат разработки

- **Отдельный BOT_TOKEN** от @BotFather (имя «Дедушка Стримов» или类似 —
  обсудить). Нужен минимальный TG-интерфейс: `/start`, `/help`,
  `/streamlogin` (для выдачи ссылки на веб-панель), уведомления
  стрим-модераторам.
- **Отдельный репозиторий / проект**. Свой `Dockerfile`, свой
  `requirements.txt`, своя `web_app.py`. Деплоится отдельным контейнером
  на Bothost.
- **Отдельный чат разработки** (с другим AI-ассистентом или в другой
  сессии). Синхронизация с основным проектом — открытый вопрос (см.
  «Открытые вопросы» в конце).

### 2. Общая PostgreSQL с основным ботом (shared DB, separate schemas)

- Bothost-managed PostgreSQL на `node1.pghost.ru:15441+`.
- База: использовать существующую `degrabans` (Bothost уже создал) или
  создать новую `streambot` — уточнить (см. открытые вопросы).
- **Схема разделения:** основной бот пишет в таблицы без префикса
  (`users`, `chat_admins`, `punishments`, …), стрим-бот — в таблицы с
  префиксом `stream_*` (`stream_moderators`, `stream_punishments`,
  `stream_channels`, `stream_integrations`, `stream_user_links`).
- **Read-only доступ к общим таблицам:** стрим-бот может читать
  `users` (чтобы сопоставлять twitch_username → tg_user_id если юзер
  ранее писал в TG-чат основного бота). Но НЕ пишет туда.
- **Миграции:** каждый бот управляет только своими таблицами. Никаких
  `DROP TABLE` чужих таблиц. Schema-versions отдельно.

### 3. Только логирование (read-only по стрим-платформам)

- ✅ Слушаем Twitch EventSub `user.moderator.ban` / `user.moderator.timeout`
- ✅ Слушаем GoodGame WebSocket `banned_user` / `muted_user`
- ✅ Записываем в `stream_punishments`
- ✅ Показываем в веб-панели + опционально постим в stream-log TG-чат
- ❌ Не вызываем Twitch Helix API для банов
- ❌ Не пишем в чат стрима
- ❌ Не требуем от модераторов стрима никаких действий

### 4. Разделение модераторов TG ↔ стрим

**Отдельная таблица `stream_moderators`** — не связана с `chat_admins`
основного бота. Поля:
- `id`, `tg_user_id` (TG-аккаунт стрим-модератора, для логина в веб-панель)
- `display_name`, `added_by`, `added_at`, `is_active`

**Роли в веб-панели стрим-бота** (своя система, не зависит от основного):
- `su` — супер-пользователь (ты). Полный доступ + настройка интеграций.
- `stream_admin` — может настраивать channels, stream-log chat, смотреть
  всю историю, удалять записи.
- `stream_moderator` — только просмотр ленты + фильтры.

**Навигация:** только разделы «Стрим-нарушители», «Каналы», «Интеграции»,
«Настройки». Никаких TG-чатов, TG-банов, TG-настроек. Пересечений с
основным ботом — ноль.

**Login:** отдельный от основного бота. Своя кука (`stream_session`),
свой `SESSION_SECRET`. Можно логиниться одним и тем же TG-аккаунтом в
обе панели, но сессии независимые.

### 5. Шифрование секретов в БД

OAuth-токены Twitch, API-токены GoodGame — секреты. Хранить в env нельзя
(нужно добавлять/обновлять через веб-панель без рестарта бота). Храним
в таблице `stream_integrations` в зашифрованном виде.

- **Алгоритм:** Fernet (симметричное, из `cryptography`).
- **Ключ:** `STREAM_ENCRYPT_KEY` в env (32 url-safe base64 bytes). Один
  на деплой. При смене ключа — старые секреты не расшифровать (нужно
  перенастраивать интеграции).
- **Что шифруется:** `access_token`, `refresh_token` для Twitch;
  `api_token` для GoodGame. В БД — `encrypted_secrets JSONB` (после
  шифрования — одно поле-строка).
- **Что НЕ шифруется:** `client_id`, `channel_name`, `platform_user_id`,
  даты — это метаданные, не секреты.

### 6. Stream-log TG-чат — отдельный, настраивает stream_admin/su

- Поле `stream_log_chat_id` в `stream_channels` (per-channel).
- Если поле пустое — логирование в TG отключено для этого канала.
- Формат rich-message (как в основном боте): аватар нарушителя, ник,
  платформа, тип наказания, длительность, причина, ссылка на веб-карточку.
- Постит стрим-бот в указанный чат от своего имени (нужно добавить бота
  в чат с правом постинга).

---

## Схема БД (только stream_* таблицы)

```sql
-- Стрим-модераторы (доступ к веб-панели стрим-бота)
CREATE TABLE stream_moderators (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tg_user_id      BIGINT UNIQUE NOT NULL,
    display_name    TEXT,
    role            TEXT NOT NULL DEFAULT 'stream_moderator'
                    CHECK (role IN ('su', 'stream_admin', 'stream_moderator')),
    added_by        BIGINT,  -- tg_user_id того, кто добавил
    added_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_active       BOOLEAN NOT NULL DEFAULT TRUE
);

-- Каналы стримов (один стример — одна запись)
CREATE TABLE stream_channels (
    id                      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    platform                TEXT NOT NULL CHECK (platform IN ('twitch', 'goodgame')),
    channel_name            TEXT NOT NULL,  -- ник стримера в нижнем регистре
    display_name            TEXT,           -- как показывать в UI
    platform_user_id        TEXT,           -- Twitch user_id / GG channel_id
    stream_log_chat_id      BIGINT,         -- TG-чат для логирования (NULL = off)
    is_active               BOOLEAN NOT NULL DEFAULT TRUE,
    added_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (platform, channel_name)
);

-- Интеграции (OAuth credentials по каналам)
CREATE TABLE stream_integrations (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    channel_id          BIGINT NOT NULL REFERENCES stream_channels(id) ON DELETE CASCADE,
    integration_type   TEXT NOT NULL,  -- 'twitch_eventsub', 'gg_websocket'
    client_id           TEXT,          -- метаданные, не секрет
    encrypted_secrets   JSONB NOT NULL,  -- {access_token, refresh_token, ...} после Fernet
    scopes              TEXT[],        -- для Twitch: ['moderation:read', ...]
    expires_at          TIMESTAMPTZ,   -- для Twitch OAuth refresh
    last_connected_at   TIMESTAMPTZ,
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    UNIQUE (channel_id, integration_type)
);

-- Лог наказаний со стримов
CREATE TABLE stream_punishments (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    platform            TEXT NOT NULL CHECK (platform IN ('twitch', 'goodgame')),
    channel_id          BIGINT NOT NULL REFERENCES stream_channels(id) ON DELETE CASCADE,
    platform_user_id    TEXT,          -- Twitch user_id / GG user_id нарушителя
    platform_username   TEXT,          -- ник нарушителя (на момент бана)
    platform_display_name TEXT,        -- отображаемое имя
    action_type         TEXT NOT NULL CHECK (action_type IN ('ban', 'timeout', 'unban')),
    duration_seconds    BIGINT,        -- NULL для перманентного бана; для timeout — длительность
    reason              TEXT,
    moderator_platform_id TEXT,        -- кто забанил на стриме (Twitch mod user_id)
    moderator_username  TEXT,
    raw_event           JSONB NOT NULL,  -- полный event payload для аудита
    tg_user_id          BIGINT,        -- если сопоставили через username с users-таблицей
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_stream_punishments_channel_created
    ON stream_punishments (channel_id, created_at DESC);
CREATE INDEX idx_stream_punishments_platform_user
    ON stream_punishments (platform, platform_user_id);

-- Сопоставление platform user → TG user (опциональное, только для отображения)
-- Заполняется автоматически: если platform_username совпал с User.username
-- в общей таблице users (read-only из основного бота).
CREATE TABLE stream_user_links (
    platform            TEXT NOT NULL CHECK (platform IN ('twitch', 'goodgame')),
    platform_user_id    TEXT NOT NULL,
    platform_username   TEXT NOT NULL,
    tg_user_id          BIGINT,  -- может быть NULL если не сопоставили
    first_seen_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_matched_at     TIMESTAMPTZ,
    PRIMARY KEY (platform, platform_user_id)
);
```

**Таблицы основного бота — read-only для стрим-бота:**
- `users` — для сопоставления `username` → `tg_user_id`. JOIN только по
  `LOWER(username) = LOWER(platform_username)`. Никаких INSERT/UPDATE.

---

## Этапы и версии

### v0.0.1 — Skeleton + schema + auth (MVP-0)

**Цель:** пустой каркас проекта, который деплоится на Bothost и
запускается, с рабочим login-ом в веб-панель.

**Что делаем:**

1. **Структура проекта:**
   ```
   streambot/
   ├── bot.py              # точка входа (FastAPI + минимальный aiogram для /start)
   ├── web_app.py          # веб-панель
   ├── db.py               # SQLAlchemy модели (stream_* таблицы)
   ├── stream_handlers.py  # (пусто, для v0.0.2)
   ├── integrations/
   │   ├── __init__.py
   │   ├── twitch.py       # (пусто, для v0.0.2)
   │   └── goodgame.py     # (пусто, для v0.0.4)
   ├── crypto.py           # Fernet шифрование секретов
   ├── templates/
   │   ├── base.html
   │   ├── login.html
   │   └── dashboard.html  # placeholder «нет данных»
   ├── Dockerfile
   ├── requirements.txt
   └── .env.example
   ```

2. **`db.py` — schema:** все `stream_*` таблицы выше. Alembic или
   ручные миграции через `init_db()` (как в основном боте).
   Подключение к PostgreSQL через `DATABASE_URL` env.

3. **Auth:**
   - `STREAM_ENCRYPT_KEY` env (Fernet).
   - `STREAM_SESSION_SECRET` env (для подписи кук).
   - `STREAM_ADMIN_TG_ID` env (твой TG ID — сидируется как `su` при
     первом старте).
   - Login flow: вводишь TG ID → бот присылает одноразовый login-URL в
     TG (как в основном боте `/streamlogin`) → клик → куку.
   - Роли: `su` / `stream_admin` / `stream_moderator`.
   - Навигация в `base.html`: фильтрация по ролям.

4. **Веб-панель (пустая):**
   - `/` — redirect на `/dashboard` или `/login`.
   - `/login` — форма ввода TG ID.
   - `/dashboard` — placeholder «Стрим-нарушителей пока нет».
   - `/admin/moderators` — CRUD над `stream_moderators` (только `su`).
   - `/admin/channels` — CRUD над `stream_channels` (только `su` /
     `stream_admin`). Пока без интеграций — просто метаданные.
   - `/admin/integrations` — список, кнопка «Add» (заглушка, для v0.0.2).
   - `/admin/settings` — placeholder.

5. **`crypto.py` — Fernet wrapper:**
   ```python
   def encrypt_secret(key: str, value: str) -> str: ...
   def decrypt_secret(key: str, encrypted: str) -> str: ...
   def encrypt_secrets_dict(key: str, d: dict) -> str: ...  # JSON → encrypt
   def decrypt_secrets_dict(key: str, encrypted: str) -> dict: ...
   ```

6. **Минимальный TG-интерфейс:**
   - `/start` — приветствие + ссылка на веб-панель.
   - `/streamlogin` — генерация одноразового login-URL (только для
     `stream_moderators` или `su`).
   - `/help` — краткий help.

7. **Dockerfile + requirements.txt:**
   ```
   aiogram==3.30.0
   sqlalchemy[asyncio]==2.0.36
   asyncpg==0.30.0
   psycopg[binary]==3.2.3
   fastapi==0.115.6
   uvicorn[standard]==0.34.0
   jinja2==3.1.5
   python-multipart==0.0.18
   aiohttp==3.13.3
   cryptography>=43.0.0    # Fernet
   ```

8. **Тесты (минимум):**
   - `test_v001_schema.py` — все таблицы создаются, FK работают.
   - `test_v001_crypto.py` — Fernet encrypt/decrypt round-trip, wrong
     key → ошибка, пустой value → ошибка.
   - `test_v001_auth.py` — login flow, роли, навигация.

**Сложность:** ~2 дня.

**Результат:** деплоенный на Bothost пустой стрим-бот, в который можно
логиниться, с рабочей БД и шифрованием, но без интеграций.

---

### v0.0.2 — Twitch EventSub integration

**Цель:** бот слушает баны/муты с одного Twitch-канала и пишет их в БД.

**Что делаем:**

1. **Twitch-приложение:**
   - Пользователь создаёт в Twitch Developer Console приложение, выдаёт
     Client ID + Secret.
   - В `/admin/integrations` — форма «Add Twitch integration»:
     выбираешь `stream_channels` запись, вводишь Client ID, Client Secret,
     нажимаешь «Authorize» → OAuth redirect → бот получает access_token
     + refresh_token с scopes `moderation:read`, `user:read:email`.
   - Токены шифруются Fernet, пишутся в `stream_integrations.encrypted_secrets`.

2. **EventSub webhook:**
   - Endpoint `POST /twitch/eventsub` — принимает webhook от Twitch.
   - Verifies `Twitch-Eventsub-Message-Signature` HMAC.
   - Handles `webhook_callback_verification` (challenge response).
   - Subscribes to `user.moderator.ban` and `user.moderator.timeout`
     for the configured channel.
   - На каждое событие — `INSERT INTO stream_punishments`.

3. **Refresh logic:** Twitch access_token истекает. Background task
   проверяет `stream_integrations.expires_at` за 1 час до истечения,
   делает refresh, обновляет encrypted_secrets.

4. **Веб-панель «Стрим-нарушители» (базовая):**
   - `/stream/punishments` — таблица последних 50 банов/мутов.
   - Колонки: время, платформа (Twitch badge), канал, нарушитель, тип,
     длительность, причина, модератор.
   - Без фильтров, без карточек — просто таблица.

5. **Тесты:**
   - `test_v002_twitch_oauth.py` — мок OAuth flow, проверка шифрования
     токенов, refresh.
   - `test_v002_eventsub_webhook.py` — мок webhook calls, signature
     verification, challenge, event parsing.
   - `test_v002_punishment_write.py` — запись в БД, raw_event сохраняется.

**Сложность:** ~2–3 дня (включая изучение Twitch API docs).

---

### v0.0.3 — Stream-log TG chat

**Цель:** бот постит rich-сообщения о стрим-банах в отдельный TG-чат.

**Что делаем:**

1. **Поле `stream_log_chat_id`** в `stream_channels` — настраивается в
   `/admin/channels` (только `su` / `stream_admin`).
2. **Rich-message формат** (как в основном боте, через Bot API 10.2):
   - Header: «🔒 Бан на стриме» / «⏱ Таймаут на стриме»
   - Нарушитель: ник + аватар (если удалось скачать с Twitch)
   - Канал: имя стримера
   - Тип + длительность
   - Причина
   - Модератор (кто забанил на стриме)
   - Ссылка на веб-карточку
3. **`stream_handlers.py`** — после `INSERT INTO stream_punishments`
   вызывает `_post_to_stream_log(channel_id, punishment)`.
4. **Тесты:** мок `bot.send_rich_message`, проверка формата.

**Сложность:** ~0.5–1 день.

---

### v0.0.4 — GoodGame WebSocket integration

**Цель:** бот слушает баны/муты с GoodGame-канала.

**Что делаем:**

1. **GG WebSocket client:** `wss://chat.goodgame.ru/chat/websocket`,
   подключение с токеном модератора (или анонимно, если паблик-чат).
2. **Join channel:** `{'type': 'join', 'data': {'channel_id': <id>, 'hidden': false}}`.
3. **Parse events:** `banned_user`, `muted_user` (если в GG API они
   есть — нужно проверить; документация скудная).
4. **Reconnect logic:** exponential backoff, max 5 attempts, потом
   помечаем `stream_integrations.is_active = false` + алерт в TG.
5. **Запись в `stream_punishments`** — аналогично Twitch.
6. **Веб-панель:** тот же `/stream/punishments`, но теперь с фильтром
   по платформе (Twitch / GoodGame / All).

**Сложность:** ~2 дня (GG WebSocket капризный, документации мало).

---

### v0.0.5 — Карточки юзеров + фильтры

**Цель:** веб-панель уровня Discord `#BAN` — карточки с историей.

**Что делаем:**

1. **Фильтры:** платформа, тип наказания, дата (from/to), канал,
   ник нарушителя (поиск).
2. **Пагинация:** 50 на страницу, infinite scroll или кнопки.
3. **Карточка нарушителя** (клик на ник в списке):
   - Аватар, ник, platform_user_id
   - Сопоставление с TG (если есть — `tg_user_id`, ссылка на основной
     бот? или просто mention)
   - Последние 5 наказаний (timeline)
   - Последние 5 сообщений со стрима (если удалось собрать до бана —
     опционально, может в v0.1.0)
4. **Аватарки:** Twitch Helix API `GET /users?login=...` → `profile_image_url`.
   Кешировать локально (или через `sticker_cache`-style BytesIO + base64 в БД?).

**Сложность:** ~2 дня.

---

### v0.1.0 — Beta

**Цель:** feature-complete для beta-тестирования модераторами стрима.

**Что делаем:**

1. **Полный набор тестов** — unit + integration.
2. **Документация:** README, `.env.example`, инструкция деплоя.
3. **Audit log** — кто из `stream_admin` что менял в настройках.
4. **Error monitoring** — Sentry или простой logger с алертами в TG.
5. **Backup** — `pg_dump` cron для `stream_*` таблиц (если Bothost не
   делает automatic).

**Сложность:** ~2 дня.

---

### v1.0.0 — Production

**Цель:** стабильный релиз, используемый модераторами стрима ежедневно.

- Все тесты зелёные.
- 7+ дней без критических багов в beta.
- Документация финализирована.
- Backups настроены.

---

## Связь с основным ботом («Дедушка Вобжак»)

### Что стрим-бот знает об основном боте

- **Read-only доступ к таблице `users`** (общая PostgreSQL). Используется
  для попытки сопоставить `platform_username` → `tg_user_id` если юзер
  ранее писал в TG-чат. Если сопоставили — в `stream_user_links.tg_user_id`
  пишем ID, в карточке нарушителя показываем «TG: @username».
- **Никаких других read-доступов.** `chat_admins`, `punishments`,
  `chat_settings` основного бота — не трогаем.

### Что основной бот знает о стрим-боте

- **Ничего.** Основной бот не читает `stream_*` таблицы, не знает о
  существовании стрим-бота. Это требование изоляции — если стрим-бот
  упадёт, основной продолжит работать.

### Что у них общее

- PostgreSQL-инстанс (Bothost managed).
- `STREAM_ENCRYPT_KEY` — только в env стрим-бота. Основной бот его не
  знает (и не должен).
- `SESSION_SECRET` — у каждого свой.
- BOT_TOKEN — у каждого свой.

### Возможная будущая интеграция (post-v1.0.0)

- Если захочется показывать стрим-баны в админке основного бота —
  основной бот делает read-only запрос к `stream_punishments`. Но это
  отдельная фича, не в scope v1.0.0 стрим-бота.

---

## Деплой

- Bothost: отдельный контейнер, отдельный bot_id.
- `DATABASE_URL` env = connection string из Bothost-панели PostgreSQL
  (например `postgresql://bothost_db_...:...@node1.pghost.ru:15441/degrabans`).
- `BOT_TOKEN` env = токен «Дедушки Стримов» от @BotFather.
- `STREAM_ENCRYPT_KEY` env = `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
- `STREAM_SESSION_SECRET` env = 32 random bytes hex.
- `STREAM_ADMIN_TG_ID` env = твой TG user ID (для начального `su`).
- Домен: `stream.bothost.tech` или подобный (Bothost Traefik).

---

## Открытые вопросы

1. **Синхронизация разработки с основным проектом.** Стрим-бот
   разрабатывается в отдельном чате (с другим AI-ассистентом или в
   другой сессии). Как синхронизировать:
   - Общую схему БД (чтобы один бот не сломал таблицы другого)?
   - Общую документацию (this file, ROADMAP_v5.0.0.md)?
   - Releases / changelog?
   - **Варианты:** (a) общий git-репозиторий с двумя подпапками, (b)
     отдельные репозитории + общий `shared-schema.md`, (c) вообще
     раздельно, синхронизация руками. Обсудить позже.

2. **База PostgreSQL:** использовать существующую `degrabans` или
   создать новую `streambot`? Если `degrabans` — нужно убедиться что
   основной бот туда ещё не пишет (или мигрировать его туда тоже, как
   часть ROADMAP v5.0.0 этап 0).

3. **Имя бота.** «Дедушка Стримов»? «Деграбанс» (по названию БД)? Другое?

4. **Twitch-приложение:** пользователь создаст когда подойдём к
   реализации v0.0.2. Client ID + Secret + redirect URI.

5. **GoodGame API:** документация скудная. Перед v0.0.4 — исследовать
   какие events вообще приходят через WebSocket, есть ли отдельный
   API для модераторов. Может потребоваться reverse-engineering.

6. **Backups PostgreSQL:** уточнить у Bothost делают ли они automatic
   snapshots. Если нет — добавить `pg_dump → B2` cron в v0.1.0.

7. **Avatar caching:** Twitch avatars через Helix API. Кешировать в БД
   (base64 в `stream_user_links`?) или на диске (но Bothost контейнер
   stateless). Решить в v0.0.5.

8. **Rate limits Twitch API:** EventSub имеет лимиты на количество
   subscriptions. Если стримеров много — может не хватить. Уточнить
   лимиты, заложить пагинацию подписок.

9. **GG WebSocket stability:** опыт подсказывает что GG-сокет часто
   рвётся. Нужен robust reconnect с jittered backoff. Тестировать
   долго (дни, не часы) перед v0.1.0.

10. **Web panel поддомен:** `stream.bothost.tech`? или
    `degraban.bothost.tech/stream/`? Первый вариант чище (отдельный
    FastAPI app), второй — проще с auth (можно шарить куку). Склоняюсь
    к первому.

---

## История изменений

- **12 августа 2026** — создан. Стрим-интеграция вынесена из
  `ROADMAP_v5.0.0.md` §2 в отдельный проект. Решение пользователя:
  разработка в отдельном чате, начиная с v0.0.1 (skeleton + schema).
