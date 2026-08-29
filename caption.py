"""Подпись под видео и ссылка на источник — общие для очереди и inline-режима.

Вынесено сюда, чтобы и services/queue.py (доставка через очередь), и
routers/inline_router.py (мгновенная отдача из кеша) строили подпись
одинаково и не теряли ссылку на оригинал.
"""

from urllib.parse import urlsplit, urlunsplit

from aiogram import html

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


def source_link(url: str, likes: int | None = None) -> str:
    """HTML-ссылка на источник с числом лайков, если оно известно."""
    likes_tail = f" | ❤️ {_format_count(likes)}" if likes is not None else ""
    return f'🔗 <a href="{html.quote(normalize_url(url))}">Source</a>{likes_tail}'


def _format_count(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def build_caption(
    description: str | None,
    source_url: str | None,
    *,
    with_source: bool,
    likes: int | None = None,
    has_spoiler: bool = False,
    music_deep_link: str | None = None,
) -> str:
    """Подпись под видео: описание ролика (если есть) + [ссылка].

    with_source управляет строкой с источником внизу подписи. Разметка — HTML;
    при has_spoiler под спойлером тегом tg-spoiler скрывается только описание видео.
    """
    source_tail = source_link(source_url, likes) if with_source and source_url else ""
    music_tail = (
        '🎵 <a href="{}"><i>Распознать музыку из видео</i></a>'.format(
            html.quote(music_deep_link),
        )
        if music_deep_link
        else ""
    )

    desc = (description or "").strip()
    description_block = ""
    if desc:
        # Бюджет под описание: лимит минус хвост и запас. Режем по сырому
        # тексту (до html.quote), чтобы не разорвать html-сущность вроде &amp;.
        budget = CAPTION_LIMIT - len(source_tail) - len(music_tail) - 36
        if budget > 0:
            if len(desc) > budget:
                desc = desc[:budget - 1].rstrip() + "…"
            description_block = f"<blockquote expandable>{html.quote(desc)}</blockquote>"
            if has_spoiler:
                description_block = f"<tg-spoiler>{description_block}</tg-spoiler>"

    parts = [part for part in (description_block, source_tail, music_tail) if part]
    if not parts:
        return ""
    return "\n\n".join(parts)
