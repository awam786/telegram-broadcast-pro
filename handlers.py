"""All user and admin handlers for Telegram Broadcast Pro."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from telegram import (
    Update, Chat, InlineKeyboardButton, InlineKeyboardMarkup,
    MessageOriginChannel, MessageOriginChat,
)
from telegram.constants import ParseMode, ChatType
from telegram.error import TelegramError
from telegram.ext import (
    ContextTypes, CommandHandler, CallbackQueryHandler, MessageHandler,
    ChatMemberHandler, filters,
)

from config import Config
from database import Database
from broadcaster import BroadcastEngine
from emoji_manager import EmojiManager
from utils import (
    esc, truncate, fmt_dt, kb, btn, paginate, parse_int,
    parse_datetime_local, parse_interval, message_to_json, summary_from_message,
    PAGE_SIZE,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Guard / auth helpers
# ---------------------------------------------------------------------------

async def _guard(update: Update, db: Database) -> Optional[Dict[str, Any]]:
    user = update.effective_user
    if not user:
        return None
    status = await db.get_user_status(user.id)
    if status == "blocked":
        if update.callback_query:
            try:
                await update.callback_query.answer("Your access is blocked.", show_alert=True)
            except TelegramError:
                pass
        elif update.effective_message:
            await update.effective_message.reply_text("Your access is blocked by an administrator.")
        return None
    await db.upsert_user(user.id, user.username, user.full_name)
    return await db.get_user(user.id)


def _is_admin(cfg: Config, user_id: int) -> bool:
    return user_id in cfg.admin_ids


async def _require_admin(update: Update, cfg: Config) -> bool:
    uid = update.effective_user.id
    if not _is_admin(cfg, uid):
        if update.callback_query:
            try:
                await update.callback_query.answer("Admin only.", show_alert=True)
            except TelegramError:
                pass
        elif update.effective_message:
            await update.effective_message.reply_text("Admin only.")
        return False
    return True


# ---------------------------------------------------------------------------
# Main menu
# ---------------------------------------------------------------------------

def main_menu_kb(is_admin: bool) -> InlineKeyboardMarkup:
    rows = [
        [btn("📣 New Broadcast", "menu:new")],
        [btn("📂 My Destinations", "menu:dest"), btn("📝 Drafts", "menu:drafts")],
        [btn("🗓 Scheduled", "menu:sched"), btn("🔁 Repeating", "menu:rep")],
        [btn("📊 My Statistics", "menu:stats"), btn("📜 History", "menu:history")],
        [btn("⚙️ Settings", "menu:settings"), btn("❓ Help", "menu:help")],
    ]
    if is_admin:
        rows.append([btn("🛡 Admin Panel", "adm:home")])
    return kb(rows)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = ctx.application.bot_data["db"]
    cfg: Config = ctx.application.bot_data["cfg"]
    user = await _guard(update, db)
    if not user:
        return
    name = esc(user["display_name"])
    txt = (
        f"👋 Welcome, <b>{name}</b>!\n\n"
        "This is <b>Telegram Broadcast Pro</b> — a multi-user broadcasting platform.\n\n"
        "• Register your groups/channels\n"
        "• Compose rich broadcasts\n"
        "• Schedule &amp; repeat\n"
        "• Track per-destination delivery\n\n"
        "Use the buttons below to begin."
    )
    await update.effective_message.reply_text(
        txt, reply_markup=main_menu_kb(_is_admin(cfg, user["telegram_id"])),
        parse_mode=ParseMode.HTML,
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    txt = (
        "<b>User Commands</b>\n"
        "/start — Main menu\n"
        "/help — This help\n"
        "/register — Run inside a group/channel to register it\n"
        "/sync — Recheck all your registered destinations\n\n"
        "<b>Registering a group or channel</b>\n"
        "1. Open the group/channel in Telegram.\n"
        "2. Administrators → Add Administrator.\n"
        "3. Search for this bot's username and add it.\n"
        "4. Grant <b>Post Messages</b> permission (channels: also <b>Edit Messages</b>).\n"
        "5. Type /register inside the chat — OR — forward a message from it to me.\n\n"
        "<b>Broadcasting</b>\n"
        "📣 New Broadcast → pick destinations → send the message you want broadcast → "
        "confirm / schedule / repeat.\n\n"
        "<b>Premium emoji</b>\n"
        "Custom emoji are preserved via Telegram's native copy when you send or forward "
        "content that contains them."
    )
    await update.effective_message.reply_text(txt, parse_mode=ParseMode.HTML)


async def cmd_adminhelp(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Config = ctx.application.bot_data["cfg"]
    if not await _require_admin(update, cfg):
        return
    txt = (
        "<b>Admin Commands</b>\n"
        "/adminhelp — This help\n"
        "/admin — Admin panel\n\n"
        "Only IDs listed in ADMIN_IDS can use the admin panel. "
        "All privileged actions are logged."
    )
    await update.effective_message.reply_text(txt, parse_mode=ParseMode.HTML)


async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Config = ctx.application.bot_data["cfg"]
    if not await _require_admin(update, cfg):
        return
    await _admin_home(update, ctx, edit=False)


async def on_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    db: Database = ctx.application.bot_data["db"]
    cfg: Config = ctx.application.bot_data["cfg"]
    user = await _guard(update, db)
    if not user:
        return
    action = q.data.split(":", 1)[1]

    if action == "new":
        await _start_new_broadcast(update, ctx)
    elif action == "dest":
        await _show_destinations(update, ctx, page=1)
    elif action == "drafts":
        await _show_drafts(update, ctx)
    elif action == "sched":
        await _show_scheduled(update, ctx)
    elif action == "rep":
        await _show_repeating(update, ctx)
    elif action == "stats":
        await _show_stats(update, ctx)
    elif action == "history":
        await _show_history(update, ctx, page=1)
    elif action == "settings":
        await _show_settings(update, ctx)
    elif action == "help":
        await q.edit_message_text(
            "Use /help for full user instructions.",
            reply_markup=kb([[btn("⬅️ Back", "menu:home")]]),
        )
    elif action == "home":
        await q.edit_message_text(
            "🏠 Main menu",
            reply_markup=main_menu_kb(_is_admin(cfg, user["telegram_id"])),
        )


# ---------------------------------------------------------------------------
# Destination registration
# ---------------------------------------------------------------------------

async def on_forward_or_group_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle a forwarded message used to register a destination."""
    msg = update.effective_message
    if not msg:
        return

    # IMPORTANT: if the user is mid-broadcast, this is content, not registration.
    if ctx.user_data.get("flow") == "await_content":
        return

    db: Database = ctx.application.bot_data["db"]
    user = await _guard(update, db)
    if not user:
        return

    origin_chat = None
    fo = msg.forward_origin
    if isinstance(fo, (MessageOriginChannel, MessageOriginChat)):
        origin_chat = fo.chat

    if origin_chat is None:
        return

    await _register_destination(update, ctx, origin_chat)


async def cmd_register_in_group(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL):
        await msg.reply_text("This command only works inside a group or channel.")
        return
    await _register_destination(update, ctx, chat)


async def _register_destination(update: Update, ctx: ContextTypes.DEFAULT_TYPE, chat: Chat) -> None:
    db: Database = ctx.application.bot_data["db"]
    user = update.effective_user
    msg = update.effective_message
    bot = ctx.bot

    chat_label = chat.title or str(chat.id)
    bot_username = bot.username or "your bot"

    try:
        live_chat = await bot.get_chat(chat.id)
    except TelegramError as e:
        s = str(e).lower()
        if any(k in s for k in ("not found", "invalid", "kicked", "deactivated")):
            await msg.reply_text(
                f"❌ I can't access <b>{esc(chat_label)}</b>.\n\n"
                "Please add me first:\n"
                "1. Open the group/channel\n"
                "2. Administrators → Add Administrator\n"
                f"3. Search <code>@{esc(bot_username)}</code> and add me\n"
                "4. Grant <b>Post Messages</b> permission\n\n"
                "Then forward a message again — or run /register inside the chat.",
                parse_mode=ParseMode.HTML,
            )
            return
        if "forbidden" in s or "admin" in s:
            await msg.reply_text(
                f"❌ I don't have permission to inspect <b>{esc(chat_label)}</b>.\n\n"
                "Please add me as administrator with <b>Post Messages</b> permission.",
                parse_mode=ParseMode.HTML,
            )
            return
        await msg.reply_text(
            f"❌ Could not access chat: <code>{esc(str(e))}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        member = await bot.get_chat_member(live_chat.id, bot.id)
    except TelegramError as e:
        s = str(e).lower()
        if "member list is inaccessible" in s or "forbidden" in s or "admin" in s:
            await msg.reply_text(
                f"⚠️ <b>Action required</b> — I'm not an administrator in "
                f"<b>{esc(live_chat.title or chat.id)}</b>.\n\n"
                "Please add me as administrator with <b>Post Messages</b> permission:\n"
                "1. Open the group/channel\n"
                "2. Administrators → Add Administrator\n"
                f"3. Search <code>@{esc(bot_username)}</code> and add me\n"
                "4. Grant <b>Post Messages</b>\n\n"
                "Then forward a message again — or run /register inside the chat.",
                parse_mode=ParseMode.HTML,
            )
            return
        await msg.reply_text(
            f"❌ Could not verify permissions: <code>{esc(str(e))}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    is_admin = getattr(member, "status", "") in ("administrator", "creator")
    if not is_admin:
        await msg.reply_text(
            f"⚠️ <b>Action required</b> — I'm a member but not administrator in "
            f"<b>{esc(live_chat.title or chat.id)}</b>.\n\n"
            "Please promote me to administrator with <b>Post Messages</b> permission.",
            parse_mode=ParseMode.HTML,
        )
        return

    can_post = True
    if live_chat.type == ChatType.CHANNEL:
        can_post = bool(getattr(member, "can_post_messages", True))
    if not can_post:
        await msg.reply_text(
            f"⚠️ I don't have permission to post messages in "
            f"<b>{esc(live_chat.title or chat.id)}</b>.\n"
            "Please grant <b>Post Messages</b>.",
            parse_mode=ParseMode.HTML,
        )
        return

    existing = await db.get_destination_by_chat(live_chat.id)
    if existing:
        if await db.is_manager(existing["id"], user.id):
            await msg.reply_text(
                f"ℹ️ <b>{esc(existing['title'])}</b> is already registered "
                f"(status: <b>{existing['status']}</b>) and you are an authorized manager.\n"
                "Use 📂 My Destinations to manage it.",
                parse_mode=ParseMode.HTML,
            )
        else:
            await msg.reply_text(
                f"ℹ️ <b>{esc(existing['title'])}</b> is already registered in the system "
                "by another manager. If you administer this destination, ask an existing "
                "manager to add you, or contact the bot admin.",
                parse_mode=ParseMode.HTML,
            )
        return

    dest_type = "channel" if live_chat.type == ChatType.CHANNEL else (
        "supergroup" if live_chat.type == ChatType.SUPERGROUP else "group"
    )
    dest_id = await db.add_destination(
        live_chat.id, live_chat.title or str(live_chat.id), dest_type, user.id,
    )
    await msg.reply_text(
        f"✅ Registered <b>{esc(live_chat.title or live_chat.id)}</b> as a {dest_type}.\n"
        f"Destination ID: <code>{dest_id}</code>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_sync(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Recheck all destinations where the bot is a member."""
    db: Database = ctx.application.bot_data["db"]
    user = update.effective_user
    msg = update.effective_message

    dests = await db.user_destinations(user.id)
    if not dests:
        await msg.reply_text("You have no registered destinations.")
        return

    ok = 0
    lost = 0
    no_admin = 0
    for d in dests:
        try:
            member = await ctx.bot.get_chat_member(d["chat_id"], ctx.bot.id)
            status = getattr(member, "status", "")
            if status in ("administrator", "creator"):
                can_post = True
                if d["dest_type"] == "channel":
                    can_post = bool(getattr(member, "can_post_messages", True))
                if can_post:
                    await db.update_destination_status(d["id"], "active", None)
                    ok += 1
                else:
                    await db.update_destination_status(
                        d["id"], "missing_permissions", "No Post Messages")
                    no_admin += 1
            else:
                await db.update_destination_status(
                    d["id"], "missing_permissions", f"Bot is {status}")
                no_admin += 1
        except TelegramError as e:
            await db.update_destination_status(d["id"], "inaccessible", str(e))
            lost += 1

    await msg.reply_text(
        f"<b>Sync complete</b>\n"
        f"✅ OK: {ok}\n"
        f"⚠️ Missing admin rights: {no_admin}\n"
        f"❌ Inaccessible: {lost}",
        parse_mode=ParseMode.HTML,
    )


# ---------------------------------------------------------------------------
# Destinations list
# ---------------------------------------------------------------------------

async def _show_destinations(update: Update, ctx: ContextTypes.DEFAULT_TYPE, page: int) -> None:
    db: Database = ctx.application.bot_data["db"]
    user = update.effective_user
    dests = await db.user_destinations(user.id)
    q = update.callback_query
    if not dests:
        txt = (
            "<b>No destinations yet.</b>\n\n"
            "To register a group/channel:\n"
            "1. Add me as administrator with <b>Post Messages</b> permission.\n"
            "2. Forward any message from that group/channel here — OR — type "
            "<code>/register</code> inside the chat."
        )
        markup = kb([[btn("⬅️ Back", "menu:home")]])
    else:
        items, page, pages = paginate(dests, page)
        rows = []
        for d in items:
            icon = "📢" if d["dest_type"] == "channel" else "👥"
            status_icon = "✅" if d["status"] == "active" else "⚠️"
            rows.append([btn(f"{icon} {status_icon} {d['title'][:40]}", f"dst:view:{d['id']}")])
        nav = []
        if page > 1:
            nav.append(btn("⬅️", f"dstp:{page-1}"))
        nav.append(btn(f"{page}/{pages}", "noop"))
        if page < pages:
            nav.append(btn("➡️", f"dstp:{page+1}"))
        rows.append(nav)
        rows.append([btn("⬅️ Back", "menu:home")])
        txt = f"<b>📂 Your Destinations</b> ({len(dests)})"
        markup = kb(rows)
    if q:
        await q.edit_message_text(txt, reply_markup=markup, parse_mode=ParseMode.HTML)
    else:
        await update.effective_message.reply_text(txt, reply_markup=markup, parse_mode=ParseMode.HTML)


async def on_dstp(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    page = parse_int(q.data.split(":", 1)[1]) or 1
    await _show_destinations(update, ctx, page)


async def on_dst_view(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    db: Database = ctx.application.bot_data["db"]
    dest_id = parse_int(q.data.split(":")[2])
    if not dest_id:
        return
    d = await db.get_destination(dest_id)
    if not d or not await db.is_manager(dest_id, update.effective_user.id):
        await q.edit_message_text("Destination not found or access denied.",
                                  reply_markup=kb([[btn("⬅️ Back", "menu:dest")]]))
        return
    mgrs = await db.list_managers(dest_id)
    mgr_str = ", ".join(f"{esc(m['role'])}:{m['user_telegram_id']}" for m in mgrs)
    txt = (
        f"<b>{esc(d['title'])}</b>\n"
        f"Type: {d['dest_type']}\n"
        f"Chat ID: <code>{d['chat_id']}</code>\n"
        f"Status: <b>{d['status']}</b>\n"
        f"Last verified: {fmt_dt(d['last_verified'])}\n"
        f"Managers: {mgr_str}\n"
        + (f"\nLast error: <code>{esc(truncate(d.get('last_error'), 200))}</code>"
           if d.get("last_error") else "")
    )
    rows = [
        [btn("🔄 Recheck", f"dst:recheck:{dest_id}")],
        [btn("🚫 Remove", f"dst:confirm_rm:{dest_id}")],
        [btn("⬅️ Back", "menu:dest")],
    ]
    await q.edit_message_text(txt, reply_markup=kb(rows), parse_mode=ParseMode.HTML)


async def on_dst_recheck(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer("Checking…")
    db: Database = ctx.application.bot_data["db"]
    dest_id = parse_int(q.data.split(":")[2])
    d = await db.get_destination(dest_id) if dest_id else None
    if not d:
        return
    try:
        chat = await ctx.bot.get_chat(d["chat_id"])
        member = await ctx.bot.get_chat_member(d["chat_id"], ctx.bot.id)
        is_admin = getattr(member, "status", "") in ("administrator", "creator")
        if not is_admin:
            await db.update_destination_status(dest_id, "missing_permissions", "Not admin")
            await q.edit_message_text(
                "⚠️ Bot is no longer administrator.",
                reply_markup=kb([[btn("⬅️ Back", f"dst:view:{dest_id}")]]),
            )
            return
        await db.update_destination_status(dest_id, "active", None)
        await q.edit_message_text(
            f"✅ Destination OK. Title: <b>{esc(chat.title or chat.id)}</b>",
            reply_markup=kb([[btn("⬅️ Back", f"dst:view:{dest_id}")]]),
            parse_mode=ParseMode.HTML,
        )
    except TelegramError as e:
        await db.update_destination_status(dest_id, "inaccessible", str(e))
        await q.edit_message_text(
            f"❌ Not accessible: <code>{esc(truncate(str(e), 200))}</code>",
            reply_markup=kb([[btn("⬅️ Back", f"dst:view:{dest_id}")]]),
            parse_mode=ParseMode.HTML,
        )


async def on_dst_confirm_rm(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    dest_id = parse_int(q.data.split(":")[2])
    if not dest_id:
        return
    await q.edit_message_text(
        "⚠️ Remove this destination? This removes it for all managers.",
        reply_markup=kb([
            [btn("✅ Yes, remove", f"dst:do_rm:{dest_id}")],
            [btn("⬅️ Cancel", f"dst:view:{dest_id}")],
        ]),
    )


async def on_dst_do_rm(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    db: Database = ctx.application.bot_data["db"]
    dest_id = parse_int(q.data.split(":")[2])
    d = await db.get_destination(dest_id) if dest_id else None
    if not d or not await db.is_manager(dest_id, update.effective_user.id):
        await q.edit_message_text("Not authorized.",
                                  reply_markup=kb([[btn("⬅️ Back", "menu:dest")]]))
        return
    await db.remove_destination(dest_id)
    await q.edit_message_text("✅ Removed.", reply_markup=kb([[btn("⬅️ Back", "menu:dest")]]))


# ---------------------------------------------------------------------------
# New broadcast flow
# ---------------------------------------------------------------------------

async def _start_new_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = ctx.application.bot_data["db"]
    user = update.effective_user
    dests = await db.user_destinations(user.id)
    q = update.callback_query
    if not dests:
        txt = "You have no destinations registered. Please register one first."
        markup = kb([[btn("⬅️ Back", "menu:home")]])
        if q:
            await q.edit_message_text(txt, reply_markup=markup)
        else:
            await update.effective_message.reply_text(txt, reply_markup=markup)
        return

    ctx.user_data["flow"] = "select_dests"
    ctx.user_data["selected_destinations"] = set()
    ctx.user_data["all_destinations"] = [d["id"] for d in dests]
    await _render_dest_selection(update, ctx, page=1)


async def _render_dest_selection(update: Update, ctx: ContextTypes.DEFAULT_TYPE, page: int) -> None:
    db: Database = ctx.application.bot_data["db"]
    user = update.effective_user
    dests = await db.user_destinations(user.id)
    selected = ctx.user_data.get("selected_destinations", set())

    items, page, pages = paginate(dests, page)
    rows = []
    for d in items:
        mark = "✅" if d["id"] in selected else "☐"
        icon = "📢" if d["dest_type"] == "channel" else "👥"
        rows.append([btn(f"{mark} {icon} {d['title'][:38]}", f"sel:{d['id']}")])

    ctrl = [btn("✅ Select all", "selall"), btn("⬜ None", "selnone")]
    nav = [btn("⬅️", f"selp:{page-1}")] if page > 1 else []
    nav.append(btn(f"{page}/{pages}", "noop"))
    if page < pages:
        nav.append(btn("➡️", f"selp:{page+1}"))

    rows.append(ctrl)
    rows.append(nav)
    rows.append([
        btn(f"➡️ Continue ({len(selected)})", "flow:confirm_dests"),
        btn("✖️ Cancel", "flow:cancel"),
    ])

    txt = (
        f"<b>Step 1/4 — Select destinations</b>\n\n"
        f"Selected: <b>{len(selected)}</b> destination(s).\n"
        "Tap a destination to toggle it."
    )
    markup = kb(rows)
    q = update.callback_query
    if q:
        await q.edit_message_text(txt, reply_markup=markup, parse_mode=ParseMode.HTML)
    else:
        await update.effective_message.reply_text(txt, reply_markup=markup, parse_mode=ParseMode.HTML)


async def on_sel_toggle(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    dest_id = parse_int(q.data.split(":")[1])
    if not dest_id:
        return
    sel = ctx.user_data.setdefault("selected_destinations", set())
    if dest_id in sel:
        sel.remove(dest_id)
    else:
        sel.add(dest_id)
    await _render_dest_selection(update, ctx, page=1)


async def on_sel_all(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    ctx.user_data["selected_destinations"] = set(ctx.user_data.get("all_destinations", []))
    await _render_dest_selection(update, ctx, page=1)


async def on_sel_none(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    ctx.user_data["selected_destinations"] = set()
    await _render_dest_selection(update, ctx, page=1)


async def on_selp(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    page = parse_int(q.data.split(":")[1]) or 1
    await _render_dest_selection(update, ctx, page=page)


async def on_flow_confirm_dests(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    sel = ctx.user_data.get("selected_destinations", set())
    if not sel:
        await q.answer("Select at least one destination.", show_alert=True)
        return
    ctx.user_data["flow"] = "await_content"
    log.info("Awaiting content from user %s, selected=%d destinations",
             update.effective_user.id, len(sel))
    await q.edit_message_text(
        "<b>Step 2/4 — Send your content</b>\n\n"
        "Now <b>send or forward the message</b> you want to broadcast. It can be text, "
        "photo, video, document, audio, sticker, etc. Custom/premium emoji will be "
        "preserved by Telegram's native copy.\n\n"
        "You can send it as a reply or a new message in this chat.",
        reply_markup=kb([[btn("✖️ Cancel", "flow:cancel")]]),
        parse_mode=ParseMode.HTML,
    )


async def on_flow_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    ctx.user_data.pop("flow", None)
    ctx.user_data.pop("selected_destinations", None)
    ctx.user_data.pop("draft_id", None)
    await q.edit_message_text("Cancelled.", reply_markup=kb([[btn("🏠 Main menu", "menu:home")]]))


async def on_content_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """User sent the message we should broadcast (only when awaiting)."""
    flow = ctx.user_data.get("flow")
    log.info("on_content_message: user=%s flow=%s",
             update.effective_user.id if update.effective_user else None, flow)
    if flow != "await_content":
        return

    db: Database = ctx.application.bot_data["db"]
    emoji: EmojiManager = ctx.application.bot_data["emoji"]
    msg = update.effective_message
    user = update.effective_user

    if not msg or not user:
        return

    replace_draft_id = ctx.user_data.get("replace_draft_id")

    if replace_draft_id:
        old = await db.get_draft(replace_draft_id, user.id)
        if not old:
            await msg.reply_text("Original draft not found. Please start a new broadcast.")
            ctx.user_data.pop("flow", None)
            ctx.user_data.pop("replace_draft_id", None)
            return
        sel = old.get("selected_destinations") or []
        await db.delete_draft(replace_draft_id, user.id)
        ctx.user_data.pop("replace_draft_id", None)
    else:
        sel = ctx.user_data.get("selected_destinations", set())
        if not sel:
            await msg.reply_text("No destinations selected. Please start again.")
            return
        sel = list(sel)

    try:
        ents = msg.entities or msg.caption_entities or []
        await emoji.learn_from_message(msg.text or msg.caption or "", ents, user.id)
    except Exception:
        log.exception("Emoji learning failed")

    try:
        draft_id = await db.create_draft(
            user_tg=user.id,
            source_chat_id=msg.chat_id,
            source_message_id=msg.message_id,
            message_json=message_to_json(msg),
            emoji_mode="auto",
        )
        await db.update_draft_selection(draft_id, user.id, list(sel))
    except Exception:
        log.exception("Failed to create draft")
        await msg.reply_text("⚠️ Failed to save draft. Please try again.")
        return

    ctx.user_data["draft_id"] = draft_id
    ctx.user_data["flow"] = "draft_options"

    summary = summary_from_message(msg)
    txt = (
        f"<b>Step 3/4 — Preview</b>\n\n"
        f"Draft ID: <code>{draft_id}</code>\n"
        f"Summary: {esc(summary)}\n"
        f"Destinations: <b>{len(sel)}</b>\n"
        f"Emoji mode: <b>auto</b> (native copy preserves premium emoji)\n\n"
        "Choose what to do:"
    )
    rows = [
        [btn("📤 Send Now", f"drf:sendnow:{draft_id}")],
        [btn("🗓 Schedule", f"drf:schedule:{draft_id}")],
        [btn("🔁 Repeat", f"drf:repeat:{draft_id}")],
        [btn("🔀 Change Emoji Mode", f"drf:emoji:{draft_id}")],
        [btn("✏️ Replace Content", f"drf:replace:{draft_id}")],
        [btn("🗑 Discard Draft", f"drf:discard:{draft_id}")],
    ]
    await msg.reply_text(txt, reply_markup=kb(rows), parse_mode=ParseMode.HTML)


async def on_drf_action(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    db: Database = ctx.application.bot_data["db"]
    parts = q.data.split(":")
    action = parts[1]
    draft_id = parse_int(parts[2])
    if not draft_id:
        return
    draft = await db.get_draft(draft_id, update.effective_user.id)
    if not draft:
        await q.edit_message_text("Draft not found.")
        return

    if action == "sendnow":
        await _send_now_confirm(update, ctx, draft)
    elif action == "schedule":
        ctx.user_data["flow"] = "schedule_input"
        ctx.user_data["schedule_draft_id"] = draft_id
        await q.edit_message_text(
            "Send the schedule time as <code>YYYY-MM-DD HH:MM</code> "
            "(in your timezone, default UTC).\n"
            "Use ⚙️ Settings to change timezone.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb([[btn("✖️ Cancel", "flow:cancel")]]),
        )
    elif action == "repeat":
        ctx.user_data["flow"] = "repeat_interval"
        ctx.user_data["repeat_draft_id"] = draft_id
        await q.edit_message_text(
            "Send the <b>first run time</b> as <code>YYYY-MM-DD HH:MM</code>.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb([[btn("✖️ Cancel", "flow:cancel")]]),
        )
    elif action == "emoji":
        rows = [
            [btn("Original mode (recommended)", f"drf:setemoji:{draft_id}:original")],
            [btn("Premium emoji mode", f"drf:setemoji:{draft_id}:premium")],
            [btn("Automatic mode", f"drf:setemoji:{draft_id}:auto")],
            [btn("⬅️ Back", f"drf:back:{draft_id}")],
        ]
        await q.edit_message_text(
            "Choose emoji mode:\n"
            "• <b>Original</b> — Telegram's native copy (preserves custom emoji best).\n"
            "• <b>Premium</b> — render text with stored custom emoji mappings.\n"
            "• <b>Auto</b> — pick the safest method (defaults to native copy).",
            reply_markup=kb(rows), parse_mode=ParseMode.HTML,
        )
    elif action == "setemoji":
        mode = parts[3]
        await db.update_draft_emoji_mode(draft_id, update.effective_user.id, mode)
        await q.edit_message_text(
            f"Emoji mode set to <b>{mode}</b>.",
            reply_markup=kb([[btn("⬅️ Back", f"drf:back:{draft_id}")]]),
            parse_mode=ParseMode.HTML,
        )
    elif action == "back":
        await _render_draft_options(update, ctx, draft)
    elif action == "replace":
        ctx.user_data["flow"] = "await_content"
        ctx.user_data["replace_draft_id"] = draft_id
        await q.edit_message_text(
            "Send the new content now.",
            reply_markup=kb([[btn("✖️ Cancel", "flow:cancel")]]),
        )
    elif action == "discard":
        await db.delete_draft(draft_id, update.effective_user.id)
        ctx.user_data.pop("flow", None)
        await q.edit_message_text(
            "🗑 Draft discarded.",
            reply_markup=kb([[btn("🏠 Main menu", "menu:home")]]),
        )


async def _render_draft_options(update: Update, ctx: ContextTypes.DEFAULT_TYPE, draft: dict) -> None:
    q = update.callback_query
    sel = draft.get("selected_destinations") or []
    rows = [
        [btn("📤 Send Now", f"drf:sendnow:{draft['id']}")],
        [btn("🗓 Schedule", f"drf:schedule:{draft['id']}")],
        [btn("🔁 Repeat", f"drf:repeat:{draft['id']}")],
        [btn("🔀 Change Emoji Mode", f"drf:emoji:{draft['id']}")],
        [btn("✏️ Replace Content", f"drf:replace:{draft['id']}")],
        [btn("🗑 Discard Draft", f"drf:discard:{draft['id']}")],
    ]
    await q.edit_message_text(
        f"<b>Draft #{draft['id']}</b>\nEmoji mode: <b>{draft['emoji_mode']}</b>\n"
        f"Destinations: <b>{len(sel)}</b>",
        reply_markup=kb(rows), parse_mode=ParseMode.HTML,
    )


async def _send_now_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE, draft: dict) -> None:
    q = update.callback_query
    sel = draft.get("selected_destinations") or []
    if not sel:
        await q.edit_message_text("No destinations selected.")
        return
    await q.edit_message_text(
        f"<b>Confirm Send Now</b>\n\n"
        f"Draft: <code>{draft['id']}</code>\n"
        f"Destinations: <b>{len(sel)}</b>\n"
        f"Emoji mode: <b>{draft['emoji_mode']}</b>\n\n"
        "Proceed?",
        reply_markup=kb([
            [btn("✅ Confirm &amp; Send", f"drf:do_send:{draft['id']}")],
            [btn("⬅️ Back", f"drf:back:{draft['id']}")],
        ]),
        parse_mode=ParseMode.HTML,
    )


async def on_drf_do_send(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    db: Database = ctx.application.bot_data["db"]
    cfg: Config = ctx.application.bot_data["cfg"]
    engine: BroadcastEngine = ctx.application.bot_data["engine"]
    draft_id = parse_int(q.data.split(":")[2])
    draft = await db.get_draft(draft_id, update.effective_user.id) if draft_id else None
    if not draft:
        await q.edit_message_text("Draft not found.")
        return
    if await db.count_open_campaigns(update.effective_user.id) >= cfg.max_open_campaigns_per_user:
        await q.edit_message_text(
            f"⚠️ You have too many open campaigns "
            f"(limit {cfg.max_open_campaigns_per_user}). Finish or cancel some first.",
            reply_markup=kb([[btn("⬅️ Back", "menu:home")]]),
        )
        return

    dest_ids = draft.get("selected_destinations") or []
    targets = []
    for did in dest_ids:
        d = await db.get_destination(did)
        if d and await db.is_manager(did, update.effective_user.id):
            targets.append({"destination_id": did, "target_chat_id": d["chat_id"]})

    if not targets:
        await q.edit_message_text("No valid destinations.")
        return

    msg_json = draft["source_message_json"]
    if isinstance(msg_json, str):
        msg_json = json.loads(msg_json)

    campaign_id = await db.create_campaign(
        user_tg=update.effective_user.id,
        source_chat_id=draft["source_chat_id"],
        source_message_id=draft["source_message_id"],
        message_json=msg_json,
        summary=truncate(str(msg_json.get("text") or msg_json.get("caption") or "message"), 80),
        emoji_mode=draft["emoji_mode"],
        status="queued",
        scheduled_at=None,
    )
    await db.add_campaign_targets(campaign_id, targets)
    await db.delete_draft(draft_id, update.effective_user.id)
    ctx.user_data.pop("flow", None)
    ctx.user_data.pop("draft_id", None)

    await q.edit_message_text(
        f"🚀 Campaign <b>#{campaign_id}</b> queued for <b>{len(targets)}</b> destination(s).\n"
        f"You'll get a report when it finishes.",
        reply_markup=kb([[btn("🏠 Main menu", "menu:home")]]),
        parse_mode=ParseMode.HTML,
    )
    await engine.enqueue(campaign_id)


# ---------------------------------------------------------------------------
# Schedule / repeat text input
# ---------------------------------------------------------------------------

async def on_text_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    flow = ctx.user_data.get("flow")
    if not flow:
        return
    if flow == "await_content":
        return
    msg = update.effective_message
    db: Database = ctx.application.bot_data["db"]
    user = update.effective_user
    tz = await db.get_user_tz(user.id)

    if flow == "schedule_input":
        dt = parse_datetime_local(msg.text or "", tz)
        if not dt:
            await msg.reply_text("Invalid format. Use YYYY-MM-DD HH:MM.")
            return
        if dt <= datetime.now(timezone.utc):
            await msg.reply_text("Time must be in the future.")
            return
        await _create_scheduled_campaign(update, ctx, dt, repeat_seconds=None, max_reps=None, end_at=None)
    elif flow == "repeat_interval":
        dt = parse_datetime_local(msg.text or "", tz)
        if not dt:
            await msg.reply_text("Invalid format. Use YYYY-MM-DD HH:MM.")
            return
        if dt <= datetime.now(timezone.utc):
            await msg.reply_text("Start time must be in the future.")
            return
        ctx.user_data["repeat_start_dt"] = dt.isoformat()
        ctx.user_data["flow"] = "repeat_interval_value"
        await msg.reply_text(
            "Send the repeat interval, e.g. <code>1h</code>, <code>30m</code>, "
            "<code>2d</code>, or a number of minutes.",
            parse_mode=ParseMode.HTML,
        )
    elif flow == "repeat_interval_value":
        secs = parse_interval(msg.text or "")
        if not secs or secs < 60:
            await msg.reply_text("Interval too small or invalid (min 60 seconds).")
            return
        ctx.user_data["repeat_seconds"] = secs
        ctx.user_data["flow"] = "repeat_max"
        await msg.reply_text("Max number of repetitions (send a number, or 0 for unlimited):")
    elif flow == "repeat_max":
        try:
            mx = int(msg.text or "0")
        except ValueError:
            await msg.reply_text("Send an integer.")
            return
        if mx < 0:
            await msg.reply_text("Must be >= 0.")
            return
        mx_opt = mx if mx > 0 else None
        start_dt = datetime.fromisoformat(ctx.user_data["repeat_start_dt"])
        secs = ctx.user_data["repeat_seconds"]
        await _create_scheduled_campaign(
            update, ctx, start_dt, repeat_seconds=secs, max_reps=mx_opt, end_at=None,
        )
    elif flow == "settings_tz":
        from zoneinfo import ZoneInfo
        try:
            ZoneInfo((msg.text or "").strip())
        except Exception:
            await msg.reply_text("Invalid timezone. Example: Europe/London")
            return
        await db.set_user_tz(user.id, (msg.text or "").strip())
        ctx.user_data.pop("flow", None)
        await msg.reply_text("✅ Timezone updated.")


async def _create_scheduled_campaign(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE, start_dt: datetime,
    repeat_seconds: Optional[int], max_reps: Optional[int], end_at: Optional[datetime],
) -> None:
    db: Database = ctx.application.bot_data["db"]
    msg = update.effective_message
    user = update.effective_user
    draft_id = ctx.user_data.get("schedule_draft_id") or ctx.user_data.get("repeat_draft_id")
    if not draft_id:
        await msg.reply_text("No draft selected.")
        return
    draft = await db.get_draft(draft_id, user.id)
    if not draft:
        await msg.reply_text("Draft not found.")
        return
    dest_ids = draft.get("selected_destinations") or []
    targets = []
    for did in dest_ids:
        d = await db.get_destination(did)
        if d and await db.is_manager(did, user.id):
            targets.append({"destination_id": did, "target_chat_id": d["chat_id"]})
    if not targets:
        await msg.reply_text("No valid destinations.")
        return

    msg_json = draft["source_message_json"]
    if isinstance(msg_json, str):
        msg_json = json.loads(msg_json)

    campaign_id = await db.create_campaign(
        user_tg=user.id,
        source_chat_id=draft["source_chat_id"],
        source_message_id=draft["source_message_id"],
        message_json=msg_json,
        summary=truncate(str(msg_json.get("text") or msg_json.get("caption") or "message"), 80),
        emoji_mode=draft["emoji_mode"],
        status="scheduled",
        scheduled_at=start_dt,
    )
    await db.add_campaign_targets(campaign_id, targets)
    await db.create_scheduled_job(
        campaign_id=campaign_id,
        user_tg=user.id,
        next_run_at=start_dt,
        repeat_interval=repeat_seconds,
        max_reps=max_reps,
        end_at=end_at,
    )
    await db.delete_draft(draft_id, user.id)
    ctx.user_data.pop("flow", None)
    ctx.user_data.pop("schedule_draft_id", None)
    ctx.user_data.pop("repeat_draft_id", None)
    ctx.user_data.pop("repeat_start_dt", None)
    ctx.user_data.pop("repeat_seconds", None)

    label = "scheduled" if not repeat_seconds else f"repeating every {repeat_seconds}s"
    await msg.reply_text(
        f"✅ Campaign <b>#{campaign_id}</b> {label}.\n"
        f"First run: {fmt_dt(start_dt)}",
        parse_mode=ParseMode.HTML,
        reply_markup=kb([[btn("🏠 Main menu", "menu:home")]]),
    )


# ---------------------------------------------------------------------------
# Drafts list
# ---------------------------------------------------------------------------

async def _show_drafts(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = ctx.application.bot_data["db"]
    drafts = await db.list_drafts(update.effective_user.id)
    q = update.callback_query
    if not drafts:
        txt = "<b>No drafts.</b>"
        markup = kb([[btn("⬅️ Back", "menu:home")]])
    else:
        rows = []
        for d in drafts:
            rows.append([btn(f"📝 Draft #{d['id']}", f"drf:back:{d['id']}")])
        rows.append([btn("⬅️ Back", "menu:home")])
        txt = "<b>📝 Drafts</b>"
        markup = kb(rows)
    if q:
        await q.edit_message_text(txt, reply_markup=markup, parse_mode=ParseMode.HTML)
    else:
        await update.effective_message.reply_text(txt, reply_markup=markup, parse_mode=ParseMode.HTML)


# ---------------------------------------------------------------------------
# Scheduled / repeating lists
# ---------------------------------------------------------------------------

async def _show_scheduled(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = ctx.application.bot_data["db"]
    jobs = await db.list_user_jobs(update.effective_user.id)
    q = update.callback_query
    jobs = [j for j in jobs if not j.get("repeat_interval_seconds")]
    if not jobs:
        txt = "<b>No scheduled broadcasts.</b>"
        markup = kb([[btn("⬅️ Back", "menu:home")]])
    else:
        rows = []
        for j in jobs:
            rows.append([btn(f"🗓 #{j['campaign_id']} → {fmt_dt(j['next_run_at'])}",
                             f"sch:view:{j['id']}")])
        rows.append([btn("⬅️ Back", "menu:home")])
        txt = "<b>🗓 Scheduled</b>"
        markup = kb(rows)
    if q:
        await q.edit_message_text(txt, reply_markup=markup, parse_mode=ParseMode.HTML)
    else:
        await update.effective_message.reply_text(txt, reply_markup=markup, parse_mode=ParseMode.HTML)


async def _show_repeating(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = ctx.application.bot_data["db"]
    jobs = await db.list_user_jobs(update.effective_user.id)
    q = update.callback_query
    jobs = [j for j in jobs if j.get("repeat_interval_seconds")]
    if not jobs:
        txt = "<b>No repeating campaigns.</b>"
        markup = kb([[btn("⬅️ Back", "menu:home")]])
    else:
        rows = []
        for j in jobs:
            rows.append([btn(f"🔁 #{j['campaign_id']} every {j['repeat_interval_seconds']}s",
                             f"sch:view:{j['id']}")])
        rows.append([btn("⬅️ Back", "menu:home")])
        txt = "<b>🔁 Repeating</b>"
        markup = kb(rows)
    if q:
        await q.edit_message_text(txt, reply_markup=markup, parse_mode=ParseMode.HTML)
    else:
        await update.effective_message.reply_text(txt, reply_markup=markup, parse_mode=ParseMode.HTML)


async def on_sch_view(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    db: Database = ctx.application.bot_data["db"]
    job_id = parse_int(q.data.split(":")[2])
    job = await db.get_job(job_id) if job_id else None
    if not job or job["user_telegram_id"] != update.effective_user.id:
        await q.edit_message_text("Not found.")
        return
    txt = (
        f"<b>Job #{job['id']}</b>\n"
        f"Campaign: <code>{job['campaign_id']}</code>\n"
        f"Status: <b>{job['status']}</b>\n"
        f"Next run: {fmt_dt(job['next_run_at'])}\n"
        f"Runs done: {job['runs_done']}\n"
        f"Repeat interval: {job['repeat_interval_seconds'] or '—'}\n"
        f"Max repetitions: {job['max_repetitions'] or '∞'}"
    )
    rows = []
    if job["status"] == "active":
        rows.append([btn("⏸ Pause", f"sch:pause:{job_id}"),
                     btn("🚫 Cancel", f"sch:confirm_cancel:{job_id}")])
    elif job["status"] == "paused":
        rows.append([btn("▶️ Resume", f"sch:resume:{job_id}"),
                     btn("🚫 Cancel", f"sch:confirm_cancel:{job_id}")])
    rows.append([btn("⬅️ Back", "menu:home")])
    await q.edit_message_text(txt, reply_markup=kb(rows), parse_mode=ParseMode.HTML)


async def on_sch_action(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    db: Database = ctx.application.bot_data["db"]
    parts = q.data.split(":")
    action = parts[1]
    job_id = parse_int(parts[2])
    job = await db.get_job(job_id) if job_id else None
    if not job or job["user_telegram_id"] != update.effective_user.id:
        await q.edit_message_text("Not found.")
        return
    if action == "pause":
        await db.set_job_status(job_id, "paused")
    elif action == "resume":
        await db.set_job_status(job_id, "active")
    elif action == "confirm_cancel":
        await q.edit_message_text(
            "⚠️ Cancel this scheduled/repeating campaign?",
            reply_markup=kb([
                [btn("✅ Yes", f"sch:cancel:{job_id}")],
                [btn("⬅️ Back", f"sch:view:{job_id}")],
            ]),
        )
        return
    elif action == "cancel":
        await db.set_job_status(job_id, "cancelled")
        await db.set_campaign_status(job["campaign_id"], "cancelled")
    await on_sch_view(update, ctx)


# ---------------------------------------------------------------------------
# Stats / history / settings
# ---------------------------------------------------------------------------

async def _show_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = ctx.application.bot_data["db"]
    s = await db.user_stats(update.effective_user.id)
    total = s["ok"] + s["fail"]
    rate = f"{(s['ok']/total*100):.1f}%" if total else "—"
    txt = (
        f"<b>📊 Your Statistics</b>\n\n"
        f"Total campaigns: <b>{s['total_campaigns']}</b>\n"
        f"Total destination deliveries: <b>{s['total_targets']}</b>\n"
        f"✅ Successful: <b>{s['ok']}</b>\n"
        f"❌ Failed: <b>{s['fail']}</b>\n"
        f"Success rate: <b>{rate}</b>\n"
        f"🗓 Scheduled: <b>{s['scheduled']}</b>\n"
        f"🔁 Repeating: <b>{s['repeating']}</b>"
    )
    q = update.callback_query
    markup = kb([[btn("⬅️ Back", "menu:home")]])
    if q:
        await q.edit_message_text(txt, reply_markup=markup, parse_mode=ParseMode.HTML)
    else:
        await update.effective_message.reply_text(txt, reply_markup=markup, parse_mode=ParseMode.HTML)


async def _show_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE, page: int) -> None:
    db: Database = ctx.application.bot_data["db"]
    total = await db.count_user_campaigns(update.effective_user.id)
    items, page, pages = paginate(
        await db.list_user_campaigns(update.effective_user.id, (page - 1) * PAGE_SIZE, PAGE_SIZE),
        page, PAGE_SIZE,
    )
    rows = []
    for c in items:
        rows.append([btn(f"#{c['id']} {c['status']} — {truncate(c['content_summary'], 30)}",
                         f"camp:view:{c['id']}")])
    nav = []
    if page > 1:
        nav.append(btn("⬅️", f"camp:page:{page-1}"))
    nav.append(btn(f"{page}/{pages}", "noop"))
    if page < pages:
        nav.append(btn("➡️", f"camp:page:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([btn("⬅️ Back", "menu:home")])
    txt = f"<b>📜 History</b> ({total})"
    q = update.callback_query
    if q:
        await q.edit_message_text(txt, reply_markup=kb(rows), parse_mode=ParseMode.HTML)
    else:
        await update.effective_message.reply_text(txt, reply_markup=kb(rows), parse_mode=ParseMode.HTML)


async def on_camp_view(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    db: Database = ctx.application.bot_data["db"]
    parts = q.data.split(":")
    if parts[1] == "page":
        await _show_history(update, ctx, parse_int(parts[2]) or 1)
        return
    cid = parse_int(parts[2])
    camp = await db.get_campaign(cid) if cid else None
    if not camp or camp["user_telegram_id"] != update.effective_user.id:
        await q.edit_message_text("Not found.")
        return
    stats = await db.campaign_stats(cid)
    txt = (
        f"<b>Campaign #{cid}</b>\n"
        f"Status: <b>{camp['status']}</b>\n"
        f"Summary: {esc(truncate(camp['content_summary'], 60))}\n"
        f"Created: {fmt_dt(camp['created_at'])}\n"
        f"Started: {fmt_dt(camp['started_at'])}\n"
        f"Completed: {fmt_dt(camp['completed_at'])}\n\n"
        f"Targets: {sum(stats.values())}\n"
        f"✅ Success: {stats.get('success', 0)}\n"
        f"❌ Failed: {stats.get('failed', 0)}\n"
        f"❓ Uncertain: {stats.get('uncertain', 0)}\n"
        f"⏳ Pending: {stats.get('pending', 0)}"
    )
    rows = [[btn("📄 Destination Details", f"camp:dets:{cid}")]]
    if camp["status"] in ("running", "queued"):
        rows.append([btn("⏸ Pause", f"camp:pause:{cid}"), btn("🚫 Cancel", f"camp:cancel:{cid}")])
    elif camp["status"] == "paused":
        rows.append([btn("▶️ Resume", f"camp:resume:{cid}")])
    rows.append([btn("⬅️ Back", "menu:history")])
    await q.edit_message_text(txt, reply_markup=kb(rows), parse_mode=ParseMode.HTML)


async def on_camp_action(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    db: Database = ctx.application.bot_data["db"]
    parts = q.data.split(":")
    action = parts[1]
    cid = parse_int(parts[2])
    camp = await db.get_campaign(cid) if cid else None
    if not camp or camp["user_telegram_id"] != update.effective_user.id:
        await q.edit_message_text("Not found.")
        return
    if action == "pause":
        await db.set_campaign_status(cid, "paused")
    elif action == "resume":
        await db.set_campaign_status(cid, "queued")
        engine: BroadcastEngine = ctx.application.bot_data["engine"]
        await engine.enqueue(cid)
    elif action == "cancel":
        await db.set_campaign_status(cid, "cancelled")
    elif action == "dets":
        targets = await db.get_campaign_targets(cid)
        lines = []
        for t in targets[:20]:
            lines.append(f"• <code>{t['target_chat_id']}</code> — {t['status']}"
                         + (f" — {esc(truncate(t['last_error'], 40))}"
                            if t.get("last_error") else ""))
        more = "" if len(targets) <= 20 else f"\n…and {len(targets)-20} more"
        await q.edit_message_text(
            "<b>Destination results</b>\n" + "\n".join(lines) + more,
            reply_markup=kb([[btn("⬅️ Back", f"camp:view:{cid}")]]),
            parse_mode=ParseMode.HTML,
        )
        return
    await on_camp_view(update, ctx)


async def _show_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = ctx.application.bot_data["db"]
    tz = await db.get_user_tz(update.effective_user.id)
    txt = f"<b>⚙️ Settings</b>\n\nTimezone: <b>{tz}</b>"
    rows = [
        [btn("🕒 Change Timezone", "settings:tz")],
        [btn("⬅️ Back", "menu:home")],
    ]
    q = update.callback_query
    if q:
        await q.edit_message_text(txt, reply_markup=kb(rows), parse_mode=ParseMode.HTML)
    else:
        await update.effective_message.reply_text(txt, reply_markup=kb(rows), parse_mode=ParseMode.HTML)


async def on_settings_action(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    action = q.data.split(":", 1)[1]
    if action == "tz":
        ctx.user_data["flow"] = "settings_tz"
        await q.edit_message_text(
            "Send the new timezone (e.g. <code>Europe/London</code>, <code>Asia/Kolkata</code>).",
            parse_mode=ParseMode.HTML,
        )


# ---------------------------------------------------------------------------
# Admin panel
# ---------------------------------------------------------------------------

async def _admin_home(update: Update, ctx: ContextTypes.DEFAULT_TYPE, edit: bool = True) -> None:
    db: Database = ctx.application.bot_data["db"]
    o = await db.admin_overview()
    txt = (
        f"<b>🛡 Admin Dashboard</b>\n\n"
        f"👥 Users: <b>{o['users']}</b>\n"
        f"📂 Destinations: <b>{o['destinations']}</b>\n"
        f"📣 Campaigns: <b>{o['campaigns']}</b>\n"
        f"🏃 Running: <b>{o['running']}</b>\n"
        f"🗓 Scheduled: <b>{o['scheduled']}</b>\n"
        f"❌ Failed targets: <b>{o['failed_targets']}</b>"
    )
    rows = [
        [btn("👥 Users", "adm:users:0"), btn("📣 Campaigns", "adm:camps:0")],
        [btn("📂 Destinations", "adm:dests:0")],
        [btn("📤 Admin Broadcast", "adm:bc")],
        [btn("📊 Global Stats", "adm:gstats")],
        [btn("📥 Export Users (Excel)", "adm:export_users")],
        [btn("🏠 Main menu", "menu:home")],
    ]
    markup = kb(rows)
    q = update.callback_query
    if edit and q:
        await q.edit_message_text(txt, reply_markup=markup, parse_mode=ParseMode.HTML)
    else:
        await update.effective_message.reply_text(txt, reply_markup=markup, parse_mode=ParseMode.HTML)


async def on_adm(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    cfg: Config = ctx.application.bot_data["cfg"]
    if not _is_admin(cfg, update.effective_user.id):
        await q.answer("Admin only.", show_alert=True)
        return
    db: Database = ctx.application.bot_data["db"]
    parts = q.data.split(":")
    action = parts[1]
    arg = parts[2] if len(parts) > 2 else None

    if action == "home":
        await _admin_home(update, ctx)
    elif action == "users":
        page = parse_int(arg) if arg else 0
        users = await db.list_users(page * 8, 8)
        total = await db.count_users()
        rows = []
        for u in users:
            rows.append([btn(
                f"{u['display_name'][:25]} ({u['telegram_id']})",
                f"adm:user:{u['telegram_id']}",
            )])
        nav = []
        if page > 0:
            nav.append(btn("⬅️", f"adm:users:{page-1}"))
        nav.append(btn(f"{page+1}/{(total+7)//8}", "noop"))
        if (page + 1) * 8 < total:
            nav.append(btn("➡️", f"adm:users:{page+1}"))
        if nav:
            rows.append(nav)
        rows.append([btn("⬅️ Back", "adm:home")])
        await q.edit_message_text(
            f"<b>👥 Users</b> ({total})",
            reply_markup=kb(rows), parse_mode=ParseMode.HTML,
        )
    elif action == "user":
        uid = parse_int(arg)
        u = await db.get_user(uid) if uid else None
        if not u:
            await q.edit_message_text("Not found.",
                                      reply_markup=kb([[btn("⬅️ Back", "adm:users:0")]]))
            return
        txt = (
            f"<b>User {u['telegram_id']}</b>\n"
            f"Name: {esc(u['display_name'])}\n"
            f"Username: @{esc(u['username'] or '—')}\n"
            f"Status: <b>{u['status']}</b>\n"
            f"Broadcasts: {u['total_broadcasts']}\n"
            f"✅ {u['successful_deliveries']} / ❌ {u['failed_deliveries']}\n"
            f"Joined: {fmt_dt(u['first_joined'])}\n"
            f"Last active: {fmt_dt(u['last_active'])}"
        )
        rows = []
        if u["status"] == "active":
            rows.append([btn("🚫 Restrict", f"adm:restrict:{uid}")])
        else:
            rows.append([btn("✅ Restore", f"adm:restore:{uid}")])
        rows.append([btn("⬅️ Back", "adm:users:0")])
        await q.edit_message_text(txt, reply_markup=kb(rows), parse_mode=ParseMode.HTML)
    elif action == "restrict":
        uid = parse_int(arg)
        await db.set_user_status(uid, "restricted")
        await db.log_admin_action(update.effective_user.id, "restrict_user", "user", str(uid))
        await q.answer("Restricted.")
        await on_adm(update, ctx)
    elif action == "restore":
        uid = parse_int(arg)
        await db.set_user_status(uid, "active")
        await db.log_admin_action(update.effective_user.id, "restore_user", "user", str(uid))
        await q.answer("Restored.")
        await on_adm(update, ctx)
    elif action == "camps":
        page = parse_int(arg) if arg else 0
        camps = await db.list_all_campaigns(page * 8, 8)
        rows = []
        for c in camps:
            rows.append([btn(
                f"#{c['id']} {c['status']} u:{c['user_telegram_id']}",
                f"camp:view:{c['id']}",
            )])
        rows.append([btn("⬅️ Back", "adm:home")])
        await q.edit_message_text("<b>📣 All Campaigns</b>",
                                  reply_markup=kb(rows), parse_mode=ParseMode.HTML)
    elif action == "dests":
        page = parse_int(arg) if arg else 0
        dests = await db.all_destinations(page * 8, 8)
        rows = []
        for d in dests:
            rows.append([btn(f"{d['title'][:30]} [{d['status']}]", f"dst:view:{d['id']}")])
        rows.append([btn("⬅️ Back", "adm:home")])
        await q.edit_message_text("<b>📂 All Destinations</b>",
                                  reply_markup=kb(rows), parse_mode=ParseMode.HTML)
    elif action == "gstats":
        o = await db.admin_overview()
        txt = "\n".join(f"{k}: <b>{v}</b>" for k, v in o.items())
        await q.edit_message_text(
            f"<b>📊 Global Stats</b>\n\n{txt}",
            reply_markup=kb([[btn("⬅️ Back", "adm:home")]]),
            parse_mode=ParseMode.HTML,
        )
    elif action == "export_users":
        await q.answer("Generating…")
        try:
            from exports import export_users_xlsx
            path = await export_users_xlsx(db, "users.xlsx")
            await ctx.bot.send_document(
                chat_id=update.effective_user.id, document=open(path, "rb"),
                filename="users.xlsx",
            )
            import os
            os.unlink(path)
        except Exception as e:
            log.exception("Export failed")
            await q.answer(f"Export failed: {e}", show_alert=True)
    elif action == "bc":
        await q.edit_message_text(
            "<b>📤 Admin broadcast</b>\n\nSend a message here and reply with "
            "whether to broadcast to <b>all users</b>.",
            reply_markup=kb([
                [btn("🌐 All users", "adm:bcall")],
                [btn("⬅️ Back", "adm:home")],
            ]),
            parse_mode=ParseMode.HTML,
        )
    elif action == "bcall":
        ctx.user_data["admin_flow"] = "broadcast_all"
        await q.edit_message_text(
            "Send the text you want broadcast to all users.",
            reply_markup=kb([[btn("✖️ Cancel", "adm:home")]]),
        )


# ---------------------------------------------------------------------------
# Global error handler
# ---------------------------------------------------------------------------

async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Unhandled exception: %s", context.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(
                "⚠️ Something went wrong. The error was logged."
            )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Handler registration
# ---------------------------------------------------------------------------

def register(app, cfg: Config) -> None:
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("adminhelp", cmd_adminhelp))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("register", cmd_register_in_group))
    app.add_handler(CommandHandler("sync", cmd_sync))

    # Menu
    app.add_handler(CallbackQueryHandler(on_menu, pattern=r"^menu:"))
    app.add_handler(CallbackQueryHandler(on_dstp, pattern=r"^dstp:"))
    app.add_handler(CallbackQueryHandler(on_dst_view, pattern=r"^dst:view:"))
    app.add_handler(CallbackQueryHandler(on_dst_recheck, pattern=r"^dst:recheck:"))
    app.add_handler(CallbackQueryHandler(on_dst_confirm_rm, pattern=r"^dst:confirm_rm:"))
    app.add_handler(CallbackQueryHandler(on_dst_do_rm, pattern=r"^dst:do_rm:"))

    # Selection
    app.add_handler(CallbackQueryHandler(on_sel_toggle, pattern=r"^sel:\d+$"))
    app.add_handler(CallbackQueryHandler(on_sel_all, pattern=r"^selall$"))
    app.add_handler(CallbackQueryHandler(on_sel_none, pattern=r"^selnone$"))
    app.add_handler(CallbackQueryHandler(on_selp, pattern=r"^selp:"))

    # Flow
    app.add_handler(CallbackQueryHandler(on_flow_confirm_dests, pattern=r"^flow:confirm_dests$"))
    app.add_handler(CallbackQueryHandler(on_flow_cancel, pattern=r"^flow:cancel$"))

    # Draft
    app.add_handler(CallbackQueryHandler(on_drf_do_send, pattern=r"^drf:do_send:"))
    app.add_handler(CallbackQueryHandler(on_drf_action, pattern=r"^drf:"))

    # Campaign
    app.add_handler(CallbackQueryHandler(on_camp_view, pattern=r"^camp:(view|page|dets):"))
    app.add_handler(CallbackQueryHandler(on_camp_action, pattern=r"^camp:(pause|resume|cancel):"))

    # Scheduled
    app.add_handler(CallbackQueryHandler(on_sch_view, pattern=r"^sch:view:"))
    app.add_handler(CallbackQueryHandler(on_sch_action, pattern=r"^sch:"))

    # Settings
    app.add_handler(CallbackQueryHandler(on_settings_action, pattern=r"^settings:"))

    # Admin
    app.add_handler(CallbackQueryHandler(on_adm, pattern=r"^adm:"))

    # Text inputs (schedule, repeat, timezone)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_input))

    # Forwarded messages → destination registration (guarded against await_content)
    app.add_handler(MessageHandler(filters.FORWARDED, on_forward_or_group_message))

    # Content for broadcast (all non-command messages)
    app.add_handler(MessageHandler(~filters.COMMAND, on_content_message))

    # Global error handler
    app.add_error_handler(_on_error)
