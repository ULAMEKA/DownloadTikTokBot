# -----------------------------------------------------------------------
# Inline-режим отложен на будущее — весь код закомментирован
# -----------------------------------------------------------------------

# import asyncio
# import logging
# import os
# import re
# import uuid
#
# import yt_dlp
# from aiogram import Router, Bot
# from aiogram.types import FSInputFile, InlineQuery, InlineQueryResultCachedVideo
# from sqlalchemy import select
# from sqlalchemy.exc import IntegrityError
#
# from db.models import VideoCache
# from db.session import SessionLocal
#
# inline_router = Router()
#
# TIKTOK_RE = re.compile(r'(https?://)?(www\.|vm\.|vt\.)?tiktok\.com/\S+')
# STORAGE_CHAT_ID = os.getenv('STORAGE_CHAT_ID')
#
# BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# VIDEO_DIR = os.path.join(BASE_DIR, "videos")
# os.makedirs(VIDEO_DIR, exist_ok=True)
#
# _downloading: set[str] = set()
#
#
# def _ydl_download(url: str, out_template: str):
#     with yt_dlp.YoutubeDL({'outtmpl': out_template, 'quiet': True}) as ydl:
#         ydl.download([url])
#
#
# async def _preload(url: str, bot: Bot):
#     _downloading.add(url)
#     filename = str(uuid.uuid4())
#     final_path = os.path.join(VIDEO_DIR, f'{filename}.mp4')
#
#     try:
#         loop = asyncio.get_event_loop()
#         await loop.run_in_executor(
#             None, _ydl_download, url, os.path.join(VIDEO_DIR, f'{filename}.%(ext)s')
#         )
#
#         video = FSInputFile(final_path)
#         msg = await bot.send_video(chat_id=STORAGE_CHAT_ID, video=video)
#         file_id = msg.video.file_id
#         await bot.delete_message(chat_id=STORAGE_CHAT_ID, message_id=msg.message_id)
#
#         async with SessionLocal() as session:
#             session.add(VideoCache(url=url, file_id=file_id))
#             await session.commit()
#     except IntegrityError:
#         pass
#     except Exception as e:
#         logging.exception("Preload error for %s: %s", url, e)
#     finally:
#         _downloading.discard(url)
#         if os.path.exists(final_path):
#             os.remove(final_path)
#
#
# @inline_router.inline_query()
# async def handle_inline_query(query: InlineQuery, bot: Bot):
#     text = query.query.strip()
#
#     if not TIKTOK_RE.match(text):
#         await query.answer([], cache_time=1)
#         return
#
#     async with SessionLocal() as session:
#         result = await session.execute(select(VideoCache).where(VideoCache.url == text))
#         cached = result.scalar()
#
#     if cached:
#         await query.answer([
#             InlineQueryResultCachedVideo(
#                 id=str(uuid.uuid4()),
#                 video_file_id=cached.file_id,
#                 title='TikTok Video',
#             )
#         ], cache_time=300)
#         return
#
#     if text not in _downloading:
#         asyncio.create_task(_preload(text, bot))
#
#     await query.answer([], cache_time=1)
