"""Распознавание ссылок на видео — TikTok, YouTube Shorts, Instagram Reels.

Общий модуль для хендлеров, inline-режима и очереди: одна точка, где описаны
поддерживаемые платформы, детектор ссылки и извлечение ключа для кеша.
"""
import re

# Единый детектор поддерживаемой ссылки — для фильтра aiogram (F.text.regexp).
SUPPORTED_URL_RE = re.compile(
    r'(https?://)?('
    r'(www\.|vm\.|vt\.)?tiktok\.com/\S+'
    r'|(www\.|m\.)?youtube\.com/shorts/\S+'
    r'|youtu\.be/\S+'
    r'|(www\.)?instagram\.com/reels?/\S+'
    r')'
)

# (платформа, regex ссылки, regex ID). Имя платформы в нижнем регистре совпадает
# с info["extractor"].lower() из yt-dlp — чтобы ключ кеша был одинаков и при
# быстрой проверке по URL, и после реальной загрузки.
_PLATFORMS: list[tuple[str, re.Pattern[str], re.Pattern[str]]] = [
    ("tiktok",    re.compile(r'(www\.|vm\.|vt\.)?tiktok\.com/\S+'), re.compile(r'/video/(\d+)')),
    ("youtube",   re.compile(r'youtube\.com/shorts/\S+'),           re.compile(r'/shorts/([\w-]+)')),
    ("youtube",   re.compile(r'youtu\.be/\S+'),                     re.compile(r'youtu\.be/([\w-]+)')),
    ("instagram", re.compile(r'instagram\.com/reels?/\S+'),         re.compile(r'/reels?/([\w-]+)')),
]


def find_video_url(text: str) -> str | None:
    """Возвращает первую поддерживаемую ссылку из текста (или None)."""
    m = SUPPORTED_URL_RE.search(text)
    return m.group(0) if m else None


def media_key(url: str) -> str | None:
    """Ключ кеша вида "<платформа>:<id>" по URL (или None, если ID не вытащить).

    None бывает для коротких ссылок (vm./vt.tiktok) — их резолвит yt-dlp в очереди.
    """
    for platform, url_re, id_re in _PLATFORMS:
        if url_re.search(url):
            m = id_re.search(url)
            return f"{platform}:{m.group(1)}" if m else None
    return None
