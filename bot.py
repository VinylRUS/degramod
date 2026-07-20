"""
bot.py — Точка входа: FastAPI (daemon-поток) + Aiogram Long Polling (главный поток).
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading

from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

from bot_handlers import router as mod_router
from db import init_db
from web_app import create_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(name)-24s │ %(levelname)-7s │ %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("shadow_logger")

# ── Env ─────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", "8000"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN env variable is required")


# ── FastAPI в daemon-потоке ─────────────────────────────────────────────────
def _run_web() -> None:
    import uvicorn
    app = create_app()
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")


# ── Aiogram в главном потоке ───────────────────────────────────────────────
async def _run_bot() -> None:
    await init_db()
    logger.info("DB initialized (WAL mode)")

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(mod_router)

    # Убираем все команды из меню бота
    await bot.delete_my_commands()
    logger.info("Bot commands cleared (stealth mode)")

    logger.info("Starting Aiogram Long Polling …")
    await dp.start_polling(bot, allowed_updates=["message"])


# ── Main ────────────────────────────────────────────────────────────────────
def main() -> None:
    # Запускаем FastAPI в daemon-потоке
    web_thread = threading.Thread(target=_run_web, daemon=True, name="web")
    web_thread.start()
    logger.info("FastAPI started on 0.0.0.0:%s (daemon thread)", PORT)

    # Запускаем Aiogram в главном потоке
    asyncio.run(_run_bot())


if __name__ == "__main__":
    main()
