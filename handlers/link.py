import asyncio
import uuid
from dataclasses import dataclass

from aiogram import F, Router
from aiogram.enums import ChatType, ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import DownloadQueue
from promo import AD_PLACEHOLDER_IMAGE, loading_placeholder_caption
from services.queue import (
    QUEUE_LIMIT_MESSAGE,
    VideoQuality,
    enqueue_download,
    hold_placeholder,
    list_video_qualities,
)
from utils.links import SUPPORTED_URL_RE, find_video_url

link = Router()


@dataclass
class PendingDownload:
    user_id: int
    url: str
    mode: str | None = None
    qualities: list[VideoQuality] | None = None


_pending: dict[str, PendingDownload] = {}


async def _expire_pending(token: str) -> None:
    await asyncio.sleep(600)
    _pending.pop(token, None)


def _quality_markup(token: str, qualities: list[VideoQuality]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=quality.label,
            callback_data=f"quality:{token}:{index}",
        )]
        for index, quality in enumerate(qualities)
    ])


@link.message(F.text.regexp(SUPPORTED_URL_RE.pattern), F.chat.type == ChatType.PRIVATE)
async def handle_video_link_private(message: Message):
    url = find_video_url(message.text or "")
    if url is None:
        return

    token = uuid.uuid4().hex
    _pending[token] = PendingDownload(message.chat.id, url)
    await message.answer(
        "Как загрузить видео?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="⬇️ Загрузить",
                callback_data=f"download:normal:{token}",
            )],
            [
                InlineKeyboardButton(
                    text="📝 Без описания",
                    callback_data=f"download:no_description:{token}",
                ),
                InlineKeyboardButton(
                    text="🙈 Под спойлером",
                    callback_data=f"download:spoiler:{token}",
                ),
            ],
            [InlineKeyboardButton(
                text="🙈 Без описания, под спойлером",
                callback_data=f"download:no_description_spoiler:{token}",
            )],
        ]),
    )
    asyncio.create_task(_expire_pending(token))


@link.callback_query(F.data.startswith("download:"))
async def handle_download_option(callback: CallbackQuery, session: AsyncSession):
    if callback.data is None:
        await callback.answer()
        return

    parts = callback.data.split(":", 2)
    if len(parts) != 3:
        await callback.answer()
        return
    _, mode, token = parts
    pending = _pending.get(token)
    if mode not in ("normal", "no_description", "spoiler", "no_description_spoiler") or pending is None:
        await callback.answer("Ссылка устарела. Отправьте её ещё раз.", show_alert=True)
        return
    if callback.from_user.id != pending.user_id:
        await callback.answer("Эта кнопка предназначена другому пользователю.", show_alert=True)
        return

    await callback.answer()
    try:
        loop = asyncio.get_running_loop()
        qualities = await loop.run_in_executor(None, list_video_qualities, pending.url)
    except Exception:
        await callback.message.edit_text("❌ Не удалось получить варианты качества. Попробуйте ещё раз.")
        return
    pending.mode = mode
    pending.qualities = qualities or [VideoQuality(
        format_id=None,
        label="⚙️ Загрузить автоматически",
        filesize=None,
    )]
    text = (
        "Выберите качество видео. Файлы больше 50 МБ бот разделит на части, "
        "если формат это позволяет."
        if qualities else
        "Варианты качества для этого видео недоступны. Можно загрузить его автоматически."
    )
    await callback.message.edit_text(
        text,
        reply_markup=_quality_markup(token, pending.qualities),
    )


@link.callback_query(F.data.startswith("quality:"))
async def handle_quality_option(callback: CallbackQuery, session: AsyncSession):
    if callback.data is None:
        await callback.answer()
        return

    parts = callback.data.split(":", 2)
    if len(parts) != 3:
        await callback.answer()
        return
    _, token, index_text = parts
    pending = _pending.get(token)
    if pending is None or pending.mode is None or pending.qualities is None:
        await callback.answer("Ссылка устарела. Отправьте её ещё раз.", show_alert=True)
        return
    if callback.from_user.id != pending.user_id:
        await callback.answer("Эта кнопка предназначена другому пользователю.", show_alert=True)
        return
    try:
        quality = pending.qualities[int(index_text)]
    except (IndexError, ValueError):
        await callback.answer("Этот вариант больше недоступен. Отправьте ссылку ещё раз.", show_alert=True)
        return

    _pending.pop(token, None)
    placeholder = None
    if callback.message:
        placeholder = await callback.message.answer_photo(
            photo=FSInputFile(str(AD_PLACEHOLDER_IMAGE)),
            caption=loading_placeholder_caption(),
            parse_mode=ParseMode.HTML,
        )
        await callback.message.delete()
    queue_item = DownloadQueue(
        chat_id=str(callback.from_user.id),
        filename=str(uuid.uuid4()),
        tiktok_link=pending.url,
        format_id=quality.format_id,
        reply_to_message_id=-placeholder.message_id if placeholder else None,
        inline_message_id=f"direct:{pending.mode}" if pending.mode != "normal" else None,
    )
    if not await enqueue_download(session, queue_item, str(callback.from_user.id)):
        if placeholder:
            await placeholder.delete()
        await callback.answer(QUEUE_LIMIT_MESSAGE, show_alert=True)
        return
    if placeholder:
        hold_placeholder(str(callback.from_user.id), placeholder.message_id)

    stmt = select(func.count()).select_from(DownloadQueue).where(DownloadQueue.id <= queue_item.id)
    position = (await session.execute(stmt)).scalar()
    if placeholder:
        await placeholder.edit_caption(
            caption=loading_placeholder_caption(position),
            parse_mode=ParseMode.HTML,
        )
    await callback.answer()


@link.callback_query(F.data == "video_stats")
async def handle_video_stats(callback: CallbackQuery):
    await callback.answer("Статистика показана на момент загрузки")


@link.message(F.text.regexp(SUPPORTED_URL_RE.pattern))
async def handle_video_link_group(message: Message, session: AsyncSession):
    url = find_video_url(message.text or "")
    if url is None:
        return

    placeholder = await message.reply_photo(
        photo=FSInputFile(str(AD_PLACEHOLDER_IMAGE)),
        caption=loading_placeholder_caption(),
        parse_mode=ParseMode.HTML,
    )
    queue_item = DownloadQueue(
        chat_id=str(message.chat.id),
        filename=str(uuid.uuid4()),
        tiktok_link=url,
        reply_to_message_id=-placeholder.message_id,
    )
    user_id = str(message.from_user.id if message.from_user else message.chat.id)
    if not await enqueue_download(session, queue_item, user_id):
        await placeholder.delete()
        await message.reply(QUEUE_LIMIT_MESSAGE)
        return
    stmt = select(func.count()).select_from(DownloadQueue).where(DownloadQueue.id <= queue_item.id)
    position = (await session.execute(stmt)).scalar_one()
    await placeholder.edit_caption(
        caption=loading_placeholder_caption(position),
        parse_mode=ParseMode.HTML,
    )
    hold_placeholder(str(message.chat.id), placeholder.message_id)


@link.message(F.chat.type == ChatType.PRIVATE)
async def handle_unsupported_link(message: Message, state: FSMContext):
    await message.answer("Пришли ссылку на TikTok или Reels.")
