# Downloader SDK — architecture, plan and limitations

One async SDK for **Reddit**, **YouTube** and **Twitter/X**, built for a
Telegram downloader bot: fetch metadata (title, author, media type, group type,
**direct CDN links** and **sizes**) *before* downloading, then download and
`save()` wherever you want.

```python
import asyncio
from downloader import Downloader

async def main():
    async with Downloader() as dl:
        meta = await dl.get_metadata("https://youtu.be/FOUwd1h_jF4")

        meta.platform           # Platform.YOUTUBE
        meta.media_type         # MediaKind.VIDEO
        meta.media_group_type   # MediaGroupType.SINGLE
        meta.title, meta.author # ...
        meta.links()            # {'video': ['https://rr4---...googlevideo.com/videoplayback?...']}
        meta.size_human         # '57.64 MB'  <- before downloading

        allowed, size = dl.affordable(meta, 50 << 20)      # premium gate
        result = await dl.download(meta, quality="720p", max_size_bytes=50 << 20)
        await result.save("downloads")

asyncio.run(main())
```

---

## 1. Architecture

```
downloader/
├── __init__.py      public surface: Downloader + core models
├── client.py        Downloader facade: routing, get_metadata, download, save, fetch, size_of
├── adapters.py      bridges the legacy Reddit SDK onto the shared core models
├── core/            ← shared layer every platform builds on
│   ├── enums.py       Platform, MediaKind, MediaGroupType, FormatKind/Origin, DownloadTarget
│   ├── exceptions.py  DownloaderError hierarchy (incl. SizeLimitExceededError)
│   ├── models.py      PostMetadata / MediaItem / MediaFormat / Thumbnail / DownloadResult
│   ├── http.py        aiohttp pool: retries, throttling, HEAD/range size probes, size cache
│   ├── ffmpeg.py      imageio-ffmpeg bridge: probe, mux, remux, HLS/DASH grab
│   ├── select.py      QualitySpec grammar + build_plan (video+audio pairing)
│   ├── engine.py      DownloadEngine: concurrent streaming, striped Range fetches, muxing
│   ├── saver.py       render_pattern, album folders, non-clobbering writes
│   ├── base.py        BaseClient: lifecycle, download orchestration, probes
│   ├── config.py      PlatformConfig: transport, quality, naming, size limits
│   ├── progress.py    ProgressEvent/ProgressPhase emission
│   ├── urls.py        host/url helpers
│   └── ytdlp.py       shared async yt-dlp extractor + error translation
├── reddit/          RedditClient  (own models/http/saver; adapted into core)
├── youtube/         YouTubeClient (yt-dlp extraction + native engine)
└── twitter/         TwitterClient (fxtwitter + yt-dlp, native engine)
```

### Layering

```
        ┌──────────────── Downloader facade (client.py) ────────────────┐
        │  platform_of()  routing │  get_metadata_many()  concurrency    │
        └───────┬───────────────────────┬───────────────────────┬───────┘
                │                       │                       │
        YouTubeClient            TwitterClient           RedditDriver → RedditClient
                │                       │                       │
                └──────────────┬────────┴───────────┬───────────┘
                               ▼                    ▼
                    core.BaseClient        core models / enums
                               │
          ┌────────────┬───────┴────────┬──────────────┐
          ▼            ▼                ▼              ▼
      core.http   core.select      core.engine     core.saver
     (probes,     (quality        (streaming,     (patterns,
      retries)     grammar)        muxing)         albums)
```

### Data flow (one url, end to end)

1. **Route** — `Downloader.platform_of(url)` checks the YouTube matcher, then
   Twitter, then Reddit (host-checked first so Reddit cannot claim the whole
   internet). `Platform.UNKNOWN` raises `UnsupportedURLError`.
2. **Extract metadata** — the platform client returns a canonical
   `PostMetadata`: identity, media type, group type, one `MediaItem` per asset,
   every `MediaFormat` rendition, thumbnails, captions, and raw/native payload.
3. **Size resolution** — yt-dlp/fxtwitter/manifest sizes are used when exact;
   missing ones are filled with a ranged `HEAD`/`GET` probe (cached with a TTL).
   `meta.size_human` / `size_for(quality)` are then meaningful *before* download.
4. **Plan** — `core.select.build_plan(items, QualitySpec)` picks the primary
   format per item and pairs a separate audio track when the primary is
   video-only (so the engine knows it must mux).
5. **Download** — `core.engine` streams each entry concurrently (bounded by
   `max_download_concurrency`), using **striped `Range` requests** when the CDN
   throttles plain GETs (YouTube, v.redd.it), into memory or a temp dir.
6. **Mux** — when video and audio are separate, ffmpeg merges them into a
   Telegram-friendly MP4 (`prefer_video_codec="avc1"`, `prefer_audio_codec="mp4a"`).
7. **Save** — `result.save(dest, pattern=...)` renders `{placeholders}`, puts
   albums in their own folder, and never clobbers an existing file.

### Key design decisions

| Decision | Why |
| --- | --- |
| One canonical model everywhere | The bot writes one upload/size/gate code path; `PostMetadata`/`DownloadResult` are identical for all three platforms. |
| Metadata ≠ download | `get_metadata` never fetches media bytes, so the bot can show a size/quality menu cheaply and gate premium features. |
| Direct CDN links in metadata | `meta.links()` returns the urls the platform actually serves files from (not the post url), so static media can be handed to Telegram by url. |
| ffmpeg muxing is automatic | Reddit/YouTube serve audio as a separate file; a video-only upload plays silently. The engine checks `needs_mux` *before* the manifest shortcut (so an HLS/DASH video rendition is never saved audioless) and muxes. |
| Downloaded files are audited | With `verify_audio=True` (default) every un-muxed video is probed with ffmpeg; a file that turned out silent produces a `DownloadResult.warnings` entry instead of failing quietly. |
| Ranged/striped downloads | Both googlevideo and v.redd.it throttle un-ranged GETs by orders of magnitude; bounded ranges restore full speed. |
| `max_size_bytes` gate | Enforced before download *and* mid-stream, so a premium check can never be bypassed by a lying Content-Length. |
| Reddit adapted, not rewritten | The Reddit SDK predates `core` and works; `adapters.py` bridges it into the canonical model without touching its public API. |
| Pluggable extraction | yt-dlp for YouTube/Twitter video, page scraping for YouTube community posts, a fixup api for Twitter photos — each isolated behind a small module. |

### Public API (facade)

```python
Downloader(*, progress_callback=None, youtube=None, twitter=None, reddit=None,
           youtube_options=None, twitter_options=None, reddit_options=None,
           platforms=None)

Downloader.platform_of(url) -> Platform        # static, no network
Downloader.supports(url) -> bool               # static, no network

await dl.get_metadata(url, *, platform=None) -> PostMetadata
await dl.get_metadata_many(urls, *, concurrency=4) -> list[PostMetadata | Exception]
await dl.download(metadata_or_url, *, quality="best", target="memory"|"disk",
                  dest=None, pattern=None, max_size_bytes=None,
                  include_audio=None, only=None, temp_dir=None, progress=None)
await dl.save(metadata_or_url_or_result, dest, **kwargs) -> list[Path]
await dl.fetch(url, *, max_bytes=None, headers=None) -> bytes     # static files
await dl.size_of(url) -> (bytes, mime)                           # any direct link
await dl.size_human_of(url) -> str
dl.affordable(metadata, max_bytes) -> (ok, size)                 # premium gate
dl.summary() -> str
```

`dl.fetch`/`size_of`/`size_human_of` work on **any** direct link, including
hosts no platform owns (imgur, generic CDNs) — they are the bot's static-file
fast path.

Scratch space: `target="disk"` keeps the downloaded file in a private work
directory until you `save()` it. The file already carries its final,
pattern-derived name and it survives closing the client, while unused scratch
directories are pruned automatically (bytes held only in memory leave nothing
behind). Call `result.free()` when you are done with a result.

### Telegram bot integration

```python
from downloader import Downloader

dl = Downloader()

# 1) images/gifs that need no processing: hand Telegram the url directly
meta = await dl.get_metadata(post_url)
if meta.media_type is MediaKind.IMAGE and not meta.is_gallery:
    await bot.send_photo(chat_id, meta.links("best")["image"][0],
                         caption=f"{meta.title}\n{meta.size_human}")

# 2) premium gate before spending bandwidth
allowed, size = dl.affordable(meta, user.quota_bytes)
if not allowed:
    await bot.reply("too large for the free tier")

# 3) videos/large files: download, then upload from disk
result = await dl.download(meta, quality=chosen_quality,
                           target="disk", max_size_bytes=user.quota_bytes,
                           progress=lambda e: report(e.phase, e.percent))
for path in result.saved_paths:
    await bot.send_video(chat_id, path)
result.free()
```

---

## 2. Plan (what was built, in order)

1. **Shared core** (`downloader/core/`) — canonical models, enums, exceptions,
   the aiohttp client with size probes, the ffmpeg bridge, the quality grammar,
   the streaming/mux engine, the pattern saver and `BaseClient`.
2. **Reddit** — an existing, working SDK. Rather than rewrite it, `adapters.py`
   converts its metadata/results into the core model (`RedditDriver`), and the
   facade is taught to route to it. Its public API is unchanged.
3. **YouTube** (`downloader/youtube/`) — yt-dlp extraction, size probing,
   adaptive audio/video pairing + muxing, dub collapsing, manifest dedupe,
   captions, playlists, and a community-post scraper for what yt-dlp cannot do.
4. **Twitter/X** (`downloader/twitter/`) — fxtwitter for photos/galleries plus
   yt-dlp for the video ladder, exact HEAD-probed sizes, HLS pruning.
5. **Orchestrator** (`downloader/client.py`) — routing, a single metadata/
   download/save surface, `get_metadata_many`, `fetch`/`size_of`, and the
   `affordable()` premium gate.
6. **Tests** — offline unit suites per platform + the orchestrator, and live
   integration suites hitting the real CDNs (all links in `links.txt`).
7. **Docs** — this file plus [Reddit](REDDIT_SDK.md), [YouTube](YOUTUBE_SDK.md)
   and [Twitter](TWITTER_SDK.md) references.

### Verification commands

```bash
python tests/test_reddit_offline.py        # 16 unit tests
python tests/test_youtube_offline.py       # 11 unit tests
python tests/test_twitter_offline.py       #  9 unit tests
python tests/test_orchestrator_offline.py  # 11 unit tests
python tests/test_reddit_live.py           # 17 live tests
python tests/test_youtube_live.py          # 14 live tests
python tests/test_twitter_live.py          # 11 live tests
python tests/test_orchestrator_live.py     # 14 live tests
```

Every live suite honours `<PREFIX>_TEST_DOWNLOAD=0` to run metadata-only, and
turns network/rate-limit failures into `skip` rather than `FAIL`.

---

## 3. Limitations (read this before shipping)

### Cross-platform

- **Sizes can require a probe.** Where a platform omits a size (Twitter
  `filesize_approx` is off by ~28%; Reddit renditions; some YouTube manifests)
  the SDK issues a `HEAD`/ranged `GET`. That costs one request per url and needs
  the CDN to answer; a few hosts refuse `HEAD` and fall back to a ranged `GET`.
- **Signed urls expire.** YouTube googlevideo urls die after ~30 min; Reddit CDN
  urls change per post. Do not cache `links()` in a database for later sending.
- **Large downloads need disk.** `target="memory"` holds the whole file in RAM
  (fine for images/short videos, risky for long ones). Prefer `target="disk"`
  plus `max_size_bytes` for user uploads.
- **ffmpeg is required for muxing.** It ships via `imageio-ffmpeg`; without it,
  separate video/audio renditions cannot be combined (`mux_audio=False` and a
  missing ffmpeg both produce a video-only file, now reported via
  `DownloadResult.warnings`).
- **`verify_audio` is best effort.** It probes the produced file with ffmpeg; if
  the binary is unavailable the check is skipped, and a genuinely silent source
  (a Twitter `gif`, a muted upload) is reported as silent rather than "fixed".
- **Only three platforms.** Instagram, TikTok, Facebook etc. are **not**
  supported (they need authenticated/undocumented APIs, and yt-dlp's Twitter
  extractor is not a general social-media scraper).
- **No DRM/paid content**, and nothing that requires an authenticated session
  unless you provide cookies.
- **Ephemeral CDN behaviour.** Very old posts, private accounts, deleted media
  and region locks will fail — reported as errors, never silently.
- **yt-dlp drift.** Extraction depends on yt-dlp; keep it updated, and expect
  occasional breakage when a platform changes its player or API.
- **Rate limits.** Metadata APIs (YouTube timedtext, fxtwitter) can 429 under
  load; the SDK surfaces/skips rather than retrying forever.

### Per platform

| Platform | Known limitations |
| --- | --- |
| **Reddit** | Public JSON API is 403 from many networks; the SDK uses archive/RSS/oEmbed fallbacks (a subset of posts is partial without OAuth credentials). `v.redd.it` DASH/CMAF renditions are **video-only** — audio is a separate `CMAF_AUDIO_*` file, so muxing is mandatory. Separate HLS videos (`v.redd.it/<id>/HLSPlaylist.m3u8`) are muxed with ffmpeg. |
| **YouTube** | Community-post media are `yt3.ggpht.com` thumbnails (largest public copy), not true originals. Live streams are metadata-only. Captions can be rate-limited (429). Some videos need cookies or a specific `player_client`. A bare 3–4 digit `quality` is read as a height (`"137"` → 137p), so pin exact formats with `"137+140"`. |
| **Twitter/X** | Photo metadata comes from the third-party key-less `api.fxtwitter.com` (use `media_api="off"` for a videos-only, dependency-free mode). Profiles/timelines are unsupported; only tweets and t.co links to them. `hls-*`/`hls-audio-*` are dropped by default because progressive mp4s are already muxed. |

### When to use the platform client directly

The facade is enough for most bots. Reach for `YouTubeClient` / `TwitterClient`
/ `RedditClient` directly when you need platform-only extras
(`download_subtitles`, `available_qualities`, `plan_summary`, Reddit's
`providers` list, per-platform config objects).