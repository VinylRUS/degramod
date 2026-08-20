"""
web/health.py — эндпоинты состояния для внешнего мониторинга.

v4.8.9: /health вынесен из create_app как proof-of-concept декомпозиции.
v4.10.2 (Task 16 / roadmap 5.0.0-07): добавлен /healthz — те же данные плюс
память, uptime и состояние Telegram, с градацией ok/degraded/down и кодами
200/503.

Оба роута публичные: не требуют ни авторизации, ни БД, ни обращения к
Telegram. Метрики Telegram берутся из снимка, который пишет фоновый пробник
(health_probe.probe_tick) — иначе мониторинг с интервалом в полминуты
превратился бы в непрерывный поток getMe к Bot API.

/health оставлен без изменений намеренно: его может опрашивать мониторинг
Bothost, а он рассчитывает на четыре поля и всегда код 200.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter
from fastapi.responses import JSONResponse

import health_probe
import web_app
from web.deps import APP_VERSION

router = APIRouter()


@router.get("/health")
async def health():
    """Health check — простой JSON-эндпоинт для мониторинга."""
    return {
        "status": "ok",
        "service": "dedushka-vobzhak",
        "version": APP_VERSION,
        "time": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/healthz")
async def healthz():
    """Расширенная проверка состояния: метрики + градация.

    Коды: ok и degraded → 200, down → 503. degraded означает «работает, но
    ухудшается» (память выше 85% лимита, Telegram недоступен или отвечает
    дольше секунды) — мониторинг должен это заметить, но не считать бота
    упавшим и не запускать авто-рестарт. 503 отдаётся только при памяти
    выше 95%, когда процесс у грани OOM.
    """
    payload = health_probe.collect_health(APP_VERSION, web_app._APP_START_TIME)
    code = 503 if payload["status"] == "down" else 200
    return JSONResponse(payload, status_code=code)
