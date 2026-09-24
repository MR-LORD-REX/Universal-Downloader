"""Exception hierarchy for the YouTube SDK.

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
    ProviderError,
    SaveError,
    SizeLimitExceededError,
)


class YouTubeError(DownloaderError):
    """Base class for every error raised by the YouTube SDK."""


class VideoUnavailableError(YouTubeError):
    """The video is private, age restricted, geo blocked or removed."""


class LiveStreamError(YouTubeError):
    """The url points at a live stream, which cannot be downloaded as a file."""


class CommunityPostError(YouTubeError):
    """The community post page could not be parsed."""


class ExtractionError(YouTubeError):
    """yt-dlp failed to extract the requested url."""


__all__ = [
    "AuthenticationError",
    "CommunityPostError",
    "DownloadError",
    "DownloaderError",
    "ExtractionError",
    "InvalidURLError",
    "LiveStreamError",
    "MediaNotAvailableError",
    "MetadataError",
    "MuxingError",
    "NoMediaError",
    "ProviderError",
    "SaveError",
    "SizeLimitExceededError",
    "VideoUnavailableError",
    "YouTubeError",
]