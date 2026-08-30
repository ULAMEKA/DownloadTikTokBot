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
        #    На существующей БД create_all не трогает уже созданные таблицы —
        #    их донастраивают миграции (ALTER идёт по реально существующей
        #    таблице, а не падает с "no such table").
        await conn.run_sync(Base.metadata.create_all)

        # 2. Миграция VideoCache: если колонки tiktok_id нет (старая схема) —
        #    удаляем таблицу, чтобы пересоздать её в шаге 4.
        try:
            await conn.execute(text("SELECT tiktok_id FROM VideoCache LIMIT 1"))
        except Exception:  # noqa: BLE001
            await conn.execute(text("DROP TABLE IF EXISTS VideoCache"))
            logging.info("VideoCache: старая схема обнаружена, таблица удалена для пересоздания")

        # 3. Миграция DownloadQueue: добавляем новые колонки, если их ещё нет.
        try:
            await conn.execute(text("SELECT inline_message_id FROM BotDownloadQueue LIMIT 1"))
        except Exception:  # noqa: BLE001
            await conn.execute(text(
                "ALTER TABLE BotDownloadQueue ADD COLUMN inline_message_id TEXT"
            ))
            logging.info("BotDownloadQueue: добавлена колонка inline_message_id")

        try:
            await conn.execute(text("SELECT reply_to_message_id FROM BotDownloadQueue LIMIT 1"))
        except Exception:  # noqa: BLE001
            await conn.execute(text(
                "ALTER TABLE BotDownloadQueue ADD COLUMN reply_to_message_id INTEGER"
            ))
            logging.info("BotDownloadQueue: добавлена колонка reply_to_message_id")

        try:
            await conn.execute(text("SELECT user_id FROM BotDownloadQueue LIMIT 1"))
        except Exception:  # noqa: BLE001
            await conn.execute(text(
                "ALTER TABLE BotDownloadQueue ADD COLUMN user_id TEXT"
            ))
            logging.info("BotDownloadQueue: добавлена колонка user_id")

        try:
            await conn.execute(text("SELECT format_id FROM BotDownloadQueue LIMIT 1"))
        except Exception:  # noqa: BLE001
            await conn.execute(text(
                "ALTER TABLE BotDownloadQueue ADD COLUMN format_id TEXT"
            ))
            logging.info("BotDownloadQueue: добавлена колонка format_id")

        try:
            await conn.execute(text("SELECT enqueued_at FROM BotDownloadQueue LIMIT 1"))
        except Exception:  # noqa: BLE001
            await conn.execute(text(
                "ALTER TABLE BotDownloadQueue ADD COLUMN enqueued_at DATETIME"
            ))
            logging.info("BotDownloadQueue: добавлена колонка enqueued_at")

        # 4. Повторный create_all пересоздаёт VideoCache, если миграция её удалила.
        await conn.run_sync(Base.metadata.create_all)

        # 5. Миграция VideoCache: добавляем description, если колонки ещё нет
        #    (БД, созданная до появления подписи-описания под видео).
        try:
            await conn.execute(text("SELECT description FROM VideoCache LIMIT 1"))
        except Exception:  # noqa: BLE001
            await conn.execute(text(
                "ALTER TABLE VideoCache ADD COLUMN description TEXT"
            ))
            logging.info("VideoCache: добавлена колонка description")

        # 6. Миграция VideoCache: сохраняем список распознанных треков в кеше.
        try:
            await conn.execute(text("SELECT music_tracks FROM VideoCache LIMIT 1"))
        except Exception:  # noqa: BLE001
            await conn.execute(text(
                "ALTER TABLE VideoCache ADD COLUMN music_tracks TEXT"
            ))
            logging.info("VideoCache: добавлена колонка music_tracks")

        # 7. Миграция VideoCache: сохраняем исходную ссылку как snapshot кеша.
        try:
            await conn.execute(text("SELECT source_url FROM VideoCache LIMIT 1"))
        except Exception:  # noqa: BLE001
            await conn.execute(text(
                "ALTER TABLE VideoCache ADD COLUMN source_url TEXT"
            ))
            logging.info("VideoCache: добавлена колонка source_url")

        # 8. Миграция VideoCache: сохраняем число лайков как snapshot кеша.
        try:
            await conn.execute(text("SELECT likes FROM VideoCache LIMIT 1"))
        except Exception:  # noqa: BLE001
            await conn.execute(text(
                "ALTER TABLE VideoCache ADD COLUMN likes INTEGER"
            ))
            logging.info("VideoCache: добавлена колонка likes")

        # 9. MusicRequest: add UI message references for installations that
        #    already created the table before manual recognition was refined.
        try:
            await conn.execute(text("SELECT status_message_id FROM MusicRequest LIMIT 1"))
        except Exception:  # noqa: BLE001
            await conn.execute(text(
                "ALTER TABLE MusicRequest ADD COLUMN status_message_id INTEGER"
            ))
            logging.info("MusicRequest: добавлена колонка status_message_id")

        try:
            await conn.execute(text("SELECT inline_message_id FROM MusicRequest LIMIT 1"))
        except Exception:  # noqa: BLE001
            await conn.execute(text(
                "ALTER TABLE MusicRequest ADD COLUMN inline_message_id TEXT"
            ))
            logging.info("MusicRequest: добавлена колонка inline_message_id")

    await engine.dispose()


if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
    )
    asyncio.run(init_db())
    logging.info("БД готова.")
