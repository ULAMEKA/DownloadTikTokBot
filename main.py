import asyncio
import logging
import os

from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv

from db.base import Base
from db.session import engine
from handlers.link import link
from middleware.db_session import DBSessionMiddleware
from routers.command_router import commands_router
from services.queue import process_queue

load_dotenv()

from db.models import DownloadQueue
from aiogram import Bot, Dispatcher

bot = Bot(token=os.getenv('TOKEN'))  # Берем токен
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
dp.message.middleware(DBSessionMiddleware())


async def main():
    await on_startup()  # ← вызываем внутри одного loop
    await bot.delete_webhook(drop_pending_updates=True)
    asyncio.create_task(process_queue(bot))
    dp.include_routers(commands_router, link)
    await dp.start_polling(bot)


async def on_startup():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler("bot.log"),
            logging.StreamHandler()
        ]
    )

    try:
        asyncio.run(main())
    except Exception as e:
        logging.exception(e)