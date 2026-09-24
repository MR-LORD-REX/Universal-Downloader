# TikTok Downloader SDK

`downloader/tiktok/` resolves **videos**, **photo (slideshow) posts**,
**profiles**, **sounds**, **hashtags**, **collections** and **live** urls into
the shared `PostMetadata` model, entirely through **yt-dlp**.

```python
from downloader.tiktok import TikTokClient

async with TikTokClient() as tt:
    meta = await tt.get_metadata("https://www.tiktok.com/@nasa/video/7253412088251534594")
    print(meta.media_type, meta.media_group_type, meta.size_human)
    print(meta.links())            # {'video': ['https://v16-webapp-prime.tiktokcdn...mp4']}
    print(meta.urls())
    files = await tt.save(await tt.download(meta, quality="720p"), "downloads")
```

Or through the facade, which routes `tiktok.com`, `tiktokv.com`,
`vm.tiktok.com` and `vt.tiktok.com` here automatically:

```python
from downloader import Downloader

async with Downloader() as dl:
    meta = await dl.get_metadata("https://www.tiktok.com/@nasa/video/7253412088251534594")
```

---

## 1. Architecture

```
url ──► urls.parse_url ──► TikTokRef(kind=video|short|profile|sound|tag|collection|live)
                                  │
              video  ─────────────┤  info_to_metadata()      ──► PostMetadata
              list kinds ─────────┘  playlist_to_metadata()   ──► PostMetadata
                                  │
                            extract.YtDlpExtractor
                                  │
                 normalize_formats()  (de-dupe urls, prune watermarks/manifests)
                                  │
                 translate_tiktok_error()  (10204 -> RegionBlockedError, ...)
```

### Why yt-dlp

TikTok's own API needs `msToken` / `X-Bogus` / `verifyFp` request signing, which
its web bundle generates in obfuscated JavaScript. yt-dlp already implements
(and continuously maintains) that signing, so this SDK is a *normaliser plus an
error translator* on top of yt-dlp rather than a from-scratch scraper.

### Key design decisions

* **Url de-duplication.** TikTok reports the same CDN url up to four times — once
  per `bitrateInfo` entry and once as `play`. `normalize_formats` keeps a single
  `MediaFormat` per url and merges the richest description into it, so
  `h264_720p_1244300` absorbs the bare `play` entry instead of appearing twice.
* **Watermarks are pruned.** `download` (the watermarked rendition) is dropped
  whenever a clean rendition exists; `include_watermarked=True` keeps it.
* **Unplayable codecs are dropped.** `bytevc2` / `h266` renditions are marked
  unplayable by yt-dlp itself and are filtered unless `include_unplayable=True`.
* **H.264 by default.** `prefer_video_codec="h264"` because H.265-in-MP4 does
  not play back reliably in every Telegram client.
* **Error translation is conservative.** Only messages TikTok actually produces
  become a `TikTokError`. A connect timeout or DNS failure stays a plain
  `MetadataError`, so callers can tell *"TikTok said no"* apart from *"the
  network died"* — and a bot knows whether to retry.
* **Sizes come free.** yt-dlp reports `filesize` (exact) or `filesize_approx`
  (estimated, flagged via `size_is_approx`), so `meta.size_human` is populated
  before anything is downloaded.

### Public API

| member | purpose |
| --- | --- |
| `TikTokClient.get_metadata(url)` | video, slideshow, profile, sound, tag, collection |
| `TikTokClient.download(meta, quality=...)` | fetch the bytes |
| `TikTokClient.save(result, dest)` | write a `DownloadResult` to disk |
| `TikTokClient.region_blocked(exc)` | helper: is this exception an IP block? |
| `downloader.tiktok.get_metadata/download/save` | module-level shortcuts |

`live` urls are refused with a clear message: yt-dlp has no live-stream support
for TikTok.

### Configuration (`TikTokConfig`)

| field | default | effect |
| --- | --- | --- |
| `include_videos` | `True` | disable video formats |
| `include_audio_only` | `True` | keep a slideshow's soundtrack / music posts |
| `include_watermarked` | `False` | keep the `download` rendition |
| `include_manifests` | `False` | keep HLS/DASH renditions |
| `include_unplayable` | `False` | keep bytevc2/h266 renditions |
| `resolve_profiles` / `playlist_max_items` | `True` / `25` | list-url support |
| `extract_flat` | `True` | list profile entries without resolving each video |
| `prefer_video_codec` | `"h264"` | codec tie-breaker |
| `cookiefile`, `cookies_from_browser`, `app_info`, `device_id` | `None` | needed for age-gated / region-locked posts |

---

## 2. Coverage

| input | result |
| --- | --- |
| `/@user/video/<id>` | single video, H.264 ladder + soundtrack, sizes known |
| `/share/video/<id>`, `/@/video/<id>` | same, alternate paths |
| `/embed/<id>`, `/embed/v2/<id>` | same |
| `/t/<code>`, `vm.tiktok.com/<code>`, `vt.tiktok.com/<code>` | short link |
| `/@user` | profile → `media_group_type=playlist` |
| `/music/<slug>-<id>` | sound → playlist of videos using it |
| `/tag/<name>` | hashtag → playlist |
| `/@user/collection/<slug>-<id>` | collection → playlist |
| photo (slideshow) post | **soundtrack only** — see limitation 2 |
| `/live` | refused with a clear message |

All url forms above are covered by `tests/test_tiktok_offline.py`, which uses a
synthetic yt-dlp payload because TikTok blocks this host.

```bash
python tests/test_tiktok_offline.py    # always runs, no network
python tests/test_tiktok_live.py       # skips itself when the IP is blocked
```

---

## 3. Limitations (read this before shipping)

1. **TikTok blocks datacentre IPs.** From cloud hosts (and most VPNs) every
   request fails with `status 10204`, or simply times out on connect — verified
   on this machine: `Connection to www.tiktok.com timed out`. This is an
   environment property, not a bug. `RegionBlockedError` is raised when TikTok
   says so; a timeout is left as a plain `MetadataError`. To get real coverage
   run it from a residential IP or through a residential/mobile proxy
   (`PROXY=...` or `TT_TEST_PROXY=...`), or pass a `cookiefile`. **This suite was
   not verified live here; test it on the VPS.**
2. **Photo (slideshow) posts yield audio only.** yt-dlp's TikTok extractor has no
   image support at all: a slideshow resolves to its `m4a` soundtrack and
   nothing else. The SDK surfaces the audio, sets `extra["audio_only"]=True`
   and a warning, rather than pretending it has the images. If you need slideshow
   images you must add a separate scraper.
3. **Everything depends on yt-dlp.** A TikTok change breaks extraction until
   yt-dlp is updated. Pin the version and update deliberately.
4. **Some posts need cookies.** Age-gated, region-locked and "log in to view"
   posts need `cookiefile=`. Anonymous extraction also gets rate limited under
   load — that is what the bot's per-platform token bucket is for.
5. **CDN urls are signed and expire.** Hand them to Telegram promptly; do not
   cache them for later delivery.
6. **`bitrateInfo` ordering is not a quality guarantee.** Format ids like
   `h264_540p_941613` embed the bitrate, but several urls can share one
   resolution; select through `MediaItem.select()` rather than by index.
7. **Watermark removal is a filter, not a guarantee.** It drops the rendition
   yt-dlp labels `watermarked`. Other renditions are assumed clean; verify on a
   sample before shipping if this matters legally.

---

## 4. Implementation plan (what was done, in order)

1. `urls.py` — host set plus `TikTokRef`, handling 16 url shapes (video, share,
   embed v1/v2, bare-id, short, profile, sound, tag, collection, live).
2. `extract.py` — re-exports the shared yt-dlp helpers and adds
   `translate_tiktok_error`, mapping the three marker families (region block,
   private, missing) onto the exception hierarchy.
3. `models.py` — `format_from_info`, `normalize_formats` (url de-dupe + merge +
   pruning), `_quality_height` (three-tier: `<n>p` token → yt-dlp quality rank →
   short side), `info_to_metadata`, `playlist_to_metadata`.
4. `client.py` — `TikTokClient`, the `live` refusal and the `region_blocked`
   helper.
5. `config` / `exceptions` / `__init__` — tunables, error hierarchy, shortcuts.
6. Facade + bot wiring: `Platform.TIKTOK`, `tiktok_options` in `Downloader`, the
   host in `bot/filters/links.py`, a caption branch in `bot/ui/descriptions.py`
   and a platform row in `platform_settings` (default quality `best`).
7. `tests/test_tiktok_offline.py` (9 tests) and `tests/test_tiktok_live.py`
   (5 tests, skip-aware).