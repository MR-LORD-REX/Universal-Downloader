"""Platform agnostic models: formats, items, groups, metadata and results."""

from __future__ import annotations

import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field

from .exceptions import MediaNotAvailableError
from .enums import (
    FormatKind,
    FormatOrigin,
    MediaGroupType,
    MediaKind,
    Platform,
    StrEnum,
)


def human_size(num: Optional[int]) -> Optional[str]:
    """Format a byte count as ``"1.4 MB"`` (``None`` passes through)."""
    if num is None:
        return None
    value = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} TB"  # pragma: no cover - unreachable


class Quality(StrEnum):
    """Shortcut quality selectors accepted by :meth:`MediaItem.select`."""

    BEST = "best"
    WORST = "worst"
    BEST_AUDIO = "audio"
    SMALLEST = "smallest"


_BEST_ALIASES = frozenset(
    {"best", "max", "highest", "largest", "source", "bestvideo", "original", "hd"}
)
_WORST_ALIASES = frozenset({"worst", "min", "lowest", "smallest", "tiny", "data", "small"})

_SELECTABLE_KINDS = frozenset(
    {
        FormatKind.IMAGE,
        FormatKind.GIF,
        FormatKind.VIDEO,
        FormatKind.MUXED,
        FormatKind.AUDIO,
    }
)


def parse_height(quality: "str | int | None") -> Optional[int]:
    """Extract a target height from ``1080``, ``"1080"`` or ``"1080p"``."""
    if quality is None:
        return None
    if isinstance(quality, int):
        return quality if quality > 0 else None
    match = re.match(r"^\s*(\d{3,4})\s*[pP]?\s*$", str(quality))
    if match:
        return int(match.group(1))
    return None


def quality_key(fmt: "MediaFormat") -> tuple:
    """Sort key ranking a format from worst to best."""
    height = fmt.quality_height or fmt.height or 0
    return (height, fmt.bitrate_kbps or 0, fmt.size_bytes or 0, fmt.pixels)


_CODEC_ALIASES: dict[str, tuple[str, ...]] = {
    "h264": ("avc1", "avc3", "h264"),
    "avc": ("avc1", "avc3", "h264"),
    "h265": ("hev1", "hvc1", "h265"),
    "aac": ("mp4a", "aac"),
    "m4a": ("mp4a", "aac"),
    "vp9": ("vp9", "vp09"),
    "av1": ("av01", "av1"),
}


def codec_matches(fmt: "MediaFormat", wanted: Optional[str], *, audio: bool = False) -> bool:
    """``True`` when ``fmt`` carries the requested codec family."""
    if not wanted:
        return False
    targets = _CODEC_ALIASES.get(wanted.lower(), (wanted.lower(),))
    codecs = [c.strip().lower() for c in (fmt.codecs or "").split(",") if c.strip()]
    actual = (fmt.audio_codec if audio else fmt.video_codec) or ""
    pool = codecs + ([actual.lower()] if actual else [])
    return any(c.startswith(t) for c in pool for t in targets)


_AUDIO_CONTAINERS = {
    "mp4": ("m4a", "mp4", "m4v", "aac", "mov"),
    "m4v": ("m4a", "mp4", "m4v", "aac", "mov"),
    "mov": ("m4a", "mp4", "m4v", "aac", "mov"),
    "webm": ("webm", "weba", "opus", "ogg", "oga"),
    "mkv": ("mka", "mkv", "m4a", "aac", "opus", "ogg"),
}


def audio_container_matches(fmt: "MediaFormat", container: Optional[str]) -> bool:
    """``True`` when ``fmt``'s audio can live in ``container`` without re-encoding."""
    if not container:
        return False
    allowed = _AUDIO_CONTAINERS.get(container.lower())
    if not allowed:
        return False
    return (fmt.extension or "").lower() in allowed


def pick_audio(
    tracks: Sequence["MediaFormat"],
    *,
    min_bitrate: int = 0,
    codec: Optional[str] = None,
    container: Optional[str] = None,
) -> Optional["MediaFormat"]:
    """Best audio track: honour codec/container first, then bitrate."""
    pool = [f for f in tracks if (f.bitrate_kbps or 0) >= min_bitrate] or list(tracks)
    if not pool:
        return None
    return max(
        pool,
        key=lambda f: (
            1 if codec_matches(f, codec, audio=True) else 0,
            1 if audio_container_matches(f, container) else 0,
            f.bitrate_kbps or 0,
            f.size_bytes or 0,
        ),
    )


_ORIGIN_RANK: dict[FormatOrigin, int] = {
    FormatOrigin.DIRECT: 4,
    FormatOrigin.DERIVED: 4,
    FormatOrigin.YTDLP: 3,
    FormatOrigin.SCRAPE: 3,
    FormatOrigin.FX: 3,
    FormatOrigin.EXTERNAL: 2,
    FormatOrigin.DASH: 2,
    FormatOrigin.HLS: 2,
    FormatOrigin.PROBE: 2,
    FormatOrigin.PREVIEW: 1,
}


def format_preference(
    fmt: "MediaFormat",
    *,
    prefer_muxed: bool = True,
    container: Optional[str] = None,
    codec: Optional[str] = None,
) -> tuple:
    """Ranking tuple for a format; bigger is better.

    Rendition height dominates so a quality request is never silently
    downgraded for container reasons. Ties are then broken by provenance (an
    original beats a preview), container, codec, "already muxed", "direct url
    rather than a manifest", "size is actually known", bitrate, fps, pixel
    count and finally the raw file size.

    That last key matters for platforms whose metadata does not distinguish a
    ladder properly: Pinterest reports the same ``640x1138`` for five renditions
    that are genuinely different, so the *larger file* is the better one and
    the only honest tie breaker available.
    """
    height = fmt.quality_height or fmt.height or 0
    return (
        height,
        _ORIGIN_RANK.get(fmt.origin, 2),
        1 if container and (fmt.extension or "").lower() == container.lower() else 0,
        1 if codec_matches(fmt, codec) else 0,
        1 if (prefer_muxed and fmt.is_muxed) else 0,
        0 if fmt.is_manifest else 1,
        1 if fmt.size_bytes is not None else 0,
        fmt.bitrate_kbps or 0,
        fmt.fps or 0,
        fmt.pixels,
        fmt.size_bytes or 0,
    )


def rank_formats(
    formats: Sequence["MediaFormat"],
    *,
    prefer_muxed: bool = True,
    container: Optional[str] = None,
    codec: Optional[str] = None,
) -> list["MediaFormat"]:
    """Every format, best first, according to :func:`format_preference`."""
    return sorted(
        formats,
        key=lambda fmt: format_preference(
            fmt, prefer_muxed=prefer_muxed, container=container, codec=codec
        ),
        reverse=True,
    )


def pick_format(
    formats: Sequence["MediaFormat"],
    quality: "str | int" = "best",
    *,
    prefer_muxed: bool = True,
    container: Optional[str] = None,
    codec: Optional[str] = None,
    strict: bool = False,
) -> "MediaFormat":
    """Choose the format that best matches ``quality``.

    Accepts ``best``/``worst`` shortcuts, a bare height (``"1080p"``/``1080``)
    or an explicit ``format_id``. ``container``/``codec`` are *preferences*
    used to break ties at equal rendition height. Raises
    :class:`~downloader.core.exceptions.MediaNotAvailableError` when nothing
    matches.
    """
    candidates = [f for f in formats if f.kind in _SELECTABLE_KINDS] or list(formats)
    if not candidates:
        raise MediaNotAvailableError("no downloadable format available")

    def best_of(pool: Sequence["MediaFormat"]) -> "MediaFormat":
        return rank_formats(
            pool, prefer_muxed=prefer_muxed, container=container, codec=codec
        )[0]

    label = str(quality).strip().lower() if quality is not None else "best"
    if label in _BEST_ALIASES:
        return best_of(candidates)
    if label in _WORST_ALIASES:
        lowest = min(_height_of(f) for f in candidates)
        return best_of([f for f in candidates if _height_of(f) == lowest])

    by_id = [f for f in candidates if f.format_id == str(quality)]
    if by_id:
        return by_id[0]

    target = parse_height(quality)
    if target is None:
        return best_of(candidates)
    below = [f for f in candidates if _height_of(f) <= target]
    if below:
        return best_of(below)
    if strict:
        raise MediaNotAvailableError(
            f"no rendition at or below {target}p (smallest is {min(_height_of(f) for f in candidates)}p)"
        )
    return best_of(candidates)


def _height_of(fmt: "MediaFormat") -> int:
    return fmt.quality_height or fmt.height or 0


class Thumbnail(BaseModel):
    """A static preview image (``thumbnail`` / ``i.redd.it`` / ``pbs.twimg.com``)."""

    model_config = ConfigDict(extra="ignore")

    url: str
    width: Optional[int] = None
    height: Optional[int] = None
    extension: Optional[str] = None
    size_bytes: Optional[int] = None
    note: Optional[str] = None

    @property
    def resolution(self) -> Optional[str]:
        if self.width and self.height:
            return f"{self.width}x{self.height}"
        return None

    @property
    def pixels(self) -> int:
        return (self.width or 0) * (self.height or 0)

    @property
    def size_human(self) -> Optional[str]:
        return human_size(self.size_bytes)

    @property
    def is_gif(self) -> bool:
        return (self.extension or "").lower() in ("gif", "gifv")


class MediaFormat(BaseModel):
    """One concrete, downloadable representation of a media item."""

    model_config = ConfigDict(extra="ignore")

    format_id: str
    url: str
    kind: FormatKind
    origin: FormatOrigin = FormatOrigin.DIRECT
    container: Optional[str] = None
    mime_type: Optional[str] = None
    extension: Optional[str] = None
    protocol: Optional[str] = None
    """``https``, ``m3u8_native``, ``http_dash_segments``, ... (yt-dlp naming)."""
    width: Optional[int] = None
    height: Optional[int] = None
    quality_height: Optional[int] = None
    """Height of the rendition ladder (a vertical 1080p video is 1080 here)."""
    fps: Optional[float] = None
    bitrate_kbps: Optional[int] = None
    audio_bitrate_kbps: Optional[int] = None
    codecs: Optional[str] = None
    video_codec: Optional[str] = None
    audio_codec: Optional[str] = None
    size_bytes: Optional[int] = None
    size_is_approx: bool = False
    """``True`` when ``size_bytes`` is an estimate rather than a CDN header."""
    size_source: Optional[str] = None
    """Where ``size_bytes`` came from: ``metadata``, ``head``, ``range``, ``estimate``."""
    has_audio: Optional[bool] = None
    has_video: Optional[bool] = None
    language: Optional[str] = None
    quality_label: Optional[str] = None
    note: Optional[str] = None
    http_headers: dict[str, str] = Field(default_factory=dict)
    """Extra request headers the CDN requires (YouTube needs, e.g., a Referer)."""
    meta: dict[str, Any] = Field(default_factory=dict)

    @property
    def pixels(self) -> int:
        return (self.width or 0) * (self.height or 0)

    @property
    def resolution(self) -> Optional[str]:
        if self.width and self.height:
            return f"{self.width}x{self.height}"
        return None

    @property
    def size_human(self) -> Optional[str]:
        return human_size(self.size_bytes)

    @property
    def size_known(self) -> bool:
        return self.size_bytes is not None

    @property
    def display_size(self) -> str:
        if self.size_bytes is None:
            return "unknown size"
        return ("~" if self.size_is_approx else "") + (self.size_human or "")

    @property
    def is_audio_only(self) -> bool:
        if self.kind == FormatKind.AUDIO:
            return True
        return self.has_audio is True and self.has_video is False

    @property
    def is_video_only(self) -> bool:
        if self.kind == FormatKind.VIDEO:
            return self.has_audio is not True
        return self.has_video is True and self.has_audio is not True

    @property
    def is_muxed(self) -> bool:
        return self.kind == FormatKind.MUXED or (
            self.has_video is True and self.has_audio is True
        )

    @property
    def is_manifest(self) -> bool:
        proto = (self.protocol or "").lower()
        return "m3u8" in proto or "dash" in proto or proto.endswith(".m3u8")

    def display(self) -> str:
        bits = [self.format_id, self.kind.value]
        if self.resolution:
            bits.append(self.resolution)
        bits.append(self.display_size)
        if self.bitrate_kbps:
            bits.append(f"{self.bitrate_kbps}kbps")
        if self.codecs:
            bits.append(self.codecs)
        bits.append(self.origin.value)
        return " | ".join(bits)


class MediaItem(BaseModel):
    """A single logical medium: one image, one video, one album entry..."""

    model_config = ConfigDict(extra="ignore")

    index: int = 0
    id: Optional[str] = None
    kind: MediaKind = MediaKind.UNKNOWN
    caption: Optional[str] = None
    source_url: Optional[str] = None
    """The human facing url (tweet url, watch url), not the CDN link."""
    mime_type: Optional[str] = None
    extension: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    duration: Optional[float] = None
    has_audio: Optional[bool] = None
    size_bytes: Optional[int] = None
    size_is_approx: bool = False
    formats: list[MediaFormat] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)

    @property
    def resolution(self) -> Optional[str]:
        if self.width and self.height:
            return f"{self.width}x{self.height}"
        return None

    @property
    def size_human(self) -> Optional[str]:
        return human_size(self.size_bytes)

    @property
    def urls(self) -> list[str]:
        return [fmt.url for fmt in self.formats]

    @property
    def total_size_bytes(self) -> Optional[int]:
        """Sum of every known format size (``None`` when nothing is known)."""
        known = [fmt.size_bytes for fmt in self.formats if fmt.size_bytes is not None]
        return sum(known) if known else None

    @property
    def video_formats(self) -> list[MediaFormat]:
        return [f for f in self.formats if f.kind in (FormatKind.VIDEO, FormatKind.MUXED)]

    @property
    def audio_formats(self) -> list[MediaFormat]:
        return [f for f in self.formats if f.kind == FormatKind.AUDIO]

    @property
    def image_formats(self) -> list[MediaFormat]:
        return [f for f in self.formats if f.kind in (FormatKind.IMAGE, FormatKind.GIF)]

    @property
    def selectable(self) -> list[MediaFormat]:
        return [f for f in self.formats if f.kind in _SELECTABLE_KINDS]

    @property
    def is_downloadable(self) -> bool:
        return bool(self.formats) and self.kind.is_downloadable

    def select(
        self,
        quality: "str | int" = "best",
        *,
        prefer_muxed: bool = True,
        container: Optional[str] = None,
        codec: Optional[str] = None,
        strict: bool = False,
    ) -> MediaFormat:
        """Return the single format matching ``quality``."""
        return pick_format(
            self.selectable or self.formats,
            quality,
            prefer_muxed=prefer_muxed,
            container=container,
            codec=codec,
            strict=strict,
        )

    def best_audio(
        self,
        *,
        min_bitrate: int = 0,
        codec: Optional[str] = None,
        container: Optional[str] = None,
    ) -> Optional[MediaFormat]:
        """Best standalone audio track, honouring codec/container preferences."""
        return pick_audio(
            self.audio_formats,
            min_bitrate=min_bitrate,
            codec=codec,
            container=container,
        )

    def format(self, format_id: str) -> Optional[MediaFormat]:
        for fmt in self.formats:
            if fmt.format_id == format_id:
                return fmt
        return None

    def size_for(
        self,
        quality: "str | int" = "best",
        *,
        prefer_muxed: bool = True,
        container: Optional[str] = None,
        codec: Optional[str] = None,
        audio_codec: Optional[str] = None,
    ) -> Optional[int]:
        """Size of the format that ``quality`` resolves to (plus audio, if split)."""
        try:
            primary = self.select(
                quality, prefer_muxed=prefer_muxed, container=container, codec=codec
            )
        except MediaNotAvailableError:
            return None
        total = primary.size_bytes
        if total is not None and primary.is_video_only and self.has_audio is not False:
            audio = self.best_audio(codec=audio_codec or codec, container=container)
            if audio is not None and audio.size_bytes is not None:
                total += audio.size_bytes
        return total

    def summary(self) -> str:
        parts = [self.kind.value]
        if self.resolution:
            parts.append(self.resolution)
        if self.duration:
            parts.append(f"{self.duration:.1f}s")
        if self.size_human:
            parts.append(self.size_human)
        return " ".join(parts)


class MediaGroup(BaseModel):
    """All media items of one kind inside a post (e.g. all images)."""

    model_config = ConfigDict(extra="ignore")

    kind: MediaKind
    label: Optional[str] = None
    items: list[MediaItem] = Field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.items)

    @property
    def urls(self) -> list[str]:
        return [fmt.url for item in self.items for fmt in item.formats]

    @property
    def media_urls(self) -> list[str]:
        """One ``(best effort)`` original url per item."""
        out: list[str] = []
        for item in self.items:
            if item.source_url:
                out.append(item.source_url)
            else:
                out.extend(item.urls[:1])
        return out

    @property
    def size_bytes(self) -> Optional[int]:
        known = [i.size_bytes for i in self.items if i.size_bytes is not None]
        return sum(known) if known else None

    @property
    def size_human(self) -> Optional[str]:
        return human_size(self.size_bytes)

    def __len__(self) -> int:
        return len(self.items)

    def __iter__(self) -> Iterator[MediaItem]:  # type: ignore[override]
        return iter(self.items)


class DownloadedFile(BaseModel):
    """Bytes (and/or a path) produced by a download."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="ignore")

    item_index: int = 0
    kind: MediaKind = MediaKind.UNKNOWN
    filename: str
    mime_type: Optional[str] = None
    format: Optional[MediaFormat] = None
    path: Optional[Path] = None
    data: Optional[bytes] = Field(default=None, repr=False, exclude=True)
    downloaded: int = 0
    elapsed: float = 0.0
    muxed: bool = False
    note: Optional[str] = None

    @property
    def extension(self) -> str:
        if self.format and self.format.extension:
            return self.format.extension
        suffix = Path(self.filename).suffix
        return suffix.lstrip(".") or "bin"

    @property
    def size(self) -> int:
        if self.data is not None:
            return len(self.data)
        if self.path is not None and self.path.exists():
            return self.path.stat().st_size
        return self.downloaded

    @property
    def size_human(self) -> str:
        return human_size(self.size) or "0 B"

    @property
    def in_memory(self) -> bool:
        return self.data is not None

    @property
    def on_disk(self) -> bool:
        return self.path is not None and self.path.exists()

    def read(self) -> bytes:
        """Return the bytes, reading them from disk when needed."""
        if self.data is not None:
            return self.data
        if self.path is not None:
            return self.path.read_bytes()
        return b""

    def write(self, target: "str | Path", *, overwrite: bool = False) -> Path:
        """Persist this file to ``target`` (a file path or a directory)."""
        dest = Path(target)
        if dest.is_dir() or target in (".", "") or str(target).endswith(("/", "\\")):
            dest = dest / self.filename
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and not overwrite:
            dest = _unique_path(dest)
        if self.data is not None:
            dest.write_bytes(self.data)
        elif self.path is not None and self.path.exists():
            if dest.resolve() != self.path.resolve():
                shutil.copy2(self.path, dest)
        else:  # pragma: no cover - defensive
            raise FileNotFoundError(f"nothing to write for {self.filename}")
        self.path = dest
        return dest

    def free(self) -> None:
        """Drop in-memory bytes (and delete the managed temp file when present)."""
        self.data = None
        if self.path is not None and self.path.exists() and self.note == "temp":
            self.path.unlink(missing_ok=True)
            self.path = None


class PostMetadata(BaseModel):
    """Everything the SDK knows about a post before/without downloading it."""

    model_config = ConfigDict(extra="ignore", arbitrary_types_allowed=True)

    # ------------------------------------------------------------- identity
    platform: Platform = Platform.UNKNOWN
    id: str
    url: Optional[str] = None
    requested_url: Optional[str] = None
    permalink: Optional[str] = None

    # ---------------------------------------------------------- descriptive
    title: Optional[str] = None
    description: Optional[str] = None
    author: Optional[str] = None
    author_id: Optional[str] = None
    author_url: Optional[str] = None
    channel: Optional[str] = None
    created_utc: Optional[float] = None
    upload_date: Optional[str] = None
    """``YYYYMMDD`` exactly as the platform reports it."""
    timestamp: Optional[int] = None
    duration: Optional[float] = None
    view_count: Optional[int] = None
    like_count: Optional[int] = None
    comment_count: Optional[int] = None
    repost_count: Optional[int] = None
    language: Optional[str] = None
    thumbnail: Optional[str] = None
    thumbnails: list[Thumbnail] = Field(default_factory=list)

    # --------------------------------------------------------------- media
    media_type: MediaKind = MediaKind.UNKNOWN
    media_group_type: MediaGroupType = MediaGroupType.NONE
    items: list[MediaItem] = Field(default_factory=list)
    groups: dict[str, MediaGroup] = Field(default_factory=dict)
    subtitles: dict[str, list[MediaFormat]] = Field(default_factory=dict)
    external_url: Optional[str] = None
    quality_hints: dict[str, Any] = Field(default_factory=dict)
    """Defaults used when resolving a quality: ``container``, ``codec``, ``prefer_muxed``.

    Platform clients fill this from their config so ``metadata.links()`` and
    ``metadata.size_bytes`` report exactly what ``download()`` would fetch.
    """

    # --------------------------------------------------------------- flags
    is_live: bool = False
    is_short: bool = False
    is_playlist: bool = False
    is_nsfw: bool = False
    is_spoiler: bool = False
    is_age_restricted: bool = False
    is_private: bool = False

    # --------------------------------------------------------- bookkeeping
    providers: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict, repr=False, exclude=True)
    native: Any = Field(default=None, repr=False, exclude=True)
    """The platform SDK's own metadata object (used to resume a download)."""

    # ------------------------------------------------------------------ time
    @property
    def created_at(self) -> Optional[datetime]:
        if self.created_utc is None:
            return None
        return datetime.fromtimestamp(self.created_utc, tz=timezone.utc)

    @property
    def upload_datetime(self) -> Optional[datetime]:
        if not self.upload_date:
            return None
        try:
            return datetime.strptime(self.upload_date, "%Y%m%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    # ----------------------------------------------------------------- size
    @property
    def count(self) -> int:
        return len(self.items)

    @property
    def primary(self) -> Optional[MediaItem]:
        return self.items[0] if self.items else None

    @property
    def size_bytes(self) -> Optional[int]:
        """Total size of the media at best quality (``None`` when unknown)."""
        return self.size_for("best")

    @property
    def size_human(self) -> Optional[str]:
        return human_size(self.size_bytes)

    @property
    def size_known(self) -> bool:
        return self.size_bytes is not None

    @property
    def size_is_approx(self) -> bool:
        known = [i for i in self.items if i.size_bytes is not None]
        return any(i.size_is_approx for i in known)

    def spec_kwargs(self, **overrides: Any) -> dict[str, Any]:
        """Merge caller overrides into the metadata's own quality defaults."""
        hints = dict(self.quality_hints or {})
        hints.update({k: v for k, v in overrides.items() if v is not None})
        return {
            "prefer_muxed": bool(hints.get("prefer_muxed", True)),
            "container": hints.get("container"),
            "codec": hints.get("codec"),
            "audio_codec": hints.get("audio_codec"),
        }

    @staticmethod
    def _select_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
        """The subset of :meth:`spec_kwargs` that :meth:`MediaItem.select` accepts."""
        return {k: v for k, v in kwargs.items() if k in ("prefer_muxed", "container", "codec")}

    def size_for(self, quality: "str | int" = "best", **overrides: Any) -> Optional[int]:
        """Total size of every item resolved at ``quality``."""
        kwargs = self.spec_kwargs(**overrides)
        total = 0
        seen = False
        for item in self.items:
            size = item.size_for(quality, **kwargs)
            if size is None:
                continue
            seen = True
            total += size
        return total if seen else None

    def size_breakdown(
        self, quality: "str | int" = "best", **overrides: Any
    ) -> list[tuple[int, str, Optional[int]]]:
        """``[(item_index, quality_label, size_bytes)]`` for the chosen quality."""
        kwargs = self.spec_kwargs(**overrides)
        out: list[tuple[int, str, Optional[int]]] = []
        for item in self.items:
            try:
                fmt = item.select(quality, **self._select_kwargs(kwargs))
            except MediaNotAvailableError:
                out.append((item.index, "n/a", None))
                continue
            out.append(
                (item.index, fmt.quality_label or fmt.format_id, item.size_for(quality, **kwargs))
            )
        return out

    # ----------------------------------------------------------------- urls
    def links(self, quality: "str | int" = "best", **overrides: Any) -> dict[str, list[str]]:
        """Direct CDN urls grouped by media kind, one per item.

        These are the urls the platform actually serves the bytes from (not the
        post permalink), so a Telegram bot can hand them straight to the Bot API
        as ``photo``/``video``/``document`` urls without any processing.
        """
        kwargs = self.spec_kwargs(**overrides)
        select_kwargs = self._select_kwargs(kwargs)
        out: dict[str, list[str]] = {}
        for item in self.items:
            try:
                fmt = item.select(quality, **select_kwargs)
            except MediaNotAvailableError:
                if item.source_url:
                    out.setdefault(str(item.kind), []).append(item.source_url)
                continue
            out.setdefault(str(item.kind), []).append(fmt.url)
        return out

    def urls(self, quality: "str | int" = "best", **overrides: Any) -> list[str]:
        """Flat list of the direct CDN urls at ``quality``."""
        return [url for group in self.links(quality, **overrides).values() for url in group]

    def format_for(self, quality: "str | int" = "best", **overrides: Any) -> list[MediaFormat]:
        """The concrete format ``download()`` would pick for each item."""
        kwargs = self.spec_kwargs(**overrides)
        out: list[MediaFormat] = []
        for item in self.items:
            try:
                out.append(item.select(quality, **self._select_kwargs(kwargs)))
            except MediaNotAvailableError:
                continue
        return out

    def all_links(self) -> dict[str, list[str]]:
        """Every known url grouped by media kind (quality ladder included)."""
        out: dict[str, list[str]] = {}
        for item in self.items:
            bucket = out.setdefault(str(item.kind), [])
            for fmt in item.formats:
                bucket.append(fmt.url)
        return out

    def sizes(self, quality: "str | int" = "best", **overrides: Any) -> dict[str, list[Optional[int]]]:
        """Per kind byte sizes matching :meth:`links`."""
        kwargs = self.spec_kwargs(**overrides)
        out: dict[str, list[Optional[int]]] = {}
        for item in self.items:
            out.setdefault(str(item.kind), []).append(item.size_for(quality, **kwargs))
        return out

    @property
    def subtitle_languages(self) -> list[str]:
        return sorted(self.subtitles)

    def subtitles_for(self, language: str) -> list[MediaFormat]:
        return self.subtitles.get(language) or self.subtitles.get(language.split("-")[0]) or []

    # ------------------------------------------------------------ serialising
    def to_dict(self, *, include_raw: bool = False, include_formats: bool = True) -> dict[str, Any]:
        data = self.model_dump(mode="json", exclude={"raw", "native"})
        if not include_formats:
            for item in data.get("items", []):
                item.pop("formats", None)
        if include_raw:
            data["raw"] = self.raw
        return data

    def summary(self) -> str:
        """One line human readable overview (handy for logs)."""
        parts = [
            str(self.platform),
            self.media_type.value,
            f"group={self.media_group_type.value}",
            f"{self.count} item(s)",
        ]
        size = self.size_human
        if size:
            parts.append(("~" if self.size_is_approx else "") + size)
        if self.duration:
            parts.append(f"{int(self.duration // 60)}m{int(self.duration % 60):02d}s")
        if self.author:
            parts.append(self.author)
        head = " | ".join(parts)
        return f"{head}\n  {(self.title or '')[:120]}"

    def describe(self) -> str:
        """Multi-line dump of every item and format (debug helper)."""
        lines = [self.summary(), f"  providers: {', '.join(self.providers) or '-'}"]
        for item in self.items:
            lines.append(f"  [{item.index}] {item.summary()}")
            for fmt in item.formats:
                lines.append(f"       - {fmt.display()}")
        for warning in self.warnings:
            lines.append(f"  ! {warning}")
        return "\n".join(lines)

    def rebuild_groups(self) -> "PostMetadata":
        self.groups = group_items(self.items)
        return self


class DownloadResult(BaseModel):
    """Outcome of ``<Platform>Client.download``."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="ignore")

    metadata: PostMetadata
    files: list[DownloadedFile] = Field(default_factory=list)
    quality: "str | int" = "best"
    elapsed: float = 0.0
    errors: dict[str, str] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.files) and not self.errors

    @property
    def total_bytes(self) -> int:
        return sum(f.size for f in self.files)

    @property
    def total_human(self) -> str:
        return human_size(self.total_bytes) or "0 B"

    @property
    def saved_paths(self) -> list[Path]:
        return [f.path for f in self.files if f.path is not None]

    def files_of(self, kind: "MediaKind | str") -> list[DownloadedFile]:
        return [f for f in self.files if str(f.kind) == str(kind)]

    def free(self) -> None:
        """Release in-memory buffers (and temp files) of every file."""
        for file in self.files:
            file.free()

    async def save(
        self,
        dest: "str | Path",
        *,
        pattern: Optional[str] = None,
        overwrite: bool = False,
        album_dir: bool = True,
    ) -> list[Path]:
        """Write the downloaded files to ``dest``.

        See :func:`downloader.core.saver.render_pattern` for the pattern grammar.
        """
        from .saver import save_result

        return await save_result(
            self,
            dest,
            pattern=pattern,
            overwrite=overwrite,
            album_dir=album_dir,
        )

    def summary(self) -> str:
        lines = [
            f"downloaded {len(self.files)} file(s) in {self.elapsed:.2f}s ({self.total_human})",
        ]
        for file in self.files:
            lines.append(f"  - {file.filename} ({file.size_human})")
        for key, message in self.errors.items():
            lines.append(f"  ! {key}: {message}")
        return "\n".join(lines)


def _unique_path(path: Path) -> Path:
    stem, suffix, parent = path.stem, path.suffix, path.parent
    counter = 1
    candidate = path
    while candidate.exists():
        candidate = parent / f"{stem}_{counter}{suffix}"
        counter += 1
    return candidate


def group_items(items: Sequence[MediaItem]) -> dict[str, MediaGroup]:
    """Bucket media items by kind, preserving order."""
    groups: dict[str, MediaGroup] = {}
    for item in items:
        key = str(item.kind)
        group = groups.get(key)
        if group is None:
            group = MediaGroup(kind=item.kind, label=str(item.kind))
            groups[key] = group
        group.items.append(item)
    return groups