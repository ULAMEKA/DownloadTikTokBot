# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

A Telegram bot that downloads short videos on demand from **TikTok, Instagram Reels, and YouTube Shorts**. Users send a supported URL; the bot queues the request, downloads the video with `yt-dlp`, sends it back, and deletes the local file.

## Running the bot

```bash
# Activate the virtual environment first
.venv\Scripts\activate   # Windows PowerShell

# Run the bot
python main.py
```

Requirements:
- **Python 3.13** (dependencies lack wheels for 3.14).
- A `.env` file in the project root (see `.env.example`) with at least `TOKEN=<telegram_bot_token>`.
- **ffmpeg on `PATH`** — yt-dlp uses it to merge separate video+audio streams (needed for YouTube Shorts). Linux: `apt install ffmpeg`; Windows: `winget install Gyan.FFmpeg`.

Run `python init_db.py` once before the first start (and after schema changes) — `main.py` does not create/migrate the schema. The `videos/` directory is created automatically.

## Architecture

The entry point is `main.py`, which:
1. Creates the `Bot` and `Dispatcher` (aiogram 3.x, long-polling).
2. Attaches `DBSessionMiddleware` so every handler receives an injected `session: AsyncSession`.
3. Spawns `process_queue(bot)` as a background asyncio task.
4. Registers routers: `commands_router` (`/start`), `link` (URL handler), `inline_router` (inline mode).

**Request flow:**
- A user sends a supported URL → `handlers/link.py` extracts it via `utils/links.py:find_video_url`. In private chat it enqueues immediately (`services/queue.py:enqueue_download`); in groups it first shows a "Загрузить видео" button.
- The background loop `services/queue.py:process_queue` polls `DownloadQueue` every second. For each item it resolves a cache key (`utils/links.py:media_key`, `"<platform>:<id>"`), checks `VideoCache`, otherwise downloads with `yt-dlp` (H.264+AAC, ffmpeg merge) into `videos/<uuid>.*`, optionally uploads to a storage channel for a reusable `file_id`, delivers the video, caches the `file_id`, and removes the file + queue row.

**Key modules:**

| Path | Purpose |
|---|---|
| `config.py` | Reads env (`TOKEN`, `STORAGE_CHAT_ID`, `DB_URL`, `DB_ECHO`); validates `TOKEN` |
| `db/models.py` | `DownloadQueue` (`BotDownloadQueue` table) + `VideoCache` models |
| `db/session.py` | Async SQLite engine + `SessionLocal` session factory |
| `middleware/db_session.py` | Injects `session` into every handler's `data` dict |
| `handlers/link.py` | URL detection (private/group/callback) + enqueue |
| `routers/inline_router.py` | Inline mode (`@bot <url>`): instant cached delivery or placeholder |
| `routers/command_router.py` | `/start` command |
| `services/queue.py` | `enqueue_download` producer + `process_queue` background worker |
| `ui/keyboards.py` | Inline keyboard factories |
| `ui/caption.py` | Video caption + source link |
| `ui/promo.py` | Promo message text |
| `utils/links.py` | Supported-URL detection + cache key |
| `states.py` | `DownloadVideo` FSM states (currently unused in active code) |

## Dependencies

Managed via `.venv`. Key packages: `aiogram` 3.x, `yt-dlp`, `sqlalchemy` + `aiosqlite`, `python-dotenv`. External: `ffmpeg` (system binary on `PATH`).

`test.py` is a standalone scratch script for testing a single yt-dlp download — not a test suite.
