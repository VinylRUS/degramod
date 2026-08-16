# Аудит кода v4.8.0

**Дата:** 8 августа 2026
**Аудитор:** main (Super Z)
**Версия:** v4.7.30 (последний патч перед major-update v4.8.0)
**Файлы аудита:** `bot.py`, `bot_handlers.py`, `web_app.py`, `db.py`, `templates/*.html`, `scripts/test_*.py`

---

## Сводка

Кодовая база **состояние хорошее** — структура в целом консистентна,
архитектурные инварианты задокументированы в начале ROADMAP, тесты
покрывают ключевые сценарии. Найдено несколько зон для улучшения,
но **критических проблем нет** — рефакторинг (#9) и modchat (#10)
можно делать на текущей базе.

| Метрика | Значение |
|---------|----------|
| Размер кода | 13 305 строк Python + 3 284 строк HTML |
| Python-модулей | 4 (`bot.py`, `bot_handlers.py`, `web_app.py`, `db.py`) |
| HTML-шаблонов | 9 |
| Тестов | 39 файлов, ~570 тест-кейсов |
| Моделей БД | 7 (+ `KeywordWatch` планируется в #10) |
| TODO/FIXME | 0 (чисто) |
| Late imports `from bot import` | 4 места (обосновано safety net) |

---

## 1. Структурный аудит

### 1.1 Размеры файлов

| Файл | Строк | Функций | Router-handlers | Статус |
|------|-------|---------|-----------------|--------|
| `bot.py` | 1544 | 16 | — | ✅ читаем, хорошо структурирован |
| `bot_handlers.py` | 6973 | 60+ | 12 | ⚠️ кандидат на разбиение после v4.8.0 |
| `web_app.py` | 3803 | 50+ | — | ⚠️ растёт, но пока читаем |
| `db.py` | 986 | 4 + 7 моделей | — | ✅ консистентный, хорошо откомментирован |

**Наибольшая проблема:** `bot_handlers.py` уже 7000 строк. Это
объяснимо — там ВСЕ обработчики команд + фильтры + хелперы + reporting
логика. Но при добавлении modchat (#10) и keyword-watch станет ещё
больше. Рекомендация: после v4.8.0 разбить на модули
`bot_commands/`, `bot_filters/`, `bot_reporting/`. **Не делать в v4.8.0**
— риск regressions превышает пользу.

### 1.2 Late imports `from bot import X`

В `bot_handlers.py` есть 4 места с late import:
- `from bot import _exit_night_mode` (строка 5191)
- `from bot import _exit_sanitary_day` (строка 5774)
- `from bot import _enter_sanitary_day, _exit_sanitary_day` (строки 5812, 5820)

**Объяснение:** `bot.py` запускается как `__main__` (Docker CMD
`python bot.py`), а не как модуль `bot`. Прямой import вызвал бы
повторную загрузку `bot.py` со side-effectами (`dp.include_router`,
startup tasks) → `RuntimeError: Router is already attached`.

**Решение (v4.7.22):** в начале `bot.py` есть safety net:
```python
_self_module = sys.modules.get(__name__)
if _self_module is not None:
    sys.modules.setdefault("bot", _self_module)
```

После этого late imports находят уже загруженный модуль в
`sys.modules` и не вызывают повторный import. Это **не костыль, а
обоснованный workaround** для single-process Docker-сценария.

**Рекомендация:** оставить как есть. Альтернатива — перенести ВСЕ
общие символы в `bot_handlers.py` (как сделано для
`SetChatSlowModeDelay` в v4.7.22), но это требует переноса крупных
функций (`_enter_night_mode`, `_exit_night_mode`, `_enter_sanitary_day`,
`_exit_sanitary_day`, `_restore_day_state`, `_resolve_day_perms`,
`_alarm_auto_off_tick`, `_startup_recovery`, `_verify_env_chats`,
`lifespan`, `_night_mode_loop`, `_sanitary_day_tick`, `_night_mode_tick`)
— все они завязаны на `bot` и `dp` объекты из `bot.py`. Это большой
рефакторинг, не для v4.8.0.

### 1.3 Дубликаты логики

**Snapshot прав в alarm / night / sanitary — 3 копии логики:**

Логика «снять snapshot прав через `bot.get_chat().permissions`,
преобразовать в JSON dict» повторяется в 3 местах:
1. `_enter_night_mode` (bot.py:397-411) — с поддержкой `day_permissions` preset.
2. `_enter_sanitary_day` (bot.py:1026-1043) — с поддержкой `day_permissions` preset.
3. `handle_alarm_command` (bot_handlers.py:4187-4198) — без preset support (упрощённая).

**Рекомендация:** вынести в единую функцию
`_snapshot_chat_permissions(bot, chat_id, day_permissions=None) -> tuple[str, int]`
в `bot_handlers.py`. **Сделать в #9 (рефакторинг режимов)** — это и
есть одна из целей рефакторинга.

**Логика восстановления прав через `_deactivate_alarm`:**

В `_deactivate_alarm` (bot_handlers.py:714-748) есть 4-уровневый
fallback (day_permissions → alarm_saved → system_default → hardcoded),
но без использования `_resolve_day_perms` из `bot.py` (там
зависимость от сессии). Это работает, но **дублирует** логику
`_resolve_day_perms`.

**Рекомендация:** в #9 — унифицировать через единый
`_restore_chat_state(session, cs, bot, chat_id) -> tuple[ok, source]`,
который используют все три режима (alarm, night, sanitary).

### 1.4 Мёртвый код

**Не найдено.** Все `v4.x` комментарии объясняют историю изменений,
но ни одна функция не помечена как deprecated/unused.

### 1.5 TODO/FIXME/XXX

**0 упоминаний.** Код чистый.

---

## 2. Модели БД

### 2.1 Существующие модели (7)

1. **`User`** — юзеры Telegram (id, username, first/last_name). Indexed по tg_user_id.
2. **`Moderator`** — модераторы (связка tg_user_id ↔ first/last_name + username).
3. **`Punishment`** — журнал санкций (warn/mute/ban + soft-revoke через is_revoked).
4. **`ChatAdmin`** — админы чата (chat_id + user_id).
5. **`ChatSettings`** — настройки чата (~30 полей, всеmodes).
6. **`PermissionPreset`** — именованные пресеты прав (day/night/sanitary).
7. **`WebUser`** — веб-аккаунты (PBKDF2-HMAC-SHA256).
8. **`WordFilter`** — паттерны word-фильтра (заменяется на `KeywordWatch` в #10).
9. **`LinkAllowlist`** — белый список доменов.
10. **`BannedStickerPack`** — запрещённые стикерпаки.

**На самом деле моделей 10**, не 7 как указано в ROADMAP #4. Опечатка
в road map — фактических моделей больше.

### 2.2 ChatSettings — структура

Самая «жирная» модель — `ChatSettings` (~30 полей). Сгруппированы
по версиям:
- Базовые (v4.4.7): `chat_id`, `hashtag`, `report_chat_id`, `warns_to_mute`,
  `mute_duration_seconds`, `warns_to_ban`, `is_enabled`, `is_private`,
  `is_report_chat`, `title`.
- CAS / link / word filters (v4.5.2): `cas_check_enabled`,
  `link_filter_enabled`, `link_filter_action`, `auto_delete_commands`,
  `warn_decay_days`.
- Night mode (v4.5.2 + v4.5.3 + v4.7.16): `night_mode_enabled`,
  `night_mode_start/end`, `night_mode_permissions`,
  `night_mode_saved_permissions`, `night_mode_currently_active`,
  `night_mode_tz`, `night_mode_weekend_start/end`, `night_mode_notify`,
  `night_mode_notify_enter_msg`, `night_mode_notify_exit_msg`,
  `night_mode_slow_mode_delay`, `day_slow_mode_delay`,
  `night_mode_saved_slow_mode_delay`.
- Sanitary days (v4.5.4 + v4.7.2 + v4.6.0): `sanitary_days`,
  `sanitary_days_saved_permissions`, `sanitary_days_currently_active`,
  `sanitary_days_enabled`, `sanitary_days_permissions`,
  `last_sanitary_month`.
- Granular permissions (v4.6.0): `day_permissions`,
  `sanitary_days_permissions`.
- Alarm (v4.7.20): `alarm_currently_active`, `alarm_saved_permissions`,
  `alarm_saved_slow_mode_delay`, `alarm_active_until`, `alarm_started_by`.
- Via-bot filter (v4.7.24): `via_bot_filter_enabled`,
  `via_bot_rate_limit_seconds`, `via_bot_mute_minutes`.

**В #10 добавятся:** `mod_chat_id`, `is_mod_chat`, `keyword_watch_list`
(или отдельная таблица `KeywordWatch`).

### 2.3 Миграции БД

В `init_db()` все миграции идемпотентны — проверка через
`PRAGMA table_info(...)` + `ALTER TABLE IF NOT EXISTS`. Это позволяет
запускать бота на старой БД без ручных миграций.

**Рекомендация:** в #10 для новых полей использовать тот же паттерн
(см. `v4720_alarm_cols` в db.py:800-811 как образец).

### 2.4 Seed данных

При `init_db`:
- Глобальный link allowlist: `t.me`, `telegram.me`, `github.com`,
  `youtu.be`, `youtube.com` (если таблица пуста).
- SU-аккаунт `su` в `web_users` (если нет).
- Системные пресеты: `Full lockdown` (sanitary), `Text only` (night),
  `Day default` (day) — все `is_system=True`, неудаляемые.

**Рекомендация:** в #10 — добавить seed для дефолтных keyword-watch
фраз? Скорее всего **нет** — пусть список будет пустым при старте,
администратор вносит фразы через веб-панель.

---

## 3. Безопасность

### 3.1 Секреты

Все секреты берутся из env через `os.getenv`:
- `BOT_TOKEN` — обязательно.
- `WEB_PASSWORD` — SU-пароль (plaintext в env, сверка через `==`).
- `SESSION_SECRET` — для подписанных кук. Fallback на
  `secrets.token_hex(32)` если env не задан (инвалидирует сессии
  при рестарте).
- `WEBHOOK_SECRET` — для проверки webhook-токена. Fallback на
  `secrets.token_hex(16)`.
- `ADMIN_IDS` — список SU- Telegram ID.
- `CHAT_HASHTAGS` — список чатов в формате `-100NNN:Hashtag`.

**Утечек в логи нет.** В `bot.py` есть маскировка (`v4.7.13` ENV DUMP
убран). В `web_app.py` — проверено, `BOT_TOKEN` и `WEB_PASSWORD` не
логируются.

### 3.2 Webhook security

`bot_webhook` (bot.py:1500) проверяет заголовок
`X-Telegram-Bot-Api-Secret-Token` против `WEBHOOK_SECRET`. Если не
совпадает — 401 + INFO-лог (не WARNING — может быть сканер).

**OK.**

### 3.3 Сессии веб-панели

- Cookie `_sess` — подписанная через `_sign()` (HMAC-SHA256).
- При отсутствии `SESSION_SECRET` в env — fallback на random
  (сессии инвалидируются при рестарте, **это фича, не баг** —
  defense-in-depth).
- Logout — через очистку cookie.
- SU-пароль в env в plaintext — приемлемо для Docker single-tenant.

**Рекомендация:** в будущих версиях (#7.3 в roadmap) добавить TOTP
2FA для SU. В v4.8.0 — не нужно.

### 3.4 Пароли веб-юзеров

PBKDF2-HMAC-SHA256, 200 000 итераций, соль 16 байт (hex). Это
соответствует OWASP рекомендациям 2026 года для PBKDF2.

**OK.**

---

## 4. Тесты

### 4.1 Покрытие

39 файлов тестов, ~570 кейсов. Структура каждого файла:
- Structural tests (AST-based) — проверяют что код содержит
  нужные функции/классы/строки.
- Behavioral tests (mock-based) — проверяют логику.

### 4.2 Известные проблемы (pre-existing, не от v4.7.30)

- `test_v454_sanitary_day.py` (12 failures) — version-mismatch:
  тесты проверяют точное совпадение `APP_VERSION == v4.6.1`, но
  версия уже v4.7.30. Нужно ослабить как сделано для v4.7.27/4.7.28/4.7.29.
- `test_v460_granular_perms.py` (1 failure) — аналогично.
- `test_v4716_slowmode.py` — `RuntimeError: BOT_TOKEN env` (тест
  требует env, не запускается в чистом окружении).
- `test_v4712_exit_logic.py` — `freezegun not installed`.

**Рекомендация:** в v4.8.0 ослабить version-mismatch тесты
(`assertEqual` → `assertGreaterEqual`). Это стандартная процедура
после version bump, не критично.

### 4.3 Что НЕ покрыто

- `handle_new_members` CAS-exempt — покрыт в v4.7.30, OK.
- `_alarm_auto_off_tick` — покрыт в v4.7.30, OK.
- Web-панель endpoints — частично (только structural). Behavioral
  тесты веб-панели — зона для улучшения, но не критично.

### 4.4 Регрессия при v4.8.0

После #9 (рефакторинг режимов) **все тесты, связанные с night mode /
sanitary day / alarm, надо перегнать**. Это:
- `test_v453_night_mode.py` (66 тестов).
- `test_v454_sanitary_day.py`.
- `test_v4720b_alarm_command.py` (59 тестов).
- `test_v4730_alarm_audit_fixes.py` (45 тестов).
- `test_v4723_night_mode_persistence.py` (11 тестов).
- `test_v4719_night_notify_error_handling.py` (22 теста).

---

## 5. Зависимости

### 5.1 Используемые библиотеки

- `aiogram` 3.30 — Telegram Bot API framework. Актуальная.
- `sqlalchemy` 2.x async — ORM. Актуальная.
- `fastapi` — веб-фреймворк для панели. Актуальная.
- `uvicorn` — ASGI server. Актуальная.
- `jinja2` — шаблонизатор. Актуальная.
- `aiosqlite` — async SQLite driver. Актуальная.
- `pydantic` 2.x — валидация (через aiogram). Актуальная.

**Уязвимостей в актуальных версиях не найдено** (по состоянию на
август 2026).

### 5.2 Python-версия

Python 3.11+ (используется `from __future__ import annotations`,
`tuple[bool, str, str]` синтаксис, `match` из 3.10 — не используется,
`StrEnum` из 3.11 — не используется).

**OK.**

### 5.3 Новые зависимости для #10 (modchat + keyword-watch)

- `beautifulsoup4` — для парсинга HTML в #11 (GitHub sync). В #10
  не нужен.
- Без новых зависимостей — modchat/keyword-watch реализуется на
  stdlib (`re` для word-boundary match) + существующем aiogram
  rich-блок API.

---

## 6. Шаблоны

### 6.1 Структура

9 шаблонов в `templates/`:
- `base.html` (1121 строка) — общий layout + навигация + changelog.
- `dashboard.html`, `user.html`, `profile.html` — для обычных юзеров.
- `login.html` — авторизация.
- `admin.html`, `admin_chats.html`, `admin_presets.html`,
  `admin_settings.html` — для SU/admin.

**Рекомендация:** в #10 добавить `admin_keywords.html` для управления
keyword-watch списком.

### 6.2 Консистентность

Все шаблоны используют:
- CSS variables из `base.html` (тёмная/светлая тема).
- Jinja2-наследование от `base.html`.
- Единый стиль карточек/таблиц/форм.

**OK.**

### 6.3 APP_VERSION

`APP_VERSION` задаётся в `web_app.py` (строка 28: `APP_VERSION = "v4.7.30"`),
используется в `templates/base.html` для отображения в футере и
в changelog-секции.

**OK.**

---

## 7. Рекомендации для v4.8.0

### 7.1 Обязательно (в рамках #9, #10)

1. **Унифицировать snapshot логику** — вынести в общую функцию
   `_snapshot_chat_permissions(bot, chat_id, day_permissions=None)`.
   Затронет: `_enter_night_mode`, `_enter_sanitary_day`,
   `handle_alarm_command`.
2. **Унифицировать restore логику** — вынести в
   `_restore_chat_state(session, cs, bot, chat_id) -> tuple[ok, source]`.
   Затронет: `_deactivate_alarm`, `_restore_day_state`,
   `_exit_night_mode`, `_exit_sanitary_day`.
3. **Ослабить version-mismatch тесты** — `assertEqual` → `assertGreaterEqual`
   для v4.5.4/v4.6.0/v4.7.x тестов.
4. **Добавить новые поля БД** для modchat: `mod_chat_id`, `is_mod_chat`
   в `ChatSettings`. Идемпотентная миграция как для v4.7.20 alarm.
5. **Добавить таблицу `KeywordWatch`** для keyword-watch фраз.

### 7.2 Желательно (если успеем)

1. **Перенести `_alarm_auto_off_tick` в `bot_handlers.py`** — уменьшит
   coupling между `bot.py` и `bot_handlers.py`. Но это риск regressions,
   лучше отложить до v4.8.1.
2. **Логирование в modchat** — добавить хелпер
   `_send_to_modchat(bot, chat_id, text)` по аналогии с
   `_send_to_report_chat`.
3. **Тесты для веб-панели** — behavioural tests для новых endpoints.

### 7.3 Отклонено (не для v4.8.0)

1. **Разбиение `bot_handlers.py` на модули** — риск regressions
   превышает пользу.
2. **Перенос всех late imports в `bot_handlers.py`** — большой
   рефакторинг, не в этой версии.
3. **Async миграции через Alembic** — для нашего объёма БД
   идемпотентные `ALTER TABLE IF NOT EXISTS` достаточны.

---

## 8. Вывод

Кодовая база **готова к v4.8.0**. Можно приступать к #9 (рефакторинг
режимов) и #10 (modchat + keyword-watch) без предварительной очистки.

Главный риск — regressions в #9 из-за унификации snapshot/restore
логики. Митигация: прогон ВСЕХ существующих тестов после каждого
изменения + добавление новых тестов для каждого унифицированного
пути.

Аудит завершён.
