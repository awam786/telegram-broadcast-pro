"""Custom/premium emoji helpers.

Telegram's Bot API preserves custom emoji entities natively when using copy_message.
For rendered text sending, we must supply MessageEntity(CUSTOM_EMOJI, offset, length,
custom_emoji_id=...). This module provides lookup + validation helpers.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from telegram import MessageEntity

from database import Database

log = logging.getLogger(__name__)


class EmojiManager:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def learn_from_message(self, text: str, entities: List[MessageEntity],
                                 added_by: int) -> int:
        """Store all custom emoji document ids from a message's entities."""
        count = 0
        for e in entities or []:
            if e.type == MessageEntity.CUSTOM_EMOJI and e.custom_emoji_id:
                fallback = text[e.offset:e.offset + e.length] if text else ""
                await self.db.add_premium_emoji(
                    unicode_fallback=fallback,
                    document_id=e.custom_emoji_id,
                    description=f"learned from user {added_by}",
                    added_by=added_by,
                )
                count += 1
        return count

    def has_custom_emoji(self, entities: Optional[List[MessageEntity]]) -> bool:
        return any(e.type == MessageEntity.CUSTOM_EMOJI for e in (entities or []))

    def count_custom_emoji(self, entities: Optional[List[MessageEntity]]) -> int:
        return sum(1 for e in (entities or []) if e.type == MessageEntity.CUSTOM_EMOJI)

    def validate_entities(self, text: str, entities: List[MessageEntity]) -> bool:
        """Return True if all entity offsets/lengths lie inside text.

        Telegram entity offsets are UTF-16 code-unit offsets.
        """
        if not entities:
            return True
        utf16_len = len(text.encode("utf-16-le")) // 2
        for e in entities:
            if e.offset < 0 or e.length <= 0:
                return False
            if e.offset + e.length > utf16_len:
                return False
            if e.type == MessageEntity.CUSTOM_EMOJI and not e.custom_emoji_id:
                return False
        return True

    async def get_emoji_id(self, unicode_fallback: str) -> Optional[str]:
        row = await self.db.get_premium_emoji(unicode_fallback)
        return row["document_id"] if row else None
