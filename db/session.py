from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from config import DB_URL, DB_ECHO

engine = create_async_engine(DB_URL, echo=DB_ECHO)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
