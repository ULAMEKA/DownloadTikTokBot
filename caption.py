"""Подпись под видео и кнопка-источник — общие для очереди и inline-режима.

Вынесено сюда, чтобы и services/queue.py (доставка через очередь), и
routers/inline_router.py (мгновенная отдача из кеша) строили подпись и
кнопку одинаково и не теряли ссылку на оригинал.
"""

from urllib.parse import urlsplit, urlunsplit

from aiogram import html
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

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
    return f'🔗 <a href="{html.quote(normalize_url(url))}">Ссылка на источник</a>'


def source_button(url: str) -> InlineKeyboardMarkup:
    """Кнопка «Ссылка на источник» — для inline-режима (текст-ссылку не вставляем)."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔗 Ссылка на источник", url=normalize_url(url))
    ]])


def build_caption(
    description: str | None,
    source_url: str | None,
    *,
    with_source: bool,
    has_spoiler: bool = False,
) -> str:
    """Подпись под видео: описание ролика (если есть) + [ссылка].

    with_source=False для inline-режима — там источник вынесен в кнопку,
    поэтому ссылку в текст не добавляем. Разметка — HTML; при has_spoiler
    подпись целиком скрывается тегом tg-spoiler.
    """
    tail_parts: list[str] = []
    if with_source and source_url:
        tail_parts.append(source_link(source_url))
    tail = "\n\n".join(tail_parts)

    desc = (description or "").strip()
    if desc:
        # Бюджет под описание: лимит минус хвост и запас. Режем по сырому
        # тексту (до html.quote), чтобы не разорвать html-сущность вроде &amp;.
        budget = CAPTION_LIMIT - len(tail) - 30
        if budget > 0:
            if len(desc) > budget:
                desc = desc[:budget - 1].rstrip() + "…"
            tail = f"<blockquote expandable>{html.quote(desc)}</blockquote>\n\n{tail}"

    if not tail:
        return ""
    return f"<tg-spoiler>{tail}</tg-spoiler>" if has_spoiler else tail
