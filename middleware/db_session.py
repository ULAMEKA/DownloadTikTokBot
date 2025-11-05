from aiogram import BaseMiddleware
from db.session import SessionLocal

class DBSessionMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        async with SessionLocal() as session:
            data["session"] = session
            return await handler(event, data)