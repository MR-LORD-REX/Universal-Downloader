"""Enumerations used across the SDK."""

from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    """Small ``str``-backed enum base (keeps ``str(x)`` readable on 3.10+)."""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return str(self.value)


class MediaKind(StrEnum):
    """Kind of *asset* a media item represents."""

    IMAGE = "image"
    GIF = "gif"
    VIDEO = "video"
    AUDIO = "audio"
    GALLERY = "gallery"
    EXTERNAL_IMAGE = "external_image"
    EXTERNAL_VIDEO = "external_video"
    LINK = "link"
    TEXT = "text"
    POLL = "poll"
    UNKNOWN = "unknown"

    @property
    def is_downloadable(self) -> bool:
        """``True`` when the SDK can fetch the bytes of this kind directly."""
        return self in _DOWNLOADABLE_KINDS

    @property
    def is_visual(self) -> bool:
        """``True`` for kinds that can be rendered as an image/video preview."""
        return self in _VISUAL_KINDS


_DOWNLOADABLE_KINDS = frozenset(
    {
        MediaKind.IMAGE,
        MediaKind.GIF,
        MediaKind.VIDEO,
        MediaKind.AUDIO,
        MediaKind.EXTERNAL_IMAGE,
    }
)
_VISUAL_KINDS = frozenset(
    {MediaKind.IMAGE, MediaKind.GIF, MediaKind.VIDEO, MediaKind.EXTERNAL_IMAGE}
)


class MediaGroupType(StrEnum):
    """How the media of a post is grouped."""

    SINGLE = "single"
    GALLERY = "gallery"
    CROSSPOST = "crosspost"
    EXTERNAL = "external"
    NONE = "none"


class FormatKind(StrEnum):
    """Technical kind of a single downloadable representation."""

    IMAGE = "image"
    GIF = "gif"
    VIDEO = "video"
    AUDIO = "audio"
    MUXED = "muxed"  # video container that already carries an audio track


class FormatOrigin(StrEnum):
    """Where a format description came from (useful when debugging)."""

    DIRECT = "direct"  # url straight from the post payload
    DASH = "dash"  # parsed out of DASHPlaylist.mpd
    HLS = "hls"  # parsed out of HLSPlaylist.m3u8
    PROBE = "probe"  # discovered by probing known CDN filenames
    PREVIEW = "preview"  # reddit preview/thumbnail rendition
    DERIVED = "derived"  # constructed from a media id (gallery originals)
    EXTERNAL = "external"  # non-reddit host (imgur, youtube, ...)


class DownloadTarget(StrEnum):
    """Where downloaded bytes are kept."""

    MEMORY = "memory"
    DISK = "disk"