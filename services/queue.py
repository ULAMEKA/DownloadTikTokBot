import asyncio
import logging
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import imageio_ffmpeg
import yt_dlp
from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import FSInputFile, InputMediaVideo
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.session import SessionLocal
from db.models import DownloadQueue, VideoCache
from caption import build_caption, stats_keyboard
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
MediaStats = tuple[int | None, int | None, int | None]
EMPTY_STATS: MediaStats = (None, None, None)
MAX_ACTIVE_DOWNLOADS_PER_USER = 2
QUEUE_LIMIT_MESSAGE = (
    "Можно поставить в очередь не больше 2 видео одновременно. "
    "Дождись завершения текущих загрузок."
)
_placeholder_ready_at: dict[tuple[str, int], float] = {}
_inline_placeholder_ready_at: dict[str, float] = {}
_enqueue_lock = asyncio.Lock()


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


def _resolve_media(url: str) -> tuple[str | None, str, MediaStats]:
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
                stats = (
                    _to_int(info.get("like_count")),
                    _to_int(info.get("comment_count")),
                    _to_int(info.get("view_count")),
                )
                return media_id, source_url, stats
    except Exception:
        pass
    return fallback_id, url, EMPTY_STATS


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


def _ydl_download(url: str, outtmpl: str) -> tuple[str | None, int | None, int | None, str | None]:
    """Скачивает видео TikTok, возвращает (ID, width, height, description)."""
    # noinspection SpellCheckingInspection
    opts: dict[str, object] = {
        "outtmpl": outtmpl,  # cspell:disable-line
        "quiet": True,
        "no_warnings": True,
        # Только H.264: HEVC (самый качественный mp4 у TikTok) Telegram играет
        # без звука. Форматы TikTok — готовые muxed-файлы, ffmpeg не нужен.
        "format": "best[vcodec^=h264]/best[vcodec!*=265][vcodec!*=hev]/best",
    }
    with yt_dlp.YoutubeDL(opts) as ydl:  # type: ignore[arg-type]
        info = ydl.extract_info(url, download=True)
        if not info:
            return None, None, None, None
        vid = str(info["id"]) if info.get("id") else None
        width, height = _extract_dimensions(info)
        raw_desc = info.get("description")
        description = str(raw_desc) if raw_desc else None
        return vid, width, height, description


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
        return SLIDESHOW_WIDTH, SLIDESHOW_HEIGHT


def _download_media(
    url: str,
    outtmpl: str,
    slideshow_path: Path,
) -> tuple[str | None, int | None, int | None, str | None]:
    try:
        return _ydl_download(url, outtmpl)
    except Exception:
        dimensions = _gallery_download(url, slideshow_path)
        if dimensions is not None:
            return _resolve_media(url)[0], *dimensions, None
        raise


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
    stats: MediaStats = EMPTY_STATS,
    reply_to_message_id: int | None = None,
    placeholder_message_id: int | None = None,
    has_spoiler: bool = False,
    without_description: bool = False,
) -> str | None:
    """Отправляет видео пользователю, возвращает file_id для кеша (или None)."""
    markup = stats_keyboard(stats)

    # Inline-режим: редактируем сообщение через file_id из Storage
    if inline_message_id:
        if not file_id:
            # Без file_id в inline-режиме залить файл нельзя — меняем
            # плейсхолдер на ошибку, иначе он висит вечно.
            logging.warning("inline_message_id есть, но file_id отсутствует — пропускаем")
            try:
                await bot.edit_message_text(
                    inline_message_id=inline_message_id,
                    text="❌ Не удалось доставить видео. Попробуй ещё раз.",
                )
            except (TelegramBadRequest, TelegramForbiddenError):
                pass
            return None
        # Источник — кнопкой: остаётся под видео и ведёт на оригинал в TikTok.
        ready_at = _inline_placeholder_ready_at.pop(inline_message_id, 0)
        await asyncio.sleep(max(0, ready_at - asyncio.get_running_loop().time()))
        try:
            await bot.edit_message_media(
                inline_message_id=inline_message_id,
                media=InputMediaVideo(
                    media=file_id,
                    caption=build_caption(
                        None if without_description else description,
                        source_url,
                        with_source=True,
                        has_spoiler=has_spoiler,
                    ),
                    parse_mode=ParseMode.HTML,
                    has_spoiler=has_spoiler,
                ),
                reply_markup=markup,
            )
        except (TelegramBadRequest, TelegramForbiddenError) as e:
            logging.warning("edit_message_media failed: %s", e)
        return file_id

    # Обычный режим: send_video в чат. Источник — кликабельным текстом в подписи.
    caption = build_caption(
        None if without_description else description,
        source_url,
        with_source=True,
        has_spoiler=has_spoiler,
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
            reply_markup=markup,
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
            reply_markup=markup,
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
            reply_markup=markup,
        )
        if msg.video:
            return msg.video.file_id

    return None


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

            chat_id: str = item.chat_id
            filename: str = item.filename
            tiktok_link: str = item.tiktok_link
            inline_message_id: str | None = item.inline_message_id
            reply_to_message_id: int | None = item.reply_to_message_id
            placeholder_message_id = (
                -reply_to_message_id
                if reply_to_message_id is not None and reply_to_message_id < 0
                else None
            )
            if placeholder_message_id:
                reply_to_message_id = None
            direct_mode = (
                inline_message_id.removeprefix("direct:")
                if inline_message_id and inline_message_id.startswith("direct:")
                else None
            )
            has_spoiler = direct_mode == "spoiler"
            without_description = direct_mode == "no_description"
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
                tiktok_id, source_url, stats = await loop.run_in_executor(
                    None, _resolve_media, tiktok_link
                )

                # 2. Проверяем кеш
                if tiktok_id:
                    cache_res = await session.execute(
                        select(VideoCache).where(VideoCache.tiktok_id == tiktok_id)
                    )
                    cached = cache_res.scalar()
                    if cached and cached.file_id:
                        logging.info("Cache hit: tiktok_id=%s → отправляем без загрузки", tiktok_id)
                        await _deliver_video(
                            bot, chat_id, inline_message_id,
                            file_id=cached.file_id, video_path=None,
                            description=cached.description, source_url=source_url,
                            stats=stats,
                            reply_to_message_id=reply_to_message_id,
                            placeholder_message_id=placeholder_message_id,
                            has_spoiler=has_spoiler,
                            without_description=without_description,
                        )
                        await session.delete(item)
                        await session.commit()
                        continue

                # 3. Скачиваем
                outtmpl = str(VIDEO_DIR / f"{filename}.%(ext)s")  # cspell:disable-line
                slideshow_path = VIDEO_DIR / f"{filename}.mp4"
                downloaded_id, width, height, description = await loop.run_in_executor(
                    None, _download_media, tiktok_link, outtmpl, slideshow_path
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
                    stats=stats,
                    reply_to_message_id=reply_to_message_id,
                    placeholder_message_id=placeholder_message_id,
                    has_spoiler=has_spoiler,
                    without_description=without_description,
                )
                if result_file_id and not file_id:
                    file_id = result_file_id  # сохраним для кеша

                # 6. Удаляем файл и кешируем
                os.remove(video_path)

                if final_id and file_id:
                    session.add(VideoCache(
                        tiktok_id=final_id,
                        file_id=file_id,
                        storage_message_id=storage_message_id,
                        description=description,
                    ))

                await session.delete(item)
                await session.commit()

            except (TelegramBadRequest, TelegramForbiddenError) as e:
                # Чат недоступен или видео > 50 МБ (лимит Bot API).
                logging.warning("Telegram delivery error for chat %s: %s", chat_id, e)
                for f in VIDEO_DIR.glob(f"{filename}.*"):
                    os.remove(f)
                if inline_message_id:
                    try:
                        await bot.edit_message_text(
                            inline_message_id=inline_message_id,
                            text="❌ Не удалось доставить видео. Возможно, оно слишком большое.",
                        )
                    except (TelegramBadRequest, TelegramForbiddenError):
                        pass
                elif placeholder_message_id:
                    try:
                        await bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=placeholder_message_id,
                            text="❌ Не удалось доставить видео. Возможно, оно слишком большое.",
                        )
                    except (TelegramBadRequest, TelegramForbiddenError):
                        pass
                else:
                    try:
                        await bot.send_message(
                            chat_id=chat_id,
                            text="Не удалось доставить видео. Возможно, оно слишком большое.",
                        )
                    except (TelegramBadRequest, TelegramForbiddenError):
                        pass
                await session.delete(item)
                await session.commit()

            except Exception:
                logging.exception("Ошибка загрузки: chat=%s url=%s", chat_id, tiktok_link)
                # Ошибка могла инвалидировать сессию — без rollback
                # delete+commit ниже упадут с PendingRollbackError.
                await session.rollback()
                for f in VIDEO_DIR.glob(f"{filename}.*"):
                    os.remove(f)
                # При ошибке в inline-режиме — редактируем статус
                if inline_message_id:
                    try:
                        await bot.edit_message_text(
                            inline_message_id=inline_message_id,
                            text="❌ Не удалось скачать видео. Попробуй ещё раз.",
                        )
                    except (TelegramBadRequest, TelegramForbiddenError):
                        pass
                elif placeholder_message_id:
                    try:
                        await bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=placeholder_message_id,
                            text="❌ Не удалось скачать видео. Попробуй ещё раз.",
                        )
                    except (TelegramBadRequest, TelegramForbiddenError):
                        pass
                else:
                    try:
                        await bot.send_message(
                            chat_id=chat_id,
                            text="Что-то пошло не так! Попробуй ещё раз.",
                        )
                    except (TelegramBadRequest, TelegramForbiddenError):
                        pass
                await asyncio.sleep(3)
                await session.delete(item)
                await session.commit()
            finally:
                if placeholder_message_id:
                    _placeholder_ready_at.pop((chat_id, placeholder_message_id), None)
                if inline_message_id:
                    _inline_placeholder_ready_at.pop(inline_message_id, None)
