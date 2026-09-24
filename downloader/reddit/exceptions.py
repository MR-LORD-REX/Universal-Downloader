"""Exception hierarchy for the Reddit downloader SDK."""

from __future__ import annotations


class RedditError(Exception):
    """Base class for every error raised by the SDK."""


class InvalidURLError(RedditError):
    """The provided URL/ID could not be parsed into a post reference."""


class PostNotFoundError(RedditError):
    """Every configured provider failed to return data for the post."""


class ProviderError(RedditError):
    """A metadata provider failed to fetch or parse a post."""

    def __init__(self, provider: str, message: str) -> None:
        self.provider = provider
        super().__init__(f"[{provider}] {message}")


class AuthenticationError(RedditError):
    """OAuth credentials are missing, invalid or expired."""


class NoMediaError(RedditError):
    """The post holds no downloadable media (self/link/poll post)."""


class MediaNotAvailableError(RedditError):
    """A specific media format could not be resolved on the CDN."""


class DownloadError(RedditError):
    """A download failed after exhausting the configured retries."""


class MuxingError(RedditError):
    """Audio/video muxing failed (usually ffmpeg is unavailable)."""


class SaveError(RedditError):
    """Writing downloaded media to disk failed."""