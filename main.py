import asyncio
import logging
from telegram import BotCommand
from telegram.ext import Application
from broadcaster import campaign_worker
from config import BOT_TOKEN, LOG_LEVEL, validate_config
from database import init_db, close_db
from handlers import register_handlers

logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger(__name__)


async def post_init(application: Application) -> None:
    await init_db()
    await application.bot.set_my_commands([
        BotCommand("start", "Start and register"), BotCommand("help", "Show help"),
        BotCommand("register", "Register this group"), BotCommand("destinations", "List your destinations"),
        BotCommand("broadcast", "Broadcast a replied-to message"), BotCommand("schedule", "Schedule a replied-to message"),
        BotCommand("campaigns", "List campaigns"), BotCommand("cancel", "Cancel a pending campaign"),
    ])
    application.bot_data["campaign_worker_task"] = asyncio.create_task(campaign_worker(application))
    logger.info("Telegram Broadcast Pro started")


async def post_shutdown(application: Application) -> None:
    task = application.bot_data.get("campaign_worker_task")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    await close_db()
    logger.info("Telegram Broadcast Pro stopped")


def main() -> None:
    validate_config()
    application = Application.builder().token(BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()
    register_handlers(application)
    application.run_polling(allowed_updates=["message", "my_chat_member"], drop_pending_updates=False)


if __name__ == "__main__":
    main()
