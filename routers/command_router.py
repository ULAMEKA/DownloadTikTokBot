from aiogram import Router
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import CommandStart
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from caption import source_link
from db.models import MusicRequest
from services.queue import (
    MUSIC_START_PREFIX,
    QUEUE_LIMIT_MESSAGE,
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
    await message.answer("Пришли ссылку на TikTok!")


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
