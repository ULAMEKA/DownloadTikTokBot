from aiogram import Router, F
from aiogram.filters import CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from states import DownloadVideo

commands_router = Router()


@commands_router.message(CommandStart())
async def start_handler(message: Message, state: FSMContext):
    await state.set_state(DownloadVideo.waiting_for_link)
    await message.answer("Бот запущен! Пришли ссылку на TikTok")



# @commands_router.message(StateFilter(None))
# async def fallback_handler(message: Message, state: FSMContext):
#     await message.answer("Пришли ссылку на TikTok")
#     await state.set_state(DownloadVideo.waiting_for_link)
