import unittest

from caption import build_caption


class CaptionTests(unittest.TestCase):
    def test_source_includes_likes_without_promo(self):
        caption = build_caption(
            None,
            "https://www.tiktok.com/@creator/video/1?share=1",
            with_source=True,
            likes=12345,
        )

        self.assertEqual(
            caption,
            '🔗 <a href="https://www.tiktok.com/@creator/video/1">Source</a> | ❤️ 12 345',
        )
        self.assertNotIn("@dwlttbot", caption)


if __name__ == "__main__":
    unittest.main()
