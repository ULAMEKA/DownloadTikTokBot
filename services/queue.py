import asyncio
import logging
import os
import re
from pathlib import Path

import yt_dlp
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import FSInputFile, InputMediaVideo
from sqlalchemy import select

from db.session import SessionLocal
from db.models import DownloadQueue, VideoCache

BASE_DIR = Path(__file__).parent
PARENT_DIR = BASE_DIR.parent
VIDEO_DIR = PARENT_DIR / "videos"
VIDEO_DIR.mkdir(exist_ok=True)

# ID канала-хранилища (отрицательное число для каналов, например -1001234567890)
STORAGE_CHAT_ID: str | None = os.getenv("STORAGE_CHAT_ID")

# Regex для извлечения ID видео из полного URL TikTok
_TIKTOK_ID_RE = re.compile(r'/video/(\d+)')


def _get_tiktok_id(url: str) -> str | None:
    """
    Извлекает ID видео TikTok.
    Быстрый путь (regex) для полных URL вида tiktok.com/@user/video/12345.
    Медленный путь (yt-dlp без загрузки) для коротких URL вида vm.tiktok.com/XXXX.
    """
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


def _ydl_download(url: str, outtmpl: str) -> str | None:
    """
    Скачивает видео TikTok и возвращает его ID.
    Запускать через run_in_executor — блокирующий вызов.
    """
    # noinspection SpellCheckingInspection
    opts: dict[str, object] = {
        "outtmpl": outtmpl,  # cspell:disable-line
        "quiet": True,
        "no_warnings": True,
        # Максимум 400p: берём лучший поток ≤400p + аудио;
        # если TikTok отдаёт единый поток — берём лучший combined ≤400p;
        # последний fallback «best» нужен на случай если видео изначально
        # ниже 400p и yt-dlp не может применить фильтр по высоте.
        "format": (
            "bestvideo[height<=400]+bestaudio"
            "/best[height<=400]"
            "/best"
        ),
    }
    with yt_dlp.YoutubeDL(opts) as ydl:  # type: ignore[arg-type]
        info = ydl.extract_info(url, download=True)
        if info and info.get("id"):
            return str(info["id"])
    return None


async def _deliver_video(
    bot: Bot,
    chat_id: str,
    inline_message_id: str | None,
    file_id: str | None,
    video_path: Path | None,
) -> str | None:
    """
    Доставляет видео пользователю одним из двух способов:

    • Если inline_message_id задан — редактируем то самое сообщение в чате,
      заменяя текст «⏳ Загружается…» на готовое видео.
    • Иначе — обычный send_video в chat_id.

    Возвращает Telegram file_id (для кеша) или None при ошибке.
    """
    # ── Inline-режим: редактируем сообщение в чате через file_id из Storage ──
    if inline_message_id:
        if not file_id:
            logging.warning("inline_message_id есть, но file_id отсутствует — пропускаем")
            return None
        try:
            await bot.edit_message_media(
                inline_message_id=inline_message_id,
                media=InputMediaVideo(media=file_id),
            )
        except (TelegramBadRequest, TelegramForbiddenError) as e:
            logging.warning("edit_message_media failed: %s", e)
        return file_id

    # ── Обычный режим: send_video в чат ──────────────────────────────────────
    if file_id:
        await bot.send_video(chat_id=chat_id, video=file_id)
        return file_id

    if video_path:
        msg = await bot.send_video(chat_id=chat_id, video=FSInputFile(str(video_path)))
        if msg.video:
            return msg.video.file_id

    return None


async def process_queue(bot: Bot) -> None:
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

                # ── 1. Получаем ID видео TikTok ───────────────────────────────────
                tiktok_id = await loop.run_in_executor(None, _get_tiktok_id, tiktok_link)

                # ── 2. Проверяем кеш ──────────────────────────────────────────────
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
                        )
                        await session.delete(item)
                        await session.commit()
                        continue

                # ── 3. Кеша нет — скачиваем ──────────────────────────────────────
                outtmpl = str(VIDEO_DIR / f"{filename}.%(ext)s")  # cspell:disable-line
                downloaded_id = await loop.run_in_executor(None, _ydl_download, tiktok_link, outtmpl)

                # Используем ID из скачивания (актуально для коротких ссылок)
                final_id: str | None = tiktok_id or downloaded_id

                # Ищем скачанный файл (расширение может отличаться от .mp4)
                matches = list(VIDEO_DIR.glob(f"{filename}.*"))
                if not matches:
                    raise FileNotFoundError(f"Файл после загрузки не найден: {filename}")
                video_path = matches[0]

                # ── 4. Отправляем в Storage-канал (если настроен) ────────────────
                file_id: str | None = None
                storage_message_id: int | None = None

                if STORAGE_CHAT_ID:
                    try:
                        storage_msg = await bot.send_video(
                            chat_id=STORAGE_CHAT_ID,
                            video=FSInputFile(str(video_path)),
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

                # ── 5. Доставляем видео (inline или обычный режим) ────────────────
                result_file_id = await _deliver_video(
                    bot, chat_id, inline_message_id,
                    file_id=file_id, video_path=video_path,
                )
                if result_file_id and not file_id:
                    file_id = result_file_id  # сохраним для кеша

                # ── 6. Удаляем локальный файл и кешируем ─────────────────────────
                os.remove(video_path)

                if final_id and file_id:
                    session.add(VideoCache(
                        tiktok_id=final_id,
                        file_id=file_id,
                        storage_message_id=storage_message_id,
                    ))

                await session.delete(item)
                await session.commit()

            except (TelegramBadRequest, TelegramForbiddenError) as e:
                # Бот удалён из чата или сообщение недоступно
                logging.warning("Telegram delivery error for chat %s: %s", chat_id, e)
                for f in VIDEO_DIR.glob(f"{filename}.*"):
                    os.remove(f)
                await session.delete(item)
                await session.commit()

            except Exception:
                logging.exception("Ошибка загрузки: chat=%s url=%s", chat_id, tiktok_link)
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
