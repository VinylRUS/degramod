# v5.8.0 — GigaChat (LLM) авторазвёртка причин наказаний

## Что в PR

Реализация v5.8.0 из roadmap §14. Подключение GigaChat как основного
LLM-провайдера для авторазвёртки коротких причин наказаний модераторов
(`!warn спам` → «Реклама онлайн-казино в основном чате (повторное
нарушение, прошлый варн 2 дня назад)»).

**Только то, что согласовано:**

- ✅ Ключ GigaChat вводится в панели SU (НЕ в env)
- ✅ Обращение к LLM — только после включения тумблера в настройках бота
- ✅ Причина mod-действий (warn/ban/mute) дорабатывается через LLM
- ✅ Контекст: причина модератора + сообщение нарушителя + правила чата
  из `docs/rules.md` (конвертировано с rules.degradach.ru)
- ❌ Суммаризация логов — оставлена на будущее (по умолчанию выкл.)

## Файлы

### Новые

| Файл | Назначение |
|---|---|
| `llm_client.py` | Обёртка над GigaChat API. По образцу `bothost_agent.py` — async, aiohttp, не бросает исключений. |
| `docs/rules.md` | Правила чата Degradach в markdown (источник: rules.degradach.ru). Контекст для LLM. |
| `migrations/versions/g5h6i7j8k9l0_v5_8_0_llm_gigachat.py` | Alembic-миграция. |
| `tests/test_v580_llm_client.py` | Тесты llm_client (T1–T16). |
| `tests/test_v580_settings.py` | Структурные тесты (T1–T15): DB, web, templates, миграции. |
| `PR_DESCRIPTION.md` | Этот файл. |

### Изменённые

| Файл | Что добавлено |
|---|---|
| `db.py` | Новая ORM-модель `LlmSettings` (singleton id=1, шифрованный ключ). Колонка `chat_settings.llm_expand_reason` (per-chat тумблер). Идемпотентная миграция в `init_db()` + seed singleton. |
| `web/admin_settings.py` | Роуты `GET/POST /admin/settings/llm` + `POST /admin/settings/llm/test`. Форма SU для ввода ключа GigaChat с шифрованием Fernet. Кнопка теста ключа. |
| `web/admin_chats.py` | Параметр `llm_expand_reason` в `/admin/chats/{id}/update`. Toggle `llm_expand` в `/admin/chats/{id}/toggle`. |
| `bot_handlers.py` | Хелпер `_maybe_spawn_llm_reason_expand()` — фоновая задача (через `_spawn_background_task`), не блокирует `_send_report`. Хелпер `_load_rules_md_for_llm()` — кеш правил на процесс. |
| `templates/admin_settings.html` | Раздел `#llm` с формой (API key, model, timeout, is_active) и кнопкой «Проверить ключ». |
| `templates/admin_chats.html` | Кнопка `LLM` в карточке чата, чекбокс в форме, бейдж в заголовке. |
| `templates/base.html` | Changelog v5.8.0. |
| `.env.example` | Документация: ключ GigaChat НЕ в env, а в /admin/settings#llm. |
| `web_app.py` | `APP_VERSION = "v5.8.0"`. |
| `CLAUDE.md` | Описание нового модуля `llm_client.py` и `docs/rules.md` в карте модулей. |
| `roadmap.md` | §14 заполнен деталями реализации. |

## Архитектура

```
┌─────────────────────────────────────────────────────────────────┐
│ SU вводит ключ GigaChat в /admin/settings#llm                   │
│         │                                                        │
│         ▼                                                        │
│  LlmSettings (singleton id=1)                                   │
│  - api_key_encrypted  (Fernet)                                  │
│  - model_name         (GigaChat-Pro по умолч.)                   │
│  - is_active          (глобальный рубильник)                     │
│         │                                                        │
│         ▼                                                        │
│  llm_client._cached_settings  (in-memory, через settings_changed)│
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│ Per-chat тумблер в /admin/chats:                                │
│  ChatSettings.llm_expand_reason (DEFAULT 0)                     │
│  кнопка LLM в карточке чата                                      │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│ Когда модератор !warn/!ban/!mute:                                │
│  _send_report()                                                  │
│   └─> _maybe_spawn_llm_reason_expand()                           │
│        ├─ is_available()? → False? выходим (рубильник выкл)      │
│        ├─ chat_settings.llm_expand_reason? False? выходим        │
│        ├─ reason >= 200 символов? модератор расписал, выходим    │
│        ├─ reason.startswith("Автобан"/"CAS auto-ban")? системный  │
│        └─ _spawn_background_task(_expand_and_report())           │
│             ├─ llm_client.expand_reason(...)                     │
│             │    system: правила + анти-галлюцинации             │
│             │    user:    тип + reason + цитата + контекст        │
│             ├─ bot.send_message(report_chat, expanded)           │
│             └─ UPDATE punishments SET reason=expanded WHERE ...  │
└─────────────────────────────────────────────────────────────────┘
```

## Безопасность

- **Ключ не в env.** SU вводит через веб-панель, шифруется Fernet'ом,
  хранится в БД (`llm_settings.api_key_encrypted`).
- **Тот же Fernet-ключ, что у GitHub PAT.** Файл
  `/app/data/.github_enc_key`. При потере — теряются оба. Это
  задокументировано в `.env.example` и `CLAUDE.md`.
- **В панель ключ не возвращается.** Только признак `is_api_key_set`.
- **Промпт и ответ не пишутся в лог.** Только длительность, токены,
  модель. `LLM_LOG_PAYLOAD` не реализован (см. roadmap §14.9).
- **Кеш только в памяти.** Промпты и ответы не пишутся в БД, не
  утекают в дамп-CSV (v5.5.0).
- **Системный промпт запрещает галлюцинации.** Модель НЕ добавляет
  фактов, которых не было в исходных данных. Если reason уже
  развёрнут (≥ 200 символов) — LLM его не трогает.
- **Системные причины пропускаются.** «Автобан: ...», «CAS auto-ban: ...»
  — их формирует бот, не модератор, LLM их не касается.

## Тесты

`tests/test_v580_llm_client.py` — 16 тестов (мокается `aiohttp.ClientSession`):
- T1–T4: fast-path если LLM выключена (нет ключа / is_active=False / нет настроек)
- T5: 200 OK → ok=True, текст из choices[0].message.content
- T6: 401 → ok=False, "unauthorized" в ошибке
- T7: 429 → ретраится, в итоге ok=False
- T8: таймаут → ok=False
- T9: не-JSON ответ → ok=False
- T10: aiohttp.ClientError → ok=False, исключение не всплывает
- T11: кеш одинаковых запросов — 1 сетевой запрос на 2 вызова
- T12: reset_cache() сбрасывает кеш
- T13: expand_reason строит system+user messages
- T14: test_connection шлёт короткий промпт
- T15: test_connection на 401
- T16: _extract_content / _extract_usage парсят тело GigaChat

`tests/test_v580_settings.py` — 15 структурных тестов:
- T1–T4: DB-модели и миграции
- T5–T7: web-роуты и шаблоны
- T8: llm_client API
- T9: docs/rules.md существует и валиден
- T10: Alembic-миграция
- T11: bot_handlers интегрирован
- T12: .env.example не содержит GigaChat-ключ
- T13: llm_client без хардкоженных ключей
- T14: admin_chats toggle корректно работает с llm_expand
- T15: в репозитории нет утечки токенов

Запуск:
```bash
uv run python tools/run_tests.py -k v580
```

## Что НЕ входит в PR

- ❌ Суммаризация логов контейнера (roadmap §14.3) — оставлена на
  будущее, после стабилизации выгрузки в Loki.
- ❌ Семантический спам-фильтр, `!why`, триаж GitHub Issues, image-to-text,
  NL-to-SQL, voice-to-text, дайджест для SU — кандидаты на v6.0.0+
  (roadmap §14.7). Любая входит в работу только после отдельного дизайна.
- ❌ Хранение истории промптов в БД — риск утечки в дамп-CSV (v5.5.0),
  в v5.8.0 не храним (roadmap §14.9).
- ❌ Полная авто-модерация без человека в цикле — LLM галлюцинирует,
  банит невиновных, для стелс-бота это репутационный удар (roadmap §14.9).
- ❌ Диалоги с пользователями — нарушает инвариант «бот молчит для юзеров»
  (`bot_handlers.py:1-15`).

## Известные ограничения (см. roadmap §14.9)

1. **`punishments.reason` обновляется «best effort».** Берётся последняя
   запись наказания этого юзера в чате. Если LLM не успела до сохранения
   записи или reason уже изменён — обновление промахнётся молча. Передача
   `punishment_id` в `_send_report` решит это, но потребует правки
   сигнатуры — отложено.
2. **Кеш правил на процесс.** При обновлении `docs/rules.md` нужен
   рестарт бота. Сознательно: правила меняются редко.
3. **Эндпоинт GigaChat хардкожен.** `https://gigachat.devices.sberbank.ru`
   — Premium-окружение не поддерживается. Документация Sber не
   рекомендует выносить URL в env (опечатка → стук в никуда).
4. **Bothost-тариф.** Исходящий HTTPS к `gigachat.devices.sberbank.ru`
   из Docker-контейнера. На free-тарифе Bothost иногда ограничен whitelist
   доменов. Проверить до релиза (roadmap §14.5).

## Что проверить ревьюеру

1. **Контракт llm_client** — не бросает исключений, fast-path на
   выключенной LLM, async/aiohttp (ASYNC230). См. `llm_client.py:1-50`.
2. **Шифрование ключа** — `_encrypt_pat`/`_decrypt_pat` переиспользуются
   от GitHub PAT. Ключ в БД только в зашифрованном виде.
   См. `web/admin_settings.py:685-700, 775-785`.
3. **Глобальный рубильник vs per-chat тумблер.** Глобальный =
   `LlmSettings.is_active`. Per-chat = `chat_settings.llm_expand_reason`.
   Без глобального per-chat игнорируется. См. `bot_handlers.py:_maybe_spawn_llm_reason_expand`.
4. **Фоновая задача не блокирует `_send_report`.** Через
   `_spawn_background_task` (CLAUDE.md «asyncio.create_task»). Если LLM
   не успела — остаётся исходный reason.
5. **Анти-галлюцинации в системном промпте.** См. `llm_client.py:expand_reason`.
   Запрещено добавлять факты, которых не было в исходных данных.
6. **Миграции идемпотентны.** `init_db()` + Alembic — обе пути
   совместимы (CLAUDE.md «Alembic включён на проде с 20.08.2026»).
7. **Тесты на моках `aiohttp.ClientSession`.** Реального сетевого
   вызова в CI нет. Проверяется поведение, не переписка с сервером.

## Deployment notes

1. После merge — на проде запустить миграции (Alembic или init_db fallback).
2. SU заходит в `/admin/settings#llm`, вводит ключ GigaChat, жмёт
   «Проверить ключ».
3. После проверки — чекбокс «Включить LLM глобально» → Сохранить.
4. На `/admin/chats` — для нужных чатов включить тумблер LLM в карточке.
5. Первый `!warn спам` в включённом чате — проверяем, что в репорт-чате
   приходит follow-up «🔍 Развёрнутая причина (LLM): ...».

## Roadmap

См. `roadmap.md` §14 для полного дизайна и §14.7 для идей на будущее.
