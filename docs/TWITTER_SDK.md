# Twitter / X Downloader SDK

An async, typed downloader for tweets: **videos** (the whole progressive
ladder), **single photos** and **multi-image galleries**, with every direct
`video.twimg.com` / `pbs.twimg.com` url and its **real size** available before
you download.

```python
import asyncio
from downloader.twitter import TwitterClient

async def main():
    async with TwitterClient() as tw:
        meta = await tw.get_metadata("https://x.com/GenshinImpact/status/2102941090571485331")

        meta.title                      # 'Event Now Available'
        meta.author                     # 'Genshin Impact'
        meta.media_type                 # MediaKind.IMAGE
        meta.media_group_type           # MediaGroupType.SINGLE
        meta.links()                    # {'image': ['https://pbs.twimg.com/media/...?name=orig']}
        meta.size_human                 # '2.97 MB'  <- before downloading

        result = await tw.download(meta)
        await result.save("downloads")  # -> downloads/Genshin Impact_2102941090571485331/...

asyncio.run(main())
```

---

## 1. Architecture

```
downloader/twitter/
├── __init__.py    public surface (TwitterClient, config, errors, helpers)
├── client.py      TwitterClient: get_metadata / download / save
├── config.py      TwitterConfig (media api, photo quality, video ladder, photos)
├── exceptions.py  error hierarchy (TweetNotFoundError, MediaAPIError, ...)
├── urls.py        status / profile / t.co parsing, tweet-id extraction
├── fx.py          api.fxtwitter.com client + t.co resolution
└── models.py      tweet payload / yt-dlp info -> MediaItem/MediaFormat (the "brain")
```

### Why two back ends?

| Source | Provides | Why |
| --- | --- | --- |
| **yt-dlp** (`Twitter` extractor) | video quality ladder (`http-256/832/2176/10368`) | Reliable for videos, but **fails outright on image-only tweets** ("No video could be found") and never returns photos. |
| **api.fxtwitter.com** | photos, galleries, video fallbacks, thumbnails, durations | The only **key-less** source that exposes photo media. `media_api="off"` disables it and restricts the SDK to videos via yt-dlp. |

The video ladder from yt-dlp is *merged* onto the fx media list, so one call
gives you photos **and** every video rendition. Photo tweets are served as
`pbs.twimg.com` variants (`?name=orig|large|medium|small`); `photo_quality`
picks which one becomes the primary link.

### Key design decisions

| Decision | Why |
| --- | --- |
| Progressive mp4s marked `muxed` | yt-dlp's Twitter extractor reports `vcodec`/`acodec` as `null` for `http-*` formats even though the mp4 really carries H.264 **and** AAC (verified with ffprobe). The kind is inferred from the format id/protocol so no pointless muxing happens. |
| HLS duplicates pruned | yt-dlp returns `hls-*` video-only and `hls-audio-*` copies alongside the progressive muxed mp4s; they are dropped unless `include_manifest_formats=True`. |
| HEAD probes for exact sizes | yt-dlp's `filesize_approx` is wrong for Twitter (off by ~28% in testing). `pbs.twimg.com` / `video.twimg.com` return a true `Content-Length` on HEAD/`Range`, so sizes are probed and marked exact. |
| `?name=orig` is the real original | The `=s288-rw-nd-v1`-style suffixes are webp previews; the `name=orig` variant is the full-resolution file. |
| Gallery grouping | A multi-image tweet becomes `MediaGroupType.GALLERY`, a mixed photo+video tweet becomes `MediaGroupType.ALBUM`. |

### Public API

```python
TwitterClient(config=None, *, progress_callback=None, **overrides)

await client.get_metadata(url, *, include_video_formats=None,
                          probe_sizes=None, photo_index=None)
await client.download(metadata, *, quality="best", target="memory"|"disk",
                      dest=None, pattern=None, progress=None, only=None,
                      max_size_bytes=None, temp_dir=None, ...)
await client.download_by_url(url, **kwargs)
client.preview_url(metadata, quality="best")   # ready-to-upload preview link
client.plan_summary(metadata, quality=None)    # human-readable (item, format, size)
```

The canonical `PostMetadata` model is shared with every other platform (see the
[YouTube doc](YOUTUBE_SDK.md#public-api)); `links()` groups the direct CDN urls
by `image` / `video`.

### Configuration (`TwitterConfig`)

```python
TwitterConfig(
    # metadata
    media_api="auto",                 # auto | fxtwitter | off
    media_api_url="https://api.fxtwitter.com",
    photo_quality="orig",             # orig | large | medium | small
    include_photos=True, include_videos=True, include_all_media=True,
    # extraction
    resolve_video_formats=True, include_manifest_formats=False,
    include_gif_as_video=False, resolve_tco_links=True,
    # quality (inherited from PlatformConfig)
    prefer_container="mp4", prefer_video_codec="avc1", probe_sizes=True,
    max_size_bytes=None,
)
```

---

## 2. Coverage (verified live)

| Post type | Metadata | Sizes | Download | Notes |
| --- | --- | --- | --- | --- |
| Video tweet | ✅ | ✅ exact | ✅ | 4 progressive renditions: 1080/720/360/270p |
| Single photo | ✅ | ✅ exact | ✅ | `?name=orig` full-resolution jpeg |
| Multi-image gallery | ✅ | ✅ | ✅ | one `MediaItem` per photo, saved as an album folder |
| GIF / amplify | ✅ | ✅ | ✅ | treated as video (or gif with `include_gif_as_video=False`) |
| t.co short link | ✅ | ✅ | ✅ | resolved to the underlying tweet |

Live numbers (2026-09):

- `2102736196065521695` (video): 1080p `http-10368` = 116,156,565 bytes (exact); 360p downloaded in ~0.6 s.
- `2102941090571485331` (photo): `?name=orig` = 3,117,627 bytes, probed size matched the download byte-for-byte.
- `2102374550868521044` (gallery): 3 images saved to an album folder.

---

## 3. Limitations (read this before shipping)

- **Photo metadata depends on a third-party API.** `api.fxtwitter.com` is the
  only key-less source of photo media. It is free and needs no token, but it is
  not operated by X, so treat it as best-effort. `media_api="off"` removes the
  dependency at the cost of videos-only support (no photos, no galleries).
- **Profiles and timelines are unsupported.** Only tweet/status urls (and t.co
  links to them) are handled.
- **cookies/age-restricted/sensitive tweets** may be withheld by yt-dlp/X; the
  SDK surfaces that as an error rather than guessing.
- **Long videos are large.** `max_size_bytes` is the premium gate and is
  enforced before and during download.
- **Deletes/protected accounts** obviously cannot be downloaded.
- **Rate limits** on the fixup api are possible under heavy load.
- **`hls-*`/`hls-audio-*` are dropped by default** because the progressive mp4s
  are already muxed; flip `include_manifest_formats=True` if you need them.

---

## 4. Implementation plan (what was done, in order)

1. `urls.py` — parse `x.com`/`twitter.com` status urls, extract tweet ids, flag
   `t.co` shorts and reserved paths (no false-positive profiles).
2. `fx.py` — async `api.fxtwitter.com` client with the required token query and
   `t.co` resolution.
3. `models.py` — photo variant ladder, fx video formats, yt-dlp ladder merge,
   gallery/album grouping, size propagation.
4. `client.py` — `TwitterClient` on top of `core.BaseClient`, with the yt-dlp
   fallback (`media_api="off"`) and size probes.
5. Tests — `tests/test_twitter_offline.py` (9 unit tests) and
   `tests/test_twitter_live.py` (11 live tests).

### Verification commands

```bash
python tests/test_twitter_offline.py    # 9 unit tests, no network
python tests/test_twitter_live.py       # 11 live tests (set TW_TEST_DOWNLOAD=0 to skip bytes)
```

### Suggested next steps

- Optional authenticated back end (X API v2) for a fixup-api-independent source.
- Multi-tweet/thread collection.
- Quoted-tweet media.