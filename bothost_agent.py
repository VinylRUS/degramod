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


def resolve_agent_url() -> str:
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
    url = f"{resolve_agent_url()}{path}"
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
