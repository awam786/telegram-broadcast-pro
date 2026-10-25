"""Application entrypoint."""
from __future__ import annotations

import logging
import signal
import asyncio
from datetime import datetime, timezone

from telegram.ext import Application, ApplicationBuilder

import handlers as h
from config import load_config
from database import Database
from broadcaster import BroadcastEngine
from scheduler import Scheduler
from emoji_manager import EmojiManager

log = logging.getLogger("tbp")


async def _post_init(app: Application) -> None:
    db: Database = app.bot_data["db"]
    await db.connect()
    app.bot_data["engine"] = BroadcastEngine(app.bot, db, app.bot_data["cfg"])
    await app.bot_data["engine"].start()

    async def enqueue(cid: int) -> None:
        await app.bot_data["engine"].enqueue(cid)

    app.bot_data["scheduler"] = Scheduler(db, app.bot_data["cfg"], enqueue)
    app.bot_data["scheduler"].start()
    log.info("Application initialized")


async def _post_shutdown(app: Application) -> None:
    try:
        await app.bot_data["scheduler"].stop()
    except Exception:
        log.exception("scheduler shutdown")
    try:
        await app.bot_data["engine"].stop()
    except Exception:
        log.exception("engine shutdown")
    try:
        await app.bot_data["db"].close()
    except Exception:
        log.exception("db shutdown")
    log.info("Application shutdown complete")


def main() -> None:
    cfg = load_config()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    app = ApplicationBuilder().token(cfg.bot_token).post_init(_post_init).post_shutdown(_post_shutdown).build()
    app.bot_data["cfg"] = cfg
    app.bot_data["db"] = Database(cfg.database_url)
    app.bot_data["emoji"] = EmojiManager(app.bot_data["db"])

    h.register(app, cfg)

    log.info("Starting polling...")
    app.run_polling(drop_pending_updates=False, allowed_updates=None)


if __name__ == "__main__":
    main()
