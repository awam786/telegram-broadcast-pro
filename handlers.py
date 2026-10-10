import asyncio
import csv
import io
import logging
import re
from datetime import datetime, timedelta, timezone
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, MessageEntity, Update
from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError
from telegram.ext import (Application, CallbackQueryHandler, ChatMemberHandler,
                          CommandHandler, ContextTypes, MessageHandler, filters)
from config import ADMIN_IDS, MAX_CAMPAIGN_TARGETS
from database import get_pool

logger = logging.getLogger(__name__)

# Reply to a message containing these custom emojis with /setpremiumemojis.
# The first custom emoji is assigned to rocket, the second to broadcast, etc.
EMOJI_KEYS = [
    ("rocket", "🚀"), ("broadcast", "📣"), ("calendar", "🗓"),
    ("destination", "📡"), ("stats", "📊"), ("help", "❔"),
    ("shield", "🛡"), ("user", "👤"), ("success", "✅"),
    ("warning", "⚠️"), ("failed", "❌"), ("clock", "🕒"),
    ("target", "🎯"), ("campaign", "📬"), ("repeat", "🔁"),
    ("sparkle", "✨"), ("settings", "⚙️"), ("chart", "📈"),
]
PAGE_SIZE = 6

# Cached custom emoji IDs are used in message text and inline-button icons.
PREMIUM_EMOJI_CACHE: dict[str, str] = {}


def premium_button(text: str, callback_data: str | None = None, url: str | None = None, **kwargs):
    """Use Telegram custom-emoji icons on buttons when an ID is configured."""
    original = str(text)
    label = original
    emoji_id = None
    for key, fallback in sorted(EMOJI_KEYS, key=lambda item: len(item[1]), reverse=True):
        if label.startswith(fallback) and PREMIUM_EMOJI_CACHE.get(key):
            emoji_id = PREMIUM_EMOJI_CACHE[key]
            label = label[len(fallback):].lstrip()
            break
    if emoji_id:
        return InlineKeyboardButton(
            text=label or "Open",
            callback_data=callback_data,
            url=url,
            icon_custom_emoji_id=emoji_id,
            **kwargs,
        )
    return InlineKeyboardButton(text=original, callback_data=callback_data, url=url, **kwargs)


def is_admin(user_id: int | None) -> bool:
    return user_id is not None and user_id in ADMIN_IDS


def escape_html(value: object) -> str:
    text = str(value if value is not None else "")
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


async def save_user(user) -> None:
    if not user or user.is_bot:
        return
    async with get_pool().acquire() as conn:
        await conn.execute(
            """INSERT INTO users(user_id, username, first_name, last_seen_at)
               VALUES($1,$2,$3,NOW()) ON CONFLICT(user_id) DO UPDATE SET
               username=EXCLUDED.username, first_name=EXCLUDED.first_name, last_seen_at=NOW()""",
            user.id, user.username, user.first_name or "",
        )


async def refresh_premium_emoji_cache(application_or_context) -> dict[str, str]:
    """Reload saved custom-emoji IDs; accepts either Application or callback context."""
    application = (
        application_or_context
        if isinstance(application_or_context, Application)
        else application_or_context.application
    )
    async with get_pool().acquire() as conn:
        rows = await conn.fetch("SELECT emoji_key, custom_emoji_id FROM premium_emojis")
    mapping = {str(row["emoji_key"]): str(row["custom_emoji_id"]) for row in rows}
    global PREMIUM_EMOJI_CACHE
    PREMIUM_EMOJI_CACHE = mapping
    application.bot_data["premium_emoji_map"] = mapping
    return mapping


def premiumize(text: str, context: ContextTypes.DEFAULT_TYPE) -> str:
    """Render configured custom emojis in bot-authored HTML text, with Unicode fallback."""
    mapping = context.application.bot_data.get("premium_emoji_map", {})
    replacements = [(fallback, mapping.get(key), key) for key, fallback in EMOJI_KEYS if mapping.get(key)]
    if not replacements:
        return text
    # Longest first avoids replacing a shorter emoji prefix in a compound emoji.
    replacements.sort(key=lambda item: len(item[0]), reverse=True)
    pattern = re.compile("|".join(re.escape(item[0]) for item in replacements))
    lookup = {fallback: (emoji_id, key) for fallback, emoji_id, key in replacements}
    return pattern.sub(lambda match: f'<tg-emoji emoji-id="{lookup[match.group(0)][0]}">{match.group(0)}</tg-emoji>', text)


async def reply_html(message, text: str, context: ContextTypes.DEFAULT_TYPE, reply_markup=None, **kwargs):
    return await message.reply_text(premiumize(text, context), parse_mode="HTML", reply_markup=reply_markup,
                                    disable_web_page_preview=True, **kwargs)


async def edit_html(query, text: str, context: ContextTypes.DEFAULT_TYPE, reply_markup=None):
    return await query.edit_message_text(premiumize(text, context), parse_mode="HTML", reply_markup=reply_markup,
                                        disable_web_page_preview=True)


def user_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [premium_button("📣 New Broadcast", callback_data="u:broadcast"), premium_button("🗓 Schedule", callback_data="u:schedule")],
        [premium_button("📡 Destinations", callback_data="u:destinations"), premium_button("📊 Campaigns", callback_data="u:campaigns")],
        [premium_button("❔ User Help", callback_data="u:help")],
    ])


def admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [premium_button("📈 System Stats & Users", callback_data="a:stats:0"), premium_button("📋 Admin Help", callback_data="a:help")],
        [premium_button("👤 Broadcast to Users", callback_data="a:scope:users")],
        [premium_button("👥 Broadcast to Groups", callback_data="a:scope:groups"), premium_button("📢 Broadcast to Channels", callback_data="a:scope:channels")],
        [premium_button("🌐 Broadcast to All", callback_data="a:scope:all")],
        [premium_button("✨ Configure Premium Emojis", callback_data="a:premium")],
        [premium_button("⬅️ User Menu", callback_data="u:home")],
    ])


def back_button(admin: bool = False) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[premium_button("⬅️ Back to Menu", callback_data="a:home" if admin else "u:home")]])


def draft_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [premium_button("🚀 Send Now", callback_data="u:sendnow"), premium_button("🗓 Schedule", callback_data="u:schedule")],
        [premium_button("✖️ Discard Draft", callback_data="u:discard")],
    ])


def schedule_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [premium_button("In 1 hour", callback_data="u:sched:60"), premium_button("In 6 hours", callback_data="u:sched:360")],
        [premium_button("In 12 hours", callback_data="u:sched:720"), premium_button("Tomorrow", callback_data="u:sched:1440")],
        [premium_button("🔁 Repeat hourly", callback_data="u:repeat:60"), premium_button("🔁 Repeat daily", callback_data="u:repeat:1440")],
        [premium_button("🔁 Repeat weekly", callback_data="u:repeat:10080")],
        [premium_button("🛠 Custom date & time", callback_data="u:custom")],
        [premium_button("⬅️ Back to Draft", callback_data="u:draft")],
        [premium_button("🏠 Main Menu", callback_data="u:home")],
    ])


USER_HELP = (
    "<b>📘 USER HELP</b>\n\n"
    "<b>Commands</b>\n/start — Open the dashboard\n/help — Show user commands\n"
    "/register — Register this group (group admins only)\n/destinations — View your destinations\n"
    "/broadcast — Send now (reply to a source message)\n"
    "/schedule YYYY-MM-DDTHH:MM [INTERVAL_MINUTES] — Schedule in UTC\n"
    "/campaigns — View recent campaigns\n/cancel CAMPAIGN_ID — Cancel a scheduled campaign\n\n"
    "<b>Quick start</b>\n1. Add me as an administrator to your group or channel.\n"
    "2. Send your broadcast content to this bot in a private chat.\n"
    "3. Tap <b>Send Now</b> or <b>Schedule</b>, then choose the timing.\n\n"
    "Text, photos, videos and other supported message types are copied where Telegram permits."
)

ADMIN_HELP = (
    "<b>🛡 ADMINISTRATOR HELP</b>\n\n<b>Admin-only commands</b>\n"
    "/adminhelp — Show this guide\n/adminstats — Open system stats and all-user report\n"
    "/adminbroadcast users|groups|channels|all — Prepare an admin broadcast (reply to source)\n"
    "/setpremiumemojis — Save custom emoji IDs from a replied-to message\n"
    "/resetpremiumemojis — Return bot interface to standard emoji\n\n"
    "Admin access is controlled by numeric Telegram IDs in Railway's ADMIN_IDS variable."
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user, message = update.effective_user, update.effective_message
    await save_user(user)
    if not message or not user:
        return
    greeting = (
        "<b>Welcome to Telegram Broadcast Pro</b> ✨\n\n"
        "Your professional broadcasting workspace for groups and channels.\n\n"
        "<b>How to get started</b>\n"
        "1. Add the bot as an administrator to your group or channel.\n"
        "2. Send your broadcast message here — text, photo, video or supported media.\n"
        "3. Use the inline buttons to send immediately or schedule delivery.\n\n"
        "Choose an option below to manage your broadcasts."
    )
    if is_admin(user.id):
        keyboard = InlineKeyboardMarkup([
            [premium_button("🧭 User Dashboard", callback_data="u:home")],
            [premium_button("🛡 Admin Dashboard", callback_data="a:home")],
            [premium_button("❔ User Help", callback_data="u:help")],
        ])
        greeting += "\n\n<b>Administrator access detected.</b>"
    else:
        keyboard = user_menu()
    await reply_html(message, greeting, context, reply_markup=keyboard)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await save_user(update.effective_user)
    if update.effective_message:
        await reply_html(update.effective_message, USER_HELP, context, reply_markup=user_menu())


async def admin_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await save_user(update.effective_user)
    user, message = update.effective_user, update.effective_message
    if not user or not message:
        return
    if not is_admin(user.id):
        await reply_html(message, "⛔ This command is available to authorized administrators only.", context)
        return
    await reply_html(message, ADMIN_HELP, context, reply_markup=admin_menu())


async def register_destination(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Register a destination and grant access to every current Telegram chat admin."""
    await save_user(update.effective_user)
    message, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if not message or not chat or not user:
        return
    if chat.type not in ("group", "supergroup", "channel"):
        await reply_html(message, "ℹ️ Run /register inside the group or channel you want to register.", context)
        return
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        if member.status not in ("administrator", "creator"):
            await reply_html(message, "⛔ Only a current group/channel administrator can register this destination.", context)
            return
        bot_member = await context.bot.get_chat_member(chat.id, context.bot.id)
        if bot_member.status != "administrator":
            await reply_html(message, "⚙️ Please promote me to administrator with permission to post messages, then try /register again.", context)
            return
        chat_admins = await context.bot.get_chat_administrators(chat.id)
    except TelegramError as exc:
        logger.info("Destination permission check failed: %s", exc)
        await reply_html(message, "I couldn't verify permissions. Check that I am an administrator and try /register again.", context)
        return

    async with get_pool().acquire() as conn:
        # Preserve original owner_id for compatibility, but do not block other verified admins.
        await conn.execute(
            """INSERT INTO destinations(chat_id,owner_id,chat_title,chat_type,is_active)
               VALUES($1,$2,$3,$4,TRUE) ON CONFLICT(chat_id) DO UPDATE SET
               chat_title=EXCLUDED.chat_title, chat_type=EXCLUDED.chat_type, is_active=TRUE""",
            chat.id, user.id, chat.title or str(chat.id), chat.type,
        )
        admin_ids = {user.id}
        for admin in chat_admins:
            admin_user = admin.user
            if admin_user.is_bot:
                continue
            admin_ids.add(admin_user.id)
            await conn.execute(
                """INSERT INTO users(user_id,username,first_name,last_seen_at)
                   VALUES($1,$2,$3,NOW()) ON CONFLICT(user_id) DO UPDATE SET
                   username=EXCLUDED.username, first_name=EXCLUDED.first_name""",
                admin_user.id, admin_user.username, admin_user.first_name or "",
            )
        for admin_id in admin_ids:
            await conn.execute(
                """INSERT INTO destination_access(chat_id,user_id) VALUES($1,$2)
                   ON CONFLICT(chat_id,user_id) DO NOTHING""",
                chat.id, admin_id,
            )
    await reply_html(
        message,
        f"<b>Destination registered.</b> Access is enabled for the current chat administrators ({len(admin_ids)} human admins). Any of them can open the bot privately and broadcast to this destination.",
        context,
        reply_markup=user_menu(),
    )


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
        chat_admins = await context.bot.get_chat_administrators(chat.id)
    except TelegramError as exc:
        logger.info("Could not sync destination administrators for %s: %s", chat.id, exc)
        return
    async with get_pool().acquire() as conn:
        await conn.execute(
            """INSERT INTO destinations(chat_id,owner_id,chat_title,chat_type,is_active)
               VALUES($1,$2,$3,$4,TRUE) ON CONFLICT(chat_id) DO UPDATE SET
               chat_title=EXCLUDED.chat_title, chat_type=EXCLUDED.chat_type, is_active=TRUE""",
            chat.id, actor.id, chat.title or str(chat.id), chat.type,
        )
        admin_ids = {actor.id}
        for admin in chat_admins:
            admin_user = admin.user
            if admin_user.is_bot:
                continue
            admin_ids.add(admin_user.id)
            await conn.execute(
                """INSERT INTO users(user_id,username,first_name,last_seen_at)
                   VALUES($1,$2,$3,NOW()) ON CONFLICT(user_id) DO UPDATE SET
                   username=EXCLUDED.username, first_name=EXCLUDED.first_name""",
                admin_user.id, admin_user.username, admin_user.first_name or "",
            )
        for admin_id in admin_ids:
            await conn.execute(
                """INSERT INTO destination_access(chat_id,user_id) VALUES($1,$2)
                   ON CONFLICT(chat_id,user_id) DO NOTHING""",
                chat.id, admin_id,
            )
    logger.info("Destination %s synced with %s current human admins", chat.id, len(admin_ids))


async def destinations(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await save_user(update.effective_user)
    user, message = update.effective_user, update.effective_message
    if not user or not message:
        return
    async with get_pool().acquire() as conn:
        rows = await conn.fetch("SELECT DISTINCT d.chat_id,d.chat_title,d.chat_type,d.is_active FROM destinations d LEFT JOIN destination_access da ON da.chat_id=d.chat_id WHERE (d.owner_id=$1 OR da.user_id=$1) ORDER BY d.added_at DESC", user.id)
    if not rows:
        await reply_html(message, "<b>No destinations registered yet.</b>\nAdd the bot as a group administrator and run /register, or promote it in a channel.", context, reply_markup=user_menu())
        return
    lines = ["<b>YOUR DESTINATIONS</b>"]
    for row in rows[:40]:
        state = "🟢 Active" if row["is_active"] else "⚪ Inactive"
        lines.append(f"\n• <b>{escape_html(row['chat_title'])}</b>\n  {escape_html(row['chat_type'].title())} · {state}\n  <code>{row['chat_id']}</code>")
    if len(rows) > 40:
        lines.append(f"\n…and {len(rows) - 40} more destinations.")
    await reply_html(message, "\n".join(lines), context, reply_markup=user_menu())


async def owned_targets(user_id: int, bot=None) -> list[int]:
    """Return destinations this user can still administer in Telegram right now."""
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT DISTINCT d.chat_id FROM destinations d LEFT JOIN destination_access da ON da.chat_id=d.chat_id WHERE (d.owner_id=$1 OR da.user_id=$1) AND d.is_active=TRUE ORDER BY d.chat_id",
            user_id,
        )
    candidates = [int(row["chat_id"]) for row in rows]
    if bot is None:
        return candidates
    allowed = []
    for chat_id in candidates:
        try:
            member = await bot.get_chat_member(chat_id, user_id)
            if member.status in ("administrator", "creator"):
                allowed.append(chat_id)
            else:
                async with get_pool().acquire() as conn:
                    await conn.execute("DELETE FROM destination_access WHERE chat_id=$1 AND user_id=$2", chat_id, user_id)
        except TelegramError:
            # If Telegram cannot verify access, fail closed rather than allowing stale access.
            async with get_pool().acquire() as conn:
                await conn.execute("DELETE FROM destination_access WHERE chat_id=$1 AND user_id=$2", chat_id, user_id)
    return allowed


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


async def queue_owned_campaign(user_id: int, draft: dict, when: datetime, repeat: int | None, bot) -> tuple[int | None, int, str | None]:
    targets = await owned_targets(user_id, bot)
    if not targets:
        return None, 0, "No active destinations. Add the bot as a group/channel administrator and register the destination first."
    if len(targets) > MAX_CAMPAIGN_TARGETS:
        return None, len(targets), f"Your {len(targets):,} destinations exceed the configured limit of {MAX_CAMPAIGN_TARGETS:,}."
    campaign_id = await create_campaign(user_id, "owned", targets, int(draft["source_chat_id"]), int(draft["source_message_id"]), when, repeat)
    return campaign_id, len(targets), None


async def broadcast_now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await save_user(update.effective_user)
    message, user = update.effective_message, update.effective_user
    if not message or not user:
        return
    source = message.reply_to_message
    if not source:
        await reply_html(message, "<b>New broadcast</b>\nSend your content to me in a private chat, or reply directly to a source message with /broadcast.", context, reply_markup=user_menu())
        return
    draft = {"source_chat_id": source.chat_id, "source_message_id": source.message_id}
    context.user_data["broadcast_draft"] = draft
    await reply_html(message, "<b>Ready to broadcast?</b>\nReview your draft using the buttons below.", context, reply_markup=draft_menu())


async def schedule_campaign(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await save_user(update.effective_user)
    message, user = update.effective_message, update.effective_user
    if not message or not user:
        return
    source = message.reply_to_message
    if not source:
        await reply_html(message, "Reply to your source message with /schedule YYYY-MM-DDTHH:MM [INTERVAL_MINUTES], or send content to me privately and use the Schedule button.", context, reply_markup=user_menu())
        return
    if not 1 <= len(context.args) <= 2:
        await reply_html(message, "Usage: /schedule YYYY-MM-DDTHH:MM [INTERVAL_MINUTES] (UTC). Example: /schedule 2026-10-12T12:00 1440", context)
        return
    try:
        scheduled = datetime.strptime(context.args[0], "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc)
        repeat = int(context.args[1]) if len(context.args) == 2 else None
        if repeat is not None and repeat < 1:
            raise ValueError
    except ValueError:
        await reply_html(message, "Invalid schedule. Use YYYY-MM-DDTHH:MM and an optional repeat interval in minutes (minimum 1). Times use UTC.", context)
        return
    if scheduled <= datetime.now(timezone.utc):
        await reply_html(message, "Please choose a future UTC date and time.", context)
        return
    draft = {"source_chat_id": source.chat_id, "source_message_id": source.message_id}
    campaign_id, count, error = await queue_owned_campaign(user.id, draft, scheduled, repeat, context.bot)
    if error:
        await reply_html(message, escape_html(error), context, reply_markup=user_menu())
        return
    repeat_text = f"\nRepeat: every {repeat:,} minute(s)" if repeat else "\nRepeat: Off"
    await reply_html(message, f"<b>Schedule created</b>\nCampaign: <code>#{campaign_id}</code>\nTime: <b>{scheduled:%Y-%m-%d %H:%M UTC}</b>\nDestinations: <b>{count}</b>{repeat_text}", context,
                     reply_markup=InlineKeyboardMarkup([[premium_button("📊 Campaigns", callback_data="u:campaigns"), premium_button("🏠 Menu", callback_data="u:home")]]))


async def custom_schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await save_user(update.effective_user)
    user, message = update.effective_user, update.effective_message
    if not user or not message:
        return
    if not context.args:
        context.user_data["awaiting_custom_schedule"] = True
        await reply_html(message, "Send a UTC schedule as <code>YYYY-MM-DD HH:MM</code>, optionally followed by a repeat interval in minutes. Example: <code>2026-10-12 15:30 1440</code>.", context)
        return
    await parse_custom_schedule(" ".join(context.args), user.id, message, context)


async def parse_custom_schedule(raw: str, user_id: int, message, context: ContextTypes.DEFAULT_TYPE) -> None:
    draft = context.user_data.get("broadcast_draft")
    if not draft:
        context.user_data.pop("awaiting_custom_schedule", None)
        await reply_html(message, "No draft is waiting to be scheduled. Send your broadcast content to me first.", context, reply_markup=user_menu())
        return
    pieces = raw.strip().split()
    if len(pieces) not in (2, 3):
        await reply_html(message, "Invalid format. Send <code>YYYY-MM-DD HH:MM</code> and optionally repeat minutes, e.g. <code>2026-10-12 15:30 1440</code>.", context)
        return
    try:
        scheduled = datetime.strptime(pieces[0] + " " + pieces[1], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        repeat = int(pieces[2]) if len(pieces) == 3 else None
        if repeat is not None and repeat < 1:
            raise ValueError
    except ValueError:
        await reply_html(message, "Invalid date/time or repeat interval. Use YYYY-MM-DD HH:MM [INTERVAL_MINUTES].", context)
        return
    if scheduled <= datetime.now(timezone.utc):
        await reply_html(message, "Please choose a future UTC date and time.", context)
        return
    campaign_id, count, error = await queue_owned_campaign(user_id, draft, scheduled, repeat, context.bot)
    context.user_data.pop("awaiting_custom_schedule", None)
    if error:
        await reply_html(message, escape_html(error), context, reply_markup=user_menu())
        return
    repeat_text = f"\nRepeats every {repeat:,} minute(s)" if repeat else "\nOne-time delivery"
    context.user_data.pop("broadcast_draft", None)
    await reply_html(message, f"<b>Scheduled successfully</b>\nCampaign: <code>#{campaign_id}</code>\nUTC time: <b>{scheduled:%Y-%m-%d %H:%M}</b>\nDestinations: <b>{count}</b>{repeat_text}", context, reply_markup=user_menu())


async def capture_private_content(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Treat non-command private messages as a draft for the inline broadcast workflow."""
    message, user = update.effective_message, update.effective_user
    if not message or not user or message.chat.type != "private":
        return
    await save_user(user)
    if context.user_data.get("awaiting_custom_schedule"):
        await parse_custom_schedule(message.text or message.caption or "", user.id, message, context)
        return
    context.user_data["broadcast_draft"] = {"source_chat_id": message.chat_id, "source_message_id": message.message_id}
    preview = "<b>Draft received</b>\n\nYour message/media is ready. Choose whether to broadcast it now or schedule it for later."
    await reply_html(message, preview, context, reply_markup=draft_menu())


async def list_campaigns(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await save_user(update.effective_user)
    user, message = update.effective_user, update.effective_message
    if not user or not message:
        return
    async with get_pool().acquire() as conn:
        rows = await conn.fetch("""SELECT id,target_scope,status,scheduled_at,total_targets,sent_count,failed_count
                                   FROM campaigns WHERE owner_id=$1 ORDER BY id DESC LIMIT 10""", user.id)
    if not rows:
        await reply_html(message, "<b>No campaigns yet.</b>\nSend your first broadcast content to this bot to get started.", context, reply_markup=user_menu())
        return
    lines = ["<b>RECENT CAMPAIGNS</b>"]
    for row in rows:
        lines.append(f"\n<b>#{row['id']} · {escape_html(row['status'].title())}</b>\nSent: {row['sent_count']}/{row['total_targets']} · Failed: {row['failed_count']}\nScheduled: {row['scheduled_at']:%Y-%m-%d %H:%M UTC} · {escape_html(row['target_scope'])}")
    await reply_html(message, "\n".join(lines), context, reply_markup=user_menu())


async def cancel_campaign(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await save_user(update.effective_user)
    user, message = update.effective_user, update.effective_message
    if not user or not message:
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        await reply_html(message, "Usage: /cancel CAMPAIGN_ID", context)
        return
    async with get_pool().acquire() as conn:
        result = await conn.execute("UPDATE campaigns SET status='cancelled',updated_at=NOW() WHERE id=$1 AND owner_id=$2 AND status='scheduled'", int(context.args[0]), user.id)
    await reply_html(message, f"Campaign #{context.args[0]} cancelled." if result.endswith(" 1") else "Campaign not found, not yours, or already running/completed.", context, reply_markup=user_menu())


async def admin_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Prepare an admin broadcast and let the admin choose all or selected targets."""
    await save_user(update.effective_user)
    message, user = update.effective_message, update.effective_user
    if not message or not user:
        return
    if not is_admin(user.id):
        await reply_html(message, "⛔ Authorized administrators only.", context)
        return
    source = message.reply_to_message
    if not source:
        await reply_html(message, "Reply to the exact message/media you want to send, then use /adminbroadcast users|groups|channels|all.", context, reply_markup=admin_menu())
        return
    if len(context.args) != 1 or context.args[0].lower() not in {"users", "groups", "channels", "all"}:
        await reply_html(message, "Usage: reply to a source message with /adminbroadcast users|groups|channels|all.", context, reply_markup=admin_menu())
        return
    scope = context.args[0].lower()
    count = await count_admin_targets(scope)
    if not count:
        await reply_html(message, f"No available targets for {scope}.", context, reply_markup=admin_menu())
        return
    context.user_data["pending_admin_selection"] = {
        "scope": scope,
        "source_chat_id": source.chat_id,
        "source_message_id": source.message_id,
    }
    keyboard = InlineKeyboardMarkup([
        [premium_button(f"📣 Send to all {scope}", callback_data=f"a:pickall:{scope}")],
        [premium_button(f"👥 Select specific {scope}", callback_data=f"a:pickselect:{scope}:0")],
        [premium_button("✖️ Cancel", callback_data="a:cancelpick")],
    ])
    await reply_html(message, f"<b>Choose broadcast audience</b>\n\nCategory: <b>{escape_html(scope.title())}</b>\nAvailable targets: <b>{count:,}</b>\n\nChoose all targets or select recipients manually. Nothing is sent until you confirm the final campaign.", context, reply_markup=keyboard)


async def count_admin_targets(scope: str) -> int:
    async with get_pool().acquire() as conn:
        if scope == "users":
            query = "SELECT COUNT(*) FROM users WHERE is_blocked=FALSE"
        elif scope == "groups":
            query = "SELECT COUNT(*) FROM destinations WHERE is_active=TRUE AND chat_type IN ('group','supergroup')"
        elif scope == "channels":
            query = "SELECT COUNT(*) FROM destinations WHERE is_active=TRUE AND chat_type='channel'"
        else:
            query = "SELECT (SELECT COUNT(*) FROM users WHERE is_blocked=FALSE) + (SELECT COUNT(*) FROM destinations WHERE is_active=TRUE)"
        return int(await conn.fetchval(query) or 0)


async def fetch_admin_target_rows(scope: str, page: int = 0, page_size: int = 8):
    """Fetch one page of labelled targets; SQL scopes are fixed and never user-provided."""
    offset = max(0, page) * page_size
    if scope == "users":
        sql = """SELECT user_id AS chat_id, first_name AS title, username AS detail, 'user' AS kind
                 FROM users WHERE is_blocked=FALSE ORDER BY first_name, user_id LIMIT $1 OFFSET $2"""
    elif scope == "groups":
        sql = """SELECT chat_id, chat_title AS title, chat_type AS detail, 'group' AS kind
                 FROM destinations WHERE is_active=TRUE AND chat_type IN ('group','supergroup')
                 ORDER BY chat_title, chat_id LIMIT $1 OFFSET $2"""
    elif scope == "channels":
        sql = """SELECT chat_id, chat_title AS title, chat_type AS detail, 'channel' AS kind
                 FROM destinations WHERE is_active=TRUE AND chat_type='channel'
                 ORDER BY chat_title, chat_id LIMIT $1 OFFSET $2"""
    else:
        sql = """SELECT * FROM (
                   SELECT user_id AS chat_id, first_name AS title, username AS detail, 'user' AS kind
                   FROM users WHERE is_blocked=FALSE
                   UNION ALL
                   SELECT chat_id, chat_title AS title, chat_type AS detail, chat_type AS kind
                   FROM destinations WHERE is_active=TRUE
                 ) AS targets ORDER BY kind, title, chat_id LIMIT $1 OFFSET $2"""
    async with get_pool().acquire() as conn:
        return await conn.fetch(sql, page_size, offset)


async def fetch_admin_target_ids(scope: str) -> list[int]:
    async with get_pool().acquire() as conn:
        if scope == "users":
            rows = await conn.fetch("SELECT user_id AS chat_id FROM users WHERE is_blocked=FALSE ORDER BY user_id")
        elif scope == "groups":
            rows = await conn.fetch("SELECT chat_id FROM destinations WHERE is_active=TRUE AND chat_type IN ('group','supergroup') ORDER BY chat_id")
        elif scope == "channels":
            rows = await conn.fetch("SELECT chat_id FROM destinations WHERE is_active=TRUE AND chat_type='channel' ORDER BY chat_id")
        else:
            rows = await conn.fetch("SELECT user_id AS chat_id FROM users WHERE is_blocked=FALSE UNION SELECT chat_id FROM destinations WHERE is_active=TRUE ORDER BY chat_id")
    return list(dict.fromkeys(int(row["chat_id"]) for row in rows))


async def render_admin_target_picker(query, context: ContextTypes.DEFAULT_TYPE, scope: str, page: int = 0) -> None:
    pending = context.user_data.get("pending_admin_selection")
    if not pending or pending.get("scope") != scope:
        await edit_html(query, "This target selection expired. Start the admin broadcast again.", context, admin_menu())
        return
    page_size = 8
    count = await count_admin_targets(scope)
    pages = max(1, (count + page_size - 1) // page_size)
    page = max(0, min(page, pages - 1))
    rows = await fetch_admin_target_rows(scope, page, page_size)
    selected = {int(item) for item in pending.get("selected_ids", [])}
    lines = [f"<b>SELECT {escape_html(scope.upper())}</b>", f"Page <b>{page + 1}/{pages}</b> · Selected <b>{len(selected)}</b>", "Tap a recipient to toggle selection:"]
    buttons = []
    for row in rows:
        target_id = int(row["chat_id"])
        title = str(row["title"] or "(no name)").replace("\n", " ")[:34]
        detail = str(row["detail"] or row["kind"])
        label = f"{'☑️' if target_id in selected else '▫️'} {title} · {detail}"[:60]
        buttons.append([premium_button(label, callback_data=f"a:picktoggle:{scope}:{target_id}:{page}")])
    nav = []
    if page > 0:
        nav.append(premium_button("⬅️ Previous", callback_data=f"a:pickpage:{scope}:{page-1}"))
    if page + 1 < pages:
        nav.append(premium_button("Next ➡️", callback_data=f"a:pickpage:{scope}:{page+1}"))
    if nav:
        buttons.append(nav)
    buttons.append([premium_button(f"✅ Use selected ({len(selected)})", callback_data=f"a:pickdone:{scope}"), premium_button("✖️ Cancel", callback_data="a:cancelpick")])
    buttons.append([premium_button("Select all", callback_data=f"a:pickall:{scope}")])
    lines.append("\n\n<b>Selected recipients only will receive this campaign.</b>")
    await edit_html(query, "\n".join(lines), context, InlineKeyboardMarkup(buttons))


async def prepare_admin_confirmation(query, context: ContextTypes.DEFAULT_TYPE, user_id: int, scope: str, targets: list[int]) -> None:
    pending = context.user_data.get("pending_admin_selection")
    if not pending:
        await edit_html(query, "This broadcast selection expired. Start again.", context, admin_menu())
        return
    targets = list(dict.fromkeys(int(target) for target in targets))
    if not targets:
        await edit_html(query, "No recipients selected. Choose at least one recipient or cancel.", context,
                        InlineKeyboardMarkup([[premium_button("⬅️ Choose recipients", callback_data=f"a:pickselect:{scope}:0")], [premium_button("✖️ Cancel", callback_data="a:cancelpick")]]))
        return
    if len(targets) > MAX_CAMPAIGN_TARGETS:
        await edit_html(query, f"{len(targets):,} recipients exceeds MAX_CAMPAIGN_TARGETS={MAX_CAMPAIGN_TARGETS:,}. Select fewer recipients.", context,
                        InlineKeyboardMarkup([[premium_button("⬅️ Choose recipients", callback_data=f"a:pickselect:{scope}:0")], [premium_button("✖️ Cancel", callback_data="a:cancelpick")]]))
        return
    context.user_data["pending_admin_campaign"] = {
        "scope": scope, "targets": targets,
        "source_chat_id": pending["source_chat_id"],
        "source_message_id": pending["source_message_id"],
    }
    context.user_data.pop("pending_admin_selection", None)
    await edit_html(query, f"<b>Review admin campaign</b>\n\nAudience: <b>{escape_html(scope.title())}</b>\nRecipients: <b>{len(targets):,}</b>\n\nNo messages have been sent yet. Confirm only if this audience is correct.", context,
        InlineKeyboardMarkup([[premium_button("✅ Confirm & Queue", callback_data="a:confirm"), premium_button("✖️ Cancel", callback_data="a:cancel")], [premium_button("🛡 Admin Menu", callback_data="a:home")]]))


async def user_report_rows(offset: int | None = None, limit: int | None = None):
    base = """WITH campaign_stats AS (
                 SELECT owner_id, COUNT(*) AS total_broadcasts FROM campaigns GROUP BY owner_id
             ), delivery_stats AS (
                 SELECT c.owner_id,
                        COUNT(d.id) FILTER (WHERE d.success=TRUE) AS successful,
                        COUNT(d.id) FILTER (WHERE d.success=FALSE) AS failed
                 FROM campaigns c LEFT JOIN campaign_deliveries d ON d.campaign_id=c.id GROUP BY c.owner_id
             )
             SELECT u.user_id,u.first_name,u.username,u.created_at,u.last_seen_at,
                    COALESCE(cs.total_broadcasts,0) AS total_broadcasts,
                    COALESCE(ds.successful,0) AS successful, COALESCE(ds.failed,0) AS failed
             FROM users u LEFT JOIN campaign_stats cs ON cs.owner_id=u.user_id
             LEFT JOIN delivery_stats ds ON ds.owner_id=u.user_id
             ORDER BY u.created_at DESC, u.user_id DESC"""
    async with get_pool().acquire() as conn:
        if offset is None or limit is None:
            return await conn.fetch(base)
        return await conn.fetch(base + " OFFSET $1 LIMIT $2", offset, limit)


async def render_admin_stats(query, context: ContextTypes.DEFAULT_TYPE, page: int = 0) -> None:
    async with get_pool().acquire() as conn:
        total_users = int(await conn.fetchval("SELECT COUNT(*) FROM users") or 0)
        destinations_count = int(await conn.fetchval("SELECT COUNT(*) FROM destinations WHERE is_active=TRUE") or 0)
        campaign_count = int(await conn.fetchval("SELECT COUNT(*) FROM campaigns") or 0)
        scheduled = int(await conn.fetchval("SELECT COUNT(*) FROM campaigns WHERE status='scheduled'") or 0)
        running = int(await conn.fetchval("SELECT COUNT(*) FROM campaigns WHERE status='running'") or 0)
        completed = int(await conn.fetchval("SELECT COUNT(*) FROM campaigns WHERE status='completed'") or 0)
        failed_campaigns = int(await conn.fetchval("SELECT COUNT(*) FROM campaigns WHERE status='failed'") or 0)
    total_pages = max(1, (total_users + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    rows = await user_report_rows(page * PAGE_SIZE, PAGE_SIZE)
    lines = [
        "<b>SYSTEM STATS · USER REPORT</b>",
        f"Users: <b>{total_users:,}</b> · Active destinations: <b>{destinations_count:,}</b>",
        f"Campaigns: <b>{campaign_count:,}</b> · Scheduled: <b>{scheduled:,}</b> · Running: <b>{running:,}</b>",
        f"Completed: <b>{completed:,}</b> · Failed campaigns: <b>{failed_campaigns:,}</b>",
        f"\n<b>ALL USERS · Page {page + 1}/{total_pages}</b>",
        "<i>Successful/failed are message-delivery attempts recorded across each user's campaigns.</i>",
    ]
    if not rows:
        lines.append("\nNo registered users yet.")
    for row in rows:
        name = escape_html(row["first_name"] or "(no name)")
        username = f"@{escape_html(row['username'])}" if row["username"] else "(no username)"
        joined = row["created_at"].strftime("%Y-%m-%d") if row["created_at"] else "—"
        last_active = row["last_seen_at"].strftime("%Y-%m-%d %H:%M UTC") if row["last_seen_at"] else "—"
        lines.append(
            f"\n<b>{name}</b> · {username}\n"
            f"ID: <code>{row['user_id']}</code>\n"
            f"Broadcasts: <b>{row['total_broadcasts']}</b> · Successful: <b>{row['successful']}</b> · Failed: <b>{row['failed']}</b>\n"
            f"Joined: {joined} · Last active: {last_active}"
        )
    nav = []
    if page > 0:
        nav.append(premium_button("⬅️ Previous", callback_data=f"a:stats:{page-1}"))
    nav.append(premium_button(f"{page+1}/{total_pages}", callback_data=f"a:stats:{page}"))
    if page + 1 < total_pages:
        nav.append(premium_button("Next ➡️", callback_data=f"a:stats:{page+1}"))
    keyboard = [nav, [premium_button("📥 Export all users (CSV)", callback_data="a:exportusers")], [premium_button("⬅️ Admin Dashboard", callback_data="a:home")]]
    await edit_html(query, "\n".join(lines), context, InlineKeyboardMarkup(keyboard))


async def export_users_csv(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = await user_report_rows()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["user_id", "name", "username", "total_broadcasts", "successful_deliveries", "failed_deliveries", "joined_date_utc", "last_active_utc"])
    for row in rows:
        writer.writerow([row["user_id"], row["first_name"], row["username"] or "", row["total_broadcasts"], row["successful"], row["failed"], row["created_at"].isoformat() if row["created_at"] else "", row["last_seen_at"].isoformat() if row["last_seen_at"] else ""])
    payload = io.BytesIO(output.getvalue().encode("utf-8-sig"))
    payload.name = "telegram_broadcast_pro_users.csv"
    await query.message.reply_document(document=InputFile(payload, filename=payload.name), caption="Complete user activity report exported by Telegram Broadcast Pro.")


async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await save_user(update.effective_user)
    user, message = update.effective_user, update.effective_message
    if not user or not message:
        return
    if not is_admin(user.id):
        await reply_html(message, "⛔ Authorized administrators only.", context)
        return
    # Open the report directly: overview plus the first page of registered-user details.
    text = await build_stats_text(0, context)
    keyboard = await stats_keyboard(0)
    await message.reply_text(premiumize(text, context), parse_mode="HTML", reply_markup=keyboard, disable_web_page_preview=True)


async def build_stats_text(page: int, context: ContextTypes.DEFAULT_TYPE) -> str:
    async with get_pool().acquire() as conn:
        total_users = int(await conn.fetchval("SELECT COUNT(*) FROM users") or 0)
        destinations_count = int(await conn.fetchval("SELECT COUNT(*) FROM destinations WHERE is_active=TRUE") or 0)
        campaign_count = int(await conn.fetchval("SELECT COUNT(*) FROM campaigns") or 0)
        scheduled = int(await conn.fetchval("SELECT COUNT(*) FROM campaigns WHERE status='scheduled'") or 0)
        running = int(await conn.fetchval("SELECT COUNT(*) FROM campaigns WHERE status='running'") or 0)
        completed = int(await conn.fetchval("SELECT COUNT(*) FROM campaigns WHERE status='completed'") or 0)
        failed_campaigns = int(await conn.fetchval("SELECT COUNT(*) FROM campaigns WHERE status='failed'") or 0)
    total_pages = max(1, (total_users + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    rows = await user_report_rows(page * PAGE_SIZE, PAGE_SIZE)
    lines = ["<b>SYSTEM STATS · USER REPORT</b>",
             f"Users: <b>{total_users:,}</b> · Active destinations: <b>{destinations_count:,}</b>",
             f"Campaigns: <b>{campaign_count:,}</b> · Scheduled: <b>{scheduled:,}</b> · Running: <b>{running:,}</b>",
             f"Completed: <b>{completed:,}</b> · Failed campaigns: <b>{failed_campaigns:,}</b>",
             f"\n<b>ALL USERS · Page {page+1}/{total_pages}</b>",
             "<i>Successful/failed count delivery attempts recorded across each user's campaigns.</i>"]
    if not rows:
        lines.append("\nNo registered users yet.")
    for row in rows:
        name = escape_html(row["first_name"] or "(no name)")
        username = f"@{escape_html(row['username'])}" if row["username"] else "(no username)"
        joined = row["created_at"].strftime("%Y-%m-%d") if row["created_at"] else "—"
        last_active = row["last_seen_at"].strftime("%Y-%m-%d %H:%M UTC") if row["last_seen_at"] else "—"
        lines.append(f"\n<b>{name}</b> · {username}\nID: <code>{row['user_id']}</code>\nBroadcasts: <b>{row['total_broadcasts']}</b> · Successful: <b>{row['successful']}</b> · Failed: <b>{row['failed']}</b>\nJoined: {joined} · Last active: {last_active}")
    return "\n".join(lines)


async def stats_keyboard(page: int) -> InlineKeyboardMarkup:
    async with get_pool().acquire() as conn:
        total_users = int(await conn.fetchval("SELECT COUNT(*) FROM users") or 0)
    total_pages = max(1, (total_users + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    nav = []
    if page > 0:
        nav.append(premium_button("⬅️ Previous", callback_data=f"a:stats:{page-1}"))
    nav.append(premium_button(f"{page+1}/{total_pages}", callback_data=f"a:stats:{page}"))
    if page + 1 < total_pages:
        nav.append(premium_button("Next ➡️", callback_data=f"a:stats:{page+1}"))
    return InlineKeyboardMarkup([nav, [premium_button("📥 Export all users (CSV)", callback_data="a:exportusers")], [premium_button("⬅️ Admin Dashboard", callback_data="a:home")]])


async def set_premium_emojis(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user, message = update.effective_user, update.effective_message
    if not user or not message:
        return
    if not is_admin(user.id):
        await reply_html(message, "⛔ Authorized administrators only.", context)
        return
    source = message.reply_to_message
    if not source:
        await reply_html(message, "Reply to a message containing your Telegram Premium custom emojis with /setpremiumemojis. The first custom emoji maps to Rocket, the second to Broadcast, and so on.", context, reply_markup=admin_menu())
        return
    entities = list(source.entities or []) + list(source.caption_entities or [])
    custom = sorted((entity for entity in entities if entity.type == MessageEntity.CUSTOM_EMOJI and entity.custom_emoji_id), key=lambda item: item.offset)
    if not custom:
        await reply_html(message, "No Telegram custom-emoji entities were found in the replied-to message. Send a message containing actual custom emojis, then reply to it with /setpremiumemojis.", context)
        return
    chosen = custom[:len(EMOJI_KEYS)]
    async with get_pool().acquire() as conn:
        await conn.execute("DELETE FROM premium_emojis")
        for (key, fallback), entity in zip(EMOJI_KEYS, chosen):
            await conn.execute("INSERT INTO premium_emojis(emoji_key,custom_emoji_id) VALUES($1,$2) ON CONFLICT(emoji_key) DO UPDATE SET custom_emoji_id=EXCLUDED.custom_emoji_id", key, entity.custom_emoji_id)
    mapping = await refresh_premium_emoji_cache(context)
    await reply_html(message, f"<b>Premium emoji style saved.</b>\nConfigured {len(mapping)} custom emoji(s). The bot's message text and inline-button icons will use these where supported. Button labels remain normal text, with the custom emoji shown as the button icon.", context, reply_markup=admin_menu())


async def reset_premium_emojis(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user, message = update.effective_user, update.effective_message
    if not user or not message:
        return
    if not is_admin(user.id):
        await reply_html(message, "⛔ Authorized administrators only.", context)
        return
    async with get_pool().acquire() as conn:
        await conn.execute("DELETE FROM premium_emojis")
    context.application.bot_data["premium_emoji_map"] = {}
    await reply_html(message, "Premium emoji overrides removed. The interface is back to standard Unicode emoji.", context, reply_markup=admin_menu())


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not user:
        return
    await query.answer()
    await save_user(user)
    data = query.data or ""
    admin_action = data.startswith("a:")
    if admin_action and not is_admin(user.id):
        await query.answer("Administrator access required.", show_alert=True)
        return

    if data in {"u:home", "a:home"}:
        admin_view = data == "a:home"
        text = "<b>ADMIN DASHBOARD</b>\nChoose an administrator tool." if admin_view else "<b>MAIN DASHBOARD</b>\nSend your broadcast content to me in this private chat, or choose a feature below."
        await edit_html(query, text, context, admin_menu() if admin_view else user_menu())
        return
    if data == "u:help":
        await edit_html(query, USER_HELP, context, user_menu())
        return
    if data == "a:help":
        await edit_html(query, ADMIN_HELP, context, admin_menu())
        return
    if data == "u:broadcast":
        await edit_html(query, "<b>NEW BROADCAST</b>\n\nSend your broadcast content to this bot in a private chat — text, photo, video or supported media. I'll show buttons to send it now or schedule it for later.\n\nYou can also reply to a source message with /broadcast.", context, back_button())
        return
    if data == "u:schedule":
        if not context.user_data.get("broadcast_draft"):
            await edit_html(query, "<b>ADVANCED SCHEDULER</b>\n\nFirst send the message, photo, video or other content you want to broadcast to this bot in private chat. Once the draft is received, tap Schedule to choose a delivery time, recurring interval, or custom UTC date/time.", context,
                            InlineKeyboardMarkup([[premium_button("📣 New Broadcast", callback_data="u:broadcast")], [premium_button("⬅️ Main Menu", callback_data="u:home")]]))
        else:
            await edit_html(query, "<b>ADVANCED SCHEDULER</b>\n\nChoose a quick time or recurring schedule. All times use UTC. For a custom time, enter <code>YYYY-MM-DD HH:MM</code> and optionally repeat interval in minutes.", context, schedule_menu())
        return
    if data == "u:draft":
        if not context.user_data.get("broadcast_draft"):
            await edit_html(query, "No draft is active. Send your content here first.", context, user_menu())
        else:
            await edit_html(query, "<b>DRAFT READY</b>\nChoose Send Now or Schedule.", context, draft_menu())
        return
    if data == "u:sendnow":
        draft = context.user_data.get("broadcast_draft")
        if not draft:
            await edit_html(query, "No draft found. Send your content to the bot first.", context, user_menu())
            return
        targets = await owned_targets(user.id, context.bot)
        if not targets:
            await edit_html(query, "No active destinations found. Register a group or channel first.", context, user_menu())
            return
        if len(targets) > MAX_CAMPAIGN_TARGETS:
            await edit_html(query, f"Your {len(targets):,} destinations exceed the configured limit ({MAX_CAMPAIGN_TARGETS:,}).", context, user_menu())
            return
        context.user_data["confirm_user_campaign"] = draft
        await edit_html(query, f"<b>CONFIRM BROADCAST</b>\n\nTargets: <b>{len(targets):,}</b> active destinations\n\nConfirm to queue this message, or cancel to return to the draft.", context,
            InlineKeyboardMarkup([[premium_button("✅ Confirm & Queue", callback_data="u:confirmnow"), premium_button("✖️ Cancel", callback_data="u:draft")]]))
        return
    if data == "u:confirmnow":
        draft = context.user_data.get("confirm_user_campaign") or context.user_data.get("broadcast_draft")
        if not draft:
            await edit_html(query, "This draft has expired. Send your content again.", context, user_menu())
            return
        campaign_id, count, error = await queue_owned_campaign(user.id, draft, datetime.now(timezone.utc), None, context.bot)
        context.user_data.pop("confirm_user_campaign", None)
        context.user_data.pop("broadcast_draft", None)
        if error:
            await edit_html(query, escape_html(error), context, user_menu())
            return
        await edit_html(query, f"<b>Broadcast queued successfully</b>\nCampaign: <code>#{campaign_id}</code>\nTargets: <b>{count:,}</b>\nStatus: Queued", context,
                        InlineKeyboardMarkup([[premium_button("📊 Campaign Reports", callback_data="u:campaigns"), premium_button("🏠 Main Menu", callback_data="u:home")]]))
        return
    if data == "u:discard":
        context.user_data.pop("broadcast_draft", None)
        context.user_data.pop("confirm_user_campaign", None)
        context.user_data.pop("awaiting_custom_schedule", None)
        await edit_html(query, "Draft discarded. No campaign was queued.", context, user_menu())
        return
    if data.startswith("u:sched:") or data.startswith("u:repeat:"):
        draft = context.user_data.get("broadcast_draft")
        if not draft:
            await edit_html(query, "No draft found. Send your content to the bot first.", context, user_menu())
            return
        repeating = data.startswith("u:repeat:")
        minutes = int(data.rsplit(":", 1)[-1])
        when = datetime.now(timezone.utc) + timedelta(minutes=minutes)
        campaign_id, count, error = await queue_owned_campaign(user.id, draft, when, minutes if repeating else None, context.bot)
        if error:
            await edit_html(query, escape_html(error), context, user_menu())
            return
        context.user_data.pop("broadcast_draft", None)
        label = f"every {minutes:,} minute(s)" if repeating else f"once at {when:%Y-%m-%d %H:%M UTC}"
        await edit_html(query, f"<b>Schedule created</b>\nCampaign: <code>#{campaign_id}</code>\nDelivery: <b>{label}</b>\nTargets: <b>{count:,}</b>", context,
                        InlineKeyboardMarkup([[premium_button("📊 Campaign Reports", callback_data="u:campaigns"), premium_button("🏠 Main Menu", callback_data="u:home")]]))
        return
    if data == "u:custom":
        if not context.user_data.get("broadcast_draft"):
            await edit_html(query, "No draft found. Send your content to the bot first.", context, user_menu())
            return
        context.user_data["awaiting_custom_schedule"] = True
        await edit_html(query, "<b>CUSTOM SCHEDULE</b>\n\nSend the UTC date/time in your next message:\n<code>YYYY-MM-DD HH:MM</code>\n\nOptional repeating interval in minutes:\n<code>2026-10-12 15:30 1440</code>\n\nUse 60 for hourly, 1440 for daily, or 10080 for weekly repetition. No message is queued until a valid time is provided.", context,
                        InlineKeyboardMarkup([[premium_button("✖️ Cancel schedule", callback_data="u:draft")]]))
        return
    if data == "u:destinations":
        async with get_pool().acquire() as conn:
            rows = await conn.fetch("SELECT DISTINCT d.chat_id,d.chat_title,d.chat_type,d.is_active FROM destinations d LEFT JOIN destination_access da ON da.chat_id=d.chat_id WHERE (d.owner_id=$1 OR da.user_id=$1) ORDER BY d.added_at DESC LIMIT 20", user.id)
        if not rows:
            text = "<b>DESTINATIONS</b>\nNo destinations registered. Add me as a group admin and run /register, or promote me in a channel."
        else:
            lines = ["<b>YOUR DESTINATIONS</b>"]
            for row in rows:
                state = "🟢 Active" if row["is_active"] else "⚪ Inactive"
                lines.append(f"\n• <b>{escape_html(row['chat_title'])}</b> · {escape_html(row['chat_type'])} · {state}\n<code>{row['chat_id']}</code>")
            text = "\n".join(lines)
        await edit_html(query, text, context, user_menu())
        return
    if data == "u:campaigns":
        async with get_pool().acquire() as conn:
            rows = await conn.fetch("SELECT id,status,scheduled_at,total_targets,sent_count,failed_count FROM campaigns WHERE owner_id=$1 ORDER BY id DESC LIMIT 8", user.id)
        if not rows:
            text = "<b>CAMPAIGNS</b>\nNo campaigns yet. Send content to the bot to start."
        else:
            lines = ["<b>RECENT CAMPAIGNS</b>"]
            for row in rows:
                lines.append(f"\n<b>#{row['id']} · {escape_html(row['status'].title())}</b>\nSent {row['sent_count']}/{row['total_targets']} · Failed {row['failed_count']}\n{row['scheduled_at']:%Y-%m-%d %H:%M UTC}")
            text = "\n".join(lines)
        await edit_html(query, text, context, user_menu())
        return
    if data == "a:cancelpick":
        context.user_data.pop("pending_admin_selection", None)
        context.user_data.pop("pending_admin_campaign", None)
        await edit_html(query, "Admin broadcast cancelled. No messages were sent.", context, admin_menu())
        return
    if data.startswith("a:pickall:"):
        scope = data.rsplit(":", 1)[-1]
        if scope not in {"users", "groups", "channels", "all"}:
            await edit_html(query, "Invalid target category.", context, admin_menu())
            return
        targets = await fetch_admin_target_ids(scope)
        await prepare_admin_confirmation(query, context, user.id, scope, targets)
        return
    if data.startswith("a:pickselect:"):
        parts = data.split(":")
        if len(parts) != 4:
            await edit_html(query, "Invalid selection page.", context, admin_menu())
            return
        scope = parts[2]
        try:
            page = int(parts[3])
        except ValueError:
            page = 0
        pending = context.user_data.get("pending_admin_selection")
        if not pending or pending.get("scope") != scope:
            await edit_html(query, "This target selection expired. Start the broadcast again.", context, admin_menu())
            return
        pending.setdefault("selected_ids", [])
        await render_admin_target_picker(query, context, scope, page)
        return
    if data.startswith("a:pickpage:"):
        parts = data.split(":")
        if len(parts) != 4:
            await edit_html(query, "Invalid selection page.", context, admin_menu())
            return
        try:
            page = int(parts[3])
        except ValueError:
            page = 0
        await render_admin_target_picker(query, context, parts[2], page)
        return
    if data.startswith("a:picktoggle:"):
        parts = data.split(":")
        if len(parts) != 5:
            await edit_html(query, "Invalid recipient selection.", context, admin_menu())
            return
        scope = parts[2]
        try:
            target_id = int(parts[3])
            page = int(parts[4])
        except ValueError:
            await edit_html(query, "Invalid recipient ID or page.", context, admin_menu())
            return
        pending = context.user_data.get("pending_admin_selection")
        if not pending or pending.get("scope") != scope:
            await edit_html(query, "This target selection expired. Start the broadcast again.", context, admin_menu())
            return
        selected = {int(item) for item in pending.get("selected_ids", [])}
        if target_id in selected:
            selected.remove(target_id)
        else:
            selected.add(target_id)
        pending["selected_ids"] = sorted(selected)
        await render_admin_target_picker(query, context, scope, page)
        return
    if data.startswith("a:pickdone:"):
        scope = data.rsplit(":", 1)[-1]
        pending = context.user_data.get("pending_admin_selection")
        if not pending or pending.get("scope") != scope:
            await edit_html(query, "This target selection expired. Start the broadcast again.", context, admin_menu())
            return
        selected = {int(item) for item in pending.get("selected_ids", [])}
        available = set(await fetch_admin_target_ids(scope))
        targets = sorted(selected.intersection(available))
        await prepare_admin_confirmation(query, context, user.id, scope, targets)
        return
    if data == "a:cancel":
        context.user_data.pop("pending_admin_campaign", None)
        context.user_data.pop("pending_admin_selection", None)
        await edit_html(query, "<b>Admin campaign cancelled.</b> No messages were queued.", context, admin_menu())
        return
    if data == "a:confirm":
        pending = context.user_data.get("pending_admin_campaign")
        if not pending:
            await edit_html(query, "This confirmation has expired. Start the admin broadcast again.", context, admin_menu())
            return
        if len(pending["targets"]) > MAX_CAMPAIGN_TARGETS:
            context.user_data.pop("pending_admin_campaign", None)
            await edit_html(query, "Target count exceeds the configured limit. No campaign was queued.", context, admin_menu())
            return
        campaign_id = await create_campaign(user.id, pending["scope"], pending["targets"], pending["source_chat_id"], pending["source_message_id"], datetime.now(timezone.utc), None)
        scope, count = pending["scope"], len(pending["targets"])
        context.user_data.pop("pending_admin_campaign", None)
        await edit_html(query, f"<b>Admin campaign queued</b>\nCampaign: <code>#{campaign_id}</code>\nAudience: <b>{escape_html(scope.title())}</b>\nRecipients: <b>{count:,}</b>", context,
                        InlineKeyboardMarkup([[premium_button("📈 System Stats", callback_data="a:stats:0"), premium_button("🛡 Admin Menu", callback_data="a:home")]]))
        return
    if data == "a:stats" or data.startswith("a:stats:"):
        try:
            page = int(data.rsplit(":", 1)[-1]) if ":" in data else 0
        except ValueError:
            page = 0
        await render_admin_stats(query, context, page)
        return
    if data == "a:exportusers":
        await export_users_csv(query, context)
        return
    if data == "a:premium":
        await edit_html(query, "<b>PREMIUM EMOJI SETUP</b>\n\n1. Send a message containing your custom/premium emojis in the order you want them used.\n2. Reply to that message with <code>/setpremiumemojis</code>.\n3. The bot saves custom emoji IDs in PostgreSQL and applies them to its own message text and inline-button icons where Telegram supports them. The bot owner must have active Telegram Premium for this feature unless the bot has the qualifying Fragment username setup.\n\nOrder: Rocket, Broadcast, Calendar, Destination, Stats, Help, Shield, User, Success, Warning, Failed, Clock, Target, Campaign, Repeat, Sparkle, Settings, Chart.\n\nUse /resetpremiumemojis to remove them.", context,
                        InlineKeyboardMarkup([[premium_button("🛡 Admin Menu", callback_data="a:home")]]))
        return
    if data.startswith("a:scope:"):
        scope = data.rsplit(":", 1)[-1]
        await edit_html(query, f"<b>ADMIN BROADCAST · {scope.upper()}</b>\n\nReply to the exact source message with:\n<code>/adminbroadcast {scope}</code>\n\nThe bot will let you choose all recipients or select specific users/groups/channels, then show a confirmation before queueing.", context, admin_menu())
        return
    await edit_html(query, "That menu option is no longer available. Open the dashboard again.", context, user_menu())


def register_handlers(application: Application) -> None:
    commands = [
        ("start", start), ("help", help_command), ("adminhelp", admin_help),
        ("register", register_destination), ("destinations", destinations),
        ("broadcast", broadcast_now), ("schedule", schedule_campaign),
        ("settime", custom_schedule_command), ("campaigns", list_campaigns),
        ("cancel", cancel_campaign), ("adminbroadcast", admin_broadcast),
        ("adminstats", admin_stats), ("setpremiumemojis", set_premium_emojis),
        ("resetpremiumemojis", reset_premium_emojis),
    ]
    for command, callback in commands:
        application.add_handler(CommandHandler(command, callback))
    application.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^[ua]:"))
    application.add_handler(ChatMemberHandler(chat_member_update, ChatMemberHandler.MY_CHAT_MEMBER))
    application.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, capture_private_content))


async def post_handlers_init(application: Application) -> None:
    """Compatibility hook for optional startup integration; main.py loads the emoji cache."""
    return
