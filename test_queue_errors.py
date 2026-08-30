import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiogram.enums import ParseMode

import services.queue as queue
from services.queue import _show_download_error


class ErrorSourceLinkTests(unittest.IsolatedAsyncioTestCase):
    async def test_error_caption_keeps_a_clickable_source_link(self):
        class BotStub:
            caption_args = None

            async def edit_message_caption(self, **kwargs):
                self.caption_args = kwargs

        bot = BotStub()
        await _show_download_error(
            bot,
            "123",
            None,
            456,
            "❌ Не удалось скачать видео.",
            "https://www.tiktok.com/@author/video/123?share=1",
        )

        self.assertEqual(bot.caption_args["parse_mode"], ParseMode.HTML)
        self.assertIn(
            'href="https://www.tiktok.com/@author/video/123"',
            bot.caption_args["caption"],
        )

    async def test_standalone_error_keeps_a_clickable_source_link(self):
        class BotStub:
            message_args = None

            async def send_message(self, **kwargs):
                self.message_args = kwargs

        bot = BotStub()
        await _show_download_error(
            bot,
            "123",
            None,
            None,
            "❌ Не удалось доставить видео.",
            "https://www.instagram.com/reel/ABC/?utm_source=test",
        )

        self.assertEqual(bot.message_args["parse_mode"], ParseMode.HTML)
        self.assertIn(
            'href="https://www.instagram.com/reel/ABC/"',
            bot.message_args["text"],
        )


class QueueErrorMessageTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _inspection(source_url: str) -> queue.MediaInspection:
        return queue.MediaInspection(
            queue.MediaIdentity(queue.MediaProvider.TIKTOK, "123"),
            source_url,
            None,
            None,
            640,
            360,
            5.0,
            (),
            {"id": "123", "webpage_url": source_url},
            "h264-aac",
        )

    async def test_oversized_video_message_offers_an_available_action(self):
        source_url = "https://www.tiktok.com/@author/video/123"
        job = queue.QueueJob(20, "123", "456", "oversized", source_url, None, None, None, None)
        inspection = self._inspection(source_url)
        show_error = AsyncMock()

        with (
            patch.object(queue, "_find_worker_cached_video", AsyncMock(return_value=None)),
            patch.object(queue, "_load_info_snapshot", return_value=inspection.info),
            patch.object(queue, "_inspection_from_snapshot", return_value=inspection),
            patch.object(
                queue,
                "_ydl_download_from_info_file",
                side_effect=queue.VideoTooLargeError,
            ),
            patch.object(queue, "_discard_job", AsyncMock()),
            patch.object(queue, "_show_download_error", show_error),
            patch.object(queue, "_remove_download_files"),
            self.assertLogs(level="INFO"),
        ):
            await queue._process_video_job(AsyncMock(), job)

        message = show_error.await_args.args[4]
        self.assertNotIn("другое качество", message.lower())
        self.assertIn("Попробуйте другое видео или повторите позже.", message)

    async def test_split_failure_message_offers_an_available_action(self):
        source_url = "https://www.tiktok.com/@author/video/123"
        job = queue.QueueJob(21, "123", "456", "split", source_url, None, None, None, None)
        inspection = self._inspection(source_url)
        show_error = AsyncMock()

        with tempfile.TemporaryDirectory() as directory:
            video_path = Path(directory) / "split.mp4"

            def download(*_args):
                video_path.write_bytes(b"video")
                return "123", 640, 360, None, 5.0

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
                ),
                True,
                "direct",
            )
            with (
                patch.object(queue, "VIDEO_DIR", Path(directory)),
                patch.object(queue, "_find_worker_cached_video", AsyncMock(return_value=None)),
                patch.object(queue, "_load_info_snapshot", return_value=inspection.info),
                patch.object(queue, "_inspection_from_snapshot", return_value=inspection),
                patch.object(queue, "_ydl_download_from_info_file", side_effect=download),
                patch.object(queue, "_prepare_telegram_video", return_value=prepared),
                patch.object(
                    queue,
                    "_split_video",
                    side_effect=queue.VideoSplitError(attempts=1),
                ),
                patch.object(queue, "_discard_job", AsyncMock()),
                patch.object(queue, "_show_download_error", show_error),
                patch.object(queue, "_remove_download_files"),
                self.assertLogs(level="WARNING"),
            ):
                await queue._process_video_job(AsyncMock(), job)

        message = show_error.await_args.args[4]
        self.assertNotIn("другое качество", message.lower())
        self.assertIn("Попробуйте другое видео или повторите позже.", message)

    async def test_no_fast_h264_error_keeps_source_link(self):
        source_url = "https://www.tiktok.com/@author/video/123"
        job = queue.QueueJob(1, "123", "456", "no-fast-h264", source_url, None, None, None, None)
        inspection = queue.MediaInspection(
            queue.MediaIdentity(queue.MediaProvider.TIKTOK, "123"),
            source_url,
            None,
            None,
            640,
            360,
            5.0,
            (),
            {"id": "123", "webpage_url": source_url},
            "h264-aac",
        )

        class BotStub:
            message_args = None

            async def send_message(self, **kwargs):
                self.message_args = kwargs

        bot = BotStub()
        with (
            patch.object(queue, "_find_worker_cached_video", AsyncMock(return_value=None)),
            patch.object(queue, "_load_info_snapshot", return_value=inspection.info),
            patch.object(queue, "_inspection_from_snapshot", return_value=inspection),
            patch.object(
                queue,
                "require_fast_h264_format",
                side_effect=queue.NoFastH264FormatError,
            ),
            patch.object(queue, "_gallery_download", return_value=None),
            patch.object(queue, "_discard_job", AsyncMock()),
            patch.object(queue, "_remove_download_files"),
            self.assertLogs(level="INFO"),
        ):
            await queue._process_video_job(bot, job)

        self.assertIn("совместимый H.264-вариант", bot.message_args["text"])
        self.assertIn('href="https://www.tiktok.com/@author/video/123"', bot.message_args["text"])

    async def test_inspection_failure_reports_video_download_failure(self):
        source_url = "https://www.tiktok.com/@author/video/123"
        job = queue.QueueJob(
            1,
            "123",
            "456",
            "failed-inspection",
            source_url,
            None,
            None,
            None,
            None,
        )

        class BotStub:
            message_args = None

            async def send_message(self, **kwargs):
                self.message_args = kwargs

        bot = BotStub()
        with (
            patch.object(
                queue,
                "_find_worker_cached_video",
                AsyncMock(return_value=None),
            ),
            patch.object(queue, "_load_info_snapshot", return_value=None),
            patch.object(queue, "inspect_media", side_effect=RuntimeError("inspect failed")),
            patch.object(queue, "_discard_job", AsyncMock()),
            patch.object(queue, "_remove_download_files"),
            patch.object(queue.asyncio, "sleep", AsyncMock()),
            self.assertLogs(level="ERROR"),
        ):
            await queue._process_video_job(bot, job)

        self.assertTrue(
            bot.message_args["text"].startswith("❌ Не удалось загрузить видео.\n\n")
        )

    async def test_media_preparation_failure_reports_video_download_failure(self):
        source_url = "https://www.tiktok.com/@author/video/123"
        job = queue.QueueJob(
            1,
            "123",
            "456",
            "failed-download",
            source_url,
            None,
            None,
            None,
            None,
        )
        inspection = queue.MediaInspection(
            queue.MediaIdentity(queue.MediaProvider.TIKTOK, "123"),
            source_url,
            None,
            None,
            None,
            None,
            None,
            (),
            {"id": "123", "webpage_url": source_url},
            "h264-aac",
        )

        class QueueStopped(Exception):
            pass

        class BotStub:
            message_args = None

            async def send_message(self, **kwargs):
                self.message_args = kwargs

        bot = BotStub()
        with (
            patch.object(
                queue,
                "_wait_for_next_job",
                AsyncMock(side_effect=[job, QueueStopped()]),
            ),
            patch.object(
                queue,
                "_find_worker_cached_video",
                AsyncMock(return_value=None),
            ),
            patch.object(queue, "_load_info_snapshot", return_value=inspection.info),
            patch.object(queue, "_inspection_from_snapshot", return_value=inspection),
            patch.object(
                queue,
                "_ydl_download_from_info_file",
                side_effect=queue.MediaPreparationError("snapshot failed"),
            ),
            patch.object(
                queue,
                "_download_media",
                side_effect=queue.MediaPreparationError("download failed"),
            ),
            patch.object(queue, "_discard_job", AsyncMock()),
            patch.object(queue, "_remove_download_files"),
        ):
            with self.assertLogs(level="ERROR"):
                with self.assertRaises(QueueStopped):
                    await queue.process_queue(bot)

        self.assertTrue(
            bot.message_args["text"].startswith("❌ Не удалось загрузить видео.\n\n")
        )


if __name__ == "__main__":
    unittest.main()
