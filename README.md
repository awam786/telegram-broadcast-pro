# Telegram Broadcast Pro

Multi-user Telegram broadcasting bot with PostgreSQL persistence, scheduling,
repeating campaigns, delivery reports, premium (custom) emoji preservation,
and a master-admin panel.

## Features

- Multi-user with per-user isolation
- Destination (group/channel) registration with multi-manager support
- Broadcast composer: text, photo, video, document, audio, sticker, voice,
  animation, media albums (individually copied), custom/premium emoji
  (native `copy_message` preserves entities)
- Drafts persisted in PostgreSQL
- Send Now / Schedule / Repeat with persistent jobs
- Delivery engine with retries, rate-limit handling, `RetryAfter`
- Per-destination delivery results and reports
- Admin dashboard, user management, exports to Excel
- `/help` (user) and `/adminhelp` (admin) separated
- Graceful shutdown, DB pool, SIGTERM handling

## Quick start (local)

```bash
cp .env.example .env
# edit .env with your BOT_TOKEN and DATABASE_URL
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

## Railway deployment

1. Push this repo to GitHub.
2. In Railway, **New Project → Deploy from GitHub repo**.
3. **Add a PostgreSQL plugin** to the project. Railway will set `DATABASE_URL`
   on your service automatically (or use the “Connect” button).
4. Set the following environment variables on your service:
   - `BOT_TOKEN` (required)
   - `ADMIN_IDS` (required, comma-separated Telegram user IDs)
   - `DATABASE_URL` (auto-set by plugin, or paste manually)
   - `TIMEZONE` (optional, e.g. `Europe/London`)
   - `LOG_LEVEL` (optional, default `INFO`)
5. Railway detects `Dockerfile` and builds.
6. Check **Deploy Logs** — you should see:
   ```
   Database connected and schema ensured
   Broadcast engine started with N workers
   Scheduler started
   Starting polling...
   ```
7. In Telegram, `/start` your bot.

> **Important:** run exactly **one** replica. The scheduler + delivery workers
> are designed for a single process. For horizontal scale you would need an
> external job queue (not implemented here).

## Registering groups / channels

1. Add the bot to the group/channel as **administrator**.
2. Grant **Post Messages** (and **Edit Messages** for channels).
3. Forward any message from that chat to the bot.
4. The bot calls `getChat` + `getChatMember` to verify and registers it.
5. If it was already registered by another manager, you will be told so
   (no confusing “already registered to another account” error).

## Premium / custom emoji

- **Native copy (`copy_message`)** preserves custom emoji entities exactly as
  Telegram intended. This is the default `original` / `auto` mode.
- Custom emoji entities are also **learned** into `premium_emojis` when they
  appear in messages sent to the bot.
- Telegram's Bot API **cannot** send custom emoji in inline keyboard button
  text. We don't claim otherwise.
- No promise is made that premium emoji work in every context — Telegram's own
  API restrictions apply.

## Delivery semantics

Telegram API sends and DB writes are not atomic. If the worker crashes after a
successful send but before the DB update, the target is marked
`uncertain` rather than blindly retried (to avoid duplicate sends). You can
inspect uncertain targets and manually delete-and-retry if you want.

## Tests

```bash
pip install pytest pytest-asyncio
pytest tests/
```

Tests use mocked Telegram interactions.

## Admin commands

- `/admin` — open the admin panel (only for IDs in `ADMIN_IDS`)
- `/adminhelp` — admin help

All admin actions are logged in `admin_actions`.
