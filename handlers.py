import logging
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
)

from config import ADMIN_IDS, MAX_CAMPAIGN_TARGETS
from database import get_pool

logger = logging.getLogger(__name__)


def is_admin(user_id: int | None) -> bool:
    return user_id is not None and user_id in ADMIN_IDS


async def save_user(user) -> None:
    if not user or user.is_bot:
        return
    async with get_pool().acquire() as conn:
        await conn.execute(
            """INSERT INTO users(user_id, username, first_name, last_seen_at)
               VALUES($1,$2,$3,NOW())
               ON CONFLICT(user_id) DO UPDATE SET username=EXCLUDED.username,
               first_name=EXCLUDED.first_name, last_seen_at=NOW()""",
            user.id, user.username, user.first_name or "",
        )


def user_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📣 New Broadcast", callback_data="u:broadcast"), InlineKeyboardButton("🗓 Schedule", callback_data="u:schedule")],
        [InlineKeyboardButton("📡 Destinations", callback_data="u:destinations"), InlineKeyboardButton("📊 Campaigns", callback_data="u:campaigns")],
        [InlineKeyboardButton("❔ User Help", callback_data="u:help")],
    ])


def admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📈 System Stats", callback_data="a:stats"), InlineKeyboardButton("📋 Admin Help", callback_data="a:help")],
        [InlineKeyboardButton("👤 Broadcast to Users", callback_data="a:scope:users")],
        [InlineKeyboardButton("👥 Broadcast to Groups", callback_data="a:scope:groups"), InlineKeyboardButton("📢 Broadcast to Channels", callback_data="a:scope:channels")],
        [InlineKeyboardButton("🌐 Broadcast to All", callback_data="a:scope:all")],
        [InlineKeyboardButton("⬅️ User Menu", callback_data="u:home")],
    ])


def back_button(admin: bool = False) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Menu", callback_data="a:home" if admin else "u:home")]])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    await save_user(user)
    if not message or not user:
        return
    greeting = (
        "🚀 <b>Welcome to Telegram Broadcast Pro</b>\n\n"
        "A smarter workspace for managing destinations, broadcasting messages, and tracking campaigns.\n\n"
        "Choose an option below to get started. Use /help for user commands."
    )
    keyboard = user_menu()
    if is_admin(user.id):
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🧭 User Dashboard", callback_data="u:home")],
            [InlineKeyboardButton("🛡 Admin Dashboard", callback_data="a:home")],
            [InlineKeyboardButton("❔ User Help", callback_data="u:help")],
        ])
        greeting += "\n\n🛡 <b>Administrator access detected.</b>"
    await message.reply_text(greeting, reply_markup=keyboard, parse_mode="HTML", disable_web_page_preview=True)


USER_HELP = (
    "<b>📘 USER HELP</b>\n\n"
    "<b>Available commands</b>\n"
    "/start — Open the main menu\n"
    "/help — Show user commands\n"
    "/register — Register a group (group admins only)\n"
    "/destinations — View your registered destinations\n"
    "/broadcast — Send a message now (reply to the source message)\n"
    "/schedule YYYY-MM-DDTHH:MM [INTERVAL_MINUTES] — Schedule a message in UTC\n"
    "/campaigns — View your recent campaigns\n"
    "/cancel CAMPAIGN_ID — Cancel a pending campaign\n\n"
    "<b>Quick start</b>\n"
    "1. Add the bot as an administrator to your group or channel.\n"
    "2. Register your group with /register, or promote the bot in your channel.\n"
    "3. Reply to the message you want to copy with /broadcast.\n\n"
    "Messages are copied, not forwarded, where Telegram permits. Scheduled times use UTC."
)

ADMIN_HELP = (
    "<b>🛡 ADMINISTRATOR HELP</b>\n\n"
    "<b>Admin-only commands</b>\n"
    "/adminhelp — Show this admin guide\n"
    "/adminstats — View system totals\n"
    "/adminbroadcast users — Broadcast to registered users\n"
    "/adminbroadcast groups — Broadcast to active groups\n"
    "/adminbroadcast channels — Broadcast to active channels\n"
    "/adminbroadcast all — Broadcast to registered users and active destinations\n\n"
    "<b>How to launch an admin campaign</b>\n"
    "Reply to the source message with the relevant /adminbroadcast command.\n\n"
    "<b>Security</b>\n"
    "Admin access is controlled by numeric Telegram IDs in Railway's ADMIN_IDS variable. Keep that list private."
)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await save_user(update.effective_user)
    if update.effective_message:
        await update.effective_message.reply_text(USER_HELP, reply_markup=user_menu(), parse_mode="HTML", disable_web_page_preview=True)


async def admin_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await save_user(update.effective_user)
    user, message = update.effective_user, update.effective_message
    if not user or not message:
        return
    if not is_admin(user.id):
        await message.reply_text("⛔ This command is available to authorized administrators only.")
        return
    await message.reply_text(ADMIN_HELP, reply_markup=admin_menu(), parse_mode="HTML", disable_web_page_preview=True)


async def register_destination(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await save_user(update.effective_user)
    message, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if not message or not chat or not user:
        return
    if chat.type not in ("group", "supergroup"):
        await message.reply_text("ℹ️ Run /register inside the group you want to register.")
        return
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        if member.status not in ("administrator", "creator"):
            await message.reply_text("⛔ Only a group administrator can register this destination.")
            return
        bot_member = await context.bot.get_chat_member(chat.id, context.bot.id)
        if bot_member.status != "administrator":
            await message.reply_text("⚙️ Please promote me to group administrator with permission to post messages, then try again.")
            return
    except TelegramError as exc:
        logger.info("Permission check failed: %s", exc)
        await message.reply_text("I couldn't verify permissions. Check that I am a group administrator and try again.")
        return
    async with get_pool().acquire() as conn:
        existing_owner = await conn.fetchval("SELECT owner_id FROM destinations WHERE chat_id=$1", chat.id)
        if existing_owner is not None and int(existing_owner) != user.id and not is_admin(user.id):
            await message.reply_text("⛔ This destination is already registered to another account. Contact support if ownership needs review.")
            return
        await conn.execute(
            """INSERT INTO destinations(chat_id,owner_id,chat_title,chat_type,is_active)
               VALUES($1,$2,$3,$4,TRUE) ON CONFLICT(chat_id) DO UPDATE SET
               chat_title=EXCLUDED.chat_title, chat_type=EXCLUDED.chat_type, is_active=TRUE""",
            chat.id, user.id if existing_owner is None else int(existing_owner), chat.title or str(chat.id), chat.type,
        )
    await message.reply_text("✅ <b>Destination registered.</b>\nYou can review it from the dashboard or /destinations.", reply_markup=user_menu(), parse_mode="HTML")


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
        existing_owner = await conn.fetchval("SELECT owner_id FROM destinations WHERE chat_id=$1", chat.id)
        if existing_owner is not None:
            # Never silently transfer a destination's ownership on a promotion update.
            await conn.execute("UPDATE destinations SET chat_title=$2, chat_type=$3, is_active=TRUE WHERE chat_id=$1", chat.id, chat.title or str(chat.id), chat.type)
        else:
            await conn.execute(
                """INSERT INTO destinations(chat_id,owner_id,chat_title,chat_type,is_active)
                   VALUES($1,$2,$3,$4,TRUE) ON CONFLICT(chat_id) DO NOTHING""",
                chat.id, actor.id, chat.title or str(chat.id), chat.type,
            )
    logger.info("Destination membership updated for chat %s", chat.id)


async def destinations(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await save_user(update.effective_user)
    user, message = update.effective_user, update.effective_message
    if not user or not message:
        return
    async with get_pool().acquire() as conn:
        rows = await conn.fetch("SELECT chat_id,chat_title,chat_type,is_active FROM destinations WHERE owner_id=$1 ORDER BY added_at DESC", user.id)
    if not rows:
        await message.reply_text("📡 <b>No destinations registered yet.</b>\nAdd the bot as a group administrator and run /register, or promote it in a channel.", reply_markup=user_menu(), parse_mode="HTML")
        return
    lines = ["<b>📡 YOUR DESTINATIONS</b>"]
    for row in rows[:50]:
        state = "🟢 Active" if row["is_active"] else "⚪ Inactive"
        lines.append(f"\n• <b>{escape_html(row['chat_title'])}</b>\n  {row['chat_type'].title()} · {state}\n  <code>{row['chat_id']}</code>")
    if len(rows) > 50:
        lines.append(f"\n…and {len(rows) - 50} more destinations.")
    await message.reply_text("\n".join(lines), reply_markup=user_menu(), parse_mode="HTML")


def escape_html(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


async def owned_targets(user_id: int) -> list[int]:
    async with get_pool().acquire() as conn:
        rows = await conn.fetch("SELECT chat_id FROM destinations WHERE owner_id=$1 AND is_active=TRUE ORDER BY added_at", user_id)
    return [int(row["chat_id"]) for row in rows]


async def create_campaign(owner_id: int, scope: str, targets: list[int], source_chat_id: int,
                          source_message_id: int, scheduled_at: datetime, repeat_minutes: int | None) -> int:
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO campaigns(owner_id,target_scope,target_ids,source_chat_id,source_message_id,
               status,scheduled_at,repeat_interval_minutes,total_targets)
               VALUES($1,$2,$3::BIGINT[],$4,$5,'scheduled',$6,$7,$8) RETURNING id""",
            owner_id, scope, targets, source_chat_id, source_message_id, scheduled_at, repeat_minutes, len(targets),
        )
    return int(row["id"])


async def broadcast_now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await save_user(update.effective_user)
    message, user = update.effective_message, update.effective_user
    if not message or not user:
        return
    source = message.reply_to_message
    if not source:
        await message.reply_text("📣 To start a broadcast, reply directly to the message you want to copy with /broadcast.")
        return
    targets = await owned_targets(user.id)
    if not targets:
        await message.reply_text("No active destinations found. Register a group or add me as a channel administrator first.", reply_markup=user_menu())
        return
    if len(targets) > MAX_CAMPAIGN_TARGETS:
        await message.reply_text(f"Target count exceeds the configured limit ({MAX_CAMPAIGN_TARGETS}).")
        return
    campaign_id = await create_campaign(user.id, "owned", targets, source.chat_id, source.message_id, datetime.now(timezone.utc), None)
    await message.reply_text(
        f"✅ <b>Campaign queued</b>\n\nCampaign: <code>#{campaign_id}</code>\nDestinations: <b>{len(targets)}</b>\nStatus: Queued",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📊 View Campaigns", callback_data="u:campaigns"), InlineKeyboardButton("🏠 Main Menu", callback_data="u:home")]]),
        parse_mode="HTML",
    )


async def schedule_campaign(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await save_user(update.effective_user)
    message, user = update.effective_message, update.effective_user
    if not message or not user:
        return
    source = message.reply_to_message
    if not source:
        await message.reply_text("Reply to your source message with /schedule YYYY-MM-DDTHH:MM [INTERVAL_MINUTES]. Times use UTC.")
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
        await message.reply_text("Invalid schedule. Example: /schedule 2026-10-10T12:00 1440. Repeat interval must be at least 1 minute.")
        return
    if scheduled <= datetime.now(timezone.utc):
        await message.reply_text("Please choose a future UTC date and time.")
        return
    targets = await owned_targets(user.id)
    if not targets:
        await message.reply_text("No active destinations found. Register a group or channel first.", reply_markup=user_menu())
        return
    if len(targets) > MAX_CAMPAIGN_TARGETS:
        await message.reply_text(f"Target count exceeds the configured limit ({MAX_CAMPAIGN_TARGETS}).")
        return
    campaign_id = await create_campaign(user.id, "owned", targets, source.chat_id, source.message_id, scheduled, repeat)
    repeat_text = f"\nRepeat: Every {repeat} minute(s)" if repeat else "\nRepeat: Off"
    await message.reply_text(
        f"✅ <b>Schedule created</b>\n\nCampaign: <code>#{campaign_id}</code>\nTime: <b>{scheduled:%Y-%m-%d %H:%M UTC}</b>\nDestinations: <b>{len(targets)}</b>{repeat_text}",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📊 View Campaigns", callback_data="u:campaigns"), InlineKeyboardButton("🏠 Main Menu", callback_data="u:home")]]),
        parse_mode="HTML",
    )


async def list_campaigns(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await save_user(update.effective_user)
    user, message = update.effective_user, update.effective_message
    if not user or not message:
        return
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            """SELECT id,target_scope,status,scheduled_at,total_targets,sent_count,failed_count
               FROM campaigns WHERE owner_id=$1 ORDER BY id DESC LIMIT 10""", user.id,
        )
    if not rows:
        await message.reply_text("📊 <b>No campaigns yet.</b>\nStart a broadcast to see delivery reports here.", reply_markup=user_menu(), parse_mode="HTML")
        return
    lines = ["<b>📊 RECENT CAMPAIGNS</b>"]
    for row in rows:
        lines.append(
            f"\n<b>#{row['id']} · {escape_html(row['status'].title())}</b>\n"
            f"Sent: {row['sent_count']}/{row['total_targets']} · Failed: {row['failed_count']}\n"
            f"Scheduled: {row['scheduled_at']:%Y-%m-%d %H:%M UTC} · Scope: {escape_html(row['target_scope'])}"
        )
    await message.reply_text("\n".join(lines), reply_markup=user_menu(), parse_mode="HTML")


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
        result = await conn.execute(
            "UPDATE campaigns SET status='cancelled',updated_at=NOW() WHERE id=$1 AND owner_id=$2 AND status='scheduled'",
            campaign_id, user.id,
        )
    if result.endswith(" 1"):
        await message.reply_text(f"✅ Campaign #{campaign_id} cancelled.", reply_markup=user_menu())
    else:
        await message.reply_text("Campaign not found, not yours, or already running/completed.", reply_markup=user_menu())


async def admin_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await save_user(update.effective_user)
    message, user = update.effective_message, update.effective_user
    if not user or not message:
        return
    if not is_admin(user.id):
        await message.reply_text("⛔ Authorized administrators only.")
        return
    source = message.reply_to_message
    if not source:
        await message.reply_text("Reply to the source message with /adminbroadcast users|groups|channels|all.", reply_markup=admin_menu())
        return
    if len(context.args) != 1 or context.args[0].lower() not in {"users", "groups", "channels", "all"}:
        await message.reply_text("Usage: reply to a source message with /adminbroadcast users|groups|channels|all.", reply_markup=admin_menu())
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
    targets = list(dict.fromkeys(int(row["chat_id"]) for row in rows))
    if not targets:
        await message.reply_text(f"No targets available for {scope}.", reply_markup=admin_menu())
        return
    if len(targets) > MAX_CAMPAIGN_TARGETS:
        await message.reply_text(f"{len(targets)} targets exceeds MAX_CAMPAIGN_TARGETS={MAX_CAMPAIGN_TARGETS}.", reply_markup=admin_menu())
        return
    # Stage the campaign for explicit confirmation to reduce accidental mass sends.
    context.user_data["pending_admin_campaign"] = {
        "scope": scope,
        "targets": targets,
        "source_chat_id": source.chat_id,
        "source_message_id": source.message_id,
    }
    await message.reply_text(
        f"⚠️ <b>Review admin campaign</b>\n\nAudience: <b>{scope.title()}</b>\nTargets: <b>{len(targets):,}</b>\n\nNo messages have been sent yet. Confirm only if this audience is correct.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Confirm & Queue", callback_data="a:confirm"), InlineKeyboardButton("✖️ Cancel", callback_data="a:cancel")],
            [InlineKeyboardButton("🛡 Admin Menu", callback_data="a:home")],
        ]),
        parse_mode="HTML",
    )


async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await save_user(update.effective_user)
    user, message = update.effective_user, update.effective_message
    if not user or not message:
        return
    if not is_admin(user.id):
        await message.reply_text("⛔ Authorized administrators only.")
        return
    async with get_pool().acquire() as conn:
        users = await conn.fetchval("SELECT COUNT(*) FROM users")
        active_destinations = await conn.fetchval("SELECT COUNT(*) FROM destinations WHERE is_active=TRUE")
        campaigns = await conn.fetchval("SELECT COUNT(*) FROM campaigns")
        pending = await conn.fetchval("SELECT COUNT(*) FROM campaigns WHERE status='scheduled'")
        running = await conn.fetchval("SELECT COUNT(*) FROM campaigns WHERE status='running'")
        completed = await conn.fetchval("SELECT COUNT(*) FROM campaigns WHERE status='completed'")
        failed = await conn.fetchval("SELECT COUNT(*) FROM campaigns WHERE status='failed'")
    text = (
        "<b>📈 SYSTEM OVERVIEW</b>\n\n"
        f"👤 Registered users: <b>{users:,}</b>\n"
        f"📡 Active destinations: <b>{active_destinations:,}</b>\n"
        f"📊 Total campaigns: <b>{campaigns:,}</b>\n\n"
        f"🕒 Scheduled: <b>{pending:,}</b>\n"
        f"⚙️ Running: <b>{running:,}</b>\n"
        f"✅ Completed: <b>{completed:,}</b>\n"
        f"⚠️ Failed: <b>{failed:,}</b>"
    )
    await message.reply_text(text, reply_markup=admin_menu(), parse_mode="HTML")


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not user:
        return
    await query.answer()
    data = query.data or ""
    await save_user(user)

    if data.startswith("a:") and not is_admin(user.id):
        await query.edit_message_text("⛔ This administrator menu is restricted to authorized administrators.", reply_markup=back_button())
        return

    if data in {"u:home", "a:home"}:
        admin_view = data == "a:home"
        text = "<b>🛡 ADMIN DASHBOARD</b>\n\nChoose an administrator tool." if admin_view else "<b>🚀 MAIN DASHBOARD</b>\n\nChoose an action to continue."
        await query.edit_message_text(text, reply_markup=admin_menu() if admin_view else user_menu(), parse_mode="HTML")
        return

    if data == "u:help":
        await query.edit_message_text(USER_HELP, reply_markup=user_menu(), parse_mode="HTML", disable_web_page_preview=True)
        return
    if data == "a:help":
        await query.edit_message_text(ADMIN_HELP, reply_markup=admin_menu(), parse_mode="HTML", disable_web_page_preview=True)
        return
    if data == "u:broadcast":
        await query.edit_message_text(
            "<b>📣 NEW BROADCAST</b>\n\nReply directly to the message you want to copy with /broadcast. The campaign will target your active registered destinations.",
            reply_markup=back_button(), parse_mode="HTML",
        )
        return
    if data == "u:schedule":
        await query.edit_message_text(
            "<b>🗓 SCHEDULE A CAMPAIGN</b>\n\nReply to your source message with:\n<code>/schedule YYYY-MM-DDTHH:MM</code>\n\nFor repeating delivery, add an interval in minutes:\n<code>/schedule 2026-10-10T12:00 1440</code>\n\nTimes are UTC.",
            reply_markup=back_button(), parse_mode="HTML",
        )
        return
    if data == "u:destinations":
        async with get_pool().acquire() as conn:
            rows = await conn.fetch("SELECT chat_id,chat_title,chat_type,is_active FROM destinations WHERE owner_id=$1 ORDER BY added_at DESC LIMIT 20", user.id)
        if not rows:
            text = "<b>📡 DESTINATIONS</b>\n\nNo destinations registered. Add the bot as a group admin and run /register, or promote it in a channel."
        else:
            lines = ["<b>📡 YOUR DESTINATIONS</b>"]
            for row in rows:
                state = "🟢 Active" if row["is_active"] else "⚪ Inactive"
                lines.append(f"\n• <b>{escape_html(row['chat_title'])}</b> · {row['chat_type']} · {state}\n<code>{row['chat_id']}</code>")
            text = "\n".join(lines)
        await query.edit_message_text(text, reply_markup=user_menu(), parse_mode="HTML")
        return
    if data == "u:campaigns":
        async with get_pool().acquire() as conn:
            rows = await conn.fetch("SELECT id,status,scheduled_at,total_targets,sent_count,failed_count FROM campaigns WHERE owner_id=$1 ORDER BY id DESC LIMIT 8", user.id)
        if not rows:
            text = "<b>📊 CAMPAIGNS</b>\n\nNo campaigns yet. Use New Broadcast to get started."
        else:
            lines = ["<b>📊 RECENT CAMPAIGNS</b>"]
            for row in rows:
                lines.append(f"\n<b>#{row['id']} · {escape_html(row['status'].title())}</b>\nSent {row['sent_count']}/{row['total_targets']} · Failed {row['failed_count']}\n{row['scheduled_at']:%Y-%m-%d %H:%M UTC}")
            text = "\n".join(lines)
        await query.edit_message_text(text, reply_markup=user_menu(), parse_mode="HTML")
        return
    if data == "a:cancel":
        context.user_data.pop("pending_admin_campaign", None)
        await query.edit_message_text("✖️ <b>Admin campaign cancelled.</b> No messages were queued.", reply_markup=admin_menu(), parse_mode="HTML")
        return
    if data == "a:confirm":
        pending = context.user_data.get("pending_admin_campaign")
        if not pending:
            await query.edit_message_text("This confirmation has expired. Start the admin broadcast again.", reply_markup=admin_menu())
            return
        if len(pending["targets"]) > MAX_CAMPAIGN_TARGETS:
            context.user_data.pop("pending_admin_campaign", None)
            await query.edit_message_text("Target count exceeds the configured limit. No campaign was queued.", reply_markup=admin_menu())
            return
        campaign_id = await create_campaign(
            user.id, pending["scope"], pending["targets"], pending["source_chat_id"],
            pending["source_message_id"], datetime.now(timezone.utc), None,
        )
        scope = pending["scope"]
        target_count = len(pending["targets"])
        context.user_data.pop("pending_admin_campaign", None)
        await query.edit_message_text(
            f"✅ <b>Admin campaign queued</b>\n\nCampaign: <code>#{campaign_id}</code>\nAudience: <b>{scope.title()}</b>\nTargets: <b>{target_count:,}</b>",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📈 System Stats", callback_data="a:stats"), InlineKeyboardButton("🛡 Admin Menu", callback_data="a:home")]]),
            parse_mode="HTML",
        )
        return
    if data == "a:stats":
        async with get_pool().acquire() as conn:
            users = await conn.fetchval("SELECT COUNT(*) FROM users")
            destinations_count = await conn.fetchval("SELECT COUNT(*) FROM destinations WHERE is_active=TRUE")
            campaign_count = await conn.fetchval("SELECT COUNT(*) FROM campaigns")
            scheduled = await conn.fetchval("SELECT COUNT(*) FROM campaigns WHERE status='scheduled'")
            running = await conn.fetchval("SELECT COUNT(*) FROM campaigns WHERE status='running'")
            completed = await conn.fetchval("SELECT COUNT(*) FROM campaigns WHERE status='completed'")
            failed = await conn.fetchval("SELECT COUNT(*) FROM campaigns WHERE status='failed'")
        text = ("<b>📈 SYSTEM OVERVIEW</b>\n\n"
                f"👤 Registered users: <b>{users:,}</b>\n📡 Active destinations: <b>{destinations_count:,}</b>\n"
                f"📊 Total campaigns: <b>{campaign_count:,}</b>\n\n🕒 Scheduled: <b>{scheduled:,}</b>\n"
                f"⚙️ Running: <b>{running:,}</b>\n✅ Completed: <b>{completed:,}</b>\n⚠️ Failed: <b>{failed:,}</b>")
        await query.edit_message_text(text, reply_markup=admin_menu(), parse_mode="HTML")
        return
    if data.startswith("a:scope:"):
        scope = data.rsplit(":", 1)[-1]
        await query.edit_message_text(
            f"<b>🛡 ADMIN BROADCAST · {scope.upper()}</b>\n\n"
            "To protect against accidental mass sends, start the campaign by replying to the exact source message with:\n\n"
            f"<code>/adminbroadcast {scope}</code>\n\n"
            "The bot will show a confirmation with the target count before queueing only in a future confirmation-enabled release. This version queues when the command is sent.",
            reply_markup=admin_menu(), parse_mode="HTML",
        )
        return
    await query.edit_message_text("That menu option is no longer available. Open the dashboard again.", reply_markup=user_menu())


def register_handlers(application: Application) -> None:
    for command, callback in [
        ("start", start), ("help", help_command), ("adminhelp", admin_help),
        ("register", register_destination), ("destinations", destinations),
        ("broadcast", broadcast_now), ("schedule", schedule_campaign),
        ("campaigns", list_campaigns), ("cancel", cancel_campaign),
        ("adminbroadcast", admin_broadcast), ("adminstats", admin_stats),
    ]:
        application.add_handler(CommandHandler(command, callback))
    application.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^[ua]:"))
    application.add_handler(ChatMemberHandler(chat_member_update, ChatMemberHandler.MY_CHAT_MEMBER))
