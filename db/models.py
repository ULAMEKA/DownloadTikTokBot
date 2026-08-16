from sqlalchemy import Column, Integer, String
from .base import Base


class DownloadQueue(Base):
    __tablename__ = "BotDownloadQueue"
    id = Column(Integer, primary_key=True)
    chat_id = Column(String)
    user_id = Column(String, nullable=True)
    filename = Column(String)
    tiktok_link = Column(String)
    format_id = Column(String, nullable=True)
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
    music_tracks = Column(String, nullable=True)           # Найденные треки для повторной отправки из кеша


class MusicRequest(Base):
    __tablename__ = "MusicRequest"
    token = Column(String, primary_key=True)
    user_id = Column(String, nullable=False)
    chat_id = Column(String, nullable=False)
    status_message_id = Column(Integer, nullable=True)
    inline_message_id = Column(String, nullable=True)
    tiktok_link = Column(String, nullable=False)
    format_id = Column(String, nullable=True)
    state = Column(String, nullable=False)
    music_tracks = Column(String, nullable=True)
