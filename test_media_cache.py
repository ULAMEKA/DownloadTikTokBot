import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from aiogram.exceptions import TelegramBadRequest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from db.base import Base
from db.models import VideoCache
import services.media_cache as media_cache


class MediaIdentityTests(unittest.TestCase):
    def test_extracts_typed_supported_identities_without_network(self):
        """Fails if provider or provider-specific identifier validation is lost."""
        tiktok_video = media_cache.cache_media_identity(
            "https://www.tiktok.com/@a/video/123"
        )
        tiktok_photo = media_cache.cache_media_identity(
            "https://www.tiktok.com/@a/photo/456"
        )
        instagram_reel = media_cache.cache_media_identity(
            "https://www.instagram.com/reel/AbC_123/"
        )
        instagram_reels = media_cache.cache_media_identity(
            "https://www.instagram.com/reels/XYZ-9"
        )

        self.assertEqual(
            tiktok_video,
            media_cache.MediaIdentity(media_cache.MediaProvider.TIKTOK, "123"),
        )
        self.assertEqual(
            tiktok_photo,
            media_cache.MediaIdentity(media_cache.MediaProvider.TIKTOK, "456"),
        )
        self.assertEqual(
            instagram_reel,
            media_cache.MediaIdentity(media_cache.MediaProvider.INSTAGRAM, "AbC_123"),
        )
        self.assertEqual(
            instagram_reels,
            media_cache.MediaIdentity(media_cache.MediaProvider.INSTAGRAM, "XYZ-9"),
        )

    def test_rejects_non_numeric_tiktok_and_invalid_instagram_syntax(self):
        """Fails if malformed provider IDs can enter the cache namespace."""
        self.assertIsNone(
            media_cache.cache_media_identity(
                "https://www.tiktok.com/@a/video/instagram_shortcode"
            )
        )
        self.assertIsNone(
            media_cache.cache_media_identity("https://www.instagram.com/reel/bad%2Fid")
        )
        self.assertIsNone(
            media_cache.cache_media_identity("https://www.tiktok.com/@a/live")
        )

    def test_provider_keys_cannot_collide(self):
        """Fails if equal TikTok and Instagram IDs still share a cache row."""
        tiktok = media_cache.MediaIdentity(media_cache.MediaProvider.TIKTOK, "123")
        instagram = media_cache.MediaIdentity(media_cache.MediaProvider.INSTAGRAM, "123")

        self.assertEqual(media_cache.media_cache_key(tiktok), "h264-aac-v2:tiktok:123")
        self.assertEqual(media_cache.media_cache_key(instagram), "h264-aac-v2:instagram:123")
        self.assertNotEqual(
            media_cache.media_cache_key(tiktok),
            media_cache.media_cache_key(instagram),
        )

    def test_invalid_file_id_classifier_is_narrow(self):
        invalid = TelegramBadRequest(MagicMock(), "Bad Request: wrong file identifier/HTTP URL specified")
        unrelated = TelegramBadRequest(MagicMock(), "Bad Request: message is not modified")
        self.assertTrue(media_cache.is_invalid_cached_file_id_error(invalid))
        self.assertFalse(media_cache.is_invalid_cached_file_id_error(unrelated))


class ProviderAwareCacheLookupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        database_path = Path(self.temp_dir.name) / "cache.sqlite3"
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.temp_dir.cleanup()

    async def _insert_cache(self, cache_key: str, file_id: str):
        async with self.session_factory() as session:
            session.add(
                VideoCache(
                    tiktok_id=cache_key,
                    file_id=file_id,
                    storage_message_id=None,
                    description="desc",
                    music_tracks=None,
                    source_url=None,
                    likes=None,
                )
            )
            await session.commit()

    async def test_new_provider_key_precedes_legacy_tiktok_fallback(self):
        """Fails if a legacy TikTok row masks the new provider-aware row."""
        await self._insert_cache("h264-aac-v2:123", "legacy-file")
        await self._insert_cache("h264-aac-v2:tiktok:123", "provider-file")

        async with self.session_factory() as session:
            cached = await media_cache.find_cached_video(
                session,
                "https://www.tiktok.com/@a/video/123",
            )

        self.assertEqual(cached.file_id, "provider-file")
        self.assertEqual(cached.cache_key, "h264-aac-v2:tiktok:123")

    async def test_legacy_tiktok_row_still_hits(self):
        """Fails if migration to provider keys breaks existing TikTok cache rows."""
        await self._insert_cache("h264-aac-v2:123", "legacy-file")

        async with self.session_factory() as session:
            cached = await media_cache.find_cached_video(
                session,
                "https://www.tiktok.com/@a/video/123",
            )

        self.assertEqual(cached.file_id, "legacy-file")
        self.assertEqual(cached.cache_key, "h264-aac-v2:123")

    async def test_instagram_never_uses_legacy_tiktok_namespace(self):
        """Fails if an Instagram shortcode can collide with a legacy TikTok ID."""
        await self._insert_cache("h264-aac-v2:123", "legacy-tiktok-file")

        async with self.session_factory() as session:
            cached = await media_cache.find_cached_video(
                session,
                "https://www.instagram.com/reel/123",
            )

        self.assertIsNone(cached)


if __name__ == "__main__":
    unittest.main()
