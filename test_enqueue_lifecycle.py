import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import handlers.link as link_handler
import routers.inline_router as inline_handler
import services.queue as queue


SOURCE_URL = "https://www.tiktok.com/@a/video/123"


class AdvertisementDeadlineOrderingTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        link_handler._pending.clear()
        queue._placeholder_ready_at.clear()
        queue._inline_placeholder_ready_at.clear()

    async def test_private_deadline_exists_before_enqueue_can_wake_worker(self):
        """Fails if private enqueue can wake the worker before its deadline exists."""
        events = []
        placeholder = SimpleNamespace(
            message_id=55,
            delete=AsyncMock(),
            edit_caption=AsyncMock(),
        )
        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=42),
            message=SimpleNamespace(
                answer_photo=AsyncMock(return_value=placeholder),
                delete=AsyncMock(),
            ),
            answer=AsyncMock(),
        )
        pending = link_handler.PendingDownload(42, SOURCE_URL, mode="normal")
        scalar = MagicMock()
        scalar.scalar_one.return_value = 1
        session = SimpleNamespace(execute=AsyncMock(return_value=scalar))

        async def enqueue(_session, item, _user_id):
            events.append("enqueue")
            item.id = 1
            return True

        def hold(*_args):
            events.append("deadline")

        with (
            patch("handlers.link.enqueue_download", side_effect=enqueue),
            patch("handlers.link.hold_placeholder", side_effect=hold),
        ):
            await link_handler._start_pending_download(
                callback,
                session,
                "private-token",
                pending,
                format_id=None,
            )

        self.assertEqual(events[:2], ["deadline", "enqueue"])

    async def test_group_deadline_exists_before_enqueue_can_wake_worker(self):
        """Fails if group enqueue can wake the worker before its deadline exists."""
        events = []
        placeholder = SimpleNamespace(
            message_id=17,
            delete=AsyncMock(),
            edit_caption=AsyncMock(),
        )
        message = SimpleNamespace(
            text=SOURCE_URL,
            chat=SimpleNamespace(id=-100, type="group"),
            from_user=SimpleNamespace(id=42),
            message_id=321,
            reply_photo=AsyncMock(return_value=placeholder),
            reply=AsyncMock(),
        )
        scalar = MagicMock()
        scalar.scalar_one.return_value = 1
        session = SimpleNamespace(execute=AsyncMock(return_value=scalar))

        async def enqueue(_session, item, _user_id):
            events.append("enqueue")
            item.id = 1
            return True

        def hold(*_args):
            events.append("deadline")

        with (
            patch("handlers.link._video_url_from_text", AsyncMock(return_value=SOURCE_URL)),
            patch("handlers.link.try_deliver_cached_video", AsyncMock(return_value=False)),
            patch("handlers.link.enqueue_download", side_effect=enqueue),
            patch("handlers.link.hold_placeholder", side_effect=hold),
        ):
            await link_handler.handle_video_link_group(message, session, SimpleNamespace())

        self.assertEqual(events[:2], ["deadline", "enqueue"])

    async def test_inline_deadline_exists_before_enqueue_can_wake_worker(self):
        """Fails if inline enqueue can wake the worker before its deadline exists."""
        events = []
        result = SimpleNamespace(
            query=SOURCE_URL,
            inline_message_id="inline-id",
            result_id="download",
            from_user=SimpleNamespace(id=42),
        )
        bot = SimpleNamespace(
            edit_message_media=AsyncMock(),
            edit_message_caption=AsyncMock(),
            edit_message_text=AsyncMock(),
            send_message=AsyncMock(),
        )
        scalar = MagicMock()
        scalar.scalar_one.return_value = 1
        session = SimpleNamespace(execute=AsyncMock(return_value=scalar))

        async def enqueue(_session, item, _user_id):
            events.append("enqueue")
            item.id = 1
            return True

        def hold(*_args):
            events.append("deadline")

        with (
            patch("routers.inline_router._canonical_url", AsyncMock(return_value=SOURCE_URL)),
            patch("routers.inline_router.try_deliver_cached_video", AsyncMock(return_value=False)),
            patch("routers.inline_router.AD_PLACEHOLDER_PHOTO_FILE_ID", None),
            patch("routers.inline_router.enqueue_download", side_effect=enqueue),
            patch("routers.inline_router.hold_inline_placeholder", side_effect=hold),
        ):
            await inline_handler.chosen_download(result, session, bot)

        self.assertEqual(events[:2], ["deadline", "enqueue"])


class PreEnqueueFailureLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        link_handler._pending.clear()
        queue._placeholder_ready_at.clear()
        queue._inline_placeholder_ready_at.clear()

    @staticmethod
    def _private_callback(token: str):
        placeholder = SimpleNamespace(
            message_id=55,
            delete=AsyncMock(),
            edit_caption=AsyncMock(),
        )
        callback = SimpleNamespace(
            data=f"download:normal:{token}",
            from_user=SimpleNamespace(id=42),
            message=SimpleNamespace(
                answer_photo=AsyncMock(return_value=placeholder),
                answer=AsyncMock(),
                delete=AsyncMock(),
                edit_text=AsyncMock(),
            ),
            answer=AsyncMock(),
        )
        return callback, placeholder

    async def test_private_snapshot_write_failures_restore_retryable_pending(self):
        """Fails if ENOSPC/serialization loses private retry state or leaks a placeholder."""
        inspection = queue.MediaInspection(
            identity=queue.MediaIdentity(queue.MediaProvider.TIKTOK, "123"),
            source_url=SOURCE_URL,
            likes=None,
            description=None,
            width=None,
            height=None,
            duration=None,
            qualities=(),
            info={"id": "123", "webpage_url": SOURCE_URL},
        )

        for token, error in (
            ("enospc", OSError(28, "No space left on device")),
            ("serialization", TypeError("not JSON serializable")),
        ):
            with self.subTest(error=type(error).__name__):
                callback, placeholder = self._private_callback(token)
                link_handler._pending[token] = link_handler.PendingDownload(42, SOURCE_URL)
                with (
                    patch("handlers.link.try_deliver_cached_video", AsyncMock(return_value=False)),
                    patch("handlers.link.inspect_media", return_value=inspection),
                    patch("handlers.link.write_info_snapshot", side_effect=error),
                ):
                    await link_handler.handle_download_option(
                        callback,
                        SimpleNamespace(),
                        SimpleNamespace(),
                    )

                self.assertIn(token, link_handler._pending)
                callback.message.answer_photo.assert_not_awaited()
                self.assertIn(
                    "❌ Не удалось загрузить видео.",
                    callback.message.answer.await_args.args[0],
                )
                link_handler._pending.clear()

    async def test_group_commit_failure_cleans_deadline_and_renders_terminal_error(self):
        """Fails if a group commit exception leaves a queue-owned placeholder behind."""
        placeholder = SimpleNamespace(message_id=17, delete=AsyncMock(), edit_caption=AsyncMock())
        message = SimpleNamespace(
            text=SOURCE_URL,
            chat=SimpleNamespace(id=-100, type="group"),
            from_user=SimpleNamespace(id=42),
            message_id=321,
            reply_photo=AsyncMock(return_value=placeholder),
            reply=AsyncMock(),
        )

        with (
            patch("handlers.link._video_url_from_text", AsyncMock(return_value=SOURCE_URL)),
            patch("handlers.link.try_deliver_cached_video", AsyncMock(return_value=False)),
            patch("handlers.link.enqueue_download", AsyncMock(side_effect=OSError("commit failed"))),
        ):
            await link_handler.handle_video_link_group(message, SimpleNamespace(), SimpleNamespace())

        placeholder.delete.assert_awaited_once()
        self.assertNotIn(("-100", 17), queue._placeholder_ready_at)
        self.assertIn("❌ Не удалось загрузить видео.", message.reply.await_args.args[0])

    async def test_inline_commit_failure_cleans_deadline_and_renders_terminal_error(self):
        """Fails if an inline commit exception leaves its placeholder spinning forever."""
        result = SimpleNamespace(
            query=SOURCE_URL,
            inline_message_id="inline-id",
            result_id="download",
            from_user=SimpleNamespace(id=42),
        )
        bot = SimpleNamespace(
            edit_message_media=AsyncMock(),
            edit_message_caption=AsyncMock(),
            edit_message_text=AsyncMock(),
            send_message=AsyncMock(),
        )

        with (
            patch("routers.inline_router._canonical_url", AsyncMock(return_value=SOURCE_URL)),
            patch("routers.inline_router.try_deliver_cached_video", AsyncMock(return_value=False)),
            patch("routers.inline_router.AD_PLACEHOLDER_PHOTO_FILE_ID", None),
            patch(
                "routers.inline_router.enqueue_download",
                AsyncMock(side_effect=OSError("commit failed")),
            ),
        ):
            await inline_handler.chosen_download(result, SimpleNamespace(), bot)

        self.assertNotIn("inline-id", queue._inline_placeholder_ready_at)
        rendered = (
            bot.edit_message_caption.await_args.kwargs.get("caption")
            if bot.edit_message_caption.await_count
            else bot.edit_message_text.await_args.kwargs.get("text")
        )
        self.assertIn("❌ Не удалось загрузить видео.", rendered)


class PendingLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        link_handler._pending.clear()
        tasks = getattr(link_handler, "_pending_expiry_tasks", {})
        for task in list(tasks.values()):
            task.cancel()
        tasks.clear()

    async def test_pending_is_bounded_per_user_and_eviction_deletes_snapshot(self):
        """Fails if a third private menu retains the oldest user's disk snapshot."""
        message = SimpleNamespace(
            text=SOURCE_URL,
            chat=SimpleNamespace(id=42),
            answer=AsyncMock(),
        )
        tokens = iter(("oldest", "middle", "newest"))

        with tempfile.TemporaryDirectory() as directory:
            with (
                patch("handlers.link._video_url_from_text", AsyncMock(return_value=SOURCE_URL)),
                patch(
                    "handlers.link.uuid.uuid4",
                    side_effect=lambda: SimpleNamespace(hex=next(tokens)),
                ),
            ):
                await link_handler.handle_video_link_private(message)
                oldest_snapshot = Path(directory) / "oldest.info.json"
                oldest_snapshot.write_text("{}", encoding="utf-8")
                link_handler._pending["oldest"].snapshot_path = oldest_snapshot
                await link_handler.handle_video_link_private(message)
                await link_handler.handle_video_link_private(message)

                self.assertEqual(list(link_handler._pending), ["middle", "newest"])
                self.assertFalse(oldest_snapshot.exists())

    async def test_pending_is_bounded_globally(self):
        """Fails if private menus can grow beyond the global 256-entry cap."""
        token_number = 0

        def next_uuid():
            nonlocal token_number
            token_number += 1
            return SimpleNamespace(hex=f"token-{token_number}")

        with (
            patch("handlers.link._video_url_from_text", AsyncMock(return_value=SOURCE_URL)),
            patch("handlers.link.uuid.uuid4", side_effect=next_uuid),
        ):
            for user_id in range(257):
                message = SimpleNamespace(
                    text=SOURCE_URL,
                    chat=SimpleNamespace(id=user_id),
                    answer=AsyncMock(),
                )
                await link_handler.handle_video_link_private(message)

        self.assertEqual(len(link_handler._pending), 256)
        self.assertNotIn("token-1", link_handler._pending)

    async def test_expiry_deletes_disk_snapshot_without_wall_clock_sleep(self):
        """Fails if TTL expiry removes metadata but leaves its info JSON on disk."""
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "pending.info.json"
            snapshot.write_text("{}", encoding="utf-8")
            pending = link_handler.PendingDownload(42, SOURCE_URL)
            pending.snapshot_path = snapshot
            link_handler._pending["token"] = pending

            with patch("handlers.link.asyncio.sleep", AsyncMock()):
                await link_handler._expire_pending("token")

            self.assertNotIn("token", link_handler._pending)
            self.assertFalse(snapshot.exists())

    async def test_large_quality_reinsert_gets_disk_snapshot_and_fresh_expiry(self):
        """Fails if the temporary-pop race reinserts a large item without a live TTL."""
        token = "large-quality"
        inspection = queue.MediaInspection(
            identity=queue.MediaIdentity(queue.MediaProvider.TIKTOK, "123"),
            source_url=SOURCE_URL,
            likes=9,
            description="desc",
            width=1080,
            height=1920,
            duration=12.5,
            qualities=(
                queue.VideoQuality(
                    "large-format",
                    "1080×1920 · 60 FPS",
                    queue.MAX_TELEGRAM_PART_BYTES + 1,
                ),
            ),
            info={"id": "123", "webpage_url": SOURCE_URL, "formats": []},
        )
        callback = SimpleNamespace(
            data=f"download:normal:{token}",
            from_user=SimpleNamespace(id=42),
            message=SimpleNamespace(
                delete=AsyncMock(),
                answer=AsyncMock(),
                edit_text=AsyncMock(),
                answer_photo=AsyncMock(),
            ),
            answer=AsyncMock(),
        )
        link_handler._pending[token] = link_handler.PendingDownload(42, SOURCE_URL)
        created_tasks = []

        class TaskStub:
            def __init__(self, coroutine):
                self.coroutine = coroutine
                self.cancelled = False

            def cancel(self):
                self.cancelled = True
                self.coroutine.close()

            def done(self):
                return self.cancelled

        def create_task(coroutine):
            task = TaskStub(coroutine)
            created_tasks.append(task)
            return task

        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(queue, "VIDEO_DIR", Path(directory)),
                patch("handlers.link.try_deliver_cached_video", AsyncMock(return_value=False)),
                patch("handlers.link.inspect_media", return_value=inspection),
                patch("handlers.link.asyncio.create_task", side_effect=create_task),
            ):
                await link_handler.handle_download_option(
                    callback,
                    SimpleNamespace(),
                    SimpleNamespace(),
                )
                pending = link_handler._pending[token]
                self.assertTrue(pending.snapshot_path.exists())
                self.assertEqual(pending.inspection.qualities, inspection.qualities)
                self.assertEqual(len(created_tasks), 1)

        for task in created_tasks:
            if not task.done():
                task.cancel()


if __name__ == "__main__":
    unittest.main()
