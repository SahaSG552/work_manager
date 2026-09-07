"""Main entry point — runs web server, Telegram bot, and email checker."""

from __future__ import annotations

import asyncio
import logging

import uvicorn
from aiogram import Bot

from app.config import settings
from app.database import init_db
from app.web.routes import app as fastapi_app
from app.channels.telegram_bot import start_telegram_bot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


async def main() -> None:
    """Start all services."""
    log.info("Initializing Work Manager...")

    # Init database
    await init_db()
    log.info("Database ready")

    # Start Telegram bot in background
    bot_task = asyncio.create_task(start_telegram_bot())
    log.info("Telegram bot task created")

    # Start MAX bridge if enabled
    from app.settings_db import get_setting
    max_cfg = await get_setting("max_bridge")
    if max_cfg and max_cfg.get("enabled"):
        from app.channels.max_bridge import start_max_bridge
        start_max_bridge()
        log.info("MAX bridge task created")
    else:
        log.info("MAX bridge disabled (enable in Settings → MAX мост)")

    # Start web server (blocking)
    config = uvicorn.Config(
        fastapi_app,
        host=settings.web_host,
        port=settings.web_port,
        log_level="info",
    )
    server = uvicorn.Server(config)

    # Run both
    await asyncio.gather(bot_task, server.serve())


if __name__ == "__main__":
    asyncio.run(main())
