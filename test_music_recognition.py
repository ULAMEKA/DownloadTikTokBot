import asyncio
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

import init_db
from caption import source_link
from services.queue import (
    MUSIC_PAGE_PREFIX,
    _add_music_track,
    _format_music_tracks,
    _music_chunk_starts,
    _music_message_parts,
    _music_result_markup,
    _music_status_text,
    _telegram_text_length,
    can_offer_music,
)


class MusicRecognitionTests(unittest.TestCase):
    def test_uses_non_overlapping_fifteen_second_chunks(self) -> None:
        self.assertEqual(list(_music_chunk_starts(31)), [0, 15, 30])

    def test_only_adjacent_duplicate_tracks_are_removed(self) -> None:
        tracks = []
        result = {"track": {"title": "Song", "subtitle": "Artist"}}

        _add_music_track(tracks, 0, result)
        _add_music_track(tracks, 15, result)
        _add_music_track(
            tracks, 30, {"track": {"title": "Another", "subtitle": "Artist"}}
        )
        _add_music_track(tracks, 45, result)

        self.assertEqual(
            tracks,
            [(0, "Artist", "Song"), (30, "Artist", "Another"), (45, "Artist", "Song")],
        )

    def test_formats_timestamps_and_keeps_video_inline_and_no_description_button_free(self) -> None:
        self.assertIn("00:15 — Artist — Song", _format_music_tracks([(15, "Artist", "Song")]) or "")
        self.assertTrue(can_offer_music(False, None))
        self.assertFalse(can_offer_music(True, None))
        self.assertFalse(can_offer_music(False, "inline-message-id"))

    def test_inline_result_pages_keep_the_remaining_tracks_reachable(self) -> None:
        token = "a" * 32
        markup = _music_result_markup(token, 0, 2)

        self.assertIsNotNone(markup)
        self.assertEqual(
            markup.inline_keyboard[0][0].callback_data,
            f"{MUSIC_PAGE_PREFIX}{token}:1",
        )

    def test_music_result_respects_telegram_utf16_limit(self) -> None:
        parts = _music_message_parts("🎵 Найденные треки:\n" + "😀" * 3000)

        self.assertTrue(all(_telegram_text_length(part) <= 4096 for part in parts))

    def test_music_status_and_result_parts_keep_the_source_link(self) -> None:
        url = "https://www.tiktok.com/@author/video/123?share=1"
        source = source_link(url)

        self.assertIn(source, _music_status_text("🎵 Идёт распознавание…", url))
        parts = _music_message_parts("🎵 Найденные треки:\n" + "Song\n" * 1000, source)

        self.assertTrue(all(source in part for part in parts))
        self.assertTrue(all(_telegram_text_length(part) <= 4096 for part in parts))

    def test_migrates_existing_cache_and_creates_music_request_table(self) -> None:
        async def check_migration() -> tuple[list[str], list[str], list[str], list[str]]:
            with tempfile.TemporaryDirectory() as temp_dir:
                database_path = Path(temp_dir) / "legacy.db"
                engine = create_async_engine(f"sqlite+aiosqlite:///{database_path.as_posix()}")
                async with engine.begin() as conn:
                    await conn.execute(text(
                        "CREATE TABLE VideoCache ("
                        "id INTEGER PRIMARY KEY, tiktok_id TEXT UNIQUE, file_id TEXT, "
                        "storage_message_id INTEGER, description TEXT)"
                    ))
                    await conn.execute(text(
                        "CREATE TABLE MusicRequest ("
                        "token TEXT PRIMARY KEY, user_id TEXT NOT NULL, chat_id TEXT NOT NULL, "
                        "tiktok_link TEXT NOT NULL, format_id TEXT, state TEXT NOT NULL, "
                        "music_tracks TEXT)"
                    ))
                    await conn.execute(text(
                        "INSERT INTO VideoCache "
                        "(id, tiktok_id, file_id, storage_message_id, description) "
                        "VALUES (1, 'legacy', 'file-id', 100, 'description')"
                    ))

                original_engine = init_db.engine
                init_db.engine = engine
                try:
                    await init_db.init_db()
                    async with engine.connect() as conn:
                        result = await conn.execute(text("PRAGMA table_info(VideoCache)"))
                        music_result = await conn.execute(text("PRAGMA table_info(MusicRequest)"))
                        tables = await conn.execute(text(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        ))
                        rows = await conn.execute(text("SELECT file_id FROM VideoCache"))
                        return (
                            [row[1] for row in result],
                            [row[1] for row in music_result],
                            [row[0] for row in tables],
                            [row[0] for row in rows],
                        )
                finally:
                    init_db.engine = original_engine
                    await engine.dispose()

        columns, music_columns, tables, cached_file_ids = asyncio.run(check_migration())
        self.assertIn("music_tracks", columns)
        self.assertIn("source_url", columns)
        self.assertIn("likes", columns)
        self.assertIn("MusicRequest", tables)
        self.assertIn("status_message_id", music_columns)
        self.assertIn("inline_message_id", music_columns)
        self.assertEqual(cached_file_ids, ["file-id"])


if __name__ == "__main__":
    unittest.main()
