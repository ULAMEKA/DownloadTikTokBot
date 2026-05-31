import re
import uuid

from aiogram import Router, F
from aiogram.types import (
    CallbackQuery,
    InlineQuery,
    InlineQueryResultArticle,
    InlineQueryResultCachedVideo,
    InputTextMessageContent,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ChosenInlineResult,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import DownloadQueue, VideoCache
from promo import PROMO_MESSAGE_HTML

inline_router = Router()

_TIKTOK_URL_RE = re.compile(r'(https?://)?(www\.|vm\.|vt\.)?tiktok\.com/\S+')
_TIKTOK_ID_RE  = re.compile(r'/video/(\d+)')


@inline_router.inline_query()
async def handle_inline_query(query: InlineQuery, session: AsyncSession):
    """
    Отвечает мгновенно — никогда не ждём загрузки внутри хендлера.

    Сценарий A (кеш есть):
        → InlineQueryResultCachedVideo: Telegram сам шлёт видео в чат.

    Сценарий Б (кеша нет):
        → InlineQueryResultArticle «⏳ Загружается…» с кнопкой.
          Кнопка нужна, чтобы Telegram вернул inline_message_id
          в событии ChosenInlineResult — через него очередь потом
          отредактирует сообщение и вставит видео прямо в чат.
    """
    text = query.query.strip()

    m_url = _TIKTOK_URL_RE.search(text)
    if not m_url:
        await query.answer([], cache_time=5)
        return

    url = m_url.group(0)

    # ── Быстрая проверка кеша (только regex, без yt-dlp) ─────────────────────
    m_id = _TIKTOK_ID_RE.search(url)
    if m_id:
        cache_res = await session.execute(
            select(VideoCache).where(VideoCache.tiktok_id == m_id.group(1))
        )
        cached = cache_res.scalar()
        if cached and cached.file_id:
            await query.answer(
                results=[
                    InlineQueryResultCachedVideo(
                        id="cached",
                        video_file_id=cached.file_id,
                        title="TikTok видео",
                    )
                ],
                cache_time=3600,
            )
            return

    # ── Кеша нет: статья-заглушка; реальная загрузка — через chosen_inline_result ──
    # reply_markup обязателен: только при наличии кнопки Telegram
    # возвращает inline_message_id в ChosenInlineResult.
    await query.answer(
        results=[
            InlineQueryResultArticle(
                id="download",
                title="📥 Загрузить видео",
                description="Видео появится прямо в этом чате",
                input_message_content=InputTextMessageContent(
                    message_text=(
                        "⏳ <b>Скачиваю TikTok видео…</b>\n\n"
                        f"{PROMO_MESSAGE_HTML}"
                    ),
                    parse_mode="HTML",
                ),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="⏳ Загружается…", callback_data="inline_loading")
                ]]),
            )
        ],
        cache_time=0,   # не кешировать — статус может смениться
    )


@inline_router.chosen_inline_result(F.result_id == "download")
async def chosen_download(result: ChosenInlineResult, session: AsyncSession):
    """
    Срабатывает, когда пользователь выбрал «Загрузить».
    Сохраняем inline_message_id — очередь отредактирует это сообщение
    и вставит видео прямо в чат.
    chat_id = user_id нужен как fallback (например, для получения file_id
    без Storage-канала).
    """
    m_url = _TIKTOK_URL_RE.search(result.query)
    if not m_url:
        return

    url = m_url.group(0)

    queue_item = DownloadQueue(
        chat_id=str(result.from_user.id),      # PM пользователя (fallback)
        filename=str(uuid.uuid4()),
        tiktok_link=url,
        inline_message_id=result.inline_message_id,  # ID сообщения в чате
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
