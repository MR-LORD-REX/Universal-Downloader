"""One process-wide `downloader.Downloader`, configured from the environment.

The SDK clients are lazy, share a connection pool and cache, and are closed
together when the bot shuts down. Everything that fetches bytes goes through
here so proxy/cookie/timeouts are configured in exactly one place.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Optional

from downloader import Downloader
from downloader.core.models import DownloadResult, PostMetadata, human_size

from bot.config import settings

logger = logging.getLogger(__name__)


class DownloaderService:
    """Thin async facade over :class:`downloader.Downloader`."""

    def __init__(
        self,
        *,
        proxy: str = "",
        cookies_file: Optional[Path] = None,
        instagram_session_file: Optional[Path] = None,
        cache_dir: Optional[Path] = None,
        timeout: float = 30.0,
        progress_callback: Optional[Any] = None,
    ) -> None:
        self._proxy = proxy or ""
        self._cookies_file = Path(cookies_file) if cookies_file else None
        self._instagram_session_file = (
            Path(instagram_session_file) if instagram_session_file else None
        )
        self._cache_dir = Path(cache_dir) if cache_dir else settings.temp_dir / "cache"
        self._timeout = timeout
        self._progress = progress_callback
        self._downloader: Optional[Downloader] = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------ lifecycle
    def _build_options(self, *, cookies: bool = True) -> dict[str, Any]:
        options: dict[str, Any] = {"timeout": self._timeout, "cache_dir": self._cache_dir}
        if self._proxy:
            options["proxy"] = self._proxy
        if cookies and self._cookies_file and self._cookies_file.exists():
            options["cookiefile"] = self._cookies_file
        return options

    def _build_instagram_options(self) -> dict[str, Any]:
        """Instagram options: proxy/cache plus the instaloader session file.

        A cookie jar is meaningless here (instaloader keeps its own session),
        so it is deliberately not forwarded.
        """
        options = {
            key: value
            for key, value in self._build_options(cookies=False).items()
            if key in ("proxy", "cache_dir", "timeout")
        }
        if self._instagram_session_file is not None:
            if not self._instagram_session_file.exists():
                logger.warning(
                    "INSTAGRAM_SESSION_FILE=%s does not exist; Instagram will be "
                    "accessed anonymously and rate limit after a few requests",
                    self._instagram_session_file,
                )
            options["session_file"] = str(self._instagram_session_file)
        return options

    async def start(self) -> Downloader:
        async with self._lock:
            if self._downloader is None:
                self._cache_dir.mkdir(parents=True, exist_ok=True)
                self._downloader = Downloader(
                    progress_callback=self._progress,
                    youtube_options=self._build_options(),
                    twitter_options=self._build_options(),
                    instagram_options=self._build_instagram_options(),
                    pinterest_options=self._build_options(),
                    tiktok_options=self._build_options(),
                    reddit_options={k: v for k, v in self._build_options(cookies=False).items()
                                    if k in ("proxy", "cache_dir", "timeout")},
                )
                logger.info("downloader ready (proxy=%s, cookies=%s)", bool(self._proxy), bool(self._cookies_file))
            return self._downloader

    @property
    def client(self) -> Downloader:
        if self._downloader is None:
            raise RuntimeError("DownloaderService.start() has not been awaited")
        return self._downloader

    async def close(self) -> None:
        async with self._lock:
            if self._downloader is not None:
                await self._downloader.close()
                self._downloader = None

    # ------------------------------------------------------------ operations
    async def get_metadata(self, url: str, **kwargs: Any) -> PostMetadata:
        await self.start()
        return await self.client.get_metadata(url, **kwargs)

    async def download(
        self,
        metadata: PostMetadata,
        *,
        quality: "str | int | None" = None,
        max_size_bytes: Optional[int] = None,
        only: Optional[list[int]] = None,
        progress: Optional[Any] = None,
    ) -> DownloadResult:
        await self.start()
        return await self.client.download(
            metadata,
            quality=quality or "best",
            target="disk",
            temp_dir=settings.temp_dir,
            max_size_bytes=max_size_bytes,
            only=only,
            progress=progress,
        )

    async def fetch(self, url: str, *, max_bytes: Optional[int] = None) -> bytes:
        """Server-side fetch of a static file (the url-send fallback)."""
        await self.start()
        return await self.client.fetch(url, max_bytes=max_bytes)

    async def size_of(self, url: str) -> Optional[int]:
        await self.start()
        size, _ = await self.client.size_of(url)
        return size

    @staticmethod
    def human(size: Optional[int]) -> str:
        return human_size(size) or "unknown"


__all__ = ["DownloaderService"]
