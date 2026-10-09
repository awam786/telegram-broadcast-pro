import asyncio
import logging
from telegram.error import Forbidden, BadRequest, RetryAfter, TelegramError
from config import SEND_DELAY_SECONDS
from database import get_pool

logger = logging.getLogger(__name__)


async def campaign_worker(application) -> None:
    """Claim due campaigns atomically; scheduled rows remain in PostgreSQL across restarts."""
    while True:
        try:
            campaign = await claim_due_campaign()
            if campaign is None:
                await asyncio.sleep(2)
                continue
            await deliver_campaign(application, campaign)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Campaign worker iteration failed")
            await asyncio.sleep(3)


async def claim_due_campaign():
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("""
                SELECT * FROM campaigns
                WHERE status='scheduled' AND scheduled_at <= NOW()
                ORDER BY scheduled_at, id FOR UPDATE SKIP LOCKED LIMIT 1
            """)
            if row is None:
                return None
            await conn.execute("UPDATE campaigns SET status='running', updated_at=NOW() WHERE id=$1", row["id"])
            return dict(row)


async def deliver_campaign(application, campaign: dict) -> None:
    pool = get_pool()
    targets = list(dict.fromkeys(int(value) for value in campaign["target_ids"]))
    sent = failed = 0
    if not targets:
        async with pool.acquire() as conn:
            await conn.execute("UPDATE campaigns SET status='completed', total_targets=0, updated_at=NOW() WHERE id=$1", campaign["id"])
        return
    async with pool.acquire() as conn:
        await conn.execute("UPDATE campaigns SET total_targets=$2, updated_at=NOW() WHERE id=$1", campaign["id"], len(targets))

    for target_id in targets:
        success, error_text = False, None
        try:
            await application.bot.copy_message(
                chat_id=target_id,
                from_chat_id=campaign["source_chat_id"],
                message_id=campaign["source_message_id"],
            )
            success = True
            sent += 1
        except RetryAfter as exc:
            delay = float(getattr(exc, "retry_after", 1))
            await asyncio.sleep(max(1.0, delay))
            try:
                await application.bot.copy_message(
                    chat_id=target_id,
                    from_chat_id=campaign["source_chat_id"],
                    message_id=campaign["source_message_id"],
                )
                success = True
                sent += 1
            except Exception as retry_exc:
                failed += 1
                error_text = str(retry_exc)[:1000]
        except (Forbidden, BadRequest, TelegramError) as exc:
            failed += 1
            error_text = str(exc)[:1000]
            if isinstance(exc, Forbidden):
                async with pool.acquire() as conn:
                    await conn.execute("UPDATE destinations SET is_active=FALSE WHERE chat_id=$1", target_id)
        except Exception as exc:
            failed += 1
            error_text = str(exc)[:1000]
            logger.warning("Campaign %s failed for %s: %s", campaign["id"], target_id, exc)

        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO campaign_deliveries(campaign_id,target_chat_id,success,error_text) VALUES($1,$2,$3,$4)",
                campaign["id"], target_id, success, error_text,
            )
            await conn.execute(
                "UPDATE campaigns SET sent_count=$2, failed_count=$3, updated_at=NOW() WHERE id=$1",
                campaign["id"], sent, failed,
            )
        if SEND_DELAY_SECONDS:
            await asyncio.sleep(SEND_DELAY_SECONDS)

    async with pool.acquire() as conn:
        if campaign["repeat_interval_minutes"]:
            interval = int(campaign["repeat_interval_minutes"])
            await conn.execute("""
                UPDATE campaigns SET status='scheduled',
                scheduled_at=GREATEST(scheduled_at + ($2 * INTERVAL '1 minute'), NOW() + INTERVAL '1 second'),
                updated_at=NOW() WHERE id=$1
            """, campaign["id"], interval)
        else:
            await conn.execute("UPDATE campaigns SET status='completed', updated_at=NOW() WHERE id=$1", campaign["id"])
    logger.info("Campaign %s finished: %s sent, %s failed, %s targets", campaign["id"], sent, failed, len(targets))
