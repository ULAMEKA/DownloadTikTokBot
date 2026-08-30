import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import handlers.link as link_handler
import services.queue as queue


SOURCE_URL = "https://www.tiktok.com/@a/video/123"


class MediaInspectionTests(unittest.TestCase):
    def test_inspection_uses_one_extract_and_sanitizes_info(self):
        ydl = MagicMock()
        ydl.__enter__.return_value = ydl
        ydl.extract_info.return_value = {
            "id": "123",
            "webpage_url": SOURCE_URL,
            "like_count": 9,
            "description": "desc",
            "formats": [],
        }
        ydl.sanitize_info.return_value = {
            "id": "123",
            "webpage_url": SOURCE_URL,
        }

        with patch.object(queue.yt_dlp, "YoutubeDL", return_value=ydl):
            inspection = queue.inspect_media(SOURCE_URL)

        ydl.extract_info.assert_called_once_with(SOURCE_URL, download=False)
        ydl.sanitize_info.assert_called_once_with(ydl.extract_info.return_value)
        self.assertEqual(inspection.media_id, "123")
        self.assertEqual(inspection.info["id"], "123")

    def test_inspection_from_snapshot_restores_application_fields(self):
        info = {
            "id": "123",
            "webpage_url": SOURCE_URL,
            "like_count": 9,
            "description": "desc",
            "width": 720,
            "height": 1280,
            "duration": 12.5,
            "formats": [
                {
                    "format_id": "combined",
                    "vcodec": "h264",
                    "acodec": "aac",
                    "width": 720,
                    "height": 1280,
                    "fps": 30,
                    "filesize": 20_000_000,
                }
            ],
        }

        inspection = queue._inspection_from_snapshot(SOURCE_URL, info)

        self.assertEqual(inspection.media_id, "123")
        self.assertEqual(inspection.source_url, SOURCE_URL)
        self.assertEqual(inspection.likes, 9)
        self.assertEqual(inspection.description, "desc")
        self.assertEqual((inspection.width, inspection.height), (720, 1280))
        self.assertEqual(inspection.duration, 12.5)
        self.assertEqual([quality.format_id for quality in inspection.qualities], ["combined"])
        self.assertIs(inspection.info, info)

    def test_snapshot_write_is_atomic_and_loads_only_json_objects(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            queue, "VIDEO_DIR", Path(directory)
        ), patch.object(queue.os, "replace", wraps=os.replace) as replace:
            path = queue.write_info_snapshot("job", {"id": "123"})

            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"id": "123"})
            self.assertEqual(queue._load_info_snapshot("job"), {"id": "123"})
            replace.assert_called_once()
            temporary_path, final_path = map(Path, replace.call_args.args)
            self.assertEqual(final_path, path)
            self.assertIn("job.", temporary_path.name)
            self.assertTrue(temporary_path.name.endswith(".info.tmp"))
            self.assertFalse(temporary_path.exists())

            path.write_text("[]", encoding="utf-8")
            self.assertIsNone(queue._load_info_snapshot("job"))

    def test_corrupt_snapshot_returns_none(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            queue, "VIDEO_DIR", Path(directory)
        ):
            (Path(directory) / "job.info.json").write_text("{broken", encoding="utf-8")

            self.assertIsNone(queue._load_info_snapshot("job"))

    def test_download_from_info_file_uses_snapshot_and_rejects_nonzero_exit(self):
        info = {
            "id": "123",
            "description": "desc",
            "width": 720,
            "height": 1280,
            "duration": 12.5,
        }
        with tempfile.TemporaryDirectory() as directory, patch.object(
            queue, "VIDEO_DIR", Path(directory)
        ):
            info_path = queue.write_info_snapshot("job", info)
            ydl = MagicMock()
            ydl.__enter__.return_value = ydl
            ydl.download_with_info_file.return_value = 0

            with patch.object(queue.yt_dlp, "YoutubeDL", return_value=ydl) as youtube_dl:
                metadata = queue._ydl_download_from_info_file(
                    SOURCE_URL,
                    info_path,
                    str(Path(directory) / "job.%(ext)s"),
                    "format-1",
                )

            self.assertEqual(metadata, ("123", 720, 1280, "desc", 12.5))
            ydl.download_with_info_file.assert_called_once_with(str(info_path))
            options = youtube_dl.call_args.args[0]
            self.assertEqual(options["format"], "format-1")
            self.assertEqual(options["max_filesize"], queue.MAX_SOURCE_VIDEO_BYTES)

            ydl.download_with_info_file.return_value = 1
            with patch.object(queue.yt_dlp, "YoutubeDL", return_value=ydl):
                with self.assertRaises(queue.MediaPreparationError):
                    queue._ydl_download_from_info_file(
                        SOURCE_URL,
                        info_path,
                        str(Path(directory) / "job.%(ext)s"),
                        None,
                    )

    def test_cleanup_removes_snapshot_remux_temp_and_part_directories(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            queue, "VIDEO_DIR", Path(directory)
        ):
            for name in ("job.info.json", "job.atomic.info.tmp", "job.telegram.mp4"):
                (Path(directory) / name).write_bytes(b"x")
            parts = Path(directory) / "job_parts_abc"
            parts.mkdir()
            (parts / "part_001.mp4").write_bytes(b"x")

            queue._remove_download_files("job")

            self.assertFalse(any(Path(directory).iterdir()))


class PrivateInspectionTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        link_handler._pending.clear()

    @staticmethod
    def _download_callback(token: str):
        return SimpleNamespace(
            data=f"download:normal:{token}",
            from_user=SimpleNamespace(id=42),
            message=SimpleNamespace(delete=AsyncMock(), edit_text=AsyncMock()),
            answer=AsyncMock(),
        )

    async def test_one_inspection_supplies_quality_choice_and_enqueued_snapshot(self):
        token = "private-inspection"
        inspection = queue.MediaInspection(
            media_id="123",
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
        link_handler._pending[token] = link_handler.PendingDownload(42, SOURCE_URL)
        callback = self._download_callback(token)

        with (
            patch("handlers.link.try_deliver_cached_video", AsyncMock(return_value=False)),
            patch("handlers.link.inspect_media", return_value=inspection) as inspect,
        ):
            await link_handler.handle_download_option(callback, SimpleNamespace(), SimpleNamespace())

        inspect.assert_called_once_with(SOURCE_URL)
        pending = link_handler._pending[token]
        self.assertIs(pending.inspection, inspection)
        self.assertEqual(pending.qualities, list(inspection.qualities))
        markup = callback.message.edit_text.await_args.kwargs["reply_markup"]
        self.assertEqual(markup.inline_keyboard[0][0].text, "1080×1920 · 60 FPS")

        placeholder = SimpleNamespace(
            message_id=55,
            delete=AsyncMock(),
            edit_caption=AsyncMock(),
        )
        quality_callback = SimpleNamespace(
            data=f"quality:{token}:0",
            from_user=SimpleNamespace(id=42),
            message=SimpleNamespace(
                answer_photo=AsyncMock(return_value=placeholder),
                delete=AsyncMock(),
            ),
            answer=AsyncMock(),
        )
        result = MagicMock()
        result.scalar_one.return_value = 1
        session = SimpleNamespace(execute=AsyncMock(return_value=result))
        captured = {}

        async def accept_enqueue(_session, item, user_id):
            item.id = 1
            captured["filename"] = item.filename
            captured["format_id"] = item.format_id
            captured["user_id"] = user_id
            captured["snapshot"] = queue._load_info_snapshot(item.filename)
            return True

        with tempfile.TemporaryDirectory() as directory, patch.object(
            queue, "VIDEO_DIR", Path(directory)
        ), patch("handlers.link.enqueue_download", side_effect=accept_enqueue):
            await link_handler.handle_quality_option(quality_callback, session)

        self.assertEqual(captured["format_id"], "large-format")
        self.assertEqual(captured["user_id"], "42")
        self.assertEqual(captured["snapshot"], inspection.info)

    async def test_enqueue_rejection_removes_the_written_snapshot(self):
        inspection = queue.MediaInspection(
            media_id="123",
            source_url=SOURCE_URL,
            likes=None,
            description=None,
            width=None,
            height=None,
            duration=None,
            qualities=(),
            info={"id": "123"},
        )
        pending = link_handler.PendingDownload(
            42,
            SOURCE_URL,
            mode="normal",
            inspection=inspection,
        )
        placeholder = SimpleNamespace(message_id=55, delete=AsyncMock())
        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=42),
            message=SimpleNamespace(answer_photo=AsyncMock(return_value=placeholder)),
            answer=AsyncMock(),
        )
        captured_filename = None

        async def reject_enqueue(_session, item, _user_id):
            nonlocal captured_filename
            captured_filename = item.filename
            return False

        with tempfile.TemporaryDirectory() as directory, patch.object(
            queue, "VIDEO_DIR", Path(directory)
        ), patch("handlers.link.enqueue_download", side_effect=reject_enqueue):
            await link_handler._start_pending_download(
                callback,
                SimpleNamespace(),
                "token",
                pending,
                format_id=None,
            )
            self.assertIsNotNone(captured_filename)
            self.assertIsNone(queue._load_info_snapshot(captured_filename))


if __name__ == "__main__":
    unittest.main()
