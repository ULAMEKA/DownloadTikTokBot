import asyncio
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import imageio_ffmpeg
import yt_dlp
from aiogram import Bot, html
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaVideo
from sqlalchemy import case, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from gallery_dl.extractor.message import Message as GalleryMessage
from gallery_dl.extractor.tiktok import TiktokPostExtractor, TiktokVmpostExtractor

from db.session import SessionLocal
from db.models import DownloadQueue, MusicRequest, VideoCache
from caption import build_caption, source_link
from promo import AD_DISPLAY_SECONDS
from services.media_cache import (
    CachedVideo,
    find_cached_video,
    invalidate_cached_video,
    is_invalid_cached_file_id_error,
)
from utils.media_cookies import cookie_files_for_url

BASE_DIR = Path(__file__).parent
PARENT_DIR = BASE_DIR.parent
VIDEO_DIR = PARENT_DIR / "videos"
VIDEO_DIR.mkdir(exist_ok=True)
PHOTO_DURATION = 2.5
SLIDESHOW_WIDTH = 720
SLIDESHOW_HEIGHT = 1280
FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()
FFPROBE_EXE = shutil.which("ffprobe")

# ID канала-хранилища (отрицательное число для каналов, например -1001234567890)
STORAGE_CHAT_ID: str | None = os.getenv("STORAGE_CHAT_ID")

# Regex для извлечения ID видео из полного URL TikTok
_TIKTOK_ID_RE = re.compile(r'/(?:video|photo)/(\d+)')

_IMAGE_EXTENSIONS = {".avif", ".jpeg", ".jpg", ".png", ".webp"}
_AUDIO_EXTENSIONS = {".aac", ".m4a", ".mp3", ".ogg", ".opus", ".wav"}
_VIDEO_EXTENSIONS = {".m4v", ".mkv", ".mov", ".mp4", ".webm"}
DEFAULT_FORMAT = "bestvideo*+bestaudio/best"
MAX_SOURCE_VIDEO_BYTES = 1_000_000_000
MAX_TELEGRAM_PART_BYTES = 48_000_000
MAX_QUALITY_OPTIONS = 6
MAX_ACTIVE_DOWNLOADS_PER_USER = 2
MUSIC_CHUNK_SECONDS = 15
MUSIC_RECOGNITION_TIMEOUT_SECONDS = 15
MUSIC_MESSAGE_LIMIT = 4096
MUSIC_JOB_PREFIX = "music:"
MUSIC_PAGE_PREFIX = "music_page:"
MUSIC_START_PREFIX = "music_"
QUEUE_WAKEUP_TIMEOUT_SECONDS = 60.0
MEDIA_CACHE_PROFILE = "h264-aac-v2"
VIDEO_TRANSCODE_CRF = 18
QUEUE_LIMIT_MESSAGE = (
    "Можно поставить в очередь не больше 2 видео одновременно. "
    "Дождись завершения текущих загрузок."
)
_placeholder_ready_at: dict[tuple[str, int], float] = {}
_inline_placeholder_ready_at: dict[str, float] = {}
_enqueue_lock = asyncio.Lock()
_queue_wakeup = asyncio.Event()
_ydl_lock = threading.Lock()
_music_bot_username_cache: str | None = None
_music_bot_username_lock = asyncio.Lock()


@dataclass(frozen=True)
class VideoQuality:
    format_id: str | None
    label: str
    filesize: int | None


@dataclass(frozen=True)
class MediaInspection:
    media_id: str | None
    source_url: str
    likes: int | None
    description: str | None
    width: int | None
    height: int | None
    duration: float | None
    qualities: tuple[VideoQuality, ...]
    info: dict[str, object]


@dataclass(frozen=True)
class MediaProbe:
    has_video: bool
    has_audio: bool
    video_codec: str | None = None
    audio_codec: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    duration: float | None = None
    pixel_format: str | None = None
    sample_aspect_ratio: str | None = None
    rotation: int = 0


@dataclass(frozen=True)
class QueueJob:
    id: int
    chat_id: str
    user_id: str | None
    filename: str
    tiktok_link: str
    format_id: str | None
    reply_to_message_id: int | None
    inline_message_id: str | None
    enqueued_at: datetime | None

    @property
    def is_music(self) -> bool:
        return bool(
            self.inline_message_id
            and self.inline_message_id.startswith(MUSIC_JOB_PREFIX)
        )


MusicTrack = tuple[int, str, str]


class VideoTooLargeError(Exception):
    pass


class VideoSplitError(Exception):
    pass


class InlineSplitError(Exception):
    pass


class MissingAudioError(Exception):
    pass


class MediaPreparationError(Exception):
    pass


async def enqueue_download(
    session: AsyncSession,
    item: DownloadQueue,
    user_id: str,
) -> bool:
    async with _enqueue_lock:
        result = await session.execute(
            select(func.count())
            .select_from(DownloadQueue)
            .where(DownloadQueue.user_id == user_id)
        )
        if result.scalar_one() >= MAX_ACTIVE_DOWNLOADS_PER_USER:
            return False

        item.user_id = user_id
        session.add(item)
        await session.commit()
        notify_queue_enqueued()
        return True


def notify_queue_enqueued() -> None:
    _queue_wakeup.set()


def hold_placeholder(chat_id: str, message_id: int) -> None:
    _placeholder_ready_at[(chat_id, message_id)] = (
        asyncio.get_running_loop().time() + AD_DISPLAY_SECONDS
    )


def hold_inline_placeholder(inline_message_id: str) -> None:
    _inline_placeholder_ready_at[inline_message_id] = (
        asyncio.get_running_loop().time() + AD_DISPLAY_SECONDS
    )


def _to_int(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _to_float(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _format_megabytes(value: int | None) -> str:
    return f"{value / 1_000_000:.0f} МБ" if value is not None else "размер неизвестен"


def _is_h264(vcodec: object) -> bool:
    return isinstance(vcodec, str) and vcodec.lower().startswith(("h264", "avc"))


def _has_video(item: dict[str, object]) -> bool:
    return item.get("vcodec") not in (None, "none")


def _has_audio(item: dict[str, object]) -> bool:
    return item.get("acodec") not in (None, "none")


def _audio_quality_key(item: dict[str, object]) -> tuple[int, int, int]:
    return (
        _to_int(item.get("abr")) or 0,
        _to_int(item.get("tbr")) or 0,
        _to_int(item.get("filesize") or item.get("filesize_approx")) or 0,
    )


def _codec_label(codec: object) -> str | None:
    if not isinstance(codec, str):
        return None
    value = codec.lower()
    if value.startswith(("h264", "avc")):
        return "H.264"
    if value.startswith(("h265", "hev", "hvc")):
        return "H.265"
    return codec.split(".", 1)[0].upper()


def _quality_options(formats: object) -> list[VideoQuality]:
    if not isinstance(formats, list):
        return []

    typed_formats = [item for item in formats if isinstance(item, dict)]
    audio_formats = [
        item
        for item in typed_formats
        if not _has_video(item) and _has_audio(item) and isinstance(item.get("format_id"), str)
    ]
    best_audio = max(audio_formats, key=_audio_quality_key, default=None)

    ranked_options: list[tuple[tuple[float, int, int, int], VideoQuality]] = []
    seen: set[tuple[object, object, object, object, int | None]] = set()
    for item in formats:
        if not isinstance(item, dict) or not _has_video(item):
            continue
        format_id = item.get("format_id")
        if not isinstance(format_id, str):
            continue

        audio_size = 0
        if not _has_audio(item):
            if best_audio is None:
                continue
            audio_format_id = best_audio.get("format_id")
            if not isinstance(audio_format_id, str):
                continue
            format_id = f"{format_id}+{audio_format_id}"
            audio_size = _to_int(
                best_audio.get("filesize") or best_audio.get("filesize_approx")
            ) or 0

        filesize = _to_int(item.get("filesize") or item.get("filesize_approx"))
        if filesize is not None:
            filesize += audio_size
        if filesize is not None and filesize > MAX_SOURCE_VIDEO_BYTES:
            continue
        width = _to_int(item.get("width"))
        height = _to_int(item.get("height"))
        bitrate = _to_int(item.get("tbr"))
        fps = _to_float(item.get("fps"))
        key = (width, height, fps, bitrate, filesize)
        if key in seen:
            continue
        seen.add(key)
        resolution = f"{width}×{height}" if width and height else "Видео"
        fps_label = f"{fps:g} FPS" if fps else None
        codec_label = _codec_label(item.get("vcodec"))
        bitrate_label = f"{bitrate} кбит/с" if bitrate else None
        label = " · ".join(part for part in (
            resolution,
            fps_label,
            codec_label,
            bitrate_label,
            _format_megabytes(filesize),
        ) if part)
        rank = (
            fps or 0.0,
            (width or 0) * (height or 0),
            bitrate or 0,
            filesize or 0,
        )
        ranked_options.append((rank, VideoQuality(format_id, label[:64], filesize)))

    ranked_options.sort(key=lambda option: option[0], reverse=True)
    return [option for _, option in ranked_options[:MAX_QUALITY_OPTIONS]]


def _inspection_from_snapshot(url: str, info: dict[str, object]) -> MediaInspection:
    match = _TIKTOK_ID_RE.search(url)
    fallback_id = match.group(1) if match else None
    media_id = str(info["id"]) if info.get("id") else fallback_id
    source_url = (
        str(info.get("webpage_url") or url)
        if "tiktok.com/" in url.lower()
        else url
    )
    raw_description = info.get("description")
    width, height = _extract_dimensions(info)
    return MediaInspection(
        media_id=media_id,
        source_url=source_url,
        likes=_to_int(info.get("like_count")),
        description=str(raw_description) if raw_description else None,
        width=width,
        height=height,
        duration=_to_float(info.get("duration")),
        qualities=tuple(_quality_options(info.get("formats"))),
        info=info,
    )


def inspect_media(url: str) -> MediaInspection:
    last_error: Exception | None = None
    with _ydl_option_variants(url) as variants:
        for index, opts in enumerate(variants):
            try:
                with _ydl_lock, yt_dlp.YoutubeDL(opts) as ydl:  # type: ignore[arg-type]
                    info = ydl.extract_info(url, download=False)
                    if not isinstance(info, dict):
                        raise MediaPreparationError("yt-dlp не вернул метаданные")
                    sanitized = ydl.sanitize_info(info)
                if not isinstance(sanitized, dict):
                    raise MediaPreparationError("yt-dlp вернул некорректные метаданные")
                return _inspection_from_snapshot(url, sanitized)
            except Exception as error:
                last_error = error
                if index + 1 < len(variants):
                    logging.info("yt-dlp: повторяем получение метаданных с cookies")
    if last_error is not None:
        raise last_error
    raise MediaPreparationError("yt-dlp не вернул метаданные")


def list_video_qualities(url: str) -> list[VideoQuality]:
    return list(inspect_media(url).qualities)


def needs_quality_choice(qualities: list[VideoQuality]) -> bool:
    """Only ask when the available source is too large for one Telegram video."""
    return any(
        quality.filesize is not None and quality.filesize > MAX_TELEGRAM_PART_BYTES
        for quality in qualities
    )


class _YtdlpLogger:
    def debug(self, message: str) -> None:
        logging.debug("yt-dlp: %s", message)

    def info(self, message: str) -> None:
        logging.debug("yt-dlp: %s", message)

    def warning(self, message: str) -> None:
        logging.info("yt-dlp: %s", message)

    def error(self, message: str) -> None:
        logging.debug("yt-dlp: %s", message)


def _ydl_base_options(cookie_file: Path | None = None) -> dict[str, object]:
    options: dict[str, object] = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "logger": _YtdlpLogger(),
        "noplaylist": True,
        "retries": 5,
        "fragment_retries": 5,
        "extractor_retries": 3,
        "socket_timeout": 30,
        "ffmpeg_location": FFMPEG_EXE,
        # Не меняем 60 FPS на 30 ради разрешения; затем берём максимум детализации.
        "format_sort": ["fps", "res", "br"],
    }
    if cookie_file is not None:
        options["cookiefile"] = str(cookie_file)
    return options


@contextmanager
def _ydl_option_variants(url: str):
    variants = [_ydl_base_options()]
    cookie_copies: list[Path] = []
    for cookie_file in cookie_files_for_url(url):
        descriptor, temporary_name = tempfile.mkstemp(
            dir=VIDEO_DIR,
            prefix="media_cookies_",
            suffix=".txt",
        )
        os.close(descriptor)
        cookie_copy = Path(temporary_name)
        try:
            shutil.copyfile(cookie_file, cookie_copy)
            variants.append(_ydl_base_options(cookie_copy))
            cookie_copies.append(cookie_copy)
        except OSError as error:
            logging.warning("Не удалось подготовить cookie-файл: %s", error)
            cookie_copy.unlink(missing_ok=True)
    try:
        yield variants
    finally:
        for cookie_copy in cookie_copies:
            cookie_copy.unlink(missing_ok=True)


def _info_snapshot_path(filename: str) -> Path:
    return VIDEO_DIR / f"{filename}.info.json"


def write_info_snapshot(filename: str, info: dict[str, object]) -> Path:
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=VIDEO_DIR,
        prefix=f"{filename}.",
        suffix=".info.tmp",
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    destination = _info_snapshot_path(filename)
    try:
        temporary_path.write_text(
            json.dumps(info, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(temporary_path, destination)
        return destination
    finally:
        temporary_path.unlink(missing_ok=True)


def _load_info_snapshot(filename: str) -> dict[str, object] | None:
    try:
        loaded = json.loads(_info_snapshot_path(filename).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _media_cache_key(media_id: str) -> str:
    return f"{MEDIA_CACHE_PROFILE}:{media_id}"


def _resolve_media(url: str) -> tuple[str | None, str, int | None]:
    m = _TIKTOK_ID_RE.search(url)
    fallback_id = m.group(1) if m else None
    with _ydl_option_variants(url) as variants:
        for index, opts in enumerate(variants):
            try:
                with _ydl_lock, yt_dlp.YoutubeDL(opts) as ydl:  # type: ignore[arg-type]
                    info = ydl.extract_info(url, download=False)
                    if info:
                        source_url = (
                            str(info.get("webpage_url") or url)
                            if "tiktok.com/" in url.lower()
                            else url
                        )
                        media_id = str(info["id"]) if info.get("id") else fallback_id
                        return media_id, source_url, _to_int(info.get("like_count"))
            except Exception:
                if index + 1 < len(variants):
                    logging.info("yt-dlp: повторяем получение метаданных с cookies")
    return fallback_id, url, None


def _extract_dimensions(info: dict[str, object]) -> tuple[int | None, int | None]:
    """Возвращает (width, height) видео из info-dict yt-dlp."""
    w, h = info.get("width"), info.get("height")
    if w and h:
        return int(w), int(h)  # type: ignore[arg-type]
    downloads = info.get("requested_downloads")
    if isinstance(downloads, list):
        for rd in downloads:
            w, h = rd.get("width"), rd.get("height")
            if w and h:
                return int(w), int(h)
    return None, None


def _ydl_download(
    url: str,
    outtmpl: str,
    format_id: str | None = None,
) -> tuple[str | None, int | None, int | None, str | None, float | None]:
    """Скачивает лучший вариант с аудио и возвращает метаданные источника."""
    # noinspection SpellCheckingInspection
    last_error: Exception | None = None
    with _ydl_option_variants(url) as variants:
        for index, base_options in enumerate(variants):
            opts = {
                **base_options,
                "outtmpl": outtmpl,  # cspell:disable-line
                "format": format_id or DEFAULT_FORMAT,
                "max_filesize": MAX_SOURCE_VIDEO_BYTES,
            }
            try:
                with _ydl_lock, yt_dlp.YoutubeDL(opts) as ydl:  # type: ignore[arg-type]
                    info = ydl.extract_info(url, download=True)
                if not info:
                    raise MediaPreparationError("yt-dlp не вернул метаданные")
                expected_size = _to_int(info.get("filesize") or info.get("filesize_approx"))
                if expected_size is not None and expected_size > MAX_SOURCE_VIDEO_BYTES:
                    raise VideoTooLargeError
                vid = str(info["id"]) if info.get("id") else None
                width, height = _extract_dimensions(info)
                raw_desc = info.get("description")
                description = str(raw_desc) if raw_desc else None
                return vid, width, height, description, _to_float(info.get("duration"))
            except VideoTooLargeError:
                raise
            except Exception as error:
                last_error = error
                if index + 1 < len(variants):
                    logging.info("yt-dlp: повторяем загрузку с cookies")
    raise MediaPreparationError("yt-dlp не смог скачать медиа") from last_error


def _ydl_download_from_info_file(
    url: str,
    info_path: Path,
    outtmpl: str,
    format_id: str | None,
) -> tuple[str | None, int | None, int | None, str | None, float | None]:
    last_error: Exception | None = None
    with _ydl_option_variants(url) as variants:
        for index, base_options in enumerate(variants):
            opts = {
                **base_options,
                "outtmpl": outtmpl,  # cspell:disable-line
                "format": format_id or DEFAULT_FORMAT,
                "max_filesize": MAX_SOURCE_VIDEO_BYTES,
            }
            try:
                with _ydl_lock, yt_dlp.YoutubeDL(opts) as ydl:  # type: ignore[arg-type]
                    return_code = ydl.download_with_info_file(str(info_path))
                if return_code:
                    raise MediaPreparationError(
                        f"yt-dlp завершился с кодом {return_code}"
                    )
                info = _load_info_snapshot(info_path.name.removesuffix(".info.json"))
                if info is None:
                    raise MediaPreparationError("Не удалось прочитать метаданные загрузки")
                expected_size = _to_int(info.get("filesize") or info.get("filesize_approx"))
                if expected_size is not None and expected_size > MAX_SOURCE_VIDEO_BYTES:
                    raise VideoTooLargeError
                inspection = _inspection_from_snapshot(url, info)
                return (
                    inspection.media_id,
                    inspection.width,
                    inspection.height,
                    inspection.description,
                    inspection.duration,
                )
            except VideoTooLargeError:
                raise
            except Exception as error:
                last_error = error
                if index + 1 < len(variants):
                    logging.info("yt-dlp: повторяем загрузку info-файла с cookies")
    raise MediaPreparationError("yt-dlp не смог скачать медиа из info-файла") from last_error


def _parse_frame_rate(value: object) -> float | None:
    if not isinstance(value, str) or not value or value in {"0/0", "N/A"}:
        return None
    try:
        numerator, separator, denominator = value.partition("/")
        if separator:
            denominator_value = float(denominator)
            return float(numerator) / denominator_value if denominator_value else None
        return float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _probe_media_with_ffprobe(path: Path) -> MediaProbe | None:
    if not FFPROBE_EXE:
        return None
    try:
        result = subprocess.run(
            [
                FFPROBE_EXE,
                "-v", "error",
                "-show_streams",
                "-show_format",
                "-of", "json",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        data = json.loads(result.stdout)
    except Exception:
        return None

    streams = data.get("streams") if isinstance(data, dict) else None
    if not isinstance(streams, list):
        streams = []
    video = next(
        (
            stream
            for stream in streams
            if isinstance(stream, dict)
            and stream.get("codec_type") == "video"
            and not (
                isinstance(stream.get("disposition"), dict)
                and stream["disposition"].get("attached_pic") == 1
            )
        ),
        None,
    )
    audio = next(
        (
            stream
            for stream in streams
            if isinstance(stream, dict) and stream.get("codec_type") == "audio"
        ),
        None,
    )

    rotation = 0
    if isinstance(video, dict):
        tags = video.get("tags")
        if isinstance(tags, dict):
            rotation = _to_int(tags.get("rotate")) or 0
        side_data = video.get("side_data_list")
        if isinstance(side_data, list):
            for item in side_data:
                if isinstance(item, dict) and item.get("rotation") is not None:
                    rotation = _to_int(item.get("rotation")) or rotation
                    break

    format_data = data.get("format") if isinstance(data, dict) else None
    duration = None
    if isinstance(format_data, dict):
        duration = _to_float(format_data.get("duration"))
    if duration is None and isinstance(video, dict):
        duration = _to_float(video.get("duration"))

    return MediaProbe(
        has_video=isinstance(video, dict),
        has_audio=isinstance(audio, dict),
        video_codec=str(video.get("codec_name")) if isinstance(video, dict) else None,
        audio_codec=str(audio.get("codec_name")) if isinstance(audio, dict) else None,
        width=_to_int(video.get("width")) if isinstance(video, dict) else None,
        height=_to_int(video.get("height")) if isinstance(video, dict) else None,
        fps=(
            _parse_frame_rate(video.get("avg_frame_rate"))
            or _parse_frame_rate(video.get("r_frame_rate"))
            if isinstance(video, dict)
            else None
        ),
        duration=duration,
        pixel_format=(
            str(video.get("pix_fmt"))
            if isinstance(video, dict) and video.get("pix_fmt")
            else None
        ),
        sample_aspect_ratio=(
            str(video.get("sample_aspect_ratio"))
            if isinstance(video, dict) and video.get("sample_aspect_ratio") not in (None, "N/A")
            else None
        ),
        rotation=rotation,
    )


def _probe_media(path: Path) -> MediaProbe:
    probe = _probe_media_with_ffprobe(path)
    if probe is not None:
        return probe

    result = subprocess.run(
        [FFMPEG_EXE, "-hide_banner", "-i", str(path)],
        capture_output=True,
        text=True,
    )
    output = "\n".join(part for part in (result.stdout, result.stderr) if part)
    duration_match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", output)
    duration = None
    if duration_match:
        duration = (
            int(duration_match.group(1)) * 3600
            + int(duration_match.group(2)) * 60
            + float(duration_match.group(3))
        )

    video_line = next((line for line in output.splitlines() if " Video: " in line), "")
    audio_line = next((line for line in output.splitlines() if " Audio: " in line), "")
    video_codec_match = re.search(r"Video:\s*([^,\s]+)", video_line)
    audio_codec_match = re.search(r"Audio:\s*([^,\s]+)", audio_line)
    dimensions_match = re.search(r"(?<!\d)(\d{2,5})x(\d{2,5})(?!\d)", video_line)
    pixel_format_match = re.search(r"Video:\s*[^,]+,\s*([^,(\s]+)", video_line)
    fps_match = re.search(r"([\d.]+)\s+fps\b", video_line)
    sar_match = re.search(r"SAR\s+(\d+:\d+)", video_line)
    rotation_match = re.search(r"rotation of\s+(-?\d+(?:\.\d+)?)", output)

    return MediaProbe(
        has_video=bool(video_line),
        has_audio=bool(audio_line),
        video_codec=video_codec_match.group(1) if video_codec_match else None,
        audio_codec=audio_codec_match.group(1) if audio_codec_match else None,
        width=int(dimensions_match.group(1)) if dimensions_match else None,
        height=int(dimensions_match.group(2)) if dimensions_match else None,
        fps=float(fps_match.group(1)) if fps_match else None,
        duration=duration,
        pixel_format=pixel_format_match.group(1) if pixel_format_match else None,
        sample_aspect_ratio=sar_match.group(1) if sar_match else None,
        rotation=round(float(rotation_match.group(1))) if rotation_match else 0,
    )


def _gallery_url_list(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [url for url in value if isinstance(url, str)]
    if isinstance(value, dict):
        urls: list[str] = []
        for key in ("MainUrl", "BackupUrl", "FallbackUrl", "UrlList"):
            urls.extend(_gallery_url_list(value.get(key)))
        return list(dict.fromkeys(urls))
    return []


def _gallery_tiktok_context(
    url: str,
    cookie_file: Path | None = None,
) -> tuple[object, dict[str, object], str] | None:
    current = url
    seen: set[str] = set()
    for _ in range(3):
        if current in seen:
            return None
        seen.add(current)
        extractor = (
            TiktokPostExtractor.from_url(current)
            or TiktokVmpostExtractor.from_url(current)
        )
        if extractor is None or getattr(extractor, "category", None) != "tiktok":
            return None

        queued_url: str | None = None
        try:
            extractor.initialize()
            if cookie_file is not None:
                extractor.cookies_load(str(cookie_file))
            for message in extractor.items():
                if message[0] == GalleryMessage.Queue:
                    queued_url = str(message[1])
                    break
                if message[0] == GalleryMessage.Directory and isinstance(message[2], dict):
                    return extractor, message[2], current
        except Exception as error:
            logging.info("gallery-dl не получил метаданные TikTok: %s", error)
            return None
        if not queued_url:
            return None
        current = queued_url
    return None


def _tiktok_video_candidates(post: dict[str, object]) -> list[dict[str, object]]:
    video = post.get("video")
    if not isinstance(video, dict):
        return []

    candidates: list[dict[str, object]] = []
    bitrate_info = video.get("bitrateInfo")
    if isinstance(bitrate_info, dict):
        bitrate_info = [bitrate_info]
    if isinstance(bitrate_info, list):
        for item in bitrate_info:
            if not isinstance(item, dict):
                continue
            address = item.get("PlayAddr")
            if not isinstance(address, dict):
                continue
            urls = _gallery_url_list(address.get("UrlList"))
            if not urls:
                continue
            audio_id = None
            video_extra = item.get("VideoExtra")
            if isinstance(video_extra, str):
                try:
                    extra_data = json.loads(video_extra)
                    if isinstance(extra_data, dict) and extra_data.get("audio_file_id"):
                        audio_id = str(extra_data["audio_file_id"])
                except (TypeError, ValueError, json.JSONDecodeError):
                    pass
            candidates.append({
                "urls": urls,
                "width": _to_int(address.get("Width")),
                "height": _to_int(address.get("Height")),
                "fps": _to_float(item.get("BitrateFPS")),
                "bitrate": _to_int(item.get("Bitrate")),
                "codec": str(item.get("CodecType") or ""),
                "filesize": _to_int(address.get("DataSize")),
                "audio_id": audio_id,
            })

    root_url = video.get("playAddr")
    root_urls = _gallery_url_list(root_url)
    if root_urls:
        candidates.append({
            "urls": root_urls,
            "width": _to_int(video.get("width")),
            "height": _to_int(video.get("height")),
            "fps": None,
            "bitrate": _to_int(video.get("bitrate")),
            "codec": str(video.get("codecType") or ""),
            "filesize": _to_int(video.get("size")),
            "audio_id": None,
        })

    candidates.sort(
        key=lambda item: (
            _to_float(item.get("fps")) or 0.0,
            (_to_int(item.get("width")) or 0) * (_to_int(item.get("height")) or 0),
            _to_int(item.get("bitrate")) or 0,
            1 if _is_h264(item.get("codec")) else 0,
        ),
        reverse=True,
    )
    return candidates


def _tiktok_audio_urls(video: dict[str, object], audio_id: str | None) -> list[str]:
    audio_info = video.get("bitrateAudioInfo")
    if isinstance(audio_info, dict):
        audio_info = [audio_info]
    if not isinstance(audio_info, list):
        return []

    candidates = [item for item in audio_info if isinstance(item, dict)]
    candidates.sort(
        key=lambda item: (
            1 if audio_id and str(item.get("FileId") or "") == audio_id else 0,
            _to_int(item.get("Bitrate")) or 0,
        ),
        reverse=True,
    )
    for item in candidates:
        urls = _gallery_url_list(item.get("UrlList"))
        if urls:
            return urls
    return []


def _download_gallery_stream(
    extractor: object,
    urls: list[str],
    output: Path,
) -> None:
    last_error: Exception | None = None
    for url in urls:
        output.unlink(missing_ok=True)
        response = None
        try:
            response = extractor.request(url, stream=True)  # type: ignore[attr-defined]
            expected_size = _to_int(response.headers.get("content-length"))
            if expected_size is not None and expected_size > MAX_SOURCE_VIDEO_BYTES:
                raise VideoTooLargeError
            size = 0
            with output.open("wb") as file:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > MAX_SOURCE_VIDEO_BYTES:
                        raise VideoTooLargeError
                    file.write(chunk)
            if size:
                return
        except VideoTooLargeError:
            output.unlink(missing_ok=True)
            raise
        except Exception as error:
            last_error = error
            output.unlink(missing_ok=True)
        finally:
            if response is not None:
                response.close()
    raise MediaPreparationError("gallery-dl не смог скачать поток") from last_error


def _mux_external_audio(video: Path, audio: Path) -> None:
    audio_probe = _probe_media(audio)
    output = video.with_name(f"{video.stem}.muxed.mp4")
    command = [
        FFMPEG_EXE, "-loglevel", "error", "-y",
        "-i", str(video),
        "-i", str(audio),
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-c:v", "copy",
    ]
    if audio_probe.audio_codec == "aac":
        command.extend(("-c:a", "copy"))
    else:
        command.extend(("-c:a", "aac", "-b:a", "192k"))
    command.extend(("-shortest", "-movflags", "+faststart", str(output)))
    try:
        subprocess.run(command, check=True, capture_output=True)
        if not _probe_media(output).has_audio:
            raise MissingAudioError
        os.replace(output, video)
    finally:
        output.unlink(missing_ok=True)


def _download_tiktok_post(
    extractor: object,
    post: dict[str, object],
    output: Path,
) -> tuple[str | None, int | None, int | None, str | None, float | None] | None:
    if post.get("post_type") != "video":
        return None

    candidates = _tiktok_video_candidates(post)
    if not candidates:
        return None
    last_error: Exception | None = None
    video_metadata = post.get("video")
    raw_description = post.get("desc")
    for selected in candidates:
        audio_path = output.with_name(f"{output.stem}.audio.m4a")
        try:
            expected_size = _to_int(selected.get("filesize"))
            if expected_size is not None and expected_size > MAX_SOURCE_VIDEO_BYTES:
                raise VideoTooLargeError
            urls = selected.get("urls")
            if not isinstance(urls, list):
                raise MediaPreparationError("TikTok не вернул URL видеопотока")
            _download_gallery_stream(extractor, urls, output)

            probe = _probe_media(output)
            if not probe.has_audio:
                selected_audio_id = selected.get("audio_id")
                audio_urls = (
                    _tiktok_audio_urls(
                        video_metadata,
                        str(selected_audio_id) if selected_audio_id else None,
                    )
                    if isinstance(video_metadata, dict)
                    else []
                )
                if not audio_urls:
                    music = post.get("music")
                    if isinstance(music, dict):
                        audio_urls = _gallery_url_list(music.get("playUrl"))
                if not audio_urls:
                    raise MissingAudioError
                _download_gallery_stream(extractor, audio_urls, audio_path)
                _mux_external_audio(output, audio_path)
                probe = _probe_media(output)
            if not probe.has_audio:
                raise MissingAudioError
            return (
                str(post.get("id")) if post.get("id") else None,
                probe.width,
                probe.height,
                str(raw_description) if raw_description else None,
                probe.duration,
            )
        except Exception as error:
            last_error = error
            output.unlink(missing_ok=True)
            logging.info(
                "Поток TikTok %sx%s/%s FPS не подошёл: %s",
                selected.get("width"),
                selected.get("height"),
                selected.get("fps"),
                error,
            )
        finally:
            audio_path.unlink(missing_ok=True)

    if isinstance(last_error, (VideoTooLargeError, MissingAudioError)):
        raise last_error
    raise MediaPreparationError("Не удалось скачать ни один поток TikTok") from last_error


def _gallery_tiktok_video_download(
    url: str,
    output: Path,
) -> tuple[str | None, int | None, int | None, str | None, float | None] | None:
    cookie_variants: list[Path | None] = [None, *cookie_files_for_url(url)]
    last_error: Exception | None = None
    for index, current_cookie_file in enumerate(cookie_variants):
        try:
            context = _gallery_tiktok_context(url, current_cookie_file)
            if context is None:
                if index + 1 < len(cookie_variants):
                    logging.info("gallery-dl: повторяем получение TikTok с cookies")
                continue
            extractor, post, _ = context
            return _download_tiktok_post(extractor, post, output)
        except VideoTooLargeError:
            raise
        except Exception as error:
            last_error = error
            output.unlink(missing_ok=True)
            if index + 1 < len(cookie_variants):
                logging.info("gallery-dl: повторяем загрузку TikTok с cookies")
                continue
    if last_error is not None:
        raise last_error
    return None


def _prepare_telegram_video(path: Path) -> tuple[Path, MediaProbe]:
    source_probe = _probe_media(path)
    if not source_probe.has_video:
        raise MediaPreparationError("В файле отсутствует видеодорожка")
    if not source_probe.has_audio:
        raise MissingAudioError

    needs_video_transcode = (
        not _is_h264(source_probe.video_codec)
        or source_probe.pixel_format not in (None, "yuv420p")
        or source_probe.sample_aspect_ratio not in (None, "1:1")
        or bool(source_probe.rotation)
    )
    needs_audio_transcode = source_probe.audio_codec != "aac"
    output = path.with_name(f"{path.stem}.telegram.mp4")
    final_path = path.with_suffix(".mp4")
    command = [
        FFMPEG_EXE, "-loglevel", "error", "-y",
        "-i", str(path),
        "-map", "0:v:0",
        "-map", "0:a:0",
        "-map_metadata", "-1",
    ]
    if needs_video_transcode:
        command.extend((
            "-vf", "setsar=1",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", str(VIDEO_TRANSCODE_CRF),
            "-pix_fmt", "yuv420p",
            "-threads", "1",
            "-fps_mode", "vfr",
            "-metadata:s:v:0", "rotate=0",
        ))
    else:
        command.extend(("-c:v", "copy"))
    if needs_audio_transcode:
        command.extend(("-c:a", "aac", "-b:a", "192k"))
    else:
        command.extend(("-c:a", "copy"))
    command.extend(("-movflags", "+faststart", str(output)))

    try:
        subprocess.run(command, check=True, capture_output=True)
        prepared_probe = _probe_media(output)
        if not prepared_probe.has_video or not prepared_probe.has_audio:
            raise MediaPreparationError("FFmpeg создал неполный файл")
        if not _is_h264(prepared_probe.video_codec):
            raise MediaPreparationError("Не удалось привести видео к H.264")
        if output.stat().st_size > MAX_SOURCE_VIDEO_BYTES:
            raise VideoTooLargeError
        os.replace(output, final_path)
        if path != final_path:
            path.unlink(missing_ok=True)
        return final_path, prepared_probe
    finally:
        output.unlink(missing_ok=True)


def _build_slideshow(images: list[Path], audio: Path | None, output: Path) -> None:
    concat_file = images[0].parent / "slides.txt"
    lines: list[str] = []
    for image in images:
        lines.extend((f"file '{image.name}'", f"duration {PHOTO_DURATION}"))
    lines.append(f"file '{images[-1].name}'")
    concat_file.write_text("\n".join(lines), encoding="utf-8")

    command = [
        FFMPEG_EXE, "-loglevel", "error", "-y",
        "-f", "concat", "-safe", "0", "-i", concat_file.name,
    ]
    if audio:
        command.extend(("-stream_loop", "-1", "-i", audio.name))
    command.extend((
        "-vf",
        f"scale={SLIDESHOW_WIDTH}:{SLIDESHOW_HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={SLIDESHOW_WIDTH}:{SLIDESHOW_HEIGHT}:(ow-iw)/2:(oh-ih)/2:black,setsar=1",
        "-t", str(len(images) * PHOTO_DURATION),
        "-c:v", "libx264", "-preset", "veryfast", "-threads", "1",
        "-pix_fmt", "yuv420p", "-fps_mode", "vfr",
    ))
    if audio:
        command.extend(("-map", "0:v:0", "-map", "1:a:0", "-c:a", "aac", "-b:a", "192k"))
    else:
        command.append("-an")
    command.extend(("-movflags", "+faststart", str(output)))
    subprocess.run(command, cwd=images[0].parent, check=True, capture_output=True)


def _gallery_download(
    url: str,
    output: Path,
) -> tuple[str | None, int | None, int | None, str | None, float | None] | None:
    try:
        tiktok_video = _gallery_tiktok_video_download(url, output)
        if tiktok_video is not None:
            return tiktok_video
    except (VideoTooLargeError, MissingAudioError):
        raise
    except Exception as error:
        logging.info("Расширенный TikTok fallback не сработал: %s", error)

    with tempfile.TemporaryDirectory(dir=VIDEO_DIR, prefix="slideshow_") as directory:
        work_dir = Path(directory)
        cookie_variants: list[Path | None] = [None, *cookie_files_for_url(url)]
        last_result: subprocess.CompletedProcess[str] | None = None
        for index, current_cookie_file in enumerate(cookie_variants):
            for path in work_dir.iterdir():
                if path.is_file():
                    path.unlink(missing_ok=True)
            command = [
                sys.executable, "-m", "gallery_dl", "--config-ignore", "--no-input",
                "--filesize-max", str(MAX_SOURCE_VIDEO_BYTES),
                "--no-mtime", "--directory", str(work_dir),
                "--filename", "{num}_{type}.{extension}",
                "--option", "extractor.tiktok.videos=true",
                "--option", "extractor.tiktok.photos=true",
                "--option", "extractor.tiktok.audio=true",
                "--option", "extractor.instagram.videos=true",
            ]
            if current_cookie_file is not None:
                command.extend((
                    "--cookies", str(current_cookie_file),
                    "--option", "extractor.cookies-update=false",
                ))
            command.append(url)
            last_result = subprocess.run(command, capture_output=True, text=True)

            files = [path for path in work_dir.iterdir() if path.is_file()]
            audio = next(
                (path for path in files if path.suffix.lower() in _AUDIO_EXTENSIONS),
                None,
            )
            video = next(
                (path for path in files if path.suffix.lower() in _VIDEO_EXTENSIONS),
                None,
            )
            if video:
                probe = _probe_media(video)
                if not probe.has_audio and audio is None and index + 1 < len(cookie_variants):
                    logging.info("gallery-dl: повторяем загрузку видео с cookies")
                    continue
                if video.stat().st_size > MAX_SOURCE_VIDEO_BYTES:
                    raise VideoTooLargeError
                target = output.with_suffix(video.suffix)
                video.replace(target)
                if not probe.has_audio and audio is not None:
                    _mux_external_audio(target, audio)
                    probe = _probe_media(target)
                return _resolve_media(url)[0], probe.width, probe.height, None, probe.duration

            images = sorted(
                (path for path in files if path.suffix.lower() in _IMAGE_EXTENSIONS),
                key=lambda path: int(path.name.partition("_")[0]),
            )
            if images:
                if audio is None and index + 1 < len(cookie_variants):
                    logging.info("gallery-dl: повторяем загрузку слайд-шоу с cookies")
                    continue
                _build_slideshow(images, audio, output)
                if output.stat().st_size > MAX_SOURCE_VIDEO_BYTES:
                    raise VideoTooLargeError
                probe = _probe_media(output)
                return (
                    _resolve_media(url)[0],
                    SLIDESHOW_WIDTH,
                    SLIDESHOW_HEIGHT,
                    None,
                    probe.duration,
                )

            if index + 1 < len(cookie_variants):
                logging.info("gallery-dl: повторяем загрузку с cookies")

        if last_result is not None and last_result.returncode:
            logging.info("gallery-dl не скачал медиа (код %s)", last_result.returncode)
        return None


def _download_media(
    url: str,
    outtmpl: str,
    slideshow_path: Path,
    format_id: str | None = None,
) -> tuple[str | None, int | None, int | None, str | None, float | None]:
    try:
        return _ydl_download(url, outtmpl, format_id)
    except VideoTooLargeError:
        raise
    except Exception as error:
        logging.info("yt-dlp не скачал медиа, используем fallback: %s", error)
        downloaded = _gallery_download(url, slideshow_path)
        if downloaded is not None:
            return downloaded
        raise


def _segment_time_seconds(size: int, duration: float | None) -> int:
    if duration is None or duration <= 0:
        return 30
    return max(1, int(duration * MAX_TELEGRAM_PART_BYTES / size * 0.97))


def _next_segment_time_seconds(part_seconds: int, largest_size: int) -> int:
    """Shrink the retry proportionally, keeping stream-copy splits near 48 MB."""
    if part_seconds <= 1:
        return 1
    if largest_size <= 0:
        return max(1, part_seconds // 2)
    estimate = int(part_seconds * MAX_TELEGRAM_PART_BYTES / largest_size * 0.95)
    return min(part_seconds - 1, max(1, estimate))


def _split_video(video_path: Path, duration: float | None) -> tuple[list[Path], Path | None]:
    size = video_path.stat().st_size
    if size <= MAX_TELEGRAM_PART_BYTES:
        return [video_path], None

    part_dir = Path(tempfile.mkdtemp(dir=VIDEO_DIR, prefix=f"{video_path.stem}_parts_"))
    part_seconds = _segment_time_seconds(size, duration)
    suffix = video_path.suffix
    try:
        for _ in range(6):
            for part in part_dir.iterdir():
                part.unlink()
            output = part_dir / f"part_%03d{suffix}"
            command = [
                FFMPEG_EXE, "-loglevel", "error", "-y", "-i", str(video_path),
                "-map", "0:v:0", "-map", "0:a?", "-c", "copy",
                "-f", "segment", "-segment_time", str(part_seconds),
                "-reset_timestamps", "1",
            ]
            if suffix.lower() == ".mp4":
                command.extend((
                    "-segment_format", "mp4",
                    "-segment_format_options", "movflags=+faststart",
                ))
            command.append(str(output))
            subprocess.run(command, check=True, capture_output=True)
            parts = sorted(part_dir.glob(f"part_*{suffix}"))
            if parts and all(part.stat().st_size <= MAX_TELEGRAM_PART_BYTES for part in parts):
                return parts, part_dir
            largest_size = max((part.stat().st_size for part in parts), default=0)
            part_seconds = _next_segment_time_seconds(part_seconds, largest_size)
    except Exception:
        shutil.rmtree(part_dir, ignore_errors=True)
        raise

    shutil.rmtree(part_dir, ignore_errors=True)
    raise VideoSplitError


async def _show_download_error(
    bot: Bot,
    chat_id: str,
    inline_message_id: str | None,
    placeholder_message_id: int | None,
    text: str,
    source_url: str | None = None,
) -> None:
    if source_url:
        text = f"{text}\n\n{source_link(source_url)}"
    if inline_message_id:
        try:
            await bot.edit_message_caption(
                inline_message_id=inline_message_id,
                caption=text,
                parse_mode=ParseMode.HTML,
                reply_markup=None,
            )
            return
        except (TelegramBadRequest, TelegramForbiddenError):
            try:
                await bot.edit_message_text(
                    inline_message_id=inline_message_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=None,
                )
                return
            except (TelegramBadRequest, TelegramForbiddenError):
                pass
    if placeholder_message_id:
        try:
            await bot.edit_message_caption(
                chat_id=chat_id,
                message_id=placeholder_message_id,
                caption=text,
                parse_mode=ParseMode.HTML,
                reply_markup=None,
            )
            return
        except (TelegramBadRequest, TelegramForbiddenError):
            pass
    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)
    except (TelegramBadRequest, TelegramForbiddenError):
        pass


def music_button_markup(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="🎵 Распознать музыку",
            callback_data=f"{MUSIC_JOB_PREFIX}{token}",
        )
    ]])


def _music_result_markup(token: str, page: int, pages: int) -> InlineKeyboardMarkup | None:
    buttons: list[InlineKeyboardButton] = []
    if page:
        buttons.append(InlineKeyboardButton(
            text="◀️", callback_data=f"{MUSIC_PAGE_PREFIX}{token}:{page - 1}"
        ))
    if page + 1 < pages:
        buttons.append(InlineKeyboardButton(
            text="▶️", callback_data=f"{MUSIC_PAGE_PREFIX}{token}:{page + 1}"
        ))
    return InlineKeyboardMarkup(inline_keyboard=[buttons]) if buttons else None


async def _deliver_video(
    bot: Bot,
    chat_id: str,
    inline_message_id: str | None,
    file_id: str | None,
    video_path: Path | None,
    width: int | None = None,
    height: int | None = None,
    description: str | None = None,
    source_url: str | None = None,
    likes: int | None = None,
    reply_to_message_id: int | None = None,
    placeholder_message_id: int | None = None,
    has_spoiler: bool = False,
    without_description: bool = False,
    music_deep_link: str | None = None,
    wait_for_placeholder: bool = True,
) -> str | None:
    """Отправляет видео пользователю, возвращает file_id для кеша (или None)."""
    # Inline-режим: редактируем сообщение через file_id из Storage
    if inline_message_id:
        if not file_id:
            # Без file_id в inline-режиме залить файл нельзя — меняем
            # плейсхолдер на ошибку, иначе он висит вечно.
            logging.warning("inline_message_id есть, но file_id отсутствует — пропускаем")
            await _show_download_error(
                bot,
                chat_id,
                inline_message_id,
                None,
                "❌ Не удалось доставить видео. Попробуй ещё раз.",
                source_url,
            )
            return None
        # Источник и лайки остаются в подписи видео.
        ready_at = _inline_placeholder_ready_at.pop(inline_message_id, 0)
        if wait_for_placeholder:
            await asyncio.sleep(max(0, ready_at - asyncio.get_running_loop().time()))
        try:
            await bot.edit_message_media(
                inline_message_id=inline_message_id,
                media=InputMediaVideo(
                    media=file_id,
                    supports_streaming=True,
                    caption=(
                        None
                        if without_description
                        else build_caption(
                            description,
                            source_url,
                            with_source=True,
                            likes=likes,
                            has_spoiler=has_spoiler,
                            music_deep_link=music_deep_link,
                        )
                    ),
                    parse_mode=ParseMode.HTML,
                    has_spoiler=has_spoiler,
                ),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[]),
            )
        except (TelegramBadRequest, TelegramForbiddenError) as e:
            logging.warning("edit_message_media failed: %s", e)
            raise
        return file_id

    # Обычный режим: send_video в чат. Источник — кликабельным текстом в подписи.
    caption = (
        None
        if without_description
        else build_caption(
            description,
            source_url,
            with_source=True,
            likes=likes,
            has_spoiler=has_spoiler,
            music_deep_link=music_deep_link,
        )
    )
    if placeholder_message_id:
        ready_at = _placeholder_ready_at.get((chat_id, placeholder_message_id), 0)
        await asyncio.sleep(max(0, ready_at - asyncio.get_running_loop().time()))
        if not file_id and not video_path:
            return None
        media = file_id or FSInputFile(str(video_path))
        msg = await bot.edit_message_media(
            chat_id=chat_id,
            message_id=placeholder_message_id,
            media=InputMediaVideo(
                media=media,
                width=width,
                height=height,
                supports_streaming=True,
                caption=caption,
                parse_mode=ParseMode.HTML,
                has_spoiler=has_spoiler,
            ),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[]),
        )
        if file_id:
            return file_id
        if msg.video:
            return msg.video.file_id
        return None

    if file_id:
        await bot.send_video(
            chat_id=chat_id,
            video=file_id,
            caption=caption,
            parse_mode=ParseMode.HTML,
            reply_to_message_id=reply_to_message_id,
            allow_sending_without_reply=True,
            has_spoiler=has_spoiler,
            supports_streaming=True,
        )
        return file_id

    if video_path:
        msg = await bot.send_video(
            chat_id=chat_id,
            video=FSInputFile(str(video_path)),
            width=width,
            height=height,
            caption=caption,
            parse_mode=ParseMode.HTML,
            reply_to_message_id=reply_to_message_id,
            allow_sending_without_reply=True,
            has_spoiler=has_spoiler,
            supports_streaming=True,
        )
        if msg.video:
            return msg.video.file_id

    return None


async def deliver_cached_video(
    bot: Bot,
    cached: CachedVideo,
    *,
    chat_id: str,
    fallback_source_url: str,
    inline_message_id: str | None = None,
    reply_to_message_id: int | None = None,
    has_spoiler: bool = False,
    without_description: bool = False,
) -> None:
    """Доставляет сохранённое Telegram-видео без локального файла и плейсхолдера."""
    await _deliver_video(
        bot,
        chat_id,
        inline_message_id,
        file_id=cached.file_id,
        video_path=None,
        description=cached.description,
        source_url=cached.source_url or fallback_source_url,
        likes=cached.likes,
        reply_to_message_id=reply_to_message_id,
        placeholder_message_id=None,
        has_spoiler=has_spoiler,
        without_description=without_description,
        wait_for_placeholder=False,
    )


async def try_deliver_cached_video(
    bot: Bot,
    session: AsyncSession,
    canonical_url: str,
    *,
    chat_id: str,
    inline_message_id: str | None = None,
    reply_to_message_id: int | None = None,
    has_spoiler: bool = False,
    without_description: bool = False,
) -> bool:
    """Пытается отправить кеш; False оставляет вызывающему обычный путь очереди."""
    try:
        cached = await find_cached_video(session, canonical_url)
    except Exception:
        logging.exception("Cache lookup failed: url=%s", canonical_url)
        await session.rollback()
        return False
    if cached is None:
        await session.rollback()
        return False

    await session.rollback()
    try:
        await deliver_cached_video(
            bot,
            cached,
            chat_id=chat_id,
            fallback_source_url=canonical_url,
            inline_message_id=inline_message_id,
            reply_to_message_id=reply_to_message_id,
            has_spoiler=has_spoiler,
            without_description=without_description,
        )
        return True
    except TelegramBadRequest as error:
        if not is_invalid_cached_file_id_error(error):
            raise
        await invalidate_cached_video(session, cached.cache_key)
        return False


def _remove_download_files(filename: str) -> None:
    for path in VIDEO_DIR.glob(f"{filename}.*"):
        if path.is_file():
            path.unlink(missing_ok=True)
    for path in VIDEO_DIR.glob(f"{filename}_parts_*"):
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)


def can_offer_music(without_description: bool, _inline_message_id: str | None) -> bool:
    """Legacy status-card eligibility; inline videos never get an extra message."""
    return not without_description and _inline_message_id is None


def can_attach_music_link(without_description: bool) -> bool:
    """Caption deep-links work for every captioned video, including inline mode."""
    return not without_description


def _music_chunk_starts(duration: float) -> range:
    if duration <= 0:
        return range(0)
    return range(0, math.ceil(duration), MUSIC_CHUNK_SECONDS)


def _format_music_timestamp(seconds: int) -> str:
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def _add_music_track(tracks: list[MusicTrack], start: int, response: object) -> None:
    """Keep only adjacent duplicates; a track returning later remains useful."""
    if not isinstance(response, dict):
        return
    track = response.get("track")
    if not isinstance(track, dict):
        return

    title = str(track.get("title") or "").strip()
    artist = str(track.get("subtitle") or "").strip()
    if not title:
        return
    if not artist:
        artist = "Неизвестный исполнитель"

    if tracks and (
        tracks[-1][1].casefold(), tracks[-1][2].casefold()
    ) == (artist.casefold(), title.casefold()):
        return
    tracks.append((start, artist, title))


def _format_music_tracks(tracks: list[MusicTrack]) -> str | None:
    if not tracks:
        return None
    lines = ["🎵 Найденные треки:"]
    lines.extend(
        f"• {_format_music_timestamp(start)} — {html.quote(artist)} — {html.quote(title)}"
        for start, artist, title in tracks
    )
    return "\n".join(lines)


def _telegram_text_length(text: str) -> int:
    """Telegram measures text limits in UTF-16 code units, not Python characters."""
    return len(text.encode("utf-16-le")) // 2


def _truncate_telegram_text(text: str, limit: int) -> str:
    if _telegram_text_length(text) <= limit:
        return text

    result: list[str] = []
    used = 0
    for character in text:
        character_length = 2 if ord(character) > 0xFFFF else 1
        if used + character_length > limit - 1:
            break
        result.append(character)
        used += character_length
    return "".join(result) + "…"


def _music_message_parts(text: str, suffix: str | None = None) -> list[str]:
    """Split a long result at line boundaries while repeating its heading."""
    suffix_text = f"\n\n{suffix}" if suffix else ""
    message_limit = MUSIC_MESSAGE_LIMIT - _telegram_text_length(suffix_text)
    heading, *lines = text.splitlines()
    parts: list[str] = []
    current = [heading]
    heading_length = _telegram_text_length(heading)
    current_length = heading_length
    for line in lines:
        line = _truncate_telegram_text(
            line,
            message_limit - heading_length - 1,
        )
        added_length = _telegram_text_length(line) + 1
        if len(current) > 1 and current_length + added_length > message_limit:
            parts.append("\n".join(current) + suffix_text)
            current = [heading, line]
            current_length = heading_length + added_length
        else:
            current.append(line)
            current_length += added_length
    parts.append("\n".join(current) + suffix_text)
    return parts


def _music_status_text(text: str, source_url: str | None) -> str:
    return f"{text}\n\n{source_link(source_url)}" if source_url else text


def _probe_duration(video_path: Path) -> float | None:
    return _probe_media(video_path).duration


def _extract_music_chunk(video_path: Path, start: int, output_path: Path) -> None:
    subprocess.run(
        [
            FFMPEG_EXE, "-loglevel", "error", "-y",
            "-ss", str(start), "-t", str(MUSIC_CHUNK_SECONDS), "-i", str(video_path),
            "-vn", "-map", "0:a:0", "-ac", "1", "-ar", "16000",
            "-c:a", "pcm_s16le", str(output_path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


async def _recognize_music_tracks(video_path: Path, duration: float | None) -> str | None:
    """Recognize sequential 15-second audio chunks without risking video delivery."""
    try:
        from shazamio import Shazam
        if (
            not isinstance(duration, (int, float))
            or not math.isfinite(duration)
            or duration <= 0
        ):
            loop = asyncio.get_running_loop()
            duration = await loop.run_in_executor(None, _probe_duration, video_path)
        if duration is None or duration <= 0:
            logging.info("Не удалось определить длительность для распознавания музыки: %s", video_path)
            return None

        loop = asyncio.get_running_loop()
        tracks: list[MusicTrack] = []
        audio_chunks = 0
        successful_responses = 0
        with tempfile.TemporaryDirectory(dir=VIDEO_DIR, prefix="recognize_") as temp_dir:
            recognizer = Shazam(segment_duration_seconds=MUSIC_CHUNK_SECONDS)
            for start in _music_chunk_starts(duration):
                chunk_path = Path(temp_dir) / f"{start:06d}.wav"
                try:
                    await loop.run_in_executor(
                        None, _extract_music_chunk, video_path, start, chunk_path
                    )
                except subprocess.CalledProcessError:
                    chunk_path.unlink(missing_ok=True)
                    logging.debug("В ролике нет читаемой аудиодорожки: %s", video_path)
                    continue
                audio_chunks += 1

                try:
                    response = await asyncio.wait_for(
                        recognizer.recognize(str(chunk_path)),
                        timeout=MUSIC_RECOGNITION_TIMEOUT_SECONDS,
                    )
                except Exception as error:  # Recognition is strictly best-effort.
                    logging.info("Не удалось распознать музыку на %s: %s", chunk_path.name, error)
                else:
                    successful_responses += 1
                    _add_music_track(tracks, start, response)
                finally:
                    chunk_path.unlink(missing_ok=True)

        if not audio_chunks:
            return ""
        if not successful_responses:
            return None
        return _format_music_tracks(tracks) or ""
    except Exception as error:  # Initialization and FFmpeg failures are also non-fatal.
        logging.warning("Распознавание музыки пропущено для %s: %s", video_path, error)
        return None


async def update_music_request_message(
    bot: Bot,
    request: MusicRequest,
    text: str,
    retry: bool = False,
    reply_markup: InlineKeyboardMarkup | None = None,
    include_source: bool = True,
) -> None:
    """Update the requested music card instead of sending an unsolicited message."""
    markup = music_button_markup(request.token) if retry else reply_markup
    if include_source:
        text = _music_status_text(text, request.tiktok_link)
    try:
        if request.inline_message_id:
            await bot.edit_message_text(
                inline_message_id=request.inline_message_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
            )
        elif request.status_message_id:
            await bot.edit_message_text(
                chat_id=request.chat_id,
                message_id=request.status_message_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
            )
    except Exception as error:
        logging.warning("Не удалось обновить карточку распознавания: %s", error)


async def show_music_request_page(
    bot: Bot,
    request: MusicRequest,
    page: int,
) -> None:
    """Show one inline page so a long result remains fully accessible."""
    music_tracks = request.music_tracks
    if not music_tracks:
        await update_music_request_message(bot, request, "🎵 Музыка в ролике не найдена.")
        return

    source = source_link(request.tiktok_link) if request.tiktok_link else None
    parts = _music_message_parts(music_tracks, source)
    page = min(max(page, 0), len(parts) - 1)
    await update_music_request_message(
        bot,
        request,
        parts[page],
        include_source=False,
        reply_markup=_music_result_markup(request.token, page, len(parts)),
    )


async def show_music_request_result(
    bot: Bot,
    request: MusicRequest,
    music_tracks: str | None,
) -> None:
    if not music_tracks:
        await update_music_request_message(bot, request, "🎵 Музыка в ролике не найдена.")
        return

    if request.inline_message_id:
        await show_music_request_page(bot, request, 0)
        return

    source = source_link(request.tiktok_link) if request.tiktok_link else None
    parts = _music_message_parts(music_tracks, source)
    await update_music_request_message(bot, request, parts[0], include_source=False)
    for part in parts[1:]:
        try:
            await bot.send_message(request.chat_id, part, parse_mode=ParseMode.HTML)
        except Exception as error:
            logging.warning("Не удалось отправить продолжение списка треков: %s", error)
            return


async def _recognize_music_from_source(
    tiktok_link: str,
    filename: str,
    format_id: str | None,
) -> str | None:
    """Download only after a button press, then clean the temporary source file."""
    outtmpl = str(VIDEO_DIR / f"{filename}.%(ext)s")  # cspell:disable-line
    slideshow_path = VIDEO_DIR / f"{filename}.mp4"
    try:
        loop = asyncio.get_running_loop()
        _, _, _, _, duration = await loop.run_in_executor(
            None, _download_media, tiktok_link, outtmpl, slideshow_path, format_id
        )
        matches = [
            path for path in VIDEO_DIR.glob(f"{filename}.*")
            if path.suffix.lower() in _VIDEO_EXTENSIONS
        ]
        if not matches:
            return None
        return await _recognize_music_tracks(matches[0], duration)
    except Exception as error:
        logging.info("Не удалось скачать ролик для распознавания музыки: %s", error)
        return None
    finally:
        _remove_download_files(filename)


async def _process_music_job(
    bot: Bot,
    session: AsyncSession,
    item: DownloadQueue,
    token: str,
) -> None:
    """Run one requested recognition in the existing FIFO worker."""
    request: MusicRequest | None = None
    music_tracks: str | None = None
    retry = False
    try:
        request = await session.get(MusicRequest, token)
        if request is None or request.state != "queued":
            await session.delete(item)
            await session.commit()
            return

        music_tracks = await _recognize_music_from_source(
            request.tiktok_link,
            item.filename,
            request.format_id,
        )
        if music_tracks is None:
            request.state = "ready"
            result_text = "❌ Не удалось распознать музыку. Попробуйте ещё раз."
            retry = True
        elif music_tracks:
            request.music_tracks = music_tracks
            request.state = "done"
            result_text = ""
        else:
            request.music_tracks = ""
            request.state = "done"
            result_text = ""

        await session.delete(item)
        await session.commit()
    except Exception:
        logging.exception("Ошибка распознавания музыки: token=%s", token)
        await session.rollback()
        try:
            request = await session.get(MusicRequest, token)
            if request is not None:
                request.state = "ready"
            queued_item = await session.get(DownloadQueue, item.id)
            if queued_item is not None:
                await session.delete(queued_item)
            await session.commit()
        except Exception:
            logging.exception("Не удалось очистить задачу распознавания: token=%s", token)
            await session.rollback()
        result_text = "❌ Не удалось распознать музыку. Попробуйте ещё раз."
        music_tracks = None
        retry = True

    if request is None:
        return
    if retry:
        await update_music_request_message(bot, request, result_text, retry=True)
    else:
        await show_music_request_result(bot, request, music_tracks)


async def _get_music_bot_username(bot: Bot) -> str | None:
    configured = os.getenv("BOT_USERNAME", "").strip().lstrip("@")
    if configured:
        return configured

    global _music_bot_username_cache
    if _music_bot_username_cache:
        return _music_bot_username_cache
    async with _music_bot_username_lock:
        if _music_bot_username_cache:
            return _music_bot_username_cache
        try:
            me = await bot.get_me()
        except Exception as error:
            logging.warning("Не удалось получить username бота для deep-link: %s", error)
            return None
        if not me.username:
            logging.warning("У бота нет username, deep-link распознавания недоступен")
            return None
        _music_bot_username_cache = me.username
        return _music_bot_username_cache


async def create_music_deep_link(
    bot: Bot,
    session: AsyncSession,
    tiktok_link: str,
    format_id: str | None,
) -> tuple[MusicRequest, str] | None:
    """Reserve one unclaimed recognition job and return its private-bot link."""
    username = await _get_music_bot_username(bot)
    if not username:
        return None
    request = MusicRequest(
        token=uuid.uuid4().hex,
        # The user is deliberately assigned only after following the deep-link.
        user_id="",
        chat_id="",
        tiktok_link=tiktok_link,
        format_id=format_id,
        state="ready",
        music_tracks=None,
    )
    session.add(request)
    await session.commit()
    return request, f"https://t.me/{username}?start={MUSIC_START_PREFIX}{request.token}"


async def prepare_music_caption_link(
    bot: Bot,
    session: AsyncSession,
    tiktok_link: str,
    format_id: str | None,
) -> tuple[str | None, str | None]:
    """Create a caption deep-link without blocking video delivery on failure."""
    try:
        result = await create_music_deep_link(bot, session, tiktok_link, format_id)
    except Exception as error:
        logging.warning("Не удалось подготовить ссылку распознавания: %s", error)
        await session.rollback()
        return None, None
    if result is None:
        return None, None
    request, link = result
    return request.token, link


async def queue_music_deep_link(
    session: AsyncSession,
    token: str,
    user_id: str,
    chat_id: str,
    status_message_id: int,
) -> bool:
    """Atomically claim a caption deep-link and put recognition into FIFO."""
    claimed = await session.execute(
        update(MusicRequest)
        .where(
            MusicRequest.token == token,
            MusicRequest.state == "ready",
            MusicRequest.user_id == "",
            MusicRequest.chat_id == "",
            MusicRequest.status_message_id.is_(None),
            MusicRequest.inline_message_id.is_(None),
        )
        .values(
            user_id=user_id,
            chat_id=chat_id,
            status_message_id=status_message_id,
            state="queued",
        )
    )
    if claimed.rowcount != 1:
        return False

    request = await session.get(MusicRequest, token)
    if request is None:
        await session.rollback()
        return False
    await session.refresh(request)
    queue_item = DownloadQueue(
        chat_id=chat_id,
        filename=f"music_{uuid.uuid4().hex}",
        tiktok_link=request.tiktok_link,
        format_id=request.format_id,
        inline_message_id=f"{MUSIC_JOB_PREFIX}{token}",
    )
    if await enqueue_download(session, queue_item, user_id):
        return True

    await session.execute(
        update(MusicRequest)
        .where(MusicRequest.token == token)
        .values(
            user_id="",
            chat_id="",
            status_message_id=None,
            state="ready",
        )
    )
    await session.commit()
    return False


async def _load_next_job() -> QueueJob | None:
    async with SessionLocal() as session:
        priority = case(
            (DownloadQueue.inline_message_id.startswith(MUSIC_JOB_PREFIX), 1),
            else_=0,
        )
        result = await session.execute(
            select(DownloadQueue)
            .order_by(priority, DownloadQueue.id)
            .limit(1)
        )
        item = result.scalar()
        if item is None:
            return None
        return QueueJob(
            id=item.id,
            chat_id=item.chat_id,
            user_id=item.user_id,
            filename=item.filename,
            tiktok_link=item.tiktok_link,
            format_id=item.format_id,
            reply_to_message_id=item.reply_to_message_id,
            inline_message_id=item.inline_message_id,
            enqueued_at=item.enqueued_at,
        )


async def _wait_for_next_job() -> QueueJob:
    while True:
        job = await _load_next_job()
        if job is not None:
            return job

        _queue_wakeup.clear()
        job = await _load_next_job()
        if job is not None:
            return job

        try:
            await asyncio.wait_for(
                _queue_wakeup.wait(),
                timeout=QUEUE_WAKEUP_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            pass


async def _process_queue_job_legacy(bot: Bot, job: QueueJob) -> None:
    async with SessionLocal() as session:
        item = await session.get(DownloadQueue, job.id)
        if item is None:
            return
        # Task 7 removes this compatibility loop together with the long session.
        for item in (item,):

            # Защита от битых записей
            if item.chat_id is None or item.filename is None or item.tiktok_link is None:
                await session.delete(item)
                await session.commit()
                continue

            if (
                item.inline_message_id
                and item.inline_message_id.startswith(MUSIC_JOB_PREFIX)
            ):
                await _process_music_job(
                    bot,
                    session,
                    item,
                    item.inline_message_id.removeprefix(MUSIC_JOB_PREFIX),
                )
                continue

            chat_id: str = item.chat_id
            filename: str = item.filename
            tiktok_link: str = item.tiktok_link
            format_id: str | None = item.format_id
            inline_message_id: str | None = item.inline_message_id
            reply_to_message_id: int | None = item.reply_to_message_id
            placeholder_message_id = (
                -reply_to_message_id
                if reply_to_message_id is not None and reply_to_message_id < 0
                else None
            )
            if placeholder_message_id:
                reply_to_message_id = None
            placeholder_for_cleanup = placeholder_message_id
            part_dir: Path | None = None
            source_url = tiktok_link
            direct_mode = (
                inline_message_id.removeprefix("direct:")
                if inline_message_id and inline_message_id.startswith("direct:")
                else None
            )
            has_spoiler = direct_mode in ("spoiler", "no_description_spoiler")
            without_description = direct_mode in ("no_description", "no_description_spoiler")
            if direct_mode:
                inline_message_id = None
            elif inline_message_id:
                while True:
                    if inline_message_id.startswith("spoiler:"):
                        has_spoiler = True
                        inline_message_id = inline_message_id.removeprefix("spoiler:")
                    elif inline_message_id.startswith("no_description:"):
                        without_description = True
                        inline_message_id = inline_message_id.removeprefix("no_description:")
                    else:
                        break
            try:
                loop = asyncio.get_running_loop()

                # 1. ID видео TikTok
                tiktok_id, source_url, likes = await loop.run_in_executor(
                    None, _resolve_media, tiktok_link
                )
                music_deep_link: str | None = None

                # 2. Проверяем кеш
                cached: VideoCache | None = None
                if tiktok_id and format_id is None:
                    cache_key = _media_cache_key(tiktok_id)
                    cache_res = await session.execute(
                        select(VideoCache).where(VideoCache.tiktok_id == cache_key)
                    )
                    cached = cache_res.scalar()
                    if cached and cached.file_id:
                        logging.info("Cache hit: tiktok_id=%s → отправляем без загрузки", tiktok_id)
                        if can_attach_music_link(without_description):
                            _, music_deep_link = await prepare_music_caption_link(
                                bot,
                                session,
                                source_url,
                                format_id,
                            )
                        await _deliver_video(
                            bot, chat_id, inline_message_id,
                            file_id=cached.file_id, video_path=None,
                            description=cached.description, source_url=source_url,
                            likes=likes,
                            reply_to_message_id=reply_to_message_id,
                            placeholder_message_id=placeholder_message_id,
                            has_spoiler=has_spoiler,
                            without_description=without_description,
                            music_deep_link=music_deep_link,
                        )
                        await session.delete(item)
                        await session.commit()
                        continue

                # 3. Скачиваем
                outtmpl = str(VIDEO_DIR / f"{filename}.%(ext)s")  # cspell:disable-line
                slideshow_path = VIDEO_DIR / f"{filename}.mp4"
                downloaded_id, width, height, description, duration = await loop.run_in_executor(
                    None, _download_media, tiktok_link, outtmpl, slideshow_path, format_id
                )

                # Используем ID из скачивания (актуально для коротких ссылок)
                final_id: str | None = tiktok_id or downloaded_id

                # Ищем скачанный файл (расширение может отличаться от .mp4)
                matches = [
                    path for path in VIDEO_DIR.glob(f"{filename}.*")
                    if path.suffix.lower() in _VIDEO_EXTENSIONS
                ]
                if not matches:
                    raise FileNotFoundError(f"Файл после загрузки не найден: {filename}")
                video_path = matches[0]
                if video_path.stat().st_size > MAX_SOURCE_VIDEO_BYTES:
                    raise VideoTooLargeError

                video_path, media_probe = await loop.run_in_executor(
                    None,
                    _prepare_telegram_video,
                    video_path,
                )
                width = media_probe.width or width
                height = media_probe.height or height
                duration = media_probe.duration or duration

                if (
                    not isinstance(duration, (int, float))
                    or not math.isfinite(duration)
                    or duration <= 0
                ):
                    duration = await loop.run_in_executor(None, _probe_duration, video_path)
                parts, part_dir = await loop.run_in_executor(None, _split_video, video_path, duration)
                if len(parts) > 1:
                    if inline_message_id:
                        raise InlineSplitError
                    if can_attach_music_link(without_description):
                        _, music_deep_link = await prepare_music_caption_link(
                            bot,
                            session,
                            source_url,
                            format_id,
                        )
                    for number, part in enumerate(parts, start=1):
                        part_description = f"Часть {number}/{len(parts)}"
                        if number == 1 and description:
                            part_description = f"{part_description}\n\n{description}"
                        for attempt in range(2):
                            try:
                                await _deliver_video(
                                    bot, chat_id, None,
                                    file_id=None, video_path=part,
                                    width=width, height=height,
                                    description=part_description, source_url=source_url if number == 1 else None,
                                    likes=likes if number == 1 else None,
                                    reply_to_message_id=reply_to_message_id,
                                    placeholder_message_id=placeholder_message_id,
                                    has_spoiler=has_spoiler,
                                    without_description=without_description,
                                    music_deep_link=(
                                        music_deep_link if number == 1 else None
                                    ),
                                )
                                break
                            except TelegramRetryAfter as error:
                                if attempt:
                                    raise
                                await asyncio.sleep(error.retry_after)
                        placeholder_message_id = None
                        part.unlink(missing_ok=True)
                    video_path.unlink(missing_ok=True)
                    await session.delete(item)
                    await session.commit()
                    continue

                # 4. Отправляем в Storage-канал
                file_id: str | None = None
                storage_message_id: int | None = None

                if STORAGE_CHAT_ID:
                    try:
                        storage_msg = await bot.send_video(
                            chat_id=STORAGE_CHAT_ID,
                            video=FSInputFile(str(video_path)),
                            width=width,
                            height=height,
                            supports_streaming=True,
                        )
                        if storage_msg.video:
                            file_id = storage_msg.video.file_id
                            storage_message_id = storage_msg.message_id
                            logging.info(
                                "Storage: tiktok_id=%s → message_id=%s",
                                final_id, storage_message_id,
                            )
                    except (TelegramBadRequest, TelegramForbiddenError) as e:
                        logging.warning("Не удалось отправить в Storage-канал: %s", e)

                # 5. Доставляем видео
                if can_attach_music_link(without_description):
                    _, music_deep_link = await prepare_music_caption_link(
                        bot,
                        session,
                        source_url,
                        format_id,
                    )
                result_file_id = await _deliver_video(
                    bot, chat_id, inline_message_id,
                    file_id=file_id, video_path=video_path,
                    width=width, height=height,
                    description=description, source_url=source_url,
                    likes=likes,
                    reply_to_message_id=reply_to_message_id,
                    placeholder_message_id=placeholder_message_id,
                    has_spoiler=has_spoiler,
                    without_description=without_description,
                    music_deep_link=music_deep_link,
                )
                if result_file_id and not file_id:
                    file_id = result_file_id  # сохраним для кеша
                # 6. Удаляем файл и кешируем
                os.remove(video_path)

                if format_id is None and final_id and file_id:
                    final_cache_key = _media_cache_key(final_id)
                    if cached and cached.tiktok_id == final_cache_key:
                        cached.file_id = file_id
                        cached.storage_message_id = storage_message_id
                        cached.description = description
                    else:
                        session.add(VideoCache(
                            tiktok_id=final_cache_key,
                            file_id=file_id,
                            storage_message_id=storage_message_id,
                            description=description,
                            music_tracks=None,
                        ))

                await session.delete(item)
                await session.commit()

            except MissingAudioError:
                logging.warning("Источник не отдал аудиодорожку: chat=%s url=%s", chat_id, tiktok_link)
                await session.rollback()
                _remove_download_files(filename)
                await _show_download_error(
                    bot, chat_id, inline_message_id, placeholder_message_id,
                    "❌ TikTok не отдал аудиодорожку. Видео не отправлено без звука — попробуйте позже.",
                    source_url,
                )
                await session.delete(item)
                await session.commit()

            except MediaPreparationError:
                logging.exception("Не удалось подготовить совместимое видео: chat=%s url=%s", chat_id, tiktok_link)
                await session.rollback()
                _remove_download_files(filename)
                await _show_download_error(
                    bot, chat_id, inline_message_id, placeholder_message_id,
                    "❌ Не удалось загрузить видео.",
                    source_url,
                )
                await session.delete(item)
                await session.commit()

            except VideoTooLargeError:
                logging.info("Видео больше 1 GB: chat=%s url=%s", chat_id, tiktok_link)
                await session.rollback()
                _remove_download_files(filename)
                await _show_download_error(
                    bot, chat_id, inline_message_id, placeholder_message_id,
                    "❌ Видео больше 1 ГБ. Выберите другое качество.",
                    source_url,
                )
                await session.delete(item)
                await session.commit()

            except InlineSplitError:
                _remove_download_files(filename)
                await _show_download_error(
                    bot, chat_id, inline_message_id, placeholder_message_id,
                    "❌ Большие видео отправляются частями только в личном чате с ботом.",
                    source_url,
                )
                await session.delete(item)
                await session.commit()

            except VideoSplitError:
                logging.warning("Не удалось разделить видео: chat=%s url=%s", chat_id, tiktok_link)
                _remove_download_files(filename)
                await _show_download_error(
                    bot, chat_id, inline_message_id, placeholder_message_id,
                    "❌ Не удалось безопасно разделить видео. Выберите другое качество.",
                    source_url,
                )
                await session.delete(item)
                await session.commit()

            except (TelegramBadRequest, TelegramForbiddenError) as e:
                logging.warning("Telegram delivery error for chat %s: %s", chat_id, e)
                _remove_download_files(filename)
                await _show_download_error(
                    bot, chat_id, inline_message_id, placeholder_message_id,
                    "❌ Не удалось доставить видео. Возможно, оно слишком большое.",
                    source_url,
                )
                await session.delete(item)
                await session.commit()

            except Exception:
                logging.exception("Ошибка загрузки: chat=%s url=%s", chat_id, tiktok_link)
                await session.rollback()
                _remove_download_files(filename)
                await _show_download_error(
                    bot, chat_id, inline_message_id, placeholder_message_id,
                    "❌ Не удалось скачать видео. Попробуй ещё раз.",
                    source_url,
                )
                await asyncio.sleep(3)
                await session.delete(item)
                await session.commit()
            finally:
                if part_dir:
                    shutil.rmtree(part_dir, ignore_errors=True)
                if placeholder_for_cleanup:
                    _placeholder_ready_at.pop((chat_id, placeholder_for_cleanup), None)
                if inline_message_id:
                    _inline_placeholder_ready_at.pop(inline_message_id, None)


async def process_queue(bot: Bot) -> None:
    """Фоновый цикл обработки очереди загрузок."""
    while True:
        job = await _wait_for_next_job()
        await _process_queue_job_legacy(bot, job)
