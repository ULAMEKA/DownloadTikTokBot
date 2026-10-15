import json
import re
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlsplit

from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import VideoCache


MEDIA_CACHE_PROFILE = "h264-aac-v2"
_TIKTOK_PATH_RE = re.compile(r"^/@[^/]+/(?:video|photo)/(\d+)/?$")
_INSTAGRAM_PATH_RE = re.compile(r"^/reels?/([A-Za-z0-9_-]+)/?$")


class MediaProvider(str, Enum):
    TIKTOK = "tiktok"
    INSTAGRAM = "instagram"


@dataclass(frozen=True)
class MediaIdentity:
    provider: MediaProvider
    media_id: str

    def __post_init__(self) -> None:
        valid = (
            self.media_id.isdigit()
            if self.provider is MediaProvider.TIKTOK
            else bool(re.fullmatch(r"[A-Za-z0-9_-]+", self.media_id))
        )
        if not valid:
            raise ValueError("Некорректный идентификатор медиа")


@dataclass(frozen=True)
class CachedVideo:
    cache_key: str
    file_id: str
    description: str | None
    source_url: str | None
    likes: int | None
    file_ids: tuple[str, ...] = ()
    storage_message_ids: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        normalized = tuple(
            value for value in self.file_ids
            if isinstance(value, str) and value
        )
        if not normalized and self.file_id:
            normalized = (self.file_id,)
        object.__setattr__(self, "file_ids", normalized)
        object.__setattr__(
            self,
            "storage_message_ids",
            tuple(
                value for value in self.storage_message_ids
                if isinstance(value, int) and not isinstance(value, bool)
            ),
        )

    @property
    def is_multipart(self) -> bool:
        return len(self.file_ids) > 1


@dataclass(frozen=True)
class VideoCacheUpdate:
    cache_key: str
    file_id: str
    storage_message_id: int | None
    description: str | None
    source_url: str
    likes: int | None
    file_ids: tuple[str, ...] = ()
    storage_message_ids: tuple[int, ...] = ()


def _normalized_file_ids(update: VideoCacheUpdate) -> tuple[str, ...]:
    return tuple(
        value for value in (update.file_ids or (update.file_id,))
        if isinstance(value, str) and value
    )


def _cache_file_ids_json(update: VideoCacheUpdate) -> str | None:
    file_ids = _normalized_file_ids(update)
    if len(file_ids) <= 1 and not update.storage_message_ids:
        return None
    payload: dict[str, object] = {"file_ids": list(file_ids)}
    if update.storage_message_ids:
        payload["storage_message_ids"] = list(update.storage_message_ids)
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _cached_parts(
    file_id: str | None,
    file_ids_json: object,
) -> tuple[tuple[str, ...], tuple[int, ...]]:
    if not isinstance(file_ids_json, str) or not file_ids_json:
        if isinstance(file_id, str) and file_id:
            return (file_id,), ()
        return (), ()

    try:
        payload = json.loads(file_ids_json)
    except (TypeError, ValueError, json.JSONDecodeError):
        return (), ()

    if isinstance(payload, list):
        raw_file_ids = payload
        raw_storage_ids: object = []
    elif isinstance(payload, dict):
        raw_file_ids = payload.get("file_ids")
        raw_storage_ids = payload.get("storage_message_ids", [])
    else:
        return (), ()

    if (
        not isinstance(raw_file_ids, list)
        or not raw_file_ids
        or any(not isinstance(value, str) or not value for value in raw_file_ids)
    ):
        return (), ()
    if (
        not isinstance(raw_storage_ids, list)
        or any(
            not isinstance(value, int) or isinstance(value, bool)
            for value in raw_storage_ids
        )
    ):
        return (), ()
    return tuple(raw_file_ids), tuple(raw_storage_ids)


def cache_media_identity(canonical_url: str) -> MediaIdentity | None:
    """Return a validated, provider-aware identity without network access."""
    parts = urlsplit(canonical_url)
    host = (parts.hostname or "").lower()
    if host in {"tiktok.com", "www.tiktok.com"}:
        match = _TIKTOK_PATH_RE.fullmatch(parts.path)
        provider = MediaProvider.TIKTOK
    elif host in {"instagram.com", "www.instagram.com"}:
        match = _INSTAGRAM_PATH_RE.fullmatch(parts.path)
        provider = MediaProvider.INSTAGRAM
    else:
        return None
    if match is None:
        return None
    return MediaIdentity(provider, match.group(1))


def media_identity_from_source(
    source_url: str,
    extracted_id: object = None,
) -> MediaIdentity | None:
    """Prefer canonical URL identity; only TikTok may use a numeric extractor ID."""
    identity = cache_media_identity(source_url)
    if identity is not None:
        return identity
    host = (urlsplit(source_url).hostname or "").lower()
    media_id = str(extracted_id) if extracted_id is not None else ""
    if host in {"tiktok.com", "www.tiktok.com"} and media_id.isdigit():
        return MediaIdentity(MediaProvider.TIKTOK, media_id)
    return None


def media_cache_key(identity: MediaIdentity) -> str:
    return f"{MEDIA_CACHE_PROFILE}:{identity.provider.value}:{identity.media_id}"


def _legacy_tiktok_cache_key(identity: MediaIdentity) -> str | None:
    if identity.provider is not MediaProvider.TIKTOK:
        return None
    return f"{MEDIA_CACHE_PROFILE}:{identity.media_id}"


def media_identity_from_cache_key(cache_key: str) -> MediaIdentity | None:
    """Восстанавливает identity из ключа кеша (включая legacy TikTok-ключи)."""
    profile, separator, rest = cache_key.partition(":")
    if profile != MEDIA_CACHE_PROFILE or not separator:
        return None
    provider_name, separator, media_id = rest.partition(":")
    try:
        if not separator:
            return MediaIdentity(MediaProvider.TIKTOK, provider_name)
        return MediaIdentity(MediaProvider(provider_name), media_id)
    except ValueError:
        return None


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
    identity = cache_media_identity(canonical_url)
    if identity is None:
        return None
    return await find_cached_video_by_identity(session, identity, canonical_url)


async def find_cached_video_by_identity(
    session: AsyncSession,
    identity: MediaIdentity,
    fallback_url: str,
) -> CachedVideo | None:
    cache_keys = [media_cache_key(identity)]
    legacy_key = _legacy_tiktok_cache_key(identity)
    if legacy_key is not None:
        cache_keys.append(legacy_key)
    for cache_key in cache_keys:
        result = await session.execute(
            select(VideoCache).where(VideoCache.tiktok_id == cache_key)
        )
        cached = result.scalar_one_or_none()
        if cached is None:
            continue
        file_ids, storage_message_ids = _cached_parts(cached.file_id, cached.file_ids)
        if not file_ids:
            continue
        return CachedVideo(
            cache_key=cache_key,
            file_id=file_ids[0],
            description=cached.description,
            source_url=cached.source_url or fallback_url,
            likes=cached.likes,
            file_ids=file_ids,
            storage_message_ids=storage_message_ids,
        )
    return None


async def invalidate_cached_video(session: AsyncSession, cache_key: str) -> None:
    await session.execute(delete(VideoCache).where(VideoCache.tiktok_id == cache_key))
    await session.commit()


async def upsert_cached_video(
    session: AsyncSession,
    update: VideoCacheUpdate,
) -> None:
    result = await session.execute(
        select(VideoCache).where(VideoCache.tiktok_id == update.cache_key)
    )
    cached = result.scalar_one_or_none()
    if cached is None:
        cached = VideoCache(tiktok_id=update.cache_key, music_tracks=None)
        session.add(cached)

    file_ids = _normalized_file_ids(update)
    cached.file_id = file_ids[0] if file_ids else update.file_id
    cached.storage_message_id = update.storage_message_id
    cached.description = update.description
    cached.source_url = update.source_url
    cached.likes = update.likes
    cached.file_ids = _cache_file_ids_json(update)
