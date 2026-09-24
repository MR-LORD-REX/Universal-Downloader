"""Exception hierarchy for the Instagram SDK.

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


class InstagramError(DownloaderError):
    """Base class for every error raised by the Instagram SDK."""


class InstagramNotFoundError(InstagramError, PostNotFoundError):
    """The post/reel does not exist, or was deleted."""


class LoginRequiredError(InstagramError, AuthenticationError):
    """Instagram refused the request; credentials are needed.

    Anonymous access is rate limited hard: the first few requests usually work,
    then Instagram answers 401 with "Please wait a few minutes before you try
    again". Supplying a session file (``InstagramConfig.session_file``) is the
    only reliable fix.
    """


class RateLimitedError(InstagramError, MetadataError):
    """Instagram throttled the client (HTTP 401/429 with a wait message)."""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        self.retry_after = retry_after
        super().__init__(message)


class ProfileNotFoundError(InstagramError, PostNotFoundError):
    """The account does not exist."""


class PrivateProfileError(InstagramError, AuthenticationError):
    """The account is private; a logged in session is required."""


class StoryUnavailableError(InstagramError, MediaNotAvailableError):
    """Stories/highlights need a logged in session and expire after 24 hours."""


class InstaloaderMissingError(InstagramError, MetadataError):
    """The optional ``instaloader`` dependency is not installed."""


class NoMediaFoundError(InstagramError, NoMediaError):
    """The post carries no downloadable media (text/link only)."""


__all__ = [
    "AuthenticationError",
    "DownloadError",
    "DownloaderError",
    "InstagramError",
    "InstagramNotFoundError",
    "InstaloaderMissingError",
    "InvalidURLError",
    "LoginRequiredError",
    "MediaNotAvailableError",
    "MetadataError",
    "MuxingError",
    "NoMediaError",
    "NoMediaFoundError",
    "PostNotFoundError",
    "PrivateProfileError",
    "ProfileNotFoundError",
    "ProviderError",
    "RateLimitedError",
    "SaveError",
    "SizeLimitExceededError",
    "StoryUnavailableError",
    "UnsupportedURLError",
]
