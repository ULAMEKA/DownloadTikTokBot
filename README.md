# DownloadTTBot

[Русский](#русский) · [English](#english)

Telegram-бот на `aiogram` для загрузки видео из TikTok и Instagram Reels.

---

## Русский

### Возможности

- Загружает видео из TikTok и Instagram Reels.
- Собирает фотопосты TikTok в MP4-слайдшоу с оригинальным звуком, если он доступен.
- Работает в личных сообщениях, группах и inline-режиме.
- В личном чате и inline-режиме позволяет убрать описание, скрыть видео под
  спойлером или применить оба варианта.
- Обрабатывает загрузки через постоянную FIFO-очередь в SQLite.
- Не принимает больше двух незавершённых загрузок от одного пользователя.
- Повторно использует Telegram `file_id`, чтобы не скачивать уже известные видео заново.
- Для обычных загрузок распознаёт последовательные треки по 15-секундным
  фрагментам и присылает список найденного. Режим «без описания» и inline-режим
  не дополняет отдельными сообщениями.
- Распознавание использует неофициальный ShazamIO, поэтому работает в режиме
  best effort: часть треков может не найтись, а сервис может быть временно недоступен.

### Требования

- Python 3.10 или новее.
- Токен Telegram-бота от [@BotFather](https://t.me/BotFather).
- Доступ к Telegram, TikTok и Instagram с машины, на которой запущен бот.

FFmpeg отдельно устанавливать не нужно: исполняемый файл предоставляет пакет
`imageio-ffmpeg`.

### Установка

```powershell
git clone https://github.com/imPOLARI5/DownloadTikTokBot.git
cd DownloadTikTokBot
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Для Linux/macOS замените последние две команды:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Создайте файл `.env` в корне проекта:

```dotenv
TOKEN=your_telegram_bot_token

# Необязательно: ID закрытого канала для хранения и кеширования видео.
STORAGE_CHAT_ID=-1001234567890
```

`TOKEN` обязателен. `STORAGE_CHAT_ID` необязателен для обычных сообщений, но
обязателен для доставки незакешированных видео в inline-режиме. Добавьте бота
администратором указанного канала.

После копирования проекта на сервер один раз выполните `.venv/bin/python
upload_ad_placeholder.py`. Скрипт отправит картинку-заглушку в storage-канал и
сохранит Telegram `file_id` локально для inline-режима.

### Настройка BotFather

- Отключите Privacy Mode через `/setprivacy`, если бот должен автоматически
  замечать ссылки в группах.
- Включите inline-режим через `/setinline`, если хотите использовать бота как
  `@dwlttbot <ссылка>`.
- Включите `/setinlinefeedback` на `100%`: бот получает выбранный inline-результат
  через `ChosenInlineResult` и только после этого ставит загрузку в очередь.

### Запуск

Перед первым запуском и после изменений схемы базы данных выполните
идемпотентный скрипт инициализации:

```powershell
.venv\Scripts\python.exe init_db.py
.venv\Scripts\python.exe main.py
```

Linux/macOS:

```bash
.venv/bin/python init_db.py
.venv/bin/python main.py
```

Бот работает через long polling. Остановить его можно сочетанием `Ctrl+C`.

### Использование

- **Личный чат:** отправьте поддерживаемую ссылку и выберите обычную загрузку,
  загрузку без описания или отправку под спойлером.
- **Группа:** отправьте ссылку. Бот создаст сообщение-заглушку и заменит его
  готовым видео.
- **Inline-режим:** в любом чате введите `@dwlttbot <ссылка>` и выберите обычную
  отправку или отправку под спойлером.

Поддерживаются ссылки `tiktok.com`, `vm.tiktok.com`, `vt.tiktok.com` и
`instagram.com/reel`/`instagram.com/reels`.

### Текущие ограничения

- Запускайте только один экземпляр бота: получение заданий из очереди не
  синхронизировано между несколькими процессами.
- Кнопки выбора в личном чате действуют 10 минут и сбрасываются при перезапуске.
- Из одного сообщения обрабатывается только первая поддерживаемая ссылка.

### Как это работает

1. Обработчик проверяет ссылку и добавляет задание в таблицу
   `BotDownloadQueue`.
2. Фоновый воркер последовательно читает FIFO-очередь.
3. Для известного ID медиа бот сначала ищет сохранённый Telegram `file_id`.
4. Новое видео сначала загружается через `yt-dlp`; при ошибке используется
   `gallery-dl`, а фотопост TikTok преобразуется в слайдшоу.
5. После доставки бот сохраняет доступный `file_id`, удаляет временный файл и
   запись очереди.

### Структура проекта

| Путь | Назначение |
|---|---|
| `main.py` | Создание бота, middleware, роутеров и фонового воркера |
| `handlers/link.py` | Личные и групповые ссылки, варианты загрузки |
| `routers/inline_router.py` | Inline-запросы и выбор результата |
| `services/queue.py` | Очередь, загрузка, слайдшоу, доставка и кеш |
| `db/models.py` | Модели очереди и кеша |
| `db/session.py` | Асинхронное подключение к SQLite |
| `middleware/db_session.py` | Передача SQLAlchemy-сессии обработчикам |
| `init_db.py` | Создание и обновление схемы SQLite |
| `caption.py` | Описание видео и ссылка на источник |
| `promo.py` | Имя бота и общий промотекст |
| `utils/links.py` | Поиск и проверка поддерживаемых ссылок |

Во время работы создаются `DownloadTT.db`, каталог `videos/` для временных
файлов и `bot.log`. Автоматизированного набора тестов в репозитории пока нет;
`test.py` — ручной скрипт для проверки одной загрузки.

---

## English

### Features

- Downloads videos from TikTok and Instagram Reels.
- Turns TikTok photo posts into MP4 slideshows with the original audio when
  available.
- Works in private chats, groups, and inline mode.
- In private chats and inline mode, it can omit the description, hide the video
  behind a spoiler, or apply both options.
- Processes downloads through a persistent FIFO queue stored in SQLite.
- Limits each user to two unfinished downloads in the queue.
- Reuses Telegram `file_id` values instead of downloading known videos again.
- For normal downloads, identifies sequential tracks from 15-second chunks and
  sends a list of matches. It does not add a companion message in no-description
  or inline mode.
- Recognition uses the unofficial ShazamIO client on a best-effort basis: some
  tracks may not be found and the service can be temporarily unavailable.

### Requirements

- Python 3.10 or newer.
- A Telegram bot token from [@BotFather](https://t.me/BotFather).
- Network access to Telegram, TikTok, and Instagram.

You do not need a separate FFmpeg installation: `imageio-ffmpeg` provides the
executable.

### Installation

```powershell
git clone https://github.com/imPOLARI5/DownloadTikTokBot.git
cd DownloadTikTokBot
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

On Linux/macOS, replace the last two commands with:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Create a `.env` file in the project root:

```dotenv
TOKEN=your_telegram_bot_token

# Optional: private channel ID used for video storage and caching.
STORAGE_CHAT_ID=-1001234567890
```

`TOKEN` is required. `STORAGE_CHAT_ID` is optional for regular messages, but it
is required to deliver uncached videos in inline mode. Add the bot to that
channel as an administrator.

After copying the project to the server, run `.venv/bin/python
upload_ad_placeholder.py` once. The script sends the placeholder image to the
storage channel and saves its Telegram `file_id` locally for inline mode.

### BotFather setup

- Disable Privacy Mode with `/setprivacy` if the bot should detect links
  automatically in groups.
- Enable inline mode with `/setinline` to use `@dwlttbot <link>`.
- Set `/setinlinefeedback` to `100%`: the bot receives the selected inline result
  through `ChosenInlineResult` and only then queues the download.

### Running

Before the first start and after database schema changes, run the idempotent
initialization script:

```powershell
.venv\Scripts\python.exe init_db.py
.venv\Scripts\python.exe main.py
```

Linux/macOS:

```bash
.venv/bin/python init_db.py
.venv/bin/python main.py
```

The bot uses long polling. Stop it with `Ctrl+C`.

### Usage

- **Private chat:** send a supported link and choose normal delivery, delivery
  without a description, or spoiler delivery.
- **Group:** send a link. The bot creates a placeholder message and replaces it
  with the downloaded video.
- **Inline mode:** type `@dwlttbot <link>` in any chat and choose normal, no
  description, spoiler, or no-description spoiler delivery.

Supported links include `tiktok.com`, `vm.tiktok.com`, `vt.tiktok.com`, and
`instagram.com/reel`/`instagram.com/reels`.

### Current limitations

- Run only one bot instance: queue jobs are not claimed atomically across
  multiple processes.
- Private-chat option buttons expire after 10 minutes and are lost on restart.
- Only the first supported link in each message is processed.

### How it works

1. A handler validates the link and inserts a job into `BotDownloadQueue`.
2. A background worker consumes the persistent FIFO queue one job at a time.
3. For a known media ID, the bot first looks for a cached Telegram `file_id`.
4. New videos are downloaded with `yt-dlp` first; `gallery-dl` is used as a
   fallback, and TikTok photo posts are converted into a slideshow.
5. After delivery, the bot stores an available `file_id`, deletes the temporary
   file, and removes the queue entry.

### Project structure

| Path | Purpose |
|---|---|
| `main.py` | Creates the bot, middleware, routers, and background worker |
| `handlers/link.py` | Private/group links and delivery options |
| `routers/inline_router.py` | Inline queries and result selection |
| `services/queue.py` | Queue processing, downloads, slideshows, delivery, and cache |
| `db/models.py` | Queue and cache models |
| `db/session.py` | Asynchronous SQLite connection |
| `middleware/db_session.py` | Injects SQLAlchemy sessions into handlers |
| `init_db.py` | Creates and updates the SQLite schema |
| `caption.py` | Video captions and source links |
| `promo.py` | Bot username and shared promotional text |
| `utils/links.py` | Supported-link matching and extraction |

At runtime, the project creates `DownloadTT.db`, a `videos/` directory for
temporary files, and `bot.log`. There is currently no automated test suite;
`test.py` is a manual single-download script.
