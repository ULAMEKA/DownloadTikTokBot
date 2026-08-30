import unittest
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
)


class VideoQualityTests(unittest.TestCase):
    def test_list_video_qualities_remains_a_compatibility_wrapper(self):
        inspection = queue.MediaInspection(
            media_id="123",
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

    def test_tiktok_fallback_prefers_fps_then_resolution_and_matching_audio(self):
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

        self.assertEqual(candidates[0]["urls"], ["https://cdn/1080-60"])
        self.assertEqual(candidates[0]["fps"], 60)
        self.assertEqual(candidates[0]["audio_id"], "audio-60")
        self.assertEqual(
            _tiktok_audio_urls(post["video"], candidates[0]["audio_id"]),
            ["https://cdn/audio-60"],
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

        self.assertEqual(candidates[0]["urls"], ["https://cdn/720-60"])
        self.assertEqual(candidates[0]["fps"], 60)

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


if __name__ == "__main__":
    unittest.main()
