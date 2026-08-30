# H.264-first Fast Media Delivery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Устранить многоминутные HEVC→H.264 transcode задержки, выбирая только быстро доставляемые H.264/AAC источники и сохраняя TikTok photo galleries с аудиотреком.

**Architecture:** Один `yt-dlp` snapshot вычисляет exact H.264 selector, который worker передаёт в info-file download без повторного page extraction. Обычные source videos допускают только direct delivery, stream-copy remux или audio-only AAC transcode; HEVC/VP9/AV1 и H.264, требующий video filter, завершаются понятной ошибкой. Photo galleries остаются отдельным small/slideshow encode path.

**Tech Stack:** Python 3.10+, aiogram 3.22, SQLAlchemy 2 + aiosqlite, yt-dlp 2026.8.19, gallery-dl, FFmpeg via imageio-ffmpeg, unittest.

**Spec:** `docs/superpowers/specs/2026-08-30-h264-first-fast-media-design.md`

## Global Constraints

- Обычный source video никогда не выполняет `-c:v libx264`, `-vf`, scale, FPS conversion, rotation filter или video CRF.
- Допускаются только H.264/AVC video с обязательным audio; preparation — direct, `-c:v copy -c:a copy` или `-c:v copy -c:a aac`.
- H.265/HEVC, VP9, AV1 или H.264, которому нужен video filter, не отправляются и не перекодируются; после H.264-oriented fallback они получают exact terminal error из спецификации.
- H.264 selector rank: combined H.264/AAC MP4 → H.264 + AAC → H.264 + другой audio; затем FPS → площадь → bitrate → filesize.
- Сохраняются source FPS и обязательный звук; `MissingAudioError` UX не меняется.
- Старые HD cache hits остаются допустимыми и мгновенными: `MEDIA_CACHE_PROFILE` остаётся `h264-aac-v2`.
- New normal jobs не показывают quality menu и создаются с `format_id=None`; legacy `format_id` не может навязать HEVC.
- Только photo slideshow вправе кодировать video: `480×854`, `libx264`, `-preset ultrafast`, `-crf 26`, AAC `128k`.
- Не добавлять downloader/FFmpeg concurrency; сохранять one-heavy-worker, sessionless внешнюю работу, cache/music/inline/storage/split contracts.
- Не запускать `test.py` или bare `unittest discover`; полный suite только `discover -p "test_*.py"`.
- Runtime production deploy, restart, push и PR не входят в этот план без отдельного явного разрешения.

## File and Responsibility Map

- Modify `services/queue.py`: selector, inspection metadata, fast-only preparation, gallery policy, worker propagation, telemetry and slideshow settings.
- Modify `handlers/link.py`: убрать private quality menu и всегда enqueue normal job без ручного format selector.
- Modify `test_video_quality.py`: H.264 selector, candidate filter and remove quality-menu expectations.
- Modify `test_media_inspection.py`: snapshot selector propagation and H.264-only stale/fallback behavior.
- Modify `test_media_preparation.py`: direct/remux/audio-only/reject policy and slideshow command.
- Modify `test_queue_worker.py`: legacy format containment, worker terminal error, no forced video normalize, telemetry.
- Modify `test_queue_errors.py`: exact no-fast-H.264 error with source link while preserving missing-audio behavior.
- Modify `test_cache_fast_path.py`, `test_music_recognition.py`, `test_enqueue_lifecycle.py` only for affected API fixtures and regressions.
- Modify `README.md`: matched RU/EN H.264-first policy, no quality menu, explicit no-fast error and photo-gallery exception.

---

### Task 1: Exact H.264 selector in the inspection snapshot

**Files:**
- Modify: `services/queue.py:97-112,422-566,695-779`
- Modify: `test_video_quality.py:22-248`
- Modify: `test_media_inspection.py:16-157`

**Interfaces:**
- Produces: `class NoFastH264FormatError(MediaPreparationError)`
- Produces: `select_fast_h264_format(formats: object) -> str | None`
- Changes: `MediaInspection.fast_format_id: str | None = None`
- Produces: `require_fast_h264_format(inspection: MediaInspection) -> str`
- Consumes later: worker passes `inspection.fast_format_id` to `_ydl_download_from_info_file()`.

- [ ] **Step 1: Write failing selector and snapshot tests**

```python
def test_fast_h264_selector_prefers_combined_aac_before_separate_streams(self):
    formats = [
        {"format_id": "hevc", "vcodec": "h265", "acodec": "aac", "fps": 60,
         "width": 1080, "height": 1920, "tbr": 1200, "filesize": 20_000_000},
        {"format_id": "h264-aac", "vcodec": "h264", "acodec": "aac", "fps": 30,
         "width": 576, "height": 1024, "tbr": 381, "filesize": 8_400_000},
        {"format_id": "h264-video", "vcodec": "h264", "acodec": "none", "fps": 60,
         "width": 720, "height": 1280, "tbr": 600, "filesize": 9_000_000},
        {"format_id": "aac", "vcodec": "none", "acodec": "aac", "abr": 128,
         "filesize": 1_000_000},
    ]
    self.assertEqual(select_fast_h264_format(formats), "h264-aac")

def test_fast_h264_selector_uses_best_aac_when_no_combined_mp4_exists(self):
    formats = [
        {"format_id": "video", "vcodec": "avc1.640028", "acodec": "none", "fps": 60,
         "width": 720, "height": 1280, "tbr": 700, "filesize": 9_000_000},
        {"format_id": "aac-low", "vcodec": "none", "acodec": "aac", "abr": 64,
         "filesize": 400_000},
        {"format_id": "aac-high", "vcodec": "none", "acodec": "aac", "abr": 128,
         "filesize": 800_000},
    ]
    self.assertEqual(select_fast_h264_format(formats), "video+aac-high")

def test_fast_h264_selector_returns_none_for_hevc_only(self):
    self.assertIsNone(select_fast_h264_format([
        {"format_id": "hevc", "vcodec": "h265", "acodec": "aac"},
    ]))
```

Add a snapshot test with literal sanitized info and assert
`inspection.fast_format_id == "h264-aac"`. Add an info-file downloader test
that asserts `YoutubeDL` receives exactly that selector in `opts["format"]` and
does not call `extract_info`.

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `\.venv\Scripts\python.exe -m unittest test_video_quality.py test_media_inspection.py -v`

Expected: import/attribute failures for the new selector and inspection field.

- [ ] **Step 3: Implement the selector and snapshot field**

```python
def select_fast_h264_format(formats: object) -> str | None:
    if not isinstance(formats, list):
        return None
    typed = [item for item in formats if isinstance(item, dict)]
    audio = [
        item for item in typed
        if not _has_video(item) and _has_audio(item)
        and isinstance(item.get("format_id"), str)
    ]
    best_aac = max(
        (item for item in audio if str(item.get("acodec", "")).lower().startswith("aac")),
        key=_audio_quality_key,
        default=None,
    )
    best_audio = max(audio, key=_audio_quality_key, default=None)
    classes = (
        lambda item: _is_h264(item.get("vcodec")) and _has_audio(item)
        and str(item.get("acodec", "")).lower().startswith("aac"),
        lambda item: _is_h264(item.get("vcodec")) and not _has_audio(item) and best_aac,
        lambda item: _is_h264(item.get("vcodec")) and not _has_audio(item) and best_audio,
    )
    for allowed in classes:
        candidates = [item for item in typed if _has_video(item) and allowed(item)]
        if not candidates:
            continue
        selected = max(candidates, key=_fast_h264_rank)
        video_id = selected.get("format_id")
        if not isinstance(video_id, str):
            continue
        if _has_audio(selected):
            return video_id
        audio_item = best_aac or best_audio
        audio_id = audio_item.get("format_id") if audio_item else None
        if isinstance(audio_id, str):
            return f"{video_id}+{audio_id}"
    return None
```

Define `_fast_h264_rank(item)` as `(fps, width * height, tbr, filesize)` using
the existing conversion helpers. Add `fast_format_id` as the final,
defaulted field in `MediaInspection`; populate it in `_inspection_from_snapshot`.
`require_fast_h264_format()` returns the field or raises
`NoFastH264FormatError`.

Keep `list_video_qualities()` only until Task 4 removes its last consumer; do
not use it for new normal jobs.

- [ ] **Step 4: Make info-file downloads accept only explicit fast selectors**

Change `_ydl_download_from_info_file()` and `_ydl_download()` so the normal
video path receives an explicit selector obtained from `require_fast_h264_format`.
Do not fall back to `DEFAULT_FORMAT` for a normal job. A caller with no selector
must receive `NoFastH264FormatError` before `YoutubeDL` is created.

- [ ] **Step 5: Run focused tests GREEN**

Run: `\.venv\Scripts\python.exe -m unittest test_video_quality.py test_media_inspection.py -v`

Expected: H.264/AAC selection, separate AAC selection, HEVC rejection and
single-extraction assertions pass.

- [ ] **Step 6: Commit Task 1**

```powershell
git add services/queue.py test_video_quality.py test_media_inspection.py
git commit -m "feat: select fast H264 media formats"
```

### Task 2: Fast-only source preparation and H.264 gallery policy

**Files:**
- Modify: `services/queue.py:405-418,983-1204,1232-1297,1330-1444`
- Modify: `test_media_preparation.py:17-174`
- Modify: `test_video_quality.py:99-201`
- Modify: `test_queue_worker.py:899-1159`

**Interfaces:**
- Consumes: `NoFastH264FormatError`, `select_fast_h264_format`
- Changes: `PreparedVideo.preparation_mode: str`
- Changes: `_prepare_telegram_video(path: Path) -> PreparedVideo`
- Produces: `_is_fast_h264_direct(path: Path, probe: MediaProbe) -> bool`
- Produces: `_fast_remux_h264(path: Path, probe: MediaProbe) -> PreparedVideo`

- [ ] **Step 1: Write failing direct/remux/reject tests**

```python
def test_h264_aac_mkv_remuxes_without_video_encoder(self):
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "source.mkv"
        _make_h264_aac_mkv(source)
        prepared = _prepare_telegram_video(source)
        self.assertEqual(prepared.preparation_mode, "remux_copy")
        self.assertEqual(prepared.probe.video_codec, "h264")
        self.assertEqual(prepared.probe.audio_codec, "aac")

def test_h264_mp3_remuxes_with_audio_only_conversion(self):
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "source.mp4"
        _make_h264_mp3_mp4(source)
        prepared = _prepare_telegram_video(source)
        self.assertEqual(prepared.preparation_mode, "audio_to_aac")
        self.assertEqual(prepared.probe.video_codec, "h264")
        self.assertEqual(prepared.probe.audio_codec, "aac")

def test_hevc_source_is_rejected_without_video_transcode(self):
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "source.mp4"
        _make_hevc_aac_mp4(source)
        with self.assertRaises(NoFastH264FormatError):
            _prepare_telegram_video(source)
```

Patch `subprocess.run` around the remux command and assert the source-video
commands contain `-c:v copy` and never contain `libx264`, `-vf`, `scale`,
`-crf` or `-fps_mode`.

- [ ] **Step 2: Run the preparation tests and verify RED**

Run: `\.venv\Scripts\python.exe -m unittest test_media_preparation.py -v`

Expected: old HEVC conversion test passes for the wrong reason or new tests fail
because preparation still invokes the video encoder.

- [ ] **Step 3: Replace source-video normalization with fast preparation**

```python
def _prepare_telegram_video(path: Path) -> PreparedVideo:
    probe = _probe_media(path)
    if not probe.has_video:
        raise MediaPreparationError("В файле отсутствует видеодорожка")
    if not probe.has_audio:
        raise MissingAudioError
    if not _is_h264(probe.video_codec):
        raise NoFastH264FormatError
    if probe.pixel_format not in (None, "yuv420p"):
        raise NoFastH264FormatError
    if probe.sample_aspect_ratio not in (None, "1:1") or probe.rotation:
        raise NoFastH264FormatError
    if path.suffix.lower() == ".mp4" and probe.audio_codec == "aac":
        return PreparedVideo(path, probe, True, "direct")
    return _fast_remux_h264(path, probe)
```

`_fast_remux_h264()` writes `<stem>.telegram.mp4`, maps one video and one audio
track, always uses `-c:v copy`, uses `-c:a copy` for AAC or `-c:a aac -b:a 192k`
otherwise, adds `-movflags +faststart`, probes the result and atomically replaces
the source. It returns `preparation_mode="remux_copy"` or `"audio_to_aac"`.

Delete `VIDEO_TRANSCODE_CRF`, `force_normalize`, the source-video `libx264`
branch and forced-normalization retry after `TelegramBadRequest`. A delivery
error after direct/remux source video follows existing terminal error UX; it
does not start another video encode.

- [ ] **Step 4: Restrict gallery paths to H.264**

Filter `_tiktok_video_candidates()` to `_is_h264(codec)` before sorting. Keep
external audio mux only after the downloaded candidate probes as H.264. In the
generic gallery CLI path, probe before moving the file and raise
`NoFastH264FormatError` for non-H.264 video. Do not call source-video
normalization afterwards.

- [ ] **Step 5: Run fast preparation and gallery tests GREEN**

Run: `\.venv\Scripts\python.exe -m unittest test_media_preparation.py test_video_quality.py test_queue_worker.py -v`

Expected: only direct/remux/audio-only source paths remain; HEVC is rejected
without `libx264` or forced retry.

- [ ] **Step 6: Commit Task 2**

```powershell
git add services/queue.py test_media_preparation.py test_video_quality.py test_queue_worker.py
git commit -m "perf: avoid source video transcoding"
```

### Task 3: Worker propagation, fallback error, and legacy format containment

**Files:**
- Modify: `services/queue.py:1422-1444,2094-2230,2484-2969`
- Modify: `test_media_inspection.py:208-350`
- Modify: `test_queue_worker.py:425-667,668-898`
- Modify: `test_queue_errors.py:58-139`
- Modify: `test_music_recognition.py:31-287`

**Interfaces:**
- Consumes: `MediaInspection.fast_format_id`, `require_fast_h264_format`,
  `NoFastH264FormatError`, `PreparedVideo.preparation_mode`
- Produces: terminal exact no-fast-H.264 error path through `_show_download_error`
- Changes: `JobStageMetrics` exposes allowlisted `preparation_mode`

- [ ] **Step 1: Write failing worker and error UX tests**

```python
async def test_legacy_hevc_format_id_cannot_force_slow_transcode(self):
    job = QueueJob(
        id=7,
        chat_id="1",
        user_id="1",
        filename="legacy",
        tiktok_link="https://www.tiktok.com/@a/video/123",
        format_id="hevc-id",
        reply_to_message_id=None,
        inline_message_id=None,
        enqueued_at=None,
    )
    inspection = _inspection_with_fast_format("h264-aac")
    with patch.object(queue, "_inspection_from_snapshot", return_value=inspection), \
         patch.object(queue, "_ydl_download_from_info_file") as download:
        await _process_video_job(_bot(), job)
    self.assertEqual(download.call_args.args[3], "h264-aac")

async def test_no_fast_h264_error_keeps_source_link(self):
    with patch.object(queue, "require_fast_h264_format", side_effect=NoFastH264FormatError):
        await _process_video_job(_bot(), _job())
    self.assertIn("совместимый H.264-вариант", _bot_error_text())
```

Add a music-job regression proving a non-H.264 source used only for opt-in audio
recognition never enters `_prepare_telegram_video()`. Add a telemetry test
asserting `preparation_mode` is one of
`direct`, `remux_copy`, `audio_to_aac`, `rejected_no_fast_h264`,
`slideshow_encode`.

- [ ] **Step 2: Run worker/error tests and verify RED**

Run: `\.venv\Scripts\python.exe -m unittest test_queue_worker.py test_media_inspection.py test_queue_errors.py test_music_recognition.py -v`

Expected: legacy `format_id` is still passed through or the no-fast error is
currently rendered as generic media preparation error.

- [ ] **Step 3: Make the worker selector authoritative**

In `_process_video_job()`, after inspection/snapshot reconstruction, call
`require_fast_h264_format(inspection)` and overwrite the local download selector
regardless of `job.format_id`. Existing queued jobs remain readable, but cannot
choose HEVC. Pass the same selector to both info-file and URL download helpers.

Do not apply H.264-first rejection to `_recognize_music_from_source()`: it does
not deliver a source video and never calls `_prepare_telegram_video()`. Preserve
its existing opt-in source/audio extraction behavior, including a non-H.264
source when that is the only way to obtain an audio track. Keep request
ownership, state transitions and UI unchanged.

Catch `NoFastH264FormatError` before generic `MediaPreparationError` and render:

```python
"❌ Источник не отдал совместимый H.264-вариант без долгого перекодирования.\n"
"Попробуйте другое видео."
```

Retain the exact existing `MissingAudioError` message and source-link rendering.
Set `metrics.preparation_mode` on every worker terminal path without adding URL
or source metadata to stage summary.

- [ ] **Step 4: Preserve controlled H.264-oriented fallback**

When an info snapshot is stale, inspect once and choose H.264 again. When the
selected info-file download fails, use the current gallery fallback directly;
its HEVC-only result raises `NoFastH264FormatError`. Local read/permission errors
remain terminal and never begin a second downloader.

- [ ] **Step 5: Run worker, music and error tests GREEN**

Run: `\.venv\Scripts\python.exe -m unittest test_queue_worker.py test_media_inspection.py test_queue_errors.py test_music_recognition.py -v`

Expected: legacy selectors are contained, source errors are specific, and no
video-transcode retry remains.

- [ ] **Step 6: Commit Task 3**

```powershell
git add services/queue.py test_media_inspection.py test_queue_worker.py test_queue_errors.py test_music_recognition.py
git commit -m "feat: enforce H264-only media delivery"
```

### Task 4: Remove quality UI and preserve photo gallery audio slideshow

**Files:**
- Modify: `handlers/link.py:21-52,137-147,159-386`
- Modify: `services/queue.py:57-58,1300-1327`
- Modify: `test_cache_fast_path.py:160-359`
- Modify: `test_enqueue_lifecycle.py`
- Modify: `test_media_inspection.py:351-484`
- Modify: `test_media_preparation.py`

**Interfaces:**
- Removes: `VideoQuality`, `PendingInspection`, `_quality_markup`,
  `handle_quality_option`, `quality:` callback route
- Changes: `_start_pending_download(callback, session, token, pending, *, callback_answered: bool) -> None`
- Produces: `_build_slideshow(...):` H.264 exception with fixed fast settings

- [ ] **Step 1: Write failing private UX and slideshow tests**

```python
async def test_private_download_enqueues_without_quality_menu(self):
    callback, session, bot = _private_callback("normal")
    with patch("handlers.link.inspect_media", return_value=_inspection_with_fast_format("h264-aac")), \
         patch("handlers.link._start_pending_download", AsyncMock()) as start:
        await handle_download_option(callback, session, bot)
    self.assertEqual(start.await_args.kwargs["callback_answered"], True)
    self.assertFalse(any(button.callback_data.startswith("quality:") for row in callback.message.reply_markup.inline_keyboard for button in row))

def test_slideshow_uses_fast_h264_exception_settings(self):
    command = _capture_slideshow_command(images=_images(), audio=_audio())
    self.assertIn("480:854", " ".join(command))
    self.assertIn("ultrafast", command)
    self.assertIn("26", command)
    self.assertIn("128k", command)
```

Add a photo-gallery test with external audio asserting both streams are mapped;
add a no-audio test asserting `-an` remains. Add cache-hit regression asserting
an old cached HD `file_id` still skips inspect/download/probe.

- [ ] **Step 2: Run private/slideshow tests and verify RED**

Run: `\.venv\Scripts\python.exe -m unittest test_cache_fast_path.py test_enqueue_lifecycle.py test_media_inspection.py test_media_preparation.py -v`

Expected: current handler produces a quality menu and slideshow still uses
`720×1280`/`veryfast` settings.

- [ ] **Step 3: Remove the quality-menu branch**

Remove `VideoQuality`, `PendingInspection`, `_quality_markup`,
`needs_quality_choice` import and the `quality:` callback handler. Keep private
mode buttons and music button. After the one inspection writes its snapshot,
always call:

```python
await _start_pending_download(
    callback,
    session,
    token,
    pending,
    callback_answered=True,
)
```

`_start_pending_download()` always creates `DownloadQueue(format_id=None)`.
Retain existing deadline-before-enqueue, snapshot ownership, queue-limit and
failure lifecycle behavior.

- [ ] **Step 4: Implement the slideshow-only encode exception**

Set:

```python
SLIDESHOW_WIDTH = 480
SLIDESHOW_HEIGHT = 854
SLIDESHOW_VIDEO_PRESET = "ultrafast"
SLIDESHOW_VIDEO_CRF = 26
SLIDESHOW_AUDIO_BITRATE = "128k"
```

Use those values only in `_build_slideshow()`. With audio, map image video and
external audio and encode AAC `128k`; without audio, retain `-an`. Do not route
photo galleries through `NoFastH264FormatError`.

- [ ] **Step 5: Run UI, slideshow and cache regressions GREEN**

Run: `\.venv\Scripts\python.exe -m unittest test_cache_fast_path.py test_enqueue_lifecycle.py test_media_inspection.py test_media_preparation.py test_queue_worker.py -v`

Expected: no quality callbacks are generated, cache hits remain immediate and
photo galleries retain their audio/silent fallback contracts.

- [ ] **Step 6: Commit Task 4**

```powershell
git add handlers/link.py services/queue.py test_cache_fast_path.py test_enqueue_lifecycle.py test_media_inspection.py test_media_preparation.py
git commit -m "perf: remove manual quality and keep gallery audio"
```

### Task 5: Bilingual documentation and full local verification

**Files:**
- Modify: `README.md` Russian and English feature/how-it-works/current-limitations sections
- Test: all `test_*.py` modules

**Interfaces:**
- Consumes: all preceding tasks
- Produces: matched RU/EN explanation of H.264-first direct/remux/audio-only delivery, no quality menu, no-fast error and photo-gallery audio exception.

- [ ] **Step 1: Update matched RU and EN documentation**

Russian text must state:

```markdown
- Для обычных видео бот выбирает совместимый H.264/AAC источник и не выполняет
  долгое перекодирование видеопотока. Если такого источника нет, бот сообщает
  об этом вместо многоминутной обработки.
- Меню выбора качества больше не показывается. Фото-галереи по-прежнему
  собираются в MP4 с исходным треком, если он доступен.
```

Mirror those two claims in English. Preserve existing cache hit, one-heavy-worker,
opt-in music and no-description contracts; do not claim HEVC playback or a
production deployment.

- [ ] **Step 2: Run the complete safe suite**

Run: `\.venv\Scripts\python.exe -m unittest discover -p "test_*.py" -v`

Expected: every automated test passes; manual `test.py` is not imported.

- [ ] **Step 3: Run compile and repository checks**

```powershell
.\.venv\Scripts\python.exe -m compileall -q main.py init_db.py caption.py promo.py db handlers middleware routers services utils
git diff --check
git status --short
```

Expected: compile exit 0, diff check clean and no generated media/database/log artifacts.

- [ ] **Step 4: Re-read the implementation against the H.264 contract**

Run: `rg -n "libx264|\\-vf|force_normalize|quality:" services handlers routers README.md`

Expected: `libx264` only appears in slideshow code/tests/docs; no source-video
prepare path, forced normalization retry or active `quality:` callback remains.

- [ ] **Step 5: Commit Task 5**

```powershell
git add README.md
git commit -m "docs: describe fast H264 media policy"
```

- [ ] **Step 6: Stop before production**

Report local verification, commits and exact runtime files changed. Do not upload,
migrate, restart, push or create a PR without a new explicit user request.
