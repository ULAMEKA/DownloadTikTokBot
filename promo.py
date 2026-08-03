"""Общий рекламный текст и заглушка бота."""

from pathlib import Path

AD_DISPLAY_SECONDS = 15
BOT_PROMO_HTML = "💡 Используйте @dwlttbot для загрузки видео"
TELEGRAM_PROFILE = "@polarisim"
TELEGRAM_PROFILE_LINK = '<a href="tg://resolve?domain=polarisim">@polarisim</a>'
AD_PLACEHOLDER_HTML = f"Здесь могла быть ваша реклама. Заказать - {TELEGRAM_PROFILE_LINK}"
AD_PLACEHOLDER_IMAGE = Path(__file__).parent / "assets" / "ad_placeholder.png"
AD_PLACEHOLDER_PHOTO_FILE_ID_PATH = (
    Path(__file__).parent / "assets" / "ad_placeholder.file_id"
)
try:
    AD_PLACEHOLDER_PHOTO_FILE_ID = (
        AD_PLACEHOLDER_PHOTO_FILE_ID_PATH.read_text(encoding="utf-8").strip()
        or None
    )
except FileNotFoundError:
    AD_PLACEHOLDER_PHOTO_FILE_ID = None


def loading_placeholder_caption(position: int | None = None) -> str:
    queue_position = (
        f"\nВаша позиция в очереди: <b>{position}</b>"
        if position is not None
        else ""
    )
    return (
        f"⏳ <b>Скачиваю видео…</b>{queue_position}\n\n"
        f"{AD_PLACEHOLDER_HTML}\n\n{BOT_PROMO_HTML}"
    )
