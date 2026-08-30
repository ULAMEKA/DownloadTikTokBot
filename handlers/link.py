import asyncio
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.enums import ChatType, ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from caption import source_link
from db.models import DownloadQueue, MusicRequest
from promo import AD_PLACEHOLDER_IMAGE, loading_placeholder_caption
from services.queue import (
    QUEUE_LIMIT_MESSAGE,
    MUSIC_JOB_PREFIX,
    MUSIC_PAGE_PREFIX,
    MediaInspection,
    VideoQuality,
    enqueue_download,
    hold_placeholder,
    inspect_media,
    move_info_snapshot,
    needs_quality_choice,
    release_placeholder,
    show_music_request_page,
    show_music_request_result,
    try_deliver_cached_video,
    update_music_request_message,
    write_info_snapshot,
)
from utils.links import SUPPORTED_URL_RE, canonicalize_video_url, find_video_url

link = Router()


@dataclass
class PendingDownload:
    user_id: int
    url: str
    mode: str | None = None
    inspection: "PendingInspection | None" = None
    snapshot_path: Path | None = None


@dataclass(frozen=True)
class PendingInspection:
    qualities: tuple[VideoQuality, ...]


_pending: dict[str, PendingDownload] = {}
_pending_expiry_tasks: dict[str, asyncio.Task[None]] = {}
PENDING_TTL_SECONDS = 600
MAX_PENDING_PER_USER = 2
MAX_PENDING_GLOBAL = 256


async def _video_url_from_text(text: str) -> str | None:
    url = find_video_url(text)
    if url is None:
        return None
    try:
        return await asyncio.get_running_loop().run_in_executor(
            None,
            canonicalize_video_url,
            url,
        )
    except Exception:
        return url


def _cleanup_pending_snapshot(pending: PendingDownload) -> None:
    snapshot_path = pending.snapshot_path
    pending.snapshot_path = None
    if snapshot_path is None:
        return
    try:
        snapshot_path.unlink(missing_ok=True)
    except OSError as error:
        logging.warning("Не удалось удалить pending info snapshot %s: %s", snapshot_path, error)


def _cancel_pending_expiry(token: str) -> None:
    task = _pending_expiry_tasks.pop(token, None)
    if task is None or task.done() or task is asyncio.current_task():
        return
    task.cancel()


def _drop_pending(token: str, *, cleanup_snapshot: bool) -> PendingDownload | None:
    pending = _pending.pop(token, None)
    _cancel_pending_expiry(token)
    if pending is not None and cleanup_snapshot:
        _cleanup_pending_snapshot(pending)
    return pending


def _take_pending(token: str) -> PendingDownload | None:
    return _drop_pending(token, cleanup_snapshot=False)


async def _expire_pending(
    token: str,
    expected: PendingDownload | None = None,
) -> None:
    if expected is None:
        expected = _pending.get(token)
    await asyncio.sleep(PENDING_TTL_SECONDS)
    if expected is not None and _pending.get(token) is expected:
        _drop_pending(token, cleanup_snapshot=True)


def _store_pending(token: str, pending: PendingDownload) -> None:
    previous = _pending.get(token)
    if previous is not None:
        _drop_pending(token, cleanup_snapshot=previous is not pending)

    same_user_tokens = [
        current_token
        for current_token, current in _pending.items()
        if current.user_id == pending.user_id
    ]
    while len(same_user_tokens) >= MAX_PENDING_PER_USER:
        _drop_pending(same_user_tokens.pop(0), cleanup_snapshot=True)

    while len(_pending) >= MAX_PENDING_GLOBAL:
        oldest_token = next(iter(_pending))
        _drop_pending(oldest_token, cleanup_snapshot=True)

    _pending[token] = pending
    _pending_expiry_tasks[token] = asyncio.create_task(_expire_pending(token, pending))


def _quality_markup(token: str, qualities: list[VideoQuality]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=quality.label,
            callback_data=f"quality:{token}:{index}",
        )]
        for index, quality in enumerate(qualities)
    ])


async def _render_private_enqueue_error(
    callback: CallbackQuery,
    text: str,
    *,
    callback_answered: bool,
) -> None:
    if callback_answered and callback.message:
        await callback.message.answer(text)
    else:
        await callback.answer(text, show_alert=True)


async def _start_pending_download(
    callback: CallbackQuery,
    session: AsyncSession,
    token: str,
    pending: PendingDownload,
    format_id: str | None,
    callback_answered: bool = False,
) -> None:
    placeholder = None
    queue_item = DownloadQueue(
        chat_id=str(callback.from_user.id),
        filename=str(uuid.uuid4()),
        tiktok_link=pending.url,
        format_id=format_id,
        inline_message_id=f"direct:{pending.mode}" if pending.mode != "normal" else None,
    )
    try:
        if pending.snapshot_path is not None:
            pending.snapshot_path = move_info_snapshot(
                pending.snapshot_path,
                queue_item.filename,
            )
        if callback.message:
            placeholder = await callback.message.answer_photo(
                photo=FSInputFile(str(AD_PLACEHOLDER_IMAGE)),
                caption=loading_placeholder_caption(),
                parse_mode=ParseMode.HTML,
            )
            queue_item.reply_to_message_id = -placeholder.message_id
            hold_placeholder(str(callback.from_user.id), placeholder.message_id)
        enqueued = await enqueue_download(session, queue_item, str(callback.from_user.id))
    except Exception as error:
        logging.exception("Не удалось поставить private-загрузку в очередь: %s", error)
        if placeholder:
            release_placeholder(str(callback.from_user.id), placeholder.message_id)
            try:
                await placeholder.delete()
            except Exception:
                logging.exception("Не удалось удалить private placeholder после ошибки enqueue")
        _store_pending(token, pending)
        await _render_private_enqueue_error(
            callback,
            "❌ Не удалось загрузить видео.",
            callback_answered=callback_answered,
        )
        return
    if not enqueued:
        if placeholder:
            release_placeholder(str(callback.from_user.id), placeholder.message_id)
            try:
                await placeholder.delete()
            except Exception:
                logging.exception("Не удалось удалить private placeholder после отказа enqueue")
        _store_pending(token, pending)
        await _render_private_enqueue_error(
            callback,
            QUEUE_LIMIT_MESSAGE,
            callback_answered=callback_answered,
        )
        return

    pending.snapshot_path = None
    if callback.message:
        await callback.message.delete()

    stmt = select(func.count()).select_from(DownloadQueue).where(DownloadQueue.id <= queue_item.id)
    position = (await session.execute(stmt)).scalar_one()
    if placeholder:
        await placeholder.edit_caption(
            caption=loading_placeholder_caption(position),
            parse_mode=ParseMode.HTML,
        )
    if not callback_answered:
        await callback.answer()


@link.message(F.text.regexp(SUPPORTED_URL_RE.pattern), F.chat.type == ChatType.PRIVATE)
async def handle_video_link_private(message: Message):
    url = await _video_url_from_text(message.text or "")
    if url is None:
        return

    token = uuid.uuid4().hex
    _store_pending(token, PendingDownload(message.chat.id, url))
    await message.answer(
        f"Как загрузить видео?\n\n{source_link(url)}",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="⬇️ Загрузить",
                callback_data=f"download:normal:{token}",
            )],
            [
                InlineKeyboardButton(
                    text="📝 Без описания",
                    callback_data=f"download:no_description:{token}",
                ),
                InlineKeyboardButton(
                    text="🙈 Под спойлером",
                    callback_data=f"download:spoiler:{token}",
                ),
            ],
            [InlineKeyboardButton(
                text="🙈 Без описания, под спойлером",
                callback_data=f"download:no_description_spoiler:{token}",
            )],
            [InlineKeyboardButton(
                text="🎵 Распознать музыку",
                callback_data=f"{MUSIC_JOB_PREFIX}{token}",
            )],
        ]),
    )


@link.callback_query(F.data.startswith("download:"))
async def handle_download_option(callback: CallbackQuery, session: AsyncSession, bot: Bot):
    if callback.data is None:
        await callback.answer()
        return

    parts = callback.data.split(":", 2)
    if len(parts) != 3:
        await callback.answer()
        return
    _, mode, token = parts
    pending = _pending.get(token)
    if mode not in ("normal", "no_description", "spoiler", "no_description_spoiler") or pending is None:
        await callback.answer("Ссылка устарела. Отправьте её ещё раз.", show_alert=True)
        return
    if callback.from_user.id != pending.user_id:
        await callback.answer("Эта кнопка предназначена другому пользователю.", show_alert=True)
        return
    pending = _take_pending(token)
    if pending is None:
        await callback.answer("Ссылка уже обрабатывается.")
        return

    await callback.answer()
    pending.mode = mode
    without_description = mode in ("no_description", "no_description_spoiler")
    has_spoiler = mode in ("spoiler", "no_description_spoiler")
    if await try_deliver_cached_video(
        bot,
        session,
        pending.url,
        chat_id=str(callback.from_user.id),
        has_spoiler=has_spoiler,
        without_description=without_description,
    ):
        _cleanup_pending_snapshot(pending)
        if callback.message:
            await callback.message.delete()
        return
    try:
        loop = asyncio.get_running_loop()
        inspection = await loop.run_in_executor(None, inspect_media, pending.url)
    except Exception:
        inspection = None
        qualities = []
    else:
        qualities = list(inspection.qualities)
        try:
            pending.snapshot_path = write_info_snapshot(
                f"pending_{token}",
                inspection.info,
            )
        except (OSError, TypeError, ValueError) as error:
            logging.warning("Не удалось сохранить pending info snapshot: %s", error)
            _store_pending(token, pending)
            await _render_private_enqueue_error(
                callback,
                "❌ Не удалось загрузить видео.",
                callback_answered=True,
            )
            return
        pending.inspection = PendingInspection(tuple(inspection.qualities))
    if not needs_quality_choice(qualities):
        await _start_pending_download(
            callback,
            session,
            token,
            pending,
            format_id=None,
            callback_answered=True,
        )
        return

    _store_pending(token, pending)
    if callback.message:
        await callback.message.edit_text(
            "Видео больше 50 МБ. Выберите качество — бот отправит его частями примерно по 50 МБ.",
            reply_markup=_quality_markup(token, qualities),
        )


@link.callback_query(F.data.startswith("quality:"))
async def handle_quality_option(callback: CallbackQuery, session: AsyncSession):
    if callback.data is None:
        await callback.answer()
        return

    parts = callback.data.split(":", 2)
    if len(parts) != 3:
        await callback.answer()
        return
    _, token, index_text = parts
    pending = _pending.get(token)
    if pending is None or pending.mode is None or pending.inspection is None:
        await callback.answer("Ссылка устарела. Отправьте её ещё раз.", show_alert=True)
        return
    if callback.from_user.id != pending.user_id:
        await callback.answer("Эта кнопка предназначена другому пользователю.", show_alert=True)
        return
    try:
        quality = pending.inspection.qualities[int(index_text)]
    except (IndexError, ValueError):
        await callback.answer("Этот вариант больше недоступен. Отправьте ссылку ещё раз.", show_alert=True)
        return
    pending = _take_pending(token)
    if pending is None:
        await callback.answer("Ссылка уже обрабатывается.")
        return

    await _start_pending_download(
        callback,
        session,
        token,
        pending,
        quality.format_id,
    )


@link.callback_query(F.data.startswith(MUSIC_JOB_PREFIX))
async def handle_music_recognition(
    callback: CallbackQuery,
    session: AsyncSession,
    bot: Bot,
):
    token = (callback.data or "").removeprefix(MUSIC_JOB_PREFIX)
    if len(token) != 32:
        await callback.answer("Кнопка устарела.", show_alert=True)
        return

    request = await session.get(MusicRequest, token)
    if request is None:
        pending = _pending.get(token)
        if (
            pending is None
            or pending.user_id != callback.from_user.id
            or callback.message is None
        ):
            await callback.answer("Кнопка устарела.", show_alert=True)
            return
        pending = _take_pending(token)
        if pending is None:
            await callback.answer("Ссылка уже обрабатывается.")
            return
        request = MusicRequest(
            token=token,
            user_id=str(callback.from_user.id),
            chat_id=str(callback.message.chat.id),
            status_message_id=callback.message.message_id,
            tiktok_link=pending.url,
            state="ready",
        )
        session.add(request)
        try:
            await session.commit()
        except Exception:
            await session.rollback()
            _store_pending(token, pending)
            await callback.answer("Не удалось запустить распознавание.", show_alert=True)
            return
    if str(callback.from_user.id) != request.user_id:
        await callback.answer("Эта кнопка предназначена другому пользователю.", show_alert=True)
        return
    if request.inline_message_id:
        if callback.inline_message_id != request.inline_message_id:
            await callback.answer("Кнопка устарела.", show_alert=True)
            return
    elif (
        callback.message is None
        or request.status_message_id != callback.message.message_id
        or str(callback.message.chat.id) != request.chat_id
    ):
        await callback.answer("Кнопка устарела.", show_alert=True)
        return

    if request.state == "done":
        await callback.answer("Результат уже готов.")
        await show_music_request_result(bot, request, request.music_tracks)
        return
    if request.state == "queued":
        await callback.answer("🎵 Распознавание уже в очереди.")
        return

    updated = await session.execute(
        update(MusicRequest)
        .where(MusicRequest.token == token, MusicRequest.state == "ready")
        .values(state="queued")
    )
    if updated.rowcount != 1:
        await callback.answer("Распознавание уже запускается.")
        return

    queue_item = DownloadQueue(
        chat_id=request.chat_id,
        filename=f"music_{uuid.uuid4().hex}",
        tiktok_link=request.tiktok_link,
        format_id=request.format_id,
        inline_message_id=f"{MUSIC_JOB_PREFIX}{token}",
    )
    if not await enqueue_download(session, queue_item, request.user_id):
        await session.execute(
            update(MusicRequest)
            .where(MusicRequest.token == token)
            .values(state="ready")
        )
        await session.commit()
        await callback.answer(QUEUE_LIMIT_MESSAGE, show_alert=True)
        return

    await update_music_request_message(bot, request, "🎵 Идёт распознавание…")
    await callback.answer("🎵 Распознавание добавлено в очередь.")


@link.callback_query(F.data.startswith(MUSIC_PAGE_PREFIX))
async def handle_music_page(
    callback: CallbackQuery,
    session: AsyncSession,
    bot: Bot,
):
    payload = (callback.data or "").removeprefix(MUSIC_PAGE_PREFIX)
    token, separator, page_text = payload.rpartition(":")
    if not separator or len(token) != 32:
        await callback.answer("Кнопка устарела.", show_alert=True)
        return
    try:
        page = int(page_text)
    except ValueError:
        await callback.answer("Кнопка устарела.", show_alert=True)
        return
    if page < 0:
        await callback.answer("Кнопка устарела.", show_alert=True)
        return

    request = await session.get(MusicRequest, token)
    if (
        request is None
        or request.state != "done"
        or not request.music_tracks
        or request.inline_message_id != callback.inline_message_id
        or request.user_id != str(callback.from_user.id)
    ):
        await callback.answer("Кнопка устарела.", show_alert=True)
        return

    await show_music_request_page(bot, request, page)
    await callback.answer()


@link.callback_query(F.data == "video_stats")
async def handle_video_stats(callback: CallbackQuery):
    await callback.answer("Статистика показана на момент загрузки")


@link.message(F.text.regexp(SUPPORTED_URL_RE.pattern))
async def handle_video_link_group(message: Message, session: AsyncSession, bot: Bot):
    url = await _video_url_from_text(message.text or "")
    if url is None:
        return

    if await try_deliver_cached_video(
        bot,
        session,
        url,
        chat_id=str(message.chat.id),
        reply_to_message_id=message.message_id,
    ):
        return

    placeholder = await message.reply_photo(
        photo=FSInputFile(str(AD_PLACEHOLDER_IMAGE)),
        caption=loading_placeholder_caption(),
        parse_mode=ParseMode.HTML,
    )
    queue_item = DownloadQueue(
        chat_id=str(message.chat.id),
        filename=str(uuid.uuid4()),
        tiktok_link=url,
        reply_to_message_id=-placeholder.message_id,
    )
    user_id = str(message.from_user.id if message.from_user else message.chat.id)
    hold_placeholder(str(message.chat.id), placeholder.message_id)
    try:
        enqueued = await enqueue_download(session, queue_item, user_id)
    except Exception as error:
        logging.exception("Не удалось поставить group-загрузку в очередь: %s", error)
        release_placeholder(str(message.chat.id), placeholder.message_id)
        try:
            await placeholder.delete()
        except Exception:
            logging.exception("Не удалось удалить group placeholder после ошибки enqueue")
        await message.reply("❌ Не удалось загрузить видео.")
        return
    if not enqueued:
        release_placeholder(str(message.chat.id), placeholder.message_id)
        try:
            await placeholder.delete()
        except Exception:
            logging.exception("Не удалось удалить group placeholder после отказа enqueue")
        await message.reply(QUEUE_LIMIT_MESSAGE)
        return
    stmt = select(func.count()).select_from(DownloadQueue).where(DownloadQueue.id <= queue_item.id)
    position = (await session.execute(stmt)).scalar_one()
    await placeholder.edit_caption(
        caption=loading_placeholder_caption(position),
        parse_mode=ParseMode.HTML,
    )


@link.message(F.chat.type == ChatType.PRIVATE)
async def handle_unsupported_link(message: Message, state: FSMContext):
    await message.answer("Пришли ссылку на TikTok или Reels.")
