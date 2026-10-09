import os
from dotenv import load_dotenv

load_dotenv()


def parse_admin_ids(raw: str) -> set[int]:
    ids: set[int] = set()
    for value in raw.split(","):
        value = value.strip()
        if value:
            try:
                ids.add(int(value))
            except ValueError as exc:
                raise ValueError("ADMIN_IDS must contain comma-separated numeric Telegram IDs") from exc
    return ids


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
ADMIN_IDS = parse_admin_ids(os.getenv("ADMIN_IDS", ""))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
SEND_DELAY_SECONDS = max(0.0, float(os.getenv("SEND_DELAY_SECONDS", "0.08")))
MAX_CAMPAIGN_TARGETS = max(1, int(os.getenv("MAX_CAMPAIGN_TARGETS", "10000")))


def validate_config() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is required")
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is required")
    if not ADMIN_IDS:
        raise RuntimeError("Set at least one numeric Telegram user ID in ADMIN_IDS")
