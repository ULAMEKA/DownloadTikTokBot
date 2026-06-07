"""Единая точка конфигурации бота.

Все переменные окружения читаются здесь — в остальном коде нет разрозненных
вызовов os.getenv. Секреты по-прежнему живут только в .env: тут лишь чтение,
никаких значений в коде не хранится.
"""
import os

from dotenv import load_dotenv

load_dotenv()

# Токен Telegram-бота — обязателен. Падаем сразу с понятным сообщением,
# а не где-то глубже с невнятной ошибкой про None.
_token = os.getenv("TOKEN")
if not _token:
    raise RuntimeError("TOKEN не задан в .env")
TOKEN: str = _token

# ID канала-хранилища (опционально). Отрицательное число для каналов,
# например -1001234567890. Если не задан — кеширование file_id отключено.
STORAGE_CHAT_ID: str | None = os.getenv("STORAGE_CHAT_ID")

# Подключение к БД.
DB_URL = os.getenv("DB_URL", "sqlite+aiosqlite:///./DownloadTT.db")
# Логирование каждого SQL-запроса движком. По умолчанию выключено —
# включается только явно через DB_ECHO=true для отладки.
DB_ECHO = os.getenv("DB_ECHO", "").lower() in ("1", "true", "yes")
