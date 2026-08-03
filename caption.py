"""Подпись под видео и кнопка-источник — общие для очереди и inline-режима.

Вынесено сюда, чтобы и services/queue.py (доставка через очередь), и
routers/inline_router.py (мгновенная отдача из кеша) строили подпись и
кнопку одинаково и не теряли ссылку на оригинал.
"""

from urllib.parse import urlsplit, urlunsplit

from aiogram import html
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from promo import BOT_PROMO_HTML

# Лимит длины подписи в Telegram. Держим запас под ссылку и
# особенности подсчёта (эмодзи в UTF-16, html-теги в видимую длину не входят).
CAPTION_LIMIT = 1024


def normalize_url(url: str) -> str:
    """Гарантирует схему — иначе URL-кнопка/ссылка невалидны для Telegram."""
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def source_link(url: str) -> str:
    """HTML-ссылка «Ссылка на источник» на оригинальное видео в TikTok."""
    return f'🔗 <a href="{html.quote(normalize_url(url))}">Source</a>'


def _format_count(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def stats_keyboard(
    stats: tuple[int | None, int | None, int | None] = (None, None, None),
) -> InlineKeyboardMarkup | None:
    """Клавиатура под видео со статистикой."""
    likes, comments, views = stats
    stats_buttons = [
        InlineKeyboardButton(text=f"❤️ {_format_count(likes)}", callback_data="video_stats")
        if likes is not None else None,
        InlineKeyboardButton(text=f"💬 {_format_count(comments)}", callback_data="video_stats")
        if comments is not None else None,
        InlineKeyboardButton(text=f"👁 {_format_count(views)}", callback_data="video_stats")
        if views is not None else None,
    ]
    stats_row = [button for button in stats_buttons if button]
    rows: list[list[InlineKeyboardButton]] = []
    if stats_row:
        rows.append(stats_row)
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def build_caption(
    description: str | None,
    source_url: str | None,
    *,
    with_source: bool,
    has_spoiler: bool = False,
) -> str:
    """Подпись под видео: описание ролика (если есть) + [ссылка].

    with_source управляет строкой с источником внизу подписи. Разметка — HTML;
    при has_spoiler под спойлером тегом tg-spoiler скрывается только описание видео.
    """
    source_tail = source_link(source_url) if with_source and source_url else ""
    promo_tail = BOT_PROMO_HTML

    desc = (description or "").strip()
    description_block = ""
    if desc:
        # Бюджет под описание: лимит минус хвост и запас. Режем по сырому
        # тексту (до html.quote), чтобы не разорвать html-сущность вроде &amp;.
        budget = CAPTION_LIMIT - len(source_tail) - len(promo_tail) - 30
        if budget > 0:
            if len(desc) > budget:
                desc = desc[:budget - 1].rstrip() + "…"
            description_block = f"<blockquote expandable>{html.quote(desc)}</blockquote>"
            if has_spoiler:
                description_block = f"<tg-spoiler>{description_block}</tg-spoiler>"

    parts = [part for part in (description_block, source_tail, promo_tail) if part]
    if not parts:
        return ""
    return "\n\n".join(parts)
