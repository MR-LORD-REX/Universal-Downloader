# Telegram Downloader Bot — architecture, plan and limitations

An aiogram v3 bot that turns a link into delivered Telegram media. Images are
handed to Telegram by url, videos that already carry audio are handed over the
same way, and anything that needs muxing goes through a RAM-bounded processing
queue before upload.

```
   user message (link)
          |
   [LinkFilter]  unrecognised hosts are ignored silently
          |
   per-platform fetch queue      <- token bucket, resizable workers
          |
   Downloader.get_metadata()
          |
   plan_route()  ------------------------+
     |                                   |
     | images, photos, ready mp4         | video-only (adaptive/DASH),
     |                                  | oversized files, audio
     v                                   v
   MediaSender.send_plans()          processing queue  <- RAM budget
   (Telegram fetches the url)             |
                                          v
                                  SDK download + ffmpeg mux
                                          |
                                          v
                                  MediaSender.send_files()
```

---

## 1. Architecture

### 1.1 Processes and entry point

`main.py` is the only entry point. It runs **one** asyncio event loop that holds
both the aiogram dispatcher and a FastAPI app:

| mode | what happens |
| --- | --- |
| `BOT_MODE=polling` (default) | `dp.start_polling()` runs as a background task; FastAPI serves `/health`, `/api/stats`, `/api/platforms` |
| `BOT_MODE=webhook` | FastAPI exposes `POST ${WEBHOOK_PATH}` and Telegram pushes updates |

```bash
python main.py                     # polling on :8080
python main.py --mode webhook      # webhook
python main.py --set-commands      # publish /commands, then exit
python main.py --migrate           # alembic upgrade head, then exit
```

### 1.2 Package layout

| path | responsibility |
| --- | --- |
| `bot/config.py` | every knob, read from `.env` via pydantic-settings |
| `bot/db/` | SQLAlchemy 2.0 models, async session plumbing, repositories |
| `bot/alembic/` | migrations (`alembic.ini` at the repo root) |
| `bot/services/downloader_service.py` | one process-wide `downloader.Downloader` (proxy, cookies, timeouts) |
| `bot/services/routing.py` | **the product decision**: url vs server vs mux, plus size-aware quality choice |
| `bot/services/queues.py` | per-platform fetch queues + the RAM-bounded processing queue |
| `bot/services/sender.py` | albums, url delivery, server fallback, uploads |
| `bot/services/status.py` | throttled editing of the "working on it" message |
| `bot/services/broadcast.py` | rate-limited copy_message fan-out |
| `bot/services/commands.py` | `setMyCommands` for the default scope and each admin |
| `bot/ui/` | the symbol vocabulary, captions and admin screens |
| `bot/filters/links.py` | recognise supported links, ignore everything else |
| `bot/middlewares/` | context injection, one DB session per update, user/chat sync, bans |
| `bot/handlers/` | `start`, `links`, `suggestion`, `admin` routers |
| `bot/app.py` | `AppContext`: owns the services and implements both queue workers |

### 1.3 The delivery decision (`plan_route`)

For every item of a post the router picks a `(send_as, action)` pair:

| item | condition | result |
| --- | --- | --- |
| photo/gallery image | size ≤ 5 MB | `photo` by **url** |
| | 5 MB < size ≤ 10 MB | `photo`, **server** fetch + upload |
| | 10 MB < size ≤ 50 MB | `document`, **server** fetch + upload |
| | > 50 MB | **rejected** with the reason |
| gif | mp4/webm/gif | `animation` by url |
| video | rendition already has audio (progressive muxed mp4) | `video` by **url** |
| | video-only rendition (YouTube DASH, Reddit `CMAF_*.mp4`) | **mux** in the processing queue |
| | genuinely silent (Twitter gif, muted upload) | `video` by url, no audio promised |
| | muxed but > 50 MB | **rejected** (Telegram cannot upload it) |
| audio | standalone track | `audio`, server fetch + upload |

Instagram is the easiest platform for this pipeline: its default `best` resolves
to the **progressive H.264 + AAC mp4**, so a photo is a `photo` url and a reel is
a `video` url - neither needs the processing lane. Carousels become albums of
`photo` urls. The one prerequisite is `INSTAGRAM_SESSION_FILE`.

Anything above the admin's `max_media_size_bytes` is rejected per item, before
any bytes move — the reply names the item and both sizes.

### 1.4 Size-aware quality (`choose_quality`)

The configured quality is a *preference*, not a promise. If the requested
rendition would exceed Telegram's upload cap, the router walks the rendition
ladder downwards and delivers the tallest one that fits, then says so in the
caption:

```
▸ Quality · 720p
...
1080p was too large, delivered 720p instead
```

This is why a 116 MB 1080p tweet still produces a video instead of an error.

### 1.5 Queues

**Per-platform fetch queue** (`PlatformQueue`, one per platform)

* bounded by `fetch_queue_size`; a submission over the bound is refused with a message
* pacing by an async **token bucket** (`fetch_rate_per_second`)
* concurrency by a **resizable gate**, so admin edits apply live without dropping jobs
* fixed worker pool of 8 tasks that all wait on the gate

**Global processing queue** (`ProcessingQueue`)

* admission requires *all* of: global depth < `PROCESSING_QUEUE_SIZE`,
  that platform's depth < its `processing_queue_size`, and
  `reserved_ram + estimate <= PROCESSING_MAX_RAM_BYTES`
* the RAM estimate is `PROCESSING_RAM_FACTOR × planned bytes`
  (or `PROCESSING_DEFAULT_ESTIMATE_BYTES × items` when sizes are unknown)
* RAM is reserved at submit time and released when the job finishes
* when admission fails the user is told why (e.g. `not enough processing memory
  (needs 55.00 MB, 12.00 MB free)`)

Both queues are in-process. A restart drops queued work; the user simply
resends the link.

### 1.6 Message flow

1. `LinkFilter` extracts supported urls; unknown hosts never match, so the bot
   stays quiet. Private chats get a one-line hint instead.
2. `UserMiddleware` upserts the user and (in groups) the chat, records
   `has_dm_access` the first time a user talks to the bot in private, and drops
   everything from banned users/chats.
3. A small status message is created (`◷ 𝗬𝗼𝘂𝗧𝘂𝗯𝗲 · queued`) unless disabled.
4. The link is deleted if `DELETE_INCOMING_LINKS` is on and the bot may delete
   in that chat (the permission is learned once and remembered).
5. The fetch worker resolves metadata, routes, sends what it can immediately and
   queues the rest. The status message is deleted once media lands, or edited to
   the error when something fails.
6. Albums of 2–10 url media become a single `send_media_group`; larger galleries
   are chunked. Captions ride on the first item only.

### 1.7 Captions

Captions follow the project's Telegram formatting rules (Unicode symbols as UI,
no decorative emoji) and are per platform: YouTube shows channel/views/likes,
Twitter shows likes/reposts/replies, Reddit shows subreddit/upvotes/comments,
Instagram shows account / kind (Post, Reel or Carousel) / likes / comments / views.
They are HTML-escaped and clipped to Telegram's 1024 character caption limit.
See `bot/ui/descriptions.py`.

### 1.8 Url fallback

Whenever Telegram refuses to fetch a url (`failed to get HTTP URL content`,
`WEBPAGE_CURL_FAILED`, ...) the sender transparently retries that item through
the server path: download with `Downloader.fetch`, upload the bytes. Only if
both lanes fail does the user see an error.

### 1.9 Admin panel

`/admin` opens an inline-keyboard panel: users, groups, analytics, platforms,
broadcast, suggestions, runtime. Every screen is also a text command
(`/stats`, `/users`, `/chats`, `/platforms`, `/queues`, `/suggestions`,
`/broadcast`).

* **Users/groups** — browse, search by name/username/id, ban, unban, purge,
  DM a user who has started the bot.
* **Analytics** — requests, bytes, files sent, users/new/active, DM reach,
  per-platform breakdown and outcome mix over 24h / 7d / 30d.
* **Platforms** — enabled flag, default quality, per-item size limit, direct-url
  video limit, fetch queue size/workers/rate, processing queue size/workers/RAM.
  Edits are applied to the live queues immediately.
* **Broadcast** — choose DM users / groups / both, then send or forward the
  message to copy. Delivery is rate-limited and per-recipient status is stored.
* **Suggestions** — `/suggestion` from any user is stored and forwarded to the
  owner; the panel lists and marks them read.

### 1.10 Commands

`register_commands` publishes `/start /help /settings /suggestion` for everyone
and adds the admin set (`/admin /stats /broadcast /users /chats /platforms
/queues`) for each id in `OWNER_ID`/`ADMIN_IDS` — no manual BotFather step.

---

## 2. Data model

| table | holds |
| --- | --- |
| `users` | profile, `has_dm_access`, role, ban state, request counters |
| `chats` | title/type/username, `can_delete_messages`, ban state, counters |
| `memberships` | which user was seen in which chat |
| `platform_settings` | the admin-editable per-platform tunables |
| `usage_events` | one row per resolved request (platform, outcome, bytes, timing) |
| `suggestions` | user feedback and its read state |
| `broadcasts` / `broadcast_targets` | broadcast jobs and per-recipient delivery status |

Migrations live in `bot/alembic/versions`. `AUTO_MIGRATE=true` applies them at
start-up; `python main.py --migrate` does it manually.

---

## 3. Implementation plan (what was built, in order)

1. **Close the silent-video hole in the SDK first.** `DownloadEngine` checked
   `is_manifest` before `needs_mux`, so an HLS/DASH video rendition needing a
   separate audio track was downloaded video-only. Reordered, added audio
   verification with ffprobe (`verify_audio`), populated `DownloadOutcome.warnings`,
   fixed the Twitter gif `has_audio` lie.
2. **Config + database.** `bot/config.py`, SQLAlchemy models, async session
   plumbing, repositories, Alembic scaffolding and an autogenerated initial
   migration.
3. **Downloader facade.** One process-wide `Downloader` with proxy/cookie/
   cache/timeout configuration in a single place.
4. **Routing.** `plan_route` (url / server / mux), `choose_quality`
   (size-aware downgrade), and the rejection reasons shown to users.
5. **Queues.** Token bucket, resizable gate, per-platform fetch queues and the
   RAM-bounded global processing queue.
6. **Sender.** Album grouping, url delivery, server fallback, uploads from disk
   or memory, flood-wait handling.
7. **UI.** Symbol vocabulary, per-platform captions, status text, admin screens
   and keyboards.
8. **Handlers.** Link intake, start/help/settings, suggestions, the full admin
   panel with FSM-driven editing.
9. **Wiring.** `AppContext`, middlewares, FastAPI + aiogram entry point,
   command registration, `/health` and small read-only APIs.
10. **Tests.** 36 offline tests (routing, queues, RAM gating, DB/Alembic,
    captions, keyboards, filters) and 9 live tests that drive the real fetch
    worker against real posts with a stubbed Telegram bot.
11. **End-to-end verification against real Telegram.** `tests/test_bot_e2e.py`
    drives the live dispatcher with real updates and a real Bot API, which
    surfaced three bugs the stubbed tests could not:
    * an empty `COOKIES_FILE=` parsed to `Path('.')` and was handed to yt-dlp as
      a cookie *file*, so **every** YouTube/Twitter metadata fetch failed with
      `[Errno 13] Permission denied: '.'`. `bot/config.py` now treats a blank
      value as "no cookies".
    * `choose_quality` could not downgrade a video whose renditions all report
      the *source* height (Twitter's fxtwitter fallback labels a 256 kbps copy
      and a 10 Mbps copy "1080p"), so a 116 MB tweet was rejected instead of
      delivered at 720p. It now steps down the real renditions by size when the
      height ladder is unusable.
    * a post finished by the processing lane was recorded twice (once as a
      successful fetch, once as a delivery), doubling its analytics footprint.
      The fetch stage now records `queued`; the processing worker records the
      real outcome.

---

## 4. Limitations (read this before shipping)

1. **Single process, in-memory queues.** Job state is not persisted; a restart
   loses queued work. Scaling out needs Redis (queues) and aiogram's Redis FSM
   storage. SQLite also serialises writes, which is fine for one process only.
2. **Bot API upload ceiling.** Without a local Bot API server, uploads are
   capped at 50 MB and url delivery at 5 MB (photo) / 20 MB (video). Larger
   media is rejected with an explanation rather than silently truncated.
3. **Processing is muxing only.** The bot stream-copies the video and audio
   tracks; it does not transcode. If a source only exists at a size above the
   cap, the item is rejected (downgrading picks a *smaller rendition*, it does
   not re-encode).
4. **No premium gate yet.** `max_media_size_bytes` is global per platform, not
   per user tier. The plumbing (size known before download, plus
   `Downloader.affordable`) is there for when you add it.
5. **Deletion is best effort.** Bots can only delete their own messages, and in
   groups only with `can_delete_messages`; the permission is learned on the
   first attempt and `my_chat_member` updates refresh it.
6. **Platform rate limits are self-imposed.** The token buckets are ours, not
   the platforms'. Aggressive values can still get a CDN to throttle you.
7. **Extraction depends on yt-dlp** for YouTube and Twitter ladders, and on
   `instaloader` for Instagram; a platform change can break extraction until the
   library is updated. Reddit metadata also relies on public endpoints (and
   optionally OAuth credentials).
8. **Instagram needs a session to be reliable**, and its cdn urls are *signed
   and expiring* (roughly 24-48 h), so a url must be handed to Telegram promptly.
   Instagram stories also need a logged-in session and vanish after 24 hours.
9. **Analytics are approximate.** Counters come from `usage_events`; a crash
   between delivery and the DB write loses one event. A post handed to the
   processing lane logs a `queued` row at fetch time; `requests` counts only
   terminal rows so one download is one request, but a post that is queued and
   then never finished still shows up (as `queued`) instead of vanishing.
10. **No content filter.** NSFW/spoiler flags from the SDK are not acted on; add
   a policy in `bot/services/routing.py` if you need one.
11. **Single-language UI.** Strings are English; no i18n layer.
12. **Transient CDN stalls are not retried per item.** The HTTP layer retries a
    request and a stalled body read is capped by `REQUEST_TIMEOUT`
    (`sock_read`), but a CDN that stops sending mid-file (observed twice on
    `video.twimg.com` under back-to-back load, while the same download
    succeeded in ~1.5 s in isolation) surfaces to the user as a per-item error.
    Nothing is corrupted - the item is reported, not half-sent - but the job is
    not re-queued automatically.

---

## 5. Configuration reference

See `.env.example` for the full list with comments. The knobs that matter most:

| variable | default | effect |
| --- | --- | --- |
| `BOT_TOKEN` | — | required |
| `OWNER_ID` | `0` | the owner's Telegram id |
| `ADMIN_IDS` | empty | extra admins (`1,2,3` or `[1,2,3]`) |
| `BOT_MODE` | `polling` | `polling` or `webhook` |
| `DELETE_INCOMING_LINKS` | `true` | remove the user's link message |
| `FETCH_RATE_PER_SECOND` | `1.0` | metadata requests/second per platform |
| `PROCESSING_MAX_RAM_BYTES` | `2 GiB` | processing queue RAM budget |
| `PROCESSING_RAM_FACTOR` | `2.5` | RAM reserved per planned byte |
| `TELEGRAM_UPLOAD_LIMIT` | `50 MB` | raise only with a local Bot API server |
| `PROXY` | empty | passed to the downloader SDKs |
| `COOKIES_FILE` | empty | Netscape cookie file for yt-dlp; **empty means no cookies** |
| `INSTAGRAM_SESSION_FILE` | empty | instaloader session file; **strongly recommended** - Instagram rate limits anonymous clients after a handful of requests (`instaloader --login=<user>` creates it) |

`COOKIES_FILE` must be left empty *or* point at a real file. A blank value is
normalised to `None`; it is never turned into a path.

---

## 6. Operations

```bash
pip install -r requirements.txt
copy .env.example .env          # then set BOT_TOKEN and OWNER_ID
python main.py --migrate        # optional: happens automatically at start-up
python main.py                  # polling + FastAPI on :8080
```

* `GET /health` — mode, bot username, enabled platforms, DB connectivity and a
  live queue snapshot.
* `GET /api/stats` — the 7-day analytics payload.
* `GET /api/platforms` — the current per-platform settings.
* `GET /docs` — the generated OpenAPI UI.

Put the process behind a supervisor and, in webhook mode, a reverse proxy
terminating TLS for `WEBHOOK_URL`.

### Tests

```bash
python tests/test_bot_offline.py    # fast, no network
python tests/test_bot_live.py       # real posts, stubbed Telegram
$env:RUN_TELEGRAM_E2E = "1"; python tests/test_bot_e2e.py
```

`test_bot_e2e.py` really sends. It drives the live dispatcher as if you had
pasted each link in a private chat and delivers to `OWNER_ID`, so point it at a
scratch account (or accept the messages). `E2E_CASES="3,4"` runs a subset.

