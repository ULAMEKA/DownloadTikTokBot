from sqlalchemy import Column, Integer, String
from .base import Base


class DownloadQueue(Base):
    __tablename__ = "BotDownloadQueue"
    id = Column(Integer, primary_key=True)
    chat_id = Column(String)
    filename = Column(String)
    tiktok_link = Column(String)
    reply_to_message_id = Column(Integer, nullable=True)
    # Для inline-режима: ID отправленного сообщения в чате.
    # Если задан — очередь редактирует это сообщение, вставляя видео прямо в чат.
    inline_message_id = Column(String, nullable=True)


class VideoCache(Base):
    __tablename__ = "VideoCache"
    id = Column(Integer, primary_key=True)
    tiktok_id = Column(String, unique=True, index=True)   # ID видео TikTok (не URL)
    file_id = Column(String)                               # Telegram file_id для быстрой пересылки
    storage_message_id = Column(Integer)                   # ID сообщения в Storage-канале
    description = Column(String, nullable=True)            # Описание ролика для подписи под видео
