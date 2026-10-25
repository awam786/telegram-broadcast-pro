"""Excel exports using openpyxl (non-blocking)."""
from __future__ import annotations

import asyncio
import os
import tempfile
from typing import List

from openpyxl import Workbook

from database import Database


def _write_users(path: str, rows: List[dict]) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Users"
    headers = ["telegram_id", "username", "display_name", "status",
               "total_broadcasts", "successful_deliveries", "failed_deliveries",
               "first_joined", "last_active"]
    ws.append(headers)
    for r in rows:
        ws.append([
            r["telegram_id"], r["username"], r["display_name"], r["status"],
            r["total_broadcasts"], r["successful_deliveries"], r["failed_deliveries"],
            str(r["first_joined"]), str(r["last_active"]),
        ])
    for col, width in zip("ABCDEFGHI", [16, 20, 30, 12, 15, 20, 15, 25, 25]):
        ws.column_dimensions[col].width = width
    wb.save(path)


async def export_users_xlsx(db: Database, filename: str) -> str:
    rows: List[dict] = []
    page = 0
    while True:
        chunk = await db.list_users(page * 200, 200)
        if not chunk:
            break
        rows.extend(chunk)
        page += 1
    path = os.path.join(tempfile.gettempdir(), filename)
    await asyncio.to_thread(_write_users, path, rows)
    return path
