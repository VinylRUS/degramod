# v5.7.0 — Логи в Grafana Cloud Loki + файловая ротация + фикс Bothost Agent

## Контекст

Инцидент 22.09.2026 показал, что без структурированных логов разбор инцидента
превращается в гадание по `getWebhookInfo`. Поддержка Bothost нашла причину
(`ClientDecodeError` в aiogram 3.30.0 на `rich_message.blocks type=buttons`),
но:

- Без файловых логов на диске мы бы не увидели stack trace после рестарта
  контейнера — stdout Bothost хранит ограниченно.
- Без удалённого логирования мы не можем искать по истории логов, не можем
  настроить alerting на рост ERROR, не можем увидеть логи с телефона.

Параллельно с этим, на странице `/admin/settings` в блоке «Bothost Agent»
всегда горел красный «недоступен» с причиной `Unauthorized: invalid token`.
Агент не использовался с v5.1.0 именно из-за этой ошибки.

Этот PR закрывает обе проблемы одной поставкой: добавляет **минимальную**
систему логирования (Grafana Cloud Loki как primary, локальный файл как
offline fallback, stdout для Bothost terminal) и **чинит авторизацию агента**.

## Что меняется

### Новые файлы

- `logging_config.py` — конфигурация root logger: 3 handler'а (Stream, File,
  Loki). Идемпотентная `configure_logging()`, graceful degradation при
  отсутствии прав на каталог или библиотеки.
- `tests/test_v570_logging_config.py` — 15 тест-кейсов (все зелёные).
- `tests/test_v571_bothost_agent_no_bearer.py` — 12 тест-кейсов (все зелёные).

### Изменённые файлы

- `pyproject.toml`:
  - `version = "5.7.0"`.
  - Новая зависимость: `python-logging-loki>=0.3.1`.
- `uv.lock` — пере-лок, добавлены `python-logging-loki`, `requests`, `rfc3339`,
  `charset-normalizer`, `urllib3`, `idna` (транзитивные).
- `bot.py`:
  - Удалён `logging.basicConfig(...)` блок (строки 110-114 в v5.6.1).
  - Добавлен `import logging_config` (~строка 50, рядом с другими
    локальными импортами).
  - После импорта `web_app.APP_VERSION` — вызов
    `os.environ.setdefault("APP_VERSION_TAG", APP_VERSION)` +
    `logging_config.configure_logging()`.
  - `logger = logging.getLogger("shadow_logger")` остался на своём месте.
- `web_app.py` — `APP_VERSION = "v5.7.0"`.
- `.env.example` — добавлены `LOKI_URL`, `LOKI_USER`, `LOKI_PASSWORD`,
  `LOKI_ENVIRONMENT`, `LOG_DIR` с подробными комментариями где брать креды.
- `templates/base.html` — changelog-запись для v5.7.0 (включая фикс агента).
- `bothost_agent.py`:
  - `_auth_headers()` возвращает пустой словарь — Bearer убран.
  - `diagnose_tokens()` больше не делает сетевых запросов, только читает env.
  - Docstring'и обновлены с объяснением, почему Bearer убран (с цитатой
    документации Bothost).
  - Функции оставлены для обратной совместимости — сигнатуры не изменились.

## Часть 1: Логи в Grafana Cloud Loki

### Архитектура

```
logging in bot.py / bot_handlers.py / web_app.py / db.py / etc.
    ↓
root logger
    ↓
[StreamHandler]        → stdout (для Bothost terminal и agent /api/bots/logs)
[TimedRotatingFileHandler] → /app/data/logs/bot.log (ежедневная ротация, 7 дней)
[LokiHandler]          → https://logs-prod-012.grafana.net/loki/api/v1/push
                          (только если LOKI_URL задан)
```

Loki labels (low cardinality — важно для производительности Loki):
- `app = "degramod"`
- `version = "v5.7.0"` (берётся из `APP_VERSION_TAG` env, который
  `bot.py` выставляет из `APP_VERSION`).
- `env = "production"` (берётся из `LOKI_ENVIRONMENT`, default "production").

Не тегаем: `chat_id`, `user_id`, `level` — взрыв кардинальности. Эти поля
живут в тексте лога, ищутся через LogQL `|= "ERROR"`.

### Backward compatibility

- **Без `LOKI_URL` бот стартует без удалённого логирования.** Только stdout
  + файл. Старые инсталляции не затрагиваются.
- **При недоступности `/app/data/logs`** (нет прав, диск readOnly) —
  file handler пропускается с warning, бот стартует с одним StreamHandler.
- **При недоступности `logging-loki` библиотеки** — Loki handler
  пропускается с warning, бот стартует без удалённого логирования.
- **При любой ошибке LokiHandler** (нет сети, неверный URL, 401) —
  warning в лог, бот работает дальше.
- Формат лога **unchanged** — тот же
  `"%(asctime)s │ %(name)-24s │ %(levelname)-7s │ %(message)s"` из v5.6.1.

## Часть 2: Фикс Bothost Agent

### Симптом на проде

Блок «Bothost Agent» в `/admin/settings` показывал:
```
URL:          http://n19.bothost.ru
Токен:        задан (BOT_API_TOKEN, длина 46)
Статус:       ❌ недоступен
Причина:      Unauthorized: invalid token
```

### Корень проблемы

Документация Bothost (https://bothost.ru/llms-full.txt) прямо говорит:

> `API_TOKEN` — Альтернативное имя для `BOT_TOKEN` (совместимость)

То есть Bothost **кладёт в `BOT_API_TOKEN`/`API_TOKEN` тот же самый
Telegram-токен** от @BotFather. Это не отдельный API key для Agent API —
это просто alias для совместимости.

Код в `bothost_agent._auth_headers()` делал:
```python
return {"Authorization": f"Bearer {token}"} if token else {}
```

И слал Bearer с Telegram-токеном. Агент не знает, что с ним делать, и
отвечал `Unauthorized: invalid token`.

Документация Agent API (там же, llms-full.txt):
> **Заголовки:**
> ```
> X-Bot-ID: bot_1764482446_5595_proxyrp
> ```
> **Или:** Бот может не передавать заголовок — система автоматически
> определит `BOT_ID` из имени контейнера.

То есть `X-Bot-ID` — основная авторизация. Bearer не нужен.

### Решение

`_auth_headers()` возвращает пустой словарь. Bearer не шлётся. Авторизация
идёт только через `X-Bot-ID` (он и так добавлялся в `_request` с v5.1.0).

`diagnose_tokens()` больше не делает сетевых запросов — раньше он
перебирал все токены и слал каждый как Bearer, все получали «invalid token».
Теперь показывает только факт наличия переменных в env, без запросов к
агенту. Сообщение в веб-панели: «задан (не используется для agent API)».

### Что должно заработать после деплоя

В `/admin/settings` → блок «Bothost Agent»:
- `URL: http://n19.bothost.ru` (или что у тебя в `BOTHOST_AGENT_URL`).
- `Статус: ✅ доступен`.
- `Raw` — JSON с реальной статистикой контейнера (CPU, memory, uptime).

Что становится доступным:
- Кнопка «Перезапустить бота» в веб-панели — `bothost_agent.restart_self()`.
- Просмотр логов контейнера в веб-панели — `bothost_agent.get_logs(200)`.
- Дашборд CPU/memory контейнера — `bothost_agent.get_stats()`.
- Self-healing при OOM (запланировано в `5.0.0-05`) — `bothost_agent.restart_self()`.

## Что НЕ вошло в этот PR

Отложено на следующие PR (issues уже оформлены в `/home/z/my-project/download/`):

- **Crash handler с DM SU** — `issue_auto_log_shipping.md`. Loki ловит ERROR
  и stack trace, этого достаточно для начала. Crash handler = backup для OOM
  и segfault.
- **Sentry** — отдельный PR для crash-tracking с breadcrumbs.
- **B2 архив логов** — отдельный PR, объединить с #10 (B2 backup SQLite).
- **Auto-restart polling** — `issue_polling_auto_restart.md`. Не связан с
  логами напрямую.
- **Sanitize секретов** — в текущем коде секреты в логи не пишутся. Добавим
  когда/если появится `logger.info(f"token={BOT_TOKEN}")` — пока не нужно.
- **Metрики (Mimir)** — отдельный PR v5.8.0 (см. `grafana-plan.md`).

## Деплой

1. **Закоммитить и запушить ОБА файла** — `pyproject.toml` и `uv.lock`.
   Образ Bothost собирается через `uv sync --frozen` — одного `pyproject.toml`
   недостаточно, версия берётся из `uv.lock`.
2. **В Bothost панели** добавить 3 env vars для Loki (см. `.env.example`):
   ```
   LOKI_URL=https://logs-prod-012.grafana.net/loki/api/v1/push
   LOKI_USER=1796903
   LOKI_PASSWORD=<твой API key с правом logs:write>
   ```
   `LOKI_ENVIRONMENT=production` уже по умолчанию.
3. **Нажать «Обновить из Git»** — пересборка + рестарт.
4. **Проверить логи в Grafana:**
   - Grafana → слева меню **Explore** → выбрать источник
     `grafanacloud-narrowlemon1415-logs`.
   - LogQL: `{app="degramod"}` → должны увидеть логи старта бота.
   - Фильтр по версии: `{app="degramod", version="v5.7.0"}`.
   - Фильтр по ошибкам: `{app="degramod"} |= "ERROR"`.
5. **Проверить агента:**
   - Открыть `https://degraban.bothost.tech/admin/settings`.
   - Блок «Bothost Agent» должен показать `available: true`.
   - `Raw` должен содержать JSON с CPU/memory/uptime.

## Тесты

### `tests/test_v570_logging_config.py` — 15 тест-кейсов

```
TestConfigureLoggingNoLoki::test_t1_no_loki_url_two_handlers         ✓
TestConfigureLoggingNoLoki::test_t5_idempotent_no_duplicate_handlers  ✓
TestConfigureLoggingWithLoki::test_t2_with_loki_url_three_handlers   ✓
TestConfigureLoggingWithLoki::test_t6_loki_with_user_and_password    ✓
TestConfigureLoggingWithLoki::test_t7_loki_with_only_password        ✓
TestConfigureLoggingWithLoki::test_t8_loki_without_any_auth         ✓
TestConfigureLoggingWithLoki::test_t9_app_version_tag_used_as_label ✓
TestConfigureLoggingWithLoki::test_t10_loki_environment_label        ✓
TestConfigureLoggingFailures::test_t3_loki_url_but_no_library        ✓
TestConfigureLoggingFailures::test_t4_log_dir_not_writable           ✓
TestModuleExists::test_t11_app_version_v570                          ✓
TestModuleExists::test_t12_pyproject_version                         ✓
TestModuleExists::test_t13_logging_config_module_exists              ✓
TestModuleExists::test_t14_logging_loki_in_deps                      ✓
TestModuleExists::test_t15_changelog_v570_in_base_html               ✓
```

### `tests/test_v571_bothost_agent_no_bearer.py` — 12 тест-кейсов

```
TestAuthHeadersReturnsNoBearer::test_t1_no_env_returns_empty            ✓
TestAuthHeadersReturnsNoBearer::test_t2_explicit_token_ignored          ✓
TestAuthHeadersReturnsNoBearer::test_t2b_env_token_ignored              ✓
TestRequestSendsXBotIdOnly::test_t3_no_authorization_header            ✓
TestRequestSendsXBotIdOnly::test_t4_x_bot_id_present                   ✓
TestRequestSendsXBotIdOnly::test_t5_no_bot_id_no_headers                ✓
TestDiagnoseTokensNoNetwork::test_t6_no_network_calls                  ✓
TestDiagnoseTokensNoNetwork::test_t6b_returns_for_each_token_in_env    ✓
TestDiagnoseTokensNoNetwork::test_t7_message_does_not_say_accepted     ✓
TestFileDoesNotHaveBearerInCode::test_t8_no_bearer_in_auth_headers_body ✓
TestFileDoesNotHaveBearerInCode::test_t9_auth_headers_callable         ✓
TestFileDoesNotHaveBearerInCode::test_t9b_diagnose_tokens_callable      ✓
```

Запуск:
```bash
uv run python tools/run_tests.py -k v570_logging
uv run python tools/run_tests.py -k v571_bothost
# или по одному:
uv run python tests/test_v570_logging_config.py
uv run python tests/test_v571_bothost_agent_no_bearer.py
```

Ruff: `All checks passed!` на всех новых и изменённых файлах.

## Файлы в PR

| Файл | Статус | Что меняется |
|---|---|---|
| `pyproject.toml` | изменён | bump version, add `python-logging-loki` dep |
| `uv.lock` | изменён | re-lock с новым dep |
| `logging_config.py` | НОВЫЙ | конфигурация логирования (3 handler'а) |
| `bot.py` | изменён | заменён `basicConfig` на `logging_config.configure_logging()` |
| `web_app.py` | изменён | `APP_VERSION = "v5.7.0"` |
| `.env.example` | изменён | добавлены `LOKI_*` vars с инструкцией |
| `templates/base.html` | изменён | changelog для v5.7.0 (включая фикс агента) |
| `bothost_agent.py` | изменён | убран Bearer, обновлён `diagnose_tokens` |
| `tests/test_v570_logging_config.py` | НОВЫЙ | 15 тестов логирования |
| `tests/test_v571_bothost_agent_no_bearer.py` | НОВЫЙ | 12 тестов Bothost Agent fix |

## Связанные issues / PR

- `issue_auto_log_shipping.md` — следующий PR: crash handler + Sentry.
- `issue_polling_auto_restart.md` — следующий PR: auto-restart polling +
  health probe для апдейтов.
- `grafana-plan.md` — дорожная карта Grafana-интеграций (метрики, SLO,
  Pyroscope).

## Knee-dependency: `python-logging-loki`

Пакет не обновлялся с 2020 (0.3.1), но:
- Код простой — ~200 строк, один `emitter.py`.
- Стабилен — нет активных issues на GitHub.
- Работает с Python 3.14 (проверено в этом PR — тесты прошли).
- Использует `requests` (sync), не `aiohttp`. Это **OK для логов** —
  `logging.Handler.emit()` синхронный, и бот уже использует `to_thread`
  для блокирующего I/O (правило `ASYNC230`). Если когда-нибудь станет
  узким местом — заменим на custom `aiohttp`-based handler.

Альтернатива — `loki-client-python` (от Grafana) — официально поддерживается,
но тянет `aiohttp` и `grpc`, тяжелее. Для нашего объёма (30 МБ/мес)
`python-logging-loki` достаточен.
