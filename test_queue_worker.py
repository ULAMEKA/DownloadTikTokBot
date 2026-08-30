import tempfile
import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import init_db
import services.queue as queue
from db.base import Base
from db.models import DownloadQueue


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
