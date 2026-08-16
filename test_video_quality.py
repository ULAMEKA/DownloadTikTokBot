import unittest

from services.queue import (
    MAX_SOURCE_VIDEO_BYTES,
    MAX_TELEGRAM_PART_BYTES,
    VideoQuality,
    _next_segment_time_seconds,
    _quality_options,
    _segment_time_seconds,
    needs_quality_choice,
)


class VideoQualityTests(unittest.TestCase):
    def test_quality_options_keep_unique_h264_formats_under_limit(self):
        options = _quality_options([
            {
                "format_id": "low",
                "vcodec": "h264",
                "acodec": "aac",
                "width": 576,
                "height": 1024,
                "tbr": 500,
                "filesize": 20_000_000,
            },
            {
                "format_id": "low-duplicate",
                "vcodec": "h264",
                "acodec": "aac",
                "width": 576,
                "height": 1024,
                "tbr": 500,
                "filesize": 20_000_000,
            },
            {
                "format_id": "high",
                "vcodec": "h264",
                "acodec": "aac",
                "width": 576,
                "height": 1024,
                "tbr": 900,
                "filesize": 30_000_000,
            },
            {
                "format_id": "hevc",
                "vcodec": "h265",
                "acodec": "aac",
                "filesize": 10_000_000,
            },
            {
                "format_id": "too-large",
                "vcodec": "h264",
                "acodec": "aac",
                "filesize": MAX_SOURCE_VIDEO_BYTES + 1,
            },
            {
                "format_id": "unknown-size",
                "vcodec": "h264",
                "acodec": "aac",
            },
        ])

        self.assertEqual([option.format_id for option in options], ["high", "low"])

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
