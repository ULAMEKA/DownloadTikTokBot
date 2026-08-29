import unittest
from unittest.mock import MagicMock

from aiogram.exceptions import TelegramBadRequest
from services.media_cache import is_invalid_cached_file_id_error, media_cache_key
from utils.links import cache_media_id


class MediaIdentityTests(unittest.TestCase):
    def test_extracts_supported_canonical_ids_without_network(self):
        self.assertEqual(cache_media_id("https://www.tiktok.com/@a/video/123"), "123")
        self.assertEqual(cache_media_id("https://www.tiktok.com/@a/photo/456"), "456")
        self.assertEqual(cache_media_id("https://www.instagram.com/reel/AbC_123/"), "AbC_123")
        self.assertEqual(cache_media_id("https://www.instagram.com/reels/XYZ-9"), "XYZ-9")

    def test_unknown_path_has_no_cache_identity(self):
        self.assertIsNone(cache_media_id("https://www.tiktok.com/@a/live"))

    def test_invalid_file_id_classifier_is_narrow(self):
        invalid = TelegramBadRequest(MagicMock(), "Bad Request: wrong file identifier/HTTP URL specified")
        unrelated = TelegramBadRequest(MagicMock(), "Bad Request: message is not modified")
        self.assertTrue(is_invalid_cached_file_id_error(invalid))
        self.assertFalse(is_invalid_cached_file_id_error(unrelated))

    def test_cache_key_keeps_existing_profile(self):
        self.assertEqual(media_cache_key("123"), "h264-aac-v2:123")
