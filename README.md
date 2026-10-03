# Telegram Existing Class Indexer Bot

A Railway-friendly Telethon bot that scans **existing Telegram channel posts** and builds a topic-wise clickable class library.

## Important
This project intentionally does **not** store Telegram API credentials in source code. Put them in Railway Variables / environment variables.

The previous crash shown in the screenshot happened because the app called `input()` for a phone/bot token. Railway has no interactive stdin, so this version uses `BOT_TOKEN` from the environment and never asks for input.

## Features
- Scan existing channel history with Telethon/MTProto.
- Group `Part-1`, `Part-2`, etc. under one normalized topic.
- Parse common date formats.
- Detect video/PDF/image/text posts.
- Generate direct Telegram message links.
- Search by topic/title/raw caption.
- Pagination for all topics.
- Owner-only `/scan` and `/setchannel`.
- SQLite index; works with Railway persistent volume.
- No new class upload is required for the initial index.

## Railway variables
Set:

- `API_ID`
- `API_HASH`
- `BOT_TOKEN`
- `TARGET_CHANNEL` (e.g. `@mychannel` or `-1001234567890`)
- `OWNER_USER_ID`
- `DB_PATH=/app/data/classes.db`

Do not paste real secrets into Git. GitHub recommends repository/environment secrets for sensitive values, and Railway should receive them as service variables.

## First run
1. Add the bot as an administrator of the target channel.
2. Set the environment variables.
3. Deploy.
4. Open the bot and send `/id`.
5. Put that numeric ID into `OWNER_USER_ID` and redeploy.
6. Send `/scan` to index the existing channel history.
7. Use `/start` → `📚 All Topics` or `/search keyword`.

## Commands
- `/start` — main menu
- `/id` — show your Telegram numeric ID
- `/setchannel @channelusername` — owner only; saves target channel
- `/scan` — owner only; scans the existing channel history
- `/search keyword` — search indexed topics/classes

## Notes about private channels
For a private channel, use the numeric channel ID (`-100...`) in `TARGET_CHANNEL` and make sure the bot has access/admin rights. The generated private message links use the `t.me/c/...` format.

## Persistence
Attach a Railway Volume mounted at `/app/data` so the SQLite index survives redeploys.
