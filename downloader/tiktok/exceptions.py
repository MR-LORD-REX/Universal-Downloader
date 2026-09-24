"""Exception hierarchy for the TikTok SDK.

Every error also inherits from the shared
:class:`~downloader.core.exceptions.DownloaderError`, so
``except DownloaderError`` catches failures from any platform.
"""

from __future__ import annotations

from ..core.exceptions import (
    AuthenticationError,
    DownloadError,
    DownloaderError,
    InvalidURLError,
    MediaNotAvailableError,
    MetadataError,
    MuxingError,
    NoMediaError,
    PostNotFoundError,
    ProviderError,
    SaveError,
    SizeLimitExceededError,
    UnsupportedURLError,
)


class TikTokError(DownloaderError):
    """Base class for every error raised by the TikTok SDK."""


class VideoNotFoundError(TikTokError, PostNotFoundError):
    """The video does not exist, was deleted or belongs to a private account."""


class VideoUnavailableError(TikTokError, MediaNotAvailableError):
    """The video exists but the extractor returned no usable format."""


class RegionBlockedError(TikTokError, MetadataError):
    """TikTok refused the request for this IP address.

    TikTok answers ``status 10204`` ("Your IP address is blocked from accessing
    this post") to most datacentre and VPN ranges. Supplying a residential
    proxy, cookies or an ``app_info``/``device_id`` pair is the only fix.
    """


class PhotoPostUnsupportedError(TikTokError, MediaNotAvailableError):
    """The post is a photo/slideshow carousel, whose images yt-dlp cannot read.

    yt-dlp exposes the slideshow soundtrack only. The SDK raises this when the
    caller asked for videos and nothing but audio came back, so the reason is
    explicit instead of "no media found".
    """


class NoMediaFoundError(TikTokError, NoMediaError):
    """The post carries no downloadable media."""


__all__ = [
    "AuthenticationError",
    "DownloadError",
    "DownloaderError",
    "InvalidURLError",
    "MediaNotAvailableError",
    "MetadataError",
    "MuxingError",
    "NoMediaError",
    "NoMediaFoundError",
    "PhotoPostUnsupportedError",
    "PostNotFoundError",
    "ProviderError",
    "RegionBlockedError",
    "SaveError",
    "SizeLimitExceededError",
    "TikTokError",
    "UnsupportedURLError",
    "VideoNotFoundError",
    "VideoUnavailableError",
]