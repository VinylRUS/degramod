"""
web/health.py — v4.8.9: роут /health (вынесен из create_app как PoC).

Простейший роут — не требует auth, не использует БД. Идеальный кандидат
для proof-of-concept декомпозиции.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter

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
