# Telegram Broadcast Pro — Menu Upgrade

This upgrade adds an inline-button dashboard, separates user help from administrator help, scopes the Telegram command menu by chat, and improves dashboard text and campaign summaries.

## Files in this package

- `handlers.py` — complete replacement with inline dashboards, user `/help`, restricted `/adminhelp`, and callback navigation.
- `main.py` — complete replacement with user-safe default command menu and a separate admin command menu for each `ADMIN_IDS` entry.

The existing `config.py`, `database.py`, `broadcaster.py`, `requirements.txt`, `Dockerfile`, and `railway.toml` are retained unchanged for this focused UI upgrade.

## Apply

1. Rotate the exposed Telegram bot token in @BotFather first.
2. In GitHub, replace `handlers.py` with the file in this package.
3. Replace `main.py` with the file in this package.
4. Commit both replacements to `main`; Railway should redeploy automatically.
5. Set the new token only in Railway Variables (`BOT_TOKEN`). Never commit it.
6. Test `/start`, `/help`, `/adminhelp`, inline menu buttons, `/destinations`, and a small broadcast.

## Important behavior

- `/help` never appends administrator commands, even for an admin.
- `/adminhelp` checks the Telegram user ID against `ADMIN_IDS`.
- The default Telegram command menu is user-only; configured admins receive a separate private-chat command scope.
- Inline menu callbacks also enforce admin authorization for `a:` actions.
- Admin audience buttons explain the matching `/adminbroadcast <scope>` command. In this version, sending the command as a reply to a source message queues the campaign immediately; it does not yet require a separate confirmation step.

## Notes

This is a focused interface and access-separation upgrade, not a full campaign-composer redesign. Test in a private group/channel before large sends. Telegram may cache command menus briefly after changes.
