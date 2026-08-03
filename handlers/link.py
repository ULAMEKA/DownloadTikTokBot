import asyncio
import uuid

from aiogram import F, Router
from aiogram.enums import ChatType, ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import DownloadQueue
from promo import AD_PLACEHOLDER_IMAGE, loading_placeholder_caption
from services.queue import QUEUE_LIMIT_MESSAGE, enqueue_download, hold_placeholder
from utils.links import SUPPORTED_URL_RE, find_video_url

link = Router()
_pending: dict[str, tuple[int, str]] = {}


async def _expire_pending(token: str) -> None:
    await asyncio.sleep(600)
    _pending.pop(token, None)


@link.message(F.text.regexp(SUPPORTED_URL_RE.pattern), F.chat.type == ChatType.PRIVATE)
async def handle_video_link_private(message: Message):
    url = find_video_url(message.text or "")
    if url is None:
        return

    token = uuid.uuid4().hex
    _pending[token] = (message.chat.id, url)
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
    if callback.from_user.id != pending[0]:
        await callback.answer("Эта кнопка предназначена другому пользователю.", show_alert=True)
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
        tiktok_link=pending[1],
        reply_to_message_id=-placeholder.message_id if placeholder else None,
        inline_message_id=f"direct:{mode}" if mode != "normal" else None,
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
