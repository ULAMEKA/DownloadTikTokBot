import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from db.base import Base
from db.models import DownloadQueue, MusicRequest, VideoCache
import services.queue as queue


SOURCE_URL = "https://www.tiktok.com/@a/video/123"


class _SessionProxy:
    def __init__(self, session, fail_commit: bool):
        self._session = session
        self._fail_commit = fail_commit

    def __getattr__(self, name):
        return getattr(self._session, name)

    async def commit(self):
        if self._fail_commit:
            raise RuntimeError("commit failed")
        await self._session.commit()


class _SessionContext:
    def __init__(self, real_factory, fail_commit: bool):
        self._real_context = real_factory()
        self._fail_commit = fail_commit

    async def __aenter__(self):
        session = await self._real_context.__aenter__()
        return _SessionProxy(session, self._fail_commit)

    async def __aexit__(self, *args):
        return await self._real_context.__aexit__(*args)


class _CommitFailureFactory:
    def __init__(self, real_factory, failures: int | None):
        self.real_factory = real_factory
        self.failures = failures
        self.calls = 0

    def __call__(self):
        self.calls += 1
        fail = self.failures is None or self.calls <= self.failures
        return _SessionContext(self.real_factory, fail)


class FinalizationRetryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        database_path = Path(self.temp_dir.name) / "finalization.sqlite3"
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.temp_dir.cleanup()

    async def _add_video_job(self, filename: str = "video-job") -> queue.QueueJob:
        async with self.session_factory() as session:
            item = DownloadQueue(
                chat_id="42",
                user_id="42",
                filename=filename,
                tiktok_link=SOURCE_URL,
            )
            session.add(item)
            await session.commit()
            return queue.QueueJob(
                item.id,
                item.chat_id,
                item.user_id,
                item.filename,
                item.tiktok_link,
                item.format_id,
                item.reply_to_message_id,
                item.inline_message_id,
                item.enqueued_at,
            )

    async def _run_video_job(self, job, session_factory):
        identity = queue.MediaIdentity(queue.MediaProvider.TIKTOK, "123")
        inspection = queue.MediaInspection(
            identity,
            SOURCE_URL,
            42,
            "description",
            640,
            360,
            5.0,
            (),
            {"id": "123", "webpage_url": SOURCE_URL},
            "h264-aac",
        )
        root = Path(self.temp_dir.name)
        video_path = root / f"{job.filename}.mp4"
        download = MagicMock()

        def download_once(*_args):
            download()
            video_path.write_bytes(b"video")
            return "123", 640, 360, "description", 5.0

        delivery = AsyncMock(return_value="telegram-file-id")
        self.last_video_download = download
        self.last_video_delivery = delivery
        prepared = queue.PreparedVideo(
            video_path,
            queue.MediaProbe(
                has_video=True,
                has_audio=True,
                video_codec="h264",
                audio_codec="aac",
                width=640,
                height=360,
                duration=5.0,
                pixel_format="yuv420p",
                sample_aspect_ratio="1:1",
            ),
            True,
        )

        with (
            patch.object(queue, "VIDEO_DIR", root),
            patch.object(queue, "SessionLocal", session_factory),
            patch.object(queue, "_find_worker_cached_video", AsyncMock(return_value=None)),
            patch.object(queue, "_load_info_snapshot", return_value=inspection.info),
            patch.object(queue, "_inspection_from_snapshot", return_value=inspection),
            patch.object(queue, "_ydl_download_from_info_file", side_effect=download_once),
            patch.object(queue, "_prepare_telegram_video", return_value=prepared),
            patch.object(
                queue,
                "_split_video",
                return_value=queue.VideoSplitOutcome([video_path], None, 0),
            ),
            patch.object(queue, "_deliver_video", delivery),
            patch.object(
                queue,
                "prepare_music_caption_link",
                AsyncMock(return_value=(None, None)),
            ),
            patch.object(queue, "_remove_download_files"),
            patch.object(queue, "_show_download_error", AsyncMock()),
        ):
            await queue._process_video_job(SimpleNamespace(), job)

        return download, delivery

    async def test_video_retries_only_final_transaction_and_inserts_full_cache(self):
        """Fails if transient finalization re-downloads/re-delivers or loses cache fields."""
        job = await self._add_video_job()
        failing_factory = _CommitFailureFactory(self.session_factory, failures=2)

        with (
            patch.object(queue.asyncio, "sleep", AsyncMock()) as sleep,
            self.assertLogs(level="ERROR"),
        ):
            download, delivery = await self._run_video_job(job, failing_factory)

        self.assertEqual(download.call_count, 1)
        self.assertEqual(delivery.await_count, 1)
        self.assertEqual(failing_factory.calls, 3)
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [0.25, 0.5])

        async with self.session_factory() as session:
            queued = await session.get(DownloadQueue, job.id)
            result = await session.execute(select(VideoCache))
            cached = result.scalar_one()
        self.assertIsNone(queued)
        self.assertEqual(cached.tiktok_id, "h264-aac-v2:tiktok:123")
        self.assertEqual(cached.file_id, "telegram-file-id")
        self.assertIsNone(cached.storage_message_id)
        self.assertEqual(cached.description, "description")
        self.assertIsNone(cached.music_tracks)
        self.assertEqual(cached.source_url, SOURCE_URL)
        self.assertEqual(cached.likes, 42)

    async def test_persistent_video_finalization_pauses_completed_job_until_cancelled(self):
        """Fails if persistent DB failure returns and lets the worker repeat media work."""
        job = await self._add_video_job("persistent-video")
        failing_factory = _CommitFailureFactory(self.session_factory, failures=None)
        sleep = AsyncMock(side_effect=[None, asyncio.CancelledError()])

        with (
            patch.object(queue.asyncio, "sleep", sleep),
            self.assertLogs(level="ERROR"),
        ):
            with self.assertRaises(asyncio.CancelledError):
                download, delivery = await self._run_video_job(job, failing_factory)

        # The completed media work happened once before the short DB retry loop paused.
        self.assertEqual(self.last_video_download.call_count, 1)
        self.assertEqual(self.last_video_delivery.await_count, 1)
        self.assertEqual(failing_factory.calls, 2)
        async with self.session_factory() as session:
            self.assertIsNotNone(await session.get(DownloadQueue, job.id))
            self.assertIsNone((await session.execute(select(VideoCache))).scalar_one_or_none())

    async def test_music_retries_only_final_transaction(self):
        """Fails if transient music finalization repeats recognition or result delivery."""
        token = "m" * 32
        async with self.session_factory() as session:
            request = MusicRequest(
                token=token,
                user_id="42",
                chat_id="42",
                status_message_id=7,
                tiktok_link=SOURCE_URL,
                state="queued",
            )
            item = DownloadQueue(
                chat_id="42",
                user_id="42",
                filename="music-job",
                tiktok_link=SOURCE_URL,
                inline_message_id=f"music:{token}",
            )
            session.add_all((request, item))
            await session.commit()
            job = queue.QueueJob(
                item.id,
                item.chat_id,
                item.user_id,
                item.filename,
                item.tiktok_link,
                item.format_id,
                item.reply_to_message_id,
                item.inline_message_id,
                item.enqueued_at,
            )
        request_snapshot = queue.MusicRequestSnapshot(
            token,
            "42",
            "42",
            7,
            None,
            SOURCE_URL,
            None,
            "queued",
            None,
        )
        recognize = AsyncMock(return_value="00:00 — Artist — Song")
        deliver = AsyncMock()
        failing_factory = _CommitFailureFactory(self.session_factory, failures=2)

        with (
            patch.object(queue, "SessionLocal", failing_factory),
            patch.object(queue, "_load_music_request", AsyncMock(return_value=request_snapshot)),
            patch.object(queue, "_recognize_music_from_source", recognize),
            patch.object(queue, "show_music_request_result", deliver),
            patch.object(queue.asyncio, "sleep", AsyncMock()),
            self.assertLogs(level="ERROR"),
        ):
            await queue._process_music_job(SimpleNamespace(), job, token)

        recognize.assert_awaited_once()
        deliver.assert_awaited_once()
        self.assertEqual(failing_factory.calls, 3)
        async with self.session_factory() as session:
            self.assertIsNone(await session.get(DownloadQueue, job.id))
            stored = await session.get(MusicRequest, token)
        self.assertEqual(stored.state, "done")
        self.assertEqual(stored.music_tracks, "00:00 — Artist — Song")

    async def test_discard_retries_only_short_delete_transaction(self):
        """Fails if a terminal discard failure leaves the scheduler free to rerun the job."""
        job = await self._add_video_job("discard")
        failing_factory = _CommitFailureFactory(self.session_factory, failures=2)

        with (
            patch.object(queue, "SessionLocal", failing_factory),
            patch.object(queue.asyncio, "sleep", AsyncMock()),
            self.assertLogs(level="ERROR"),
        ):
            await queue._discard_job(job.id)

        self.assertEqual(failing_factory.calls, 3)
        async with self.session_factory() as session:
            self.assertIsNone(await session.get(DownloadQueue, job.id))

    async def test_persistent_retry_backoff_is_capped(self):
        """Fails if a persistent DB outage grows the worker sleep without a bound."""
        job = await self._add_video_job("capped-backoff")
        failing_factory = _CommitFailureFactory(self.session_factory, failures=None)
        delays = []

        async def stop_after_cap(delay):
            delays.append(delay)
            if len(delays) == 7:
                raise asyncio.CancelledError

        with (
            patch.object(queue, "SessionLocal", failing_factory),
            patch.object(queue.asyncio, "sleep", side_effect=stop_after_cap),
            self.assertLogs(level="ERROR"),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await queue._discard_job(job.id)

        self.assertEqual(delays, [0.25, 0.5, 1.0, 2.0, 4.0, 5.0, 5.0])

    async def test_session_open_failures_are_retried_before_discard(self):
        """Fails if DB connection/context errors escape and free the worker to rerun media."""
        job = await self._add_video_job("session-open")
        session_factory = MagicMock(
            side_effect=[
                RuntimeError("open failed 1"),
                RuntimeError("open failed 2"),
                self.session_factory(),
            ]
        )

        with (
            patch.object(queue, "SessionLocal", session_factory),
            patch.object(queue.asyncio, "sleep", AsyncMock()),
            self.assertLogs(level="ERROR"),
        ):
            await queue._discard_job(job.id)

        self.assertEqual(session_factory.call_count, 3)
        async with self.session_factory() as session:
            self.assertIsNone(await session.get(DownloadQueue, job.id))


if __name__ == "__main__":
    unittest.main()
