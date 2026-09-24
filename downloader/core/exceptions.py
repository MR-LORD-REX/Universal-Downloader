"""Exception hierarchy shared by every downloader SDK.

Each platform package keeps its own backwards compatible subclass (for example
``downloader.reddit.exceptions.RedditError``) so callers can catch either the
narrow platform error or the shared :class:`DownloaderError` base.
"""

from __future__ import annotations


class DownloaderError(Exception):
    """Base class for every error raised by the SDKs."""


class UnsupportedURLError(DownloaderError):
    """The url does not belong to any platform the SDK knows about."""


class InvalidURLError(DownloaderError):
    """The url/ID could not be parsed into a post reference."""


class PostNotFoundError(DownloaderError):
    """Every configured provider failed to return data for the post."""


class MetadataError(DownloaderError):
    """Metadata extraction failed (bad payload, blocked request, ...)."""


class ProviderError(MetadataError):
    """A metadata provider failed to fetch or parse a post."""

    def __init__(self, provider: str, message: str) -> None:
        self.provider = provider
        super().__init__(f"[{provider}] {message}")


class AuthenticationError(DownloaderError):
    """Credentials are missing, invalid or expired."""


class NoMediaError(DownloaderError):
    """The post holds no downloadable media (self/link/poll only post)."""


class MediaNotAvailableError(DownloaderError):
    """A specific media format could not be resolved on the CDN."""


class SizeLimitExceededError(DownloaderError):
    """The media is larger than the caller supplied ``max_size_bytes``."""

    def __init__(self, size: int, limit: int) -> None:
        self.size = size
        self.limit = limit
        super().__init__(f"media is {size} bytes, above the {limit} byte limit")


class DownloadError(DownloaderError):
    """A download failed after exhausting the configured retries."""


class MuxingError(DownloaderError):
    """Audio/video muxing failed (usually ffmpeg is unavailable)."""


class SaveError(DownloaderError):
    """Writing downloaded media to disk failed."""