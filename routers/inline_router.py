import uuid

from aiogram import Router, F
from aiogram.types import (
    CallbackQuery,
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ChosenInlineResult,
)
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import DownloadQueue
from promo import AD_PLACEHOLDER_HTML
from services.queue import hold_inline_placeholder
from utils.links import SUPPORTED_URL_RE

inline_router = Router()


@inline_router.inline_query()
async def handle_inline_query(query: InlineQuery):
    text = query.query.strip()

    if not SUPPORTED_URL_RE.search(text):
        await query.answer([], cache_time=5)
        return

    content = InputTextMessageContent(
        message_text=(
            "⏳ <b>Скачиваю видео…</b>\n\n"
            f"{AD_PLACEHOLDER_HTML}"
        ),
        parse_mode="HTML",
    )
    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⏳ Загружается…", callback_data="inline_loading")
    ]])
    await query.answer(
        results=[
            InlineQueryResultArticle(
                id="download",
                title="📤 Отправить",
                description="Отправить видео с описанием",
                input_message_content=content,
                reply_markup=markup,
            ),
            InlineQueryResultArticle(
                id="download_spoiler",
                title="🙈 Отправить под спойлером",
                description="Скрыть видео и описание до нажатия",
                input_message_content=content,
                reply_markup=markup,
            ),
            InlineQueryResultArticle(
                id="download_no_description",
                title="🧹 Без описания",
                description="Отправить видео без описания",
                input_message_content=content,
                reply_markup=markup,
            ),
            InlineQueryResultArticle(
                id="download_no_description_spoiler",
                title="🙈 Без описания, под спойлером",
                description="Скрыть видео до нажатия",
                input_message_content=content,
                reply_markup=markup,
            ),
        ],
        cache_time=0,
    )


@inline_router.chosen_inline_result(
    (F.result_id == "download")
    | (F.result_id == "download_spoiler")
    | (F.result_id == "download_no_description")
    | (F.result_id == "download_no_description_spoiler")
)
async def chosen_download(result: ChosenInlineResult, session: AsyncSession):
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
    inline_message_id = result.inline_message_id
    if inline_message_id:
        hold_inline_placeholder(inline_message_id)
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
    session.add(queue_item)
    await session.commit()


@inline_router.callback_query(F.data == "inline_loading")
async def _ack_inline_loading(callback: CallbackQuery):
    """
    Кнопка «⏳ Загружается…» в inline-плейсхолдере — фиктивная,
    просто чтобы Telegram вернул inline_message_id в ChosenInlineResult.
    Если пользователь нажмёт — погасим спиннер сразу, иначе он крутится 15с.
    """
    await callback.answer("Видео ещё загружается, подожди немного")
