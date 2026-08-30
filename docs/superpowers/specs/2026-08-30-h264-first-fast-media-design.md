# H.264-first fast media delivery

Дата: 2026-08-30
Статус: проект спецификации для проверки
Область: локальный DownloadTTBot; production deploy требует отдельного разрешения

## Контекст

Пользователь сообщил о многоминутной загрузке TikTok-видео. Разбор конкретного
ролика `7679504834784939285` показал:

- TikTok отдаёт готовый `576×1024` H.264/AAC MP4 около 8,4 МБ;
- также доступны HEVC video-only варианты до `1080×1920`;
- production stage summary зафиксировал около 1 секунды на download и около
  834 секунд на FFmpeg preparation.

Причина — текущий selector `bestvideo*+bestaudio/best` выбирает максимальный
HEVC-вариант. Затем бот перекодирует весь видеопоток в H.264/AAC с одним
FFmpeg-потоком на малом VDS. Более ранний H.264-first selector избегал этой
ветки для готовых совместимых MP4.

Пользователь утвердил speed-first политику: не ограничивать разрешение до 480p,
не выполнять долгое video transcode и использовать только источники, которые
можно сразу отправить или подготовить без перекодирования видеопотока.

## Цели

1. Исключить H.265/HEVC, VP9, AV1 и любые другие source-video transcode из
   обычного download пути.
2. Сначала выбирать лучший доступный H.264-вариант, сохраняя максимальный FPS,
   затем разрешение и битрейт.
3. Сохранять обязательный звук; не отправлять немое видео.
4. Разрешать только быстрые операции подготовки: stream-copy remux и
   audio-only transcode в AAC.
5. Сохранить current cache fast path, inline/storage, split, music deep-link,
   one-heavy-worker и sessionless DB boundaries.
6. Сохранить старые HD cache hits по выбранной переходной политике.
7. Сохранить TikTok photo galleries: скачать изображения и исходный трек,
   затем собрать слайд-шоу с аудио.
8. Сделать причину отказа явной, когда совместимого H.264-источника нет.

## Не цели

- Не применять фиксированное ограничение 480p к обычным source videos.
- Не перекодировать HEVC/VP9/AV1 в H.264 даже для коротких роликов.
- Не отправлять HEVC как обычное Telegram video или как document fallback.
- Не удалять старые cache rows или storage messages.
- Не добавлять параллельные download/FFmpeg workers.
- Не менять production в рамках этой спецификации.

## Политика форматов

### Подходящие источники

Из одного `yt-dlp` info snapshot выбирается единственный explicit format
selector в таком порядке:

1. H.264/AVC video + AAC audio в одном MP4.
2. H.264/AVC video + отдельный AAC audio stream.
3. H.264/AVC video + отдельный audio stream другого кодека.

Внутри первого применимого класса rank идёт по:

1. известному FPS по убыванию;
2. площади кадра по убыванию;
3. битрейту по убыванию;
4. размеру файла по убыванию.

Это сохраняет максимальный доступный FPS и качество, но никогда не выбирает
не-H.264 видеокодек. Для второго и третьего класса selector включает лучший
доступный audio stream.

### Разрешённая подготовка

После скачивания probe подтверждает видео и аудио.

- Готовый H.264/AAC MP4 с подходящими pixel format, SAR и rotation отправляется
  напрямую без FFmpeg.
- H.264/AAC в другом контейнере remux-ится в MP4 с `-c:v copy -c:a copy`.
- H.264 с не-AAC audio remux-ится с `-c:v copy`; перекодируется только audio в
  AAC.
- Ни один путь не использует `-c:v libx264`, `-vf`, scale, fps conversion,
  rotation filter или video CRF для source video.

Если H.264-source требует video filter для Telegram-совместимости, он считается
неподходящим, а не перекодируется.

### Отказ

Если `yt-dlp` не нашёл подходящий H.264 source, бот пробует H.264-oriented
gallery fallback. Если fallback также не даёт H.264 с аудио или audio stream
для быстрого mux, пользователь получает понятную ошибку:

> ❌ Источник не отдал совместимый H.264-вариант без долгого перекодирования.
> Попробуйте другое видео.

Бот не начинает HEVC→H.264 conversion после этой ошибки.

## Компоненты и поток данных

### Selector и info snapshot

`services/queue.py` получает helper `select_fast_h264_format(formats)`, который
возвращает exact `format_id` или `None`. `MediaInspection` хранит этот selector
как metadata summary. `download_with_info_file()` получает его напрямую, поэтому
worker не делает второе page extraction.

При stale info snapshot сохраняется существующая controlled fallback логика, но
она повторно выбирает только H.264 source и не переключается на best HEVC.

### Private, group и inline

Private по-прежнему показывает выбор normal/no-description/spoiler, но больше
не показывает menu качества. После выбора создаётся обычная queue job с
`format_id=None`.

Group и inline используют тот же worker selector. Existing queued jobs с
legacy `format_id` не получают право выбрать HEVC: worker проверяет выбранный
источник против H.264-first policy. Колонки `format_id` в SQLite остаются для
совместимости существующей схемы и MusicRequest.

### Cache

`MEDIA_CACHE_PROFILE` остаётся `h264-aac-v2` по выбранной пользователем
переходной политике:

- старый HD `file_id` cache hit отправляется сразу;
- новый cache miss скачивается H.264-first и записывается под тем же key;
- существующий HD hit не принудительно перезаписывается и не удаляется.

Это сознательно означает, что качество cache hit зависит от того, когда запись
была создана. Приоритет перехода — скорость и отсутствие повторного скачивания
старых популярных роликов.

### Gallery fallback

TikTok candidate ranking фильтрует non-H.264 video до загрузки и сохраняет
audio attach/mux rules. Gallery CLI output проходит тот же H.264 probe; HEVC,
VP9 и AV1 не запускают video transcode.

### Фотослайд-шоу

Photo posts — единственное исключение: видео создаётся из изображений, поэтому
H.264 encoding неизбежен. Для него используется небольшой `480×854` canvas,
audio AAC `128k`, preset `ultrafast` и CRF `26`. Это не преобразование
скачанного source video.

Этот путь не удаляется и не считается ошибкой из-за отсутствия H.264 source
video: gallery fallback сохраняет последовательность image download → source
audio download → H.264/AAC slideshow. Если исходный трек недоступен, остаётся
существующий явный сценарий слайд-шоу без audio, а не отказ от всей галереи.

## Ошибки и совместимость

- `MissingAudioError` сохраняет текущий понятный UX: видео без аудио не
  отправляется.
- `NoFastH264FormatError` обрабатывается как terminal media preparation error
  с новым понятным сообщением выше.
- Existing cached `file_id` не подвергается probe или FFmpeg.
- Direct H.264 send, cache invalidation, split, storage и music deep-link
  сохраняют существующие contracts.
- Forced video normalization retry после `TelegramBadRequest` удаляется.
  Разрешён только retry с fast remux/audio-only path, если первый fast path ещё
  не был применён и видео не было доставлено.

## Наблюдаемость

Stage summary получает allowlisted `preparation_mode`:

- `direct`;
- `remux_copy`;
- `audio_to_aac`;
- `rejected_no_fast_h264`;
- `slideshow_encode`.

`download_backend`, `download_fallback`, split metrics и privacy rules остаются
без изменений. Это позволяет отличить быстрый H.264 path от terminal reject без
логирования URL, cookies, токенов или descriptions.

## Тестирование

TDD добавляет или обновляет тесты для:

1. Selector: combined H.264/AAC, separate H.264+AAC, H.264+non-AAC,
   HEVC-only и ranking FPS/resolution.
2. Info snapshot download: exact fast selector передаётся в `yt-dlp` без
   повторного extraction.
3. Preparation: direct compatible H.264/AAC, fast remux, audio-only transcode,
   H.264 video requiring a filter rejection и no `libx264` source path.
4. Gallery TikTok candidates: H.264 filter, audio mux и HEVC-only rejection.
5. Private quality menu removal; group/inline use the same queue selector.
6. Existing HD cache hit remains direct; new H.264 cache miss stores a normal
   `file_id` under the unchanged profile.
7. Legacy queued `format_id` cannot force HEVC conversion.
8. User-facing no-fast-H.264 error and preserved missing-audio error.
9. Photo slideshow encoding at `480×854` with fast preset.
10. Photo gallery with an external source track keeps the audio in the final
    slideshow; gallery without a track still produces the existing silent
    slideshow path.
11. Existing cache, queue lifecycle, session, telemetry, split, music and
    error regressions.

The full safe suite remains:

```powershell
.venv\Scripts\python.exe -m unittest discover -p "test_*.py" -v
```

`test.py` is excluded because it performs a live download.

## Критерии готовности

- Для ролика `7679504834784939285` выбран готовый H.264/AAC `576×1024` source,
  без video transcode.
- Обычные source videos never execute `libx264` or a video filter.
- H.264/AAC direct and stream-copy/audio-only paths preserve audio and source
  FPS.
- HEVC-only source produces a clear terminal error after H.264-oriented
  fallback, not a multi-minute conversion.
- Existing HD cache hit remains immediate.
- New normal jobs never show a quality menu.
- All safe automated tests, compile check and diff check pass.
- Production deployment is a separate explicitly approved step.

## Риски и смягчение

- Некоторые TikTok videos не имеют H.264 source. Они будут rejected instead of
  eventually delivered after a slow conversion; это сознательный speed-first
  trade-off.
- H.264 source can have lower resolution than available HEVC. Пользователь
  выбрал предсказуемую latency вместо максимального разрешения.
- Audio-only conversion может увеличить время на длинных роликах, но не требует
  video decode/encode и остаётся bounded compared with HEVC transcode.
- Generated slideshows still encode video. Небольшой canvas и fast preset
  ограничивают эту неизбежную работу.
