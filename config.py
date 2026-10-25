"""Environment configuration and validation."""
from __future__ import annotations

import os
import logging
from dataclasses import dataclass
from typing import Set
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)


class ConfigError(RuntimeError):
    pass


def _require(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise ConfigError(f"Missing required environment variable: {name}")
    return v


def _int(name: str, default: int, minv: int = 0) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        n = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if n < minv:
        raise ConfigError(f"{name} must be >= {minv}")
    return n


def _ids(name: str) -> Set[int]:
    raw = os.getenv(name, "")
    if not raw.strip():
        return set()
    out: Set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(int(part))
        except ValueError as exc:
            raise ConfigError(f"{name} contains non-numeric value: {part}") from exc
    return out


def _db_url(raw: str) -> str:
    # asyncpg needs postgres:// -> postgresql://
    if raw.startswith("postgres://"):
        raw = raw.replace("postgres://", "postgresql://", 1)
    return raw


@dataclass(frozen=True)
class Config:
    bot_token: str
    database_url: str
    admin_ids: Set[int]
    timezone: str
    log_level: str
    max_concurrent_deliveries: int
    max_open_campaigns_per_user: int
    max_destinations_per_user: int
    rate_limit_seconds: float
    scheduler_poll_seconds: int


def load_config() -> Config:
    token = _require("BOT_TOKEN").strip()
    if ":" not in token:
        raise ConfigError("BOT_TOKEN does not look like a valid token")

    db_url = _db_url(_require("DATABASE_URL"))
    admins = _ids("ADMIN_IDS")
    tz = os.getenv("TIMEZONE", "UTC").strip() or "UTC"
    try:
        ZoneInfo(tz)
    except Exception as exc:
        raise ConfigError(f"Invalid TIMEZONE: {tz}") from exc

    cfg = Config(
        bot_token=token,
        database_url=db_url,
        admin_ids=admins,
        timezone=tz,
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        max_concurrent_deliveries=_int("MAX_CONCURRENT_DELIVERIES", 5, 1),
        max_open_campaigns_per_user=_int("MAX_OPEN_CAMPAIGNS_PER_USER", 3, 1),
        max_destinations_per_user=_int("MAX_DESTINATIONS_PER_USER", 50, 1),
        rate_limit_seconds=float(os.getenv("RATE_LIMIT_SECONDS", "0.06")),
        scheduler_poll_seconds=_int("SCHEDULER_POLL_SECONDS", 15, 5),
    )
    log.info(
        "Config loaded: admins=%s tz=%s max_deliveries=%s",
        cfg.admin_ids, cfg.timezone, cfg.max_concurrent_deliveries,
    )
    return cfg
