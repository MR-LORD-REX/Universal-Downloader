"""Exception hierarchy for the Twitter/X SDK.

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
    UnsupportedURLError,
)


class TwitterError(DownloaderError):
    """Base class for every error raised by the Twitter SDK."""


class TweetNotFoundError(TwitterError):
    """The tweet is deleted, protected or does not exist."""


class TweetUnavailableError(TwitterError):
    """The tweet exists but its media cannot be read (NSFW, age gated, region)."""


class MediaAPIError(TwitterError):
    """The third party media api (fxtwitter) failed or returned no payload."""


class NoMediaFoundError(TwitterError, NoMediaError):
    """The tweet carries no downloadable media (text only)."""


__all__ = [
    "AuthenticationError",
    "DownloadError",
    "DownloaderError",
    "InvalidURLError",
    "MediaAPIError",
    "MediaNotAvailableError",
    "MetadataError",
    "MuxingError",
    "NoMediaError",
    "NoMediaFoundError",
    "ProviderError",
    "SaveError",
    "SizeLimitExceededError",
    "TweetNotFoundError",
    "TweetUnavailableError",
    "TwitterError",
    "UnsupportedURLError",
]