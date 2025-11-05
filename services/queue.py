import asyncio
import os

import yt_dlp
from aiogram import Bot
from aiogram.types import FSInputFile
from sqlalchemy import select
from db.session import SessionLocal
from db.models import DownloadQueue

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PARENT_DIR = os.path.dirname(BASE_DIR)
VIDEO_DIR = os.path.join(PARENT_DIR, "videos")
os.makedirs(VIDEO_DIR, exist_ok=True)


async def process_queue(bot: Bot):
    while True:
        async with SessionLocal() as session:
            stmt = select(DownloadQueue).order_by(DownloadQueue.id).limit(1)
            result = await session.execute(stmt)
            item = result.scalar()
            if item:
                try:
                    ydl_opts = {
                        'outtmpl': os.path.join(VIDEO_DIR, f'{item.filename}.%(ext)s'),
                    }
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        ydl.download([item.tiktok_link])
                    video = FSInputFile(f"{VIDEO_DIR}/{item.filename}.mp4")
                    await bot.send_video(chat_id=item.user_id, video=video)
                    os.remove(f"{VIDEO_DIR}/{item.filename}.mp4")
                    await bot.send_message(chat_id=item.user_id, text="Пришли ссылку на TikTok!")
                    await session.delete(item)
                    await session.commit()
                except Exception:
                    await bot.send_message(chat_id=item.user_id, text="Что-то пошло не так! Попробуй еще раз!")
                    await asyncio.sleep(3)
                    await session.delete(item)
                    await session.commit()
                    raise Exception
            else:
                await asyncio.sleep(1)
