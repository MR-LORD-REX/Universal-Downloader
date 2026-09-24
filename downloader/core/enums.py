"""Platform agnostic enumerations shared by every downloader SDK."""

from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    """Small ``str``-backed enum base (keeps ``str(x)`` readable on 3.10+)."""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return str(self.value)


class Platform(StrEnum):
    """Which service a post was fetched from."""

    REDDIT = "reddit"
    YOUTUBE = "youtube"
    TWITTER = "twitter"
    INSTAGRAM = "instagram"
    PINTEREST = "pinterest"
    TIKTOK = "tiktok"
    UNKNOWN = "unknown"


class MediaKind(StrEnum):
    """Kind of *asset* a media item represents."""

    IMAGE = "image"
    GIF = "gif"
    VIDEO = "video"
    AUDIO = "audio"
    SUBTITLE = "subtitle"
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
        MediaKind.SUBTITLE,
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
    ALBUM = "album"
    PLAYLIST = "playlist"
    CROSSPOST = "crosspost"
    EXTERNAL = "external"
    NONE = "none"


class FormatKind(StrEnum):
    """Technical kind of a single downloadable representation."""

    IMAGE = "image"
    GIF = "gif"
    VIDEO = "video"
    AUDIO = "audio"
    MUXED = "muxed"  # container that already carries both a video and audio track
    SUBTITLE = "subtitle"
    STORYBOARD = "storyboard"


class FormatOrigin(StrEnum):
    """Where a format description came from (useful when debugging)."""

    DIRECT = "direct"  # url straight from the post payload
    YTDLP = "ytdlp"  # produced by the yt-dlp extractor
    DASH = "dash"  # parsed out of a DASH manifest
    HLS = "hls"  # parsed out of an HLS manifest
    PROBE = "probe"  # discovered by probing a known CDN filename
    PREVIEW = "preview"  # thumbnail / preview rendition
    DERIVED = "derived"  # constructed from a media id (gallery originals)
    EXTERNAL = "external"  # non platform host (imgur, youtube, ...)
    FX = "fx"  # third party fixup api (fxtwitter)
    SCRAPE = "scrape"  # parsed out of page html / embedded json


class DownloadTarget(StrEnum):
    """Where downloaded bytes are kept before :meth:`save`."""

    MEMORY = "memory"
    DISK = "disk"


class Container(StrEnum):
    """Preferred container of the muxed output."""

    MP4 = "mp4"
    WEBM = "webm"
    MKV = "mkv"
    AUDIO = "audio"
    IMAGE = "image"
    ANY = "any"