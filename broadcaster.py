"""Delivery engine: async, rate-limited, retry-aware, idempotent-ish."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Optional

from telegram import Bot
from telegram.error import Forbidden, BadRequest, RetryAfter, TelegramError, NetworkError

from config import Config
from database import Database

log = logging.getLogger(__name__)


class BroadcastEngine:
    def __init__(self, bot: Bot, db: Database, cfg: Config) -> None:
        self.bot = bot
        self.db = db
        self.cfg = cfg
        self.queue: asyncio.Queue[int] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._sem = asyncio.Semaphore(cfg.max_concurrent_deliveries)
        self._stopping = False

    async def start(self) -> None:
        await self.db.recover_stuck_targets()
        n = max(2, self.cfg.max_concurrent_deliveries)
        for i in range(n):
            self._workers.append(asyncio.create_task(self._worker(i), name=f"bc-worker-{i}"))
        log.info("Broadcast engine started with %d workers", n)

    async def stop(self) -> None:
        self._stopping = True
        for w in self._workers:
            w.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()

    async def enqueue(self, campaign_id: int) -> None:
        await self.queue.put(campaign_id)

    async def _worker(self, idx: int) -> None:
        while not self._stopping:
            try:
                cid = await self.queue.get()
            except asyncio.CancelledError:
                return
            try:
                await self._run_campaign(cid)
            except Exception:
                log.exception("Campaign %s crashed in worker %d", cid, idx)
            finally:
                self.queue.task_done()

    async def _run_campaign(self, campaign_id: int) -> None:
        campaign = await self.db.get_campaign(campaign_id)
        if not campaign:
            log.warning("Campaign %s not found", campaign_id)
            return
        if campaign["status"] not in ("queued", "running"):
            log.info("Skipping campaign %s with status %s", campaign_id, campaign["status"])
            return

        await self.db.set_campaign_status(campaign_id, "running")
        targets = await self.db.get_campaign_targets(campaign_id)
        pending = [t for t in targets if t["status"] == "pending"]
        log.info("Campaign %s: %d pending targets", campaign_id, len(pending))

        for t in pending:
            if self._stopping:
                break
            async with self._sem:
                await self._deliver_one(campaign, t)

        stats = await self.db.campaign_stats(campaign_id)
        ok = stats.get("success", 0)
        fail = stats.get("failed", 0) + stats.get("uncertain", 0)
        pending_left = stats.get("pending", 0) + stats.get("sending", 0)
        if pending_left:
            final = "paused"
        elif fail == 0 and ok > 0:
            final = "completed"
        elif ok > 0:
            final = "completed_with_errors"
        else:
            final = "failed"
        await self.db.set_campaign_status(campaign_id, final)
        await self.db.bump_user_counters(campaign["user_telegram_id"], ok, fail)
        log.info("Campaign %s finished status=%s ok=%d fail=%d", campaign_id, final, ok, fail)

        # Notify user
        try:
            await self.bot.send_message(
                campaign["user_telegram_id"],
                f"✅ Campaign <b>#{campaign_id}</b> finished: <b>{final}</b>\n"
                f"✔️ Success: {ok}   ❌ Failed: {fail}",
                parse_mode="HTML",
            )
        except TelegramError:
            pass

    async def _deliver_one(self, campaign: Dict[str, Any], target: Dict[str, Any]) -> None:
        from_chat = campaign["source_chat_id"]
        from_msg = campaign["source_message_id"]
        target_chat = target["target_chat_id"]

        for attempt in range(3):
            try:
                await self.db.update_target(target["id"], "sending", bump_attempt=True)
                await self.bot.copy_message(
                    chat_id=target_chat,
                    from_chat_id=from_chat,
                    message_id=from_msg,
                )
                await self.db.update_target(target["id"], "success", bump_attempt=False)
                return
            except RetryAfter as e:
                wait = int(getattr(e, "retry_after", 5)) + 1
                log.warning("Rate limited on target %s; sleeping %ss", target["id"], wait)
                await asyncio.sleep(wait)
                continue
            except Forbidden as e:
                await self.db.update_target(
                    target["id"], "failed",
                    err=f"Bot lacks permission or was removed: {e}", err_cat="forbidden",
                )
                await self.db.update_destination_status(target["destination_id"], "inaccessible", str(e)) \
                    if target.get("destination_id") else None
                return
            except BadRequest as e:
                s = str(e).lower()
                if "message to copy not found" in s or "message_id_invalid" in s:
                    await self.db.update_target(
                        target["id"], "failed", err=str(e), err_cat="source_missing")
                    return
                if "chat not found" in s or "chat_invalid" in s:
                    await self.db.update_target(
                        target["id"], "failed", err=str(e), err_cat="chat_missing")
                    return
                await self.db.update_target(
                    target["id"], "failed", err=str(e), err_cat="bad_request")
                return
            except NetworkError as e:
                await asyncio.sleep(2 ** attempt)
                if attempt == 2:
                    await self.db.update_target(
                        target["id"], "uncertain", err=str(e), err_cat="network")
                continue
            except TelegramError as e:
                await self.db.update_target(
                    target["id"], "failed", err=str(e), err_cat="telegram")
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("Unexpected delivery error target=%s", target["id"])
                await self.db.update_target(
                    target["id"], "failed", err=str(e), err_cat="unknown")
                return
        # loop exhausted
        await self.db.update_target(
            target["id"], "uncertain", err="Max retries exceeded", err_cat="retry_exhausted")
