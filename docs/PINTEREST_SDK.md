# Pinterest Downloader SDK

`downloader/pinterest/` resolves **pins**, **idea (story) pins**, **image pins**,
**video pins** and **boards** into the shared `PostMetadata` model, and exposes
the *real* `i.pinimg.com` / `v1.pinimg.com` urls Telegram can fetch directly.

```python
from downloader.pinterest import PinterestClient

async with PinterestClient() as pin:
    meta = await pin.get_metadata("https://www.pinterest.com/pin/1084663891475263837/")
    print(meta.media_type, meta.media_group_type, meta.size_human)
    print(meta.links())            # {'video': ['https://v1.pinimg.com/videos/mc/720p/....mp4']}
    print(meta.urls())             # the same links, flat
    files = await pin.save(await pin.download(meta, quality="best"), "downloads")
```

Or through the facade, which routes `pinterest.<tld>` and `pin.it` here
automatically:

```python
from downloader import Downloader

async with Downloader() as dl:
    meta = await dl.get_metadata("https://pin.it/3fJd2lQ")
```

---

## 1. Architecture

```
url ──► urls.parse_url ──► PinterestRef(kind=pin|board|profile|short)
                                    │
                  short ────────────┴─► follow redirect (pin.it) ──► retry
                                    │
        pin ──► api.PinterestAPI.pin ──► pin_to_metadata() ──► PostMetadata
        board ─► api.PinterestAPI.board_feed ─► board_to_metadata() ─► PostMetadata
                                    │
                    get_metadata() ─┴─► client._add_video_ladder()  (pin urls only)
                    get_metadata() ───► client._fill_sizes()        (HEAD probe)
```

### Why a private API instead of only yt-dlp?

Pinterest serves pins from an internal JSON endpoint
(`/resource/PinResource/get/?data=...`). It is stable, key-less and returns the
whole rendition ladder — the yt-dlp extractor returns one video url and no
images at all. So Pinterest uses **both**:

* `api.py` — the resource endpoint, used for pin *and* board payloads. It is what
  makes image pins, story pages and boards work, and it is the source of the
  original post's own metadata (title, description, saves, comments).
* `yt-dlp` — the video quality ladder, used only when a pin url is resolved
  directly. Boards skip it on purpose: one extraction per pin would be far too
  slow, so board items carry the API's single progressive rendition and a
  warning. Open the individual pin to get the full ladder.

### Key design decisions

* **`--<id>` pins.** Pinterest appends a slug to the id
  (`/pin/bird-lover--1145110643587112231/`); `pin_id_of` takes the *last*
  numeric group, so both the bare and slugged forms work.
* **~40 country TLDs.** `HOST_RE` covers `pinterest.co.uk`, `pinterest.de`,
  `pinterest.com.mx`, … not just `.com`.
* **Manifests are opt-in.** HLS `V_HLSV3_MOBILE` renditions are dropped when a
  progressive copy of the same height exists (`include_manifest_formats=False`),
  because Telegram's url fetch cannot consume an m3u8.
* **Sizes before download.** `probe_sizes=True` (the default) sends a `HEAD`
  request per rendition and fills `MediaFormat.size_bytes`, so a bot can decide
  what to fetch — or refuse — before spending bandwidth.
* **Duplicate resolutions are real.** Pinterest reports `640x1138` for five
  genuinely different renditions of the same video. `format_preference()` in
  `downloader/core/models.py` therefore breaks ties on file size as a last
  resort.

### Public API

| member | purpose |
| --- | --- |
| `PinterestClient.get_metadata(url)` | metadata for a pin, board or short link |
| `PinterestClient.download(meta, quality=...)` | fetch the bytes |
| `PinterestClient.save(result, dest)` | write a `DownloadResult` to disk |
| `PinterestClient.preview_url(meta)` | a thumbnail/cover url for previews |
| `PinterestClient.plan_summary(meta)` | human readable plan, one line per item |
| `downloader.pinterest.get_metadata/download/save` | module-level shortcuts |

### Configuration (`PinterestConfig`)

| field | default | effect |
| --- | --- | --- |
| `include_images` / `include_videos` | `True` | disable a kind entirely |
| `photo_quality` | `"orig"` | which rendition ranks first (`736x`, `474x`, …) |
| `max_image_candidates` | `5` | how many renditions to expose per photo |
| `include_story_pages` / `max_story_pages` | `True` / `20` | idea-pin expansion |
| `resolve_short_links` | `True` | follow `pin.it` |
| `resolve_boards` / `board_max_items` | `True` / `25` | board support |
| `include_manifest_formats` | `False` | keep HLS renditions |
| `include_source_link` | `True` | keep the outbound link in `metadata.extra` |
| `probe_sizes` | `True` | `HEAD` probe every rendition for its real size |
| `cookiefile`, `extractor_args` | `None` / `{}` | pass-through to the yt-dlp video ladder |

---

## 2. Coverage (verified live)

| input | result |
| --- | --- |
| `pinterest.com/pin/1145110643587112231/` | image pin, `736x1307` original, 5 renditions, 163.58 KB probed |
| `pinterest.com/pin/1084663891475263837/` | video pin, `V_EXP7` → `/720p/` mp4, `640x1138`, h264+aac, 4.03 MB, needs no muxing |
| `pinterest.com/mashal0407/cool-diys/` | board → `media_group_type=playlist`, 5 items |
| `pin.it/<code>` | short link followed to the underlying pin |

`tests/test_pinterest_live.py` reproduces all of the above:

```bash
python tests/test_pinterest_live.py                 # metadata + download + save
$env:PIN_TEST_DOWNLOAD="0"; python tests/test_pinterest_live.py   # metadata only
```

---

## 3. Limitations (read this before shipping)

1. **Boards are shallow.** `board_feed` is paginated; only the first
   `board_max_items` (25) pins are listed, and board items get the API's single
   video rendition rather than the yt-dlp ladder. The metadata carries a warning
   saying so.
2. **The original image size is not always the largest file.** Pinterest's
   `736x` rendition can be a *bigger* jpeg than `orig` (verified: 187.6 KB vs
   163.6 KB). Selection ranks the `orig` name first on purpose; use
   `photo_quality` if you would rather rank by size.
3. **Video metadata comes from an undocumented endpoint.** A Pinterest change
   can break pin/board resolution until this SDK is updated. The yt-dlp path
   covers pin urls only.
4. **Pinterest reports the same resolution for several renditions.** Never key a
   cache on `(width, height)`; use `format_id`.
5. **`.m3u8` covers are not usable by Telegram.** HLS renditions are dropped by
   default for that reason.
6. **No login.** Secret/private boards and mature-content pins need cookies;
   pass `cookiefile=` to `PinterestConfig` if you have them.
7. **Pins are frequently deleted.** Treat a 404 as a normal, expected outcome —
   `PinNotFoundError`/`PinUnavailableError` are raised for exactly that.

---

## 4. Implementation plan (what was done, in order)

1. `urls.py` — host regex over ~40 TLDs, `pin.it` short hosts, and a
   `PinterestRef` covering pin / board / profile / short, including slugged pins.
2. `api.py` — the resource endpoint client (`PinResource`, `BoardResource`,
   `BoardFeedResource`) with the `X-Pinterest-PWS-Handler` header Pinterest
   expects, plus short-url resolution.
3. `models.py` — pure functions turning payloads into `MediaFormat`/`MediaItem`/
   `PostMetadata`: the image rendition ladder, `best_variant`, the API fallback
   ladder, the yt-dlp ladder and `attach_video_formats`, and the pin/board
   mappers.
4. `client.py` — `PinterestClient` tying it together, plus the size probe and
   the preview/plan helpers.
5. `config` / `exceptions` / `__init__` — tunables, the error hierarchy and the
   module shortcuts.
6. Facade + bot wiring: `Platform.PINTEREST`, `pinterest_options` in
   `Downloader`, the host in `bot/filters/links.py`, a caption branch in
   `bot/ui/descriptions.py` and a platform row in `platform_settings`.
7. `tests/test_pinterest_offline.py` (10 tests) and
   `tests/test_pinterest_live.py` (7 tests).