import asyncpg
from config import DATABASE_URL

_pool: asyncpg.Pool | None = None


async def init_db() -> asyncpg.Pool:
    global _pool
    if _pool is not None:
        return _pool
    _pool = await asyncpg.create_pool(
        dsn=DATABASE_URL, min_size=1, max_size=8, command_timeout=30,
        server_settings={"application_name": "telegram-broadcast-pro"},
    )
    async with _pool.acquire() as conn:
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            username TEXT,
            first_name TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            is_blocked BOOLEAN NOT NULL DEFAULT FALSE
        );
        CREATE TABLE IF NOT EXISTS destinations (
            chat_id BIGINT PRIMARY KEY,
            owner_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            chat_title TEXT NOT NULL DEFAULT '',
            chat_type TEXT NOT NULL CHECK (chat_type IN ('group','supergroup','channel')),
            added_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            is_active BOOLEAN NOT NULL DEFAULT TRUE
        );
        CREATE INDEX IF NOT EXISTS idx_destinations_owner ON destinations(owner_id);
        CREATE INDEX IF NOT EXISTS idx_destinations_type ON destinations(chat_type, is_active);
        CREATE TABLE IF NOT EXISTS destination_access (
            chat_id BIGINT NOT NULL REFERENCES destinations(chat_id) ON DELETE CASCADE,
            user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            added_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (chat_id, user_id)
        );
        CREATE INDEX IF NOT EXISTS idx_destination_access_user ON destination_access(user_id);
        INSERT INTO destination_access(chat_id, user_id)
        SELECT chat_id, owner_id FROM destinations
        ON CONFLICT(chat_id, user_id) DO NOTHING;
        CREATE TABLE IF NOT EXISTS campaigns (
            id BIGSERIAL PRIMARY KEY,
            owner_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            target_scope TEXT NOT NULL,
            target_ids BIGINT[] NOT NULL DEFAULT ARRAY[]::BIGINT[],
            source_chat_id BIGINT NOT NULL,
            source_message_id BIGINT NOT NULL,
            status TEXT NOT NULL DEFAULT 'scheduled' CHECK (status IN ('scheduled','running','completed','cancelled','failed')),
            scheduled_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            repeat_interval_minutes INTEGER,
            total_targets INTEGER NOT NULL DEFAULT 0,
            sent_count INTEGER NOT NULL DEFAULT 0,
            failed_count INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_error TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_campaigns_due ON campaigns(status, scheduled_at);
        CREATE INDEX IF NOT EXISTS idx_campaigns_owner ON campaigns(owner_id, created_at DESC);
        CREATE TABLE IF NOT EXISTS campaign_deliveries (
            id BIGSERIAL PRIMARY KEY,
            campaign_id BIGINT NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
            target_chat_id BIGINT NOT NULL,
            success BOOLEAN NOT NULL,
            error_text TEXT,
            delivered_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_deliveries_campaign ON campaign_deliveries(campaign_id);
        CREATE TABLE IF NOT EXISTS premium_emojis (
            emoji_key TEXT PRIMARY KEY,
            custom_emoji_id TEXT NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """)
        # Recover interrupted campaigns after a process crash. Delivery records remain available for audit.
        await conn.execute("UPDATE campaigns SET status='scheduled', updated_at=NOW() WHERE status='running'")
    return _pool


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool has not been initialized")
    return _pool


async def close_db() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
