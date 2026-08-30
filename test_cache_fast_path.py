import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.exceptions import TelegramBadRequest

import services.queue as queue
from services.media_cache import CachedVideo
import handlers.link as link_handler
import routers.inline_router as inline_handler


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

    async def test_captioned_private_group_and_inline_cache_hits_get_music_deep_links(self):
        """Fails if any captioned handler cache route omits the opt-in music link."""
        cached = CachedVideo(
            "h264-aac-v2:tiktok:123",
            "file-id",
            "desc",
            "https://www.tiktok.com/@a/video/123",
            7,
        )
        routes = (
            {"chat_id": "42"},
            {"chat_id": "-100", "reply_to_message_id": 321},
            {"chat_id": "42", "inline_message_id": "inline-id"},
        )

        for route in routes:
            with self.subTest(route=route):
                events = []
                session = AsyncMock()
                session.rollback.side_effect = lambda: events.append("read-released")

                async def prepare(*_args):
                    self.assertEqual(events, ["read-released"])
                    return "token", "https://t.me/testbot?start=music_token"

                with (
                    patch.object(queue, "find_cached_video", AsyncMock(return_value=cached)),
                    patch.object(queue, "prepare_music_caption_link", side_effect=prepare) as link,
                    patch.object(queue, "deliver_cached_video", AsyncMock()) as deliver,
                ):
                    delivered = await queue.try_deliver_cached_video(
                        SimpleNamespace(),
                        session,
                        "https://www.tiktok.com/@a/video/123",
                        **route,
                    )

                self.assertTrue(delivered)
                link.assert_awaited_once()
                self.assertEqual(
                    deliver.await_args.kwargs["music_deep_link"],
                    "https://t.me/testbot?start=music_token",
                )

    async def test_no_description_cache_hit_never_creates_music_deep_link(self):
        """Fails if no-description cache delivery creates an unused MusicRequest."""
        cached = CachedVideo("h264-aac-v2:tiktok:123", "file-id", "desc", None, 7)
        session = AsyncMock()

        with (
            patch.object(queue, "find_cached_video", AsyncMock(return_value=cached)),
            patch.object(queue, "prepare_music_caption_link", AsyncMock()) as link,
            patch.object(queue, "deliver_cached_video", AsyncMock()) as deliver,
        ):
            delivered = await queue.try_deliver_cached_video(
                SimpleNamespace(),
                session,
                "https://www.tiktok.com/@a/video/123",
                chat_id="42",
                without_description=True,
            )

        self.assertTrue(delivered)
        link.assert_not_awaited()
        self.assertIsNone(deliver.await_args.kwargs["music_deep_link"])


class HandlerCacheFastPathTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        link_handler._pending.clear()

    def _callback(self, mode: str, token: str):
        return SimpleNamespace(
            data=f"download:{mode}:{token}",
            from_user=SimpleNamespace(id=42),
            message=SimpleNamespace(
                delete=AsyncMock(),
                answer=AsyncMock(),
                edit_text=AsyncMock(),
            ),
            answer=AsyncMock(),
        )

    async def test_private_cache_hits_skip_inspection_and_enqueue_for_every_delivery_mode(self):
        """Fails if any direct-download mode inspects or queues a cached video."""
        expected_flags = {
            "normal": (False, False),
            "no_description": (True, False),
            "spoiler": (False, True),
            "no_description_spoiler": (True, True),
        }
        bot = SimpleNamespace()
        session = SimpleNamespace()

        for mode, (without_description, has_spoiler) in expected_flags.items():
            with self.subTest(mode=mode):
                token = f"{mode}-token"
                callback = self._callback(mode, token)
                link_handler._pending[token] = link_handler.PendingDownload(
                    42, "https://www.tiktok.com/@a/video/123"
                )
                with (
                    patch("handlers.link.try_deliver_cached_video", AsyncMock(return_value=True)) as deliver,
                    patch("handlers.link.inspect_media") as inspection,
                    patch("handlers.link.enqueue_download", AsyncMock()) as enqueue,
                ):
                    await link_handler.handle_download_option(callback, session, bot)

                self.assertEqual(deliver.await_args.args[2], "https://www.tiktok.com/@a/video/123")
                self.assertEqual(deliver.await_args.kwargs["without_description"], without_description)
                self.assertEqual(deliver.await_args.kwargs["has_spoiler"], has_spoiler)
                inspection.assert_not_called()
                enqueue.assert_not_awaited()
                callback.message.delete.assert_awaited_once()
                self.assertNotIn(token, link_handler._pending)

    async def test_private_cache_miss_continues_to_inspection_and_pending_download(self):
        """Fails if a cache miss suppresses the existing direct-download path."""
        token = "private-miss"
        callback = self._callback("normal", token)
        session = SimpleNamespace()
        link_handler._pending[token] = link_handler.PendingDownload(
            42, "https://www.tiktok.com/@a/video/123"
        )

        with (
            patch("handlers.link.try_deliver_cached_video", AsyncMock(return_value=False)),
            patch(
                "handlers.link.inspect_media",
                return_value=queue.MediaInspection(
                    identity=queue.MediaIdentity(queue.MediaProvider.TIKTOK, "123"),
                    source_url="https://www.tiktok.com/@a/video/123",
                    likes=None,
                    description=None,
                    width=None,
                    height=None,
                    duration=None,
                    qualities=(),
                    info={"id": "123"},
                ),
            ) as inspection,
            patch("handlers.link._start_pending_download", AsyncMock()) as start,
        ):
            await link_handler.handle_download_option(callback, session, SimpleNamespace())

        inspection.assert_called_once_with("https://www.tiktok.com/@a/video/123")
        start.assert_awaited_once()

    async def test_group_cache_hit_skips_placeholder_queue_and_hold(self):
        """Fails if a group cache hit creates any queue-owned placeholder."""
        message = SimpleNamespace(
            text="https://www.tiktok.com/@a/video/123",
            chat=SimpleNamespace(id=-100, type="group"),
            from_user=SimpleNamespace(id=42),
            message_id=321,
            reply_photo=AsyncMock(),
            reply=AsyncMock(),
        )

        with (
            patch(
                "handlers.link._video_url_from_text",
                AsyncMock(return_value="https://www.tiktok.com/@a/video/123"),
            ),
            patch("handlers.link.try_deliver_cached_video", AsyncMock(return_value=True)) as deliver,
            patch("handlers.link.enqueue_download", AsyncMock()) as enqueue,
            patch("handlers.link.hold_placeholder") as hold,
        ):
            await link_handler.handle_video_link_group(message, SimpleNamespace(), SimpleNamespace())

        self.assertEqual(deliver.await_args.kwargs["reply_to_message_id"], 321)
        message.reply_photo.assert_not_awaited()
        enqueue.assert_not_awaited()
        hold.assert_not_called()

    async def test_group_cache_miss_creates_placeholder_and_enqueues(self):
        """Fails if a group cache miss does not resume the queue path."""
        placeholder = SimpleNamespace(message_id=17, delete=AsyncMock(), edit_caption=AsyncMock())
        message = SimpleNamespace(
            text="https://www.tiktok.com/@a/video/123",
            chat=SimpleNamespace(id=-100, type="group"),
            from_user=SimpleNamespace(id=42),
            message_id=321,
            reply_photo=AsyncMock(return_value=placeholder),
            reply=AsyncMock(),
        )

        with (
            patch(
                "handlers.link._video_url_from_text",
                AsyncMock(return_value="https://www.tiktok.com/@a/video/123"),
            ),
            patch("handlers.link.try_deliver_cached_video", AsyncMock(return_value=False)),
            patch("handlers.link.enqueue_download", AsyncMock(return_value=False)) as enqueue,
        ):
            await link_handler.handle_video_link_group(message, SimpleNamespace(), SimpleNamespace())

        message.reply_photo.assert_awaited_once()
        enqueue.assert_awaited_once()

    async def test_inline_cache_hits_pass_bare_id_and_flags_without_queue_work(self):
        """Fails if an inline cache hit serializes an id or enters the placeholder queue path."""
        flags = {
            "download": (False, False),
            "download_no_description": (True, False),
            "download_spoiler": (False, True),
            "download_no_description_spoiler": (True, True),
        }
        bot = SimpleNamespace(
            edit_message_media=AsyncMock(),
            edit_message_caption=AsyncMock(),
            edit_message_text=AsyncMock(),
            send_message=AsyncMock(),
        )
        session = SimpleNamespace()

        for result_id, (without_description, has_spoiler) in flags.items():
            with self.subTest(result_id=result_id):
                result = SimpleNamespace(
                    query="https://www.tiktok.com/@a/video/123",
                    inline_message_id="bare-inline-id",
                    result_id=result_id,
                    from_user=SimpleNamespace(id=42),
                )
                with (
                    patch(
                        "routers.inline_router._canonical_url",
                        AsyncMock(return_value="https://www.tiktok.com/@a/video/123"),
                    ),
                    patch("routers.inline_router.try_deliver_cached_video", AsyncMock(return_value=True)) as deliver,
                    patch("routers.inline_router.DownloadQueue") as queue_model,
                    patch("routers.inline_router.enqueue_download", AsyncMock()) as enqueue,
                    patch("routers.inline_router.hold_inline_placeholder") as hold,
                ):
                    await inline_handler.chosen_download(result, session, bot)

                self.assertEqual(deliver.await_args.kwargs["inline_message_id"], "bare-inline-id")
                self.assertEqual(deliver.await_args.kwargs["without_description"], without_description)
                self.assertEqual(deliver.await_args.kwargs["has_spoiler"], has_spoiler)
                queue_model.assert_not_called()
                bot.edit_message_media.assert_not_awaited()
                enqueue.assert_not_awaited()
                hold.assert_not_called()

    async def test_inline_cache_miss_serializes_queue_id_and_enqueues(self):
        """Fails if an inline cache miss does not resume the serialized queue path."""
        result = SimpleNamespace(
            query="https://www.tiktok.com/@a/video/123",
            inline_message_id="bare-inline-id",
            result_id="download_no_description_spoiler",
            from_user=SimpleNamespace(id=42),
        )
        bot = SimpleNamespace(
            edit_message_media=AsyncMock(),
            edit_message_caption=AsyncMock(),
            edit_message_text=AsyncMock(),
            send_message=AsyncMock(),
        )

        with (
            patch(
                "routers.inline_router._canonical_url",
                AsyncMock(return_value="https://www.tiktok.com/@a/video/123"),
            ),
            patch("routers.inline_router.try_deliver_cached_video", AsyncMock(return_value=False)),
            patch("routers.inline_router.AD_PLACEHOLDER_PHOTO_FILE_ID", None),
            patch("routers.inline_router.enqueue_download", AsyncMock(return_value=False)) as enqueue,
        ):
            await inline_handler.chosen_download(result, SimpleNamespace(), bot)

        queue_item = enqueue.await_args.args[1]
        self.assertEqual(queue_item.inline_message_id, "spoiler:no_description:bare-inline-id")
