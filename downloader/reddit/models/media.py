"""Media level models: formats, items, groups and downloaded files."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Iterator, Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field

from ..exceptions import MediaNotAvailableError
from .enums import FormatKind, FormatOrigin, MediaKind, StrEnum


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


_SELECTABLE_KINDS = {
    FormatKind.IMAGE,
    FormatKind.GIF,
    FormatKind.VIDEO,
    FormatKind.MUXED,
}


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
    width: Optional[int] = None
    height: Optional[int] = None
    quality_height: Optional[int] = None
    """Height of the rendition ladder (a vertical 1080p video is 1080 here)."""
    fps: Optional[float] = None
    bitrate_kbps: Optional[int] = None
    codecs: Optional[str] = None
    size_bytes: Optional[int] = None
    has_audio: Optional[bool] = None
    quality_label: Optional[str] = None
    note: Optional[str] = None

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
    def is_audio_only(self) -> bool:
        return self.kind == FormatKind.AUDIO

    def display(self) -> str:
        bits = [self.kind.value]
        if self.resolution:
            bits.append(self.resolution)
        if self.size_human:
            bits.append(self.size_human)
        if self.bitrate_kbps:
            bits.append(f"{self.bitrate_kbps}kbps")
        bits.append(self.origin.value)
        return " | ".join(bits)


class MediaItem(BaseModel):
    """A single logical medium: one image, one video, one gallery entry..."""

    model_config = ConfigDict(extra="ignore")

    index: int = 0
    id: Optional[str] = None
    kind: MediaKind = MediaKind.UNKNOWN
    caption: Optional[str] = None
    source_url: Optional[str] = None
    mime_type: Optional[str] = None
    extension: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    duration: Optional[float] = None
    has_audio: Optional[bool] = None
    size_bytes: Optional[int] = None
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
        quality: str | int = "best",
        *,
        prefer_muxed: bool = True,
        container: Optional[str] = None,
    ) -> MediaFormat:
        """Pick the format that matches ``quality``.

        ``quality`` accepts ``"best"``, ``"worst"``/``"smallest"``, an integer
        target height (``720``), or a label such as ``"1080p"``.
        """
        return pick_format(
            self.selectable or self.formats,
            quality,
            prefer_muxed=prefer_muxed,
            container=container,
        )

    def best_audio(self) -> Optional[MediaFormat]:
        audios = self.audio_formats
        if not audios:
            return None
        return max(audios, key=lambda f: (f.bitrate_kbps or 0, f.size_bytes or 0))

    def add_formats(self, formats: Sequence[MediaFormat]) -> None:
        """Add formats, skipping duplicates by url+kind."""
        seen = {(f.url, f.kind) for f in self.formats}
        for fmt in formats:
            key = (fmt.url, fmt.kind)
            if key not in seen:
                seen.add(key)
                self.formats.append(fmt)

    def merged(self) -> "MediaItem":
        """Collapse duplicate urls, keeping the richest description."""
        best: dict[tuple[str, str], MediaFormat] = {}
        for fmt in self.formats:
            key = (fmt.url, str(fmt.kind))
            current = best.get(key)
            if current is None or _richness(fmt) > _richness(current):
                best[key] = fmt
        self.formats = list(best.values())
        if self.size_bytes is None and self.formats:
            self.size_bytes = max(
                (f.size_bytes or 0) for f in self.formats
            ) or None
        return self


def _richness(fmt: MediaFormat) -> int:
    score = 0
    for field in ("width", "height", "bitrate_kbps", "size_bytes", "codecs", "mime_type"):
        if getattr(fmt, field) is not None:
            score += 1
    return score


def _rank(fmt: MediaFormat, prefer_muxed: bool) -> tuple[int, int, int, int]:
    """Sort key used by :func:`pick_format` (bigger == better)."""
    muxed_bonus = 1 if (prefer_muxed and fmt.kind == FormatKind.MUXED) else 0
    return (
        fmt.quality_height or fmt.height or fmt.width or 0,
        muxed_bonus,
        fmt.bitrate_kbps or 0,
        fmt.size_bytes or 0,
    )


def pick_format(
    formats: Sequence[MediaFormat],
    quality: str | int = "best",
    *,
    prefer_muxed: bool = True,
    container: Optional[str] = None,
) -> MediaFormat:
    """Select a format from ``formats`` using a quality policy.

    Notes
    -----
    * ``"best"`` / ``"worst"`` rank by resolution, then by whether audio is
      already muxed in, then bitrate and file size.
    * An integer/``"720p"`` target picks the largest format that is **not
      bigger** than the target; if every format is bigger, the smallest one is
      returned so a download is always possible.
    """
    if not formats:
        raise MediaNotAvailableError("no downloadable formats available")

    candidates = list(formats)
    if container:
        narrowed = [f for f in candidates if (f.container or "").lower() == container.lower()]
        if narrowed:
            candidates = narrowed

    if isinstance(quality, int) or (isinstance(quality, str) and quality.isdigit()):
        target = int(quality)
        ordered = sorted(candidates, key=lambda f: _rank(f, prefer_muxed), reverse=True)
        fitting = [f for f in ordered if (f.quality_height or f.height or 0) <= target]
        return fitting[0] if fitting else ordered[-1]

    key = str(quality).lower().strip()
    if key.endswith("p") and key[:-1].isdigit():
        return pick_format(candidates, int(key[:-1]), prefer_muxed=prefer_muxed)

    ordered = sorted(candidates, key=lambda f: _rank(f, prefer_muxed), reverse=True)
    if key in ("best", "max", "highest", "largest", "source"):
        return ordered[0]
    if key in ("worst", "min", "lowest", "smallest", "tiny"):
        return ordered[-1]
    # Unknown label: fall back to the biggest format.
    return ordered[0]


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

    def write(self, target: str | Path, *, overwrite: bool = False) -> Path:
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
        """Drop in-memory bytes (and delete the temp file when present)."""
        self.data = None
        if self.path is not None and self.path.exists() and self.note == "temp":
            self.path.unlink(missing_ok=True)
            self.path = None


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

