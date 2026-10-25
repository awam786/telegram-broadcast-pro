"""Shared helpers."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message

PAGE_SIZE = 8


def esc(s: Any) -> str:
    """Escape for Telegram HTML parse_mode."""
    s = str(s if s is not None else "")
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def truncate(s: Optional[str], n: int = 60) -> str:
    if not s:
        return ""
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def fmt_dt(dt: Optional[datetime], tz: str = "UTC") -> str:
    if not dt:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M:%S %Z")


def message_to_json(msg: Message) -> Dict[str, Any]:
    """Serialize enough of a Telegram message to reconstruct/copy it later."""
    data: Dict[str, Any] = {
        "message_id": msg.message_id,
        "chat_id": msg.chat_id,
        "date": msg.date.isoformat() if msg.date else None,
        "text": msg.text,
        "caption": msg.caption,
        "entities": [
            {"type": e.type, "offset": e.offset, "length": e.length,
             "url": e.url, "user": None,
             "custom_emoji_id": getattr(e, "custom_emoji_id", None),
             "language": getattr(e, "language", None)}
            for e in (msg.entities or msg.caption_entities or [])
        ],
        "media_group_id": msg.media_group_id,
    }
    for attr in ("photo", "video", "document", "audio", "voice", "animation", "sticker", "video_note", "poll"):
        v = getattr(msg, attr, None)
        if v is not None:
            data["media_type"] = attr
            break
    return data


def summary_from_message(msg: Message, prefix: str = "") -> str:
    base = msg.text or msg.caption or f"[{ (getattr(msg, 'photo', None) and 'photo') or (getattr(msg, 'video', None) and 'video') or (getattr(msg, 'document', None) and 'document') or 'message' }]"
    return prefix + truncate(base, 80)


def kb(rows: List[List[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(rows)


def btn(text: str, cb: Optional[str] = None, url: Optional[str] = None) -> InlineKeyboardButton:
    if url:
        return InlineKeyboardButton(text=text, url=url)
    return InlineKeyboardButton(text=text, callback_data=cb or "noop")


def parse_int(raw: str) -> Optional[int]:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def paginate(items: List[Any], page: int, size: int = PAGE_SIZE):
    total = len(items)
    pages = max(1, (total + size - 1) // size)
    page = max(1, min(page, pages))
    start = (page - 1) * size
    return items[start:start + size], page, pages


def parse_datetime_local(raw: str, tz_name: str) -> Optional[datetime]:
    """Accept 'YYYY-MM-DD HH:MM' interpreted in user's tz, return UTC aware dt."""
    raw = raw.strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%d-%m-%Y %H:%M"):
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.replace(tzinfo=ZoneInfo(tz_name)).astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def parse_interval(raw: str) -> Optional[int]:
    """Parse things like '5m', '2h', '1d', or integer minutes."""
    raw = raw.strip().lower()
    m = re.fullmatch(r"(\d+)\s*([smhd])?", raw)
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2) or "m"
    return {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit] * n
