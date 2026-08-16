import unittest

from aiogram.enums import ParseMode

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


if __name__ == "__main__":
    unittest.main()
