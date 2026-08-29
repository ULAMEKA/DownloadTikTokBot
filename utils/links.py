import html
import re
from urllib.parse import urlsplit, urlunsplit

from gallery_dl.extractor.message import Message as GalleryMessage
from gallery_dl.extractor.tiktok import TiktokVmpostExtractor


SUPPORTED_URL_RE = re.compile(
    r"(https?://)?("
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


def find_video_url(text: str) -> str | None:
    match = SUPPORTED_URL_RE.search(text)
    if match is None:
        return None
    try:
        return clean_video_url(match.group(0))
    except ValueError:
        return None
