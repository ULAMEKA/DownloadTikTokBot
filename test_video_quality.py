import unittest

from services.queue import (
    MAX_SOURCE_VIDEO_BYTES,
    MAX_TELEGRAM_PART_BYTES,
    _quality_options,
    _segment_time_seconds,
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

    def test_segment_time_uses_a_safe_fraction_of_the_target_size(self):
        self.assertEqual(
            _segment_time_seconds(MAX_TELEGRAM_PART_BYTES * 4, 120),
            25,
        )
        self.assertEqual(_segment_time_seconds(MAX_TELEGRAM_PART_BYTES * 2, None), 30)


if __name__ == "__main__":
    unittest.main()
