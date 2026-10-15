import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest

import routers.command_router as command_router
import services.queue as queue
from services.media_cache import CachedVideo, MediaIdentity, MediaProvider


class VideoDeepLinkTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_handler_routes_video_payload(self):
        message = SimpleNamespace(text="/start video_instagram_AbC_123")
        session = SimpleNamespace()
        with patch.object(command_router, "_start_video_deep_link", AsyncMock()) as start:
            await command_router.start_handler(message, session)

        start.assert_awaited_once_with(message, session, "video_instagram_AbC_123")

    async def test_video_share_link_rejects_payload_over_telegram_limit(self):
        identity = MediaIdentity(MediaProvider.INSTAGRAM, "a" * 60)
        bot = SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(username="bot")))

        link = await queue.create_video_share_link(bot, identity)

        self.assertIsNone(link)

    async def test_private_start_delivers_all_cached_parts_to_any_recipient(self):
        cached = CachedVideo(
            "h264-aac-v2:instagram:AbC_123",
            "part-1",
            "desc",
            "https://www.instagram.com/reel/AbC_123/",
            7,
            file_ids=("part-1", "part-2"),
        )
        message = SimpleNamespace(
            text="/start video_instagram_AbC_123",
            chat=SimpleNamespace(type=ChatType.PRIVATE, id=999),
            from_user=SimpleNamespace(id=999),
            bot=SimpleNamespace(),
            answer=AsyncMock(),
        )
        session = SimpleNamespace(rollback=AsyncMock())

        with (
            patch.object(
                command_router,
                "find_cached_video_by_identity",
                AsyncMock(return_value=cached),
            ),
            patch.object(command_router, "deliver_cached_video", AsyncMock()) as deliver,
        ):
            await command_router._start_video_deep_link(
                message,
                session,
                "video_instagram_AbC_123",
            )

        deliver.assert_awaited_once()
        self.assertEqual(deliver.await_args.kwargs["chat_id"], "999")
        message.answer.assert_not_awaited()

    async def test_invalid_or_missing_video_share_is_friendly_and_does_not_deliver(self):
        message = SimpleNamespace(
            text="/start video_instagram_bad/id",
            chat=SimpleNamespace(type=ChatType.PRIVATE, id=999),
            from_user=SimpleNamespace(id=999),
            bot=SimpleNamespace(),
            answer=AsyncMock(),
        )

        await command_router._start_video_deep_link(
            message,
            SimpleNamespace(rollback=AsyncMock()),
            "video_instagram_bad/id",
        )

        message.answer.assert_awaited_once()
        self.assertIn("недействительна", message.answer.await_args.args[0])

    async def test_stale_cached_video_is_invalidated_without_leaking_other_cache(self):
        cached = CachedVideo("h264-aac-v2:tiktok:123", "bad", None, None, None)
        message = SimpleNamespace(
            text="/start video_tiktok_123",
            chat=SimpleNamespace(type=ChatType.PRIVATE, id=999),
            from_user=SimpleNamespace(id=999),
            bot=SimpleNamespace(),
            answer=AsyncMock(),
        )
        error = TelegramBadRequest(MagicMock(), "Bad Request: wrong file identifier")

        with (
            patch.object(
                command_router,
                "find_cached_video_by_identity",
                AsyncMock(return_value=cached),
            ),
            patch.object(command_router, "deliver_cached_video", AsyncMock(side_effect=error)),
            patch.object(command_router, "invalidate_cached_video", AsyncMock()) as invalidate,
        ):
            await command_router._start_video_deep_link(
                message,
                SimpleNamespace(rollback=AsyncMock()),
                "video_tiktok_123",
            )

        invalidate.assert_awaited_once()
        self.assertIn("недоступно", message.answer.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
