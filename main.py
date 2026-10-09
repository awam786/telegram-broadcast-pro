import asyncio
import logging

from telegram import BotCommand, BotCommandScopeChat, BotCommandScopeDefault
from telegram.ext import Application

from broadcaster import campaign_worker
from config import ADMIN_IDS, BOT_TOKEN, LOG_LEVEL, validate_config
from database import close_db, init_db
from handlers import register_handlers

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

USER_COMMANDS = [
    BotCommand("start", "Open the main menu"),
    BotCommand("help", "Show user commands"),
    BotCommand("register", "Register this group"),
    BotCommand("destinations", "View your destinations"),
    BotCommand("broadcast", "Broadcast a replied-to message"),
    BotCommand("schedule", "Schedule a replied-to message"),
    BotCommand("campaigns", "View your campaigns"),
    BotCommand("cancel", "Cancel a pending campaign"),
]
ADMIN_COMMANDS = USER_COMMANDS + [
    BotCommand("adminhelp", "Show administrator commands"),
    BotCommand("adminstats", "View system statistics"),
    BotCommand("adminbroadcast", "Broadcast to an audience"),
]


async def post_init(application: Application) -> None:
    await init_db()
    # Default command menu is user-safe: no admin commands are exposed here.
    await application.bot.set_my_commands(USER_COMMANDS, scope=BotCommandScopeDefault())
    # Admins receive a separate Telegram command menu in private chat.
    for admin_id in ADMIN_IDS:
        try:
            await application.bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=admin_id))
        except Exception as exc:
            logger.warning("Could not set admin command menu for %s: %s", admin_id, exc)
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
    application.run_polling(allowed_updates=["message", "callback_query", "my_chat_member"], drop_pending_updates=False)


if __name__ == "__main__":
    main()
