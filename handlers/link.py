import logging
import uuid

from aiogram import Router, F
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message, CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from services.queue import enqueue_download, queue_position
from ui.keyboards import download_button
from ui.promo import PROMO_MESSAGE
from utils.links import SUPPORTED_URL_RE, find_video_url

link = Router()

# Временное хранилище token -> url для кнопок в групповых чатах.
# Сбрасывается при перезапуске бота — при нажатии на старую кнопку
# пользователь увидит сообщение "ссылка устарела".
_PENDING_MAX = 1000
_pending: dict[str, str] = {}


def _remember_pending(token: str, url: str) -> None:
    """Запоминает url под токеном, не давая словарю расти без предела."""
    if len(_pending) >= _PENDING_MAX:
        # Вытесняем самый старый токен (dict хранит порядок вставки).
        _pending.pop(next(iter(_pending)))
    _pending[token] = url


# ── Приватный чат: сразу в очередь ──────────────────────────────────────────

@link.message(F.text.regexp(SUPPORTED_URL_RE.pattern), F.chat.type == ChatType.PRIVATE)
async def handle_video_link_private(message: Message, session: AsyncSession):
    url = find_video_url(message.text or "")
    if url is None:
        return

    item = await enqueue_download(session, chat_id=str(message.chat.id), source_url=url)
    position = await queue_position(session, item)

    await message.answer(
        f"Вы добавлены в очередь\\! Ваша позиция: *{position}*\n\n"
        f"{PROMO_MESSAGE}",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


# ── Групповой / супергрупповой чат: сначала кнопка ──────────────────────────

@link.message(F.text.regexp(SUPPORTED_URL_RE.pattern))
async def handle_video_link_group(message: Message):
    url = find_video_url(message.text or "")
    if url is None:
        return

    token = str(uuid.uuid4())  # 36 символов — вместе с "dl:" = 39 < 64 байт
    _remember_pending(token, url)

    try:
        await message.reply(
            "Нажмите кнопку, чтобы загрузить видео\\.\n\n"
            f"{PROMO_MESSAGE}",
            reply_markup=download_button(token),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    except TelegramBadRequest as e:
        # Обычно: исходное сообщение удалено, пока бот формировал ответ.
        # Также возможен «can't parse entities» из-за MARKDOWN_V2 — логируем,
        # чтобы это не было невидимо.
        logging.warning("Group reply failed (token=%s): %s", token, e)
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

    item = await enqueue_download(session, chat_id=str(callback.message.chat.id), source_url=url)
    position = await queue_position(session, item)

    try:
        await callback.message.edit_text(
            f"✅ Добавлено в очередь\\! Позиция: *{position}*\n\n"
            f"{PROMO_MESSAGE}",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    except TelegramBadRequest:
        # Сообщение с кнопкой было удалено — просто продолжаем
        pass

    await callback.answer()


@link.message(F.chat.type == ChatType.PRIVATE)
async def handle_unsupported_message(message: Message):
    await message.answer("Пришли ссылку на TikTok, Reels или Shorts.")
