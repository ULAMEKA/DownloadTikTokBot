import asyncio
import logging
import os

from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv

from handlers.link import link
from routers.command_router import commands_router

load_dotenv()


from aiogram import Bot, Dispatcher
from aiogram.types import Message
from aiogram.filters import CommandStart, Command
from aiogram import F

bot = Bot(token=os.getenv('TOKEN'))  # Берем токен
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    dp.include_routers(commands_router, link)
    await dp.start_polling(
        bot)


if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler("bot.log"),
            logging.StreamHandler()
        ]
    )

    while True:
        try:
            asyncio.run(main())
        except Exception as e:
            logging.exception(e)