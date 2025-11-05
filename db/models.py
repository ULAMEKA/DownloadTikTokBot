from sqlalchemy import Column, Integer, String
from .base import Base

class DownloadQueue(Base):
    __tablename__ = "BotDownloadQueue"
    id = Column(Integer, primary_key=True)
    user_id = Column(String)
    filename = Column(String)
    tiktok_link = Column(String)
