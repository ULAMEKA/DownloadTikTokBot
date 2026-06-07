"""Подпись под видео и кнопка-источник — общие для очереди и inline-режима.

Вынесено сюда, чтобы и services/queue.py (доставка через очередь), и
routers/inline_router.py (мгновенная отдача из кеша) строили подпись и
кнопку одинаково и не теряли ссылку на оригинал.
"""

from aiogram import html

from ui.promo import PROMO_MESSAGE_HTML

# Лимит длины подписи в Telegram. Держим запас под промо/ссылку и
# особенности подсчёта (эмодзи в UTF-16, html-теги в видимую длину не входят).
CAPTION_LIMIT = 1024


def normalize_url(url: str) -> str:
    """Гарантирует схему — иначе URL-кнопка/ссылка невалидны для Telegram."""
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def source_link(url: str) -> str:
    """HTML-ссылка «Ссылка на источник» на оригинальное видео."""
    return f'🔗 <a href="{html.quote(normalize_url(url))}">Ссылка на источник</a>'


def build_caption(
    description: str | None,
    source_url: str | None,
    *,
    with_source: bool,
) -> str:
    """Подпись под видео: описание ролика (если есть) + [ссылка] + промо.

    with_source=False для inline-режима — там источник вынесен в кнопку,
    поэтому ссылку в текст не добавляем. Разметка — HTML.
    """
    tail_parts: list[str] = []
    if with_source and source_url:
        tail_parts.append(source_link(source_url))
    tail_parts.append(PROMO_MESSAGE_HTML)
    tail = "\n\n".join(tail_parts)

    desc = (description or "").strip()
    if not desc:
        return tail

    # Бюджет под описание: лимит минус хвост и запас. Режем по сырому
    # тексту (до html.quote), чтобы не разорвать html-сущность вроде &amp;.
    budget = CAPTION_LIMIT - len(tail) - 30
    if budget <= 0:
        return tail
    if len(desc) > budget:
        desc = desc[:budget - 1].rstrip() + "…"
    return f"{html.quote(desc)}\n\n{tail}"
