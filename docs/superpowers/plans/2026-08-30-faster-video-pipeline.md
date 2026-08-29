# Faster Video Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ускорить первую и повторную доставку видео, сохранив пятисекундную рекламу для новых загрузок и мгновенно выдавая валидный Telegram-кеш без очереди.

**Architecture:** Один тяжёлый media-worker остаётся ограничителем памяти, но очередь получает event wakeup и приоритет обычных видео. Кеш проверяется локально до `yt-dlp`; cache-miss использует один переносимый info snapshot, совместимый MP4 обходит FFmpeg, private/group пропускают storage pre-upload, а внешние операции выполняются без долгоживущей БД-сессии.

**Tech Stack:** Python 3.10+, aiogram 3.22, SQLAlchemy 2 async + aiosqlite, yt-dlp 2026.8.19, gallery-dl, imageio-ffmpeg/FFmpeg, unittest.

**Spec:** `docs/superpowers/specs/2026-08-30-download-pipeline-performance-design.md`

## Global Constraints

- `AD_DISPLAY_SECONDS` должен быть ровно `5` для cache-miss и не применяться к cache hit.
- Валидный cache hit не создаёт `DownloadQueue`, не вызывает `yt-dlp`, FFmpeg или storage upload.
- Private сохраняет выбор normal/no-description/spoiler; кеш отправляется сразу после выбора.
- Group и inline доставляют кеш сразу после локальной проверки.
- Одновременно выполняется не более одной тяжёлой media-задачи; новые download/FFmpeg workers не добавляются.
- Обычные video jobs имеют приоритет над ещё не начатыми music jobs; FIFO сохраняется внутри каждого класса.
- Production, restart сервиса и миграция live-БД не входят в реализацию без отдельного явного разрешения.
- `services/queue.py` и `test_queue_errors.py` уже содержат пользовательский diff. Его поведение и текст ошибки сохранить; не использовать `git add -A` и не включать исходные hunks в несвязанный commit.
- Все новые тесты используют `unittest`; `test.py` остаётся ручным scratch-скриптом и не входит в discovery suite.
- Не использовать нестабильные wall-clock assertions: проверять вызовы, event/future и детерминированный `monotonic` mock.

## File and Responsibility Map

- Create `utils/logging_config.py`: единая ограниченная настройка логов.
- Create `services/media_cache.py`: чистая cache identity, immutable snapshot, DB lookup/invalidation и узкая классификация invalid `file_id`.
- Modify `utils/links.py`: локальное извлечение TikTok/Instagram media ID из canonical URL.
- Modify `db/models.py`: cache metadata и `DownloadQueue.enqueued_at`.
- Modify `init_db.py`: идемпотентные миграции новых nullable-колонок.
- Modify `services/queue.py`: cache delivery, inspection/info snapshot, scheduler, sessionless worker, MP4 bypass, selective storage и metrics.
- Modify `handlers/link.py`: private/group cache fast path и повторное использование inspection.
- Modify `routers/inline_router.py`: inline cache fast path.
- Modify `main.py`, `db/session.py`, `promo.py`: runtime guardrails.
- Create focused root tests: `test_runtime_config.py`, `test_media_cache.py`, `test_cache_fast_path.py`, `test_media_inspection.py`, `test_queue_worker.py`.
- Modify existing tests only where public return types/migrations change: `test_links.py`, `test_music_recognition.py`, `test_media_preparation.py`, `test_video_quality.py`.
- Modify `README.md`: синхронно описать RU/EN cache fast path и приоритет видео.

---

### Task 1: Runtime guardrails — five-second ad and bounded logs

**Files:**
- Create: `utils/logging_config.py`
- Create: `test_runtime_config.py`
- Modify: `promo.py:5`
- Modify: `db/session.py:3`
- Modify: `main.py:1-57`

**Interfaces:**
- Produces: `utils.logging_config.configure_logging() -> None`
- Produces: `promo.AD_DISPLAY_SECONDS == 5`
- Produces: SQLAlchemy engine with `echo=False`

- [ ] **Step 1: Write failing runtime configuration tests**

```python
# test_runtime_config.py
import unittest
from unittest.mock import patch

import promo
from db.session import engine
from utils.logging_config import configure_logging


class RuntimeConfigTests(unittest.TestCase):
    def test_uncached_ad_delay_is_five_seconds(self):
        self.assertEqual(promo.AD_DISPLAY_SECONDS, 5)

    def test_sql_echo_is_disabled(self):
        self.assertFalse(engine.echo)

    @patch("utils.logging_config.logging.basicConfig")
    def test_bot_log_uses_bounded_rotation(self, basic_config):
        configure_logging()
        handlers = basic_config.call_args.kwargs["handlers"]
        rotating = next(handler for handler in handlers if hasattr(handler, "maxBytes"))
        self.assertEqual(rotating.maxBytes, 45_000_000)
        self.assertEqual(rotating.backupCount, 1)
        self.assertEqual(rotating.encoding.lower().replace("-", ""), "utf8")
```

- [ ] **Step 2: Run the focused test and verify RED**

Run: `\.venv\Scripts\python.exe -m unittest test_runtime_config.py -v`

Expected: import failure for `utils.logging_config` or assertions showing 15 seconds / `echo=True`.

- [ ] **Step 3: Implement the minimal runtime configuration**

```python
# utils/logging_config.py
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
```

Set `AD_DISPLAY_SECONDS = 5`, initialize the engine with
`create_async_engine("sqlite+aiosqlite:///./DownloadTT.db", echo=False)`, import
`configure_logging` in `main.py`, and replace the inline `basicConfig` block with
`configure_logging()`.

- [ ] **Step 4: Run focused and baseline tests GREEN**

Run: `\.venv\Scripts\python.exe -m unittest test_runtime_config.py test_queue_errors.py -v`

Expected: all tests pass, including the pre-existing user error-message regression.

- [ ] **Step 5: Commit only Task 1 files**

```powershell
git add promo.py db/session.py main.py utils/logging_config.py test_runtime_config.py
git commit -m "perf: bound logs and shorten ad delay"
```

### Task 2: Local media identity, cache snapshots, and schema migration

**Files:**
- Create: `services/media_cache.py`
- Create: `test_media_cache.py`
- Modify: `utils/links.py:1-75`
- Modify: `test_links.py:1-45`
- Modify: `db/models.py:15-27`
- Modify: `init_db.py:64-95`
- Modify: `test_music_recognition.py:75-116`

**Interfaces:**
- Produces: `utils.links.cache_media_id(url: str) -> str | None`
- Produces: `services.media_cache.CachedVideo`
- Produces: `media_cache_key(media_id: str) -> str`
- Produces: `find_cached_video(session: AsyncSession, canonical_url: str) -> CachedVideo | None`
- Produces: `find_cached_video_by_id(session: AsyncSession, media_id: str, fallback_url: str) -> CachedVideo | None`
- Produces: `invalidate_cached_video(session: AsyncSession, cache_key: str) -> None`
- Produces: `is_invalid_cached_file_id_error(error: TelegramBadRequest) -> bool`

- [ ] **Step 1: Add failing identity and classifier tests**

```python
# test_media_cache.py
import unittest
from unittest.mock import MagicMock

from aiogram.exceptions import TelegramBadRequest
from services.media_cache import is_invalid_cached_file_id_error, media_cache_key
from utils.links import cache_media_id


class MediaIdentityTests(unittest.TestCase):
    def test_extracts_supported_canonical_ids_without_network(self):
        self.assertEqual(cache_media_id("https://www.tiktok.com/@a/video/123"), "123")
        self.assertEqual(cache_media_id("https://www.tiktok.com/@a/photo/456"), "456")
        self.assertEqual(cache_media_id("https://www.instagram.com/reel/AbC_123/"), "AbC_123")
        self.assertEqual(cache_media_id("https://www.instagram.com/reels/XYZ-9"), "XYZ-9")

    def test_unknown_path_has_no_cache_identity(self):
        self.assertIsNone(cache_media_id("https://www.tiktok.com/@a/live"))

    def test_invalid_file_id_classifier_is_narrow(self):
        invalid = TelegramBadRequest(MagicMock(), "Bad Request: wrong file identifier/HTTP URL specified")
        unrelated = TelegramBadRequest(MagicMock(), "Bad Request: message is not modified")
        self.assertTrue(is_invalid_cached_file_id_error(invalid))
        self.assertFalse(is_invalid_cached_file_id_error(unrelated))

    def test_cache_key_keeps_existing_profile(self):
        self.assertEqual(media_cache_key("123"), "h264-aac-v2:123")
```

Extend `test_music_recognition.py::test_migrates_existing_cache_and_creates_music_request_table` to assert that a legacy `VideoCache` gains both `source_url` and `likes`.

- [ ] **Step 2: Run focused tests and verify RED**

Run: `\.venv\Scripts\python.exe -m unittest test_media_cache.py test_links.py test_music_recognition.py -v`

Expected: missing helpers/columns.

- [ ] **Step 3: Implement identity, immutable snapshot, and migration**

```python
# services/media_cache.py
from dataclasses import dataclass

from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import VideoCache
from utils.links import cache_media_id

MEDIA_CACHE_PROFILE = "h264-aac-v2"


@dataclass(frozen=True)
class CachedVideo:
    cache_key: str
    file_id: str
    description: str | None
    source_url: str | None
    likes: int | None


def media_cache_key(media_id: str) -> str:
    return f"{MEDIA_CACHE_PROFILE}:{media_id}"


def is_invalid_cached_file_id_error(error: TelegramBadRequest) -> bool:
    message = str(error).casefold()
    return any(marker in message for marker in (
        "wrong file identifier",
        "file identifier is invalid",
        "file reference expired",
    ))
```

Implement `cache_media_id()` with `urlsplit(url).path` and anchored regexes for TikTok `/video|photo/` and Instagram `/reel|reels/`. Implement lookup functions with `select(VideoCache)` and return a detached `CachedVideo`; `source_url` falls back to the canonical URL. `invalidate_cached_video()` executes a keyed delete and commits.

Add nullable `source_url = Column(String)` and `likes = Column(Integer)` to `VideoCache`; add two independent `SELECT column` / `ALTER TABLE ADD COLUMN` migrations to `init_db.py`.

- [ ] **Step 4: Run focused tests GREEN**

Run: `\.venv\Scripts\python.exe -m unittest test_media_cache.py test_links.py test_music_recognition.py -v`

Expected: all pass; legacy schema keeps existing rows and gains nullable columns.

- [ ] **Step 5: Commit Task 2**

```powershell
git add services/media_cache.py test_media_cache.py utils/links.py test_links.py db/models.py init_db.py test_music_recognition.py
git commit -m "feat: add local video cache identity"
```

### Task 3: Shared cached delivery with safe invalidation boundary

**Files:**
- Create: `test_cache_fast_path.py`
- Modify: `services/queue.py:1209-1341`
- Modify: `services/media_cache.py`

**Interfaces:**
- Consumes: `CachedVideo`, `find_cached_video`, `invalidate_cached_video`, `is_invalid_cached_file_id_error`
- Produces: `deliver_cached_video(bot: Bot, cached: CachedVideo, *, chat_id: str, fallback_source_url: str, inline_message_id: str | None = None, reply_to_message_id: int | None = None, has_spoiler: bool = False, without_description: bool = False) -> None`
- Produces: `try_deliver_cached_video(bot: Bot, session: AsyncSession, canonical_url: str, *, chat_id: str, inline_message_id: str | None = None, reply_to_message_id: int | None = None, has_spoiler: bool = False, without_description: bool = False) -> bool`; `True` means delivered, `False` means cache miss/invalid and caller must continue its existing queue path.

- [ ] **Step 1: Write failing delivery and invalidation tests**

```python
# test_cache_fast_path.py
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.exceptions import TelegramBadRequest

import services.queue as queue
from services.media_cache import CachedVideo


class CachedDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_cached_delivery_has_no_placeholder_sleep_or_local_file(self):
        cached = CachedVideo("h264-aac-v2:123", "file-id", "desc", None, 7)
        bot = SimpleNamespace(send_video=AsyncMock(return_value=SimpleNamespace(video=None)))
        with patch.object(queue.asyncio, "sleep", new=AsyncMock()) as sleep_mock:
            await queue.deliver_cached_video(
                bot,
                cached,
                chat_id="1",
                fallback_source_url="https://www.tiktok.com/@a/video/123",
            )
        sleep_mock.assert_not_awaited()
        self.assertEqual(bot.send_video.await_args.kwargs["video"], "file-id")

    async def test_invalid_file_id_is_deleted_and_returns_miss(self):
        session = AsyncMock()
        cached = CachedVideo("h264-aac-v2:123", "bad", None, None, None)
        error = TelegramBadRequest(MagicMock(), "Bad Request: wrong file identifier")
        with (
            patch.object(queue, "find_cached_video", AsyncMock(return_value=cached)),
            patch.object(queue, "deliver_cached_video", AsyncMock(side_effect=error)),
            patch.object(queue, "invalidate_cached_video", AsyncMock()) as invalidate,
        ):
            delivered = await queue.try_deliver_cached_video(
                SimpleNamespace(), session, "https://www.tiktok.com/@a/video/123", chat_id="1"
            )
        self.assertFalse(delivered)
        invalidate.assert_awaited_once_with(session, cached.cache_key)
```

Add a negative case where `TelegramBadRequest("message is not modified")` is re-raised and cache invalidation is not called.

- [ ] **Step 2: Run test and verify RED**

Run: `\.venv\Scripts\python.exe -m unittest test_cache_fast_path.py -v`

Expected: cached delivery helpers are missing.

- [ ] **Step 3: Implement the shared delivery boundary**

`deliver_cached_video()` must delegate to `_deliver_video()` with `file_id=cached.file_id`, `video_path=None`, `placeholder_message_id=None`, cached description/likes and `cached.source_url or fallback_source_url`.

```python
async def try_deliver_cached_video(bot, session, canonical_url, *, chat_id, **delivery):
    try:
        cached = await find_cached_video(session, canonical_url)
    except Exception:
        logging.exception("Cache lookup failed: url=%s", canonical_url)
        await session.rollback()
        return False
    if cached is None:
        await session.rollback()
        return False
    await session.rollback()  # release the read transaction before Telegram I/O
    try:
        await deliver_cached_video(
            bot, cached, chat_id=chat_id,
            fallback_source_url=canonical_url, **delivery,
        )
        return True
    except TelegramBadRequest as error:
        if not is_invalid_cached_file_id_error(error):
            raise
        await invalidate_cached_video(session, cached.cache_key)
        return False
```

- [ ] **Step 4: Run focused tests GREEN**

Run: `\.venv\Scripts\python.exe -m unittest test_cache_fast_path.py test_queue_errors.py -v`

Expected: cache errors are narrow; pre-existing user error test still passes.

- [ ] **Step 5: Commit only new/clean files and Task 3 queue hunks**

```powershell
git add services/media_cache.py test_cache_fast_path.py
git add -p services/queue.py
git commit -m "feat: add cached video delivery boundary"
```

When `git add -p` shows the pre-existing Russian error-text hunk, answer `n`; stage only the new cached-delivery functions.

### Task 4: Private, group, and inline cache fast paths

**Files:**
- Modify: `handlers/link.py:73-247,385-414`
- Modify: `routers/inline_router.py:100-194`
- Modify: `test_cache_fast_path.py`

**Interfaces:**
- Consumes: `try_deliver_cached_video(bot, session, canonical_url, *, chat_id, inline_message_id=None, reply_to_message_id=None, has_spoiler=False, without_description=False) -> bool`
- Produces: handler behavior with zero enqueue/quality/storage work on cache hit

- [ ] **Step 1: Add failing handler tests for all delivery modes**

In `test_cache_fast_path.py`, create lightweight callback/message/result stubs and patch the handler module boundary.

```python
for mode, without_description, has_spoiler in (
    ("normal", False, False),
    ("no_description", True, False),
    ("spoiler", False, True),
    ("no_description_spoiler", True, True),
):
    with (
        patch("handlers.link.try_deliver_cached_video", AsyncMock(return_value=True)) as deliver,
        patch("handlers.link.list_video_qualities") as qualities,
        patch("handlers.link.enqueue_download", AsyncMock()) as enqueue,
    ):
        await handle_download_option(callback, session)
    self.assertEqual(deliver.await_args.kwargs["without_description"], without_description)
    self.assertEqual(deliver.await_args.kwargs["has_spoiler"], has_spoiler)
    qualities.assert_not_called()
    enqueue.assert_not_awaited()
```

Add group assertions: `reply_photo`, `enqueue_download`, `hold_placeholder` are not called; `reply_to_message_id` equals the source message ID. Add inline assertions: the bare `raw_inline_message_id` is passed with flags decoded from `result_id`; no `DownloadQueue`, placeholder photo, enqueue, or `hold_inline_placeholder` occurs.

Add three miss-continuation tests by making `try_deliver_cached_video()` return
`False`: private must call inspection and `_start_pending_download`, group must
create `reply_photo` and call `enqueue_download`, and inline must build its
placeholder/serialized queue ID and call `enqueue_download`. These tests cover
both a real miss and the invalid-`file_id` fallback returned by Task 3.

- [ ] **Step 2: Run handler tests RED**

Run: `\.venv\Scripts\python.exe -m unittest test_cache_fast_path.py -v`

Expected: handlers still enter quality/placeholder/queue paths.

- [ ] **Step 3: Insert cache checks before all expensive work**

Private order must be: validate ownership → acknowledge callback → derive mode flags → call `try_deliver_cached_video()` → on `True`, remove pending/menu and return → on `False`, call inspection/quality and existing `_start_pending_download()`.

Group adds `bot: Bot` to the DI signature and checks cache before `reply_photo()`. Inline computes `has_spoiler`/`without_description` from `result.result_id`, checks cache with the bare inline ID, and only then builds the serialized queue prefix for a miss.

Do not catch non-invalid Telegram errors as cache misses; let the existing handler/error path surface them without deleting cache.

- [ ] **Step 4: Run handler and routing regressions GREEN**

Run: `\.venv\Scripts\python.exe -m unittest test_cache_fast_path.py test_links.py test_caption.py -v`

Expected: every cache hit skips queue and downloader boundaries.

- [ ] **Step 5: Commit Task 4**

```powershell
git add handlers/link.py routers/inline_router.py test_cache_fast_path.py
git commit -m "perf: bypass queue for cached videos"
```

### Task 5: Single yt-dlp inspection and atomic info snapshots

**Files:**
- Create: `test_media_inspection.py`
- Modify: `services/queue.py:79-434,1066-1081,1344-1347`
- Modify: `handlers/link.py:33-113,164-247`
- Modify: `test_video_quality.py`

**Interfaces:**
- Produces: immutable `MediaInspection`
- Produces: `inspect_media(url: str) -> MediaInspection`
- Produces: `_inspection_from_snapshot(url: str, info: dict[str, object]) -> MediaInspection`
- Produces: `write_info_snapshot(filename: str, info: dict[str, object]) -> Path`
- Produces: `_load_info_snapshot(filename: str) -> dict[str, object] | None`
- Produces: `_ydl_download_from_info_file(url: str, info_path: Path, outtmpl: str, format_id: str | None) -> tuple[str | None, int | None, int | None, str | None, float | None]`
- Consumes: `PendingDownload.inspection: MediaInspection | None`

- [ ] **Step 1: Write failing inspection/snapshot tests**

```python
# test_media_inspection.py
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import services.queue as queue


class MediaInspectionTests(unittest.TestCase):
    def test_inspection_uses_one_extract_and_sanitizes_info(self):
        ydl = MagicMock()
        ydl.__enter__.return_value = ydl
        ydl.extract_info.return_value = {
            "id": "123", "webpage_url": "https://www.tiktok.com/@a/video/123",
            "like_count": 9, "description": "desc", "formats": [],
        }
        ydl.sanitize_info.return_value = {"id": "123", "webpage_url": "https://www.tiktok.com/@a/video/123"}
        with patch.object(queue.yt_dlp, "YoutubeDL", return_value=ydl):
            inspection = queue.inspect_media("https://www.tiktok.com/@a/video/123")
        ydl.extract_info.assert_called_once_with(
            "https://www.tiktok.com/@a/video/123", download=False
        )
        self.assertEqual(inspection.media_id, "123")
        self.assertEqual(inspection.info["id"], "123")

    def test_snapshot_write_is_atomic_and_loads_only_json_objects(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(queue, "VIDEO_DIR", Path(directory)):
            path = queue.write_info_snapshot("job", {"id": "123"})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"id": "123"})
            self.assertEqual(queue._load_info_snapshot("job"), {"id": "123"})
```

Add a test that corrupt JSON returns `None`, and a private handler test proving one `inspect_media()` result supplies both quality options and the snapshot written at enqueue.

Add deterministic artifact cleanup coverage:

```python
def test_cleanup_removes_snapshot_remux_temp_and_part_directories(self):
    with tempfile.TemporaryDirectory() as directory, patch.object(queue, "VIDEO_DIR", Path(directory)):
        for name in ("job.info.json", "job.atomic.info.tmp", "job.telegram.mp4"):
            (Path(directory) / name).write_bytes(b"x")
        parts = Path(directory) / "job_parts_abc"
        parts.mkdir()
        (parts / "part_001.mp4").write_bytes(b"x")
        queue._remove_download_files("job")
        self.assertFalse(any(Path(directory).iterdir()))
```

- [ ] **Step 2: Run focused tests RED**

Run: `\.venv\Scripts\python.exe -m unittest test_media_inspection.py test_video_quality.py -v`

Expected: missing `MediaInspection` and snapshot helpers.

- [ ] **Step 3: Implement inspection and durable job snapshot**

```python
@dataclass(frozen=True)
class MediaInspection:
    media_id: str | None
    source_url: str
    likes: int | None
    description: str | None
    width: int | None
    height: int | None
    duration: float | None
    qualities: tuple[VideoQuality, ...]
    info: dict[str, object]
```

`inspect_media()` must retain the existing anonymous-then-cookie variants and `_ydl_lock`, call exactly one `extract_info(download=False)`, build application fields, and store `ydl.sanitize_info(info)`. `_inspection_from_snapshot()` must read the same ID/source/likes/description/dimensions/duration/quality fields from a sanitized JSON dict without any network call.

Use `tempfile.mkstemp(dir=VIDEO_DIR, prefix=f"{filename}.", suffix=".info.tmp")`, close the descriptor, write UTF-8 JSON, then `os.replace(temp_path, _info_snapshot_path(filename))` in `try/finally`.

Add `inspection` to `PendingDownload`. Replace `list_video_qualities()` in the private callback with one executor call to `inspect_media()`. `_start_pending_download()` writes `pending.inspection.info` immediately before enqueue and deletes the snapshot if enqueue fails. Large-quality selection keeps the same `PendingDownload.inspection` and uses the selected `format_id`.

Extend `_remove_download_files(filename)` so it unlinks matching
`VIDEO_DIR.glob(f"{filename}.*")` files and recursively removes only matching
`VIDEO_DIR.glob(f"{filename}_parts_*")` directories. Do not broaden either glob
beyond the job-specific filename.

`_ydl_download_from_info_file()` sets the same `outtmpl`, `format`, max size and cookie variants, calls `download_with_info_file()`, rejects non-zero return code, and derives return metadata from `_load_info_snapshot()`.

- [ ] **Step 4: Run inspection and private-path tests GREEN**

Run: `\.venv\Scripts\python.exe -m unittest test_media_inspection.py test_video_quality.py test_cache_fast_path.py -v`

Expected: private cache-miss performs one inspection; cache hit performs none.

- [ ] **Step 5: Commit Task 5 without the pre-existing queue hunk**

```powershell
git add handlers/link.py test_media_inspection.py test_video_quality.py
git add -p services/queue.py
git commit -m "perf: reuse yt-dlp media inspection"
```

### Task 6: Event-driven priority scheduler and queue timestamps

**Files:**
- Create: `test_queue_worker.py`
- Modify: `db/models.py:5-14`
- Modify: `init_db.py:35-64`
- Modify: `services/queue.py:73-75,124-141,1824-1853`

**Interfaces:**
- Produces: `QueueJob` immutable snapshot with `is_music` property
- Produces: `_load_next_job() -> QueueJob | None`
- Produces: `_wait_for_next_job() -> QueueJob`
- Produces: `notify_queue_enqueued() -> None`
- Produces: `QUEUE_WAKEUP_TIMEOUT_SECONDS = 60.0`

- [ ] **Step 1: Add failing priority and lost-wakeup tests**

```python
# test_queue_worker.py
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import services.queue as queue


class QueueWakeupTests(unittest.IsolatedAsyncioTestCase):
    async def test_second_read_closes_enqueue_clear_race(self):
        job = queue.QueueJob(
            1, "1", "1", "job", "https://example", None, None, None, None
        )
        with (
            patch.object(queue, "_load_next_job", AsyncMock(side_effect=[None, job])) as load,
            patch.object(queue._queue_wakeup, "wait", AsyncMock()) as wait,
        ):
            self.assertEqual(await queue._wait_for_next_job(), job)
        self.assertEqual(load.await_count, 2)
        wait.assert_not_awaited()

    async def test_failed_enqueue_does_not_signal(self):
        result = MagicMock()
        result.scalar_one.return_value = queue.MAX_ACTIVE_DOWNLOADS_PER_USER
        session = AsyncMock()
        session.execute.return_value = result
        queue._queue_wakeup.clear()
        accepted = await queue.enqueue_download(session, MagicMock(), "user-1")
        self.assertFalse(accepted)
        self.assertFalse(queue._queue_wakeup.is_set())
        session.commit.assert_not_awaited()
```

Add SQLite-backed selection tests inserting an older `music:<token>` job plus newer video jobs and asserting video first, FIFO by `id` within video, then FIFO within music. Do not assert elapsed milliseconds.

- [ ] **Step 2: Run scheduler tests RED**

Run: `\.venv\Scripts\python.exe -m unittest test_queue_worker.py -v`

Expected: missing snapshot/event helpers and FIFO priority.

- [ ] **Step 3: Implement timestamp, priority SELECT, and race-free Event wait**

Add nullable `enqueued_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))` plus an idempotent migration. Define:

```python
QUEUE_WAKEUP_TIMEOUT_SECONDS = 60.0
_queue_wakeup = asyncio.Event()


@dataclass(frozen=True)
class QueueJob:
    id: int
    chat_id: str
    user_id: str | None
    filename: str
    tiktok_link: str
    format_id: str | None
    reply_to_message_id: int | None
    inline_message_id: str | None
    enqueued_at: datetime | None

    @property
    def is_music(self) -> bool:
        return bool(self.inline_message_id and self.inline_message_id.startswith(MUSIC_JOB_PREFIX))
```

`_load_next_job()` uses a short `SessionLocal()` and orders by SQLAlchemy `case((DownloadQueue.inline_message_id.startswith(MUSIC_JOB_PREFIX), 1), else_=0), DownloadQueue.id`. `_wait_for_next_job()` performs load → clear → second load → `wait_for(Event.wait(), 60)` and loops after timeout.

Call `_queue_wakeup.set()` only after successful enqueue commit. Extract the current per-item processing block into `_process_queue_job_legacy(bot, job)` for this task; it may temporarily reopen a session and fetch `DownloadQueue` by ID. `process_queue()` becomes a small loop around `_wait_for_next_job()` and the legacy adapter. Task 7 removes that long session.

- [ ] **Step 4: Run scheduler and enqueue tests GREEN**

Run: `\.venv\Scripts\python.exe -m unittest test_queue_worker.py test_queue_errors.py -v`

Expected: enqueue wakes immediately, priority is deterministic, user error test remains green.

- [ ] **Step 5: Commit scheduler task**

```powershell
git add db/models.py init_db.py test_queue_worker.py
git add -p services/queue.py
git commit -m "perf: wake and prioritize the download queue"
```

### Task 7: Sessionless video worker and one-extractor worker path

**Files:**
- Modify: `services/queue.py:1610-1822,1824-2161`
- Modify: `services/media_cache.py`
- Modify: `test_queue_worker.py`
- Modify: `test_media_inspection.py`

**Interfaces:**
- Consumes: `QueueJob`, `MediaInspection`, info snapshots and cache helpers
- Produces: `_process_video_job(bot: Bot, job: QueueJob) -> None`
- Produces: `_discard_job(job_id: int) -> None`
- Produces: `_finish_video_job(job_id: int, cache_update: VideoCacheUpdate | None) -> None`
- Produces: `VideoCacheUpdate` frozen dataclass
- Changes: `prepare_music_caption_link(bot: Bot, tiktok_link: str, format_id: str | None) -> tuple[str | None, str | None]` owns its short DB transaction; no session argument

- [ ] **Step 1: Add failing short-session and call-count tests**

```python
class ShortSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_download_starts_after_load_session_exits(self):
        session_exited = False

        async def download(*args):
            self.assertTrue(session_exited)
            raise queue.MediaPreparationError("stop")

        # SessionLocal context marks session_exited in __aexit__.
        # _process_video_job receives a detached QueueJob and the patched downloader.

    async def test_fresh_snapshot_skips_resolve_and_second_extract(self):
        job = queue.QueueJob(
            1, "1", "1", "job", "https://www.tiktok.com/@a/video/123",
            None, None, None, None,
        )
        inspection = queue.MediaInspection(
            "123", job.tiktok_link, 9, "desc", 720, 1280, 10.0, (),
            {"id": "123", "webpage_url": job.tiktok_link},
        )
        with (
            patch.object(queue, "_inspection_from_snapshot", return_value=inspection),
            patch.object(queue, "inspect_media") as inspect,
            patch.object(
                queue,
                "_ydl_download_from_info_file",
                return_value=("123", 720, 1280, "desc", 10.0),
            ) as download,
            patch.object(
                queue,
                "_prepare_telegram_video",
                side_effect=queue.MediaPreparationError("stop after download"),
            ),
            patch.object(queue, "_discard_job", AsyncMock()),
            patch.object(queue, "_show_download_error", AsyncMock()),
            patch.object(queue, "_remove_download_files"),
        ):
            await queue._process_video_job(MagicMock(), job)
        inspect.assert_not_called()
        download.assert_called_once()
```

Add a test where no snapshot exists: `inspect_media` is called once, snapshot is written once, cache is checked using `inspection.media_id`, and download consumes that snapshot without `_resolve_media`.

- [ ] **Step 2: Run worker tests RED**

Run: `\.venv\Scripts\python.exe -m unittest test_queue_worker.py test_media_inspection.py test_queue_errors.py -v`

Expected: legacy adapter still holds a session and repeats metadata extraction.

- [ ] **Step 3: Split database lifecycle from external processing**

```python
@dataclass(frozen=True)
class VideoCacheUpdate:
    cache_key: str
    file_id: str
    storage_message_id: int | None
    description: str | None
    source_url: str
    likes: int | None


async def _discard_job(job_id: int) -> None:
    async with SessionLocal() as session:
        item = await session.get(DownloadQueue, job_id)
        if item is not None:
            await session.delete(item)
            await session.commit()
```

`_finish_video_job()` opens one short session, upserts `VideoCache` fields including `source_url`/`likes`, deletes the queue row, and commits once. If commit fails, rollback/log and leave the row recoverable.

`_process_video_job()` receives only `QueueJob`; no ORM object or `AsyncSession` crosses into `_resolve/inspect`, executor, FFmpeg, storage, Telegram, ad wait, or cleanup. Preserve the current error messages and cleanup branches, including the user's `❌ Не удалось загрузить видео.` wording.

Worker metadata order:

1. Try local cache by canonical job URL in a short session.
2. Load valid info snapshot; if absent, call `inspect_media()` once and atomically write it.
3. Recheck cache by `inspection.media_id` in a short session.
4. On miss call `_ydl_download_from_info_file()`; on corrupt/stale failure use its URL fallback, then existing gallery-dl fallback.
5. Never call `_resolve_media()` on the successful path.

Refactor `create_music_deep_link`/`prepare_music_caption_link` so username lookup occurs first, then a new short session inserts `MusicRequest`; update callers to remove the session argument.

- [ ] **Step 4: Run worker/error tests GREEN**

Run: `\.venv\Scripts\python.exe -m unittest test_queue_worker.py test_media_inspection.py test_queue_errors.py test_music_recognition.py -v`

Expected: external boundaries observe no open worker session and call counts prove one extractor phase.

- [ ] **Step 5: Commit only Task 7 hunks**

```powershell
git add services/media_cache.py test_queue_worker.py test_media_inspection.py
git add -p services/queue.py
git commit -m "refactor: release database during media work"
```

### Task 8: Compatible MP4 bypass, forced normalization, and selective storage

**Files:**
- Modify: `services/queue.py:86-98,887-943,1209-1341,1930-2073`
- Modify: `test_media_preparation.py`
- Modify: `test_queue_worker.py`

**Interfaces:**
- Produces: `PreparedVideo(path: Path, probe: MediaProbe, bypassed_ffmpeg: bool)`
- Produces: `_can_bypass_telegram_normalization(path: Path, probe: MediaProbe) -> bool`
- Changes: `_prepare_telegram_video(path: Path, *, force_normalize: bool = False) -> PreparedVideo`
- Produces: `_upload_to_storage(bot: Bot, video_path: Path, width: int | None, height: int | None) -> tuple[str | None, int | None]`

- [ ] **Step 1: Add failing media fast-path and routing tests**

Extend `test_media_preparation.py` with a generated H.264/AAC/yuv420p MP4 and assert:

```python
prepared = _prepare_telegram_video(source)
self.assertEqual(prepared.path, source)
self.assertTrue(prepared.bypassed_ffmpeg)
self.assertFalse(source.with_name(f"{source.stem}.telegram.mp4").exists())
```

Update the HEVC test to inspect `prepared.path`, `prepared.probe`, and `bypassed_ffmpeg is False`. Add predicate unit cases for wrong suffix, audio codec, rotation and SAR.

In `test_queue_worker.py`, add async tests proving private/group with configured `STORAGE_CHAT_ID` call local delivery directly and never call `_upload_to_storage`; first inline miss calls `_upload_to_storage` exactly once and edits by returned `file_id`.

- [ ] **Step 2: Run media tests RED**

Run: `\.venv\Scripts\python.exe -m unittest test_media_preparation.py test_queue_worker.py -v`

Expected: tuple return has no bypass flag and storage is still unconditional.

- [ ] **Step 3: Implement conservative bypass and selective storage**

```python
@dataclass(frozen=True)
class PreparedVideo:
    path: Path
    probe: MediaProbe
    bypassed_ffmpeg: bool


def _can_bypass_telegram_normalization(path: Path, probe: MediaProbe) -> bool:
    return (
        path.suffix.lower() == ".mp4"
        and probe.has_video and probe.has_audio
        and _is_h264(probe.video_codec)
        and probe.pixel_format in (None, "yuv420p")
        and probe.audio_codec == "aac"
        and probe.sample_aspect_ratio in (None, "1:1")
        and not probe.rotation
    )
```

Return early unless `force_normalize=True`. Otherwise run the current FFmpeg block unchanged and return `PreparedVideo(final_path, prepared_probe, False)`.

For local-file `TelegramBadRequest` after a bypass, call `_prepare_telegram_video(path, force_normalize=True)` once, recompute split because size may change, and retry delivery once. Do not retry `TelegramForbiddenError`, `TelegramRetryAfter`, cached `file_id`, or an already normalized file.

Call `_upload_to_storage()` only when a real `inline_message_id` exists. Private/group pass the local `video_path` to `_deliver_video`, use its returned `file_id`, and persist `storage_message_id=None`.

- [ ] **Step 4: Run media, delivery, and error tests GREEN**

Run: `\.venv\Scripts\python.exe -m unittest test_media_preparation.py test_queue_worker.py test_queue_errors.py -v`

Expected: safe MP4 starts no remux process; inline performs one storage upload; direct paths perform none.

- [ ] **Step 5: Commit Task 8 queue hunks separately**

```powershell
git add test_media_preparation.py test_queue_worker.py
git add -p services/queue.py
git commit -m "perf: skip redundant media and storage passes"
```

### Task 9: Sessionless music jobs and structured stage metrics

**Files:**
- Modify: `services/queue.py:1360-1822,1824-2161`
- Modify: `test_music_recognition.py`
- Modify: `test_queue_worker.py`

**Interfaces:**
- Produces: immutable `MusicRequestSnapshot`
- Produces: `_load_music_request(token: str) -> MusicRequestSnapshot | None`
- Produces: `_finish_music_job(job_id: int, token: str, music_tracks: str | None, retry: bool) -> MusicRequestSnapshot | None`
- Changes: `_process_music_job(bot: Bot, job: QueueJob, token: str) -> None` owns no long-lived session
- Produces: `JobStageMetrics.stage(name: str)` context manager and `log_summary(*, cache_status: str, ffmpeg_bypassed: bool | None, queue_wait_ms: int | None) -> None`

- [ ] **Step 1: Add failing music-session and deterministic metric tests**

```python
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import services.queue as queue


class MusicSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_recognition_runs_after_request_session_is_closed(self):
        session_closed = asyncio.Event()
        request = SimpleNamespace(
            token="a" * 32,
            user_id="1",
            chat_id="1",
            status_message_id=7,
            inline_message_id=None,
            tiktok_link="https://www.tiktok.com/@a/video/123",
            format_id=None,
            state="queued",
            music_tracks=None,
        )
        session = AsyncMock()
        session.get.return_value = request
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=session)

        async def close_session(*_args):
            session_closed.set()

        context.__aexit__ = AsyncMock(side_effect=close_session)

        async def recognize(*_args):
            self.assertTrue(session_closed.is_set())
            return ""

        job = queue.QueueJob(
            1, "1", "1", "music_job", request.tiktok_link,
            None, None, f"music:{request.token}", None,
        )
        with (
            patch.object(queue, "SessionLocal", return_value=context),
            patch.object(queue, "_recognize_music_from_source", side_effect=recognize),
            patch.object(queue, "_finish_music_job", AsyncMock(return_value=request)),
            patch.object(queue, "show_music_request_result", AsyncMock()),
        ):
            await queue._process_music_job(MagicMock(), job, request.token)

    def test_stage_metrics_use_monotonic_deltas(self):
        with patch("services.queue.time.monotonic", side_effect=[10.0, 10.2, 10.5]):
            metrics = queue.JobStageMetrics(job_id=7)
            with metrics.stage("download"):
                pass
            self.assertEqual(metrics.durations_ms["download"], 300)
```

Add an `assertLogs` case proving the summary includes numeric `queue_wait_ms`, `inspect_ms`, `download_ms`, `prepare_ms`, `storage_ms`, `delivery_ms`, `total_ms`, cache hit/miss and FFmpeg bypass, but excludes URL query strings, cookies and headers.

- [ ] **Step 2: Run music/metric tests RED**

Run: `\.venv\Scripts\python.exe -m unittest test_music_recognition.py test_queue_worker.py -v`

Expected: music still carries ORM session through recognition and metrics type is absent.

- [ ] **Step 3: Implement snapshots, short finish transaction, and metrics**

`MusicRequestSnapshot` contains token, user/chat IDs, status/inline message IDs, source URL, format ID, state and tracks. `_load_music_request()` copies it inside a short session. `_finish_music_job()` opens a new session, applies `ready/done`, deletes the queue row, commits, and returns a fresh snapshot for UI rendering.

`_process_music_job()` performs download, FFmpeg chunks and Shazam only from snapshot values. UI helpers continue accepting attribute-compatible snapshot objects. On terminal failure, use one short transaction to restore `ready` and remove the job.

```python
@dataclass
class JobStageMetrics:
    job_id: int
    started_at: float = field(default_factory=time.monotonic)
    durations_ms: dict[str, int] = field(default_factory=dict)

    @contextmanager
    def stage(self, name: str):
        started = time.monotonic()
        try:
            yield
        finally:
            self.durations_ms[name] = round((time.monotonic() - started) * 1000)
```

Instrument both video and music processors at the exact external boundaries. Compute queue wait from nullable `enqueued_at`; omit that field for legacy rows. Emit one INFO summary per completed/failed job with IDs and numeric durations only.

- [ ] **Step 4: Run focused runtime tests GREEN**

Run: `\.venv\Scripts\python.exe -m unittest test_music_recognition.py test_queue_worker.py test_queue_errors.py -v`

Expected: music recognition sees no open session, video priority remains intact, metric assertions pass.

- [ ] **Step 5: Commit Task 9**

```powershell
git add test_music_recognition.py test_queue_worker.py
git add -p services/queue.py
git commit -m "perf: isolate music jobs and log stage timing"
```

### Task 10: Bilingual behavior docs and complete verification

**Files:**
- Modify: `README.md` Russian and English feature/how-it-works/current-limitations sections
- Test: all `test_*.py` except manual `test.py`

**Interfaces:**
- Consumes: all previous tasks
- Produces: synchronized RU/EN description of immediate cache hits, five-second cache-miss advertising, video-over-music priority, and single heavy worker

- [ ] **Step 1: Update RU and EN documentation with matched claims**

Russian wording must state:

```markdown
- Кешированные видео отправляются по Telegram `file_id` сразу после выбора режима
  и не попадают в очередь; для новых загрузок рекламная заглушка показывается
  минимум пять секунд.
- Обычные видео имеют приоритет над ещё не начатым распознаванием музыки, при этом
  внутри каждого типа сохраняется FIFO и одновременно работает одна тяжёлая задача.
```

Mirror the same facts in English without claiming parallel downloads or production deployment. Update the current FIFO description so it no longer claims a single global FIFO across video and music.

- [ ] **Step 2: Run the complete automated suite**

Run: `\.venv\Scripts\python.exe -m unittest discover -p "test_*.py" -v`

Expected: all automated tests pass; `test.py` is not discovered because it does not match `test_*.py`.

- [ ] **Step 3: Run compile and repository checks**

```powershell
.\.venv\Scripts\python.exe -m compileall -q main.py init_db.py caption.py promo.py db handlers middleware routers services utils
git diff --check
git status --short
```

Expected: compile exit 0; diff check exit 0. Status may still show the two original user changes, but no unexpected generated DB, log, media, temp snapshot or cache artifacts.

- [ ] **Step 4: Verify the original performance contracts by call counts**

Run: `\.venv\Scripts\python.exe -m unittest test_cache_fast_path.py test_media_inspection.py test_media_preparation.py test_queue_worker.py -v`

Expected assertions:

- cache hit: zero enqueue/extractor/FFmpeg/storage calls and no `asyncio.sleep`;
- cache miss: five-second configured deadline;
- fresh snapshot: one extractor phase;
- safe MP4: FFmpeg bypass;
- private/group: no storage upload;
- inline miss: one storage upload;
- external video/music work begins after worker sessions close;
- waiting video is selected before waiting music.

- [ ] **Step 5: Review the final diff without staging user-owned baseline hunks**

```powershell
git diff -- services/queue.py test_queue_errors.py
git diff --stat
git log --oneline -12
```

Confirm the user's existing error text and its regression test remain present. If any implementation hunk is inseparable from the pre-existing queue hunk, leave it unstaged and report it rather than silently committing user work.

- [ ] **Step 6: Commit documentation only**

```powershell
git add README.md
git commit -m "docs: describe faster media delivery"
```

- [ ] **Step 7: Stop before production**

Report local test/compile evidence, changed files, commits, remaining user-owned diff, and measured call-count improvements. Do not upload, migrate live SQLite, restart `tgbot.service`, push, or create a PR without a new explicit user instruction.
