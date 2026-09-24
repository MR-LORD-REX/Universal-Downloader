# Telegram downloader bot + multi-platform downloader SDK

An async, fully typed downloader toolkit for a Telegram bot: **metadata and
file sizes before you download**, direct CDN links for static files, automatic
ffmpeg muxing of the separate video/audio tracks these platforms serve, and a
`save()` that writes wherever you want.

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
        meta.size_human         # '57.64 MB'   <- before downloading anything

        allowed, size = dl.affordable(meta, 50 << 20)     # premium gate
        result = await dl.download(meta, quality="720p", max_size_bytes=50 << 20)
        await result.save("downloads")                    # -> downloads/youtube_FOUwd1h_jF4_1_720p_avc1.mp4

asyncio.run(main())
```

One facade, one canonical `PostMetadata`/`DownloadResult` for every platform:

* `get_metadata(url)` - title, author, `media_type`, `media_group_type`,
  items/**direct CDN links** grouped by kind, every rendition with its size,
  thumbnails, captions, NSFW/spoiler flags, crosspost parent, raw payload.
* `download(...)` - `best`/`worst`/`720`/`"1080p"`/`"audio"`/`"137+140"`, memory or
  disk, concurrent, **muxes video+audio with ffmpeg**, progress callbacks, and a
  `max_size_bytes` gate enforced before *and* during the download.
* `save(dest, pattern=...)` - `{platform}_{id}_{index}_{quality}.{ext}` patterns,
  album folders, non-clobbering writes.
* `fetch(url)` / `size_of(url)` - grab **any** direct link (images, thumbnails,
  generic CDNs) without spinning up a platform SDK: the bot's static-file fast path.

## Platforms

| Platform | Metadata | Direct links + sizes | Download | Extras |
| --- | --- | --- | --- | --- |
| **Reddit** | ✅ | ✅ | ✅ | images, gifs, DASH/CMAF video + separate audio, galleries, crossposts |
| **YouTube** | ✅ | ✅ | ✅ | videos, shorts, playlists, community posts, adaptive muxing, captions |
| **Twitter/X** | ✅ | ✅ | ✅ | video ladder, single photos, multi-image galleries, t.co links |

## Install

```bash
pip install -r requirements.txt
```

## The Telegram bot

`main.py` runs the bot: **aiogram v3** + **FastAPI** in one process, **SQLAlchemy**
over **SQLite** with **Alembic** migrations.

```bash
copy .env.example .env     # set BOT_TOKEN and OWNER_ID
python main.py             # polling + FastAPI on :8080
python main.py --mode webhook
```

* Send it a link in a DM or a group. Images and ready-to-play videos are handed
  to Telegram **by url** (nothing touches your disk); anything that needs muxing
  is queued, downloaded, muxed and uploaded.
* One **fetch queue per platform** (token-bucket paced) and one **RAM-bounded
  processing queue** (`PROCESSING_MAX_RAM_BYTES`).
* If the requested rendition is too big for the Bot API it **downgrades to the
  best rendition that fits** instead of failing.
* Albums become `send_media_group` with a per-platform caption; url delivery
  falls back to a server fetch when Telegram cannot get the file.
* `/admin` gives the owner users, groups, analytics, per-platform limits,
  broadcast and suggestions.
* Unknown links are ignored, the user's link message can be deleted, and
  `/suggestion` forwards feedback to the owner.

See [`docs/BOT.md`](docs/BOT.md) for the architecture, plan and limitations.

## Test

```bash
# offline unit suites (no network)
python tests/test_reddit_offline.py
python tests/test_youtube_offline.py
python tests/test_twitter_offline.py
python tests/test_orchestrator_offline.py
python tests/test_bot_offline.py

# live integration suites (real posts + real CDNs and a stubbed Telegram bot)
python tests/test_reddit_live.py
python tests/test_youtube_live.py
python tests/test_twitter_live.py
python tests/test_orchestrator_live.py
python tests/test_bot_live.py
```

Every live suite accepts `<PREFIX>_TEST_DOWNLOAD=0` to run metadata-only
(`REDDIT_TEST_DOWNLOAD`, `YT_TEST_DOWNLOAD`, `TW_TEST_DOWNLOAD`,
`ORCH_TEST_DOWNLOAD`), and reports network/rate-limit problems as `skip` instead
of failures.

```bash
python -m downloader.reddit https://redd.it/1basx0i --describe -d output
```

## Using one platform directly

The facade is usually enough, but each SDK stands alone:

```python
from downloader.youtube import YouTubeClient
from downloader.twitter import TwitterClient
from downloader.reddit import RedditClient

async with YouTubeClient() as yt:
    meta = await yt.get_metadata("https://youtube.com/shorts/sUVemoSeY10")
    print(yt.available_qualities(meta))
    await yt.download_subtitles(meta, "subs", languages=["en"])
```

## Docs

* [`docs/BOT.md`](docs/BOT.md) - the Telegram bot: queueing, routing, admin
  panel, data model, config reference and limitations.
* [`docs/ORCHESTRATOR.md`](docs/ORCHESTRATOR.md) - architecture, plan, Telegram
  integration recipe and the full limitation list.
* [`docs/REDDIT_SDK.md`](docs/REDDIT_SDK.md) - Reddit reference.
* [`docs/YOUTUBE_SDK.md`](docs/YOUTUBE_SDK.md) - YouTube reference.
* [`docs/TWITTER_SDK.md`](docs/TWITTER_SDK.md) - Twitter/X reference.

## Optional credentials

Reddit's public JSON API blocks anonymous requests from many networks, so the
SDK ships three key-less metadata sources (community archive, RSS, oEmbed) and
falls back to them automatically. A free Reddit "script" app unlocks the
official API, which is the only source that is complete for every post type:

```bash
set REDDIT_CLIENT_ID=...
set REDDIT_CLIENT_SECRET=...
set REDDIT_REFRESH_TOKEN=...
```

YouTube and Twitter work without any credentials. See
[`docs/ORCHESTRATOR.md`](docs/ORCHESTRATOR.md#3-limitations-read-this-before-shipping)
for what that means in practice.