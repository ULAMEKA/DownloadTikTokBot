"""
Создание и миграция базы данных. Запускать ВРУЧНУЮ — при первом развёртывании
или после изменения схемы:

    python init_db.py

main.py схему БД не трогает: бот рассчитывает, что таблицы уже созданы этим
скриптом. Скрипт идемпотентен — повторный запуск ничего не ломает.
"""
import asyncio
import logging

from sqlalchemy import text

from db.base import Base
from db.session import engine
# Импорт моделей обязателен: без него классы не регистрируются в Base.metadata
# и create_all не создаст ни одной таблицы.
import db.models  # noqa: F401


async def init_db() -> None:
    async with engine.begin() as conn:
        # 1. Создаём недостающие таблицы по текущим моделям. На чистой БД они
        #    появятся сразу с актуальной схемой, и миграции ниже станут no-op.
        #    На существующей БД create_all не трогает уже созданные таблицы.
        await conn.run_sync(Base.metadata.create_all)

        # 2. Миграция VideoCache: ключ кеша теперь композитный (media_id =
        #    "<платформа>:<id>" вместо голого tiktok_id). Старые значения
        #    несовместимы, а это просто кеш — пересоздаём таблицу, если колонки
        #    media_id ещё нет (шаг 4 создаст её заново уже с актуальной схемой).
        try:
            await conn.execute(text("SELECT media_id FROM VideoCache LIMIT 1"))
        except Exception:  # noqa: BLE001
            await conn.execute(text("DROP TABLE IF EXISTS VideoCache"))
            logging.info("VideoCache: старая схема обнаружена, таблица удалена для пересоздания")

        # 3. Миграция DownloadQueue: переименовать tiktok_link → source_url,
        #    сохранив записи очереди (бот теперь качает не только TikTok).
        try:
            await conn.execute(text("SELECT source_url FROM BotDownloadQueue LIMIT 1"))
        except Exception:  # noqa: BLE001
            await conn.execute(text(
                "ALTER TABLE BotDownloadQueue RENAME COLUMN tiktok_link TO source_url"
            ))
            logging.info("BotDownloadQueue: колонка tiktok_link переименована в source_url")

        # 4. Повторный create_all пересоздаёт VideoCache, если шаг 2 её удалил.
        await conn.run_sync(Base.metadata.create_all)

    await engine.dispose()


if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
    )
    asyncio.run(init_db())
    logging.info("БД готова.")
