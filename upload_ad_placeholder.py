"""Upload the inline placeholder photo to Telegram storage once."""

import asyncio
import os

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.types import FSInputFile
from dotenv import load_dotenv

from promo import AD_PLACEHOLDER_HTML, AD_PLACEHOLDER_IMAGE, AD_PLACEHOLDER_PHOTO_FILE_ID_PATH


async def main() -> None:
    load_dotenv()
    token = os.getenv("TOKEN")
    storage_chat_id = os.getenv("STORAGE_CHAT_ID")
    if not token or not storage_chat_id:
        raise SystemExit("TOKEN and STORAGE_CHAT_ID are required")

    bot = Bot(token=token)
    try:
        message = await bot.send_photo(
            chat_id=storage_chat_id,
            photo=FSInputFile(str(AD_PLACEHOLDER_IMAGE)),
            caption=AD_PLACEHOLDER_HTML,
            parse_mode=ParseMode.HTML,
        )
        file_id = message.photo[-1].file_id
        AD_PLACEHOLDER_PHOTO_FILE_ID_PATH.write_text(file_id + "\n", encoding="utf-8")
        print(f"Saved {AD_PLACEHOLDER_PHOTO_FILE_ID_PATH}: {file_id}")
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
