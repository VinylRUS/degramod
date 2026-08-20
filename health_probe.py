"""
health_probe.py — метрики здоровья процесса для /healthz (v4.10.2, Task 16).

Зачем отдельный модуль. Данные о Telegram собирает фоновая таска бота, а
отдаёт их роут веб-панели: положить состояние в `web/` нельзя (тогда `bot.py`
импортировал бы веб-слой), в `web_app.py` — тоже (он про сборку приложения).
Модуль в корне, рядом с `chat_modes.py` и `modchat.py`, не тянет за собой ни
aiogram, ни FastAPI.

Почему пробник фоновый, а не по запросу. `/healthz` рассчитан на внешний
мониторинг с интервалом в десятки секунд. Если бы роут сам звал `get_me()`,
каждый опрос уходил бы в сеть: ответ на сотни миллисекунд вместо единиц и
постоянный поток запросов к Bot API. Пробник ходит в Telegram раз в минуту,
роут читает готовый снимок.

Почему пробник не встроен в `_night_mode_loop`. Там инвариант порядка тиков
(alarm → sanitary → night, см. `CLAUDE.md`), и зависший `get_me()` задержал
бы снятие режимов чата. Отдельная таска изолирует этот риск.
"""
from __future__ import annotations

import os
import time
from collections import deque
from datetime import datetime, timezone

# Пороги из roadmap.md, 5.0.0-07.
_MEMORY_DEGRADED_PERCENT = 85.0
_MEMORY_DOWN_PERCENT = 95.0
_TELEGRAM_SLOW_MS = 1000

# roadmap 5.0.0-08: сколько подряд медленных ответов считаем деградацией и
# как часто позволяем беспокоить SU.
_ALERT_STREAK = 5
_ALERT_COOLDOWN_SECONDS = 30 * 60
_HISTORY_SIZE = 10

# cgroup v2 пишет литерал `max`, если лимита нет; v1 в той же ситуации
# отдаёт число размером с адресное пространство. Всё, что выше этого
# порога, считаем «лимит не задан».
_UNLIMITED_THRESHOLD = 1 << 62

_CGROUP_LIMIT_FILES = (
    "/sys/fs/cgroup/memory.max",                  # cgroup v2
    "/sys/fs/cgroup/memory/memory.limit_in_bytes",  # cgroup v1
)

# Снимок последнего обращения к Telegram. Пишет probe_tick, читает snapshot.
# None в telegram_connected означает «ещё не проверяли», а не «связи нет»:
# сразу после старта пробник ещё не отработал, и врать про обрыв нельзя.
_state: dict = {
    "telegram_connected": None,
    "telegram_api_latency_ms": None,
    "checked_at": None,
}


# История последних замеров для детекции устойчивых задержек (5.0.0-08).
# None означает «связи не было» — в серию медленных не засчитывается.
_latency_history: deque = deque(maxlen=_HISTORY_SIZE)

# Когда в последний раз беспокоили SU. Ноль — не беспокоили ни разу.
_alert_state: dict = {"last_sent": 0.0}


def reset_state() -> None:
    """Сбрасывает снимок и историю. Нужен тестам для изоляции."""
    _state["telegram_connected"] = None
    _state["telegram_api_latency_ms"] = None
    _state["checked_at"] = None
    _latency_history.clear()
    _alert_state["last_sent"] = 0.0


def record_latency(value_ms: int | None) -> None:
    """Добавляет замер в историю. None — обрыв связи, не «медленно»."""
    _latency_history.append(value_ms)


def latency_history() -> list:
    """Копия истории замеров, свежие в конце."""
    return list(_latency_history)


def latency_average_ms() -> int | None:
    """Среднее по успешным замерам. None, если успешных нет."""
    values = [v for v in _latency_history if v is not None]
    if not values:
        return None
    return int(sum(values) / len(values))


def should_alert() -> bool:
    """Пора ли слать SU алерт о задержках Telegram.

    Условие из roadmap 5.0.0-08: пять последних замеров подряд выше порога.
    Пробник ходит раз в минуту, то есть это пять минут стабильных тормозов,
    а не случайный всплеск.

    Обрыв связи (None) серию не продолжает и не начинает: это отдельная
    авария, её показывает `degraded` в /healthz. Если считать её медленным
    ответом, SU получит алерт про задержки вместо «Telegram недоступен».

    Антиспам: не чаще раза в 30 минут.
    """
    if len(_latency_history) < _ALERT_STREAK:
        return False
    tail = list(_latency_history)[-_ALERT_STREAK:]
    if not all(v is not None and v > _TELEGRAM_SLOW_MS for v in tail):
        return False
    return (time.time() - _alert_state["last_sent"]) >= _ALERT_COOLDOWN_SECONDS


def mark_alert_sent() -> None:
    """Запоминает момент отправки — включает антиспам-окно."""
    _alert_state["last_sent"] = time.time()


def _read_first_int(path: str) -> int | None:
    """Читает файл с одним числом. `max` и любой мусор → None."""
    try:
        with open(path) as f:
            raw = f.read().strip()
    except OSError:
        return None
    if raw == "max":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def memory_limit_bytes() -> int | None:
    """Лимит памяти контейнера: cgroup v2 → cgroup v1 → None.

    Память хоста намеренно не используется как запасной вариант: 300 МБ от
    32 ГБ — это 1%, порог никогда не сработает, и метрика будет врать, что
    всё хорошо, пока контейнер с лимитом 512 МБ ловит OOM.
    """
    for path in _CGROUP_LIMIT_FILES:
        value = _read_first_int(path)
        if value is not None and 0 < value < _UNLIMITED_THRESHOLD:
            return value
    return None


def memory_rss_bytes() -> int:
    """RSS процесса из /proc/self/status (Linux). Вне Linux — 0."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(parts[1]) * 1024
                    break
    except (OSError, ValueError):
        pass
    return 0


def grade(
    *,
    memory_percent: float | None,
    tg_connected: bool | None,
    tg_latency_ms: int | None,
) -> str:
    """Сводит метрики в ok / degraded / down — берётся худшее состояние.

    down — только по памяти: процесс у грани OOM. Проблемы с Telegram дают
    degraded, потому что бот при этом жив и веб-панель работает; мониторинг
    не должен считать это падением и уводить контейнер в рестарт.

    memory_percent=None (лимит контейнера неизвестен) порогов не включает.
    """
    if memory_percent is not None and memory_percent > _MEMORY_DOWN_PERCENT:
        return "down"
    if memory_percent is not None and memory_percent > _MEMORY_DEGRADED_PERCENT:
        return "degraded"
    if tg_connected is False:
        return "degraded"
    if tg_latency_ms is not None and tg_latency_ms > _TELEGRAM_SLOW_MS:
        return "degraded"
    return "ok"


async def probe_tick(bot) -> None:
    """Один прогон пробника: зовёт get_me и запоминает результат.

    Исключения наружу не выпускает — таска, которая падает от сетевого
    сбоя, перестала бы проверять здоровье ровно тогда, когда это нужнее
    всего.
    """
    if bot is None:
        return
    started = time.monotonic()
    try:
        await bot.get_me()
    except Exception:
        _state["telegram_connected"] = False
        _state["telegram_api_latency_ms"] = None
        record_latency(None)
    else:
        _state["telegram_connected"] = True
        elapsed_ms = int((time.monotonic() - started) * 1000)
        _state["telegram_api_latency_ms"] = elapsed_ms
        record_latency(elapsed_ms)
    _state["checked_at"] = time.time()


def snapshot() -> dict:
    """Последний результат пробника. В сеть не ходит."""
    return dict(_state)


def collect_health(app_version: str, start_time: float) -> dict:
    """Полный ответ /healthz.

    app_version и start_time приходят параметрами, а не импортом из
    web_app: модуль не должен зависеть от веб-слоя.
    """
    rss = memory_rss_bytes()
    limit = memory_limit_bytes()
    percent = round(rss / limit * 100, 1) if limit else None
    snap = snapshot()
    return {
        "status": grade(
            memory_percent=percent,
            tg_connected=snap["telegram_connected"],
            tg_latency_ms=snap["telegram_api_latency_ms"],
        ),
        # BOT_ID и HOSTNAME задаёт Bothost. Локально их нет — отдаём null,
        # а не выдуманное значение.
        "bot_id": os.getenv("BOT_ID") or None,
        "container_id": os.getenv("HOSTNAME") or None,
        "version": app_version,
        "uptime_seconds": int(time.time() - start_time),
        "memory_mb": round(rss / 1024 / 1024, 1),
        "memory_percent": percent,
        "telegram_connected": snap["telegram_connected"],
        "telegram_api_latency_ms": snap["telegram_api_latency_ms"],
        # 5.0.0-08: среднее по истории — по одному замеру не видно тренда.
        "telegram_api_latency_avg_ms": latency_average_ms(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
