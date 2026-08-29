import asyncio
import logging
import os

from dotenv import load_dotenv
load_dotenv()

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from handlers.link import link
from middleware.db_session import DBSessionMiddleware
from routers.command_router import commands_router
from routers.inline_router import inline_router
from services.queue import process_queue
from utils.logging_config import configure_logging

bot = Bot(token=os.getenv('TOKEN'))  # type: ignore[arg-type]
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Middleware для сессии БД
dp.message.middleware(DBSessionMiddleware())
dp.callback_query.middleware(DBSessionMiddleware())
dp.inline_query.middleware(DBSessionMiddleware())
dp.chosen_inline_result.middleware(DBSessionMiddleware())


async def main():
    # Схему БД создаёт отдельный скрипт init_db.py — запусти его вручную
    # перед первым стартом (или после изменения схемы). Здесь таблицы
    # считаются уже существующими.
    await bot.delete_webhook(drop_pending_updates=True)
    asyncio.create_task(process_queue(bot))
    dp.include_routers(
        commands_router,
        link,
        inline_router,
    )
    await dp.start_polling(bot)


if __name__ == '__main__':
    configure_logging()

    try:
        asyncio.run(main())
    except Exception as e:
        logging.exception(e)
