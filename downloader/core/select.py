"""Quality parsing and format selection shared by every SDK.

The selector is deliberately independent of yt-dlp's ``-f`` grammar: the SDK
already holds a normalised :class:`~downloader.core.models.MediaFormat` ladder,
so picking a rendition is a pure function over that ladder. That keeps
``get_metadata`` able to report *exactly* which url and size a quality choice
would download, before any bytes move.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from .enums import FormatKind
from .exceptions import MediaNotAvailableError
from .models import MediaFormat, MediaItem, parse_height, pick_audio, pick_format

MODE_BEST = "best"
MODE_WORST = "worst"
MODE_HEIGHT = "height"
MODE_AUDIO = "audio"
MODE_FORMAT = "format"
MODE_PAIR = "pair"

_BEST = frozenset({"best", "max", "highest", "largest", "source", "bestvideo", "hd", "original"})
_WORST = frozenset({"worst", "min", "lowest", "smallest", "tiny", "data", "small", "low"})
_AUDIO = frozenset({"audio", "bestaudio", "music", "sound", "mp3", "m4a"})

_VIDEO_KINDS = frozenset({FormatKind.VIDEO, FormatKind.MUXED})
_AUDIO_KINDS = frozenset({FormatKind.AUDIO})

_AAC_CODECS = ("mp4a", "aac")
_H264_CODECS = ("avc1", "h264")


@dataclass(slots=True)
class QualitySpec:
    """A parsed quality request."""

    raw: "str | int" = "best"
    mode: str = MODE_BEST
    height: Optional[int] = None
    format_id: Optional[str] = None
    audio_id: Optional[str] = None
    container: Optional[str] = None
    codec: Optional[str] = None
    audio_codec: Optional[str] = None
    audio_bitrate: Optional[int] = None
    prefer_muxed: bool = True
    strict: bool = False
    """When ``True`` a quality taller than the source raises instead of clamping."""

    @property
    def wants_audio_only(self) -> bool:
        return self.mode == MODE_AUDIO

    @property
    def label(self) -> str:
        """Short human label used in filenames and quality ladders."""
        if self.mode == MODE_AUDIO:
            base = "audio"
            return f"{base}{self.audio_bitrate}" if self.audio_bitrate else base
        if self.mode == MODE_FORMAT:
            return str(self.format_id)
        if self.mode == MODE_PAIR:
            return f"{self.format_id}+{self.audio_id}"
        if self.mode == MODE_WORST:
            return "worst"
        if self.height:
            return f"{self.height}p"
        return "best"


def parse_quality(
    quality: "str | int | QualitySpec" = "best",
    *,
    container: Optional[str] = None,
    codec: Optional[str] = None,
    audio_codec: Optional[str] = None,
    audio_bitrate: Optional[int] = None,
    prefer_muxed: bool = True,
    strict: bool = False,
) -> QualitySpec:
    """Turn ``"1080p"`` / ``1080`` / ``"bestvideo+bestaudio"`` into a spec."""
    if isinstance(quality, QualitySpec):
        return quality
    if isinstance(quality, int):
        return QualitySpec(
            raw=quality,
            mode=MODE_HEIGHT if quality > 0 else MODE_BEST,
            height=quality or None,
            container=container,
            codec=codec,
            audio_codec=audio_codec,
            audio_bitrate=audio_bitrate,
            prefer_muxed=prefer_muxed,
        )
    text = str(quality).strip() or "best"
    lowered = text.lower()
    common = {
        "container": container,
        "codec": codec,
        "audio_codec": audio_codec,
        "audio_bitrate": audio_bitrate,
        "prefer_muxed": prefer_muxed,
        "strict": strict,
        "raw": text,
    }
    if lowered in _AUDIO:
        return QualitySpec(mode=MODE_AUDIO, **common)  # type: ignore[arg-type]
    if lowered in _BEST:
        return QualitySpec(mode=MODE_BEST, **common)  # type: ignore[arg-type]
    if lowered in _WORST:
        return QualitySpec(mode=MODE_WORST, **common)  # type: ignore[arg-type]
    if "+" in text:
        left, _, right = text.partition("+")
        return QualitySpec(
            mode=MODE_PAIR, format_id=left.strip(), audio_id=right.strip(), **common
        )  # type: ignore[arg-type]
    target = parse_height(text)
    if target is not None:
        return QualitySpec(mode=MODE_HEIGHT, height=target, **common)  # type: ignore[arg-type]
    return QualitySpec(mode=MODE_FORMAT, format_id=text, **common)  # type: ignore[arg-type]


def _height_of(fmt: MediaFormat) -> int:
    return fmt.quality_height or fmt.height or 0


def _codec_matches(fmt: MediaFormat, wanted: Optional[str], *, audio: bool) -> int:
    if not wanted:
        return 0
    wanted = wanted.lower()
    codecs = [c.strip().lower() for c in (fmt.codecs or "").split(",") if c.strip()]
    target = {wanted}
    if wanted in ("aac", "m4a"):
        target |= set(_AAC_CODECS)
    if wanted in ("h264", "avc"):
        target |= set(_H264_CODECS)
    actual = fmt.audio_codec if audio else fmt.video_codec
    pool = codecs + ([actual.lower()] if actual else [])
    return 1 if any(any(c.startswith(t) for t in target) for c in pool) else 0


def video_candidates(item: MediaItem) -> list[MediaFormat]:
    """Every video bearing rendition of ``item``, storyboards excluded."""
    return [f for f in item.formats if f.kind in _VIDEO_KINDS]


def audio_candidates(item: MediaItem) -> list[MediaFormat]:
    """Every audio track of ``item`` (standalone audio or split audio)."""
    tracks = [f for f in item.formats if f.kind in _AUDIO_KINDS]
    if tracks:
        return tracks
    return [f for f in item.formats if f.has_audio is True and f.has_video is False]


def select_video(item: MediaItem, spec: QualitySpec) -> Optional[MediaFormat]:
    """Best video rendition of ``item`` for ``spec`` (``None`` when audio only)."""
    if spec.wants_audio_only:
        return None
    pool = video_candidates(item)
    if not pool:
        return None
    if spec.format_id is not None and spec.mode in (MODE_FORMAT, MODE_PAIR):
        explicit = item.format(spec.format_id)
        if explicit is None:
            raise MediaNotAvailableError(
                f"item {item.index} has no format {spec.format_id!r}"
            )
        return explicit
    return pick_format(
        pool,
        spec.raw,
        prefer_muxed=spec.prefer_muxed,
        container=spec.container,
        codec=spec.codec,
        strict=spec.strict,
    )


def select_audio(item: MediaItem, spec: QualitySpec) -> Optional[MediaFormat]:
    """Best audio track of ``item`` for ``spec`` (``None`` when absent)."""
    if spec.mode == MODE_PAIR and spec.audio_id:
        explicit = item.format(spec.audio_id)
        if explicit is not None:
            return explicit
    return pick_audio(
        audio_candidates(item),
        min_bitrate=spec.audio_bitrate or 0,
        codec=spec.audio_codec or spec.codec,
        container=spec.container,
    )


def select_still(item: MediaItem, spec: QualitySpec) -> Optional[MediaFormat]:
    """Pick an image rendition for still-only items (photos, galleries)."""
    pool = [
        f
        for f in item.formats
        if f.kind in (FormatKind.IMAGE, FormatKind.GIF) and not f.is_manifest
    ]
    if not pool:
        return None
    return pick_format(pool, spec.raw, prefer_muxed=False, container=spec.container)


@dataclass(slots=True)
class PlanEntry:
    """Everything needed to fetch one medium."""

    item: MediaItem
    primary: MediaFormat
    audio: Optional[MediaFormat] = None
    position: int = 0

    @property
    def needs_mux(self) -> bool:
        """``True`` when a separate audio track has to be merged in."""
        return self.audio is not None and not self.primary.is_muxed

    @property
    def total_size(self) -> Optional[int]:
        if self.primary.size_bytes is None:
            return None
        if self.needs_mux and self.audio is not None and self.audio.size_bytes is not None:
            return self.primary.size_bytes + self.audio.size_bytes
        return self.primary.size_bytes

    @property
    def size_is_approx(self) -> bool:
        if self.primary.size_is_approx:
            return True
        return bool(self.needs_mux and self.audio and self.audio.size_is_approx)


def build_plan(
    items: Sequence[MediaItem],
    spec: "QualitySpec | str | int" = "best",
    *,
    include_audio: bool = True,
    only: Optional[Sequence[int]] = None,
) -> list[PlanEntry]:
    """Resolve ``(item, video, audio)`` download entries for every item."""
    if not isinstance(spec, QualitySpec):
        spec = parse_quality(spec)
    wanted = set(only) if only is not None else None
    plan: list[PlanEntry] = []
    for item in items:
        if wanted is not None and item.index not in wanted:
            continue
        if not item.formats:
            continue
        try:
            primary = select_video(item, spec)
        except MediaNotAvailableError:
            if spec.format_id:
                raise
            primary = None
        if primary is None:
            primary = select_audio(item, spec)
        if primary is None:
            primary = select_still(item, spec)
        if primary is None:
            continue
        audio: Optional[MediaFormat] = None
        if include_audio and not primary.is_muxed and item.has_audio is not False:
            audio = select_audio(item, spec)
        plan.append(PlanEntry(item=item, primary=primary, audio=audio, position=len(plan)))
    if not plan:
        raise MediaNotAvailableError("no format matched the requested quality")
    return plan