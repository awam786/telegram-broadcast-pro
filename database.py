"""PostgreSQL pool, schema and typed query helpers."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

import asyncpg

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    telegram_id BIGINT UNIQUE NOT NULL,
    username TEXT,
    display_name TEXT,
    first_joined TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_active TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    total_broadcasts INTEGER NOT NULL DEFAULT 0,
    successful_deliveries INTEGER NOT NULL DEFAULT 0,
    failed_deliveries INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS destinations (
    id SERIAL PRIMARY KEY,
    chat_id BIGINT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    dest_type TEXT NOT NULL,
    registered_by BIGINT NOT NULL,
    registered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_verified TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'active',
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS destination_managers (
    id SERIAL PRIMARY KEY,
    destination_id INTEGER NOT NULL REFERENCES destinations(id) ON DELETE CASCADE,
    user_telegram_id BIGINT NOT NULL,
    role TEXT NOT NULL DEFAULT 'manager',
    added_by BIGINT,
    added_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(destination_id, user_telegram_id)
);

CREATE TABLE IF NOT EXISTS drafts (
    id SERIAL PRIMARY KEY,
    user_telegram_id BIGINT NOT NULL,
    source_chat_id BIGINT,
    source_message_id BIGINT,
    source_message_json JSONB,
    emoji_mode TEXT NOT NULL DEFAULT 'auto',
    selected_destinations JSONB,
    buttons_config JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS campaigns (
    id SERIAL PRIMARY KEY,
    user_telegram_id BIGINT NOT NULL,
    title TEXT,
    source_chat_id BIGINT,
    source_message_id BIGINT,
    source_message_json JSONB,
    content_summary TEXT,
    emoji_mode TEXT NOT NULL DEFAULT 'auto',
    status TEXT NOT NULL DEFAULT 'draft',
    is_admin_campaign BOOLEAN DEFAULT FALSE,
    scheduled_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS campaign_targets (
    id SERIAL PRIMARY KEY,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    destination_id INTEGER REFERENCES destinations(id) ON DELETE SET NULL,
    target_chat_id BIGINT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_attempt TIMESTAMPTZ,
    last_error TEXT,
    error_category TEXT,
    UNIQUE(campaign_id, target_chat_id)
);

CREATE TABLE IF NOT EXISTS scheduled_jobs (
    id SERIAL PRIMARY KEY,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    user_telegram_id BIGINT NOT NULL,
    next_run_at TIMESTAMPTZ NOT NULL,
    repeat_interval_seconds INTEGER,
    max_repetitions INTEGER,
    runs_done INTEGER NOT NULL DEFAULT 0,
    end_at TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS premium_emojis (
    id SERIAL PRIMARY KEY,
    unicode_fallback TEXT,
    document_id TEXT NOT NULL UNIQUE,
    description TEXT,
    added_by BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS learned_premium_emojis (
    id SERIAL PRIMARY KEY,
    example_text TEXT,
    entities_json JSONB,
    document_ids TEXT[],
    validated BOOLEAN DEFAULT FALSE,
    learned_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS user_settings (
    user_telegram_id BIGINT PRIMARY KEY,
    timezone TEXT DEFAULT 'UTC',
    notifications_enabled BOOLEAN DEFAULT TRUE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS bot_settings (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS admin_actions (
    id SERIAL PRIMARY KEY,
    admin_telegram_id BIGINT NOT NULL,
    action TEXT NOT NULL,
    target_type TEXT,
    target_id TEXT,
    details JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_users_tg ON users(telegram_id);
CREATE INDEX IF NOT EXISTS idx_dest_chat ON destinations(chat_id);
CREATE INDEX IF NOT EXISTS idx_dest_owner ON destinations(registered_by);
CREATE INDEX IF NOT EXISTS idx_dm_user ON destination_managers(user_telegram_id);
CREATE INDEX IF NOT EXISTS idx_drafts_user ON drafts(user_telegram_id);
CREATE INDEX IF NOT EXISTS idx_camp_user ON campaigns(user_telegram_id);
CREATE INDEX IF NOT EXISTS idx_camp_status ON campaigns(status);
CREATE INDEX IF NOT EXISTS idx_ct_camp ON campaign_targets(campaign_id);
CREATE INDEX IF NOT EXISTS idx_ct_status ON campaign_targets(status);
CREATE INDEX IF NOT EXISTS idx_sj_next ON scheduled_jobs(next_run_at) WHERE status='active';
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Database:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self.pool: Optional[asyncpg.Pool] = None

    async def connect(self) -> None:
        self.pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=10)
        async with self.pool.acquire() as con:
            await con.execute(SCHEMA)
        log.info("Database connected and schema ensured")

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()
            self.pool = None

    # ---------------- Users ----------------
    async def upsert_user(self, telegram_id: int, username: str | None, display_name: str) -> None:
        async with self.pool.acquire() as con:
            await con.execute(
                """
                INSERT INTO users(telegram_id, username, display_name)
                VALUES($1, $2, $3)
                ON CONFLICT (telegram_id) DO UPDATE SET
                    username = EXCLUDED.username,
                    display_name = EXCLUDED.display_name,
                    last_active = NOW()
                """,
                telegram_id, username, display_name,
            )
            await con.execute(
                """
                INSERT INTO user_settings(user_telegram_id) VALUES($1)
                ON CONFLICT DO NOTHING
                """,
                telegram_id,
            )

    async def touch_user(self, telegram_id: int) -> None:
        async with self.pool.acquire() as con:
            await con.execute(
                "UPDATE users SET last_active=NOW() WHERE telegram_id=$1 AND last_active < NOW() - INTERVAL '5 minutes'",
                telegram_id,
            )

    async def get_user(self, telegram_id: int) -> Optional[Dict[str, Any]]:
        async with self.pool.acquire() as con:
            row = await con.fetchrow("SELECT * FROM users WHERE telegram_id=$1", telegram_id)
            return dict(row) if row else None

    async def get_user_status(self, telegram_id: int) -> str:
        async with self.pool.acquire() as con:
            v = await con.fetchval("SELECT status FROM users WHERE telegram_id=$1", telegram_id)
            return v or "unknown"

    async def set_user_status(self, telegram_id: int, status: str) -> None:
        async with self.pool.acquire() as con:
            await con.execute("UPDATE users SET status=$2 WHERE telegram_id=$1", telegram_id, status)

    async def list_users(self, offset: int, limit: int) -> List[Dict[str, Any]]:
        async with self.pool.acquire() as con:
            rows = await con.fetch(
                "SELECT * FROM users ORDER BY first_joined DESC OFFSET $1 LIMIT $2", offset, limit
            )
            return [dict(r) for r in rows]

    async def count_users(self) -> int:
        async with self.pool.acquire() as con:
            return await con.fetchval("SELECT COUNT(*) FROM users") or 0

    async def search_users(self, q: str, limit: int = 20) -> List[Dict[str, Any]]:
        async with self.pool.acquire() as con:
            like = f"%{q}%"
            rows = await con.fetch(
                """SELECT * FROM users
                   WHERE CAST(telegram_id AS TEXT) LIKE $1 OR username ILIKE $1 OR display_name ILIKE $1
                   ORDER BY last_active DESC LIMIT $2""",
                like, limit,
            )
            return [dict(r) for r in rows]

    async def bump_user_counters(self, telegram_id: int, ok: int, fail: int) -> None:
        async with self.pool.acquire() as con:
            await con.execute(
                """UPDATE users SET
                   total_broadcasts = total_broadcasts + 1,
                   successful_deliveries = successful_deliveries + $2,
                   failed_deliveries = failed_deliveries + $3
                   WHERE telegram_id=$1""",
                telegram_id, ok, fail,
            )

    # ---------------- Destinations ----------------
    async def get_destination_by_chat(self, chat_id: int) -> Optional[Dict[str, Any]]:
        async with self.pool.acquire() as con:
            row = await con.fetchrow("SELECT * FROM destinations WHERE chat_id=$1", chat_id)
            return dict(row) if row else None

    async def get_destination(self, dest_id: int) -> Optional[Dict[str, Any]]:
        async with self.pool.acquire() as con:
            row = await con.fetchrow("SELECT * FROM destinations WHERE id=$1", dest_id)
            return dict(row) if row else None

    async def add_destination(
        self, chat_id: int, title: str, dest_type: str, owner_tg: int
    ) -> int:
        async with self.pool.acquire() as con:
            async with con.transaction():
                dest_id = await con.fetchval(
                    """INSERT INTO destinations(chat_id, title, dest_type, registered_by, last_verified)
                       VALUES($1,$2,$3,$4,NOW()) RETURNING id""",
                    chat_id, title, dest_type, owner_tg,
                )
                await con.execute(
                    """INSERT INTO destination_managers(destination_id, user_telegram_id, role, added_by)
                       VALUES($1,$2,'owner',$2) ON CONFLICT DO NOTHING""",
                    dest_id, owner_tg,
                )
                return dest_id

    async def add_manager(self, dest_id: int, user_tg: int, added_by: int) -> None:
        async with self.pool.acquire() as con:
            await con.execute(
                """INSERT INTO destination_managers(destination_id, user_telegram_id, role, added_by)
                   VALUES($1,$2,'manager',$3) ON CONFLICT DO NOTHING""",
                dest_id, user_tg, added_by,
            )

    async def is_manager(self, dest_id: int, user_tg: int) -> bool:
        async with self.pool.acquire() as con:
            v = await con.fetchval(
                "SELECT 1 FROM destination_managers WHERE destination_id=$1 AND user_telegram_id=$2",
                dest_id, user_tg,
            )
            return v is not None

    async def list_managers(self, dest_id: int) -> List[Dict[str, Any]]:
        async with self.pool.acquire() as con:
            rows = await con.fetch(
                "SELECT * FROM destination_managers WHERE destination_id=$1 ORDER BY role, added_at",
                dest_id,
            )
            return [dict(r) for r in rows]

    async def user_destinations(self, user_tg: int) -> List[Dict[str, Any]]:
        async with self.pool.acquire() as con:
            rows = await con.fetch(
                """SELECT d.* FROM destinations d
                   JOIN destination_managers m ON m.destination_id=d.id
                   WHERE m.user_telegram_id=$1
                   ORDER BY d.title""",
                user_tg,
            )
            return [dict(r) for r in rows]

    async def all_destinations(self, offset: int = 0, limit: int = 50) -> List[Dict[str, Any]]:
        async with self.pool.acquire() as con:
            rows = await con.fetch(
                "SELECT * FROM destinations ORDER BY registered_at DESC OFFSET $1 LIMIT $2",
                offset, limit,
            )
            return [dict(r) for r in rows]

    async def count_destinations(self) -> int:
        async with self.pool.acquire() as con:
            return await con.fetchval("SELECT COUNT(*) FROM destinations") or 0

    async def update_destination_status(self, dest_id: int, status: str, err: str | None = None) -> None:
        async with self.pool.acquire() as con:
            await con.execute(
                "UPDATE destinations SET status=$2, last_error=$3, last_verified=NOW() WHERE id=$1",
                dest_id, status, err,
            )

    async def remove_destination(self, dest_id: int) -> None:
        async with self.pool.acquire() as con:
            await con.execute("DELETE FROM destinations WHERE id=$1", dest_id)

    # ---------------- Drafts ----------------
    async def create_draft(
        self, user_tg: int, source_chat_id: int, source_message_id: int,
        message_json: Dict[str, Any], emoji_mode: str,
    ) -> int:
        async with self.pool.acquire() as con:
            return await con.fetchval(
                """INSERT INTO drafts(user_telegram_id, source_chat_id, source_message_id,
                                      source_message_json, emoji_mode)
                   VALUES($1,$2,$3,$4,$5) RETURNING id""",
                user_tg, source_chat_id, source_message_id,
                json.dumps(message_json), emoji_mode,
            )

    async def get_draft(self, draft_id: int, user_tg: int) -> Optional[Dict[str, Any]]:
        async with self.pool.acquire() as con:
            row = await con.fetchrow(
                "SELECT * FROM drafts WHERE id=$1 AND user_telegram_id=$2", draft_id, user_tg
            )
            return dict(row) if row else None

    async def list_drafts(self, user_tg: int) -> List[Dict[str, Any]]:
        async with self.pool.acquire() as con:
            rows = await con.fetch(
                "SELECT * FROM drafts WHERE user_telegram_id=$1 ORDER BY updated_at DESC", user_tg
            )
            return [dict(r) for r in rows]

    async def update_draft_selection(self, draft_id: int, user_tg: int, dest_ids: Sequence[int]) -> None:
        async with self.pool.acquire() as con:
            await con.execute(
                "UPDATE drafts SET selected_destinations=$3::jsonb, updated_at=NOW() WHERE id=$1 AND user_telegram_id=$2",
                draft_id, user_tg, json.dumps(list(dest_ids)),
            )

    async def update_draft_emoji_mode(self, draft_id: int, user_tg: int, mode: str) -> None:
        async with self.pool.acquire() as con:
            await con.execute(
                "UPDATE drafts SET emoji_mode=$3, updated_at=NOW() WHERE id=$1 AND user_telegram_id=$2",
                draft_id, user_tg, mode,
            )

    async def delete_draft(self, draft_id: int, user_tg: int) -> None:
        async with self.pool.acquire() as con:
            await con.execute(
                "DELETE FROM drafts WHERE id=$1 AND user_telegram_id=$2", draft_id, user_tg
            )

    # ---------------- Campaigns ----------------
    async def count_open_campaigns(self, user_tg: int) -> int:
        async with self.pool.acquire() as con:
            return await con.fetchval(
                """SELECT COUNT(*) FROM campaigns
                   WHERE user_telegram_id=$1 AND status IN ('scheduled','queued','running','paused')""",
                user_tg,
            ) or 0

    async def create_campaign(
        self, user_tg: int, source_chat_id: int, source_message_id: int,
        message_json: Dict[str, Any], summary: str, emoji_mode: str,
        status: str, scheduled_at: Optional[datetime],
        is_admin: bool = False,
    ) -> int:
        async with self.pool.acquire() as con:
            return await con.fetchval(
                """INSERT INTO campaigns(user_telegram_id, source_chat_id, source_message_id,
                                         source_message_json, content_summary, emoji_mode,
                                         status, scheduled_at, is_admin_campaign)
                   VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING id""",
                user_tg, source_chat_id, source_message_id,
                json.dumps(message_json), summary, emoji_mode,
                status, scheduled_at, is_admin,
            )

    async def add_campaign_targets(self, campaign_id: int, targets: Sequence[Dict[str, Any]]) -> None:
        async with self.pool.acquire() as con:
            async with con.transaction():
                for t in targets:
                    await con.execute(
                        """INSERT INTO campaign_targets(campaign_id, destination_id, target_chat_id)
                           VALUES($1,$2,$3) ON CONFLICT DO NOTHING""",
                        campaign_id, t.get("destination_id"), t["target_chat_id"],
                    )

    async def get_campaign(self, campaign_id: int) -> Optional[Dict[str, Any]]:
        async with self.pool.acquire() as con:
            row = await con.fetchrow("SELECT * FROM campaigns WHERE id=$1", campaign_id)
            return dict(row) if row else None

    async def get_campaign_targets(self, campaign_id: int) -> List[Dict[str, Any]]:
        async with self.pool.acquire() as con:
            rows = await con.fetch(
                "SELECT * FROM campaign_targets WHERE campaign_id=$1 ORDER BY id", campaign_id
            )
            return [dict(r) for r in rows]

    async def list_user_campaigns(self, user_tg: int, offset: int, limit: int) -> List[Dict[str, Any]]:
        async with self.pool.acquire() as con:
            rows = await con.fetch(
                """SELECT * FROM campaigns WHERE user_telegram_id=$1
                   ORDER BY created_at DESC OFFSET $2 LIMIT $3""",
                user_tg, offset, limit,
            )
            return [dict(r) for r in rows]

    async def count_user_campaigns(self, user_tg: int) -> int:
        async with self.pool.acquire() as con:
            return await con.fetchval(
                "SELECT COUNT(*) FROM campaigns WHERE user_telegram_id=$1", user_tg
            ) or 0

    async def list_all_campaigns(self, offset: int, limit: int) -> List[Dict[str, Any]]:
        async with self.pool.acquire() as con:
            rows = await con.fetch(
                "SELECT * FROM campaigns ORDER BY created_at DESC OFFSET $1 LIMIT $2",
                offset, limit,
            )
            return [dict(r) for r in rows]

    async def set_campaign_status(self, campaign_id: int, status: str) -> None:
        async with self.pool.acquire() as con:
            if status == "running":
                await con.execute(
                    "UPDATE campaigns SET status=$2, started_at=COALESCE(started_at,NOW()) WHERE id=$1",
                    campaign_id, status,
                )
            elif status in ("completed", "completed_with_errors", "failed", "cancelled"):
                await con.execute(
                    "UPDATE campaigns SET status=$2, completed_at=NOW() WHERE id=$1",
                    campaign_id, status,
                )
            else:
                await con.execute("UPDATE campaigns SET status=$2 WHERE id=$1", campaign_id, status)

    async def update_target(
        self, target_id: int, status: str, err: Optional[str] = None,
        err_cat: Optional[str] = None, bump_attempt: bool = True,
    ) -> None:
        async with self.pool.acquire() as con:
            await con.execute(
                """UPDATE campaign_targets SET status=$2, last_error=$3, error_category=$4,
                       last_attempt=NOW(),
                       attempts = attempts + CASE WHEN $5 THEN 1 ELSE 0 END
                   WHERE id=$1""",
                target_id, status, err, err_cat, bump_attempt,
            )

    async def recover_stuck_targets(self) -> int:
        async with self.pool.acquire() as con:
            res = await con.execute(
                """UPDATE campaign_targets SET status='uncertain',
                        last_error='Worker interrupted; delivery may or may not have succeeded'
                   WHERE status='sending' AND last_attempt < NOW() - INTERVAL '5 minutes'""",
            )
            log.info("Recovered stuck targets: %s", res)
            return 0

    async def count_pending_targets(self, campaign_id: int) -> int:
        async with self.pool.acquire() as con:
            return await con.fetchval(
                "SELECT COUNT(*) FROM campaign_targets WHERE campaign_id=$1 AND status IN ('pending','sending')",
                campaign_id,
            ) or 0

    async def campaign_stats(self, campaign_id: int) -> Dict[str, int]:
        async with self.pool.acquire() as con:
            rows = await con.fetch(
                "SELECT status, COUNT(*) AS c FROM campaign_targets WHERE campaign_id=$1 GROUP BY status",
                campaign_id,
            )
            return {r["status"]: r["c"] for r in rows}

    async def user_stats(self, user_tg: int) -> Dict[str, int]:
        async with self.pool.acquire() as con:
            return {
                "total_campaigns": await con.fetchval(
                    "SELECT COUNT(*) FROM campaigns WHERE user_telegram_id=$1", user_tg) or 0,
                "scheduled": await con.fetchval(
                    "SELECT COUNT(*) FROM campaigns WHERE user_telegram_id=$1 AND status='scheduled'", user_tg) or 0,
                "repeating": await con.fetchval(
                    "SELECT COUNT(*) FROM scheduled_jobs WHERE user_telegram_id=$1 AND status='active' AND repeat_interval_seconds IS NOT NULL",
                    user_tg) or 0,
                "total_targets": await con.fetchval(
                    """SELECT COUNT(*) FROM campaign_targets t
                       JOIN campaigns c ON c.id=t.campaign_id WHERE c.user_telegram_id=$1""",
                    user_tg) or 0,
                "ok": await con.fetchval(
                    """SELECT COUNT(*) FROM campaign_targets t
                       JOIN campaigns c ON c.id=t.campaign_id
                       WHERE c.user_telegram_id=$1 AND t.status='success'""",
                    user_tg) or 0,
                "fail": await con.fetchval(
                    """SELECT COUNT(*) FROM campaign_targets t
                       JOIN campaigns c ON c.id=t.campaign_id
                       WHERE c.user_telegram_id=$1 AND t.status IN ('failed','uncertain')""",
                    user_tg) or 0,
            }

    # ---------------- Scheduler jobs ----------------
    async def create_scheduled_job(
        self, campaign_id: int, user_tg: int, next_run_at: datetime,
        repeat_interval: Optional[int], max_reps: Optional[int], end_at: Optional[datetime],
    ) -> int:
        async with self.pool.acquire() as con:
            return await con.fetchval(
                """INSERT INTO scheduled_jobs(campaign_id, user_telegram_id, next_run_at,
                                              repeat_interval_seconds, max_repetitions, end_at)
                   VALUES($1,$2,$3,$4,$5,$6) RETURNING id""",
                campaign_id, user_tg, next_run_at, repeat_interval, max_reps, end_at,
            )

    async def due_jobs(self, now: datetime, limit: int = 10) -> List[Dict[str, Any]]:
        async with self.pool.acquire() as con:
            rows = await con.fetch(
                """SELECT * FROM scheduled_jobs
                   WHERE status='active' AND next_run_at <= $1
                   ORDER BY next_run_at LIMIT $2 FOR UPDATE SKIP LOCKED""",
                now, limit,
            )
            return [dict(r) for r in rows]

    async def advance_job(
        self, job_id: int, next_run_at: Optional[datetime], status: str,
    ) -> None:
        async with self.pool.acquire() as con:
            await con.execute(
                """UPDATE scheduled_jobs SET
                     runs_done = runs_done + 1,
                     next_run_at = COALESCE($2, next_run_at),
                     status = $3
                   WHERE id=$1""",
                job_id, next_run_at, status,
            )

    async def list_user_jobs(self, user_tg: int) -> List[Dict[str, Any]]:
        async with self.pool.acquire() as con:
            rows = await con.fetch(
                """SELECT * FROM scheduled_jobs WHERE user_telegram_id=$1
                   AND status IN ('active','paused') ORDER BY next_run_at""",
                user_tg,
            )
            return [dict(r) for r in rows]

    async def get_job(self, job_id: int) -> Optional[Dict[str, Any]]:
        async with self.pool.acquire() as con:
            row = await con.fetchrow("SELECT * FROM scheduled_jobs WHERE id=$1", job_id)
            return dict(row) if row else None

    async def set_job_status(self, job_id: int, status: str) -> None:
        async with self.pool.acquire() as con:
            await con.execute("UPDATE scheduled_jobs SET status=$2 WHERE id=$1", job_id, status)

    # ---------------- Premium emojis ----------------
    async def add_premium_emoji(self, unicode_fallback: str, document_id: str,
                                description: str, added_by: int) -> int:
        async with self.pool.acquire() as con:
            return await con.fetchval(
                """INSERT INTO premium_emojis(unicode_fallback, document_id, description, added_by)
                   VALUES($1,$2,$3,$4) ON CONFLICT (document_id) DO UPDATE
                   SET unicode_fallback=EXCLUDED.unicode_fallback RETURNING id""",
                unicode_fallback, document_id, description, added_by,
            )

    async def get_premium_emoji(self, unicode_fallback: str) -> Optional[Dict[str, Any]]:
        async with self.pool.acquire() as con:
            row = await con.fetchrow(
                "SELECT * FROM premium_emojis WHERE unicode_fallback=$1 LIMIT 1", unicode_fallback
            )
            return dict(row) if row else None

    # ---------------- Settings / audit ----------------
    async def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        async with self.pool.acquire() as con:
            v = await con.fetchval("SELECT value FROM bot_settings WHERE key=$1", key)
            return v if v is not None else default

    async def set_setting(self, key: str, value: str) -> None:
        async with self.pool.acquire() as con:
            await con.execute(
                """INSERT INTO bot_settings(key,value) VALUES($1,$2)
                   ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()""",
                key, value,
            )

    async def get_user_tz(self, user_tg: int) -> str:
        async with self.pool.acquire() as con:
            v = await con.fetchval(
                "SELECT timezone FROM user_settings WHERE user_telegram_id=$1", user_tg
            )
            return v or "UTC"

    async def set_user_tz(self, user_tg: int, tz: str) -> None:
        async with self.pool.acquire() as con:
            await con.execute(
                """INSERT INTO user_settings(user_telegram_id, timezone) VALUES($1,$2)
                   ON CONFLICT (user_telegram_id) DO UPDATE SET timezone=EXCLUDED.timezone, updated_at=NOW()""",
                user_tg, tz,
            )

    async def log_admin_action(self, admin_tg: int, action: str,
                               target_type: Optional[str] = None,
                               target_id: Optional[str] = None,
                               details: Optional[Dict[str, Any]] = None) -> None:
        async with self.pool.acquire() as con:
            await con.execute(
                """INSERT INTO admin_actions(admin_telegram_id, action, target_type, target_id, details)
                   VALUES($1,$2,$3,$4,$5)""",
                admin_tg, action, target_type, target_id,
                json.dumps(details) if details else None,
            )

    async def admin_overview(self) -> Dict[str, Any]:
        async with self.pool.acquire() as con:
            return {
                "users": await con.fetchval("SELECT COUNT(*) FROM users") or 0,
                "destinations": await con.fetchval("SELECT COUNT(*) FROM destinations") or 0,
                "campaigns": await con.fetchval("SELECT COUNT(*) FROM campaigns") or 0,
                "running": await con.fetchval("SELECT COUNT(*) FROM campaigns WHERE status='running'") or 0,
                "scheduled": await con.fetchval("SELECT COUNT(*) FROM campaigns WHERE status='scheduled'") or 0,
                "failed_targets": await con.fetchval(
                    "SELECT COUNT(*) FROM campaign_targets WHERE status IN ('failed','uncertain')") or 0,
            }
