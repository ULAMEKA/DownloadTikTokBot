from dataclasses import dataclass

from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import VideoCache
from utils.links import cache_media_id


MEDIA_CACHE_PROFILE = "h264-aac-v2"


@dataclass(frozen=True)
class CachedVideo:
    cache_key: str
    file_id: str
    description: str | None
    source_url: str | None
    likes: int | None


def media_cache_key(media_id: str) -> str:
    return f"{MEDIA_CACHE_PROFILE}:{media_id}"


def is_invalid_cached_file_id_error(error: TelegramBadRequest) -> bool:
    message = str(error).casefold()
    return any(marker in message for marker in (
        "wrong file identifier",
        "file identifier is invalid",
        "file reference expired",
    ))


async def find_cached_video(
    session: AsyncSession,
    canonical_url: str,
) -> CachedVideo | None:
    media_id = cache_media_id(canonical_url)
    if media_id is None:
        return None
    return await find_cached_video_by_id(session, media_id, canonical_url)


async def find_cached_video_by_id(
    session: AsyncSession,
    media_id: str,
    fallback_url: str,
) -> CachedVideo | None:
    cache_key = media_cache_key(media_id)
    result = await session.execute(
        select(VideoCache).where(VideoCache.tiktok_id == cache_key)
    )
    cached = result.scalar_one_or_none()
    if cached is None or not cached.file_id:
        return None
    return CachedVideo(
        cache_key=cache_key,
        file_id=cached.file_id,
        description=cached.description,
        source_url=cached.source_url or fallback_url,
        likes=cached.likes,
    )


async def invalidate_cached_video(session: AsyncSession, cache_key: str) -> None:
    await session.execute(delete(VideoCache).where(VideoCache.tiktok_id == cache_key))
    await session.commit()
