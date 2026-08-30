import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import services.queue as queue

from services.queue import (
    MAX_SOURCE_VIDEO_BYTES,
    MAX_TELEGRAM_PART_BYTES,
    VideoQuality,
    _next_segment_time_seconds,
    _quality_options,
    _segment_time_seconds,
    _tiktok_audio_urls,
    _tiktok_video_candidates,
    _ydl_base_options,
    needs_quality_choice,
    select_fast_h264_format,
)


class VideoQualityTests(unittest.TestCase):
    def test_fast_h264_selector_prefers_combined_aac_before_separate_streams(self):
        formats = [
            {"format_id": "hevc", "vcodec": "h265", "acodec": "aac", "fps": 60,
             "width": 1080, "height": 1920, "tbr": 1200, "filesize": 20_000_000},
            {"format_id": "h264-aac", "vcodec": "h264", "acodec": "aac", "fps": 30,
             "width": 576, "height": 1024, "tbr": 381, "filesize": 8_400_000},
            {"format_id": "h264-video", "vcodec": "h264", "acodec": "none", "fps": 60,
             "width": 720, "height": 1280, "tbr": 600, "filesize": 9_000_000},
            {"format_id": "aac", "vcodec": "none", "acodec": "aac", "abr": 128,
             "filesize": 1_000_000},
        ]

        self.assertEqual(select_fast_h264_format(formats), "h264-aac")

    def test_fast_h264_selector_uses_best_aac_when_no_combined_mp4_exists(self):
        formats = [
            {"format_id": "video", "vcodec": "avc1.640028", "acodec": "none", "fps": 60,
             "width": 720, "height": 1280, "tbr": 700, "filesize": 9_000_000},
            {"format_id": "aac-low", "vcodec": "none", "acodec": "aac", "abr": 64,
             "filesize": 400_000},
            {"format_id": "aac-high", "vcodec": "none", "acodec": "aac", "abr": 128,
             "filesize": 800_000},
        ]

        self.assertEqual(select_fast_h264_format(formats), "video+aac-high")

    def test_fast_h264_selector_returns_none_for_hevc_only(self):
        self.assertIsNone(select_fast_h264_format([
            {"format_id": "hevc", "vcodec": "h265", "acodec": "aac"},
        ]))

    def test_list_video_qualities_remains_a_compatibility_wrapper(self):
        inspection = queue.MediaInspection(
            identity=queue.MediaIdentity(queue.MediaProvider.TIKTOK, "123"),
            source_url="https://www.tiktok.com/@a/video/123",
            likes=None,
            description=None,
            width=None,
            height=None,
            duration=None,
            qualities=(VideoQuality("format", "quality", 1),),
            info={"id": "123"},
        )

        with patch.object(queue, "inspect_media", return_value=inspection) as inspect:
            qualities = queue.list_video_qualities(inspection.source_url)

        inspect.assert_called_once_with(inspection.source_url)
        self.assertEqual(qualities, list(inspection.qualities))

    def test_ytdlp_prioritizes_source_fps_before_resolution(self):
        self.assertEqual(_ydl_base_options()["format_sort"], ["fps", "res", "br"])

    def test_quality_options_keep_best_video_and_attach_best_audio(self):
        options = _quality_options([
            {
                "format_id": "audio",
                "vcodec": "none",
                "acodec": "aac",
                "abr": 192,
                "filesize": 2_000_000,
            },
            {
                "format_id": "combined",
                "vcodec": "h264",
                "acodec": "aac",
                "width": 720,
                "height": 1280,
                "fps": 30,
                "tbr": 900,
                "filesize": 20_000_000,
            },
            {
                "format_id": "best-video",
                "vcodec": "h265",
                "acodec": "none",
                "width": 1080,
                "height": 1920,
                "fps": 60,
                "tbr": 1800,
                "filesize": 30_000_000,
            },
            {
                "format_id": "best-video-duplicate",
                "vcodec": "h265",
                "acodec": "none",
                "width": 1080,
                "height": 1920,
                "fps": 60,
                "tbr": 1800,
                "filesize": 30_000_000,
            },
            {
                "format_id": "too-large",
                "vcodec": "h264",
                "acodec": "aac",
                "filesize": MAX_SOURCE_VIDEO_BYTES + 1,
            },
        ])

        self.assertEqual(
            [option.format_id for option in options],
            ["best-video+audio", "combined"],
        )
        self.assertIn("60 FPS", options[0].label)
        self.assertEqual(options[0].filesize, 32_000_000)

    def test_tiktok_fallback_filters_non_h264_before_ranking_and_matching_audio(self):
        post = {
            "video": {
                "bitrateInfo": [
                    {
                        "Bitrate": 900,
                        "BitrateFPS": 60,
                        "CodecType": "h264",
                        "PlayAddr": {
                            "Width": 720,
                            "Height": 1280,
                            "DataSize": 10,
                            "UrlList": ["https://cdn/720"],
                        },
                        "VideoExtra": '{"audio_file_id":"audio-low"}',
                    },
                    {
                        "Bitrate": 1600,
                        "BitrateFPS": 30,
                        "CodecType": "h265_hvc1",
                        "PlayAddr": {
                            "Width": 1080,
                            "Height": 1920,
                            "DataSize": 20,
                            "UrlList": ["https://cdn/1080"],
                        },
                        "VideoExtra": '{"audio_file_id":"audio-high"}',
                    },
                    {
                        "Bitrate": 1500,
                        "BitrateFPS": 60,
                        "CodecType": "h265_hvc1",
                        "PlayAddr": {
                            "Width": 1080,
                            "Height": 1920,
                            "DataSize": 21,
                            "UrlList": ["https://cdn/1080-60"],
                        },
                        "VideoExtra": '{"audio_file_id":"audio-60"}',
                    },
                ],
                "bitrateAudioInfo": [
                    {
                        "FileId": "audio-low",
                        "Bitrate": 64,
                        "UrlList": {"MainUrl": "https://cdn/audio-low"},
                    },
                    {
                        "FileId": "audio-high",
                        "Bitrate": 128,
                        "UrlList": {"MainUrl": "https://cdn/audio-high"},
                    },
                    {
                        "FileId": "audio-60",
                        "Bitrate": 128,
                        "UrlList": {"MainUrl": "https://cdn/audio-60"},
                    },
                ],
            },
        }

        candidates = _tiktok_video_candidates(post)

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["urls"], ["https://cdn/720"])
        self.assertEqual(candidates[0]["fps"], 60)
        self.assertEqual(candidates[0]["audio_id"], "audio-low")
        self.assertEqual(
            _tiktok_audio_urls(post["video"], candidates[0]["audio_id"]),
            ["https://cdn/audio-low"],
        )

    def test_tiktok_fallback_does_not_trade_sixty_fps_for_resolution(self):
        post = {
            "video": {
                "bitrateInfo": [
                    {
                        "Bitrate": 2000,
                        "BitrateFPS": 30,
                        "CodecType": "h265_hvc1",
                        "PlayAddr": {
                            "Width": 1080,
                            "Height": 1920,
                            "UrlList": ["https://cdn/1080-30"],
                        },
                    },
                    {
                        "Bitrate": 1200,
                        "BitrateFPS": 60,
                        "CodecType": "h264",
                        "PlayAddr": {
                            "Width": 720,
                            "Height": 1280,
                            "UrlList": ["https://cdn/720-60"],
                        },
                    },
                ],
            },
        }

        candidates = _tiktok_video_candidates(post)

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["urls"], ["https://cdn/720-60"])
        self.assertEqual(candidates[0]["fps"], 60)

    def test_tiktok_external_audio_is_not_muxed_until_downloaded_video_is_h264(self):
        post = {
            "post_type": "video",
            "id": "123",
            "video": {
                "bitrateInfo": [{
                    "CodecType": "h264",
                    "PlayAddr": {"UrlList": ["https://cdn/video"]},
                    "VideoExtra": '{"audio_file_id":"audio"}',
                }],
                "bitrateAudioInfo": [{
                    "FileId": "audio",
                    "UrlList": {"MainUrl": "https://cdn/audio"},
                }],
            },
        }
        hevc_probe = queue.MediaProbe(
            has_video=True,
            has_audio=False,
            video_codec="hevc",
        )
        h264_probe = queue.MediaProbe(
            has_video=True,
            has_audio=True,
            video_codec="h264",
            audio_codec="aac",
        )

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "video.mp4"
            with (
                patch.object(queue, "_download_gallery_stream"),
                patch.object(queue, "_probe_media", side_effect=[hevc_probe, h264_probe]),
                patch.object(queue, "_mux_external_audio") as mux_audio,
            ):
                with self.assertRaises(queue.MediaPreparationError):
                    queue._download_tiktok_post(object(), post, output)

            mux_audio.assert_not_called()

    def test_generic_gallery_rejects_non_h264_video_before_moving_it(self):
        probe = queue.MediaProbe(
            has_video=True,
            has_audio=True,
            video_codec="hevc",
            audio_codec="aac",
        )

        with tempfile.TemporaryDirectory() as directory, patch.object(
            queue,
            "VIDEO_DIR",
            Path(directory),
        ):
            output = Path(directory) / "job.mp4"

            def gallery_cli(command, **_kwargs):
                work_dir = Path(command[command.index("--directory") + 1])
                (work_dir / "1_video.mp4").write_bytes(b"video")
                return SimpleNamespace(returncode=0)

            with (
                patch.object(queue, "_gallery_tiktok_video_download", return_value=None),
                patch.object(queue, "cookie_files_for_url", return_value=[]),
                patch.object(queue.subprocess, "run", side_effect=gallery_cli),
                patch.object(queue, "_probe_media", return_value=probe),
            ):
                with self.assertRaises(queue.NoFastH264FormatError):
                    queue._gallery_download(
                        "https://www.instagram.com/reel/AbC_123",
                        output,
                    )

            self.assertFalse(output.exists())

    def test_quality_menu_is_only_needed_for_large_sources(self):
        self.assertFalse(needs_quality_choice([
            VideoQuality("small", "small", MAX_TELEGRAM_PART_BYTES),
            VideoQuality("unknown", "unknown", None),
        ]))
        self.assertTrue(needs_quality_choice([
            VideoQuality("large", "large", MAX_TELEGRAM_PART_BYTES + 1),
        ]))

    def test_segment_time_targets_about_fifty_megabytes_and_retries_proportionally(self):
        self.assertEqual(
            _segment_time_seconds(MAX_TELEGRAM_PART_BYTES * 4, 120),
            29,
        )
        self.assertEqual(_segment_time_seconds(MAX_TELEGRAM_PART_BYTES * 2, None), 30)
        self.assertEqual(
            _next_segment_time_seconds(60, MAX_TELEGRAM_PART_BYTES * 2),
            28,
        )

    def test_split_reports_real_ffmpeg_attempt_count(self):
        """Fails if adaptive split retries are absent from required telemetry."""
        with tempfile.TemporaryDirectory() as directory, patch.object(
            queue,
            "VIDEO_DIR",
            Path(directory),
        ), patch.object(queue, "MAX_TELEGRAM_PART_BYTES", 10):
            video = Path(directory) / "large.mp4"
            video.write_bytes(b"x" * 100)
            ffmpeg_calls = 0

            def split_once(command, **_kwargs):
                nonlocal ffmpeg_calls
                ffmpeg_calls += 1
                output_dir = Path(command[-1]).parent
                size = 20 if ffmpeg_calls == 1 else 5
                (output_dir / "part_000.mp4").write_bytes(b"x" * size)

            with patch.object(queue.subprocess, "run", side_effect=split_once):
                outcome = queue._split_video(video, duration=10.0)

            self.assertEqual(outcome.attempts, 2)
            self.assertEqual(len(outcome.parts), 1)
            self.assertEqual(outcome.parts[0].stat().st_size, 5)

    def test_exhausted_split_reports_all_six_attempts(self):
        """Fails if failed split jobs log zero attempts after six FFmpeg runs."""
        with tempfile.TemporaryDirectory() as directory, patch.object(
            queue,
            "VIDEO_DIR",
            Path(directory),
        ), patch.object(queue, "MAX_TELEGRAM_PART_BYTES", 10):
            video = Path(directory) / "large.mp4"
            video.write_bytes(b"x" * 100)

            def always_oversized(command, **_kwargs):
                output_dir = Path(command[-1]).parent
                (output_dir / "part_000.mp4").write_bytes(b"x" * 20)

            with patch.object(queue.subprocess, "run", side_effect=always_oversized):
                with self.assertRaises(queue.VideoSplitError) as captured:
                    queue._split_video(video, duration=10.0)

            self.assertEqual(captured.exception.attempts, 6)


if __name__ == "__main__":
    unittest.main()
