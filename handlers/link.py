import logging
import re

from aiogram import Router, F
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.types import FSInputFile
from aiogram.types import Message
import uuid
import yt_dlp
import os

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from db.models import DownloadQueue
from states import DownloadVideo




link = Router()


@link.message(F.text.regexp(r'(https?:\/\/)?(www\.|vm\.|vt\.)?tiktok\.com\/\S+'))
async def handle_tiktok_link(message: Message, state: FSMContext, session: AsyncSession):
    # try:
    title = str(uuid.uuid4())
    queue_item = DownloadQueue(user_id=str(message.from_user.id), filename=title, tiktok_link=message.text)
    session.add(queue_item)
    await session.commit()
    stmt = select(func.count()).select_from(DownloadQueue).where(DownloadQueue.id <= queue_item.id)
    result = await session.execute(stmt)
    position = result.scalar()
    await message.answer(f"Вы добавлены в очередь\! Ваша позиция в очереди: *{position}*", parse_mode=ParseMode.MARKDOWN_V2)

    #     ydl_opts = {
    #         'outtmpl': os.path.join(VIDEO_DIR, f'{title}.%(ext)s'),
    #     }
    #     with yt_dlp.YoutubeDL(ydl_opts) as ydl:
    #         ydl.download([message.text])
    #     video = FSInputFile(f"{VIDEO_DIR}/{title}.mp4")
    #     await message.reply_video(video)
    #     os.remove(f"{VIDEO_DIR}/{title}.mp4")
    #     await message.answer("Пришли ссылку на TikTok")
    #     await state.set_state(DownloadVideo.waiting_for_link)
    # except Exception:
    #     await message.answer("Что-то пошло не так. Попробуй ещё раз.")
    #     raise Exception


@link.message(F.text.func(lambda text: not re.search(r'(https?:\/\/)?(www\.|vm\.|vt\.)?tiktok\.com/\S+', text)))
async def handle_non_tiktok_link(message: Message, state: FSMContext):
    await message.answer("Это не TikTok-ссылка. Пришли правильную ссылку.")