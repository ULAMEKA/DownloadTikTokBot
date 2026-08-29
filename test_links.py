import unittest
from unittest.mock import patch

from gallery_dl.extractor.message import Message as GalleryMessage

from utils.links import cache_media_id, canonicalize_video_url, clean_video_url, find_video_url


class LinkCleanupTests(unittest.TestCase):
    def test_tracking_and_escaped_underscores_are_removed(self):
        self.assertEqual(
            clean_video_url(
                r"https://www.tiktok.com/@creator/video/123?\_r=1&\_t=tracking"
            ),
            "https://www.tiktok.com/@creator/video/123",
        )

    def test_trailing_message_punctuation_is_not_part_of_url(self):
        self.assertEqual(
            find_video_url("Смотри https://vt.tiktok.com/ABC123/)."),
            "https://vt.tiktok.com/ABC123/",
        )
        self.assertEqual(
            find_video_url(
                "[https://vt.tiktok.com/ABC123/](https://vt.tiktok.com/ABC123/)"
            ),
            "https://vt.tiktok.com/ABC123/",
        )

    def test_spoofed_host_is_rejected(self):
        self.assertIsNone(
            find_video_url("https://www.tiktok.com.evil.example/@creator/video/123")
        )

    def test_short_tiktok_url_is_canonicalized(self):
        class FakeExtractor:
            def __iter__(self):
                yield (
                    GalleryMessage.Queue,
                    "https://www.tiktok.com/@creator/video/123?_r=1",
                    {},
                )

        with patch(
            "utils.links.TiktokVmpostExtractor.from_url",
            return_value=FakeExtractor(),
        ):
            self.assertEqual(
                canonicalize_video_url("https://vt.tiktok.com/ABC123/"),
                "https://www.tiktok.com/@creator/video/123",
            )

    def test_cache_media_id_rejects_unrelated_supported_urls(self):
        self.assertIsNone(cache_media_id("https://www.tiktok.com/@creator/live"))


if __name__ == "__main__":
    unittest.main()
