import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.exceptions import TelegramBadRequest

import services.queue as queue
from services.media_cache import CachedVideo


class CachedDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_cached_delivery_has_no_placeholder_sleep_or_local_file(self):
        """Fails if cache delivery starts waiting for a placeholder or opens a file."""
        cached = CachedVideo("h264-aac-v2:123", "file-id", "desc", None, 7)
        bot = SimpleNamespace(send_video=AsyncMock(return_value=SimpleNamespace(video=None)))

        with patch.object(queue.asyncio, "sleep", new=AsyncMock()) as sleep_mock:
            await queue.deliver_cached_video(
                bot,
                cached,
                chat_id="1",
                fallback_source_url="https://www.tiktok.com/@a/video/123",
            )

        sleep_mock.assert_not_awaited()
        self.assertEqual(bot.send_video.await_args.kwargs["video"], "file-id")

    async def test_cached_inline_delivery_has_no_placeholder_sleep(self):
        """Fails if cached inline delivery waits for the normal placeholder deadline."""
        cached = CachedVideo("h264-aac-v2:123", "file-id", "desc", None, 7)
        bot = SimpleNamespace(edit_message_media=AsyncMock())

        with patch.object(queue.asyncio, "sleep", new=AsyncMock()) as sleep_mock:
            await queue.deliver_cached_video(
                bot,
                cached,
                chat_id="1",
                fallback_source_url="https://www.tiktok.com/@a/video/123",
                inline_message_id="inline-message-id",
            )

        sleep_mock.assert_not_awaited()
        self.assertEqual(bot.edit_message_media.await_args.kwargs["media"].media, "file-id")

    async def test_invalid_file_id_is_deleted_and_returns_miss(self):
        """Fails if a known-invalid cached file id is kept after Telegram rejects it."""
        session = AsyncMock()
        cached = CachedVideo("h264-aac-v2:123", "bad", None, None, None)
        error = TelegramBadRequest(MagicMock(), "Bad Request: wrong file identifier")

        with (
            patch.object(queue, "find_cached_video", AsyncMock(return_value=cached)),
            patch.object(queue, "deliver_cached_video", AsyncMock(side_effect=error)),
            patch.object(queue, "invalidate_cached_video", AsyncMock()) as invalidate,
        ):
            delivered = await queue.try_deliver_cached_video(
                SimpleNamespace(), session, "https://www.tiktok.com/@a/video/123", chat_id="1"
            )

        self.assertFalse(delivered)
        invalidate.assert_awaited_once_with(session, cached.cache_key)

    async def test_unrelated_bad_request_is_reraised_without_cache_invalidation(self):
        """Fails if an unrelated delivery failure deletes an otherwise usable cache entry."""
        session = AsyncMock()
        cached = CachedVideo("h264-aac-v2:123", "file-id", None, None, None)
        error = TelegramBadRequest(MagicMock(), "Bad Request: message is not modified")

        with (
            patch.object(queue, "find_cached_video", AsyncMock(return_value=cached)),
            patch.object(queue, "deliver_cached_video", AsyncMock(side_effect=error)),
            patch.object(queue, "invalidate_cached_video", AsyncMock()) as invalidate,
        ):
            with self.assertRaisesRegex(TelegramBadRequest, "message is not modified"):
                await queue.try_deliver_cached_video(
                    SimpleNamespace(), session, "https://www.tiktok.com/@a/video/123", chat_id="1"
                )

        invalidate.assert_not_awaited()
