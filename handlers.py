import logging
from datetime import datetime, timezone
from telegram import Update
from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, ChatMemberHandler
from config import ADMIN_IDS, MAX_CAMPAIGN_TARGETS
from database import get_pool

logger = logging.getLogger(__name__)


def is_admin(user_id: int | None) -> bool:
    return user_id is not None and user_id in ADMIN_IDS


async def save_user(user) -> None:
    if not user or user.is_bot:
        return
    async with get_pool().acquire() as conn:
        await conn.execute("""
            INSERT INTO users(user_id, username, first_name, last_seen_at)
            VALUES($1,$2,$3,NOW()) ON CONFLICT(user_id) DO UPDATE SET
            username=EXCLUDED.username, first_name=EXCLUDED.first_name, last_seen_at=NOW()
        """, user.id, user.username, user.first_name or "")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await save_user(update.effective_user)
    if update.effective_message:
        await update.effective_message.reply_text(
            "Welcome to Telegram Broadcast Pro.\n\n"
            "Add me as an administrator to your group/channel, then register it.\n"
            "Reply to a source message with /broadcast to send now, or use "
            "/schedule YYYY-MM-DDTHH:MM [INTERVAL_MINUTES] for UTC scheduling.\n\nUse /help for commands."
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await save_user(update.effective_user)
    text = (
        "Commands\n/start, /help - start and help\n/register - register this group (group admins only)\n"
        "/destinations - list your destinations\nReply to a message with /broadcast - send now\n"
        "Reply to a message with /schedule YYYY-MM-DDTHH:MM [INTERVAL_MINUTES] - schedule in UTC\n"
        "/campaigns - recent campaigns\n/cancel ID - cancel a pending campaign"
    )
    if is_admin(update.effective_user.id if update.effective_user else None):
        text += "\n\nMaster admin: reply with /adminbroadcast users|groups|channels|all; /adminstats"
    if update.effective_message:
        await update.effective_message.reply_text(text)


async def register_destination(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await save_user(update.effective_user)
    message, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if not message or not chat or not user:
        return
    if chat.type not in ("group", "supergroup"):
        await message.reply_text("Run /register inside the group you want to register.")
        return
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        if member.status not in ("administrator", "creator"):
            await message.reply_text("Only a group administrator can register this destination.")
            return
        bot_member = await context.bot.get_chat_member(chat.id, context.bot.id)
        if bot_member.status != "administrator":
            await message.reply_text("Please promote me to group administrator first, with permission to post messages.")
            return
    except TelegramError as exc:
        logger.info("Permission check failed: %s", exc)
        await message.reply_text("I couldn't verify permissions. Make sure I am a group administrator and try again.")
        return
    async with get_pool().acquire() as conn:
        await conn.execute("""
            INSERT INTO destinations(chat_id,owner_id,chat_title,chat_type,is_active)
            VALUES($1,$2,$3,$4,TRUE) ON CONFLICT(chat_id) DO UPDATE SET
            owner_id=EXCLUDED.owner_id, chat_title=EXCLUDED.chat_title,
            chat_type=EXCLUDED.chat_type, is_active=TRUE
        """, chat.id, user.id, chat.title or str(chat.id), chat.type)
    await message.reply_text("Destination registered to your account. Use /destinations in private chat to review it.")


async def chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    change = update.my_chat_member
    if not change:
        return
    chat, actor = change.chat, change.from_user
    if change.new_chat_member.status != ChatMemberStatus.ADMINISTRATOR:
        return
    if chat.type not in ("channel", "group", "supergroup") or not actor or actor.is_bot:
        return
    await save_user(actor)
    try:
        bot_member = await context.bot.get_chat_member(chat.id, context.bot.id)
        if bot_member.status != ChatMemberStatus.ADMINISTRATOR:
            return
    except TelegramError:
        return
    async with get_pool().acquire() as conn:
        await conn.execute("""
            INSERT INTO destinations(chat_id,owner_id,chat_title,chat_type,is_active)
            VALUES($1,$2,$3,$4,TRUE) ON CONFLICT(chat_id) DO UPDATE SET
            chat_title=EXCLUDED.chat_title, chat_type=EXCLUDED.chat_type, is_active=TRUE
        """, chat.id, actor.id, chat.title or str(chat.id), chat.type)
    logger.info("Registered destination %s for user %s", chat.id, actor.id)


async def destinations(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await save_user(update.effective_user)
    user, message = update.effective_user, update.effective_message
    if not user or not message:
        return
    async with get_pool().acquire() as conn:
        rows = await conn.fetch("SELECT chat_id,chat_title,chat_type,is_active FROM destinations WHERE owner_id=$1 ORDER BY added_at DESC", user.id)
    if not rows:
        await message.reply_text("No destinations yet. Add me as a group administrator and run /register, or promote me in a channel.")
        return
    lines = ["Your destinations:"]
    for row in rows:
        lines.append(f"• {row['chat_title']} ({row['chat_type']}) — {'active' if row['is_active'] else 'inactive'} — ID {row['chat_id']}")
    await message.reply_text("\n".join(lines))


async def owned_targets(user_id: int) -> list[int]:
    async with get_pool().acquire() as conn:
        rows = await conn.fetch("SELECT chat_id FROM destinations WHERE owner_id=$1 AND is_active=TRUE ORDER BY added_at", user_id)
    return [int(row['chat_id']) for row in rows]


async def create_campaign(owner_id: int, scope: str, targets: list[int], source_chat_id: int,
                          source_message_id: int, scheduled_at: datetime, repeat_minutes: int | None) -> int:
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO campaigns(owner_id,target_scope,target_ids,source_chat_id,source_message_id,
                status,scheduled_at,repeat_interval_minutes,total_targets)
            VALUES($1,$2,$3::BIGINT[],$4,$5,'scheduled',$6,$7,$8) RETURNING id
        """, owner_id, scope, targets, source_chat_id, source_message_id, scheduled_at, repeat_minutes, len(targets))
    return int(row['id'])


async def broadcast_now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await save_user(update.effective_user)
    message, user = update.effective_message, update.effective_user
    if not message or not user:
        return
    source = message.reply_to_message
    if not source:
        await message.reply_text("Reply directly to the message you want to copy with /broadcast.")
        return
    targets = await owned_targets(user.id)
    if not targets:
        await message.reply_text("No active destinations. Register a group or add me as a channel administrator first.")
        return
    if len(targets) > MAX_CAMPAIGN_TARGETS:
        await message.reply_text(f"Target count exceeds MAX_CAMPAIGN_TARGETS ({MAX_CAMPAIGN_TARGETS}).")
        return
    campaign_id = await create_campaign(user.id, "owned", targets, source.chat_id, source.message_id, datetime.now(timezone.utc), None)
    await message.reply_text(f"Campaign #{campaign_id} queued for {len(targets)} destination(s). Use /campaigns for status.")


async def schedule_campaign(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await save_user(update.effective_user)
    message, user = update.effective_message, update.effective_user
    if not message or not user:
        return
    source = message.reply_to_message
    if not source:
        await message.reply_text("Reply to the content message with /schedule YYYY-MM-DDTHH:MM [INTERVAL_MINUTES].")
        return
    if not 1 <= len(context.args) <= 2:
        await message.reply_text("Usage: /schedule YYYY-MM-DDTHH:MM [INTERVAL_MINUTES] (UTC). Reply to the source message.")
        return
    try:
        scheduled = datetime.strptime(context.args[0], "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc)
        repeat = int(context.args[1]) if len(context.args) == 2 else None
        if repeat is not None and repeat < 1:
            raise ValueError
    except ValueError:
        await message.reply_text("Invalid schedule. Use a future UTC time like /schedule 2026-10-10T12:00; repeat interval must be at least 1 minute.")
        return
    if scheduled <= datetime.now(timezone.utc):
        await message.reply_text("Please choose a future UTC date and time.")
        return
    targets = await owned_targets(user.id)
    if not targets:
        await message.reply_text("No active destinations found. Register a group or channel first.")
        return
    if len(targets) > MAX_CAMPAIGN_TARGETS:
        await message.reply_text(f"Target count exceeds MAX_CAMPAIGN_TARGETS ({MAX_CAMPAIGN_TARGETS}).")
        return
    campaign_id = await create_campaign(user.id, "owned", targets, source.chat_id, source.message_id, scheduled, repeat)
    suffix = f"; repeats every {repeat} minute(s)" if repeat else ""
    await message.reply_text(f"Campaign #{campaign_id} scheduled for {scheduled:%Y-%m-%d %H:%M UTC}{suffix}. Targets: {len(targets)}.")


async def list_campaigns(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await save_user(update.effective_user)
    user, message = update.effective_user, update.effective_message
    if not user or not message:
        return
    async with get_pool().acquire() as conn:
        if is_admin(user.id):
            rows = await conn.fetch("SELECT id,target_scope,status,scheduled_at,total_targets,sent_count,failed_count FROM campaigns ORDER BY id DESC LIMIT 10")
        else:
            rows = await conn.fetch("SELECT id,target_scope,status,scheduled_at,total_targets,sent_count,failed_count FROM campaigns WHERE owner_id=$1 ORDER BY id DESC LIMIT 10", user.id)
    if not rows:
        await message.reply_text("No campaigns found.")
        return
    lines = ["Recent campaigns:"]
    for row in rows:
        lines.append(f"#{row['id']} | {row['status']} | {row['sent_count']}/{row['total_targets']} sent | {row['failed_count']} failed | {row['scheduled_at']:%Y-%m-%d %H:%M UTC} | {row['target_scope']}")
    await message.reply_text("\n".join(lines))


async def cancel_campaign(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await save_user(update.effective_user)
    user, message = update.effective_user, update.effective_message
    if not user or not message:
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        await message.reply_text("Usage: /cancel CAMPAIGN_ID")
        return
    campaign_id = int(context.args[0])
    async with get_pool().acquire() as conn:
        if is_admin(user.id):
            result = await conn.execute("UPDATE campaigns SET status='cancelled',updated_at=NOW() WHERE id=$1 AND status='scheduled'", campaign_id)
        else:
            result = await conn.execute("UPDATE campaigns SET status='cancelled',updated_at=NOW() WHERE id=$1 AND owner_id=$2 AND status='scheduled'", campaign_id, user.id)
    await message.reply_text(f"Campaign #{campaign_id} cancelled." if result.endswith(" 1") else "Campaign not found, not yours, or already running/completed.")


async def admin_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await save_user(update.effective_user)
    message, user = update.effective_message, update.effective_user
    if not message or not user:
        return
    if not is_admin(user.id):
        await message.reply_text("Master administrators only.")
        return
    source = message.reply_to_message
    if not source:
        await message.reply_text("Reply to a message with /adminbroadcast users|groups|channels|all.")
        return
    if len(context.args) != 1 or context.args[0].lower() not in ("users", "groups", "channels", "all"):
        await message.reply_text("Usage: /adminbroadcast users|groups|channels|all (reply to source message)")
        return
    scope = context.args[0].lower()
    async with get_pool().acquire() as conn:
        if scope == "users":
            rows = await conn.fetch("SELECT user_id AS chat_id FROM users WHERE is_blocked=FALSE")
        elif scope == "groups":
            rows = await conn.fetch("SELECT chat_id FROM destinations WHERE is_active=TRUE AND chat_type IN ('group','supergroup')")
        elif scope == "channels":
            rows = await conn.fetch("SELECT chat_id FROM destinations WHERE is_active=TRUE AND chat_type='channel'")
        else:
            rows = await conn.fetch("SELECT user_id AS chat_id FROM users WHERE is_blocked=FALSE")
            rows += await conn.fetch("SELECT chat_id FROM destinations WHERE is_active=TRUE")
    targets = list(dict.fromkeys(int(row['chat_id']) for row in rows))
    if not targets:
        await message.reply_text(f"No targets available for {scope}.")
        return
    if len(targets) > MAX_CAMPAIGN_TARGETS:
        await message.reply_text(f"{len(targets)} targets exceeds MAX_CAMPAIGN_TARGETS={MAX_CAMPAIGN_TARGETS}.")
        return
    campaign_id = await create_campaign(user.id, scope, targets, source.chat_id, source.message_id, datetime.now(timezone.utc), None)
    await message.reply_text(f"Admin campaign #{campaign_id} queued for {len(targets)} target(s), scope={scope}.")


async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await save_user(update.effective_user)
    user, message = update.effective_user, update.effective_message
    if not user or not message:
        return
    if not is_admin(user.id):
        await message.reply_text("Master administrators only.")
        return
    async with get_pool().acquire() as conn:
        users = await conn.fetchval("SELECT COUNT(*) FROM users")
        dests = await conn.fetchval("SELECT COUNT(*) FROM destinations WHERE is_active=TRUE")
        campaigns = await conn.fetchval("SELECT COUNT(*) FROM campaigns")
        pending = await conn.fetchval("SELECT COUNT(*) FROM campaigns WHERE status='scheduled'")
    await message.reply_text(f"System stats\nUsers: {users}\nActive destinations: {dests}\nCampaigns: {campaigns}\nPending: {pending}")


def register_handlers(application: Application) -> None:
    for command, callback in [
        ("start", start), ("help", help_command), ("register", register_destination),
        ("destinations", destinations), ("broadcast", broadcast_now), ("schedule", schedule_campaign),
        ("campaigns", list_campaigns), ("cancel", cancel_campaign),
        ("adminbroadcast", admin_broadcast), ("adminstats", admin_stats),
    ]:
        application.add_handler(CommandHandler(command, callback))
    application.add_handler(ChatMemberHandler(chat_member_update, ChatMemberHandler.MY_CHAT_MEMBER))
