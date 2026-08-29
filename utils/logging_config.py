import logging
from logging.handlers import RotatingFileHandler


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[
            RotatingFileHandler(
                "bot.log",
                maxBytes=45_000_000,
                backupCount=1,
                encoding="utf-8",
            ),
            logging.StreamHandler(),
        ],
    )
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
