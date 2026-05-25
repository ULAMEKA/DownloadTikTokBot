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
# from routers.inline_router import inline_router  # отложено на будущее
from services.queue import process_queue

bot = Bot(token=os.getenv('TOKEN'))  # type: ignore[arg-type]
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Middleware для сессии БД — нужен и message-, и callback_query-хендлерам
dp.message.middleware(DBSessionMiddleware())
dp.callback_query.middleware(DBSessionMiddleware())


async def main():
    await on_startup()
    await bot.delete_webhook(drop_pending_updates=True)
    asyncio.create_task(process_queue(bot))
    dp.include_routers(
        commands_router,
        link,
        # inline_router,  # отложено на будущее
    )
    await dp.start_polling(bot)


async def on_startup():
    async with engine.begin() as conn:
        # Миграция: если таблица VideoCache существует со старой схемой
        # (колонка url вместо tiktok_id) — пересоздаём её.
        try:
            await conn.execute(text("SELECT tiktok_id FROM VideoCache LIMIT 1"))
        except Exception:  # noqa: BLE001 — любая ошибка означает старую/отсутствующую схему
            await conn.execute(text("DROP TABLE IF EXISTS VideoCache"))
            logging.info("VideoCache: старая схема обнаружена, таблица пересоздана")

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
