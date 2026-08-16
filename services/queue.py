import asyncio
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

import imageio_ffmpeg
import yt_dlp
from aiogram import Bot, html
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaVideo
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.session import SessionLocal
from db.models import DownloadQueue, MusicRequest, VideoCache
from caption import build_caption, source_link
from promo import AD_DISPLAY_SECONDS

BASE_DIR = Path(__file__).parent
PARENT_DIR = BASE_DIR.parent
VIDEO_DIR = PARENT_DIR / "videos"
VIDEO_DIR.mkdir(exist_ok=True)
PHOTO_DURATION = 2.5
SLIDESHOW_WIDTH = 720
SLIDESHOW_HEIGHT = 1280
FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()

# ID канала-хранилища (отрицательное число для каналов, например -1001234567890)
STORAGE_CHAT_ID: str | None = os.getenv("STORAGE_CHAT_ID")

# Regex для извлечения ID видео из полного URL TikTok
_TIKTOK_ID_RE = re.compile(r'/(?:video|photo)/(\d+)')

_IMAGE_EXTENSIONS = {".avif", ".jpeg", ".jpg", ".png", ".webp"}
_AUDIO_EXTENSIONS = {".aac", ".m4a", ".mp3", ".ogg", ".opus", ".wav"}
_VIDEO_EXTENSIONS = {".m4v", ".mkv", ".mov", ".mp4", ".webm"}
DEFAULT_FORMAT = "best[vcodec^=h264]/best[vcodec!*=265][vcodec!*=hev]/best"
MAX_SOURCE_VIDEO_BYTES = 1_000_000_000
MAX_TELEGRAM_PART_BYTES = 48_000_000
MAX_QUALITY_OPTIONS = 6
MAX_ACTIVE_DOWNLOADS_PER_USER = 2
MUSIC_CHUNK_SECONDS = 15
MUSIC_RECOGNITION_TIMEOUT_SECONDS = 15
MUSIC_MESSAGE_LIMIT = 4096
MUSIC_JOB_PREFIX = "music:"
MUSIC_PAGE_PREFIX = "music_page:"
QUEUE_LIMIT_MESSAGE = (
    "Можно поставить в очередь не больше 2 видео одновременно. "
    "Дождись завершения текущих загрузок."
)
_placeholder_ready_at: dict[tuple[str, int], float] = {}
_inline_placeholder_ready_at: dict[str, float] = {}
_enqueue_lock = asyncio.Lock()


@dataclass(frozen=True)
class VideoQuality:
    format_id: str | None
    label: str
    filesize: int | None


MusicTrack = tuple[int, str, str]


class VideoTooLargeError(Exception):
    pass


class VideoSplitError(Exception):
    pass


class InlineSplitError(Exception):
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
        return True


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


def _quality_options(formats: object) -> list[VideoQuality]:
    if not isinstance(formats, list):
        return []

    options: list[VideoQuality] = []
    seen: set[tuple[object, object, object, int | None]] = set()
    for item in formats:
        if not isinstance(item, dict) or not _is_h264(item.get("vcodec")):
            continue
        if item.get("acodec") in (None, "none"):
            continue
        format_id = item.get("format_id")
        if not isinstance(format_id, str):
            continue
        filesize = _to_int(item.get("filesize") or item.get("filesize_approx"))
        if filesize is None or filesize > MAX_SOURCE_VIDEO_BYTES:
            continue
        width = _to_int(item.get("width"))
        height = _to_int(item.get("height"))
        bitrate = _to_int(item.get("tbr"))
        key = (width, height, bitrate, filesize)
        if key in seen:
            continue
        seen.add(key)
        resolution = f"{width}×{height}" if width and height else "Видео"
        bitrate_label = f"{bitrate} кбит/с" if bitrate else None
        label = " · ".join(part for part in (
            resolution, bitrate_label, _format_megabytes(filesize),
        ) if part)
        options.append(VideoQuality(format_id, label, filesize))

    options.sort(key=lambda option: option.filesize or -1, reverse=True)
    return options[:MAX_QUALITY_OPTIONS]


def list_video_qualities(url: str) -> list[VideoQuality]:
    opts: dict[str, object] = {"quiet": True, "no_warnings": True}
    with yt_dlp.YoutubeDL(opts) as ydl:  # type: ignore[arg-type]
        info = ydl.extract_info(url, download=False)
    return _quality_options(info.get("formats") if info else None)


def needs_quality_choice(qualities: list[VideoQuality]) -> bool:
    """Only ask when the available source is too large for one Telegram video."""
    return any(
        quality.filesize is not None and quality.filesize > MAX_TELEGRAM_PART_BYTES
        for quality in qualities
    )


def _resolve_media(url: str) -> tuple[str | None, str, int | None]:
    m = _TIKTOK_ID_RE.search(url)
    fallback_id = m.group(1) if m else None
    try:
        opts: dict[str, object] = {"quiet": True, "no_warnings": True}
        with yt_dlp.YoutubeDL(opts) as ydl:  # type: ignore[arg-type]
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
        pass
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
    """Скачивает выбранный H.264-вариант и возвращает его метаданные."""
    # noinspection SpellCheckingInspection
    opts: dict[str, object] = {
        "outtmpl": outtmpl,  # cspell:disable-line
        "quiet": True,
        "no_warnings": True,
        "format": format_id or DEFAULT_FORMAT,
        "max_filesize": MAX_SOURCE_VIDEO_BYTES,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:  # type: ignore[arg-type]
        info = ydl.extract_info(url, download=False)
        if not info:
            return None, None, None, None, None
        expected_size = _to_int(info.get("filesize") or info.get("filesize_approx"))
        if expected_size is not None and expected_size > MAX_SOURCE_VIDEO_BYTES:
            raise VideoTooLargeError
        info = ydl.extract_info(url, download=True)
        if not info:
            return None, None, None, None, None
        vid = str(info["id"]) if info.get("id") else None
        width, height = _extract_dimensions(info)
        raw_desc = info.get("description")
        description = str(raw_desc) if raw_desc else None
        return vid, width, height, description, _to_float(info.get("duration"))


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
) -> tuple[int | None, int | None] | None:
    with tempfile.TemporaryDirectory(dir=VIDEO_DIR, prefix="slideshow_") as directory:
        work_dir = Path(directory)
        result = subprocess.run(
            [
                sys.executable, "-m", "gallery_dl", "--config-ignore", "--no-input",
                "--filesize-max", str(MAX_SOURCE_VIDEO_BYTES),
                "--no-mtime", "--directory", str(work_dir),
                "--filename", "{num}_{type}.{extension}",
                "--option", "extractor.tiktok.videos=true",
                "--option", "extractor.tiktok.photos=true",
                "--option", "extractor.tiktok.audio=true",
                "--option", "extractor.instagram.videos=true",
                url,
            ],
            capture_output=True,
            text=True,
        )
        files = [path for path in work_dir.iterdir() if path.is_file()]
        video = next(
            (path for path in files if path.suffix.lower() in _VIDEO_EXTENSIONS),
            None,
        )
        if video:
            if video.stat().st_size > MAX_SOURCE_VIDEO_BYTES:
                raise VideoTooLargeError
            video.replace(output.with_suffix(video.suffix))
            return (None, None)

        images = sorted(
            (path for path in files if path.suffix.lower() in _IMAGE_EXTENSIONS),
            key=lambda path: int(path.name.partition("_")[0]),
        )
        if not images:
            if result.returncode:
                logging.info("gallery-dl не скачал медиа: %s", result.stderr.strip())
            return None

        audio = next((path for path in files if path.suffix.lower() in _AUDIO_EXTENSIONS), None)
        _build_slideshow(images, audio, output)
        if output.stat().st_size > MAX_SOURCE_VIDEO_BYTES:
            raise VideoTooLargeError
        return SLIDESHOW_WIDTH, SLIDESHOW_HEIGHT


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
    except Exception:
        if format_id is not None:
            raise
        dimensions = _gallery_download(url, slideshow_path)
        if dimensions is not None:
            return _resolve_media(url)[0], *dimensions, None, None
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
        await asyncio.sleep(max(0, ready_at - asyncio.get_running_loop().time()))
        try:
            await bot.edit_message_media(
                inline_message_id=inline_message_id,
                media=InputMediaVideo(
                    media=file_id,
                    caption=(
                        None
                        if without_description
                        else build_caption(
                            description,
                            source_url,
                            with_source=True,
                            likes=likes,
                            has_spoiler=has_spoiler,
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
        )
        if msg.video:
            return msg.video.file_id

    return None


def _remove_download_files(filename: str) -> None:
    for path in VIDEO_DIR.glob(f"{filename}.*"):
        if path.is_file():
            path.unlink(missing_ok=True)


def can_offer_music(without_description: bool, inline_message_id: str | None) -> bool:
    """Literal-video and video-inline modes stay free of the extra status message."""
    return not without_description and not inline_message_id


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
    result = subprocess.run(
        [FFMPEG_EXE, "-hide_banner", "-i", str(video_path)],
        check=False,
        capture_output=True,
        text=True,
        errors="replace",
    )
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr)
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


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


async def _create_music_request(
    session: AsyncSession,
    user_id: str,
    chat_id: str,
    tiktok_link: str,
    format_id: str | None,
    music_tracks: str | None = None,
    inline_message_id: str | None = None,
) -> MusicRequest:
    request = MusicRequest(
        token=uuid.uuid4().hex,
        user_id=user_id,
        chat_id=chat_id,
        inline_message_id=inline_message_id,
        tiktok_link=tiktok_link,
        format_id=format_id,
        state="done" if music_tracks is not None else "ready",
        music_tracks=music_tracks,
    )
    session.add(request)
    # The row must be visible before Telegram receives the callback button.
    await session.commit()
    return request


async def create_music_status(
    bot: Bot,
    session: AsyncSession,
    user_id: str,
    chat_id: str,
    tiktok_link: str,
    format_id: str | None,
    music_tracks: str | None = None,
) -> None:
    request: MusicRequest | None = None
    try:
        request = await _create_music_request(
            session,
            user_id,
            chat_id,
            tiktok_link,
            format_id,
            music_tracks,
        )
        message = await bot.send_message(
            chat_id,
            _music_status_text(
                "🎵 Нажмите кнопку ниже, чтобы распознать музыку.",
                tiktok_link,
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=music_button_markup(request.token),
        )
        request.status_message_id = message.message_id
        await session.commit()
    except Exception as error:
        logging.warning("Не удалось создать карточку распознавания: %s", error)
        await session.rollback()
        if request is not None:
            try:
                request = await session.get(MusicRequest, request.token)
                if request is not None:
                    await session.delete(request)
                    await session.commit()
            except Exception:
                await session.rollback()


async def process_queue(bot: Bot) -> None:
    """Фоновый цикл обработки очереди загрузок."""
    while True:
        async with SessionLocal() as session:
            stmt = select(DownloadQueue).order_by(DownloadQueue.id).limit(1)
            result = await session.execute(stmt)
            item = result.scalar()

            if item is None:
                await asyncio.sleep(1)
                continue

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

                # 2. Проверяем кеш
                cached: VideoCache | None = None
                if tiktok_id and format_id is None:
                    cache_res = await session.execute(
                        select(VideoCache).where(VideoCache.tiktok_id == tiktok_id)
                    )
                    cached = cache_res.scalar()
                    if cached and cached.file_id:
                        logging.info("Cache hit: tiktok_id=%s → отправляем без загрузки", tiktok_id)
                        delivered_file_id = await _deliver_video(
                            bot, chat_id, inline_message_id,
                            file_id=cached.file_id, video_path=None,
                            description=cached.description, source_url=source_url,
                            likes=likes,
                            reply_to_message_id=reply_to_message_id,
                            placeholder_message_id=placeholder_message_id,
                            has_spoiler=has_spoiler,
                            without_description=without_description,
                        )
                        if (
                            delivered_file_id
                            and can_offer_music(without_description, inline_message_id)
                            and item.user_id
                        ):
                            await create_music_status(
                                bot,
                                session,
                                item.user_id,
                                chat_id,
                                source_url,
                                format_id,
                                cached.music_tracks,
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
                                )
                                break
                            except TelegramRetryAfter as error:
                                if attempt:
                                    raise
                                await asyncio.sleep(error.retry_after)
                        placeholder_message_id = None
                        part.unlink(missing_ok=True)
                    video_path.unlink(missing_ok=True)
                    if can_offer_music(without_description, inline_message_id) and item.user_id:
                        await create_music_status(
                            bot,
                            session,
                            item.user_id,
                            chat_id,
                            source_url,
                            format_id,
                        )
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
                )
                if result_file_id and not file_id:
                    file_id = result_file_id  # сохраним для кеша
                if (
                    result_file_id
                    and can_offer_music(without_description, inline_message_id)
                    and item.user_id
                ):
                    await create_music_status(
                        bot,
                        session,
                        item.user_id,
                        chat_id,
                        source_url,
                        format_id,
                    )

                # 6. Удаляем файл и кешируем
                os.remove(video_path)

                if format_id is None and final_id and file_id:
                    if cached and cached.tiktok_id == final_id:
                        cached.file_id = file_id
                        cached.storage_message_id = storage_message_id
                        cached.description = description
                    else:
                        session.add(VideoCache(
                            tiktok_id=final_id,
                            file_id=file_id,
                            storage_message_id=storage_message_id,
                            description=description,
                            music_tracks=None,
                        ))

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
