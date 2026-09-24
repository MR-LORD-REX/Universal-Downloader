# Instagram Downloader SDK

An async, typed downloader for Instagram posts: **single photos**, **reels /
videos**, and **carousels** (albums), with the *direct* CDN urls and the **real
size of every rendition** available before you download a byte.

```python
import asyncio
from downloader.instagram import InstagramClient, InstagramConfig

async def main():
    async with InstagramClient(InstagramConfig(session_file="ig.session")) as ig:
        meta = await ig.get_metadata("https://www.instagram.com/p/DdPEofvmpPd/")

        meta.title                      # "8 years until infinity ❤️ NEW BEGINNINGS ♾️"
        meta.author                     # 'lilbieber'
        meta.media_type                 # MediaKind.GALLERY
        meta.media_group_type           # MediaGroupType.ALBUM
        meta.count                      # 4
        meta.links()                    # {'image': ['https://instagram.fnag1-3.fna.fbcdn.net/v/t51...']}
        meta.size_human                 # '2.79 MB'  <- before downloading

        result = await ig.download(meta, quality="best")
        await result.save("downloads")  # -> downloads/instagram_DdPEofvmpPd_1/...

asyncio.run(main())
```

---

## 1. Architecture

```
downloader/instagram/
├── __init__.py    public surface (InstagramClient, config, errors, helpers)
├── client.py      InstagramClient: get_metadata / download / save
├── config.py      InstagramConfig (session file, media toggles, DASH opt-in)
├── exceptions.py  error hierarchy (InstagramNotFoundError, RateLimitedError, ...)
├── urls.py        /p/, /reel/, /reels/, /tv/, /stories/ and profile parsing
└── models.py      raw GraphQL node -> MediaItem/MediaFormat (the "brain")
downloader/core/manifests.py
                   shared reader for byte-ranged DASH manifests (reused here)
```

### Why instaloader?

Instagram has no key-less JSON endpoint for media, and its web GraphQL requests
are signed by a rotating JS bundle. [`instaloader`](https://github.com/instaloader/instaloader)
already maintains that handshake, so the SDK wraps it instead of reimplementing
it. instaloader is **synchronous** (`requests` underneath), so every call runs on
a worker thread (`asyncio.to_thread`) behind a lock plus a minimum interval - a
burst of links therefore cannot trip Instagram's rate limiter. Once metadata is
resolved the rest of the pipeline is the usual one: CDN urls, the shared download
engine, ffmpeg muxing and `save`.

The SDK reads `post._node` (the raw GraphQL dict) rather than instaloader's
convenience wrappers, because that is the only place that carries the full
`image_versions2` candidate ladder, the progressive `video_versions` mp4s **and**
the `video_dash_manifest`.

### Key design decisions

| Decision | Why |
| --- | --- |
| Quality comes from the **short side** of the frame | Instagram labels a `1080x1920` reel "1080p" and its `720x1280` copy "720p". Rendition height dominates the shared ranker, so `quality_height` is taken from `FBQualityLabel` (or `min(width, height)`), never the raw height. |
| The DASH ladder is **opt-in** (`include_dash=False`) | The ladder is higher quality but **VP9 and video-only**, so a plain `best` would resolve to 1080p VP9 and force an ffmpeg mux for *every* reel. Leaving it off keeps the progressive **H.264 + AAC** mp4 (`720x1280`, already muxed) as the pick - a url the Bot API can fetch with no download at all. |
| `size_bytes` for video comes from the manifest | Facebook annotates every DASH `<Representation>` with `FBContentLength` (exact bytes), so no probing is needed. Images have no such field, so their candidates are sized with a cheap `HEAD` probe. |
| Carousel children are read from `carousel_media` | `Post.get_sidecar_nodes()` returns a namedtuple that does **not** expose `image_versions2` / `video_dash_manifest`, so the raw children are used instead. |
| Namespace-agnostic MPD parsing | Instagram's manifest is namespaced (`{urn:mpeg:dash:...}BaseURL`), and `Element.find` cannot match that; `core/manifests.py` matches on the local name. |
| Carousels become `MediaGroupType.ALBUM` | Matches the bot brief ("gallery type post will be sent as album"). A mixed photo+video carousel is still an album, with `links()` grouped per kind. |

### Public API

```python
InstagramClient(config=None, *, progress_callback=None, **overrides)

await client.get_metadata(url, *, probe_sizes=None)
await client.download(metadata, *, quality="best", target="memory"|"disk",
                      dest=None, pattern=None, progress=None, only=None,
                      max_size_bytes=None, temp_dir=None, ...)
await client.download_by_url(url, **kwargs)
```

The canonical `PostMetadata` model is shared with every other platform (see the
[YouTube doc](YOUTUBE_SDK.md#public-api));

* `links()` groups the direct CDN urls by `image` / `video`,
* `items[i].formats` is the rendition ladder with sizes,
* `meta.quality_hints` is `{"prefer_muxed": True, "container": "mp4", "codec": "avc1"}`.

### Configuration (`InstagramConfig`)

```python
InstagramConfig(
    # credentials (the reliable path)
    session_file="ig.session",        # written by `instaloader --login=<user>`
    username=None, password=None,     # used once when session_file is absent
    custom_user_agent=None,
    # media
    include_images=True, include_videos=True,
    include_carousels=True, include_stories=True,
    max_carousel_items=20,
    # formats
    include_progressive=True,         # instagram's muxed H.264+AAC mp4
    include_dash=False,               # the VP9 ladder (video only, opt-in)
    max_image_candidates=4,
    # quality (inherited from PlatformConfig)
    prefer_container="mp4", prefer_video_codec="avc1", probe_sizes=True,
    max_size_bytes=None,
)
```

---

## 2. Coverage (verified live)

| Post type | Metadata | Sizes | Download | Notes |
| --- | --- | --- | --- | --- |
| Photo (`/p/`) | ✅ | ✅ exact (HEAD) | ✅ | 12 candidate variants, largest first |
| Reel / video (`/reel/`) | ✅ | ✅ exact | ✅ | progressive mp4, **already muxed** |
| Carousel (`/p/`, `carousel_container`) | ✅ | ✅ | ✅ | one `MediaItem` per child, saved as an album folder |
| `/tv/`, `/reels/`, `instagr.am`, `ig.me` | ✅ | ✅ | ✅ | all canonicalised onto the shortcode |
| DASH ladder (`include_dash=True`) | ✅ | ✅ from `FBContentLength` | ✅ muxed | 240p…1080p VP9 + one AAC audio track |
| Story / highlight | ⚠️ login only | — | — | needs `session_file`; expires after 24 h |
| Profile / timeline | ❌ | — | — | raises `UnsupportedURLError` with a clear message |

Live numbers (2026-09, anonymous):

- `DdPEofvmpPd` (carousel, @lilbieber): 4 photos, top candidates `2552x2552`,
  `2021x2021`, `3214x3214`, `4003x4003`; 521 / 310 / 694 / 1333 KB; total `2.79 MB`.
- `DdhvW0GslGe` (reel, 1080x1920): progressive `720x1280` H.264 + AAC =
  **1,026,296 bytes** (HEAD probe matched the download byte-for-byte); DASH
  ladder of 8 renditions 240p…1080p where 1080p = 2,365,448 bytes and the audio
  track = 36,717 bytes. Muxing 1080p produced a 2.29 MB `vp9 + aac` file.

---

## 3. Limitations (read this before shipping)

- **Anonymous access is rate limited hard.** The first few requests succeed,
  then Instagram answers **HTTP 401 "Please wait a few minutes before you try
  again"** (profiles fail immediately). Configure `session_file` for anything
  beyond occasional use; create it once with `instaloader --login=<username>`
  (the bot reads it from `INSTAGRAM_SESSION_FILE`). The SDK maps this to
  `RateLimitedError` and never pretends it was a real post.
- **CDN urls are signed and expire.** The `oe` parameter is a hex epoch:
  roughly **33 h** for video and **108 h** for images. Deliver or download
  promptly, and never cache a url for later.
- **Stories need a login** *and* expire 24 hours after posting, so even with a
  session they are a best-effort source (`StoryUnavailableError` otherwise).
- **Profiles/timelines are unsupported by design.** Only post/reel/tv/story
  urls are accepted; a bare profile url raises `UnsupportedURLError` instead of
  silently scraping a whole account.
- **The DASH ladder is VP9 video-only.** Turning it on means the processing lane
  for those items (and a VP9-in-mp4 mux). It also contains no audio at all, so
  `entry.needs_mux` is `True`; the audio side comes from the same manifest.
- **`video_duration` is sometimes absent.** Instagram returns `None` for it on
  some reels, so `meta.duration`/`item.duration` can be `None` even for a video.
- **A video post's `original_width/height` is the upload frame**, not the
  rendition you get: a reel reports `1080x1920` while the progressive copy is
  `720x1280`. Read `format.width/height` (or `quality_label`) for the actual file.
- **`instaloader` is a third-party dependency** whose handshake Instagram can
  break; pin it and expect occasional breakage.
- **Private accounts** need a session that follows them (`PrivateProfileError`
  otherwise).

---

## 4. Implementation plan (what was done, in order)

1. `urls.py` — parse every shortcode form (`/p/`, `/reel/`, `/reels/`, `/tv/`,
   `instagr.am`, `ig.me`, user-prefixed), plus stories/highlights and profiles,
   with a static reserved-path list so a profile is never a false positive.
2. `core/manifests.py` — a namespace-agnostic reader for byte-ranged DASH
   manifests that yields `DashTrack`s with `FBContentLength` sizes and
   `FBQualityLabel` quality.
3. `models.py` — the "brain": image candidate ladder, progressive muxed mp4s,
   optional DASH ladder, short-side quality mapping, carousel expansion,
   `PostMetadata` assembly.
4. `config.py` / `exceptions.py` — session handling and a typed error hierarchy
   that joins the shared `DownloaderError` tree.
5. `client.py` — `InstagramClient` on top of `core.BaseClient`: thread-offloaded
   instaloader calls under a lock + interval, lazy login/session caching, size
   probes, and `_story_metadata` for logged-in story resolution.
6. Wiring — `Platform.INSTAGRAM` in the facade (`Downloader`, `platform_of`,
   `instagram`/`instagram_options`), the bot's `PLATFORMS`/queue/platform
   settings, an Instagram caption and `INSTAGRAM_SESSION_FILE`.
7. Tests — `tests/test_instagram_offline.py` (20 unit tests) and
   `tests/test_instagram_live.py` (11 live tests).

### Verification commands

```bash
python tests/test_instagram_offline.py   # 20 unit tests, no network
python tests/test_instagram_live.py      # 11 live tests; IG_TEST_DOWNLOAD=0 to skip bytes
IG_TEST_ALL=1 python tests/test_instagram_live.py   # + the slow 1080p DASH mux case
IG_TEST_SESSION=ig.session python tests/test_instagram_live.py   # use a session file
```

### Suggested next steps

- Follow a *carousel/album* from a story highlight into individual items.
- Optional thumbnails for reels (Instagram exposes `image_versions2` there too).
- A cookie/session health check on startup, so a stale session is reported in
  `/admin` instead of surfacing as a 401 on the first user request.
- Multi-account session rotation to spread the anonymous/one-account rate limit.
