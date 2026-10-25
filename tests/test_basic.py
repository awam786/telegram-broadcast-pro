"""Basic unit tests (no live Telegram)."""
import asyncio
import os
import pytest

from config import load_config, ConfigError


def test_config_missing_token(monkeypatch):
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@h:5432/d")
    with pytest.raises(ConfigError):
        load_config()


def test_config_valid(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123:abc")
    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@h:5432/d")
    monkeypatch.setenv("ADMIN_IDS", "1, 2 ,3")
    monkeypatch.setenv("TIMEZONE", "UTC")
    cfg = load_config()
    assert cfg.database_url.startswith("postgresql://")
    assert cfg.admin_ids == {1, 2, 3}
    assert cfg.max_concurrent_deliveries >= 1


def test_interval_parsing():
    from utils import parse_interval
    assert parse_interval("5m") == 300
    assert parse_interval("2h") == 7200
    assert parse_interval("30") == 1800
    assert parse_interval("1d") == 86400
    assert parse_interval("bad") is None


def test_paginate():
    from utils import paginate
    items, page, pages = paginate(list(range(20)), 2, 8)
    assert len(items) == 8
    assert page == 2
    assert pages == 3


def test_message_summary():
    from utils import truncate
    assert truncate("a" * 100, 10) == "a" * 9 + "…"
