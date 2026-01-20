import asyncio
import logging
import sys
import os
from contextlib import suppress
from flask import Flask
from threading import Thread

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from config import config
from db import init_db, close_db
from handlers import register_all_handlers
from middlewares import register_all_middlewares
from services.announcements import set_bot as set_announcements_bot, run_scheduler
from services.healthcheck import start_health_server, stop_health_server, get_health_server
from services.cache import start_batch_flush_task, stop_batch_flush_task, flush_member_updates
from services import ml_manager

# --- RENDER PORT BINDING (MUST START FIRST) ---
app = Flask(__name__)

@app.route('/')
def health_check():
    return "Samurai Bot is Running!", 200

def run_flask():
    # Render automatically assigns a PORT
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

# --- LOGGING ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

_scheduler_task: asyncio.Task | None = None

async def on_startup(bot: Bot) -> None:
    """Startup hook."""
    global _scheduler_task
    logger.info("Bot starting up...")
    
    await init_db()
    start_batch_flush_task()
    set_announcements_bot(bot)
    
    _scheduler_task = asyncio.create_task(run_scheduler())
    
    # ML monitor only starts if ML is enabled in config
    ml_manager.start_monitor()

    if config.healthcheck.enabled:
        get_health_server().set_ready(True)
    
    logger.info(f"Bot version: {config.bot.version}")

async def on_shutdown(bot: Bot) -> None:
    """Shutdown hook."""
    global _scheduler_task
    logger.info("Bot shutting down...")

    if config.healthcheck.enabled:
        get_health_server().set_ready(False)

    ml_manager.stop_monitor()

    if _scheduler_task and not _scheduler_task.done():
        _scheduler_task.cancel()
        with suppress(asyncio.CancelledError):
            await _scheduler_task

    stop_batch_flush_task()
    await flush_member_updates()
    await close_db()
    
    if config.healthcheck.enabled:
        await stop_health_server()

async def main() -> None:
    """Main bot function."""
    if not config.bot.token.get_secret_value():
        logger.error("No bot token provided")
        sys.exit(1)

    bot = Bot(
        token=config.bot.token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    dp = Dispatcher()

    register_all_handlers(dp)
    register_all_middlewares(
        dp,
        default_locale=config.locale.default,
        enable_throttling=config.throttling.enabled,
        throttle_rate=config.throttling.rate_limit,
        throttle_max_messages=config.throttling.max_messages,
        throttle_time_window=config.throttling.time_window
    )

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    logger.info("Bot logic initialized. Starting polling...")
    try:
        await dp.start_polling(bot, skip_updates=True)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    # 1. Sabse pehle Flask thread start karein taaki Render port detect kar sake
    server_thread = Thread(target=run_flask, daemon=True)
    server_thread.start()
    logger.info("Web server thread started for Render port binding.")

    # 2. Phir main asyncio loop chalayein
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
        
