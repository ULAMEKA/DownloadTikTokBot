import tempfile
import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import FSInputFile
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import init_db
import services.queue as queue
from db.base import Base
from db.models import DownloadQueue, VideoCache


class QueueWakeupTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self) -> None:
        if hasattr(queue, "_queue_wakeup"):
            queue._queue_wakeup.clear()

    async def test_second_read_closes_enqueue_clear_race(self):
        job = queue.QueueJob(
            1, "1", "1", "job", "https://example", None, None, None, None
        )
        with (
            patch.object(
                queue,
                "_load_next_job",
                AsyncMock(side_effect=[None, job]),
            ) as load,
            patch.object(queue._queue_wakeup, "wait", AsyncMock()) as wait,
        ):
            self.assertEqual(await queue._wait_for_next_job(), job)
        self.assertEqual(load.await_count, 2)
        wait.assert_not_awaited()

    async def test_python_310_asyncio_timeout_reloads_the_queue(self):
        class Python310AsyncioTimeoutError(Exception):
            pass

        job = queue.QueueJob(
            1, "1", "1", "job", "https://example", None, None, None, None
        )

        async def expire(waiter, *, timeout):
            waiter.close()
            raise Python310AsyncioTimeoutError

        with (
            patch.object(
                queue,
                "_load_next_job",
                AsyncMock(side_effect=[None, None, job]),
            ) as load,
            patch.object(
                queue.asyncio,
                "TimeoutError",
                Python310AsyncioTimeoutError,
            ),
            patch.object(queue.asyncio, "wait_for", new=expire),
        ):
            self.assertEqual(await queue._wait_for_next_job(), job)

        self.assertEqual(load.await_count, 3)

    async def test_failed_enqueue_does_not_signal(self):
        result = MagicMock()
        result.scalar_one.return_value = queue.MAX_ACTIVE_DOWNLOADS_PER_USER
        session = AsyncMock()
        session.execute.return_value = result
        queue._queue_wakeup.clear()

        accepted = await queue.enqueue_download(session, MagicMock(), "user-1")

        self.assertFalse(accepted)
        self.assertFalse(queue._queue_wakeup.is_set())
        session.commit.assert_not_awaited()

    async def test_successful_enqueue_signals_only_after_commit(self):
        result = MagicMock()
        result.scalar_one.return_value = 0
        session = MagicMock()
        session.execute = AsyncMock(return_value=result)

        async def commit() -> None:
            self.assertFalse(queue._queue_wakeup.is_set())

        session.commit = AsyncMock(side_effect=commit)
        queue._queue_wakeup.clear()

        accepted = await queue.enqueue_download(session, MagicMock(), "user-1")

        self.assertTrue(accepted)
        self.assertTrue(queue._queue_wakeup.is_set())


class QueueSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        database_path = Path(self.temp_dir.name) / "queue.sqlite3"
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
        if hasattr(queue, "_queue_wakeup"):
            queue._queue_wakeup.clear()

    async def _delete_job(self, job_id: int) -> None:
        async with self.session_factory() as session:
            await session.execute(delete(DownloadQueue).where(DownloadQueue.id == job_id))
            await session.commit()

    async def test_video_jobs_precede_music_and_each_class_is_fifo(self):
        started = datetime(2026, 8, 30, tzinfo=timezone.utc)
        async with self.session_factory() as session:
            session.add_all(
                [
                    DownloadQueue(
                        chat_id="music-old",
                        user_id="1",
                        filename="music-old",
                        tiktok_link="https://example/music-old",
                        inline_message_id="music:old",
                        enqueued_at=started,
                    ),
                    DownloadQueue(
                        chat_id="video-first",
                        user_id="2",
                        filename="video-first",
                        tiktok_link="https://example/video-first",
                        enqueued_at=started + timedelta(seconds=1),
                    ),
                    DownloadQueue(
                        chat_id="video-second",
                        user_id="3",
                        filename="video-second",
                        tiktok_link="https://example/video-second",
                        enqueued_at=started + timedelta(seconds=2),
                    ),
                    DownloadQueue(
                        chat_id="music-new",
                        user_id="4",
                        filename="music-new",
                        tiktok_link="https://example/music-new",
                        inline_message_id="music:new",
                        enqueued_at=started + timedelta(seconds=3),
                    ),
                ]
            )
            await session.commit()

        selected = []
        for _ in range(4):
            job = await queue._load_next_job()
            self.assertIsNotNone(job)
            selected.append(job)
            await self._delete_job(job.id)

        self.assertEqual(
            [job.filename for job in selected],
            ["video-first", "video-second", "music-old", "music-new"],
        )
        self.assertEqual([job.is_music for job in selected], [False, False, True, True])

    async def test_loaded_job_is_an_immutable_detached_snapshot(self):
        enqueued_at = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
        async with self.session_factory() as session:
            session.add(
                DownloadQueue(
                    chat_id="10",
                    user_id="20",
                    filename="video",
                    tiktok_link="https://example/video",
                    enqueued_at=enqueued_at,
                )
            )
            await session.commit()

        job = await queue._load_next_job()

        self.assertEqual(job.chat_id, "10")
        self.assertEqual(job.enqueued_at.replace(tzinfo=timezone.utc), enqueued_at)
        with self.assertRaises(FrozenInstanceError):
            job.filename = "changed"

    async def test_new_queue_row_gets_enqueue_timestamp(self):
        async with self.session_factory() as session:
            item = DownloadQueue(
                chat_id="10",
                filename="video",
                tiktok_link="https://example/video",
            )
            session.add(item)
            await session.commit()

        self.assertIsNotNone(item.enqueued_at)


class JobStageMetricsTests(unittest.TestCase):
    def test_stage_metrics_use_monotonic_deltas(self):
        with patch("services.queue.time.monotonic", side_effect=[10.0, 10.2, 10.5]):
            metrics = queue.JobStageMetrics(job_id=7)
            with metrics.stage("download"):
                pass

        self.assertEqual(metrics.durations_ms["download"], 300)

    def test_queue_wait_handles_nullable_naive_and_aware_datetimes(self):
        now = datetime(2026, 8, 30, 12, 0, 1, tzinfo=timezone.utc)

        self.assertIsNone(queue._queue_wait_ms(None, now=now))
        self.assertEqual(
            queue._queue_wait_ms(datetime(2026, 8, 30, 12, 0, 0), now=now),
            1000,
        )
        self.assertEqual(
            queue._queue_wait_ms(
                datetime(2026, 8, 30, 12, 0, 0, tzinfo=timezone.utc),
                now=now,
            ),
            1000,
        )
        self.assertEqual(
            queue._queue_wait_ms(
                datetime(2026, 8, 30, 15, 0, 0, tzinfo=timezone(timedelta(hours=3))),
                now=now,
            ),
            1000,
        )

    def test_summary_is_bounded_numeric_and_redacts_sensitive_metadata(self):
        metrics = queue.JobStageMetrics(job_id=9, started_at=100.0)
        metrics.durations_ms.update(
            {
                "inspect": 11,
                "download": 22,
                "prepare": 33,
                "storage": 44,
                "delivery": 55,
            }
        )
        metrics.source_url = "https://example.test/video?secret_query=1"
        metrics.cookies = "session-cookie"
        metrics.headers = {"Authorization": "Bearer header-secret"}

        with (
            patch("services.queue.time.monotonic", return_value=100.9),
            self.assertLogs(level="INFO") as captured,
        ):
            metrics.log_summary(
                cache_status="miss secret_query=1",
                ffmpeg_bypassed=True,
                queue_wait_ms=125,
            )

        self.assertEqual(len(captured.output), 1)
        summary = captured.output[0]
        self.assertIn("job_id=9", summary)
        self.assertIn("cache_status=unknown", summary)
        self.assertIn("ffmpeg_bypassed=true", summary)
        for field in (
            "queue_wait",
            "inspect",
            "download",
            "prepare",
            "storage",
            "delivery",
            "total",
        ):
            self.assertRegex(summary, rf"\b{field}_ms=\d+\b")
        for sensitive in (
            "secret_query",
            "session-cookie",
            "Authorization",
            "header-secret",
        ):
            self.assertNotIn(sensitive, summary)

    def test_summary_omits_queue_wait_for_legacy_rows(self):
        metrics = queue.JobStageMetrics(job_id=10, started_at=100.0)

        with (
            patch("services.queue.time.monotonic", return_value=100.1),
            self.assertLogs(level="INFO") as captured,
        ):
            metrics.log_summary(
                cache_status="miss",
                ffmpeg_bypassed=None,
                queue_wait_ms=None,
            )

        self.assertNotIn("queue_wait_ms=", captured.output[0])


class JobProcessorMetricsTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_video_job_emits_one_terminal_summary(self):
        metrics = queue.JobStageMetrics(job_id=99)
        job = queue.QueueJob(99, None, None, None, None, None, None, None, None)

        with (
            patch.object(queue, "JobStageMetrics", return_value=metrics),
            patch.object(queue, "_discard_job", AsyncMock()),
            patch.object(
                metrics,
                "log_summary",
                wraps=metrics.log_summary,
            ) as log_summary,
        ):
            await queue._process_video_job(MagicMock(), job)

        log_summary.assert_called_once_with(
            cache_status="miss",
            ffmpeg_bypassed=None,
            queue_wait_ms=None,
        )


class ShortSessionTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _job() -> queue.QueueJob:
        return queue.QueueJob(
            1,
            "1",
            "1",
            "job",
            "https://www.tiktok.com/@a/video/123",
            None,
            None,
            None,
            None,
        )

    @staticmethod
    def _inspection(job: queue.QueueJob) -> queue.MediaInspection:
        return queue.MediaInspection(
            "123",
            job.tiktok_link,
            9,
            "desc",
            720,
            1280,
            10.0,
            (),
            {"id": "123", "webpage_url": job.tiktok_link},
        )

    async def test_download_starts_after_load_session_exits(self):
        job = self._job()
        inspection = self._inspection(job)
        session_active = False
        session = SimpleNamespace()
        test_case = self

        class SessionContext:
            async def __aenter__(self):
                nonlocal session_active
                test_case.assertFalse(session_active)
                session_active = True
                return session

            async def __aexit__(self, *_args):
                nonlocal session_active
                session_active = False

        def download(*_args):
            self.assertFalse(session_active)
            raise queue.MediaPreparationError("stop")

        with (
            patch.object(queue, "SessionLocal", side_effect=SessionContext),
            patch.object(queue, "find_cached_video", AsyncMock(return_value=None)),
            patch.object(
                queue,
                "find_cached_video_by_id",
                AsyncMock(return_value=None),
            ),
            patch.object(queue, "_load_info_snapshot", return_value=inspection.info),
            patch.object(queue, "_inspection_from_snapshot", return_value=inspection),
            patch.object(
                queue,
                "_ydl_download_from_info_file",
                side_effect=download,
            ) as snapshot_download,
            patch.object(
                queue,
                "_download_media",
                side_effect=download,
            ) as url_download,
            patch.object(queue, "_discard_job", AsyncMock()),
            patch.object(queue, "_show_download_error", AsyncMock()),
            patch.object(queue, "_remove_download_files"),
            self.assertLogs(level="ERROR"),
        ):
            await queue._process_video_job(MagicMock(), job)

        self.assertFalse(session_active)
        snapshot_download.assert_called_once()
        url_download.assert_called_once()

    async def test_fresh_snapshot_skips_resolve_and_second_extract(self):
        job = self._job()
        inspection = self._inspection(job)
        session_context = AsyncMock()
        session_context.__aenter__.return_value = SimpleNamespace()

        with tempfile.TemporaryDirectory() as directory:
            video_path = Path(directory) / "job.mp4"
            snapshot_path = Path(directory) / "job.info.json"

            def download(*_args):
                video_path.write_bytes(b"video")
                return "123", 720, 1280, "desc", 10.0

            with (
                patch.object(queue, "VIDEO_DIR", Path(directory)),
                patch.object(queue, "SessionLocal", return_value=session_context),
                patch.object(queue, "find_cached_video", AsyncMock(return_value=None)),
                patch.object(
                    queue,
                    "find_cached_video_by_id",
                    AsyncMock(return_value=None),
                ),
                patch.object(queue, "_load_info_snapshot", return_value=inspection.info),
                patch.object(queue, "_inspection_from_snapshot", return_value=inspection),
                patch.object(queue, "inspect_media") as inspect,
                patch.object(queue, "write_info_snapshot") as write_snapshot,
                patch.object(
                    queue,
                    "_ydl_download_from_info_file",
                    side_effect=download,
                ) as download_from_snapshot,
                patch.object(queue, "_download_media") as fallback_download,
                patch.object(
                    queue,
                    "_prepare_telegram_video",
                    side_effect=queue.MediaPreparationError("stop after download"),
                ),
                patch.object(queue, "_resolve_media") as resolve,
                patch.object(queue, "_discard_job", AsyncMock()),
                patch.object(queue, "_show_download_error", AsyncMock()),
                patch.object(queue, "_remove_download_files"),
                self.assertLogs(level="ERROR"),
            ):
                await queue._process_video_job(MagicMock(), job)

        inspect.assert_not_called()
        write_snapshot.assert_not_called()
        download_from_snapshot.assert_called_once_with(
            job.tiktok_link,
            snapshot_path,
            str(Path(directory) / "job.%(ext)s"),
            None,
        )
        fallback_download.assert_not_called()
        resolve.assert_not_called()

    async def test_music_username_lookup_precedes_its_short_insert_session(self):
        session_opened = False
        session_exited = False
        session = SimpleNamespace(add=MagicMock(), commit=AsyncMock())
        test_case = self

        class BotStub:
            async def get_me(self):
                test_case.assertFalse(session_opened)
                return SimpleNamespace(username="download_bot")

        class SessionContext:
            async def __aenter__(self):
                nonlocal session_opened
                session_opened = True
                return session

            async def __aexit__(self, *_args):
                nonlocal session_exited
                session_exited = True

        with (
            patch.object(queue, "SessionLocal", side_effect=SessionContext),
            patch.object(queue, "_music_bot_username_cache", None),
            patch.object(queue.os, "getenv", return_value=""),
        ):
            token, link = await queue.prepare_music_caption_link(
                BotStub(),
                "https://www.tiktok.com/@a/video/123",
                None,
            )

        self.assertTrue(session_exited)
        self.assertIsNotNone(token)
        self.assertEqual(link, f"https://t.me/download_bot?start=music_{token}")
        session.add.assert_called_once()
        session.commit.assert_awaited_once()

    async def test_missing_snapshot_inspects_once_and_downloads_from_written_snapshot(self):
        job = self._job()
        inspection = self._inspection(job)
        session = SimpleNamespace()
        session_context = AsyncMock()
        session_context.__aenter__.return_value = session

        with tempfile.TemporaryDirectory() as directory:
            video_path = Path(directory) / "job.mp4"
            snapshot_path = Path(directory) / "job.info.json"

            def download(*_args):
                video_path.write_bytes(b"video")
                return "123", 720, 1280, "desc", 10.0

            cache_by_id = AsyncMock(return_value=None)
            with (
                patch.object(queue, "VIDEO_DIR", Path(directory)),
                patch.object(queue, "SessionLocal", return_value=session_context),
                patch.object(queue, "find_cached_video", AsyncMock(return_value=None)),
                patch.object(queue, "find_cached_video_by_id", cache_by_id),
                patch.object(queue, "_load_info_snapshot", return_value=None),
                patch.object(queue, "inspect_media", return_value=inspection) as inspect,
                patch.object(
                    queue,
                    "write_info_snapshot",
                    return_value=snapshot_path,
                ) as write_snapshot,
                patch.object(
                    queue,
                    "_ydl_download_from_info_file",
                    side_effect=download,
                ) as download_from_snapshot,
                patch.object(queue, "_download_media") as fallback_download,
                patch.object(
                    queue,
                    "_prepare_telegram_video",
                    side_effect=queue.MediaPreparationError("stop after download"),
                ),
                patch.object(queue, "_resolve_media") as resolve,
                patch.object(queue, "_discard_job", AsyncMock()),
                patch.object(queue, "_show_download_error", AsyncMock()),
                patch.object(queue, "_remove_download_files"),
                self.assertLogs(level="ERROR"),
            ):
                await queue._process_video_job(MagicMock(), job)

        inspect.assert_called_once_with(job.tiktok_link)
        write_snapshot.assert_called_once_with(job.filename, inspection.info)
        cache_by_id.assert_awaited_once_with(
            session,
            inspection.media_id,
            inspection.source_url,
        )
        download_from_snapshot.assert_called_once_with(
            job.tiktok_link,
            snapshot_path,
            str(Path(directory) / "job.%(ext)s"),
            None,
        )
        fallback_download.assert_not_called()
        resolve.assert_not_called()


class DeliveryRoutingTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _job(
        inline_message_id: str | None = None,
        enqueued_at: datetime | None = None,
    ) -> queue.QueueJob:
        return queue.QueueJob(
            1,
            "-100200",
            "42",
            "delivery-job",
            "https://www.tiktok.com/@a/video/123",
            None,
            None,
            inline_message_id,
            enqueued_at,
        )

    @staticmethod
    def _probe() -> queue.MediaProbe:
        return queue.MediaProbe(
            has_video=True,
            has_audio=True,
            video_codec="h264",
            audio_codec="aac",
            width=640,
            height=360,
            duration=5.0,
            pixel_format="yuv420p",
            sample_aspect_ratio="1:1",
        )

    async def _run_single_delivery(
        self,
        inline_message_id: str | None,
        *,
        enqueued_at: datetime | None = None,
        on_download=None,
    ):
        job = self._job(inline_message_id, enqueued_at)
        probe = self._probe()
        inspection = queue.MediaInspection(
            "123",
            job.tiktok_link,
            9,
            "description",
            640,
            360,
            5.0,
            (),
            {"id": "123", "webpage_url": job.tiktok_link},
        )
        bot = SimpleNamespace(
            send_video=AsyncMock(
                return_value=SimpleNamespace(
                    video=SimpleNamespace(file_id="direct-file-id"),
                    message_id=55,
                )
            ),
            edit_message_media=AsyncMock(
                return_value=SimpleNamespace(video=SimpleNamespace(file_id="unused"))
            ),
            edit_message_caption=AsyncMock(),
            edit_message_text=AsyncMock(),
            send_message=AsyncMock(),
        )
        upload_to_storage = AsyncMock(return_value=("storage-file-id", 77))
        finish = AsyncMock()
        discard = AsyncMock()
        show_error = AsyncMock()

        with tempfile.TemporaryDirectory() as directory:
            video_path = Path(directory) / f"{job.filename}.mp4"

            def download(*_args):
                if on_download is not None:
                    on_download()
                video_path.write_bytes(b"video")
                return "123", 640, 360, "description", 5.0

            prepared = queue.PreparedVideo(
                path=video_path,
                probe=probe,
                bypassed_ffmpeg=True,
            )
            with (
                patch.object(queue, "VIDEO_DIR", Path(directory)),
                patch.object(queue, "STORAGE_CHAT_ID", "-100999"),
                patch.object(queue, "_find_worker_cached_video", AsyncMock(return_value=None)),
                patch.object(queue, "_load_info_snapshot", return_value=inspection.info),
                patch.object(queue, "_inspection_from_snapshot", return_value=inspection),
                patch.object(queue, "_ydl_download_from_info_file", side_effect=download),
                patch.object(queue, "_prepare_telegram_video", return_value=prepared),
                patch.object(queue, "_split_video", return_value=([video_path], None)),
                patch.object(
                    queue,
                    "_upload_to_storage",
                    upload_to_storage,
                    create=True,
                ),
                patch.object(
                    queue,
                    "prepare_music_caption_link",
                    AsyncMock(return_value=(None, None)),
                ),
                patch.object(queue, "_finish_video_job", finish),
                patch.object(queue, "_discard_job", discard),
                patch.object(queue, "_show_download_error", show_error),
                patch.object(queue, "_remove_download_files"),
                patch.object(queue.asyncio, "sleep", AsyncMock()),
            ):
                await queue._process_video_job(bot, job)

            sent_video = (
                bot.send_video.await_args.kwargs["video"]
                if bot.send_video.await_count
                else None
            )

        return SimpleNamespace(
            bot=bot,
            upload_to_storage=upload_to_storage,
            finish=finish,
            discard=discard,
            show_error=show_error,
            sent_video=sent_video,
        )

    async def test_private_and_group_misses_deliver_local_file_without_storage(self):
        for inline_message_id in (None, "direct:no_description"):
            with self.subTest(inline_message_id=inline_message_id):
                result = await self._run_single_delivery(inline_message_id)

                result.upload_to_storage.assert_not_awaited()
                self.assertEqual(result.bot.send_video.await_count, 1)
                self.assertIsInstance(result.sent_video, FSInputFile)
                result.bot.edit_message_media.assert_not_awaited()
                cache_update = result.finish.await_args.args[1]
                self.assertEqual(cache_update.file_id, "direct-file-id")
                self.assertIsNone(cache_update.storage_message_id)
                result.discard.assert_not_awaited()
                result.show_error.assert_not_awaited()

    async def test_inline_miss_uploads_to_storage_once_and_edits_by_file_id(self):
        result = await self._run_single_delivery("inline-message-id")

        result.upload_to_storage.assert_awaited_once()
        result.bot.send_video.assert_not_awaited()
        self.assertEqual(
            result.bot.edit_message_media.await_args.kwargs["media"].media,
            "storage-file-id",
        )
        cache_update = result.finish.await_args.args[1]
        self.assertEqual(cache_update.file_id, "storage-file-id")
        self.assertEqual(cache_update.storage_message_id, 77)
        result.discard.assert_not_awaited()
        result.show_error.assert_not_awaited()

    async def test_video_success_records_external_stages_and_one_summary(self):
        metrics = queue.JobStageMetrics(job_id=1)

        with (
            patch.object(queue, "JobStageMetrics", return_value=metrics),
            patch.object(
                metrics,
                "log_summary",
                wraps=metrics.log_summary,
            ) as log_summary,
        ):
            await self._run_single_delivery("inline-message-id")

        self.assertTrue(
            {"cache", "inspect", "download", "prepare", "storage", "delivery"}
            <= metrics.durations_ms.keys()
        )
        log_summary.assert_called_once_with(
            cache_status="miss",
            ffmpeg_bypassed=True,
            queue_wait_ms=None,
        )

    async def test_queue_wait_is_snapshotted_before_video_processing(self):
        enqueued_at = datetime(2026, 8, 30, 12, 0, 0, tzinfo=timezone.utc)
        clock = SimpleNamespace(current=enqueued_at + timedelta(seconds=1))

        class FakeDateTime:
            @classmethod
            def now(cls, _tz=None):
                return clock.current

        def advance_clock():
            clock.current = enqueued_at + timedelta(seconds=6)

        with (
            patch.object(queue, "datetime", FakeDateTime),
            self.assertLogs(level="INFO") as captured,
        ):
            await self._run_single_delivery(
                "inline-message-id",
                enqueued_at=enqueued_at,
                on_download=advance_clock,
            )

        summary = next(line for line in captured.output if "job_stage_summary" in line)
        self.assertIn("queue_wait_ms=1000", summary)
        self.assertNotIn("queue_wait_ms=6000", summary)


class ForcedNormalizationRetryTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _job(inline_message_id: str | None = None) -> queue.QueueJob:
        return queue.QueueJob(
            1,
            "42",
            "42",
            "retry-job",
            "https://www.tiktok.com/@a/video/123",
            None,
            None,
            inline_message_id,
            None,
        )

    @staticmethod
    def _probe() -> queue.MediaProbe:
        return queue.MediaProbe(
            has_video=True,
            has_audio=True,
            video_codec="h264",
            audio_codec="aac",
            width=640,
            height=360,
            duration=5.0,
            pixel_format="yuv420p",
            sample_aspect_ratio="1:1",
        )

    async def _run_failed_local_delivery(
        self,
        error: Exception,
        *,
        bypassed_ffmpeg: bool,
        inline_message_id: str | None = None,
        split_after_normalize: bool = False,
    ):
        job = self._job(inline_message_id)
        probe = self._probe()
        inspection = queue.MediaInspection(
            "123",
            job.tiktok_link,
            None,
            None,
            640,
            360,
            5.0,
            (),
            {"id": "123", "webpage_url": job.tiktok_link},
        )
        prepare_forces: list[bool] = []
        split_paths: list[Path] = []
        deliver = AsyncMock(side_effect=[error, "part-one", "part-two"])
        upload_to_storage = AsyncMock(return_value=("storage-file-id", 77))
        finish = AsyncMock()
        discard = AsyncMock()
        show_error = AsyncMock()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / f"{job.filename}.mp4"
            normalized = root / f"{job.filename}.normalized.mp4"
            part_dir = root / "normalized-parts"

            def download(*_args):
                source.write_bytes(b"source")
                return "123", 640, 360, None, 5.0

            def prepare(path: Path, *, force_normalize: bool = False):
                prepare_forces.append(force_normalize)
                if force_normalize:
                    normalized.write_bytes(b"normalized")
                    return queue.PreparedVideo(
                        path=normalized,
                        probe=probe,
                        bypassed_ffmpeg=False,
                    )
                return queue.PreparedVideo(
                    path=path,
                    probe=probe,
                    bypassed_ffmpeg=bypassed_ffmpeg,
                )

            def split(path: Path, _duration: float | None):
                split_paths.append(path)
                if path == normalized and split_after_normalize:
                    part_dir.mkdir()
                    first = part_dir / "part_000.mp4"
                    second = part_dir / "part_001.mp4"
                    first.write_bytes(b"one")
                    second.write_bytes(b"two")
                    return [first, second], part_dir
                return [path], None

            bot = SimpleNamespace(send_video=AsyncMock())
            with (
                patch.object(queue, "VIDEO_DIR", root),
                patch.object(queue, "STORAGE_CHAT_ID", None),
                patch.object(queue, "_find_worker_cached_video", AsyncMock(return_value=None)),
                patch.object(queue, "_load_info_snapshot", return_value=inspection.info),
                patch.object(queue, "_inspection_from_snapshot", return_value=inspection),
                patch.object(queue, "_ydl_download_from_info_file", side_effect=download),
                patch.object(queue, "_prepare_telegram_video", side_effect=prepare),
                patch.object(queue, "_split_video", side_effect=split),
                patch.object(queue, "_deliver_video", deliver),
                patch.object(
                    queue,
                    "_upload_to_storage",
                    upload_to_storage,
                    create=True,
                ),
                patch.object(
                    queue,
                    "prepare_music_caption_link",
                    AsyncMock(return_value=(None, None)),
                ),
                patch.object(queue, "_finish_video_job", finish),
                patch.object(queue, "_discard_job", discard),
                patch.object(queue, "_show_download_error", show_error),
                patch.object(queue, "_remove_download_files"),
                patch.object(queue.asyncio, "sleep", AsyncMock()),
                self.assertLogs(level="WARNING"),
            ):
                await queue._process_video_job(bot, job)

        return SimpleNamespace(
            prepare_forces=prepare_forces,
            split_paths=split_paths,
            source=source,
            normalized=normalized,
            deliver=deliver,
            upload_to_storage=upload_to_storage,
            finish=finish,
            discard=discard,
            show_error=show_error,
        )

    async def test_first_bypassed_local_bad_request_forces_one_normalize_and_resplit(self):
        result = await self._run_failed_local_delivery(
            TelegramBadRequest(MagicMock(), "Bad Request: wrong file format"),
            bypassed_ffmpeg=True,
            split_after_normalize=True,
        )

        self.assertEqual(result.prepare_forces, [False, True])
        self.assertEqual(result.split_paths, [result.source, result.normalized])
        self.assertEqual(result.deliver.await_count, 3)
        result.upload_to_storage.assert_not_awaited()
        result.finish.assert_awaited_once_with(1, None)
        result.discard.assert_not_awaited()
        result.show_error.assert_not_awaited()

    async def test_nonlocal_or_already_normalized_errors_do_not_force_normalization(self):
        cases = (
            (
                "already-normalized",
                TelegramBadRequest(MagicMock(), "Bad Request: wrong file format"),
                False,
                None,
            ),
            (
                "forbidden",
                TelegramForbiddenError(MagicMock(), "Forbidden"),
                True,
                None,
            ),
            (
                "rate-limit",
                TelegramRetryAfter(MagicMock(), "Retry later", 1),
                True,
                None,
            ),
            (
                "file-id-delivery",
                TelegramBadRequest(MagicMock(), "Bad Request: message is not modified"),
                True,
                "inline-message-id",
            ),
        )

        for name, error, bypassed, inline_message_id in cases:
            with self.subTest(name=name):
                result = await self._run_failed_local_delivery(
                    error,
                    bypassed_ffmpeg=bypassed,
                    inline_message_id=inline_message_id,
                )

                self.assertEqual(result.prepare_forces, [False])
                self.assertEqual(result.split_paths, [result.source])
                self.assertEqual(result.deliver.await_count, 1)

    async def test_bad_request_after_a_successful_part_does_not_force_normalization(self):
        job = self._job()
        probe = self._probe()
        inspection = queue.MediaInspection(
            "123",
            job.tiktok_link,
            None,
            None,
            640,
            360,
            5.0,
            (),
            {"id": "123", "webpage_url": job.tiktok_link},
        )
        prepare_forces: list[bool] = []
        deliver = AsyncMock(
            side_effect=[
                "first-part-file-id",
                TelegramBadRequest(MagicMock(), "Bad Request: wrong file format"),
            ]
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / f"{job.filename}.mp4"
            part_dir = root / "parts"
            part_dir.mkdir()
            parts = [part_dir / "part_000.mp4", part_dir / "part_001.mp4"]
            for part in parts:
                part.write_bytes(b"part")

            def download(*_args):
                source.write_bytes(b"source")
                return "123", 640, 360, None, 5.0

            def prepare(path: Path, *, force_normalize: bool = False):
                prepare_forces.append(force_normalize)
                return queue.PreparedVideo(path=path, probe=probe, bypassed_ffmpeg=True)

            with (
                patch.object(queue, "VIDEO_DIR", root),
                patch.object(queue, "STORAGE_CHAT_ID", None),
                patch.object(queue, "_find_worker_cached_video", AsyncMock(return_value=None)),
                patch.object(queue, "_load_info_snapshot", return_value=inspection.info),
                patch.object(queue, "_inspection_from_snapshot", return_value=inspection),
                patch.object(queue, "_ydl_download_from_info_file", side_effect=download),
                patch.object(queue, "_prepare_telegram_video", side_effect=prepare),
                patch.object(queue, "_split_video", return_value=(parts, part_dir)),
                patch.object(queue, "_deliver_video", deliver),
                patch.object(
                    queue,
                    "prepare_music_caption_link",
                    AsyncMock(return_value=(None, None)),
                ),
                patch.object(queue, "_discard_job", AsyncMock()),
                patch.object(queue, "_show_download_error", AsyncMock()),
                patch.object(queue, "_remove_download_files"),
                self.assertLogs(level="WARNING"),
            ):
                await queue._process_video_job(SimpleNamespace(), job)

        self.assertEqual(prepare_forces, [False])
        self.assertEqual(deliver.await_count, 2)


class VideoJobFinalizationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        database_path = Path(self.temp_dir.name) / "finish.sqlite3"
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

    async def _add_job(self, filename: str = "job") -> int:
        async with self.session_factory() as session:
            item = DownloadQueue(
                chat_id="1",
                user_id="1",
                filename=filename,
                tiktok_link="https://www.tiktok.com/@a/video/123",
            )
            session.add(item)
            await session.commit()
            return item.id

    async def test_finish_upserts_complete_cache_snapshot_and_deletes_job(self):
        job_id = await self._add_job()
        async with self.session_factory() as session:
            session.add(
                VideoCache(
                    tiktok_id="h264-aac-v2:123",
                    file_id="old-file",
                    storage_message_id=1,
                    description="old",
                    source_url="https://old.example",
                    likes=1,
                )
            )
            await session.commit()

        cache_update = queue.VideoCacheUpdate(
            cache_key="h264-aac-v2:123",
            file_id="new-file",
            storage_message_id=9,
            description="desc",
            source_url="https://www.tiktok.com/@a/video/123",
            likes=42,
        )
        with self.assertRaises(FrozenInstanceError):
            cache_update.likes = 0

        await queue._finish_video_job(job_id, cache_update)

        async with self.session_factory() as session:
            item = await session.get(DownloadQueue, job_id)
            result = await session.execute(
                select(VideoCache).where(VideoCache.tiktok_id == cache_update.cache_key)
            )
            cached = result.scalar_one()

        self.assertIsNone(item)
        self.assertEqual(cached.file_id, "new-file")
        self.assertEqual(cached.storage_message_id, 9)
        self.assertEqual(cached.description, "desc")
        self.assertEqual(cached.source_url, cache_update.source_url)
        self.assertEqual(cached.likes, 42)

    async def test_finish_commit_failure_rolls_back_and_leaves_job_recoverable(self):
        job_id = await self._add_job("recoverable")
        worker_session = self.session_factory()
        cache_update = queue.VideoCacheUpdate(
            cache_key="h264-aac-v2:123",
            file_id="file",
            storage_message_id=None,
            description=None,
            source_url="https://www.tiktok.com/@a/video/123",
            likes=None,
        )

        with (
            patch.object(queue, "SessionLocal", return_value=worker_session),
            patch.object(
                worker_session,
                "commit",
                AsyncMock(side_effect=RuntimeError("commit failed")),
            ),
            self.assertLogs(level="ERROR"),
        ):
            await queue._finish_video_job(job_id, cache_update)

        async with self.session_factory() as session:
            item = await session.get(DownloadQueue, job_id)
            result = await session.execute(select(VideoCache))

        self.assertIsNotNone(item)
        self.assertIsNone(result.scalar_one_or_none())

    async def test_discard_deletes_only_the_selected_job(self):
        discarded_id = await self._add_job("discarded")
        kept_id = await self._add_job("kept")

        await queue._discard_job(discarded_id)

        async with self.session_factory() as session:
            self.assertIsNone(await session.get(DownloadQueue, discarded_id))
            self.assertIsNotNone(await session.get(DownloadQueue, kept_id))


class QueueTimestampMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_queue_gains_enqueued_at_idempotently(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "legacy.sqlite3"
            engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
            try:
                async with engine.begin() as connection:
                    await connection.execute(
                        text(
                            'CREATE TABLE "BotDownloadQueue" ('
                            "id INTEGER PRIMARY KEY, chat_id TEXT, filename TEXT, "
                            "tiktok_link TEXT)"
                        )
                    )

                with patch.object(init_db, "engine", engine):
                    await init_db.init_db()
                    await init_db.init_db()

                async with engine.connect() as connection:
                    rows = await connection.execute(
                        text('PRAGMA table_info("BotDownloadQueue")')
                    )
                    column_names = [row[1] for row in rows]

                self.assertEqual(column_names.count("enqueued_at"), 1)
            finally:
                await engine.dispose()


if __name__ == "__main__":
    unittest.main()
