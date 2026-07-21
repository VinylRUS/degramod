"""
bot.py — Точка входа: FastAPI + Aiogram.
Режим работы определяется автоматически:
  - Если WEBHOOK_URL задан И вебхук удалось установить → webhook
  - Иначе → Long Polling (надёжный фоллбэк)

FastAPI запускается всегда — для веб-панели (когда Bothost починит Traefik).
"""

import asyncio
import logging
import os
import socket
import time
from contextlib import asynccontextmanager

import uvicorn
from aiogram import Bot, Dispatcher, types
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
PORT = int(os.getenv("PORT", "3000"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
WEBHOOK_PATH = "/webhook" if "/webhook" in WEBHOOK_URL else "/webhook"

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN env variable is required")

# ── Диагностика ─────────────────────────────────────────────────────────────
logger.info("=== ENV DUMP ===")
for key, val in sorted(os.environ.items()):
    if key in ("BOT_TOKEN", "API_TOKEN", "BOT_API_TOKEN", "TELEGRAM_BOT_TOKEN",
               "TOKEN", "WEB_PASSWORD", "SESSION_SECRET"):
        val = val[:8] + "..." if val else "(empty)"
    logger.info("  %s = %s", key, val)
logger.info("=== END ENV ===")

_hostname = socket.gethostname()
_host_ip = socket.gethostbyname(_hostname) if _hostname else "?"
logger.info("Hostname: %s | IP: %s | Listening: 0.0.0.0:%d | Webhook: %s",
            _hostname, _host_ip, PORT, WEBHOOK_URL or "(not set)")

# ── Глобальные объекты бота ────────────────────────────────────────────────
bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()
dp.include_router(mod_router)

# Флаги режима
_webhook_set = False
_polling_task = None


async def _start_polling():
    """Запуск Long Polling."""
    logger.info("Starting Long Polling...")
    try:
        await dp.start_polling(bot, handle_signals=False)
    except Exception as e:
        logger.error("Polling error: %s", e)


# ── Lifespan ────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app):
    global _webhook_set, _polling_task

    # ── Startup ─────────────────────────────────────────────────
    await init_db()
    logger.info("DB initialized (WAL mode)")

    # Убираем команды из меню (стелс)
    try:
        await bot.delete_my_commands()
        logger.info("Bot commands cleared (stealth mode)")
    except Exception as e:
        logger.warning("delete_my_commands failed: %s", e)

    # Пробуем установить вебхук
    if WEBHOOK_URL:
        try:
            await bot.set_webhook(
                url=WEBHOOK_URL,
                allowed_updates=["message"],
            )
            info = await bot.get_webhook_info()
            logger.info("Webhook set to %s (info.url=%s)", WEBHOOK_URL, info.url)
            _webhook_set = True
        except Exception as e:
            logger.error("set_webhook FAILED: %s — falling back to polling", e)
            _webhook_set = False

    # Если вебхук не установлен — Long Polling
    if not _webhook_set:
        if WEBHOOK_URL:
            logger.info("Webhook not confirmed — deleting webhook and starting Long Polling")
            try:
                await bot.delete_webhook()
            except Exception:
                pass
        else:
            logger.info("WEBHOOK_URL not set — using Long Polling mode")
        _polling_task = asyncio.create_task(_start_polling())

    yield

    # ── Shutdown ────────────────────────────────────────────────
    if _polling_task:
        _polling_task.cancel()
        try:
            await _polling_task
        except asyncio.CancelledError:
            pass
    try:
        await bot.delete_webhook()
    except Exception:
        pass
    await bot.session.close()
    logger.info("Shutdown complete")


# ── Создаём приложение с lifespan ──────────────────────────────────────────
app = create_app(lifespan=lifespan)


# ── Webhook endpoint — Telegram шлёт сюда обновления ───────────────────────
@app.post(WEBHOOK_PATH)
async def bot_webhook(update: dict):
    """Telegram отправляет POST с Update на этот эндпоинт."""
    try:
        telegram_update = types.Update.model_validate(update)
        await dp.feed_update(bot=bot, update=telegram_update)
    except Exception as e:
        logger.error("Webhook feed_update error: %s", e)
    return {"ok": True}


# ── Диагностический эндпоинт (не зависит от авторизации) ──────────────────
@app.get("/ping")
async def ping():
    """Простой пинг для проверки, что сервер доступен."""
    return {
        "status": "ok",
        "time": time.time(),
        "webhook_mode": _webhook_set,
        "webhook_url": WEBHOOK_URL,
        "port": PORT,
    }


if __name__ == "__main__":
    logger.info("Starting Uvicorn on 0.0.0.0:%d", PORT)
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=PORT,
        log_level="info",
        timeout_keep_alive=30,
    )
