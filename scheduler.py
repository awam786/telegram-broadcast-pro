"""Persistent scheduler — reads scheduled_jobs from DB and enqueues due work."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from config import Config
from database import Database

log = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, db: Database, cfg: Config, enqueue_campaign) -> None:
        self.db = db
        self.cfg = cfg
        self.enqueue_campaign = enqueue_campaign
        self._task: Optional[asyncio.Task] = None
        self._stopping = False

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="scheduler")
        log.info("Scheduler started")

    async def stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("Scheduler stopped")

    async def _loop(self) -> None:
        while not self._stopping:
            try:
                await self._tick()
            except asyncio.CancelledError:
                return
            except Exception:
                log.exception("Scheduler tick failed")
            await asyncio.sleep(self.cfg.scheduler_poll_seconds)

    async def _tick(self) -> None:
        now = datetime.now(timezone.utc)
        due = await self.db.due_jobs(now, limit=10)
        for job in due:
            try:
                await self._fire(job)
            except Exception:
                log.exception("Failed firing job %s", job["id"])

    async def _fire(self, job: dict) -> None:
        camp = await self.db.get_campaign(job["campaign_id"])
        if not camp:
            await self.db.advance_job(job["id"], None, "cancelled")
            return

        # Reset all target statuses to pending for a repeat run — but a new campaign
        # is more correct for history. We'll clone the campaign targets as a fresh
        # campaign for each repetition to keep per-run history.
        if job["repeat_interval_seconds"] is None:
            # one-shot scheduled campaign: just run it
            await self.db.set_campaign_status(camp["id"], "queued")
            await self.enqueue_campaign(camp["id"])
            await self.db.advance_job(job["id"], None, "completed")
            return

        # repeating campaign: clone
        from datetime import datetime as _dt
        new_id = await self.db.create_campaign(
            user_tg=camp["user_telegram_id"],
            source_chat_id=camp["source_chat_id"],
            source_message_id=camp["source_message_id"],
            message_json=camp["source_message_json"],
            summary=camp["content_summary"],
            emoji_mode=camp["emoji_mode"],
            status="queued",
            scheduled_at=None,
            is_admin=camp["is_admin_campaign"],
        )
        # copy targets
        tgts = await self.db.get_campaign_targets(camp["id"])
        await self.db.add_campaign_targets(
            new_id,
            [{"destination_id": t["destination_id"], "target_chat_id": t["target_chat_id"]} for t in tgts],
        )
        await self.enqueue_campaign(new_id)

        # Advance
        runs_done = job["runs_done"] + 1
        next_run = datetime.now(timezone.utc) + timedelta(seconds=job["repeat_interval_seconds"])
        status = "active"
        if job["max_repetitions"] and runs_done >= job["max_repetitions"]:
            status = "completed"
            next_run = None
        if job["end_at"] and next_run and next_run > job["end_at"]:
            status = "completed"
            next_run = None
        await self.db.advance_job(job["id"], next_run, status)
        log.info("Repeating campaign fired: source=%s new=%s job=%s", camp["id"], new_id, job["id"])
