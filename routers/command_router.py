import logging
import re

from aiogram import Router
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from caption import source_link
from db.models import MusicRequest
from services.media_cache import (
    MediaIdentity,
    MediaProvider,
    find_cached_video_by_identity,
    invalidate_cached_video,
    is_invalid_cached_file_id_error,
)
from services.queue import (
    MUSIC_START_PREFIX,
    QUEUE_LIMIT_MESSAGE,
    TELEGRAM_START_PAYLOAD_LIMIT,
    VIDEO_START_PREFIX,
    deliver_cached_video,
    queue_music_deep_link,
)

commands_router = Router()


@commands_router.message(CommandStart())
async def start_handler(
    message: Message,
    session: AsyncSession,
):
    payload = (message.text or "").partition(" ")[2].strip()
    if payload.startswith(MUSIC_START_PREFIX):
        await _start_music_deep_link(message, session, payload)
        return
    if payload.startswith(VIDEO_START_PREFIX):
        await _start_video_deep_link(message, session, payload)
        return
    await message.answer("Пришли ссылку на TikTok!")


def _video_identity_from_payload(payload: str) -> MediaIdentity | None:
    if (
        len(payload) > TELEGRAM_START_PAYLOAD_LIMIT
        or not payload.startswith(VIDEO_START_PREFIX)
        or re.fullmatch(
            rf"{re.escape(VIDEO_START_PREFIX)}(?:tiktok|instagram)_[A-Za-z0-9_-]+",
            payload,
        ) is None
    ):
        return None
    identity_payload = payload.removeprefix(VIDEO_START_PREFIX)
    provider_name, separator, media_id = identity_payload.partition("_")
    if not separator or provider_name not in {
        MediaProvider.TIKTOK.value,
        MediaProvider.INSTAGRAM.value,
    }:
        return None
    try:
        return MediaIdentity(MediaProvider(provider_name), media_id)
    except ValueError:
        return None


def _video_identity_fallback_url(identity: MediaIdentity) -> str:
    if identity.provider is MediaProvider.INSTAGRAM:
        return f"https://www.instagram.com/reel/{identity.media_id}/"
    return f"https://www.tiktok.com/video/{identity.media_id}"


async def _start_video_deep_link(
    message: Message,
    session: AsyncSession,
    payload: str,
) -> None:
    if (
        message.chat.type != ChatType.PRIVATE
        or message.from_user is None
    ):
        await message.answer("Открой эту ссылку в личном чате с ботом.")
        return

    identity = _video_identity_from_payload(payload)
    if identity is None:
        await message.answer("Эта ссылка недействительна или устарела.")
        return

    fallback_url = _video_identity_fallback_url(identity)
    try:
        cached = await find_cached_video_by_identity(
            session,
            identity,
            fallback_url,
        )
    except Exception:
        await session.rollback()
        logging.exception("Video share cache lookup failed: %s", payload)
        await message.answer("Видео больше недоступно.")
        return
    if cached is None:
        await session.rollback()
        await message.answer("Видео больше недоступно.")
        return

    await session.rollback()
    try:
        await deliver_cached_video(
            message.bot,
            cached,
            chat_id=str(message.chat.id),
            fallback_source_url=fallback_url,
        )
    except TelegramBadRequest as error:
        if is_invalid_cached_file_id_error(error):
            await invalidate_cached_video(session, cached.cache_key)
        else:
            logging.warning("Video share delivery failed: %s", error)
        await message.answer("Видео больше недоступно.")
    except Exception:
        logging.exception("Video share delivery failed: %s", payload)
        await message.answer("Видео больше недоступно.")


async def _start_music_deep_link(
    message: Message,
    session: AsyncSession,
    payload: str,
) -> None:
    token = payload.removeprefix(MUSIC_START_PREFIX)
    if (
        message.chat.type != ChatType.PRIVATE
        or message.from_user is None
        or len(token) != 32
        or any(char not in "0123456789abcdef" for char in token)
    ):
        await message.answer("Открой эту ссылку в личном чате с ботом.")
        return

    request = await session.get(MusicRequest, token)
    if (
        request is None
        or request.state != "ready"
        or request.user_id
        or request.chat_id
        or request.status_message_id is not None
        or request.inline_message_id is not None
    ):
        await message.answer("Эта ссылка уже использована или устарела.")
        return

    status = await message.answer(
        f"🎵 Идёт распознавание…\n\n{source_link(request.tiktok_link)}",
        parse_mode=ParseMode.HTML,
    )
    try:
        queued = await queue_music_deep_link(
            session,
            token,
            str(message.from_user.id),
            str(message.chat.id),
            status.message_id,
        )
    except Exception:
        await session.rollback()
        await status.edit_text("❌ Не удалось запустить распознавание.")
        return

    if not queued:
        await status.edit_text(QUEUE_LIMIT_MESSAGE)

# @commands_router.message(StateFilter(None))
# async def fallback_handler(message: Message, state: FSMContext):
#     await message.answer("Ошибка! Пришлите ссылку на TikTok ещё раз")
