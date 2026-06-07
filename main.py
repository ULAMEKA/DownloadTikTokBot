import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from config import TOKEN
from handlers.link import link
from middleware.db_session import DBSessionMiddleware
from routers.command_router import commands_router
from routers.inline_router import inline_router
from services.queue import process_queue

bot = Bot(token=TOKEN)
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
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler("bot.log"),
            logging.StreamHandler()
        ]
    )
    # Глушим спам SQLAlchemy (BEGIN, ROLLBACK, SELECT каждую секунду)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

    try:
        asyncio.run(main())
    except Exception as e:
        logging.exception(e)
