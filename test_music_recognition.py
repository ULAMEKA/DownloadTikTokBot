import asyncio
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import init_db
import services.queue as queue
from caption import source_link
from db.base import Base
from db.models import DownloadQueue, MusicRequest
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


class MusicSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_recognition_and_ui_run_after_request_session_is_closed(self):
        session_closed = asyncio.Event()
        metrics = queue.JobStageMetrics(job_id=1)
        request = SimpleNamespace(
            token="a" * 32,
            user_id="1",
            chat_id="1",
            status_message_id=7,
            inline_message_id=None,
            tiktok_link="https://www.tiktok.com/@a/video/123",
            format_id=None,
            state="queued",
            music_tracks=None,
        )
        session = AsyncMock()
        session.get.return_value = request
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=session)

        async def close_session(*_args):
            session_closed.set()

        context.__aexit__ = AsyncMock(side_effect=close_session)

        async def recognize(*_args, **_kwargs):
            self.assertTrue(session_closed.is_set())
            return ""

        async def show_result(_bot, finished_request, music_tracks):
            self.assertTrue(session_closed.is_set())
            self.assertIs(finished_request, request)
            self.assertEqual(music_tracks, "")

        job = queue.QueueJob(
            1,
            "1",
            "1",
            "music_job",
            request.tiktok_link,
            None,
            None,
            f"music:{request.token}",
            None,
        )
        with (
            patch.object(queue, "SessionLocal", return_value=context),
            patch.object(queue, "JobStageMetrics", return_value=metrics),
            patch.object(queue, "_recognize_music_from_source", side_effect=recognize),
            patch.object(
                queue,
                "_finish_music_job",
                AsyncMock(return_value=request),
                create=True,
            ),
            patch.object(queue, "show_music_request_result", side_effect=show_result),
            patch.object(
                metrics,
                "log_summary",
                wraps=metrics.log_summary,
            ) as log_summary,
        ):
            await queue._process_music_job(MagicMock(), job, request.token)

        self.assertIn("delivery", metrics.durations_ms)
        log_summary.assert_called_once_with(
            cache_status="not_applicable",
            ffmpeg_bypassed=None,
            queue_wait_ms=None,
        )

    async def test_queue_wait_is_snapshotted_before_music_processing(self):
        enqueued_at = datetime(2026, 8, 30, 12, 0, 0, tzinfo=timezone.utc)
        clock = SimpleNamespace(current=enqueued_at + timedelta(seconds=1))

        class FakeDateTime:
            @classmethod
            def now(cls, _tz=None):
                return clock.current

        request = SimpleNamespace(
            token="f" * 32,
            user_id="1",
            chat_id="1",
            status_message_id=7,
            inline_message_id=None,
            tiktok_link="https://www.tiktok.com/@a/video/123",
            format_id=None,
            state="queued",
            music_tracks=None,
        )

        async def recognize(*_args, **_kwargs):
            clock.current = enqueued_at + timedelta(seconds=6)
            return ""

        job = queue.QueueJob(
            2,
            "1",
            "1",
            "music_job",
            request.tiktok_link,
            None,
            None,
            f"music:{request.token}",
            enqueued_at,
        )
        with (
            patch.object(queue, "datetime", FakeDateTime),
            patch.object(queue, "_load_music_request", AsyncMock(return_value=request)),
            patch.object(queue, "_recognize_music_from_source", side_effect=recognize),
            patch.object(queue, "_finish_music_job", AsyncMock(return_value=request)),
            patch.object(queue, "show_music_request_result", AsyncMock()),
            self.assertLogs(level="INFO") as captured,
        ):
            await queue._process_music_job(MagicMock(), job, request.token)

        summary = next(line for line in captured.output if "job_stage_summary" in line)
        self.assertIn("queue_wait_ms=1000", summary)
        self.assertNotIn("queue_wait_ms=6000", summary)


class MusicStageMetricsTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_h264_music_source_never_enters_video_preparation(self):
        """Music recognition only needs an audio-extractable source, not Telegram video."""
        with tempfile.TemporaryDirectory() as directory:
            video_dir = Path(directory)

            def download(*_args):
                (video_dir / "music_hevc.mp4").write_bytes(b"hevc source")
                return "123", 640, 360, "description", 15.0

            with (
                patch.object(queue, "VIDEO_DIR", video_dir),
                patch.object(queue, "_download_media", side_effect=download) as download_media,
                patch.object(queue, "_recognize_music_tracks", AsyncMock(return_value="Artist — Song")),
                patch.object(queue, "_prepare_telegram_video") as prepare_video,
            ):
                result = await queue._recognize_music_from_source(
                    "https://www.tiktok.com/@a/video/123",
                    "music_hevc",
                    "hevc-only",
                )

        self.assertEqual(result, "Artist — Song")
        self.assertEqual(download_media.call_args.args[3], "hevc-only")
        prepare_video.assert_not_called()

    async def test_download_ffmpeg_and_shazam_have_separate_stages(self):
        metrics = queue.JobStageMetrics(job_id=2)

        class FakeShazam:
            def __init__(self, *, segment_duration_seconds):
                self.segment_duration_seconds = segment_duration_seconds

            async def recognize(self, _path):
                return {"track": {"title": "Song", "subtitle": "Artist"}}

        with tempfile.TemporaryDirectory() as directory:
            video_dir = Path(directory)

            def download(*_args):
                (video_dir / "music_job.mp4").write_bytes(b"video")
                return "123", 640, 360, "description", 15.0

            def extract(_video_path, _start, output_path):
                output_path.write_bytes(b"audio")

            with (
                patch.object(queue, "VIDEO_DIR", video_dir),
                patch.object(queue, "_download_media", side_effect=download),
                patch.object(queue, "_extract_music_chunk", side_effect=extract),
                patch.dict(
                    "sys.modules",
                    {"shazamio": SimpleNamespace(Shazam=FakeShazam)},
                ),
            ):
                result = await queue._recognize_music_from_source(
                    "https://www.tiktok.com/@a/video/123",
                    "music_job",
                    None,
                    metrics=metrics,
                )

        self.assertIn("Artist — Song", result)
        self.assertIn("download", metrics.durations_ms)
        self.assertIn("prepare", metrics.durations_ms)
        self.assertIn("inspect", metrics.durations_ms)


class MusicJobFinalizationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        database_path = Path(self.temp_dir.name) / "music-finish.sqlite3"
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.session_patch = patch.object(queue, "SessionLocal", self.session_factory)
        self.session_patch.start()

    async def asyncTearDown(self) -> None:
        self.session_patch.stop()
        await self.engine.dispose()
        self.temp_dir.cleanup()

    async def _add_job(self, token: str) -> int:
        async with self.session_factory() as session:
            session.add(
                MusicRequest(
                    token=token,
                    user_id="1",
                    chat_id="1",
                    status_message_id=7,
                    inline_message_id=None,
                    tiktok_link="https://www.tiktok.com/@a/video/123",
                    format_id=None,
                    state="queued",
                    music_tracks=None,
                )
            )
            job = DownloadQueue(
                chat_id="1",
                user_id="1",
                filename=f"music_{token}",
                tiktok_link="https://www.tiktok.com/@a/video/123",
                inline_message_id=f"music:{token}",
            )
            session.add(job)
            await session.commit()
            return job.id

    async def test_load_returns_an_immutable_detached_snapshot(self):
        token = "b" * 32
        await self._add_job(token)

        snapshot = await queue._load_music_request(token)

        self.assertIsInstance(snapshot, queue.MusicRequestSnapshot)
        self.assertEqual(snapshot.token, token)
        self.assertEqual(snapshot.state, "queued")
        with self.assertRaises(FrozenInstanceError):
            snapshot.state = "done"

    async def test_successful_finish_marks_done_and_deletes_job_atomically(self):
        token = "c" * 32
        job_id = await self._add_job(token)

        snapshot = await queue._finish_music_job(
            job_id,
            token,
            "00:00 — Artist — Song",
            retry=False,
        )

        async with self.session_factory() as session:
            request = await session.get(MusicRequest, token)
            job = await session.get(DownloadQueue, job_id)
        self.assertIsNone(job)
        self.assertEqual(request.state, "done")
        self.assertEqual(request.music_tracks, "00:00 — Artist — Song")
        self.assertEqual(snapshot.state, "done")
        self.assertEqual(snapshot.music_tracks, request.music_tracks)

    async def test_retry_finish_restores_ready_and_deletes_job_atomically(self):
        token = "d" * 32
        job_id = await self._add_job(token)

        snapshot = await queue._finish_music_job(
            job_id,
            token,
            None,
            retry=True,
        )

        async with self.session_factory() as session:
            request = await session.get(MusicRequest, token)
            job = await session.get(DownloadQueue, job_id)
        self.assertIsNone(job)
        self.assertEqual(request.state, "ready")
        self.assertIsNone(request.music_tracks)
        self.assertEqual(snapshot.state, "ready")

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
        self.assertIn("file_ids", columns)
        self.assertIn("MusicRequest", tables)
        self.assertIn("status_message_id", music_columns)
        self.assertIn("inline_message_id", music_columns)
        self.assertEqual(cached_file_ids, ["file-id"])


if __name__ == "__main__":
    unittest.main()
