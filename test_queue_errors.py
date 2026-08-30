import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

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
    async def test_media_preparation_failure_reports_video_download_failure(self):
        source_url = "https://www.tiktok.com/@author/video/123"
        item = SimpleNamespace(
            id=1,
            chat_id="123",
            user_id="456",
            filename="failed-download",
            tiktok_link=source_url,
            format_id=None,
            inline_message_id=None,
            reply_to_message_id=None,
            enqueued_at=None,
        )
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

        class QueueStopped(Exception):
            pass

        session = AsyncMock()
        session.get.return_value = item
        session_context = MagicMock()
        session_context.__aenter__ = AsyncMock(return_value=session)
        session_context.__aexit__ = AsyncMock(return_value=False)
        session_factory = MagicMock(return_value=session_context)

        class BotStub:
            message_args = None

            async def send_message(self, **kwargs):
                self.message_args = kwargs

        bot = BotStub()
        with (
            patch.object(queue, "SessionLocal", session_factory),
            patch.object(
                queue,
                "_wait_for_next_job",
                AsyncMock(side_effect=[job, QueueStopped()]),
            ),
            patch.object(queue, "_resolve_media", return_value=(None, source_url, None)),
            patch.object(
                queue,
                "_download_media",
                side_effect=queue.MediaPreparationError("download failed"),
            ),
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
