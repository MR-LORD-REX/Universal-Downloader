"""Exception hierarchy for the Pinterest SDK.

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


class PinterestError(DownloaderError):
    """Base class for every error raised by the Pinterest SDK."""


class PinNotFoundError(PinterestError, PostNotFoundError):
    """The pin does not exist, was deleted or is private."""


class PinUnavailableError(PinterestError, MetadataError):
    """The pin exists but its media cannot be read (region or secret board)."""


class BoardNotFoundError(PinterestError, PostNotFoundError):
    """The board does not exist or its owner is private."""


class PinterestAPIError(PinterestError, ProviderError):
    """The Pinterest resource api failed or returned an unusable payload."""

    def __init__(self, message: str) -> None:
        super().__init__("pinterest", message)


class NoMediaFoundError(PinterestError, NoMediaError):
    """The pin carries no downloadable media (a plain link pin)."""


__all__ = [
    "AuthenticationError",
    "BoardNotFoundError",
    "DownloadError",
    "DownloaderError",
    "InvalidURLError",
    "MediaNotAvailableError",
    "MetadataError",
    "MuxingError",
    "NoMediaError",
    "NoMediaFoundError",
    "PinNotFoundError",
    "PinUnavailableError",
    "PinterestAPIError",
    "PinterestError",
    "PostNotFoundError",
    "ProviderError",
    "SaveError",
    "SizeLimitExceededError",
    "UnsupportedURLError",
]