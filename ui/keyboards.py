"""Фабрики инлайн-клавиатур бота.

Вся разметка кнопок собрана здесь, чтобы хендлеры не строили
InlineKeyboardMarkup вручную, а текст/раскладку можно было менять в одном месте.
"""
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from ui.caption import normalize_url


def download_button(token: str) -> InlineKeyboardMarkup:
    """Кнопка «Загрузить видео» для групповых чатов (callback_data=dl:<token>)."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⬇️ Загрузить видео", callback_data=f"dl:{token}")
    ]])


def loading_placeholder() -> InlineKeyboardMarkup:
    """Кнопка-заглушка «Загружается…» для inline-плейсхолдера.

    Нужна, чтобы Telegram вернул inline_message_id в ChosenInlineResult.
    """
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⏳ Загружается…", callback_data="inline_loading")
    ]])


def source_button(url: str) -> InlineKeyboardMarkup:
    """Кнопка «Ссылка на источник» — для inline-режима (текст-ссылку не вставляем)."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔗 Ссылка на источник", url=normalize_url(url))
    ]])
