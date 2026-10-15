import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram import F
from aiogram.exceptions import TelegramBadRequest

import services.queue as queue
from services.media_cache import CachedVideo, media_identity_from_cache_key
from utils.links import SUPPORTED_URL_RE


def _bad_request(message: str) -> TelegramBadRequest:
    return TelegramBadRequest(method=SimpleNamespace(), message=message)


class LinkFilterTests(unittest.TestCase):
    def test_link_after_text_matches_handler_filter(self):
        link_filter = F.text.regexp(SUPPORTED_URL_RE, mode="search")

        self.assertTrue(link_filter.resolve(
            SimpleNamespace(text="Смотри https://www.tiktok.com/@a/video/1")
        ))


class CacheKeyIdentityTests(unittest.TestCase):
    def test_identity_from_current_and_legacy_keys(self):
        current = media_identity_from_cache_key("h264-aac-v2:instagram:AbC_1")
        legacy = media_identity_from_cache_key("h264-aac-v2:123")

        self.assertEqual(current.media_id, "AbC_1")
        self.assertEqual(legacy.provider, queue.MediaProvider.TIKTOK)
        self.assertIsNone(media_identity_from_cache_key("old-profile:1"))
        self.assertIsNone(media_identity_from_cache_key("h264-aac-v2:tiktok:abc"))


class QueueLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_unexpected_job_error_does_not_stop_queue(self):
        job = queue.QueueJob(7, "42", "42", "file", "https://www.tiktok.com/@a/video/1", None, None, None, None)

        class QueueStopped(BaseException):
            pass

        with (
            patch.object(queue, "_wait_for_next_job", AsyncMock(side_effect=[job, QueueStopped()])),
            patch.object(queue, "_process_video_job", AsyncMock(side_effect=RuntimeError("network"))),
            patch.object(queue, "_discard_job", AsyncMock()) as discard,
            patch.object(queue, "QUEUE_ERROR_BACKOFF_SECONDS", 0),
        ):
            with self.assertLogs(level="ERROR"):
                with self.assertRaises(QueueStopped):
                    await queue.process_queue(SimpleNamespace())

        discard.assert_awaited_once_with(7)


class PartsDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_parts_return_file_ids_for_cache(self):
        parts = [f"part-{number}" for number in range(1, 12)]
        bot = SimpleNamespace(
            send_media_group=AsyncMock(return_value=[
                SimpleNamespace(video=SimpleNamespace(file_id=f"sent-{number}"))
                for number in range(1, 11)
            ]),
            send_video=AsyncMock(),
        )

        file_ids = await queue._deliver_video_parts(
            bot,
            chat_id="42",
            parts=parts,
            width=None,
            height=None,
            description=None,
            source_url=None,
            likes=None,
            reply_to_message_id=None,
            placeholder_message_id=None,
            has_spoiler=False,
            without_description=False,
            music_deep_link=None,
        )

        self.assertEqual(file_ids[:10], tuple(f"sent-{n}" for n in range(1, 11)))
        self.assertEqual(file_ids[10], "part-11")

    async def test_failure_after_first_batch_is_partial(self):
        parts = [f"part-{number}" for number in range(1, 13)]
        bot = SimpleNamespace(
            send_media_group=AsyncMock(side_effect=[[], _bad_request("boom")]),
        )

        with self.assertRaises(queue.PartialVideoDeliveryError) as captured:
            await queue._deliver_video_parts(
                bot,
                chat_id="42",
                parts=parts,
                width=None,
                height=None,
                description=None,
                source_url=None,
                likes=None,
                reply_to_message_id=None,
                placeholder_message_id=None,
                has_spoiler=False,
                without_description=False,
                music_deep_link=None,
            )

        self.assertEqual((captured.exception.delivered, captured.exception.total), (10, 12))


    async def test_placeholder_is_replaced_by_first_part_and_album_replies_to_it(self):
        parts = [f"part-{number}" for number in range(1, 4)]
        bot = SimpleNamespace(send_media_group=AsyncMock(return_value=[]))

        with patch.object(queue, "_deliver_video", AsyncMock(return_value="part-1")) as deliver:
            await queue._deliver_video_parts(
                bot,
                chat_id="-100",
                parts=parts,
                width=None,
                height=None,
                description=None,
                source_url=None,
                likes=None,
                reply_to_message_id=None,
                placeholder_message_id=77,
                has_spoiler=False,
                without_description=False,
                music_deep_link=None,
            )

        self.assertEqual(deliver.await_args.kwargs["placeholder_message_id"], 77)
        album = bot.send_media_group.await_args.kwargs
        self.assertEqual([item.media for item in album["media"]], ["part-2", "part-3"])
        self.assertEqual(album["reply_to_message_id"], 77)
        self.assertEqual(album["request_timeout"], 2 * queue.MEDIA_GROUP_TIMEOUT_PER_PART_SECONDS)


class LinkBoundaryTests(unittest.TestCase):
    def test_tiktok_subdomains_are_not_treated_as_video_links(self):
        from utils.links import find_video_url

        self.assertIsNone(find_video_url("business.tiktok.com/x"))
        self.assertEqual(
            find_video_url("(vm.tiktok.com/ZM1/)"),
            "https://vm.tiktok.com/ZM1/",
        )


class CachedMultipartInlineTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _cached(source_url: str | None) -> CachedVideo:
        return CachedVideo(
            "h264-aac-v2:tiktok:123",
            "part-1",
            "desc",
            source_url,
            7,
            file_ids=("part-1", "part-2"),
        )

    async def test_identity_comes_from_cache_key(self):
        cached = self._cached("https://www.tiktok.com/video/123")
        bot = SimpleNamespace(edit_message_media=AsyncMock())

        with patch.object(
            queue,
            "create_video_share_link",
            AsyncMock(return_value="https://t.me/bot?start=video_tiktok_123"),
        ) as share:
            await queue.deliver_cached_video(
                bot,
                cached,
                chat_id="42",
                fallback_source_url=cached.source_url,
                inline_message_id="inline-id",
            )

        self.assertEqual(share.await_args.args[1].media_id, "123")
        bot.edit_message_media.assert_awaited_once()

    async def test_missing_share_link_defers_to_queue(self):
        cached = self._cached("https://www.tiktok.com/@a/video/123")
        bot = SimpleNamespace(edit_message_media=AsyncMock())

        with (
            patch.object(queue, "find_cached_video", AsyncMock(return_value=cached)),
            patch.object(queue, "create_video_share_link", AsyncMock(return_value=None)),
        ):
            delivered = await queue.try_deliver_cached_video(
                bot,
                AsyncMock(),
                cached.source_url,
                chat_id="42",
                inline_message_id="inline-id",
            )

        self.assertFalse(delivered)
        bot.edit_message_media.assert_not_awaited()


class WorkerFallbackTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _inspection():
        source_url = "https://www.instagram.com/reel/AbC_123/"
        return queue.MediaInspection(
            identity=queue.MediaIdentity(queue.MediaProvider.INSTAGRAM, "AbC_123"),
            source_url=source_url,
            likes=7,
            description="desc",
            width=720,
            height=1280,
            duration=10.0,
            qualities=(),
            info={"id": "AbC_123", "webpage_url": source_url},
            fast_format_id="unknown-mp4",
        )

    @staticmethod
    def _prepared(path: Path) -> queue.PreparedVideo:
        return queue.PreparedVideo(
            path,
            queue.MediaProbe(
                has_video=True,
                has_audio=True,
                video_codec="h264",
                audio_codec="aac",
                width=720,
                height=1280,
                duration=10.0,
            ),
            True,
        )

    async def test_non_h264_download_falls_back_to_gallery(self):
        inspection = self._inspection()
        job = queue.QueueJob(5, "42", "42", "file", inspection.source_url, None, None, None, None)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "file.mp4").write_bytes(b"vp9")
            gallery_path = root / "gallery.mp4"
            gallery_path.write_bytes(b"h264")
            gallery = SimpleNamespace(
                path=gallery_path,
                generated_slideshow=False,
                metadata=lambda: ("AbC_123", 720, 1280, "desc", 10.0),
            )
            prepare_calls: list[Path] = []

            def prepare(path):
                prepare_calls.append(path)
                if len(prepare_calls) == 1:
                    raise queue.NoFastH264FormatError("vp9")
                return self._prepared(path)

            with (
                patch.object(queue, "VIDEO_DIR", root),
                patch.object(queue, "_find_worker_cached_video", AsyncMock(return_value=None)),
                patch.object(queue, "_load_info_snapshot", return_value=inspection.info),
                patch.object(queue, "_inspection_from_snapshot", return_value=inspection),
                patch.object(queue, "_ydl_download_from_info_file", return_value=("AbC_123", 720, 1280, "desc", 10.0)),
                patch.object(queue, "_prepare_telegram_video", side_effect=prepare),
                patch.object(queue, "_gallery_download", return_value=gallery) as gallery_download,
                patch.object(queue, "_split_video", return_value=queue.VideoSplitOutcome([gallery_path], None, 0)),
                patch.object(queue, "_deliver_video", AsyncMock(return_value="file-id")) as deliver,
                patch.object(queue, "prepare_music_caption_link", AsyncMock(return_value=(None, None))),
                patch.object(queue, "_finish_video_job", AsyncMock()) as finish,
            ):
                await queue._process_video_job(SimpleNamespace(), job)

        gallery_download.assert_called_once()
        self.assertEqual(prepare_calls[-1], gallery_path)
        self.assertEqual(deliver.await_args.kwargs["video_path"], gallery_path)
        self.assertEqual(finish.await_args.args[1].file_id, "file-id")

    async def test_multipart_chat_delivery_is_cached(self):
        inspection = self._inspection()
        job = queue.QueueJob(6, "42", "42", "file", inspection.source_url, None, None, None, None)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video_path = root / "file.mp4"
            video_path.write_bytes(b"source")
            parts = [root / "part-1.mp4", root / "part-2.mp4"]
            for part in parts:
                part.write_bytes(b"part")

            with (
                patch.object(queue, "VIDEO_DIR", root),
                patch.object(queue, "_find_worker_cached_video", AsyncMock(return_value=None)),
                patch.object(queue, "_load_info_snapshot", return_value=inspection.info),
                patch.object(queue, "_inspection_from_snapshot", return_value=inspection),
                patch.object(queue, "_ydl_download_from_info_file", return_value=("AbC_123", 720, 1280, "desc", 10.0)),
                patch.object(queue, "_prepare_telegram_video", return_value=self._prepared(video_path)),
                patch.object(queue, "_split_video", return_value=queue.VideoSplitOutcome(parts, None, 1)),
                patch.object(queue, "_deliver_video_parts", AsyncMock(return_value=("a", "b"))),
                patch.object(queue, "prepare_music_caption_link", AsyncMock(return_value=(None, None))),
                patch.object(queue, "_finish_video_job", AsyncMock()) as finish,
            ):
                await queue._process_video_job(SimpleNamespace(), job)

        update = finish.await_args.args[1]
        self.assertEqual(update.file_ids, ("a", "b"))
        self.assertEqual(update.cache_key, "h264-aac-v2:instagram:AbC_123")


if __name__ == "__main__":
    unittest.main()
