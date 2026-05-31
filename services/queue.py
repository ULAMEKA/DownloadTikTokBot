import asyncio
import logging
import os
import re
from pathlib import Path

import yt_dlp
from aiogram import Bot, html
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import (
    FSInputFile,
    InputMediaVideo,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from sqlalchemy import select

from db.session import SessionLocal
from db.models import DownloadQueue, VideoCache
from promo import PROMO_MESSAGE_HTML

BASE_DIR = Path(__file__).parent
PARENT_DIR = BASE_DIR.parent
VIDEO_DIR = PARENT_DIR / "videos"
VIDEO_DIR.mkdir(exist_ok=True)

# ID канала-хранилища (отрицательное число для каналов, например -1001234567890)
STORAGE_CHAT_ID: str | None = os.getenv("STORAGE_CHAT_ID")

# Regex для извлечения ID видео из полного URL TikTok
_TIKTOK_ID_RE = re.compile(r'/video/(\d+)')

# Лимит длины подписи в Telegram. Держим запас под промо/ссылку и
# особенности подсчёта (эмодзи в UTF-16, html-теги в видимую длину не входят).
CAPTION_LIMIT = 1024


def _normalize_url(url: str) -> str:
    """Гарантирует схему — иначе URL-кнопка/ссылка невалидны для Telegram."""
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def _source_link(url: str) -> str:
    """HTML-ссылка «Ссылка на источник» на оригинальное видео в TikTok."""
    return f'🔗 <a href="{html.quote(_normalize_url(url))}">Ссылка на источник</a>'


def _build_caption(
    description: str | None,
    source_url: str | None,
    *,
    with_source: bool,
) -> str:
    """Подпись под видео: описание ролика (если есть) + [ссылка] + промо.

    with_source=False для inline-режима — там источник вынесен в кнопку,
    поэтому ссылку в текст не добавляем. Разметка — HTML.
    """
    tail_parts: list[str] = []
    if with_source and source_url:
        tail_parts.append(_source_link(source_url))
    tail_parts.append(PROMO_MESSAGE_HTML)
    tail = "\n\n".join(tail_parts)

    desc = (description or "").strip()
    if not desc:
        return tail

    # Бюджет под описание: лимит минус хвост и запас. Режем по сырому
    # тексту (до html.quote), чтобы не разорвать html-сущность вроде &amp;.
    budget = CAPTION_LIMIT - len(tail) - 30
    if budget <= 0:
        return tail
    if len(desc) > budget:
        desc = desc[:budget - 1].rstrip() + "…"
    return f"{html.quote(desc)}\n\n{tail}"


def _get_tiktok_id(url: str) -> str | None:
    """Возвращает ID видео TikTok из ссылки (или None)."""
    m = _TIKTOK_ID_RE.search(url)
    if m:
        return m.group(1)
    # Короткая ссылка — резолвим через yt-dlp без скачивания
    try:
        opts: dict[str, object] = {"quiet": True, "no_warnings": True}
        with yt_dlp.YoutubeDL(opts) as ydl:  # type: ignore[arg-type]
            info = ydl.extract_info(url, download=False)
            if info and info.get("id"):
                return str(info["id"])
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
        # Источник — кнопкой: остаётся под видео и ведёт на оригинал в TikTok.
        markup = None
        if source_url:
            markup = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text="🔗 Ссылка на источник",
                    url=_normalize_url(source_url),
                )
            ]])
        try:
            await bot.edit_message_media(
                inline_message_id=inline_message_id,
                media=InputMediaVideo(
                    media=file_id,
                    caption=_build_caption(description, source_url, with_source=False),
                    parse_mode=ParseMode.HTML,
                ),
                reply_markup=markup,
            )
        except (TelegramBadRequest, TelegramForbiddenError) as e:
            logging.warning("edit_message_media failed: %s", e)
        return file_id

    # Обычный режим: send_video в чат. Источник — кликабельным текстом в подписи.
    caption = _build_caption(description, source_url, with_source=True)
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

            try:
                loop = asyncio.get_running_loop()

                # 1. ID видео TikTok
                tiktok_id = await loop.run_in_executor(None, _get_tiktok_id, tiktok_link)

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
                            description=cached.description, source_url=tiktok_link,
                        )
                        await session.delete(item)
                        await session.commit()
                        continue

                # 3. Скачиваем
                outtmpl = str(VIDEO_DIR / f"{filename}.%(ext)s")  # cspell:disable-line
                downloaded_id, width, height, description = await loop.run_in_executor(
                    None, _ydl_download, tiktok_link, outtmpl
                )

                # Используем ID из скачивания (актуально для коротких ссылок)
                final_id: str | None = tiktok_id or downloaded_id

                # Ищем скачанный файл (расширение может отличаться от .mp4)
                matches = list(VIDEO_DIR.glob(f"{filename}.*"))
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
                    description=description, source_url=tiktok_link,
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
