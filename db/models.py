from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class DownloadQueue(Base):
    __tablename__ = "BotDownloadQueue"

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[str | None] = mapped_column()
    filename: Mapped[str | None] = mapped_column()
    source_url: Mapped[str | None] = mapped_column()
    # Для inline-режима: ID отправленного сообщения в чате.
    # Если задан — очередь редактирует это сообщение, вставляя видео прямо в чат.
    inline_message_id: Mapped[str | None] = mapped_column()


class VideoCache(Base):
    __tablename__ = "VideoCache"

    id: Mapped[int] = mapped_column(primary_key=True)
    media_id: Mapped[str | None] = mapped_column(unique=True, index=True)  # ключ кеша "<платформа>:<id>"
    file_id: Mapped[str | None] = mapped_column()                          # Telegram file_id для быстрой пересылки
    storage_message_id: Mapped[int | None] = mapped_column()               # ID сообщения в Storage-канале
    description: Mapped[str | None] = mapped_column()                      # Описание ролика для подписи под видео
