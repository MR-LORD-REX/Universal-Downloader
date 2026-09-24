# YouTube Downloader SDK

An async, typed YouTube downloader built on **yt-dlp** for extraction and a
**native streaming engine** for the bytes. Every rendition yt-dlp reports is
exposed with its **exact size before you download**, videos are muxed back
together with ffmpeg when YouTube serves the audio separately, and the same
metadata/quality/save grammar is used everywhere.

```python
import asyncio
from downloader.youtube import YouTubeClient

async def main():
    async with YouTubeClient() as yt:
        meta = await yt.get_metadata("https://youtu.be/FOUwd1h_jF4")

        meta.title                      # '【手书 HonkaiStarRail】...'
        meta.author                     # '羽'
        meta.media_type                 # MediaKind.VIDEO
        meta.media_group_type           # MediaGroupType.SINGLE
        meta.duration                   # 266.0
        meta.links()                    # {'video': ['https://rr4---...googlevideo.com/videoplayback?...']}
        meta.size_human                 # '57.64 MB'  <- before downloading

        result = await yt.download(meta, quality="1080p")
        await result.save("downloads")  # -> downloads/youtube_FOUwd1h_jF4_1_1080p_avc1.mp4

asyncio.run(main())
```

---

## 1. Architecture

```
downloader/youtube/
├── __init__.py       public surface (YouTubeClient, config, errors, helpers)
├── client.py         YouTubeClient: get_metadata / download / subtitles / CLI helpers
├── config.py         YouTubeConfig (formats, dubs, subtitles, backend, ranges)
├── exceptions.py     error hierarchy (ExtractionError, CommunityPostError, ...)
├── urls.py           video / shorts / playlist / channel / community-post parsing
├── extract.py        yt-dlp bridge + the `-f`/`-S` selector grammar
├── models.py         yt-dlp info dict -> MediaItem/MediaFormat (the "brain")
└── posts.py          `youtube.com/post/<id>` scraper (yt-dlp cannot handle them)
```

It sits on the shared [`downloader/core`](../downloader/core) layer (models,
HTTP client with size probes, ffmpeg bridge, download engine, saver) and reuses
the quality/planning code from `core/select.py`, so a YouTube plan looks exactly
like a Reddit or Twitter plan.

### Data flow

1. `parse_url(url)` classifies the link (`video`, `shorts`, `playlist`,
   `channel`, `post`) and normalises it to a `watch?v=`/`post/<id>` canonical.
2. `get_metadata` calls **yt-dlp with `download=False`** and translates the info
   dict into a canonical `PostMetadata`:
   - every `formats[]` entry becomes a `MediaFormat` (kind, codec, container,
     resolution, fps, size, protocol, http headers);
   - dubbed audio tracks are collapsed to the original/default one
     (`select_audio_tracks`);
   - `m3u8_native` duplicates are dropped when a progressive copy of the same
     resolution + codec family exists;
   - captions and thumbnails become `MediaFormat`s / `Thumbnail`s.
3. Missing sizes are confirmed with a ranged `HEAD`/`GET` probe.
4. `download` builds a plan, streams the chosen renditions through the shared
   engine, and **muxes video+audio with ffmpeg** when they are separate.

### Key design decisions

| Decision | Why |
| --- | --- |
| yt-dlp for extraction, not download | Its extractor is the only reliable source of signed CDN urls, and `download=False` keeps metadata cheap. Downloading is delegated to our engine so quality/size/progress/save behave like every other platform. |
| Native streaming by default (`backend="auto"`) | Gives exact byte progress, muxing, memory-or-disk targets, size gating and resumable ranged fetches. `backend="ytdlp"` is available as a fallback. |
| Ranged downloads (`ranged_download=True`) | YouTube throttles a single un-ranged `GET` to ~13 KB/s; bounded `Range` requests run at 1.5–3 MB/s. |
| Dub collapsing | A dubbed video reports one audio format **per language** (`139-0`..`139-N`). Keeping them all multiplies the ladder for no benefit, so the original track survives unless configured otherwise. |
| Manifest dedupe on resolution+codec | yt-dlp reports each rendition twice (progressive `https` with an exact size + `m3u8_native` with no size). Dropping the manifest copy keeps every remaining entry sized. |
| Community-post scraper | yt-dlp fails on `youtube.com/post/<id>` ("does not have a tab"), so the page's `ytInitialData` is parsed instead. |
| Url TTL re-extraction | googlevideo urls are signed and expire (~30 min). `url_ttl` triggers a re-extract before downloading stale metadata. |

### Public API

```python
YouTubeClient(config=None, *, progress_callback=None, **overrides)

# metadata
await client.get_metadata(url, *, playlist_limit=None, probe_sizes=None)
await client.save_metadata_json(metadata, dest)
client.available_qualities(metadata)          # ['1080p_avc1', '720p_vp9', ...]

# download
await client.download(metadata, *, quality="best", target="memory"|"disk",
                      dest=None, pattern=None, progress=None, only=None,
                      include_audio=None, max_size_bytes=None, temp_dir=None,
                      container=None, codec=None, backend=None, refresh="auto")
await client.download_by_url(url, **kwargs)
await client.download_subtitles(metadata, dest, *, languages=None,
                                convert_to_srt=False, overwrite=False)
```

`PostMetadata` is the canonical model shared by every platform:

| Field / method | Meaning |
| --- | --- |
| `platform`, `id`, `url`, `title`, `author`, `channel` | Identity |
| `media_type`, `media_group_type` | What the post is (`video`/`image`/`playlist` …) |
| `items` | One `MediaItem` per downloadable asset |
| `item.formats` | Every rendition (`video`, `audio`, `muxed`, `storyboard`, `subtitle`) |
| `item.video_formats` / `audio_formats` / `image_formats` | Filtered views |
| `links(quality="best")` | `{kind: [direct CDN url, ...]}` — the urls reddit/YouTube/Twitter serve the file from |
| `all_links()` | Every url, no quality filtering |
| `size_human` / `size_bytes` / `size_is_approx` | Size before downloading |
| `size_for(quality)` / `size_breakdown(quality)` | Per-quality size (video + audio) |
| `format_for(quality)` | The chosen `MediaFormat`s |
| `subtitle_languages` / `subtitles_for(lang)` | Captions |
| `to_dict()` / `summary()` / `describe()` | Serialisation helpers |

### Configuration (`YouTubeConfig`)

```python
YouTubeConfig(
    # formats
    include_manifest_formats=False, include_storyboards=False,
    include_all_audio_languages=False, audio_languages=[],
    prefer_short_side=True,              # label vertical/shorts by the short side
    # subtitles
    include_subtitles=True, include_auto_captions=True, subtitle_languages=[],
    # extractor
    player_client=None, cookiefile=None, cookies_from_browser=None,
    extra_ytdlp_options={},
    # fetching
    download_backend="auto",             # auto | native | ytdlp
    url_ttl=1800.0, playlist_max_items=25, resolve_playlists=True,
    community_post_extraction=True,
    ranged_download=True, range_chunk_size=4 * 1024 * 1024,
    # quality (inherited from PlatformConfig)
    prefer_video_codec="avc1", prefer_container="mp4", prefer_audio_codec="mp4a",
    mux_audio=True, verify_audio=True, probe_sizes=True, max_size_bytes=None,
)
```

---

## 2. Quality grammar

`quality` accepts:

| Form | Meaning |
| --- | --- |
| `"best"` / `"worst"` | Highest / lowest usable rendition (muxed automatically) |
| `1080` / `"1080p"` | Closest height at or below the target (Telegram-safe codec/container preferred) |
| `"audio"` | Best audio-only track |
| `"137+140"` | Explicit video id + audio id (pair mode) |
| `"18"` | An explicit format id when it is not 3–4 digits |
| `"bestvideo+bestaudio"` | Standard yt-dlp selector pass-through |

> **Note:** a bare 3–4 digit string such as `"137"` is parsed as a **height**
> (`137p`), because that is the useful reading for `"1080"`. Use `"137+140"` to
> pin an exact format pair.

Extra kwargs (`container=`, `codec=`, `include_audio=`, `max_size_bytes=`,
`only=[...]`) refine the pick; `prefer_video_codec="avc1"` + `prefer_container="mp4"`
keep output Telegram-friendly (H.264 + AAC in MP4).

---

## 3. Coverage (verified live)

| Post type | Metadata | Sizes | Download | Notes |
| --- | --- | --- | --- | --- |
| Long video (adaptive) | ✅ | ✅ exact | ✅ muxed | 18 video + 5 audio renditions, all sized |
| Shorts (vertical) | ✅ | ✅ | ✅ | 4K vertical supported; dubs collapsed to the original |
| Playlist / channel | ✅ | ✅ | ✅ | capped by `playlist_max_items` |
| Community post (image) | ✅ | ✅ | ✅ | scraped from `ytInitialData`, full-resolution PNG |
| Captions | ✅ | n/a | ✅ `.vtt` | auto-captions; may be rate-limited |

Live numbers (2026-09):

- `FOUwd1h_jF4`: best `1080p_avc1` = 57.64 MB, plan `137 + 251` = 60.44 MB; a 720p probe matched the metadata byte-for-byte.
- `sUVemoSeY10` (shorts): 2160×3840 available; original dub kept out of 105 audio entries.
- Community post: 1 image, 2.01 MB PNG.

---

## 4. Limitations (read this before shipping)

- **Signed urls expire.** googlevideo urls carry an `expire=` (~30 min). The SDK
  re-extracts when metadata is older than `url_ttl`, but you should not cache
  `links()` beyond that window.
- **`filesize` is not always present.** yt-dlp omits sizes for some storyboards
  and manifest-only renditions; the SDK probes the ones it keeps, but a
  manifest-only live stream may still report an approximate size.
- **Community posts expose `yt3.ggpht.com` thumbnails**, not the author's true
  original (400 KB–2.5 MB). That is the largest copy YouTube serves publicly.
- **Captions are rate-limited.** Auto-caption requests can return HTTP 429; the
  downloader skips those instead of failing.
- **Po-token / bot checks.** Some videos need a cookies file or a specific
  `player_client`; set `cookiefile=` or `player_client=` when extraction fails.
- **Live streams** are not recorded (metadata is returned with a warning).
- **Ages/regions/DRM**: yt-dlp limitations apply unchanged.
- **Long videos are large.** `max_size_bytes` (or `YouTubeConfig.max_size_bytes`)
  is the premium gate — it is enforced *before* downloading and again mid-stream.
- **yt-dlp must be installed** (`pip install -r requirements.txt`) and kept
  reasonably current; extraction breaks when YouTube changes its player.

---

## 5. Implementation plan (what was done, in order)

1. `urls.py` — classify every YouTube link shape (video, shorts, playlist,
   channel, community post) and normalise to a canonical url.
2. `models.py` — map the yt-dlp info dict onto the shared `PostMetadata`:
   formats, sizes, thumbnails, captions, dub collapsing, manifest dedupe.
3. `extract.py` — a thin async yt-dlp wrapper plus the `-f`/`-S` selector
   grammar used by the `ytdlp` backend.
4. `client.py` — `YouTubeClient` on top of `core.BaseClient`: metadata,
   native download with size gating, `ytdlp` fallback backend, subtitles.
5. `posts.py` — the community-post scraper yt-dlp cannot replace.
6. Tests — `tests/test_youtube_offline.py` (11 unit tests) and
   `tests/test_youtube_live.py` (14 live tests).

### Verification commands

```bash
python tests/test_youtube_offline.py    # 11 unit tests, no network
python tests/test_youtube_live.py       # 14 live tests (set YT_TEST_DOWNLOAD=0 to skip bytes)
```

### Suggested next steps

- Cookie/player-client auto-retry when YouTube ramps up bot checks.
- Recording live streams (currently metadata-only).
- Channel/playlist pagination beyond `playlist_max_items`.