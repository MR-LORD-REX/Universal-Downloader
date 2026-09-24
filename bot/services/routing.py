"""Decide how each media item should reach Telegram.

The bot has two delivery lanes and this module is the switch between them:

* **direct** - Telegram's own backend fetches the CDN url. Free for us, instant
  for the user, and the only option for images (see the project brief).
* **processing** - the file is video-only (Reddit's `CMAF_1080.mp4`, YouTube's
  adaptive renditions, Twitter's HLS splits) or too big to be fetched by url.
  It has to be downloaded (and muxed with its audio track) before upload.

Everything here is pure: it reads metadata and returns a plan.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from downloader.core.enums import MediaKind
from downloader.core.models import MediaFormat, MediaItem, PostMetadata

from bot.utils.text import format_size


class Action:
    """How a planned item gets to Telegram."""

    URL = "url"
    """Hand the CDN url to the Bot API and let Telegram fetch it."""

    SERVER = "server"
    """Fetch the bytes ourselves, then upload (size limits, fallbacks)."""

    MUX = "mux"
    """Download the video, mux the separate audio track, then upload."""


class SendAs:
    PHOTO = "photo"
    VIDEO = "video"
    ANIMATION = "animation"
    AUDIO = "audio"
    DOCUMENT = "document"


@dataclass(slots=True)
class PlannedItem:
    """One media item of a post, with the delivery decision made."""

    index: int
    item: MediaItem
    send_as: str
    action: str
    fmt: Optional[MediaFormat] = None
    url: Optional[str] = None
    size: Optional[int] = None
    reason: str = ""

    @property
    def kind(self) -> str:
        return str(self.item.kind)

    @property
    def label(self) -> str:
        return f"{self.send_as} #{self.index + 1} ({format_size(self.size)})"


@dataclass(slots=True)
class RejectedItem:
    """An item the configured size limit (or Telegram's limits) refuses."""

    index: int
    item: MediaItem
    size: Optional[int]
    reason: str


@dataclass(slots=True)
class RoutePlan:
    """The full delivery plan for one post."""

    direct: list[PlannedItem] = field(default_factory=list)
    process: list[PlannedItem] = field(default_factory=list)
    rejected: list[RejectedItem] = field(default_factory=list)

    @property
    def total_items(self) -> int:
        return len(self.direct) + len(self.process) + len(self.rejected)

    @property
    def planned(self) -> list[PlannedItem]:
        return [*self.direct, *self.process]

    @property
    def direct_bytes(self) -> int:
        return sum(item.size or 0 for item in self.direct)

    @property
    def process_bytes(self) -> int:
        return sum(item.size or 0 for item in self.process)

    @property
    def needs_processing(self) -> bool:
        return bool(self.process)

    def process_ram_estimate(self, *, factor: float, fallback: int) -> int:
        """Bytes of RAM to reserve for the processing lane."""
        estimate = self.process_bytes or fallback * max(1, len(self.process))
        return int(estimate * factor)


def _select_kwargs(meta: PostMetadata) -> dict[str, object]:
    hints = meta.spec_kwargs()
    return {key: hints[key] for key in ("prefer_muxed", "container", "codec") if key in hints}


def _size_of(item: MediaItem, quality: "str | int", kw: dict[str, object], audio_codec: object) -> Optional[int]:
    try:
        return item.size_for(quality, audio_codec=audio_codec, **kw)  # type: ignore[arg-type]
    except Exception:  # pragma: no cover - defensive
        return item.size_bytes


def _video_needs_mux(fmt: MediaFormat, item: MediaItem) -> bool:
    """True when the chosen rendition has no audio of its own."""
    if item.has_audio is False:
        return False
    if fmt.has_audio is True:
        return False
    if fmt.is_muxed:
        return False
    return True


def item_sizes(meta: PostMetadata, quality: "str | int", **overrides: object) -> list[Optional[int]]:
    """What each item of `meta` would weigh at `quality`."""
    return [
        item.size_for(quality, **overrides)  # type: ignore[arg-type]
        for item in meta.items
    ]


def fits_limits(
    meta: PostMetadata,
    quality: "str | int",
    *,
    upload_limit: int,
    max_item_bytes: Optional[int] = None,
    **overrides: object,
) -> bool:
    """True when every item at `quality` is known to be deliverable.

    Items whose size is unknown are treated as fitting: the download engine
    still enforces `max_size_bytes`, and the routing layer rejects afterwards
    if the bytes turn out to be too big.
    """
    for size in item_sizes(meta, quality, **overrides):
        if size is None:
            continue
        if size > upload_limit:
            return False
        if max_item_bytes is not None and size > max_item_bytes:
            return False
    return True


def choose_quality(
    meta: PostMetadata,
    *,
    requested: str = "best",
    upload_limit: int,
    max_item_bytes: Optional[int] = None,
) -> tuple[str, Optional[str]]:
    """The best quality at or below `requested` that Telegram can accept.

    Returns `(quality, note)`; the note explains a downgrade so the caller can
    tell the admin what happened. A rendition ladder that is entirely too big
    is left alone - the routing layer turns that into a clear rejection.
    """
    hints = _select_kwargs(meta)
    if fits_limits(meta, requested, upload_limit=upload_limit, max_item_bytes=max_item_bytes, **hints):
        return requested, None

    heights: set[int] = set()
    for item in meta.items:
        for fmt in item.formats:
            if fmt.is_manifest:
                continue
            height = fmt.quality_height or fmt.height
            if height:
                heights.add(int(height))
    for height in sorted(heights, reverse=True):
        candidate = f"{height}p"
        if candidate == requested:
            continue
        if fits_limits(meta, candidate, upload_limit=upload_limit, max_item_bytes=max_item_bytes, **hints):
            return candidate, f"{requested} was too large, delivered {candidate} instead"

    # Some sources report every rendition at the source height (Twitter's
    # fxtwitter fallback labels a 256 kbps copy and a 10 Mbps copy "1080p"), so
    # no "720p"-style string can name the smaller copy. Step down the concrete
    # renditions by size instead: same intent, driven by bytes not labels.
    for format_id, label in _shared_format_ids(meta):
        if _format_fits(meta, format_id, upload_limit=upload_limit, max_item_bytes=max_item_bytes):
            return format_id, (
                f"{requested} was too large, delivered a smaller rendition ({label})"
            )
    return requested, None


def _shared_format_ids(meta: PostMetadata) -> list[tuple[str, str]]:
    """Real rendition ids every item can resolve, largest first.

    A rendition is only offered when *every* item of the post carries that id,
    so a returned id can never silently resolve to the wrong copy.
    """
    if not meta.items:
        return []
    shared: Optional[set[str]] = None
    labels: dict[str, str] = {}
    weight: dict[str, int] = {}
    for item in meta.items:
        ids: set[str] = set()
        for fmt in item.formats:
            if fmt.is_manifest or fmt.size_bytes is None:
                continue
            ids.add(fmt.format_id)
            labels.setdefault(
                fmt.format_id,
                f"{fmt.bitrate_kbps} kbps"
                if fmt.bitrate_kbps
                else (fmt.quality_label or (f"{fmt.height}p" if fmt.height else fmt.format_id)),
            )
            weight[fmt.format_id] = max(weight.get(fmt.format_id, 0), fmt.size_bytes)
        shared = ids if shared is None else (shared & ids)
    if not shared:
        return []
    ordered = sorted(shared, key=lambda fid: weight[fid], reverse=True)
    return [(fid, labels[fid]) for fid in ordered]


def _format_fits(
    meta: PostMetadata,
    format_id: str,
    *,
    upload_limit: int,
    max_item_bytes: Optional[int] = None,
) -> bool:
    """True when every item can take `format_id` and stays under both limits."""
    for item in meta.items:
        fmt = next((f for f in item.formats if f.format_id == format_id), None)
        if fmt is None or fmt.size_bytes is None:
            return False
        if fmt.size_bytes > upload_limit:
            return False
        if max_item_bytes is not None and fmt.size_bytes > max_item_bytes:
            return False
    return True


def _place(plan: RoutePlan, planned: PlannedItem) -> None:
    """URL deliveries are free; everything else needs the processing lane."""
    if planned.action == Action.URL:
        plan.direct.append(planned)
    else:
        plan.process.append(planned)


def plan_route(
    meta: PostMetadata,
    *,
    quality: "str | int" = "best",
    max_item_bytes: Optional[int] = None,
    photo_url_limit: int = 5 * 1024 * 1024,
    video_url_limit: int = 20 * 1024 * 1024,
    upload_limit: int = 50 * 1024 * 1024,
    photo_upload_limit: int = 10 * 1024 * 1024,
) -> RoutePlan:
    """Classify every item of `meta` into url / server / mux delivery."""
    plan = RoutePlan()
    kw = _select_kwargs(meta)
    select_kwargs = dict(kw)
    audio_codec = meta.spec_kwargs().get("audio_codec")

    for item in meta.items:
        fmt: Optional[MediaFormat] = None
        try:
            fmt = item.select(quality, **select_kwargs)  # type: ignore[arg-type]
        except Exception:
            fmt = None
        size = _size_of(item, quality, kw, audio_codec)
        kind = item.kind

        if max_item_bytes is not None and size is not None and size > max_item_bytes:
            plan.rejected.append(
                RejectedItem(item.index, item, size, f"item is {format_size(size)}, limit is {format_size(max_item_bytes)}")
            )
            continue

        url = fmt.url if fmt is not None else item.source_url
        if not url:
            plan.rejected.append(RejectedItem(item.index, item, size, "no downloadable url in the metadata"))
            continue

        if kind in (MediaKind.IMAGE, MediaKind.EXTERNAL_IMAGE):
            if size is not None and size > upload_limit:
                plan.rejected.append(
                    RejectedItem(item.index, item, size, f"image is {format_size(size)} which exceeds the upload limit")
                )
                continue
            send_as = SendAs.PHOTO if (size is None or size <= photo_upload_limit) else SendAs.DOCUMENT
            action = Action.URL if (size is None or size <= photo_url_limit) else Action.SERVER
            _place(plan, PlannedItem(item.index, item, send_as, action, fmt, url, size))

        elif kind is MediaKind.GIF:
            extension = (fmt.extension if fmt is not None else item.extension) or ""
            send_as = SendAs.ANIMATION if extension in ("gif", "mp4", "webm") else SendAs.VIDEO
            action, reason = _url_or_server(size, video_url_limit, upload_limit)
            if action == Action.SERVER and reason:
                plan.rejected.append(RejectedItem(item.index, item, size, reason))
                continue
            _place(plan, PlannedItem(item.index, item, send_as, action, fmt, url, size))

        elif kind is MediaKind.VIDEO:
            if fmt is None:
                _place(plan, PlannedItem(item.index, item, SendAs.VIDEO, Action.URL, fmt, url, size))
                continue
            if _video_needs_mux(fmt, item):
                if size is not None and size > upload_limit:
                    plan.rejected.append(
                        RejectedItem(
                            item.index,
                            item,
                            size,
                            f"video is {format_size(size)}; it needs muxing but exceeds the {format_size(upload_limit)} upload limit",
                        )
                    )
                    continue
                plan.process.append(
                    PlannedItem(item.index, item, SendAs.VIDEO, Action.MUX, fmt, url, size, "needs audio muxing")
                )
                continue
            action, reason = _url_or_server(size, video_url_limit, upload_limit)
            if action == Action.SERVER and reason:
                plan.rejected.append(RejectedItem(item.index, item, size, reason))
                continue
            _place(plan, PlannedItem(item.index, item, SendAs.VIDEO, action, fmt, url, size))

        elif kind is MediaKind.AUDIO:
            if size is not None and size > upload_limit:
                plan.rejected.append(RejectedItem(item.index, item, size, "audio exceeds the upload limit"))
                continue
            plan.process.append(
                PlannedItem(item.index, item, SendAs.AUDIO, Action.SERVER, fmt, url, size, "audio upload")
            )

        else:
            _place(plan, 
                PlannedItem(item.index, item, SendAs.DOCUMENT, Action.URL, fmt, url, size, "unclassified media")
            )

    return plan


def _url_or_server(size: Optional[int], url_limit: int, upload_limit: int) -> tuple[str, str]:
    """URL delivery when Telegram can fetch it, else server delivery."""
    if size is not None and size > upload_limit:
        return Action.SERVER, (
            f"file is {format_size(size)} which is above the {format_size(upload_limit)} Bot API upload limit"
        )
    if size is not None and size > url_limit:
        return Action.SERVER, ""
    return Action.URL, ""


__all__ = [
    "Action",
    "choose_quality",
    "fits_limits",
    "item_sizes",
    "PlannedItem",
    "RejectedItem",
    "RoutePlan",
    "SendAs",
    "plan_route",
]
