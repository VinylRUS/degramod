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

import asyncio
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

# v5.1.0 (fix-2): публичного дефолта здесь больше нет. Стоял выдуманный
# http://agent.bothost.ru — хост резолвится, но отказывает в соединении на
# 80, 443 и 8000, то есть бот стучался в никуда и рапортовал «агент
# недоступен», словно адрес верный. Публичный адрес у каждой установки свой
# (вида <нода>.bothost.ru или agent.<домен>), угадать его нельзя — он
# задаётся переменной BOTHOST_AGENT_URL. Не задан — так и говорим.

# Кеш адреса: в пределах жизни контейнера он не меняется, дёргать socket
# на каждый запрос незачем.
_cached_url: str | None = None
_internal_reason: str | None = None


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
    global _cached_url, _internal_reason
    _cached_url = None
    _internal_reason = None


# Таймаут проверки доступности — накладывается снаружи через asyncio.wait_for
# (ASYNC109: async-функция не должна принимать параметр timeout сама).
_CONNECT_CHECK_TIMEOUT = 1.0


async def _can_connect(host: str, port: int) -> bool:
    """Устанавливает TCP-соединение. Ошибки пробрасывает наружу.

    v5.1.0: синхронный socket.create_connection здесь не годился — его
    параметр timeout не покрывает фазу DNS-резолвинга, и вызов замирал на
    несколько секунд (замерено: 3.61s при timeout=1.0), останавливая
    обработку сообщений во всех чатах. Таймаут накладывает вызывающий
    (resolve_agent_url) через asyncio.wait_for — он ограничивает операцию
    целиком, включая резолвинг.
    """
    # v5.1.0 (fix-3): OSError больше НЕ глушится здесь. gaierror и
    # ConnectionRefusedError — его наследники, и, проглотив их, мы теряли
    # единственное, что отличает «бот не в сети агента» от «порт закрыт».
    # Разбирает исключения diagnose_internal, она же превращает их в текст.
    _reader, writer = await asyncio.open_connection(host, port)
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return True


async def diagnose_internal() -> tuple[bool, str]:
    """Проверяет agent:8000 и объясняет исход человеческими словами.

    v5.1.0 (fix-3): раньше здесь было голое True/False, и три разных
    диагноза выглядели одинаково. Различать их обязательно:
      • имя не резолвится — контейнер бота не в Docker-сети агента;
      • соединение отвергнуто — сеть та, но на порту никто не слушает;
      • таймаут — проверка не уложилась в отведённое время, агент может
        быть жив, а отбраковали мы его сами.
    """
    try:
        reachable = await asyncio.wait_for(
            _can_connect(_INTERNAL_HOST, _INTERNAL_PORT),
            timeout=_CONNECT_CHECK_TIMEOUT,
        )
    except TimeoutError:
        return False, f"таймаут {_CONNECT_CHECK_TIMEOUT}s при проверке {_INTERNAL_HOST}:{_INTERNAL_PORT}"
    except socket.gaierror as e:
        return False, f"имя {_INTERNAL_HOST} не резолвится ({e})"
    except ConnectionRefusedError:
        return False, f"соединение с {_INTERNAL_HOST}:{_INTERNAL_PORT} отвергнуто"
    except OSError as e:
        return False, f"{type(e).__name__}: {e}"
    if reachable:
        return True, f"{_INTERNAL_HOST}:{_INTERNAL_PORT} доступен"
    return False, f"{_INTERNAL_HOST}:{_INTERNAL_PORT} не отвечает"


async def resolve_agent_url() -> str | None:
    """Адрес агента: внутренний agent:8000 → BOTHOST_AGENT_URL → None.

    None означает «адреса нет» — это честное состояние, а не ошибка:
    снаружи Docker-сети Bothost агента не существует, пока владелец не
    укажет публичный адрес в BOTHOST_AGENT_URL.
    """
    global _cached_url
    if _cached_url is not None:
        return _cached_url

    global _internal_reason
    reachable, _internal_reason = await diagnose_internal()

    if reachable:
        _cached_url = f"http://{_INTERNAL_HOST}:{_INTERNAL_PORT}"
    else:
        _cached_url = (os.getenv("BOTHOST_AGENT_URL") or "").rstrip("/") or None
    logger.info(
        "Bothost agent URL: %s (внутренний: %s)",
        _cached_url or "не задан", _internal_reason,
    )
    return _cached_url


def internal_reason() -> str | None:
    """Последний диагноз по внутреннему адресу — для блока диагностики."""
    return _internal_reason


def _bot_id() -> str | None:
    return os.getenv("BOT_ID") or None


def _auth_headers() -> dict[str, str]:
    """Заголовок авторизации агента, если токен задан.

    v5.1.0 (fix-2): раньше запросы уходили вовсе без авторизации, хотя
    roadmaps/ROADMAP_v5.0.0.md:651 требует Bearer token для agent API —
    при переносе в спеку v5.1.0 пункт потерялся. Токена нет — заголовок
    не выдумываем: пусть агент ответит 401, это внятнее подделки.
    """
    token = os.getenv("BOT_API_TOKEN") or ""
    return {"Authorization": f"Bearer {token}"} if token else {}


async def _request(method: str, path: str, **kwargs) -> AgentResult:
    """Один запрос к агенту с полной обработкой отказов.

    Любая сетевая проблема, таймаут или неразбираемый ответ превращаются в
    AgentResult(ok=False) с внятной причиной.
    """
    base = await resolve_agent_url()
    if not base:
        return AgentResult(
            ok=False,
            error="адрес агента не задан: внутренний agent:8000 не отвечает, "
                  "а переменная BOTHOST_AGENT_URL пуста",
        )
    url = f"{base}{path}"
    # Заголовки вызывающего (X-Bot-ID у restart_self) дополняются авторизацией,
    # а не заменяются ею.
    headers = {**_auth_headers(), **(kwargs.pop("headers", None) or {})}
    timeout = aiohttp.ClientTimeout(total=_TIMEOUT_SECONDS)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(method, url, headers=headers, **kwargs) as response:
                try:
                    payload = await response.json()
                except Exception:
                    text = await response.text()
                    return AgentResult(
                        ok=False,
                        error=f"агент вернул не JSON (HTTP {response.status}) на {url}: {text[:120]}",
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
        return AgentResult(ok=False, error=f"агент недоступен ({url}): {type(e).__name__}")
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
