import uuid

from aiogram import Bot, Router, F
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import (
    CallbackQuery,
    InlineQuery,
    InlineQueryResultArticle,
    InputMediaPhoto,
    InputTextMessageContent,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ChosenInlineResult,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import DownloadQueue
from caption import source_link
from promo import AD_PLACEHOLDER_PHOTO_FILE_ID, loading_placeholder_caption
from services.queue import QUEUE_LIMIT_MESSAGE, enqueue_download, hold_inline_placeholder
from utils.links import SUPPORTED_URL_RE

inline_router = Router()


def _inline_result(
    result_id: str,
    title: str,
    description: str,
    caption: str,
    markup: InlineKeyboardMarkup,
):
    return InlineQueryResultArticle(
        id=result_id,
        title=title,
        description=description,
        input_message_content=InputTextMessageContent(
            message_text=caption,
            parse_mode="HTML",
        ),
        reply_markup=markup,
    )


def _placeholder_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⏳ Загружается…", callback_data="inline_loading")
    ]])


@inline_router.inline_query()
async def handle_inline_query(query: InlineQuery):
    text = query.query.strip()

    if not SUPPORTED_URL_RE.search(text):
        await query.answer([], cache_time=5)
        return

    caption = loading_placeholder_caption()
    markup = _placeholder_markup()
    await query.answer(
        results=[
            _inline_result("download", "📤 Отправить", "Отправить видео с описанием", caption, markup),
            _inline_result("download_spoiler", "🙈 Отправить под спойлером", "Скрыть видео и описание до нажатия", caption, markup),
            _inline_result("download_no_description", "🧹 Без описания", "Отправить видео без описания", caption, markup),
            _inline_result("download_no_description_spoiler", "🙈 Без описания, под спойлером", "Скрыть видео до нажатия", caption, markup),
        ],
        cache_time=0,
    )


@inline_router.chosen_inline_result(
    (F.result_id == "download")
    | (F.result_id == "download_spoiler")
    | (F.result_id == "download_no_description")
    | (F.result_id == "download_no_description_spoiler")
)
async def chosen_download(result: ChosenInlineResult, session: AsyncSession, bot: Bot):
    """
    Срабатывает, когда пользователь выбрал «Загрузить».
    Сохраняем inline_message_id — очередь отредактирует это сообщение
    и вставит видео прямо в чат.
    chat_id = user_id нужен как fallback (например, для получения file_id
    без Storage-канала).
    """
    m_url = SUPPORTED_URL_RE.search(result.query)
    if not m_url:
        return

    url = m_url.group(0)
    raw_inline_message_id = result.inline_message_id
    inline_message_id = raw_inline_message_id
    if inline_message_id:
        prefix = {
            "download_spoiler": "spoiler:",
            "download_no_description": "no_description:",
            "download_no_description_spoiler": "spoiler:no_description:",
        }.get(result.result_id, "")
        inline_message_id = f"{prefix}{inline_message_id}"

    queue_item = DownloadQueue(
        chat_id=str(result.from_user.id),      # PM пользователя (fallback)
        filename=str(uuid.uuid4()),
        tiktok_link=url,
        inline_message_id=inline_message_id,
    )

    if raw_inline_message_id and AD_PLACEHOLDER_PHOTO_FILE_ID:
        try:
            await bot.edit_message_media(
                inline_message_id=raw_inline_message_id,
                media=InputMediaPhoto(
                    media=AD_PLACEHOLDER_PHOTO_FILE_ID,
                    caption=loading_placeholder_caption(),
                    parse_mode="HTML",
                    show_caption_above_media=True,
                ),
                reply_markup=_placeholder_markup(),
            )
        except (TelegramBadRequest, TelegramForbiddenError):
            pass

    if not await enqueue_download(session, queue_item, str(result.from_user.id)):
        if raw_inline_message_id:
            error_text = f"{QUEUE_LIMIT_MESSAGE}\n\n{source_link(url)}"
            try:
                await bot.edit_message_caption(
                    inline_message_id=raw_inline_message_id,
                    caption=error_text,
                    parse_mode="HTML",
                    reply_markup=None,
                )
            except (TelegramBadRequest, TelegramForbiddenError):
                try:
                    await bot.edit_message_text(
                        inline_message_id=raw_inline_message_id,
                        text=error_text,
                        parse_mode="HTML",
                        reply_markup=None,
                    )
                except (TelegramBadRequest, TelegramForbiddenError):
                    try:
                        await bot.send_message(
                            chat_id=result.from_user.id,
                            text=error_text,
                            parse_mode="HTML",
                        )
                    except (TelegramBadRequest, TelegramForbiddenError):
                        pass
        return
    stmt = select(func.count()).select_from(DownloadQueue).where(DownloadQueue.id <= queue_item.id)
    position = (await session.execute(stmt)).scalar_one()
    if raw_inline_message_id:
        caption = loading_placeholder_caption(position)
        try:
            await bot.edit_message_caption(
                inline_message_id=raw_inline_message_id,
                caption=caption,
                parse_mode="HTML",
                reply_markup=_placeholder_markup(),
            )
        except (TelegramBadRequest, TelegramForbiddenError):
            try:
                await bot.edit_message_text(
                    inline_message_id=raw_inline_message_id,
                    text=caption,
                    parse_mode="HTML",
                    reply_markup=_placeholder_markup(),
                )
            except (TelegramBadRequest, TelegramForbiddenError):
                pass
    if raw_inline_message_id:
        hold_inline_placeholder(raw_inline_message_id)


@inline_router.callback_query(F.data == "inline_loading")
async def _ack_inline_loading(callback: CallbackQuery):
    """
    Кнопка «⏳ Загружается…» в inline-плейсхолдере — фиктивная,
    просто чтобы Telegram вернул inline_message_id в ChosenInlineResult.
    Если пользователь нажмёт — погасим спиннер сразу, иначе он крутится 15с.
    """
    await callback.answer("Видео ещё загружается, подожди немного")
