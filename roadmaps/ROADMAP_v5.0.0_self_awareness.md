# Roadmap v5.0.0 — Bot Self-Awareness & Bothost Integration

**Тема релиза:** бот учится узнавать о себе всё, что Bothost ему готов рассказать, и получает инструменты самоуправления через веб-панель.

**Источники:**
- https://bothost.ru/docs/api-reference — Agent REST API
- https://bothost.ru/docs/environment-variables — стандартные env-переменные
- https://bothost.ru/docs/database-storage — `/app/data` и persistence

**Ключевые env-переменные Bothost, которые бот будет использовать:**
`BOT_ID`, `USER_ID`, `DOMAIN`, `TEMPLATE`, `PORT`, `BOTHOST_AGENT_URL` (опционально, по умолчанию `http://agent:8000`).

---

## 5.0.0-01 · Вкладка Settings — блок «Информация о боте» (причесать существующее)

### Контекст
Во вкладке Settings уже есть какой-то блок с данными о боте, но он либо не работает, либо отображается криво. Надо привести в порядок и сделать красиво.

### Что сделать
- Показать из env: `BOT_ID`, `DOMAIN`, `TEMPLATE`, `USER_ID` (владелец Bothost), `PORT`
- Показать из рантайма: `APP_VERSION`, `HOSTNAME` (container ID из `os.environ.get("HOSTNAME")` или `socket.gethostname()`)
- Показать из filesystem: наличие `/app/data` (флаг «Persisted data dir: yes/no»), наличие `/.dockerenv` (флаг «Running in Docker: yes/no»)
- Сверху — большой статус-бейдж:
  - 🟢 «Бот активен» — TG polling работает, /stats отвечает
  - 🟡 «Проблемы с TG API» — см. 5.0.0-08
  - 🔴 «Перезапуск» — бот в процессе рестарта
- Секции с иконками, аккуратная вёрстка, mono-шрифт для ID/версий

### Файлы
- `web_app.py` — роут `GET /api/self-info`
- `templates/settings.html` — переработать существующий блок
- `static/css/settings.css` — стили

---

## 5.0.0-02 · Кнопка «Перезапустить бота»

### Контекст
Сейчас для рестарта надо открывать панель Bothost. Хочется делать это из своей веб-панели.

### Что сделать
- В блоке «Информация о боте» (см. 5.0.0-01) добавить кнопку «Перезапустить»
- По клику — POST `/api/self/restart` на FastAPI
- FastAPI зовёт Bothost Agent: `POST http://agent:8000/api/bots/self/restart` с заголовком `X-Bot-ID: <BOT_ID>` (или без заголовка — Bothost сам определит по имени контейнера)
- После клика: toast «Перезапуск через 3...2...1...», страница уходит в reconnect-режим (poll `/api/self-info` каждые 2 сек, как только ответила — бот жив)

### Fallback
- Если `agent:8000` недоступен (локальный dev) — кнопка disabled, тултип «Bothost Agent недоступен на этом окружении»
- URL агента определять через хелпер с auto-detect: пробуем `socket.create_connection(('agent', 8000), timeout=1)`, иначе fallback на `BOTHOST_AGENT_URL` env или `http://agent.bothost.ru`

### Файлы
- `web_app.py` — роут `POST /api/self/restart`
- `templates/settings.html` — кнопка + JS-обработчик
- `bot/bothost_agent.py` — новый модуль-обёртка над Bothost Agent API (будет использоваться и в 5.0.0-03, 5.0.0-04, 5.0.0-05)

---

## 5.0.0-03 · Окно «Логи контейнера» в Settings

### Контекст
Полезно смотреть логи бота, не выходя из своей панели и не открывая Bothost.

### Что сделать
- Небольшое окно (высота ~350px, моноширинный шрифт, тёмный фон, автоскролл вниз)
- Кнопка «Загрузить логи» → POST `/api/self/logs?lines=200` → FastAPI зовёт Bothost Agent `POST /api/bots/logs` с `{"bot_id": ..., "lines": 200}`
- Показать последние 200 строк
- Кнопки: «Обновить», «Автопрокрутка» (toggle), «Очистить экран» (только визуально)
- Опционально: фильтр по ключевому слову (regex)

### Файлы
- `web_app.py` — роут `POST /api/self/logs`
- `templates/settings.html` — окно логов
- `bot/bothost_agent.py` — метод `get_logs(lines: int) -> str`

---

## 5.0.0-04 · Дашборд «Здоровье контейнера» (в той же вкладке Settings)

### Контекст
Рядом с кнопкой «Перезапустить» — компактный виджет с живой статистикой. Бот дёргает `/stats` у Bothost Agent, отдаёт в веб.

### Что сделать
- Данные из `GET http://agent:8000/api/bots/{BOT_ID}/stats`:
  - `cpu_percent` — круговой индикатор
  - `memory_usage` (MB) + `memory_percent` — прогресс-бар
  - `uptime` — форматированная строка
- Обновление раз в 30 секунд через `setInterval` в JS
- При `memory_percent > 85` — индикатор краснеет, подсказка «Близко к лимиту тарифа. Рассмотрите перезапуск.»
- При `memory_percent > 95` — автоматически активируется кнопка «Перезапустить» с пульсирующей подсветкой

### Файлы
- `web_app.py` — роут `GET /api/self/stats` (прокси к Bothost с кэшем 10 сек, чтобы не дёргать Agent при каждом открытии страницы)
- `templates/settings.html` — виджет
- `static/js/health_widget.js` — логика обновления

---

## 5.0.0-05 · Self-healing — авто-рестарт при проблемах

### Контекст
Бот сам себя перезапускает, если что-то пошло не так. Без ручного вмешательства SU.

### Что сделать
- Фоновая asyncio-таска, запускается при старте бота
- Раз в 60 секунд проверяет:
  - `memory_percent > 90` (из `/stats`) → рестарт с причиной `high_memory`
  - Пойманы 3+ `MemoryError` за 5 минут → рестарт с причиной `memory_error`
  - Telegram API не отвечает 5 минут подряд → рестарт с причиной `tg_api_unreachable`
- Перед рестартом: запись в лог `SELF_HEALING: restarting due to <reason>, metrics: {...}`
- Вызов `POST /api/bots/self/restart`
- **Защита от цикла**: не более 3 рестартов за 10 минут. Если превысили — бот останавливается, отправляет SU в DM «Self-healing остановлен: слишком частые рестарты, требуется ручное вмешательство» и больше не рестартит до ручного запуска

### Риски
- Может оборвать активные команды пользователей. Решение: перед рестартом проверять `active_tasks_count` (если бот трекает такие), если > 0 — отложить на 30 сек и перепроверить
- Может конфликтовать с 5.0.0-02 (ручной рестарт). Решение: лочить мьютекс `restart_in_progress`, чтобы не было двух рестартов подряд

### Файлы
- `bot/self_healing.py` — новый модуль с таской и логикой
- `bot/main.py` — запуск таски при старте
- `bot/bothost_agent.py` — переиспользование `restart_self()`

---

## 5.0.0-06 · Авто-определение webhook URL из DOMAIN env

### Контекст
Сейчас webhook URL, скорее всего, захардкожен или настраивается вручную. Bothost кладёт домен в `DOMAIN` env — можно использовать.

### Что сделать
- При старте: если `os.getenv("DOMAIN")` задан и непустой → бот строит URL `https://{DOMAIN}/webhook` и регистрирует его через `setWebhook`
- Если `DOMAIN` пустой (локальный dev) → fallback на long polling
- В веб-панели в настройках показать: «Webhook URL: `https://...bothost.tech/webhook` (auto-detected from DOMAIN env)» с кнопкой «Переопределить вручную»

### ⚠️ Риски (важно!)
Может сломать существующую логику, если бот сейчас работает на long polling и вдруг переключится на webhook.

### Решение
- Новый env-флаг `BOT_WEBHOOK_MODE`:
  - `off` (по умолчанию) — ничего не меняется, текущее поведение
  - `auto` — включается новое поведение (читает `DOMAIN`)
  - `force` — всегда webhook, даже если `DOMAIN` пустой (тогда требует `WEBHOOK_URL` env)
- В v5.0.0 флаг по умолчанию `off` — обновление безопасно
- В v5.1.0 можно поменять default на `auto`, когда убедимся что всё ок
- Документировать в CHANGES.md как опциональную фичу, требующую явного включения

### Файлы
- `bot/telegram_setup.py` — логика выбора режима
- `web_app.py` — показывать статус webhook в Settings
- `templates/settings.html` — блок webhook
- `CHANGES.md` — предупреждение про флаг

---

## 5.0.0-07 · Эндпоинт `/healthz` для внешнего мониторинга

### Контекст
Лёгкий публичный эндпоинт для uptime-мониторинга (uptime-kuma, BetterStack, GitHub Actions cron).

### Что сделать
- `GET /healthz` на FastAPI, не требует БД, не требует Telegram, не требует авторизации
- Возвращает:
  ```json
  {
    "status": "ok" | "degraded" | "down",
    "bot_id": "bot_...",
    "container_id": "a1b2c3d4",
    "version": "v5.0.0",
    "uptime_seconds": 12345,
    "memory_mb": 125,
    "memory_percent": 12.3,
    "telegram_connected": true,
    "telegram_api_latency_ms": 250,
    "timestamp": "2026-08-14T12:34:56Z"
  }
  ```
- `status` = `degraded` если `memory_percent > 85` ИЛИ `telegram_connected == false` ИЛИ `telegram_api_latency_ms > 1000`
- `status` = `down` если `memory_percent > 95`
- HTTP-код: 200 для `ok`/`degraded`, 503 для `down`

### Ответ на сомнение «если бот мёртв — веб-панель не работает»
- `/healthz` **не требует Telegram вообще** — FastAPI отвечает даже когда polling упал, поэтому он будет жить дольше основного цикла бота
- Если упал сам процесс бота (не контейнер) — Bothost обычно перезапускает контейнер через несколько секунд, в это время `/healthz` действительно будет 502/504 (но это уже работа Bothost, не наша)
- Главная ценность `/healthz` — обнаружение **деградации до полной смерти**. Memory leak растёт постепенно, `/healthz` даст алерт за 30+ минут до краша. Telegram API тоже начинает тормозить до того, как совсем отвалится
- Внешний мониторинг слёзит каждые 5 минут и алертит SU при первом `degraded` — это и есть смысл эндпоинта

### Файлы
- `web_app.py` — роут `GET /healthz`
- `tests/test_healthz.py` — тесты на все 3 статуса

---

## 5.0.0-08 · Алерт SU в DM при задержках TG API

### Контекст
Если Telegram API начинает тормозить — бот формально жив, но пользователи страдают. SU должен об этом узнать.

### Что сделать
- Обернуть все вызовы `bot.api.*` (или хотя бы ключевые: `sendMessage`, `getUpdates`, `editMessageText`) в хелпер с замером времени
- Хранить в памяти `deque(maxlen=10)` последних latency-замеров
- Если **5 подряд > 1000ms** → отправить SU в DM:
  ```
  ⚠️ Замедление Telegram API

  Последние 5 запросов: 1.2s, 1.4s, 1.1s, 1.3s, 1.5s
  Медиана за 10 запросов: 1.2s

  Бот работает, но ответы пользователям задерживаются.
  Возможные причины:
  • Нагрузка на Telegram-серверы
  • Проблемы с сетью на ноде Bothost
  • Долгая обработка в БД (не TG-вина)

  Проверить: /healthz → telegram_api_latency_ms
  ```
- **Антиспам**: не чаще 1 алерта в 30 минут
- В `/healthz` добавить поле `telegram_api_latency_ms` (медиана за последние 10)
- В виджете здоровья (5.0.0-04) показать `TG latency: 250ms` с цветовой индикацией

### Файлы
- `bot/telegram_client.py` — обёртка над Bot API с замерами
- `bot/alerts.py` — модуль алертов SU (DM-отправка с антиспамом)
- `bot/main.py` — инициализация обёртки

---

## Порядок внедрения (рекомендация)

| # | Задача | Сложность | Зависимости | Риск |
|---|--------|-----------|-------------|------|
| 1 | 5.0.0-01 (причесать Settings) | 🟢 низкая | нет | нет |
| 2 | 5.0.0-07 (/healthz) | 🟢 низкая | нет | нет |
| 3 | 5.0.0-04 (дашборд здоровья) | 🟡 средняя | Bothost Agent API | нет |
| 4 | 5.0.0-02 (кнопка restart) | 🟡 средняя | 5.0.0-01 | низкий |
| 5 | 5.0.0-03 (окно логов) | 🟢 низкая | Bothost Agent API | нет |
| 6 | 5.0.0-08 (алерты SU) | 🟡 средняя | обёртка над API | низкий |
| 7 | 5.0.0-05 (self-healing) | 🔴 высокая | 5.0.0-02 | средний |
| 8 | 5.0.0-06 (авто-webhook) | 🟡 средняя | нет | **высокий** (за флагом) |

**Можно отложить на 5.1.0:** 5.0.0-06 если рискованно. Всё остальное идёт в 5.0.0.

---

## Общая архитектура нового модуля `bot/bothost_agent.py`

```python
# bot/bothost_agent.py
import os
import socket
import aiohttp
from typing import Optional

class BothostAgent:
    """Обёртка над Bothost Agent REST API."""

    def __init__(self):
        self.bot_id = os.getenv("BOT_ID")
        self.user_id = os.getenv("USER_ID")
        self.agent_url = self._detect_agent_url()
        self._session: Optional[aiohttp.ClientSession] = None

    def _detect_agent_url(self) -> str:
        """Auto-detect: пробуем внутренний Docker URL, иначе fallback."""
        if os.getenv("BOTHOST_AGENT_URL"):
            return os.getenv("BOTHOST_AGENT_URL")
        try:
            socket.create_connection(("agent", 8000), timeout=1)
            return "http://agent:8000"
        except OSError:
            return "http://agent.bothost.ru"

    @property
    def is_available(self) -> bool:
        return bool(self.bot_id)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            )
        return self._session

    async def restart_self(self) -> dict:
        """POST /api/bots/self/restart — рекомендуемый self-restart."""
        session = await self._get_session()
        async with session.post(
            f"{self.agent_url}/api/bots/self/restart",
            headers={"X-Bot-ID": self.bot_id},
        ) as resp:
            return await resp.json()

    async def get_stats(self) -> dict:
        """GET /api/bots/{bot_id}/stats — CPU, memory, uptime."""
        session = await self._get_session()
        async with session.get(
            f"{self.agent_url}/api/bots/{self.bot_id}/stats"
        ) as resp:
            return await resp.json()

    async def get_logs(self, lines: int = 100) -> str:
        """POST /api/bots/logs — последние N строк логов."""
        session = await self._get_session()
        async with session.post(
            f"{self.agent_url}/api/bots/logs",
            json={"bot_id": self.bot_id, "lines": lines},
        ) as resp:
            data = await resp.json()
            return data.get("logs", "")

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
```

Используется во всех задачах 5.0.0-02 / 03 / 04 / 05.
