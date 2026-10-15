import html
import re
from urllib.parse import urlsplit, urlunsplit

from gallery_dl.extractor.message import Message as GalleryMessage
from gallery_dl.extractor.tiktok import TiktokVmpostExtractor


SUPPORTED_URL_RE = re.compile(
    r"(?<![\w.])(https?://)?("
    r"(www\.|vm\.|vt\.)?tiktok\.com/[^\s<>()\[\]{}]+"
    r"|(www\.)?instagram\.com/reels?/[^\s<>()\[\]{}]+"
    r")"
)

_TRAILING_URL_CHARS = ".,!?;:)]}>\"'»…"
_SUPPORTED_HOSTS = {
    "instagram.com",
    "tiktok.com",
    "vm.tiktok.com",
    "vt.tiktok.com",
    "www.instagram.com",
    "www.tiktok.com",
}
def clean_video_url(url: str) -> str:
    """Очищает пользовательскую ссылку до безопасного URL без трекинга."""
    url = html.unescape(url).strip().replace("\\_", "_")
    url = url.rstrip(_TRAILING_URL_CHARS)
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host not in _SUPPORTED_HOSTS:
        raise ValueError("Неподдерживаемый адрес видео")

    path = parts.path.rstrip(_TRAILING_URL_CHARS)
    return urlunsplit(("https", host, path, "", ""))


def canonicalize_video_url(url: str) -> str:
    """Разворачивает короткую TikTok-ссылку и возвращает очищенный URL."""
    cleaned = clean_video_url(url)
    parts = urlsplit(cleaned)
    host = (parts.hostname or "").lower()
    if host not in {"vm.tiktok.com", "vt.tiktok.com"} and not parts.path.startswith("/t/"):
        return cleaned

    current = cleaned
    for _ in range(3):
        extractor = TiktokVmpostExtractor.from_url(current)
        if extractor is None:
            break
        queued_url: str | None = None
        try:
            for message in extractor:
                if message[0] == GalleryMessage.Queue:
                    queued_url = str(message[1])
                    break
        except Exception:
            break
        if not queued_url:
            break
        current = clean_video_url(queued_url)
        if (urlsplit(current).hostname or "").lower() not in {
            "vm.tiktok.com",
            "vt.tiktok.com",
        }:
            return current
    return cleaned


def cache_media_id(url: str) -> str | None:
    """Compatibility wrapper; provider-aware identity lives in media_cache."""
    from services.media_cache import cache_media_identity

    identity = cache_media_identity(url)
    return identity.media_id if identity is not None else None


def find_video_url(text: str) -> str | None:
    match = SUPPORTED_URL_RE.search(text)
    if match is None:
        return None
    try:
        return clean_video_url(match.group(0))
    except ValueError:
        return None


_SHORT_LINK_HOSTS = {"vm.tiktok.com", "vt.tiktok.com"}
_TIKTOK_MEDIA_PATH_RE = re.compile(r"/(?:video|photo)/\d+/?$")
_URL_PREFIXES = tuple(
    f"{scheme}{host}/"
    for scheme in ("https://", "http://", "")
    for host in (
        "www.tiktok.com", "tiktok.com", "vm.tiktok.com", "vt.tiktok.com",
        "www.instagram.com/reel", "instagram.com/reel",
    )
)


def is_downloadable_video_url(url: str) -> bool:
    """Проверяет без сети, что очищенная ссылка указывает на конкретное видео."""
    from services.media_cache import cache_media_identity

    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host in _SHORT_LINK_HOSTS or parts.path.startswith("/t/"):
        return True
    if cache_media_identity(url) is not None:
        return True
    return host in {"tiktok.com", "www.tiktok.com"} and bool(
        _TIKTOK_MEDIA_PATH_RE.search(parts.path)
    )


def is_partial_video_url(text: str) -> bool:
    """True, если текст похож на ещё не дописанную ссылку на поддерживаемое видео."""
    text = text.strip().lower()
    return bool(text) and any(prefix.startswith(text) for prefix in _URL_PREFIXES)
