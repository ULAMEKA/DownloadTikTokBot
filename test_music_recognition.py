import asyncio
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

import init_db
from services.queue import (
    _add_music_track,
    _format_music_tracks,
    _music_chunk_starts,
    _should_refresh_cached_music,
    _should_send_music_tracks,
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

    def test_formats_timestamps_and_keeps_no_description_and_inline_clean(self) -> None:
        self.assertIn("00:15 — Artist — Song", _format_music_tracks([(15, "Artist", "Song")]) or "")
        self.assertTrue(_should_send_music_tracks(False, None))
        self.assertFalse(_should_send_music_tracks(True, None))
        self.assertFalse(_should_send_music_tracks(False, "inline-message-id"))
        self.assertTrue(_should_refresh_cached_music(None, True))
        self.assertFalse(_should_refresh_cached_music("", True))

    def test_migrates_existing_cache_with_music_tracks_column(self) -> None:
        async def check_migration() -> list[str]:
            with tempfile.TemporaryDirectory() as temp_dir:
                database_path = Path(temp_dir) / "legacy.db"
                engine = create_async_engine(f"sqlite+aiosqlite:///{database_path.as_posix()}")
                async with engine.begin() as conn:
                    await conn.execute(text(
                        "CREATE TABLE VideoCache ("
                        "id INTEGER PRIMARY KEY, tiktok_id TEXT UNIQUE, file_id TEXT, "
                        "storage_message_id INTEGER, description TEXT)"
                    ))

                original_engine = init_db.engine
                init_db.engine = engine
                try:
                    await init_db.init_db()
                    async with engine.connect() as conn:
                        result = await conn.execute(text("PRAGMA table_info(VideoCache)"))
                        return [row[1] for row in result]
                finally:
                    init_db.engine = original_engine
                    await engine.dispose()

        self.assertIn("music_tracks", asyncio.run(check_migration()))


if __name__ == "__main__":
    unittest.main()
