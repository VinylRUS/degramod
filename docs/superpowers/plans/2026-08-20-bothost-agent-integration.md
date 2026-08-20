# Интеграция с Bothost Agent API — план внедрения (v5.1.0)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** дать боту управление собственным контейнером через Bothost Agent
API: чтение логов и статистики, ручной перезапуск, автовосстановление и
автоопределение вебхука — последние два за выключенными по умолчанию флагами.

**Architecture:** один модуль-обёртка `bothost_agent.py` инкапсулирует HTTP и
определение адреса агента; роуты панели в `web/self_admin.py` вызывают его и
показывают результат; фоновая задача в `bot.py` использует ту же обёртку для
автовосстановления. Обёртка никогда не бросает исключений — отсутствие агента
это штатное состояние.

**Tech Stack:** Python 3.14.7, aiohttp 3.13.3 (уже в зависимостях), FastAPI
0.141.1, aiogram 3.30.0, Jinja2, uv, ruff, unittest + `tools/run_tests.py`.

**Spec:** `docs/superpowers/specs/2026-08-20-bothost-agent-integration-design.md`

## Global Constraints

- **Язык кода и комментариев — русский** (требование `CLAUDE.md`).
- **Агент недоступен локально.** `agent:8000` не резолвится, внешние адреса
  отказывают. Ни одну функцию нельзя проверить живым вызовом: тесты идут на
  моках `aiohttp`. Отсутствие агента — штатное состояние, панель обязана
  работать без него.
- **Обёртка не бросает исключений наружу.** Все методы возвращают
  `AgentResult(ok, data, error)`. Вызовы идут из веб-роутов и фоновой задачи:
  роут должен показать «агент недоступен», а не 500; фоновая задача, падающая
  от сетевого сбоя, перестала бы работать ровно тогда, когда нужнее всего.
- **Обе автоматики выключены по умолчанию.** `SELF_HEALING_ENABLED=0` и
  `BOT_WEBHOOK_MODE=off` уже заданы владельцем на проде. После деплоя
  поведение прода не меняется.
- **POST-роуты требуют `require_csrf_su`**, GET — `require_su`. Инвариант
  проекта, за ним следит `tests/test_v488_verify_csrf.py`.
- **Критичные вызовы Bot API — через `tg_safe_call`.** За этим следит
  `tests/test_v4103_critical_calls_wrapped.py`.
- **Импорт роутера — только внутри `create_app()`.** Top-level импорт `web.*`
  в `web_app.py` даёт цикл.
- **Обращения к хелперам `web_app` — через модуль** (`web_app._helper(...)`),
  не `from web_app import _helper`.
- **Прогон после каждой задачи:** `uv run python tools/run_tests.py`
  (timeout=600000, ~400 сек) и `uv run ruff check .`.
- **Версия бампается один раз** в финальной задаче.

### Эталонные числа (на `7cdbad7`, v5.0.0)

- Сюита: **72 файла**, все PASS.
- Роутов у приложения: **55** (`tests/test_v490_decomposition.py`).
- `ruff check .`: All checks passed.

### Как запускать сюиту

Она идёт ~400 секунд, дефолтный таймаут Bash — 120. Передавай таймаут полем
инструмента Bash, не частью команды:

    Bash(command="uv run python tools/run_tests.py", timeout=600000)

Не используй `run_in_background` — результат нужен сразу.

---

## File Structure

| Файл | Ответственность | Задача |
|---|---|---|
| `bothost_agent.py` | обёртка над Agent API: адрес, HTTP, `AgentResult` | 1 |
| `web/self_admin.py` | роуты `/api/self/*` для панели | 3, 4, 5 |
| `templates/admin_settings.html` | блоки диагностики, логов, здоровья, кнопка | 2, 4, 5 |
| `web_app.py` | подключение роутера внутри `create_app()` | 3 |
| `bot.py` | фоновая задача self-healing, режим вебхука | 6, 7 |
| `.env.example`, `CLAUDE.md`, `roadmap.md` | новые переменные и статусы | 8 |
| `tests/test_v510_bothost_agent.py` | обёртка и роуты | 1, 3, 4, 5 |
| `tests/test_v510_self_healing.py` | автоматика | 6, 7 |

---

## Task 1: Модуль-обёртка `bothost_agent.py`

Фундамент. Ни на что не влияет: модуль создаётся, но никем ещё не вызывается.

**Files:**
- Create: `bothost_agent.py`
- Test: `tests/test_v510_bothost_agent.py`

**Interfaces:**
- Produces:
  - `AgentResult` — датакласс с полями `ok: bool`, `data: dict | None`, `error: str | None`
  - `async resolve_agent_url() -> str`  # ВНИМАНИЕ: асинхронная. Стала такой в Task 1 (fix round 1):
    #   проверка TCP-доступности agent:8000 идёт через asyncio.open_connection + wait_for,
    #   потому что socket.create_connection блокировал event loop на 3.61s (DNS не покрыт timeout).
    #   Любой вызов ОБЯЗАН быть await-нутым.
  - `reset_cache() -> None`
  - `async probe() -> AgentResult`
  - `async get_stats() -> AgentResult`
  - `async get_logs(lines: int = 200) -> AgentResult`
  - `async restart_self() -> AgentResult`

- [ ] **Step 1: Написать падающий тест**

Создать `tests/test_v510_bothost_agent.py`:

```python
"""
test_v510_bothost_agent.py — обёртка над Bothost Agent API (v5.1.0).

Агент доступен только изнутри Docker-сети Bothost: локально `agent:8000` не
резолвится, внешние адреса отказывают в соединении. Поэтому здесь мокается
HTTP-клиент, а проверяется поведение обёртки — что она возвращает, когда
агента нет, когда он отвечает мусором и когда таймаутит.

Главное требование: обёртка НЕ бросает исключений. Её зовут веб-роуты (там
исключение превратится в 500 вместо честного «агент недоступен») и фоновая
задача (там оно убьёт наблюдение ровно тогда, когда оно нужнее всего).
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from _paths import _P

os.environ.setdefault("BOT_TOKEN", "1:test")
os.environ.setdefault("ADMIN_IDS", "1")

sys.path.insert(0, _P())

import bothost_agent  # noqa: E402


class _AgentCase(unittest.TestCase):
    def setUp(self):
        bothost_agent.reset_cache()
        self._prev_bot_id = os.environ.get("BOT_ID")
        self._prev_url = os.environ.get("BOTHOST_AGENT_URL")
        os.environ["BOT_ID"] = "bot_test_123"

    def tearDown(self):
        for key, prev in (("BOT_ID", self._prev_bot_id),
                          ("BOTHOST_AGENT_URL", self._prev_url)):
            if prev is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prev
        bothost_agent.reset_cache()


class TestUrlResolution(_AgentCase):
    """Адрес агента: внутренний → переменная → дефолт."""

    def test_prefers_internal_docker_address(self):
        """Внутри сети Bothost агент доступен как agent:8000."""
        with patch.object(bothost_agent, "_can_connect", return_value=True):
            self.assertEqual(await bothost_agent.resolve_agent_url(), "http://agent:8000")

    def test_falls_back_to_env_variable(self):
        os.environ["BOTHOST_AGENT_URL"] = "http://msk1.bothost.ru"
        with patch.object(bothost_agent, "_can_connect", return_value=False):
            self.assertEqual(
                await bothost_agent.resolve_agent_url(), "http://msk1.bothost.ru",
            )

    def test_falls_back_to_public_default(self):
        os.environ.pop("BOTHOST_AGENT_URL", None)
        with patch.object(bothost_agent, "_can_connect", return_value=False):
            self.assertEqual(
                await bothost_agent.resolve_agent_url(), "http://agent.bothost.ru",
            )

    def test_result_is_cached(self):
        """Адрес не меняется в пределах жизни контейнера — socket дёргаем раз."""
        with patch.object(bothost_agent, "_can_connect", return_value=True) as probe:
            await bothost_agent.resolve_agent_url()
            await bothost_agent.resolve_agent_url()
        self.assertEqual(probe.call_count, 1)


class TestMissingBotId(_AgentCase):
    """Без BOT_ID в сеть не ходим вовсе."""

    def test_stats_refuses_without_bot_id(self):
        os.environ.pop("BOT_ID", None)
        result = asyncio.run(bothost_agent.get_stats())
        self.assertFalse(result.ok)
        self.assertIn("BOT_ID", result.error)

    def test_restart_refuses_without_bot_id(self):
        os.environ.pop("BOT_ID", None)
        result = asyncio.run(bothost_agent.restart_self())
        self.assertFalse(result.ok)


if __name__ == "__main__":
    unittest.main(verbosity=2)
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `uv run pytest tests/test_v510_bothost_agent.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'bothost_agent'`

- [ ] **Step 3: Создать `bothost_agent.py`**

```python
"""
bothost_agent.py — обёртка над Bothost Agent API (v5.1.0).

Агент управляет контейнером бота: перезапуск, логи, статистика. Доступен
только изнутри Docker-сети Bothost по адресу agent:8000 — снаружи (и в
локальной разработке) его нет вовсе, и это штатное состояние, а не авария.

Отсюда контракт: методы возвращают AgentResult и НЕ бросают исключений. Их
зовут веб-роуты, где исключение превратилось бы в 500 вместо честного
«агент недоступен», и фоновая задача автовосстановления, где падение
остановило бы наблюдение ровно тогда, когда оно нужнее всего.

Документация API: см. спеку
docs/superpowers/specs/2026-08-20-bothost-agent-integration-design.md
"""
from __future__ import annotations

import logging
import os
import socket
from dataclasses import dataclass

import aiohttp

logger = logging.getLogger("shadow_logger.agent")

# Таймаут из примеров документации Bothost.
_TIMEOUT_SECONDS = 10

# Внутренний адрес в Docker-сети — предпочтительный по документации.
_INTERNAL_HOST = "agent"
_INTERNAL_PORT = 8000
_PUBLIC_FALLBACK = "http://agent.bothost.ru"

# Кеш адреса: в пределах жизни контейнера он не меняется, дёргать socket
# на каждый запрос незачем.
_cached_url: str | None = None


@dataclass
class AgentResult:
    """Результат обращения к агенту.

    ok=False означает «не получилось», а не «сломалось»: агента может не
    быть в принципе (локальная разработка), и вызывающий обязан это
    показать, а не упасть.
    """

    ok: bool
    data: dict | None = None
    error: str | None = None


def reset_cache() -> None:
    """Сбрасывает кеш адреса. Нужен тестам для изоляции."""
    global _cached_url
    _cached_url = None


def _can_connect(host: str, port: int, timeout: float = 1.0) -> bool:
    """Проверяет TCP-доступность. Вынесено отдельно, чтобы мокать в тестах."""
    try:
        socket.create_connection((host, port), timeout=timeout).close()
        return True
    except OSError:
        return False


async def resolve_agent_url() -> str:
    """Адрес агента по алгоритму из документации Bothost.

    Порядок: внутренний agent:8000 → BOTHOST_AGENT_URL → публичный дефолт.
    """
    global _cached_url
    if _cached_url is not None:
        return _cached_url

    if _can_connect(_INTERNAL_HOST, _INTERNAL_PORT):
        _cached_url = f"http://{_INTERNAL_HOST}:{_INTERNAL_PORT}"
    else:
        _cached_url = os.getenv("BOTHOST_AGENT_URL") or _PUBLIC_FALLBACK
    logger.info("Bothost agent URL: %s", _cached_url)
    return _cached_url


def _bot_id() -> str | None:
    return os.getenv("BOT_ID") or None


async def _request(method: str, path: str, **kwargs) -> AgentResult:
    """Один запрос к агенту с полной обработкой отказов.

    Любая сетевая проблема, таймаут или неразбираемый ответ превращаются в
    AgentResult(ok=False) с внятной причиной.
    """
    url = f"{await resolve_agent_url()}{path}"
    timeout = aiohttp.ClientTimeout(total=_TIMEOUT_SECONDS)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(method, url, **kwargs) as response:
                try:
                    payload = await response.json()
                except Exception:
                    text = await response.text()
                    return AgentResult(
                        ok=False,
                        error=f"агент вернул не JSON (HTTP {response.status}): {text[:120]}",
                    )
                if not isinstance(payload, dict):
                    return AgentResult(ok=False, error="агент вернул не объект")
                if not payload.get("ok", False):
                    return AgentResult(
                        ok=False,
                        data=payload,
                        error=payload.get("msg") or f"агент ответил ok=false (HTTP {response.status})",
                    )
                return AgentResult(ok=True, data=payload)
    except TimeoutError:
        return AgentResult(ok=False, error=f"таймаут {_TIMEOUT_SECONDS}s")
    except aiohttp.ClientError as e:
        return AgentResult(ok=False, error=f"агент недоступен: {type(e).__name__}")
    except Exception as e:
        logger.warning("agent request failed: %s: %s", type(e).__name__, e)
        return AgentResult(ok=False, error=f"{type(e).__name__}: {e}")


async def get_stats() -> AgentResult:
    """CPU, память и uptime контейнера."""
    bot_id = _bot_id()
    if not bot_id:
        return AgentResult(ok=False, error="BOT_ID не задан в окружении")
    return await _request("GET", f"/api/bots/{bot_id}/stats")


async def probe() -> AgentResult:
    """Диагностика: отвечает ли агент. Тот же вызов, что и stats."""
    return await get_stats()


async def get_logs(lines: int = 200) -> AgentResult:
    """Последние N строк логов контейнера."""
    bot_id = _bot_id()
    if not bot_id:
        return AgentResult(ok=False, error="BOT_ID не задан в окружении")
    return await _request(
        "POST", "/api/bots/logs", json={"bot_id": bot_id, "lines": lines},
    )


async def restart_self() -> AgentResult:
    """Самоперезапуск контейнера.

    Используется /api/bots/self/restart, а не общий /api/bots/restart:
    документация называет его безопасным для самоперезапуска, и он не
    требует передавать user_id.
    """
    bot_id = _bot_id()
    if not bot_id:
        return AgentResult(ok=False, error="BOT_ID не задан в окружении")
    return await _request(
        "POST", "/api/bots/self/restart", headers={"X-Bot-ID": bot_id},
    )
```

- [ ] **Step 4: Запустить тест — должен проходить**

Run: `uv run pytest tests/test_v510_bothost_agent.py -q`
Expected: 6 passed

- [ ] **Step 5: Дописать тесты на отказы**

Добавить в `tests/test_v510_bothost_agent.py` перед `if __name__`:

```python
class TestFailureHandling(_AgentCase):
    """Отказы превращаются в AgentResult, а не в исключения."""

    def _mock_session(self, *, json_value=None, json_raises=None,
                      text_value="", status=200, request_raises=None):
        """Подменяет aiohttp.ClientSession одним ответом."""
        response = MagicMock()
        response.status = status
        response.json = AsyncMock(
            side_effect=json_raises, return_value=json_value,
        )
        response.text = AsyncMock(return_value=text_value)
        response.__aenter__ = AsyncMock(return_value=response)
        response.__aexit__ = AsyncMock(return_value=False)

        session = MagicMock()
        if request_raises is not None:
            session.request = MagicMock(side_effect=request_raises)
        else:
            session.request = MagicMock(return_value=response)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        return session

    def test_unreachable_agent_returns_error(self):
        """Нет агента — честный ok=False, а не исключение наружу."""
        session = self._mock_session(
            request_raises=aiohttp.ClientConnectorError(MagicMock(), OSError()),
        )
        with patch.object(bothost_agent.aiohttp, "ClientSession", return_value=session):
            result = asyncio.run(bothost_agent.get_stats())
        self.assertFalse(result.ok)
        self.assertIsNotNone(result.error)

    def test_timeout_returns_error(self):
        session = self._mock_session(request_raises=TimeoutError())
        with patch.object(bothost_agent.aiohttp, "ClientSession", return_value=session):
            result = asyncio.run(bothost_agent.get_stats())
        self.assertFalse(result.ok)
        self.assertIn("таймаут", result.error)

    def test_non_json_response_returns_error(self):
        """HTML вместо JSON — типичный ответ прокси, а не агента."""
        session = self._mock_session(
            json_raises=ValueError("not json"), text_value="<html>502</html>", status=502,
        )
        with patch.object(bothost_agent.aiohttp, "ClientSession", return_value=session):
            result = asyncio.run(bothost_agent.get_stats())
        self.assertFalse(result.ok)
        self.assertIn("не JSON", result.error)

    def test_agent_says_not_ok(self):
        """Агент ответил ok=false — причину показываем как есть."""
        session = self._mock_session(
            json_value={"ok": False, "msg": "Контейнер не найден"},
        )
        with patch.object(bothost_agent.aiohttp, "ClientSession", return_value=session):
            result = asyncio.run(bothost_agent.get_stats())
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "Контейнер не найден")

    def test_successful_response_carries_data(self):
        session = self._mock_session(
            json_value={"ok": True, "stats": {"cpu_percent": 15.5}},
        )
        with patch.object(bothost_agent.aiohttp, "ClientSession", return_value=session):
            result = asyncio.run(bothost_agent.get_stats())
        self.assertTrue(result.ok)
        self.assertEqual(result.data["stats"]["cpu_percent"], 15.5)

    def test_restart_sends_bot_id_header(self):
        """X-Bot-ID обязателен по документации."""
        session = self._mock_session(json_value={"ok": True, "message": "перезапущен"})
        with patch.object(bothost_agent.aiohttp, "ClientSession", return_value=session):
            asyncio.run(bothost_agent.restart_self())
        kwargs = session.request.call_args.kwargs
        self.assertEqual(kwargs["headers"]["X-Bot-ID"], "bot_test_123")
```

Добавить импорт в шапку файла: `import aiohttp`.

- [ ] **Step 6: Запустить — все проходят**

Run: `uv run pytest tests/test_v510_bothost_agent.py -q`
Expected: 12 passed

- [ ] **Step 7: Полная сюита и линтер**

```bash
uv run ruff check .
```
Плюс сюита с `timeout=600000`. Expected: 73/73 PASS (добавился файл), ruff чист.

- [ ] **Step 8: Коммит**

```bash
git add bothost_agent.py tests/test_v510_bothost_agent.py
git commit -m "feat: обёртка над Bothost Agent API (v5.1.0)

Модуль инкапсулирует HTTP и определение адреса агента: внутренний
agent:8000 → BOTHOST_AGENT_URL → публичный дефолт, с кешем.

Методы не бросают исключений — возвращают AgentResult(ok, data, error).
Агента может не быть в принципе (локальная разработка, внешняя сеть), и это
штатное состояние: веб-роут должен показать «недоступен» вместо 500, а
фоновая задача не имеет права падать от сетевого сбоя.

Тесты на моках aiohttp: живого агента нет ни локально, ни в CI.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 2: Диагностический блок в Settings

Первое, что увидит владелец. Даёт факты о реальном агенте до того, как под
документацию написаны четыре функции.

**Files:**
- Modify: `web/admin_settings.py` (контекст страницы)
- Modify: `templates/admin_settings.html` (блок)
- Test: `tests/test_v510_bothost_agent.py`

**Interfaces:**
- Consumes: `bothost_agent.probe()`, `await bothost_agent.resolve_agent_url()` (Task 1; обе асинхронные)
- Produces: ключ `agent_info` в контексте шаблона `admin_settings.html`

- [ ] **Step 1: Написать падающий тест**

Добавить в `tests/test_v510_bothost_agent.py`:

```python
class TestSettingsAgentBlock(unittest.TestCase):
    """Страница Settings показывает состояние агента и не падает без него."""

    def setUp(self):
        os.environ["WEB_PASSWORD"] = "test-pwd"
        os.environ["WEB_ALLOW_NO_SECRET"] = "1"

    def test_agent_info_present_in_context(self):
        """Хелпер отдаёт словарь с адресом и доступностью."""
        import web.admin_settings as admin_settings

        info = asyncio.run(admin_settings._agent_info())
        for key in ("url", "available", "error", "raw"):
            self.assertIn(key, info)

    def test_unavailable_agent_does_not_raise(self):
        """Агента нет — это состояние, а не ошибка страницы."""
        import web.admin_settings as admin_settings

        info = asyncio.run(admin_settings._agent_info())
        self.assertIn(info["available"], (True, False))
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `uv run pytest tests/test_v510_bothost_agent.py -q -k Settings`
Expected: FAIL — `AttributeError: module 'web.admin_settings' has no attribute '_agent_info'`

- [ ] **Step 3: Добавить хелпер в `web/admin_settings.py`**

После функции `_bot_info` добавить:

```python
async def _agent_info() -> dict:
    """Состояние связи с Bothost Agent для блока диагностики.

    v5.1.0: агент доступен только изнутри Docker-сети Bothost. Локально его
    нет, и это нормально — блок покажет «недоступен», страница продолжит
    работать.

    Поле `raw` намеренно отдаёт сырой ответ: документация Bothost датирована
    2025 годом, и первое, что нужно увидеть на проде, — совпадает ли
    фактический формат с описанным.
    """
    import bothost_agent

    result = await bothost_agent.probe()
    return {
        "url": await bothost_agent.resolve_agent_url(),
        "bot_id": os.getenv("BOT_ID") or None,
        "available": result.ok,
        "error": result.error,
        "raw": result.data,
    }
```

Добавить в контекст страницы, рядом с `"bot_info": await _bot_info(),`:

```python
        "agent_info": await _agent_info(),
```

- [ ] **Step 4: Запустить тест — проходит**

Run: `uv run pytest tests/test_v510_bothost_agent.py -q -k Settings`
Expected: 2 passed

- [ ] **Step 5: Добавить блок в шаблон**

В `templates/admin_settings.html` после блока `bot-info` (перед
`<div id="database"`) вставить:

```html
{# ── Bothost Agent ────────────────────────────────────────────────────────── #}
<div id="agent" class="section-title" style="margin-top: var(--sp-6);">Bothost Agent <a class="anchor" href="#agent" style="color: var(--text-dim); font-weight: normal; text-transform: none; letter-spacing: 0;">#</a></div>
<div style="background: var(--bg2); border: 1px solid var(--border); border-radius: var(--r-md); padding: var(--sp-4) var(--sp-5); margin-bottom: var(--sp-4); margin-left: var(--sp-4); border-left: 3px solid {% if agent_info.available %}var(--ok){% else %}var(--text-dim){% endif %};">
    <div style="font-size: 11px; color: var(--text-dim); margin-bottom: var(--sp-3); line-height: 1.7;">
        Служебный API хостинга: логи, статистика контейнера и перезапуск.
        Доступен только изнутри Docker-сети Bothost — вне контейнера
        показывает «недоступен», и это нормально.
    </div>
    <table style="width: 100%; font-size: 12px; font-family: var(--font-mono);">
        <tr><td style="color: var(--text-dim); padding: 4px 0;">URL</td><td>{{ agent_info.url }}</td></tr>
        <tr><td style="color: var(--text-dim); padding: 4px 0;">BOT_ID</td><td>{{ agent_info.bot_id or '—' }}</td></tr>
        <tr><td style="color: var(--text-dim); padding: 4px 0;">Статус</td>
            <td>{% if agent_info.available %}<span style="color: var(--ok);">● отвечает</span>{% else %}<span style="color: var(--text-dim);">○ недоступен</span>{% endif %}</td></tr>
        {% if agent_info.error %}
        <tr><td style="color: var(--text-dim); padding: 4px 0;">Причина</td><td style="color: var(--warn);">{{ agent_info.error }}</td></tr>
        {% endif %}
    </table>
    {% if agent_info.raw %}
    <div style="font-size: 11px; color: var(--text-dim); margin-top: var(--sp-3); margin-bottom: 4px;">Сырой ответ агента:</div>
    <pre style="background: var(--bg); border: 1px solid var(--border); border-radius: var(--r-sm); padding: var(--sp-3); font-size: 11px; overflow-x: auto; margin: 0;">{{ agent_info.raw | tojson(indent=2) }}</pre>
    {% endif %}
</div>
```

- [ ] **Step 6: Проверить рендер страницы**

```bash
uv run pytest tests/test_v486_settings_render.py -q
```
Expected: PASS — страница рендерится с новым блоком.

- [ ] **Step 7: Полная сюита и линтер**

Сюита с `timeout=600000`, затем `uv run ruff check .`.
Expected: 73/73 PASS, ruff чист.

- [ ] **Step 8: Коммит**

```bash
git add web/admin_settings.py templates/admin_settings.html tests/test_v510_bothost_agent.py
git commit -m "feat: блок диагностики Bothost Agent в Settings (v5.1.0)

Показывает выбранный URL, BOT_ID, отвечает ли агент и сырой ответ на stats.

Сырой ответ нужен намеренно: документация Bothost датирована 2025 годом, и
прежде чем писать под неё логи, статистику и перезапуск, стоит увидеть
фактический формат. Блок только читает и доступен только SU.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

- [ ] **Step 9: ПАУЗА — показать блок владельцу**

Сообщить владельцу: после деплоя открыть `/admin/settings#agent`, посмотреть
статус и сырой ответ, прислать его. Задачи 3–7 пишутся под фактический
формат ответа, а не под документацию.

Если формат совпал с документацией — продолжать без изменений.

---

## Task 3: Роуты `/api/self/*` — статистика

**Files:**
- Create: `web/self_admin.py`
- Modify: `web_app.py` (регистрация роутера внутри `create_app()`)
- Test: `tests/test_v510_bothost_agent.py`

**Interfaces:**
- Consumes: `bothost_agent.get_stats()` (Task 1), `web.deps.require_su`
- Produces: `GET /api/self/stats`; модуль `web/self_admin.py` с `router`

- [ ] **Step 1: Написать падающий тест**

```python
class TestSelfStatsRoute(unittest.TestCase):
    """GET /api/self/stats — статистика контейнера с кешем."""

    def setUp(self):
        os.environ["WEB_PASSWORD"] = "test-pwd"
        os.environ["WEB_ALLOW_NO_SECRET"] = "1"
        import web.self_admin as self_admin
        self_admin.reset_cache()

    def _client(self):
        from fastapi.testclient import TestClient
        import web_app
        return TestClient(web_app.create_app())

    def test_requires_auth(self):
        """Без куки — редирект на логин, а не данные контейнера."""
        r = self._client().get("/api/self/stats", follow_redirects=False)
        self.assertIn(r.status_code, (302, 303))

    def test_returns_unavailable_without_agent(self):
        """Агента нет — понятный ответ, а не 500."""
        import bothost_agent
        import web.self_admin as self_admin

        with patch.object(
            self_admin.bothost_agent, "get_stats",
            AsyncMock(return_value=bothost_agent.AgentResult(
                ok=False, error="агент недоступен: ClientConnectorError")),
        ):
            payload = asyncio.run(self_admin._collect_stats())
        self.assertFalse(payload["ok"])
        self.assertIn("недоступен", payload["error"])

    def test_caches_for_ten_seconds(self):
        """Второй запрос подряд не идёт к агенту.

        Дашборд обновляется раз в 30 секунд; несколько открытых вкладок без
        кеша превратились бы в поток запросов.
        """
        import bothost_agent
        import web.self_admin as self_admin

        call = AsyncMock(return_value=bothost_agent.AgentResult(
            ok=True, data={"ok": True, "stats": {"cpu_percent": 1.0}}))
        with patch.object(self_admin.bothost_agent, "get_stats", call):
            asyncio.run(self_admin._collect_stats())
            asyncio.run(self_admin._collect_stats())
        self.assertEqual(call.await_count, 1)
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `uv run pytest tests/test_v510_bothost_agent.py -q -k SelfStats`
Expected: FAIL — `ModuleNotFoundError: No module named 'web.self_admin'`

- [ ] **Step 3: Создать `web/self_admin.py`**

```python
"""
web/self_admin.py — роуты управления собственным контейнером (v5.1.0).

Обращаются к Bothost Agent через bothost_agent. Агент доступен только
изнутри Docker-сети хостинга; когда его нет, роуты отдают понятное
состояние, а не ошибку — панель обязана работать и без него.

Все роуты только для SU: логи и статистика раскрывают внутренности
контейнера, перезапуск — разрушительное действие.
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends

import bothost_agent
from web.deps import AuthUser, require_su

router = APIRouter()

# Кеш статистики: дашборд обновляется раз в 30 секунд, и несколько открытых
# вкладок без кеша дали бы поток запросов к агенту.
_STATS_TTL_SECONDS = 10
_stats_cache: dict = {"at": 0.0, "payload": None}


def reset_cache() -> None:
    """Сбрасывает кеш статистики. Нужен тестам."""
    _stats_cache["at"] = 0.0
    _stats_cache["payload"] = None


async def _collect_stats() -> dict:
    """Статистика контейнера с кешем на 10 секунд."""
    now = time.time()
    if _stats_cache["payload"] is not None and now - _stats_cache["at"] < _STATS_TTL_SECONDS:
        return _stats_cache["payload"]

    result = await bothost_agent.get_stats()
    payload = {
        "ok": result.ok,
        "stats": (result.data or {}).get("stats") if result.ok else None,
        "error": result.error,
    }
    _stats_cache["at"] = now
    _stats_cache["payload"] = payload
    return payload


@router.get("/api/self/stats")
async def self_stats(_auth: AuthUser = Depends(require_su)):
    """Статистика контейнера: CPU, память, uptime."""
    return await _collect_stats()
```

- [ ] **Step 4: Зарегистрировать роутер в `create_app()`**

В `web_app.py`, в блоке late-импортов роутеров, добавить в алфавитном порядке:

```python
    from web.self_admin import router as self_admin_router
```

и рядом с остальными:

```python
    app.include_router(self_admin_router)
```

- [ ] **Step 5: Обновить эталон числа роутов**

В `tests/test_v490_decomposition.py` заменить:

```python
_EXPECTED_ROUTES = 55
```

на:

```python
# v5.1.0: +1 роут /api/self/stats (интеграция с Bothost Agent).
_EXPECTED_ROUTES = 56
```

- [ ] **Step 6: Запустить тесты**

Run: `uv run pytest tests/test_v510_bothost_agent.py tests/test_v490_decomposition.py -q`
Expected: все проходят.

- [ ] **Step 7: Полная сюита и линтер**

Сюита с `timeout=600000`, `uv run ruff check .`.
Expected: 73/73 PASS, ruff чист.

- [ ] **Step 8: Коммит**

```bash
git add web/self_admin.py web_app.py tests/
git commit -m "feat: GET /api/self/stats — статистика контейнера (5.0.0-04, часть 1)

Роут отдаёт CPU, память и uptime из Bothost Agent, только для SU. Кеш на
10 секунд: дашборд обновляется раз в 30 секунд, и несколько открытых вкладок
без кеша дали бы поток запросов к агенту.

Без агента роут отвечает понятным ok=false, а не 500.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 4: Логи контейнера

**Files:**
- Modify: `web/self_admin.py`
- Modify: `templates/admin_settings.html`
- Test: `tests/test_v510_bothost_agent.py`

**Interfaces:**
- Consumes: `bothost_agent.get_logs(lines)` (Task 1)
- Produces: `POST /api/self/logs`

- [ ] **Step 1: Написать падающий тест**

```python
class TestSelfLogsRoute(unittest.TestCase):
    """POST /api/self/logs — последние строки логов контейнера."""

    def setUp(self):
        os.environ["WEB_PASSWORD"] = "test-pwd"
        os.environ["WEB_ALLOW_NO_SECRET"] = "1"

    def test_returns_logs_from_agent(self):
        import bothost_agent
        import web.self_admin as self_admin

        with patch.object(
            self_admin.bothost_agent, "get_logs",
            AsyncMock(return_value=bothost_agent.AgentResult(
                ok=True, data={"ok": True, "logs": "строка один\nстрока два"})),
        ):
            payload = asyncio.run(self_admin._collect_logs(50))
        self.assertTrue(payload["ok"])
        self.assertIn("строка один", payload["logs"])

    def test_unavailable_agent_returns_error(self):
        import bothost_agent
        import web.self_admin as self_admin

        with patch.object(
            self_admin.bothost_agent, "get_logs",
            AsyncMock(return_value=bothost_agent.AgentResult(
                ok=False, error="таймаут 10s")),
        ):
            payload = asyncio.run(self_admin._collect_logs(50))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "таймаут 10s")

    def test_lines_parameter_is_clamped(self):
        """Запрос на миллион строк не должен вешать агента и браузер."""
        import bothost_agent
        import web.self_admin as self_admin

        call = AsyncMock(return_value=bothost_agent.AgentResult(
            ok=True, data={"ok": True, "logs": ""}))
        with patch.object(self_admin.bothost_agent, "get_logs", call):
            asyncio.run(self_admin._collect_logs(1_000_000))
        self.assertLessEqual(call.await_args.args[0], 1000)
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `uv run pytest tests/test_v510_bothost_agent.py -q -k SelfLogs`
Expected: FAIL — `AttributeError: module 'web.self_admin' has no attribute '_collect_logs'`

- [ ] **Step 3: Добавить в `web/self_admin.py`**

```python
# Потолок строк: запрос на миллион повесил бы и агента, и браузер.
_MAX_LOG_LINES = 1000


async def _collect_logs(lines: int) -> dict:
    """Логи контейнера, число строк ограничено сверху."""
    safe_lines = max(1, min(int(lines), _MAX_LOG_LINES))
    result = await bothost_agent.get_logs(safe_lines)
    return {
        "ok": result.ok,
        "logs": (result.data or {}).get("logs", "") if result.ok else "",
        "error": result.error,
    }


@router.post("/api/self/logs")
async def self_logs(
    lines: int = 200,
    _auth: AuthUser = Depends(require_csrf_su),
):
    """Последние строки логов контейнера."""
    return await _collect_logs(lines)
```

Добавить `require_csrf_su` в импорт из `web.deps`.

- [ ] **Step 4: Обновить эталон роутов**

В `tests/test_v490_decomposition.py`: `_EXPECTED_ROUTES = 57` с комментарием
про `/api/self/logs`.

- [ ] **Step 5: Запустить тесты**

Run: `uv run pytest tests/test_v510_bothost_agent.py tests/test_v490_decomposition.py -q`
Expected: все проходят.

- [ ] **Step 6: Добавить окно логов в шаблон**

В `templates/admin_settings.html` внутри блока `#agent`, перед закрывающим
`</div>` блока, вставить:

```html
    <div style="margin-top: var(--sp-4);">
        <div style="display: flex; gap: var(--sp-3); align-items: center; margin-bottom: var(--sp-3);">
            <button type="button" id="logs-refresh" {% if not agent_info.available %}disabled{% endif %}
                    style="padding: 6px 14px; background: transparent; border: 1px solid var(--border); border-radius: var(--r-sm); color: var(--text); font-family: var(--font-mono); font-size: 11px; cursor: pointer;">
                Обновить логи
            </button>
            <label style="font-size: 11px; color: var(--text-dim); display: flex; align-items: center; gap: 6px;">
                <input type="checkbox" id="logs-autoscroll" checked> автопрокрутка
            </label>
            <button type="button" id="logs-clear"
                    style="padding: 6px 14px; background: transparent; border: 1px solid var(--border); border-radius: var(--r-sm); color: var(--text-dim); font-family: var(--font-mono); font-size: 11px; cursor: pointer;">
                Очистить экран
            </button>
        </div>
        <pre id="logs-window" style="height: 350px; overflow-y: auto; background: var(--bg); border: 1px solid var(--border); border-radius: var(--r-sm); padding: var(--sp-3); font-size: 11px; margin: 0; white-space: pre-wrap;">{% if not agent_info.available %}Агент недоступен — логи не читаются.{% else %}Нажмите «Обновить логи».{% endif %}</pre>
    </div>
    <script>
    (function () {
        const win = document.getElementById('logs-window');
        const refresh = document.getElementById('logs-refresh');
        const autoscroll = document.getElementById('logs-autoscroll');
        const clear = document.getElementById('logs-clear');
        if (!refresh) return;
        refresh.addEventListener('click', async function () {
            refresh.disabled = true;
            try {
                const body = new FormData();
                body.append('csrf_token', '{{ csrf_token }}');
                const r = await fetch('/api/self/logs?lines=200', {method: 'POST', body: body});
                const data = await r.json();
                win.textContent = data.ok ? (data.logs || '(пусто)') : ('Ошибка: ' + data.error);
                if (autoscroll.checked) win.scrollTop = win.scrollHeight;
            } catch (e) {
                win.textContent = 'Ошибка запроса: ' + e;
            } finally {
                refresh.disabled = false;
            }
        });
        clear.addEventListener('click', function () { win.textContent = ''; });
    })();
    </script>
```

- [ ] **Step 7: Полная сюита и линтер**

Сюита с `timeout=600000`, `uv run ruff check .`.
Expected: 73/73 PASS, ruff чист.

- [ ] **Step 8: Коммит**

```bash
git add web/self_admin.py templates/admin_settings.html tests/
git commit -m "feat: окно логов контейнера в Settings (5.0.0-03)

POST /api/self/logs отдаёт последние строки из Bothost Agent, только для SU
и с CSRF-токеном. Число строк ограничено сверху: запрос на миллион повесил
бы и агента, и браузер.

В панели — окно с моно-шрифтом, автопрокруткой и кнопкой очистки. Без
агента кнопка неактивна, а окно объясняет причину.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 5: Кнопка перезапуска и дашборд здоровья

**Files:**
- Modify: `web/self_admin.py`
- Modify: `templates/admin_settings.html`
- Test: `tests/test_v510_bothost_agent.py`

**Interfaces:**
- Consumes: `bothost_agent.restart_self()` (Task 1), `_collect_stats()` (Task 3)
- Produces: `POST /api/self/restart`

- [ ] **Step 1: Написать падающий тест**

```python
class TestSelfRestartRoute(unittest.TestCase):
    """POST /api/self/restart — ручной перезапуск контейнера."""

    def setUp(self):
        os.environ["WEB_PASSWORD"] = "test-pwd"
        os.environ["WEB_ALLOW_NO_SECRET"] = "1"

    def test_calls_agent_restart(self):
        import bothost_agent
        import web.self_admin as self_admin

        call = AsyncMock(return_value=bothost_agent.AgentResult(
            ok=True, data={"ok": True, "message": "Бот перезапущен"}))
        with patch.object(self_admin.bothost_agent, "restart_self", call):
            payload = asyncio.run(self_admin._do_restart())
        call.assert_awaited_once()
        self.assertTrue(payload["ok"])

    def test_unavailable_agent_reports_error(self):
        """Перезапуск не состоялся — говорим об этом прямо."""
        import bothost_agent
        import web.self_admin as self_admin

        with patch.object(
            self_admin.bothost_agent, "restart_self",
            AsyncMock(return_value=bothost_agent.AgentResult(
                ok=False, error="агент недоступен: ClientConnectorError")),
        ):
            payload = asyncio.run(self_admin._do_restart())
        self.assertFalse(payload["ok"])
        self.assertIn("недоступен", payload["error"])
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `uv run pytest tests/test_v510_bothost_agent.py -q -k SelfRestart`
Expected: FAIL — нет `_do_restart`

- [ ] **Step 3: Добавить в `web/self_admin.py`**

```python
async def _do_restart() -> dict:
    """Просит агента перезапустить контейнер.

    Разрушительное действие, поэтому вызывается только из роута под
    require_csrf_su и после подтверждения в интерфейсе.
    """
    result = await bothost_agent.restart_self()
    return {
        "ok": result.ok,
        "message": (result.data or {}).get("message") if result.ok else None,
        "error": result.error,
    }


@router.post("/api/self/restart")
async def self_restart(_auth: AuthUser = Depends(require_csrf_su)):
    """Перезапуск собственного контейнера."""
    return await _do_restart()
```

- [ ] **Step 4: Обновить эталон роутов**

`_EXPECTED_ROUTES = 58` с комментарием про `/api/self/restart`.

- [ ] **Step 5: Запустить тесты**

Run: `uv run pytest tests/test_v510_bothost_agent.py tests/test_v490_decomposition.py -q`
Expected: все проходят.

- [ ] **Step 6: Добавить дашборд и кнопку в шаблон**

В блок `#agent`, после таблицы состояния, вставить:

```html
    <div id="agent-health" style="margin-top: var(--sp-4); font-size: 12px; font-family: var(--font-mono); color: var(--text-dim);">
        {% if agent_info.available %}Загрузка статистики…{% else %}Статистика недоступна — агент не отвечает.{% endif %}
    </div>
    <form method="post" action="/api/self/restart" style="margin-top: var(--sp-4);"
          onsubmit="return confirm('Перезапустить бота? Он будет недоступен несколько секунд.');">
        {{ csrf_field() }}
        <button type="submit" {% if not agent_info.available %}disabled title="Агент недоступен"{% endif %}
                style="padding: 10px 20px; background: transparent; border: 1px solid var(--warn); border-radius: var(--r-sm); color: var(--warn); font-family: var(--font-mono); font-size: 11px; text-transform: uppercase; letter-spacing: 2px; font-weight: 700; cursor: pointer;">
            ⟳ Перезапустить бота
        </button>
    </form>
    <script>
    (function () {
        const box = document.getElementById('agent-health');
        if (!box || !{{ 'true' if agent_info.available else 'false' }}) return;
        async function load() {
            try {
                const r = await fetch('/api/self/stats');
                const data = await r.json();
                if (!data.ok) { box.textContent = 'Ошибка: ' + data.error; return; }
                const s = data.stats || {};
                box.textContent = 'CPU ' + (s.cpu_percent ?? '—') + '% · память ' +
                    (s.memory_usage ?? '—') + ' (' + (s.memory_percent ?? '—') + '%) · uptime ' +
                    (s.uptime ?? '—');
            } catch (e) { box.textContent = 'Ошибка запроса: ' + e; }
        }
        load();
        setInterval(load, 30000);
    })();
    </script>
```

- [ ] **Step 7: Полная сюита и линтер**

Сюита с `timeout=600000`, `uv run ruff check .`.
Expected: 73/73 PASS, ruff чист.

- [ ] **Step 8: Коммит**

```bash
git add web/self_admin.py templates/admin_settings.html tests/
git commit -m "feat: кнопка перезапуска и дашборд здоровья контейнера (5.0.0-02, -04)

POST /api/self/restart зовёт /api/bots/self/restart с заголовком X-Bot-ID —
документация называет его безопасным для самоперезапуска, и он не требует
передавать user_id. Роут под require_csrf_su, кнопка спрашивает
подтверждение; без агента она неактивна с пояснением.

Дашборд показывает CPU, память и uptime, обновляется раз в 30 секунд и
опирается на кеш роута статистики.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 6: Self-healing за флагом

Автоматика, способная перезапустить прод. По умолчанию выключена.

**Files:**
- Create: `tests/test_v510_self_healing.py`
- Modify: `bot.py` (фоновая задача)
- Modify: `bot_handlers.py` (алерт SU о перезапуске)

**Interfaces:**
- Consumes: `bothost_agent.restart_self()` (Task 1),
  `health_probe.memory_limit_bytes()`, `health_probe.snapshot()`
- Produces: `_self_healing_loop()` в `bot.py`;
  `send_restart_alert_to_su(bot, *, reason: str)` в `bot_handlers.py`

- [ ] **Step 1: Написать падающий тест**

Создать `tests/test_v510_self_healing.py`:

```python
"""
test_v510_self_healing.py — автоперезапуск контейнера (roadmap 5.0.0-05).

Самая опасная функция релиза: она способна перезапустить прод без участия
человека. Ошибка в пороге или в чтении статистики уводит контейнер в цикл
перезапусков, и бот перестаёт обрабатывать сообщения вовсе.

Поэтому здесь проверяется не только «срабатывает когда надо», но и —
подробнее — «молчит когда не надо»: выключен по умолчанию, не реагирует на
неизвестный лимит памяти, соблюдает потолок перезапусков.
"""
from __future__ import annotations

import os
import sys
import unittest

from _paths import _P

os.environ.setdefault("BOT_TOKEN", "1:test")
os.environ.setdefault("ADMIN_IDS", "1")

sys.path.insert(0, _P())

import self_healing  # noqa: E402


class _HealingCase(unittest.TestCase):
    def setUp(self):
        self._prev = os.environ.get("SELF_HEALING_ENABLED")
        self_healing.reset_state()

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("SELF_HEALING_ENABLED", None)
        else:
            os.environ["SELF_HEALING_ENABLED"] = self._prev
        self_healing.reset_state()


class TestDisabledByDefault(_HealingCase):

    def test_disabled_when_env_absent(self):
        """Без переменной автоматика молчит.

        Прод получает код раньше, чем решение его включить.
        """
        os.environ.pop("SELF_HEALING_ENABLED", None)
        self.assertFalse(self_healing.is_enabled())

    def test_disabled_when_zero(self):
        os.environ["SELF_HEALING_ENABLED"] = "0"
        self.assertFalse(self_healing.is_enabled())

    def test_enabled_only_on_explicit_one(self):
        os.environ["SELF_HEALING_ENABLED"] = "1"
        self.assertTrue(self_healing.is_enabled())


class TestRestartDecision(_HealingCase):

    def test_high_memory_triggers(self):
        reason = self_healing.decide(memory_percent=92.0, tg_unreachable_seconds=0)
        self.assertEqual(reason, "high_memory")

    def test_unknown_memory_never_triggers(self):
        """Лимит контейнера не прочитан — молчим.

        Иначе на хосте без cgroup-лимита автоматика считала бы проценты от
        памяти всей машины и перезапускала бота на ровном месте.
        """
        self.assertIsNone(
            self_healing.decide(memory_percent=None, tg_unreachable_seconds=0),
        )

    def test_moderate_memory_does_not_trigger(self):
        self.assertIsNone(
            self_healing.decide(memory_percent=80.0, tg_unreachable_seconds=0),
        )

    def test_long_telegram_outage_triggers(self):
        reason = self_healing.decide(memory_percent=10.0, tg_unreachable_seconds=400)
        self.assertEqual(reason, "tg_api_unreachable")

    def test_short_telegram_outage_does_not_trigger(self):
        """Минутный сбой связи — не повод рвать контейнер."""
        self.assertIsNone(
            self_healing.decide(memory_percent=10.0, tg_unreachable_seconds=60),
        )


class TestRestartBudget(_HealingCase):

    def test_allows_first_restarts(self):
        for _ in range(3):
            self.assertTrue(self_healing.budget_allows())
            self_healing.record_restart()

    def test_blocks_fourth_restart_in_window(self):
        """Потолок 3 за 10 минут: иначе цикл перезапусков.

        Если причина не устраняется рестартом, бот бесконечно перезагружался
        бы, не обрабатывая сообщения вовсе.
        """
        for _ in range(3):
            self_healing.record_restart()
        self.assertFalse(self_healing.budget_allows())

    def test_budget_recovers_after_window(self):
        for _ in range(3):
            self_healing.record_restart()
        self_healing.reset_state()
        self.assertTrue(self_healing.budget_allows())


if __name__ == "__main__":
    unittest.main(verbosity=2)
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `uv run pytest tests/test_v510_self_healing.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'self_healing'`

- [ ] **Step 3: Создать `self_healing.py`**

```python
"""
self_healing.py — решение о перезапуске контейнера (roadmap 5.0.0-05).

Модуль отвечает только за РЕШЕНИЕ: пора ли перезапускаться и не исчерпан ли
лимит попыток. Сам перезапуск делает bothost_agent, вызов — фоновая задача в
bot.py. Разделение нужно, чтобы логику порогов можно было проверить тестами
без сети и без контейнера.

Выключено по умолчанию: включает только SELF_HEALING_ENABLED=1. Автоматика,
способная перезапустить прод, не должна активироваться самим фактом деплоя.
"""
from __future__ import annotations

import os
import time

# Пороги из roadmap.md, 5.0.0-05.
_MEMORY_RESTART_PERCENT = 90.0
_TG_UNREACHABLE_SECONDS = 300

# Потолок перезапусков: если причина не устраняется рестартом, бот
# бесконечно перезагружался бы, не обрабатывая сообщения вовсе.
_MAX_RESTARTS = 3
_BUDGET_WINDOW_SECONDS = 600

_restarts: list[float] = []


def reset_state() -> None:
    """Очищает историю перезапусков. Нужен тестам."""
    _restarts.clear()


def is_enabled() -> bool:
    """Автоматика включается только явной единицей."""
    return os.getenv("SELF_HEALING_ENABLED") == "1"


def decide(*, memory_percent: float | None, tg_unreachable_seconds: float) -> str | None:
    """Причина перезапуска или None.

    memory_percent=None означает «лимит контейнера неизвестен» — в этом
    случае условие по памяти не применяется вовсе. Иначе на хосте без
    cgroup-лимита проценты считались бы от памяти всей машины, и бот
    перезапускался бы на ровном месте.
    """
    if memory_percent is not None and memory_percent > _MEMORY_RESTART_PERCENT:
        return "high_memory"
    if tg_unreachable_seconds >= _TG_UNREACHABLE_SECONDS:
        return "tg_api_unreachable"
    return None


def budget_allows() -> bool:
    """Не исчерпан ли лимит перезапусков за окно."""
    now = time.time()
    recent = [t for t in _restarts if now - t < _BUDGET_WINDOW_SECONDS]
    _restarts[:] = recent
    return len(recent) < _MAX_RESTARTS


def record_restart() -> None:
    """Фиксирует перезапуск для учёта лимита."""
    _restarts.append(time.time())
```

- [ ] **Step 4: Запустить тесты — проходят**

Run: `uv run pytest tests/test_v510_self_healing.py -q`
Expected: 11 passed

- [ ] **Step 5: Добавить алерт о перезапуске**

В `bot_handlers.py`, рядом с `send_latency_alert_to_su`, добавить:

```python
async def send_restart_alert_to_su(bot: types.Bot, *, reason: str) -> None:
    """Сообщает SU, что автоматика перезапускает контейнер.

    Молчаливый рестарт прода недопустим: владелец должен узнать о нём из
    личных сообщений, а не из графика простоя.
    """
    su_tg_ids: set[int] = set(ADMIN_IDS)
    try:
        async with async_session() as session:
            su_wus = (await session.execute(
                select(WebUser).where(WebUser.role == "su", WebUser.is_active.is_(True))
            )).scalars().all()
            for wu in su_wus:
                if wu.tg_user_id:
                    su_tg_ids.add(wu.tg_user_id)
    except Exception as e:
        logger.warning("send_restart_alert_to_su: cannot read WebUser: %s", e)

    human = {
        "high_memory": "память контейнера выше 90% лимита",
        "tg_api_unreachable": "Telegram недоступен дольше пяти минут",
    }.get(reason, reason)

    text = (
        "♻️ <b>Автоматический перезапуск бота</b>\n\n"
        f"Причина: <b>{human}</b>\n\n"
        "Перезапуск инициирован автоматикой самовосстановления. "
        "Если это повторяется — загляни в <code>/healthz</code> и логи контейнера."
    )
    for su_id in su_tg_ids:
        try:
            await tg_safe_call(
                lambda sid=su_id: bot.send_message(
                    chat_id=sid, text=text, parse_mode="HTML",
                ),
                label="restart_alert_su",
            )
        except Exception as e:
            logger.warning(
                "send_restart_alert_to_su: failed to DM su_id=%s: %s", su_id, e,
            )
```

- [ ] **Step 6: Добавить фоновую задачу в `bot.py`**

Рядом с `_health_probe_loop` добавить:

```python
async def _self_healing_loop():
    """Background loop: автоперезапуск контейнера при деградации.

    v5.1.0 (roadmap 5.0.0-05). Выключено по умолчанию — включает только
    SELF_HEALING_ENABLED=1. Автоматика, способная перезапустить прод, не
    должна активироваться самим фактом деплоя.

    Решение о перезапуске принимает self_healing (там пороги и лимит
    попыток), сам перезапуск делает bothost_agent.
    """
    await asyncio.sleep(60)
    if not self_healing.is_enabled():
        logger.info("Self-healing disabled (SELF_HEALING_ENABLED != 1)")
        return
    logger.warning("Self-healing ENABLED — контейнер может перезапускаться автоматически")

    while True:
        try:
            snap = health_probe.snapshot()
            limit = health_probe.memory_limit_bytes()
            rss = health_probe.memory_rss_bytes()
            memory_percent = round(rss / limit * 100, 1) if limit else None

            unreachable_for = 0.0
            if snap["telegram_connected"] is False and snap["checked_at"]:
                unreachable_for = time.time() - snap["checked_at"]

            reason = self_healing.decide(
                memory_percent=memory_percent,
                tg_unreachable_seconds=unreachable_for,
            )
            if reason:
                if not self_healing.budget_allows():
                    logger.error(
                        "Self-healing: лимит перезапусков исчерпан, причина=%s. "
                        "Требуется ручное вмешательство.", reason,
                    )
                    await send_restart_alert_to_su(bot, reason="budget_exhausted")
                else:
                    logger.warning("Self-healing: перезапуск, причина=%s", reason)
                    await send_restart_alert_to_su(bot, reason=reason)
                    self_healing.record_restart()
                    result = await bothost_agent.restart_self()
                    if not result.ok:
                        logger.error("Self-healing: перезапуск не удался: %s", result.error)
        except Exception as e:
            logger.error("Self-healing tick error: %s", e)
        await asyncio.sleep(60)
```

Добавить импорты в шапку `bot.py`: `import bothost_agent`, `import self_healing`,
`import time`, и `send_restart_alert_to_su` в список из `bot_handlers`.

Зарегистрировать задачу в `TaskGroup` рядом с `health_task` и **обязательно
добавить в `bg_tasks`** — забыть значит подвесить shutdown:

```python
            healing_task = tg.create_task(
                _self_healing_loop(), name="self_healing_loop",
            )
```

```python
            bg_tasks = [night_task, health_task, healing_task]
```

- [ ] **Step 7: Дописать тест на проводку**

```python
class TestLoopWiring(unittest.TestCase):
    """Задача должна быть зарегистрирована и корректно гаситься."""

    def test_loop_registered_and_cancelled(self):
        from pathlib import Path
        src = Path(_P("bot.py")).read_text(encoding="utf-8")
        self.assertIn("_self_healing_loop", src)
        self.assertRegex(
            src, r"bg_tasks\s*=\s*\[[^\]]*healing_task",
            "healing_task не в bg_tasks — shutdown подвиснет",
        )

    def test_alert_function_importable(self):
        """Регресс v5.0.0: вызов добавили, импорт забыли — NameError в проде."""
        import bot
        self.assertTrue(hasattr(bot, "send_restart_alert_to_su"))
```

- [ ] **Step 8: Полная сюита и линтер**

Сюита с `timeout=600000`, `uv run ruff check .`.
Expected: 74/74 PASS, ruff чист.

- [ ] **Step 9: Коммит**

```bash
git add self_healing.py bot.py bot_handlers.py tests/test_v510_self_healing.py
git commit -m "feat: автоперезапуск контейнера за флагом (5.0.0-05)

Выключено по умолчанию: включает только SELF_HEALING_ENABLED=1. На проде
владелец заранее выставил 0, поэтому деплой поведения не меняет.

Решение о перезапуске вынесено в self_healing.py — пороги и лимит попыток
проверяются тестами без сети и контейнера. Условия: память выше 90% лимита
либо Telegram недоступен дольше пяти минут. Потолок — 3 перезапуска за 10
минут, дальше стоп и алерт SU: если причина не устраняется рестартом, бот
бесконечно перезагружался бы, не обрабатывая сообщения.

Неизвестный лимит памяти (cgroup не прочитан) условие по памяти отключает —
иначе проценты считались бы от памяти всей машины.

Каждый перезапуск сопровождается алертом SU с причиной: молчаливый рестарт
прода недопустим.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 7: Авто-webhook за флагом

**Files:**
- Modify: `bot.py` (выбор канала при старте)
- Test: `tests/test_v510_self_healing.py`

**Interfaces:**
- Produces: `resolve_webhook_url() -> str` в `bot.py`

- [ ] **Step 1: Написать падающий тест**

```python
class TestWebhookMode(unittest.TestCase):
    """BOT_WEBHOOK_MODE: off по умолчанию, auto собирает URL из DOMAIN."""

    def setUp(self):
        self._prev = {k: os.environ.get(k) for k in
                      ("BOT_WEBHOOK_MODE", "WEBHOOK_URL", "DOMAIN")}

    def tearDown(self):
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_off_keeps_current_behaviour(self):
        """off — ровно то, что было до v5.1.0: только явный WEBHOOK_URL."""
        import bot
        os.environ["BOT_WEBHOOK_MODE"] = "off"
        os.environ["WEBHOOK_URL"] = ""
        os.environ["DOMAIN"] = "mybot.bothost.ru"
        self.assertEqual(bot.resolve_webhook_url(), "")

    def test_off_is_the_default(self):
        """Без переменной поведение не меняется."""
        import bot
        os.environ.pop("BOT_WEBHOOK_MODE", None)
        os.environ["WEBHOOK_URL"] = ""
        os.environ["DOMAIN"] = "mybot.bothost.ru"
        self.assertEqual(bot.resolve_webhook_url(), "")

    def test_auto_builds_from_domain(self):
        import bot
        os.environ["BOT_WEBHOOK_MODE"] = "auto"
        os.environ["WEBHOOK_URL"] = ""
        os.environ["DOMAIN"] = "mybot.bothost.ru"
        self.assertEqual(
            bot.resolve_webhook_url(), "https://mybot.bothost.ru/webhook",
        )

    def test_explicit_url_wins_over_domain(self):
        """Заданный вручную адрес важнее автоопределения."""
        import bot
        os.environ["BOT_WEBHOOK_MODE"] = "auto"
        os.environ["WEBHOOK_URL"] = "https://custom.example/webhook"
        os.environ["DOMAIN"] = "mybot.bothost.ru"
        self.assertEqual(
            bot.resolve_webhook_url(), "https://custom.example/webhook",
        )

    def test_auto_without_domain_falls_back_to_polling(self):
        """DOMAIN не задан — пустая строка, значит long polling."""
        import bot
        os.environ["BOT_WEBHOOK_MODE"] = "auto"
        os.environ["WEBHOOK_URL"] = ""
        os.environ.pop("DOMAIN", None)
        self.assertEqual(bot.resolve_webhook_url(), "")
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `uv run pytest tests/test_v510_self_healing.py -q -k Webhook`
Expected: FAIL — нет `resolve_webhook_url`

- [ ] **Step 3: Добавить функцию в `bot.py`**

Рядом с определением `WEBHOOK_URL`:

```python
def resolve_webhook_url() -> str:
    """Адрес вебхука с учётом BOT_WEBHOOK_MODE (roadmap 5.0.0-06).

    Режимы:
      off  — по умолчанию: только явный WEBHOOK_URL, как было до v5.1.0;
      auto — если WEBHOOK_URL пуст, собирается из DOMAIN, который задаёт
             Bothost;
      force — то же, что auto (обязательность вебхука проверяется при
             установке: при неудаче бот уходит в long polling и пишет
             в лог).

    Явно заданный WEBHOOK_URL всегда важнее автоопределения.

    Риск режима auto именно в тишине: ошибка не роняет бота, а лишает его
    входящих сообщений — панель работает, логи чистые, а бот молчит. Поэтому
    по умолчанию off, а неудачная установка вебхука откатывается на polling.
    """
    explicit = os.getenv("WEBHOOK_URL", "")
    if explicit:
        return explicit
    mode = os.getenv("BOT_WEBHOOK_MODE", "off").lower()
    if mode in ("auto", "force"):
        domain = os.getenv("DOMAIN", "").strip()
        if domain:
            return f"https://{domain}/webhook"
    return ""
```

Заменить использование модульной константы в `lifespan` на вызов
`resolve_webhook_url()`; саму константу `WEBHOOK_URL` оставить для
совместимости логов.

- [ ] **Step 4: Запустить тесты**

Run: `uv run pytest tests/test_v510_self_healing.py -q`
Expected: 16 passed

- [ ] **Step 5: Полная сюита и линтер**

Сюита с `timeout=600000`, `uv run ruff check .`.
Expected: 74/74 PASS, ruff чист.

- [ ] **Step 6: Коммит**

```bash
git add bot.py tests/test_v510_self_healing.py
git commit -m "feat: автоопределение адреса вебхука за флагом (5.0.0-06)

BOT_WEBHOOK_MODE: off (по умолчанию, поведение не меняется) / auto (собирает
адрес из DOMAIN, который задаёт Bothost) / force. Явно заданный WEBHOOK_URL
всегда важнее автоопределения.

По умолчанию off, потому что риск здесь тихий: ошибка не роняет бота, а
лишает его входящих сообщений — панель работает, логи чистые, а бот молчит.
При неудачной установке вебхука бот уходит в long polling и пишет об этом
в лог, а не остаётся без обоих каналов.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 8: Документация и версия 5.1.0

**Files:**
- Modify: `.env.example`, `CLAUDE.md`, `roadmap.md`
- Modify: `web_app.py` (`APP_VERSION`), `pyproject.toml`, `uv.lock`
- Modify: `templates/base.html` (changelog)

- [ ] **Step 1: Описать новые переменные в `.env.example`**

```bash
# ── Bothost Agent (v5.1.0) ──────────────────────────────────────────────────
# Служебный API хостинга: логи, статистика контейнера, перезапуск.
# BOT_ID и USER_ID задаёт платформа автоматически.
# Адрес агента определяется сам: agent:8000 внутри Docker-сети, иначе эта
# переменная, иначе http://agent.bothost.ru
# BOTHOST_AGENT_URL=http://agent:8000

# Автоперезапуск при деградации (память выше 90% лимита либо Telegram
# недоступен дольше пяти минут). 1 включает, любое другое значение — выключено.
# Потолок: 3 перезапуска за 10 минут, дальше стоп и алерт SU.
SELF_HEALING_ENABLED=0

# Автоопределение адреса вебхука: off (по умолчанию) / auto / force.
# auto собирает адрес из DOMAIN, если WEBHOOK_URL пуст.
# Осторожно: ошибка здесь не роняет бота, а лишает его входящих сообщений.
BOT_WEBHOOK_MODE=off
```

- [ ] **Step 2: Дополнить таблицу переменных в `CLAUDE.md`**

```markdown
| `BOTHOST_AGENT_URL` | нет | адрес Bothost Agent; по умолчанию `agent:8000` внутри Docker-сети |
| `SELF_HEALING_ENABLED` | нет | `1` включает автоперезапуск контейнера; по умолчанию выключен |
| `BOT_WEBHOOK_MODE` | нет | `off` (дефолт) / `auto` / `force` — автоопределение адреса вебхука |
```

Добавить в «Карту модулей»:

```markdown
- `bothost_agent.py` — обёртка над Bothost Agent API (логи, статистика,
  перезапуск). Методы не бросают исключений: агента может не быть.
- `self_healing.py` — решение о перезапуске: пороги и лимит попыток.
  Выключено по умолчанию.
```

- [ ] **Step 3: Обновить `roadmap.md`**

Отметить `5.0.0-02`, `-03`, `-04`, `-05`, `-06` как сделанные в v5.1.0,
добавить строку версии в таблицу релизов.

- [ ] **Step 4: Бампнуть версию**

```bash
sed -i 's/^APP_VERSION = "v5.0.0"$/APP_VERSION = "v5.1.0"/' web_app.py
sed -i 's/^version = "5.0.0"/version = "5.1.0"/' pyproject.toml
uv lock
```

- [ ] **Step 5: Добавить запись changelog в `templates/base.html`**

Запись про v5.1.0 в стиле существующих: что появилось (логи, статистика,
кнопка перезапуска), что за флагами и почему, что деплой поведения не меняет.

- [ ] **Step 6: Финальная проверка**

```bash
uv run ruff check .
```
Плюс сюита с `timeout=600000` и проверка старта:

```bash
BOT_TOKEN=1:x WEB_PASSWORD=x ADMIN_IDS=1 WEB_ALLOW_NO_SECRET=1 \
  uv run python -c "import web_app; print(web_app.APP_VERSION); web_app.create_app(); print('OK')"
```

Expected: 74/74 PASS, ruff чист, версия v5.1.0.

- [ ] **Step 7: Коммит**

```bash
git add -A
git commit -m "docs: версия 5.1.0 — интеграция с Bothost Agent завершена

Пять пунктов Bot Self-Awareness, ждавшие доступа к Agent API, закрыты.
Автоматика (перезапуск и вебхук) приезжает выключенной: на проде
SELF_HEALING_ENABLED=0 и BOT_WEBHOOK_MODE=off заданы заранее, поэтому
деплой поведения не меняет.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Self-Review

**Покрытие спеки.** §3 (модуль) → Task 1. §4 (диагностика) → Task 2. §5.1
(логи) → Task 4. §5.2 (статистика) → Task 3. §6 (перезапуск) → Task 5. §7
(self-healing) → Task 6. §8 (авто-webhook) → Task 7. §9 (тесты) → в каждой
задаче. §10 (файлы) → File Structure. §11 (порядок с паузой) → Task 2 Step 9.
§12 (вне объёма) → в плане отсутствует, как и требуется.

**Плейсхолдеров нет:** каждый шаг содержит код или точную команду.
Единственное место без готового текста — Task 8 Step 3 и Step 5
(обновление roadmap и changelog): там нужен связный текст по факту
выполненного, и заранее его писать бессмысленно.

**Согласованность имён.** `AgentResult(ok, data, error)` объявлен в Task 1 и
используется в задачах 2–6 с теми же полями. `reset_cache()` в Task 1
(модуль агента) и в Task 3 (кеш статистики) — разные функции в разных
модулях, вызываются по-разному: `bothost_agent.reset_cache()` и
`self_admin.reset_cache()`. `_EXPECTED_ROUTES` растёт последовательно:
55 → 56 (Task 3) → 57 (Task 4) → 58 (Task 5).
