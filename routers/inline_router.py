from aiogram import Router, F
from aiogram.types import (
    CallbackQuery,
    InlineQuery,
    InlineQueryResultArticle,
    InlineQueryResultCachedVideo,
    InputTextMessageContent,
    ChosenInlineResult,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import VideoCache
from services.queue import enqueue_download
from ui.caption import build_caption
from ui.keyboards import source_button, loading_placeholder
from ui.promo import PROMO_MESSAGE_HTML
from utils.links import find_video_url, media_key

inline_router = Router()


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
    url = find_video_url(query.query.strip())
    if not url:
        await query.answer([], cache_time=5)
        return

    # ── Быстрая проверка кеша (только regex, без yt-dlp) ─────────────────────
    key = media_key(url)
    if key:
        cache_res = await session.execute(
            select(VideoCache).where(VideoCache.media_id == key)
        )
        cached = cache_res.scalar()
        if cached and cached.file_id:
            await query.answer(
                results=[
                    InlineQueryResultCachedVideo(
                        id="cached",
                        video_file_id=cached.file_id,
                        title="Видео",
                        # Подпись и кнопка-источник — как при обычной доставке,
                        # иначе из кеша видео уходит без ссылки на оригинал.
                        caption=build_caption(cached.description, url, with_source=False),
                        parse_mode="HTML",
                        reply_markup=source_button(url),
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
                        "⏳ <b>Скачиваю видео…</b>\n\n"
                        f"{PROMO_MESSAGE_HTML}"
                    ),
                    parse_mode="HTML",
                ),
                reply_markup=loading_placeholder(),
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
    url = find_video_url(result.query)
    if not url:
        return

    await enqueue_download(
        session,
        chat_id=str(result.from_user.id),      # PM пользователя (fallback)
        source_url=url,
        inline_message_id=result.inline_message_id,  # ID сообщения в чате
    )


@inline_router.callback_query(F.data == "inline_loading")
async def _ack_inline_loading(callback: CallbackQuery):
    """
    Кнопка «⏳ Загружается…» в inline-плейсхолдере — фиктивная,
    просто чтобы Telegram вернул inline_message_id в ChosenInlineResult.
    Если пользователь нажмёт — погасим спиннер сразу, иначе он крутится 15с.
    """
    await callback.answer("Видео ещё загружается, подожди немного")
