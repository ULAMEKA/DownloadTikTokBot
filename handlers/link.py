import uuid

from aiogram import Router, F
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import DownloadQueue

link = Router()

TIKTOK_RE = r'(https?:\/\/)?(www\.|vm\.|vt\.)?tiktok\.com\/\S+'

# Временное хранилище token -> url для кнопок в групповых чатах.
# Сбрасывается при перезапуске бота — при нажатии на старую кнопку
# пользователь увидит сообщение "ссылка устарела".
_pending: dict[str, str] = {}


# ── Приватный чат: сразу в очередь ──────────────────────────────────────────

@link.message(F.text.regexp(TIKTOK_RE), F.chat.type == ChatType.PRIVATE)
async def handle_tiktok_link_private(message: Message, session: AsyncSession):
    if message.text is None:
        return

    filename = str(uuid.uuid4())
    queue_item = DownloadQueue(
        chat_id=str(message.chat.id),
        filename=filename,
        tiktok_link=message.text,
    )
    session.add(queue_item)
    await session.commit()

    stmt = select(func.count()).select_from(DownloadQueue).where(DownloadQueue.id <= queue_item.id)
    result = await session.execute(stmt)
    position = result.scalar()

    await message.answer(
        f"Вы добавлены в очередь\! Ваша позиция: *{position}*",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


# ── Групповой / супергрупповой чат: сначала кнопка ──────────────────────────

@link.message(F.text.regexp(TIKTOK_RE))
async def handle_tiktok_link_group(message: Message):
    if message.text is None:
        return

    token = str(uuid.uuid4())          # 36 символов — вместе с "dl:" = 39 < 64 байт
    _pending[token] = message.text

    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⬇️ Загрузить видео", callback_data=f"dl:{token}")
    ]])

    try:
        await message.reply(
            "Нажмите кнопку, чтобы загрузить видео.",
            reply_markup=keyboard,
        )
    except TelegramBadRequest:
        # Исходное сообщение уже удалено, пока бот формировал ответ
        _pending.pop(token, None)


# ── Callback: нажатие кнопки "Загрузить видео" ───────────────────────────────

@link.callback_query(F.data.startswith("dl:"))
async def handle_download_callback(callback: CallbackQuery, session: AsyncSession):
    if callback.data is None or callback.message is None:
        await callback.answer()
        return

    token = callback.data.removeprefix("dl:")
    url = _pending.pop(token, None)

    if url is None:
        # Токен уже использован или бот перезапускался
        await callback.answer(
            "Ссылка устарела или уже добавлена в очередь.",
            show_alert=True,
        )
        return

    filename = str(uuid.uuid4())
    chat_id = str(callback.message.chat.id)
    queue_item = DownloadQueue(chat_id=chat_id, filename=filename, tiktok_link=url)
    session.add(queue_item)
    await session.commit()

    stmt = select(func.count()).select_from(DownloadQueue).where(DownloadQueue.id <= queue_item.id)
    result = await session.execute(stmt)
    position = result.scalar()

    try:
        await callback.message.edit_text(
            f"✅ Добавлено в очередь\! Позиция: *{position}*",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    except TelegramBadRequest:
        # Сообщение с кнопкой было удалено — просто продолжаем
        pass

    await callback.answer()


# ── Приватный чат: не TikTok-ссылка ─────────────────────────────────────────

@link.message(F.chat.type == ChatType.PRIVATE)
async def handle_non_tiktok_link(message: Message, state: FSMContext):
    await message.answer("Это не TikTok-ссылка. Пришли правильную ссылку.")
