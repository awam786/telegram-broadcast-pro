# Telegram Broadcast Pro — Advanced UI & Scheduling Upgrade

This package replaces `handlers.py`, `database.py`, `main.py`, and `README.md` in the existing project. It retains the current `config.py`, `broadcaster.py`, `requirements.txt`, Dockerfile and Railway setup.

## Added
- User-only `/help` and separate admin-only `/adminhelp`.
- Inline user/admin dashboards and private-chat draft workflow for text, photos, videos and supported message types.
- Schedule presets: 1 hour, 6 hours, 12 hours, tomorrow; recurring hourly/daily/weekly; custom UTC date/time and repeat interval.
- System stats page with all registered users, paginated details, campaigns count, successful/failed delivery attempts, joined date and last active date.
- CSV export of the full user report.
- Admin command `/setpremiumemojis` to save Telegram custom emoji IDs from a replied-to message; `/resetpremiumemojis` restores Unicode fallbacks.
- PostgreSQL migration adds the `premium_emojis` table automatically.

## Premium/custom emoji setup
1. Send a message in a private chat with the bot containing your Telegram custom/premium emojis in the desired order.
2. Reply to that message with `/setpremiumemojis`.
3. The first custom emoji maps to Rocket, second to Broadcast, third to Calendar, then Destination, Stats, Help, Shield, User, Success, Warning, Failed, Clock, Target, Campaign, Repeat, Sparkle, Settings, Chart.
4. The bot uses Telegram's custom-emoji HTML entity in its own message text where supported. Inline keyboard button labels remain standard text because Telegram does not provide the same entity support for button labels.
5. Use `/resetpremiumemojis` to remove the custom IDs.

The source message must contain actual Telegram custom-emoji entities, not regular Unicode emoji characters.

## Scheduling
Send the content to the bot in a private chat. Use the inline `Schedule` button and choose a preset, repeat hourly/daily/weekly, or custom time. Custom times use UTC: `YYYY-MM-DD HH:MM [INTERVAL_MINUTES]`. Existing `/schedule YYYY-MM-DDTHH:MM [INTERVAL_MINUTES]` remains supported by replying to the source message.

## Admin user report
Click **System Stats & Users** in the admin dashboard or run `/adminstats`. The report includes registered user name, username, campaign count, successful and failed message-delivery attempts, joined date, last active timestamp, pagination and CSV export. Delivery totals come from `campaign_deliveries`; campaign count is the number of campaign records owned by that user.

## Deploy
1. Replace the four files above in the existing GitHub repository.
2. Commit to `main`; Railway will redeploy.
3. Confirm `BOT_TOKEN`, `DATABASE_URL`, and `ADMIN_IDS` are set in Railway. Never commit real credentials.
4. Test `/start`, `/help`, `/adminhelp`, custom emoji setup, draft broadcast, schedule presets, and the admin user report in a private test destination before large campaigns.

Only run one polling replica per bot token. Large campaigns should target people and destinations that expect the messages and comply with Telegram's rules and applicable law.


## Premium custom emojis and shared destination admins

- To configure custom emojis, send a message containing real Telegram custom emoji entities, then reply to that message with `/setpremiumemojis`. Ordinary Unicode emojis do not carry custom emoji IDs.
- The bot owner needs active Telegram Premium for custom emoji icons in inline buttons, unless the bot qualifies through Telegram's Fragment additional-username path. Button custom emojis are displayed as the button's icon, not embedded inside the button label.
- After `/register` is run by any current administrator in a group/channel, the bot syncs all current human chat administrators into the shared destination access table. Each administrator can then open the bot privately and use that group's destination.
- Destination access is rechecked against Telegram when a campaign is queued; former/demoted admins are not allowed to broadcast.
- Existing destination owners are retained; the old “already registered to another account” ownership block is removed for verified chat administrators.


## Admin targeted broadcasts

Administrators can target **all users, all groups, all channels, or all destinations**, or manually select recipients before queuing a campaign. Reply to the source message/media with `/adminbroadcast users`, `/adminbroadcast groups`, `/adminbroadcast channels`, or `/adminbroadcast all`. Choose **Send to all** or **Select specific**. The recipient picker is paginated, supports selecting/deselecting individual entries, and requires a final confirmation before anything is queued. `MAX_CAMPAIGN_TARGETS` still applies.
