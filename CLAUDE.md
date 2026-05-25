# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

A Telegram bot that downloads TikTok videos on demand. Users send a TikTok URL; the bot queues the request, downloads the video with `yt-dlp`, sends it back, and deletes the local file.

## Running the bot

```bash
# Activate the virtual environment first
.venv\Scripts\activate   # Windows PowerShell

# Run the bot
python main.py
```

The bot requires a `.env` file in the project root with:
```
TOKEN=<telegram_bot_token>
```

The SQLite database (`DownloadTT.db`) and the `videos/` directory are created automatically on first run.

## Architecture

The entry point is `main.py`, which:
1. Creates the `Bot` and `Dispatcher` (aiogram 3.x, long-polling).
2. Attaches `DBSessionMiddleware` so every message handler receives an injected `session: AsyncSession`.
3. Spawns `process_queue(bot)` as a background asyncio task.
4. Registers routers: `commands_router` (slash commands) and `link` (TikTok URL handler).

**Request flow:**
- A user sends a TikTok URL → `handlers/link.py:handle_tiktok_link` validates it via regex, inserts a `DownloadQueue` row, and replies with the user's current queue position.
- The background loop in `services/queue.py:process_queue` polls `DownloadQueue` every second. When an item is found it downloads the video with `yt-dlp` into `videos/<uuid>.mp4`, sends it to the user via `bot.send_video`, deletes the file, then removes the row.

**Key modules:**

| Path | Purpose |
|---|---|
| `db/models.py` | `DownloadQueue` SQLAlchemy model (`BotDownloadQueue` table) |
| `db/session.py` | Async SQLite engine + `SessionLocal` session factory |
| `middleware/db_session.py` | Injects `session` into every handler's `data` dict |
| `handlers/link.py` | URL detection + queue insertion handler |
| `services/queue.py` | Background worker that processes the download queue |
| `routers/command_router.py` | `/start` command |
| `states.py` | `DownloadVideo` FSM states (currently unused in active code) |

**Routers vs Handlers:** `routers/` contains mostly empty placeholder files (`admin_router.py`, `callback_router.py`, `link_router.py`). Active routing lives in `handlers/link.py` (the `link` Router) and `routers/command_router.py`.

## Dependencies

Managed via `.venv`. Key packages: `aiogram` 3.x, `yt-dlp`, `sqlalchemy` + `aiosqlite`, `python-dotenv`.

`test.py` is a standalone scratch script for testing a single yt-dlp download — not a test suite.