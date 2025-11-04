import logging
import re

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import FSInputFile
from aiogram.types import Message
import uuid
import yt_dlp
import os

from states import DownloadVideo

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PARENT_DIR = os.path.dirname(BASE_DIR)
VIDEO_DIR = os.path.join(PARENT_DIR, "videos")
os.makedirs(VIDEO_DIR, exist_ok=True)

link = Router()


@link.message(
    # DownloadVideo.waiting_for_link,
    F.text.regexp(r'(https?:\/\/)?(www\.|vm\.|vt\.)?tiktok\.com\/\S+'))
async def handle_tiktok_link(message: Message, state: FSMContext):
    await message.reply("Видео получено! Загружаю...")
    try:
        title = uuid.uuid4()
        ydl_opts = {
            'outtmpl': os.path.join(VIDEO_DIR, f'{title}.%(ext)s'),
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([message.text])
        video = FSInputFile(f"{VIDEO_DIR}/{title}.mp4")
        await message.reply_video(video)
        os.remove(f"{VIDEO_DIR}/{title}.mp4")
        await message.answer("Пришли ссылку на TikTok")
        await state.set_state(DownloadVideo.waiting_for_link)
    except Exception:
        await message.answer("Что-то пошло не так. Попробуй ещё раз.")
        raise Exception

@link.message(
    # DownloadVideo.waiting_for_link,
    F.text.func(lambda text: not re.search(r'(https?:\/\/)?(www\.|vm\.|vt\.)?tiktok\.com\/\S+', text))
)
async def handle_non_tiktok_link(message: Message, state: FSMContext):
    await message.answer("Это не TikTok-ссылка. Пришли правильную ссылку.")
    await state.set_state(DownloadVideo.waiting_for_link)
