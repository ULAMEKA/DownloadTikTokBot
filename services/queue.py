import asyncio
import logging
import os
import uuid
from pathlib import Path

import yt_dlp
from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import FSInputFile, InputMediaVideo
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from config import STORAGE_CHAT_ID
from db.session import SessionLocal
from db.models import DownloadQueue, VideoCache
from ui.caption import build_caption
from ui.keyboards import source_button
from utils.links import media_key

VIDEO_DIR = Path(__file__).parent.parent / "videos"
VIDEO_DIR.mkdir(exist_ok=True)


# ── Постановка в очередь (producer) ──────────────────────────────────────────

async def enqueue_download(
    session: AsyncSession,
    *,
    chat_id: str,
    source_url: str,
    inline_message_id: str | None = None,
) -> DownloadQueue:
    """Добавляет ссылку в очередь загрузок и возвращает созданную запись."""
    item = DownloadQueue(
        chat_id=chat_id,
        filename=str(uuid.uuid4()),
        source_url=source_url,
        inline_message_id=inline_message_id,
    )
    session.add(item)
    await session.commit()
    return item


async def queue_position(session: AsyncSession, item: DownloadQueue) -> int | None:
    """Позиция записи в очереди (число элементов с id <= item.id)."""
    stmt = select(func.count()).select_from(DownloadQueue).where(DownloadQueue.id <= item.id)
    result = await session.execute(stmt)
    return result.scalar()


# ── Работа с yt-dlp ──────────────────────────────────────────────────────────

def _resolve_media_key(url: str) -> str | None:
    """Ключ кеша "<платформа>:<id>" по ссылке (или None).

    Сначала пытаемся вытащить из URL без сети; для коротких ссылок
    (vm./vt.tiktok) резолвим через yt-dlp без скачивания.
    """
    key = media_key(url)
    if key:
        return key
    try:
        opts: dict[str, object] = {"quiet": True, "no_warnings": True}
        with yt_dlp.YoutubeDL(opts) as ydl:  # type: ignore[arg-type]
            info = ydl.extract_info(url, download=False)
            if info and info.get("id") and info.get("extractor"):
                return f"{str(info['extractor']).lower()}:{info['id']}"
    except Exception:
        pass
    return None


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
    """Скачивает видео, возвращает (media_key, width, height, description)."""
    # noinspection SpellCheckingInspection
    opts: dict[str, object] = {
        "outtmpl": outtmpl,  # cspell:disable-line
        "quiet": True,
        "no_warnings": True,
        # Единый формат для всех платформ (TikTok/Reels/Shorts): лучшее видео H.264 +
        # лучшее аудио AAC, склейка через ffmpeg. H.264/AAC обязательны — HEVC/opus
        # Telegram играет без звука. Запасные варианты: готовый muxed-файл H.264,
        # затем любой (на случай, когда раздельных дорожек/ffmpeg нет).
        "format": (
            "bv*[vcodec~='^(avc1|h264)']+ba[acodec~='^(mp4a|aac)']/"
            "b[vcodec~='^(avc1|h264)']/b"
        ),
        "merge_output_format": "mp4",
    }
    with yt_dlp.YoutubeDL(opts) as ydl:  # type: ignore[arg-type]
        info = ydl.extract_info(url, download=True)
        if not info:
            return None, None, None, None
        if info.get("id") and info.get("extractor"):
            key = f"{str(info['extractor']).lower()}:{info['id']}"
        else:
            key = None
        width, height = _extract_dimensions(info)
        raw_desc = info.get("description")
        description = str(raw_desc) if raw_desc else None
        return key, width, height, description


# ── Доставка видео ───────────────────────────────────────────────────────────

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
) -> str | None:
    """Отправляет видео пользователю, возвращает file_id для кеша (или None)."""
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
        # Источник — кнопкой: остаётся под видео и ведёт на оригинал.
        markup = source_button(source_url) if source_url else None
        try:
            await bot.edit_message_media(
                inline_message_id=inline_message_id,
                media=InputMediaVideo(
                    media=file_id,
                    caption=build_caption(description, source_url, with_source=False),
                    parse_mode=ParseMode.HTML,
                ),
                reply_markup=markup,
            )
        except (TelegramBadRequest, TelegramForbiddenError) as e:
            logging.warning("edit_message_media failed: %s", e)
        return file_id

    # Обычный режим: send_video в чат. Источник — кликабельным текстом в подписи.
    caption = build_caption(description, source_url, with_source=True)
    if file_id:
        await bot.send_video(
            chat_id=chat_id,
            video=file_id,
            caption=caption,
            parse_mode=ParseMode.HTML,
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
        )
        if msg.video:
            return msg.video.file_id

    return None


async def _send_to_storage(
    bot: Bot,
    video_path: Path,
    width: int | None,
    height: int | None,
    key: str | None,
) -> tuple[str | None, int | None]:
    """Заливает видео в Storage-канал ради постоянного file_id.

    Возвращает (file_id, message_id) или (None, None), если канал не задан
    либо отправка не удалась.
    """
    if not STORAGE_CHAT_ID:
        return None, None
    try:
        msg = await bot.send_video(
            chat_id=STORAGE_CHAT_ID,
            video=FSInputFile(str(video_path)),
            width=width,
            height=height,
        )
        if msg.video:
            logging.info("Storage: %s → message_id=%s", key, msg.message_id)
            return msg.video.file_id, msg.message_id
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        logging.warning("Не удалось отправить в Storage-канал: %s", e)
    return None, None


# ── Очистка и уведомления ────────────────────────────────────────────────────

def _cleanup_files(filename: str) -> None:
    """Удаляет все файлы загрузки с данным именем (любое расширение)."""
    for f in VIDEO_DIR.glob(f"{filename}.*"):
        try:
            os.remove(f)
        except OSError as e:
            logging.warning("Не удалось удалить файл %s: %s", f, e)


async def _notify_failure(
    bot: Bot,
    chat_id: str,
    inline_message_id: str | None,
    *,
    inline_text: str,
    chat_text: str,
) -> None:
    """Сообщает пользователю об ошибке: правит inline-сообщение либо шлёт в чат."""
    try:
        if inline_message_id:
            await bot.edit_message_text(inline_message_id=inline_message_id, text=inline_text)
        else:
            await bot.send_message(chat_id=chat_id, text=chat_text)
    except (TelegramBadRequest, TelegramForbiddenError):
        pass


# ── Фоновый цикл обработки очереди (consumer) ────────────────────────────────

async def _next_item(session: AsyncSession) -> DownloadQueue | None:
    """Возвращает самую старую запись очереди (или None, если пусто)."""
    stmt = select(DownloadQueue).order_by(DownloadQueue.id).limit(1)
    result = await session.execute(stmt)
    return result.scalar()


async def _discard(session: AsyncSession, item: DownloadQueue) -> None:
    """Удаляет запись из очереди и коммитит (вместе с накопленными изменениями)."""
    await session.delete(item)
    await session.commit()


async def _process_item(
    bot: Bot,
    session: AsyncSession,
    *,
    chat_id: str,
    filename: str,
    source_url: str,
    inline_message_id: str | None,
) -> None:
    """Обрабатывает одну запись очереди: кеш → скачивание → доставка → кеш."""
    loop = asyncio.get_running_loop()

    # 1. Ключ кеша по ссылке (для коротких ссылок резолвится через yt-dlp)
    key = await loop.run_in_executor(None, _resolve_media_key, source_url)

    # 2. Проверяем кеш — при попадании отдаём видео без скачивания
    if key:
        cache_res = await session.execute(
            select(VideoCache).where(VideoCache.media_id == key)
        )
        cached = cache_res.scalar()
        if cached and cached.file_id:
            logging.info("Cache hit: %s → отправляем без загрузки", key)
            await _deliver_video(
                bot, chat_id, inline_message_id,
                file_id=cached.file_id, video_path=None,
                description=cached.description, source_url=source_url,
            )
            return

    # 3. Скачиваем
    outtmpl = str(VIDEO_DIR / f"{filename}.%(ext)s")  # cspell:disable-line
    info_key, width, height, description = await loop.run_in_executor(
        None, _ydl_download, source_url, outtmpl
    )
    # Ключ из URL приоритетнее (стабилен для будущих проверок); info_key —
    # запасной для коротких ссылок, где из URL ID не вытащить.
    final_key = key or info_key

    # Ищем скачанный файл (расширение может отличаться от .mp4)
    matches = list(VIDEO_DIR.glob(f"{filename}.*"))
    if not matches:
        raise FileNotFoundError(f"Файл после загрузки не найден: {filename}")
    video_path = matches[0]

    # 4. Отправляем в Storage-канал ради постоянного file_id
    file_id, storage_message_id = await _send_to_storage(bot, video_path, width, height, final_key)

    # 5. Доставляем видео пользователю
    result_file_id = await _deliver_video(
        bot, chat_id, inline_message_id,
        file_id=file_id, video_path=video_path,
        width=width, height=height,
        description=description, source_url=source_url,
    )
    if result_file_id and not file_id:
        file_id = result_file_id  # сохраним для кеша

    # 6. Удаляем файл и кешируем file_id
    os.remove(video_path)
    if final_key and file_id:
        session.add(VideoCache(
            media_id=final_key,
            file_id=file_id,
            storage_message_id=storage_message_id,
            description=description,
        ))


async def process_queue(bot: Bot) -> None:
    """Фоновый цикл обработки очереди загрузок."""
    while True:
        async with SessionLocal() as session:
            item = await _next_item(session)
            if item is None:
                await asyncio.sleep(1)
                continue

            # Защита от битых записей
            if item.chat_id is None or item.filename is None or item.source_url is None:
                logging.warning("Битая запись очереди id=%s — удаляем", item.id)
                await _discard(session, item)
                continue

            chat_id = item.chat_id
            filename = item.filename
            source_url = item.source_url
            inline_message_id = item.inline_message_id

            try:
                await _process_item(
                    bot, session,
                    chat_id=chat_id, filename=filename,
                    source_url=source_url, inline_message_id=inline_message_id,
                )
            except (TelegramBadRequest, TelegramForbiddenError) as e:
                # Чат недоступен или видео > 50 МБ (лимит Bot API).
                logging.warning("Telegram delivery error for chat %s: %s", chat_id, e)
                _cleanup_files(filename)
                await _notify_failure(
                    bot, chat_id, inline_message_id,
                    inline_text="❌ Не удалось доставить видео. Возможно, оно слишком большое.",
                    chat_text="Не удалось доставить видео. Возможно, оно слишком большое.",
                )
            except Exception:
                logging.exception("Ошибка загрузки: chat=%s url=%s", chat_id, source_url)
                # Ошибка могла инвалидировать сессию — без rollback delete ниже
                # упадёт с PendingRollbackError.
                await session.rollback()
                _cleanup_files(filename)
                await _notify_failure(
                    bot, chat_id, inline_message_id,
                    inline_text="❌ Не удалось скачать видео. Попробуй ещё раз.",
                    chat_text="Что-то пошло не так! Попробуй ещё раз.",
                )
                await asyncio.sleep(3)

            await _discard(session, item)
