"""Промо-сообщение бота.

Вынесено в отдельный модуль, чтобы в будущем сюда можно было
подставлять рекламу — текст меняется в одном месте, хендлеры не трогаем.
Существует в двух форматах, потому что хендлеры используют разную
разметку: MarkdownV2 (handlers/link.py) и HTML (routers/inline_router.py).
"""

# Юзернейм бота для inline-режима.
BOT_USERNAME = "@dwlttbot"

# MarkdownV2-версия — handlers/link.py.
PROMO_MESSAGE = (
    f"💡 В любом чате введите: `{BOT_USERNAME} *ссылка*` — "
    "и видео отправится прямо в чат"
)

# HTML-версия — routers/inline_router.py.
PROMO_MESSAGE_HTML = (
    f"💡 В любом чате введите: <code>{BOT_USERNAME} *ссылка*</code> — "
    "и видео отправится прямо в чат"
)
