# Reddit Downloader SDK

An async, typed toolkit for turning any Reddit post into downloadable media:
images, gifs, hosted videos (all renditions), galleries/albums and external
links - with **metadata and file sizes available before a single byte is
downloaded**.

```python
import asyncio
from downloader.reddit import RedditClient

async def main():
    async with RedditClient() as client:
        meta = await client.get_metadata("https://redd.it/1basx0i")

        meta.title                      # 'Is it possible to transfer the detail ...'
        meta.author                     # 'miiMiller'
        meta.media_type                 # MediaKind.GALLERY
        meta.media_group_type           # MediaGroupType.GALLERY
        meta.links()                    # {'image': ['https://i.redd.it/...png', ...]}
        meta.size_human                 # '1.14 MB'  <- before downloading

        result = await client.download(meta, quality="best")
        await result.save("output")     # -> output/miiMiller_1basx0i/...

asyncio.run(main())
```

---

## 1. Architecture

```
downloader/reddit/
├─ __init__.py           public surface (RedditClient, models, errors)
├─ client.py             RedditClient facade: get_metadata / download / save
├─ reddit.py             legacy alias (Reddit == RedditClient)
├─ __main__.py           `python -m downloader.reddit <url>` CLI
├─ config.py             RedditConfig (auth, http, probing, naming, cache)
├─ exceptions.py         error hierarchy
├─ urls.py               URL/ID parsing, share-link detection, helpers
├─ http.py               aiohttp pool: retries, throttling, size probes, streams
├─ manifests.py          DASH (.mpd) + HLS (.m3u8) parsers
├─ formats.py            reddit payload -> MediaItem/MediaFormat (the "brain")
├─ downloader.py         concurrent streaming download + ffmpeg muxing + HLS
├─ saver.py              filename patterns, album folders, overwrite policy
├─ models/
│  ├─ enums.py           MediaKind, MediaGroupType, FormatKind, FormatOrigin
│  ├─ media.py           MediaFormat, MediaItem, MediaGroup, DownloadedFile
│  ├─ post.py            PostMetadata, VideoInfo, CrosspostInfo, DownloadResult
│  └─ progress.py        ProgressEvent / ProgressPhase + callback plumbing
└─ providers/
   ├─ base.py            MetadataProvider contract
   ├─ oauth.py           official API (oauth.reddit.com) - highest fidelity
   ├─ arctic_shift.py    public archive (no keys required)
   ├─ rss.py             reddit's Atom feed (survives some network blocks)
   ├─ oembed.py          title/author/thumbnail only
   ├─ manual.py          payloads the caller already has
   └─ direct.py          the url already *is* the media
```

### Data flow

```
url ──▶ urls.parse ──▶ PostRef ──▶ provider chain ──▶ reddit-shaped payload
                                          │
                                          ▼
                              formats.build_items  ──▶ MediaItem[]
                                          │
                    manifests + HEAD probes (enrich)  ──▶ every format + size
                                          │
                                       PostMetadata  ──▶ links()/groups/sizes
                                          │
                             downloader ──▶ DownloadResult ──▶ saver ──▶ files
```

### Key design decisions

* **One payload shape everywhere.** Every provider must return the same
  reddit-style dict (`id`, `title`, `media_metadata`, `secure_media`, ...), so
  item building, format expansion and sizing are source agnostic. Providers can
  therefore be mixed freely and merged field-by-field when one of them is
  partial.
* **Providers are tried in order and merged.** Default order is
  `oauth → arctic_shift → rss → oembed`. The chain stops as soon as the merged
  payload is "rich" (carries media info), which keeps the common path at a
  single request while still repairing partial results.
* **Media kinds vs. group kinds.** `media_type` describes the asset
  (`image`, `gif`, `video`, `gallery`, `external_image`, ...) while
  `media_group_type` describes the post layout (`single`, `gallery`,
  `crosspost`, `external`, `none`). `groups` buckets items by asset kind, which
  is the "links grouped by type" view.
* **Sizes are a first-class feature.** `MediaFormat.size_bytes` is filled by
  `HEAD` (falling back to a one-byte `Range` GET for CDNs that reject `HEAD`),
  concurrently and cached in memory + `cache_dir/sizes.json`.
  `MediaItem.size_bytes` is the size of its best format;
  `PostMetadata.size_bytes` is the size of the **default download plan**
  (so a video reports video + audio).
* **Never lie about formats.** A rendition only appears if it actually exists:
  DASH/HLS manifests are parsed, and when no manifest is served the SDK probes
  the known `v.redd.it` filename ladder. Streaming pages (YouTube, Vimeo) are
  reported with **zero formats** so `download()` raises instead of failing
  halfway.
* **Audio is handled explicitly.** `v.redd.it` serves video-only MP4s
  (`CMAF_720.mp4`) plus separate audio (`CMAF_AUDIO_128.mp4`). The downloader
  fetches both and muxes them with the bundled ffmpeg
  (`imageio-ffmpeg`), falling back to two files when ffmpeg is missing.
* **Memory or disk.** `target="disk"` streams into a scratch directory and
  `save()` *moves* the finished files into place, which is what a bot wants for
  large videos.

### Public API

| Method | Purpose |
| --- | --- |
| `RedditClient.get_metadata(url, *, post_id, data, include_sizes, expand_formats, include_previews, providers, progress)` | Full `PostMetadata`, no media transferred |
| `RedditClient.download(source, *, quality, include_audio, mux, target, dest, pattern, progress, only)` | `DownloadResult` (bytes or temp files) |
| `RedditClient.save(source, dest, *, pattern, overwrite, album_dir, save_info)` | Download if needed, then write to disk |
| `RedditClient.metadata_from_json(payload)` | Offline metadata from JSON you already have |
| `RedditClient.close()` / `async with` | Close pools + drop scratch files |
| `DownloadResult.save(...)` / `.free()` | Write / release |
| `downloader.reddit.get_metadata / download / save` | One-shot helpers |

Quality policies: `"best"`, `"worst"`/`"smallest"`, `720` (int) or `"1080p"`.
An integer target means *the largest rendition at or below that ladder height*;
if everything is bigger, the smallest rendition is used so a download always
succeeds.

Filename patterns accept `{id} {author} {subreddit} {title} {index} {count}
{quality} {ext} {kind} {date} {media_id} {width} {height} {provider}`.

### Configuration

```python
from downloader.reddit import RedditConfig, RedditClient

config = RedditConfig.from_env()          # REDDIT_CLIENT_ID / _SECRET / _REFRESH_TOKEN ...
config = RedditConfig(
    providers=["arctic_shift", "rss"],    # skip oauth
    probe_sizes=True,
    expand_formats=True,
    max_download_concurrency=4,
    mux_audio=True,
    default_pattern="{subreddit}_{id}_{index}_{quality}.{ext}",
    cache_dir=".cache/reddit",
)
```

---

## 2. Post-type coverage (verified live)

| Post shape | media_type | group | Formats exposed | Download |
| --- | --- | --- | --- | --- |
| Image (`i.redd.it/*.jpeg|png`) | `image` | `single` | original + optional previews | ✅ single file |
| GIF (`i.redd.it/*.gif`) | `gif` | `single` | original gif + optional mp4 render | ✅ single file |
| Hosted video (modern CMAF) | `video` | `single` | every `CMAF_<h>.mp4` + every `CMAF_AUDIO_<b>.mp4` from the MPD | ✅ muxed mp4 |
| Hosted video (legacy DASH) | `video` | `single` | `DASH_<h>.mp4` + `DASH_AUDIO_<b>.mp4` | ✅ muxed mp4 |
| HLS-only video | `video` | `single` | `CMAF_<h>.m3u8` renditions | ✅ segments concatenated |
| Gallery / album (images) | `gallery` | `gallery` | one item per slide, original + renditions | ✅ album folder |
| Gallery with gif/video slides | `gallery` | `gallery` | `s.gif` / `s.mp4` / `i.redd.it` originals | ✅ |
| Crosspost | inherited | `crosspost` | original post's media + `crosspost.parent` | ✅ |
| Direct `i.redd.it` / `v.redd.it` url | `image` / `video` | `single` | probed/expanded | ✅ |
| Imgur direct file | `external_image` | `external` | the file | ✅ |
| External video page (YouTube) | `external_video` | `external` | none (by design) | ❌ use yt-dlp |
| Self / link / poll post | `text` / `link` / `poll` | `none` | none | `NoMediaError` |

---

## 3. Limitations (read this before shipping)

**Metadata availability**

1. **Reddit's own JSON API is blocked for anonymous, non-browser traffic.** In
   the environment where this SDK was built, every `*.json` request (and
   `old.reddit.com`) returned HTTP 403 with *"You've been blocked by network
   security"*. The SDK therefore defaults to community sources.
2. **No OAuth credentials ⇒ galleries are best-effort.** The default
   `arctic_shift` provider is a third-party archive. It carries
   `media_metadata` for most posts, but its coverage is not guaranteed - some
   posts (including recently ingested ones) come back without gallery data, and
   then the SDK reports `media_type=gallery` with `items=[]` plus a warning
   telling you to configure OAuth. With a Reddit app (client id/secret +
   refresh token) the `oauth` provider is authoritative and always complete.
3. **RSS is rate limited (HTTP 429)** and only yields title, author, timestamp
   and the post's `[link]` url - never galleries, durations or renditions.
4. **oEmbed yields a title and an author.** Nothing else - reddit stopped
   returning `thumbnail_url` for most posts, so no media url can be derived
   from it.
5. **Deleted/quarantined/private posts** may exist in none of the sources.
   `PostNotFoundError` lists every provider that was tried.
6. Archive freshness: the community mirror lags reddit by seconds to minutes,
   so a brand-new post can be missing from it (RSS/oEmbed usually still work).

**Media handling**

7. **Audio/video muxing needs ffmpeg.** `imageio-ffmpeg` (already a dependency
   of `moviepy` in this repo) is detected automatically; on a machine without
   any ffmpeg the SDK returns the video and audio as two separate files and
   notes it in `DownloadedFile.note`.
8. **Reddit never serves a single muxed file for hosted video** - `CMAF_*.mp4`
   is video only, so "one file" always means "downloaded then remuxed".
9. **HLS (`m3u8`) downloads are assembled in memory** segment by segment. Fine
   for reddit's short clips, wasteful for very long videos; prefer the mp4
   renditions (the SDK does, by default).
10. **External hosts are out of scope.** YouTube/Vimeo/Twitch pages are
    reported as `external_video` with no formats. Imgur *albums*
    (`imgur.com/a/...`) are also not enumerated - only direct image files.
11. **Reddit-hosted video urls can expire.** `media.reddit_video.dash_url` in
    archived payloads carries a signed query; the SDK strips it and uses the
    stable path (`/<id>/DASHPlaylist.mpd`), which currently works without a
    signature. If reddit changes that, use fresh OAuth metadata.
12. **Sizes can be unknown.** `size_bytes` stays `None` when the CDN omits
    `Content-Length`; `metadata.sizes_known` tells you whether every item
    resolved.
13. **`quality` targets a rendition ladder, not the frame height.** A vertical
    "1080p" video has frame height 1920; `media_type`/`quality_label` report the
    ladder (1080p) while `MediaFormat.width/height` report the real frame.

**Legal / operational**

14. Downloaded content belongs to its creators. Respect copyright, subreddit
    rules and reddit's terms; do not use this to mass-scrape or to redistribute
    other people's media.
15. **Be polite.** Defaults keep concurrency low, add jittered backoff on
    `429/5xx` and honour `Retry-After`. Raising `max_download_concurrency`
    aggressively against `v.redd.it`/`i.redd.it` will get you throttled.
16. Reddit can change any of these internal endpoints at any time. Because
    providers are pluggable (`providers/base.py`), adding or repairing a source
    is a single small file, and `RedditClient.get_metadata(data=...)` lets you
    inject your own payloads meanwhile.

---

## 4. Implementation plan (what was done, in order)

1. **Reconnaissance** - mapped reddit's media surface and probed what is
   reachable from this network: `i.redd.it` ✅, `v.redd.it` ✅ (with `HEAD`
   sizes), `preview.redd.it`/`external-preview.redd.it` ✅ (signed), reddit
   `*.json` ❌ 403, `old.reddit.com` ❌ login wall, `api.reddit.com` ❌,
   `oauth.reddit.com/api/v1/access_token` ✅ (so OAuth works),
   `/comments/<id>/.rss` ✅ (rate limited), `oembed` ✅, arctic-shift API ✅.
   Captured the exact payload schema for images, gifs, CMAF and legacy DASH
   videos, galleries (`media_metadata` + `gallery_data`), crossposts and
   external links.
2. **Foundations** - `config`, `exceptions`, `urls`, `models/*`.
3. **HTTP layer** - pooled aiohttp session, throttling, exponential backoff,
   case-insensitive response headers, `HEAD`/`Range` size probing with a TTL
   cache, resumable streaming with `Range`-based resume after a mid-body error.
4. **Payload → items** - `formats.py` (image/gif/video/gallery/crosspost/
   external builders, preview renditions, gallery original derivation
   `i.redd.it/<media_id>.<ext>`) and `manifests.py` (MPD + m3u8 parsers).
5. **Providers** - oauth (token cache + refresh on 401), arctic-shift, RSS,
   oEmbed, direct, manual + registry/chain builder.
6. **Downloader** - concurrent plan execution, progress throttling, `memory`
   and `disk` targets, HLS segment assembly, ffmpeg muxing.
7. **Saver** - pattern rendering, sanitising, album directories, non-clobbering
   writes, metadata sidecars.
8. **Client + CLI + docs**.
9. **Tests** - 15 offline unit tests and 17 live integration tests over a
   corpus spanning every post type above.

### Verification commands

```bash
.venv/Scripts/python.exe tests/test_reddit_offline.py     # no network
.venv/Scripts/python.exe tests/test_reddit_live.py        # real posts + CDN
REDDIT_TEST_DOWNLOAD=0 .venv/Scripts/python.exe tests/test_reddit_live.py   # metadata only
.venv/Scripts/python.exe -m downloader.reddit https://redd.it/1basx0i --describe -d output
```

### Suggested next steps

* Add OAuth (script app) credentials to get 100 % gallery coverage and a live
  view of every post; the provider is already implemented, it just needs keys.
* Optional `yt-dlp` bridge for `external_video` items.
* Imgur album resolution for `imgur.com/a/...` links.
* A subreddit listing/feed API (`downloads/new`, `downloads/top`) reusing the
  same provider + item pipeline.
* Optional on-disk resume for interrupted large downloads (the streaming layer
  already supports `Range`, it just needs a persisted `.part` file).