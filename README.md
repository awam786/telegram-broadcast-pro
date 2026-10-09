# Telegram Broadcast Pro

A multi-user Telegram broadcast bot using Python, python-telegram-bot, PostgreSQL, and Railway.

## Features
- User registration and destination ownership.
- Register groups with `/register` after adding the bot as an administrator; channel registration is attempted when the bot is promoted.
- Immediate campaigns by replying to a source message with `/broadcast`.
- UTC scheduled or repeating campaigns by replying with `/schedule YYYY-MM-DDTHH:MM [INTERVAL_MINUTES]`.
- Campaign history and cancellation.
- Master-admin campaigns targeting `users`, `groups`, `channels`, or `all` by replying with `/adminbroadcast users|groups|channels|all`.
- PostgreSQL-backed campaigns and delivery logs; scheduled work survives restarts.
- Telegram `copyMessage` used to preserve supported message formatting, media, and custom-emoji entities when Telegram allows it.
- Retry-after handling and bounded sending pace.

## Setup
1. Create a bot with BotFather.
2. Create a PostgreSQL database.
3. Copy `.env.example` to `.env` and set `BOT_TOKEN`, `DATABASE_URL`, and `ADMIN_IDS` (comma-separated numeric Telegram user IDs).
4. Install with `pip install -r requirements.txt`.
5. Run `python main.py`.

Never commit `.env` or real credentials.

## Railway
1. Deploy this repository from GitHub in Railway.
2. Add a PostgreSQL service, or provide an existing PostgreSQL URL.
3. Set `BOT_TOKEN`, `DATABASE_URL`, `ADMIN_IDS`, `LOG_LEVEL`, `SEND_DELAY_SECONDS`, and `MAX_CAMPAIGN_TARGETS` in Railway Variables.
4. Deploy using the included Dockerfile. Run only one polling replica per bot token.

## Commands
All users:
- `/start`, `/help`
- `/register` (run in a group; caller must be a group administrator and bot must be an administrator)
- `/destinations`
- Reply to a source message with `/broadcast`
- Reply to a source message with `/schedule 2026-10-10T12:00` or `/schedule 2026-10-10T12:00 1440` (UTC; repeat interval is minutes)
- `/campaigns`
- `/cancel CAMPAIGN_ID`

Master admins configured by `ADMIN_IDS`:
- Reply to a source message with `/adminbroadcast users`, `/adminbroadcast groups`, `/adminbroadcast channels`, or `/adminbroadcast all`
- `/adminstats`

## Important limitations
- A bot cannot initiate a private conversation with someone who has never started it. The `users` audience includes registered users who have started the bot.
- Add the bot as an administrator with permission to post to every destination.
- Campaigns copy a source message accessible to the bot. Telegram may restrict copying protected content or certain message types.
- Global campaigns should only target audiences that expect the messages and must comply with Telegram rules and applicable law.
- This is a functional starting release. Test in a private group and channel before large campaigns, and monitor Railway logs.
