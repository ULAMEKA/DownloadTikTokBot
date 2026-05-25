import asyncio
import logging
import os

from dotenv import load_dotenv
load_dotenv()

from sqlalchemy import text

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from db.base import Base
from db.session import engine
from handlers.link import link
from middleware.db_session import DBSessionMiddleware
from routers.command_router import commands_router
from routers.inline_router import inline_router
from services.queue import process_queue

bot = Bot(token=os.getenv('TOKEN'))  # type: ignore[arg-type]
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Middleware для сессии БД
dp.message.middleware(DBSessionMiddleware())
dp.callback_query.middleware(DBSessionMiddleware())
dp.inline_query.middleware(DBSessionMiddleware())
dp.chosen_inline_result.middleware(DBSessionMiddleware())


async def main():
    await on_startup()
    await bot.delete_webhook(drop_pending_updates=True)
    asyncio.create_task(process_queue(bot))
    dp.include_routers(
        commands_router,
        link,
        inline_router,
    )
    await dp.start_polling(bot)


async def on_startup():
    async with engine.begin() as conn:
        # Миграция VideoCache: если колонка tiktok_id отсутствует — пересоздаём таблицу.
        try:
            await conn.execute(text("SELECT tiktok_id FROM VideoCache LIMIT 1"))
        except Exception:  # noqa: BLE001
            await conn.execute(text("DROP TABLE IF EXISTS VideoCache"))
            logging.info("VideoCache: старая схема обнаружена, таблица пересоздана")

        # Миграция DownloadQueue: добавляем inline_message_id если ещё нет.
        try:
            await conn.execute(text("SELECT inline_message_id FROM BotDownloadQueue LIMIT 1"))
        except Exception:  # noqa: BLE001
            await conn.execute(text(
                "ALTER TABLE BotDownloadQueue ADD COLUMN inline_message_id TEXT"
            ))
            logging.info("BotDownloadQueue: добавлена колонка inline_message_id")

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
    # Глушим спам SQLAlchemy (BEGIN, ROLLBACK, SELECT каждую секунду)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

    try:
        asyncio.run(main())
    except Exception as e:
        logging.exception(e)
