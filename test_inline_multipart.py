import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import services.queue as queue


class InlineMultipartWorkerTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _inspection_and_job():
        source_url = "https://www.instagram.com/reel/AbC_123/"
        identity = queue.MediaIdentity(queue.MediaProvider.INSTAGRAM, "AbC_123")
        inspection = queue.MediaInspection(
            identity=identity,
            source_url=source_url,
            likes=7,
            description="desc",
            width=720,
            height=1280,
            duration=100.0,
            qualities=(),
            info={"id": "AbC_123", "webpage_url": source_url},
            fast_format_id="h264-aac",
        )
        job = queue.QueueJob(50, "42", "42", "large", source_url, None, None, "inline-id", None)
        return source_url, inspection, job

    def test_inline_large_caption_reserves_telegram_caption_budget(self):
        caption = queue._inline_large_video_caption(
            description="x" * 5000,
            source_url="https://www.instagram.com/reel/AbC_123/",
            likes=7,
            has_spoiler=False,
            without_description=False,
        )

        self.assertIsNotNone(caption)
        self.assertLessEqual(len(caption), queue.CAPTION_LIMIT)
        self.assertIn("включая эту часть", caption)

    async def test_large_inline_uploads_all_parts_persists_cache_before_publishing_link(self):
        source_url = "https://www.instagram.com/reel/AbC_123/"
        identity = queue.MediaIdentity(queue.MediaProvider.INSTAGRAM, "AbC_123")
        inspection = queue.MediaInspection(
            identity=identity,
            source_url=source_url,
            likes=7,
            description="desc",
            width=720,
            height=1280,
            duration=100.0,
            qualities=(),
            info={"id": "AbC_123", "webpage_url": source_url},
            fast_format_id="h264-aac",
        )
        job = queue.QueueJob(50, "42", "42", "large", source_url, None, None, "inline-id", None)
        events: list[str] = []

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video_path = root / "large.mp4"
            video_path.write_bytes(b"source")
            part_one = root / "part-1.mp4"
            part_two = root / "part-2.mp4"
            part_one.write_bytes(b"part one")
            part_two.write_bytes(b"part two")
            prepared = queue.PreparedVideo(
                video_path,
                queue.MediaProbe(
                    has_video=True,
                    has_audio=True,
                    video_codec="h264",
                    audio_codec="aac",
                    width=720,
                    height=1280,
                    duration=100.0,
                ),
                True,
            )

            async def upload(_bot, path, _width, _height):
                events.append(f"upload:{path.name}")
                return (f"file-{path.stem}", 100 + len(events))

            async def finish(_job_id, update):
                events.append("persist")
                self.assertEqual(update.file_ids, ("file-part-1", "file-part-2"))

            bot = SimpleNamespace(
                get_me=AsyncMock(return_value=SimpleNamespace(username="download_bot")),
                edit_message_media=AsyncMock(side_effect=lambda **_kwargs: events.append("publish")),
            )

            with (
                patch.object(queue, "VIDEO_DIR", root),
                patch.object(queue, "_find_worker_cached_video", AsyncMock(return_value=None)),
                patch.object(queue, "_load_info_snapshot", return_value=inspection.info),
                patch.object(queue, "_inspection_from_snapshot", return_value=inspection),
                patch.object(queue, "_ydl_download_from_info_file", return_value=("AbC_123", 720, 1280, "desc", 100.0)),
                patch.object(queue, "_prepare_telegram_video", return_value=prepared),
                patch.object(
                    queue,
                    "_split_video",
                    return_value=queue.VideoSplitOutcome([part_one, part_two], root / "parts", 1),
                ),
                patch.object(queue, "_upload_to_storage", side_effect=upload) as upload_mock,
                patch.object(queue, "_finish_video_job", side_effect=finish) as finish_mock,
                patch.object(queue, "_remove_download_files"),
                patch.object(queue, "_deliver_video", AsyncMock()) as deliver,
            ):
                await queue._process_video_job(bot, job)

        self.assertEqual(upload_mock.await_count, 2)
        self.assertEqual(events[-1], "publish")
        self.assertLess(events.index("persist"), events.index("publish"))
        finish_mock.assert_awaited_once()
        deliver.assert_not_awaited()
        media = bot.edit_message_media.await_args.kwargs["media"]
        self.assertEqual(media.media, "file-part-1")
        markup = bot.edit_message_media.await_args.kwargs["reply_markup"]
        self.assertIn("video_instagram_AbC_123", markup.inline_keyboard[0][0].url)
        self.assertIn("большим видео", media.caption)

    async def test_local_split_parts_use_album_batches_and_single_tail_video(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parts = []
            for number in range(1, 12):
                path = root / f"part-{number}.mp4"
                path.write_bytes(b"part")
                parts.append(path)
            bot = SimpleNamespace(
                send_media_group=AsyncMock(return_value=[]),
                send_video=AsyncMock(return_value=SimpleNamespace(video=None)),
            )

            await queue._deliver_video_parts(
                bot,
                chat_id="42",
                parts=parts,
                width=720,
                height=1280,
                description="desc",
                source_url="https://www.instagram.com/reel/AbC_123/",
                likes=7,
                reply_to_message_id=55,
                placeholder_message_id=None,
                has_spoiler=False,
                without_description=False,
                music_deep_link=None,
            )

        self.assertEqual(len(bot.send_media_group.await_args.kwargs["media"]), 10)
        self.assertIsInstance(bot.send_video.await_args.kwargs["video"], queue.FSInputFile)

    async def test_large_inline_storage_failure_never_publishes_success_link(self):
        source_url, inspection, job = self._inspection_and_job()
        prepared = None
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video_path = root / "large.mp4"
            video_path.write_bytes(b"source")
            part_one = root / "part-1.mp4"
            part_two = root / "part-2.mp4"
            part_one.write_bytes(b"part one")
            part_two.write_bytes(b"part two")
            prepared = queue.PreparedVideo(
                video_path,
                queue.MediaProbe(
                    has_video=True,
                    has_audio=True,
                    video_codec="h264",
                    audio_codec="aac",
                    width=720,
                    height=1280,
                    duration=100.0,
                ),
                True,
            )
            bot = SimpleNamespace(
                get_me=AsyncMock(return_value=SimpleNamespace(username="download_bot")),
                edit_message_text=AsyncMock(),
                delete_message=AsyncMock(),
            )
            show_error = AsyncMock()
            finish = AsyncMock()

            with (
                patch.object(queue, "VIDEO_DIR", root),
                patch.object(queue, "_find_worker_cached_video", AsyncMock(return_value=None)),
                patch.object(queue, "_load_info_snapshot", return_value=inspection.info),
                patch.object(queue, "_inspection_from_snapshot", return_value=inspection),
                patch.object(queue, "_ydl_download_from_info_file", return_value=("AbC_123", 720, 1280, "desc", 100.0)),
                patch.object(queue, "_prepare_telegram_video", return_value=prepared),
                patch.object(
                    queue,
                    "_split_video",
                    return_value=queue.VideoSplitOutcome([part_one, part_two], root / "parts", 1),
                ),
                patch.object(
                    queue,
                    "_upload_to_storage",
                    AsyncMock(side_effect=[("file-part-1", 101), (None, None)]),
                ),
                patch.object(queue, "STORAGE_CHAT_ID", "-100storage"),
                patch.object(queue, "_finish_video_job", finish),
                patch.object(queue, "_show_download_error", show_error),
                patch.object(queue, "_discard_job", AsyncMock()),
                patch.object(queue, "_remove_download_files") as remove,
                patch.object(queue.asyncio, "sleep", AsyncMock()),
            ):
                with self.assertLogs(level="ERROR"):
                    await queue._process_video_job(bot, job)

        finish.assert_not_awaited()
        bot.edit_message_text.assert_not_awaited()
        show_error.assert_awaited_once()
        self.assertEqual(show_error.await_args.args[4], "❌ Не удалось загрузить видео.")
        remove.assert_called_once_with(job.filename)
        bot.delete_message.assert_awaited_once_with(
            chat_id="-100storage",
            message_id=101,
        )

    async def test_large_inline_persist_failure_never_publishes_success_link(self):
        source_url, inspection, job = self._inspection_and_job()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video_path = root / "large.mp4"
            video_path.write_bytes(b"source")
            part_one = root / "part-1.mp4"
            part_two = root / "part-2.mp4"
            part_one.write_bytes(b"part one")
            part_two.write_bytes(b"part two")
            prepared = queue.PreparedVideo(
                video_path,
                queue.MediaProbe(
                    has_video=True,
                    has_audio=True,
                    video_codec="h264",
                    audio_codec="aac",
                    width=720,
                    height=1280,
                    duration=100.0,
                ),
                True,
            )
            bot = SimpleNamespace(
                get_me=AsyncMock(return_value=SimpleNamespace(username="download_bot")),
                edit_message_text=AsyncMock(),
            )
            show_error = AsyncMock()
            finish = AsyncMock(side_effect=OSError("cache commit failed"))

            with (
                patch.object(queue, "VIDEO_DIR", root),
                patch.object(queue, "_find_worker_cached_video", AsyncMock(return_value=None)),
                patch.object(queue, "_load_info_snapshot", return_value=inspection.info),
                patch.object(queue, "_inspection_from_snapshot", return_value=inspection),
                patch.object(queue, "_ydl_download_from_info_file", return_value=("AbC_123", 720, 1280, "desc", 100.0)),
                patch.object(queue, "_prepare_telegram_video", return_value=prepared),
                patch.object(
                    queue,
                    "_split_video",
                    return_value=queue.VideoSplitOutcome([part_one, part_two], root / "parts", 1),
                ),
                patch.object(
                    queue,
                    "_upload_to_storage",
                    AsyncMock(side_effect=[("file-part-1", 101), ("file-part-2", 102)]),
                ),
                patch.object(queue, "_finish_video_job", finish),
                patch.object(queue, "_show_download_error", show_error),
                patch.object(queue, "_discard_job", AsyncMock()),
                patch.object(queue, "_remove_download_files") as remove,
                patch.object(queue.asyncio, "sleep", AsyncMock()),
            ):
                with self.assertLogs(level="ERROR"):
                    await queue._process_video_job(bot, job)

        finish.assert_awaited_once()
        bot.edit_message_text.assert_not_awaited()
        show_error.assert_awaited_once()
        self.assertEqual(show_error.await_args.args[4], "❌ Не удалось загрузить видео.")
        remove.assert_called_once_with(job.filename)


if __name__ == "__main__":
    unittest.main()
