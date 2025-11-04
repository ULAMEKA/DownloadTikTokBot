import uuid

import yt_dlp
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

VIDEO_DIR = os.path.join(BASE_DIR, "videos")

os.makedirs(VIDEO_DIR, exist_ok=True)

title = uuid.uuid4()

ydl_opts = {
    'outtmpl': os.path.join(VIDEO_DIR, f'{title}.%(ext)s'),
}

# Скачиваем видео
with yt_dlp.YoutubeDL(ydl_opts) as ydl:
    test = ydl.download(['https://www.tiktok.com/@rikait5/video/7568339809077447948?is_from_webapp=1&sender_device=pc'])