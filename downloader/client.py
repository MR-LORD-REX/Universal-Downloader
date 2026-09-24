"""One facade over every platform SDK.

``Downloader`` routes a url to the right SDK, hands back a single canonical
:class:`~downloader.core.models.PostMetadata` shape no matter where the post
came from, and reuses the same download engine, quality grammar, progress
events and ``save`` semantics everywhere. It is the entry point a Telegram bot
wants: one object, one metadata model, one result type.

```python
async with Downloader() as dl:
    meta = await dl.get_metadata(url)
    print(meta.platform, meta.media_type, meta.media_group_type)
    print(meta.links())          # direct CDN urls, grouped by kind
    print(meta.size_human)       # known before downloading
    result = await dl.download(meta, quality="1080p", max_size_bytes=50 << 20)
    await result.save("downloads")
```
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional, Sequence

from .adapters import RedditDriver, make_reddit_driver
from .core.enums import Platform
from .core.exceptions import UnsupportedURLError
from .core.http import HttpClient
from .core.models import DownloadResult, PostMetadata, human_size
from .youtube import YouTubeClient, is_youtube_url
from .twitter import TwitterClient, is_twitter_url
from .instagram import InstagramClient, is_instagram_url
from .pinterest import PinterestClient, is_pinterest_url
from .tiktok import TikTokClient, is_tiktok_url

DEFAULT_PLATFORM_ORDER: tuple[Platform, ...] = (
    Platform.YOUTUBE,
    Platform.TWITTER,
    Platform.INSTAGRAM,
    Platform.PINTEREST,
    Platform.TIKTOK,
    Platform.REDDIT,
)


class Downloader:
    """The orchestrating SDK: one API for Reddit, YouTube, Twitter/X, Instagram,
    Pinterest and TikTok.

    Platform clients are created lazily on first use, share the progress
    callback, and are closed together by :meth:`close` (or ``async with``).
    """

    def __init__(
        self,
        *,
        progress_callback: Optional[Any] = None,
        youtube: Optional[YouTubeClient] = None,
        twitter: Optional[TwitterClient] = None,
        instagram: Optional[InstagramClient] = None,
        pinterest: Optional[PinterestClient] = None,
        tiktok: Optional[TikTokClient] = None,
        reddit: Optional[Any] = None,
        reddit_options: Optional[dict[str, Any]] = None,
        youtube_options: Optional[dict[str, Any]] = None,
        twitter_options: Optional[dict[str, Any]] = None,
        instagram_options: Optional[dict[str, Any]] = None,
        pinterest_options: Optional[dict[str, Any]] = None,
        tiktok_options: Optional[dict[str, Any]] = None,
        platforms: Optional[Sequence[Platform | str]] = None,
    ) -> None:
        self._progress = progress_callback
        self._youtube = youtube
        self._twitter = twitter
        self._instagram = instagram
        self._pinterest = pinterest
        self._tiktok = tiktok
        self._reddit = RedditDriver(reddit) if reddit is not None else None
        self._options = {
            Platform.YOUTUBE: dict(youtube_options or {}),
            Platform.TWITTER: dict(twitter_options or {}),
            Platform.INSTAGRAM: dict(instagram_options or {}),
            Platform.PINTEREST: dict(pinterest_options or {}),
            Platform.TIKTOK: dict(tiktok_options or {}),
            Platform.REDDIT: dict(reddit_options or {}),
        }
        self._enabled = (
            tuple(_as_platform(p) for p in platforms) if platforms else DEFAULT_PLATFORM_ORDER
        )
        self._owned: list[Any] = []
        self._static_http: Optional[HttpClient] = None

    # ------------------------------------------------------------- lifecycle
    async def __aenter__(self) -> "Downloader":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        """Close every client this facade created."""
        for client in list(self._owned):
            try:
                await client.close()
            except Exception:  # pragma: no cover - best effort cleanup
                pass
        self._owned.clear()

    # --------------------------------------------------------------- clients
    @property
    def youtube(self) -> YouTubeClient:
        """The YouTube client (created on first access)."""
        if self._youtube is None:
            self._youtube = YouTubeClient(
                progress_callback=self._progress, **self._options[Platform.YOUTUBE]
            )
            self._owned.append(self._youtube)
        return self._youtube

    @property
    def twitter(self) -> TwitterClient:
        """The Twitter/X client (created on first access)."""
        if self._twitter is None:
            self._twitter = TwitterClient(
                progress_callback=self._progress, **self._options[Platform.TWITTER]
            )
            self._owned.append(self._twitter)
        return self._twitter

    @property
    def instagram(self) -> InstagramClient:
        """The Instagram client (created on first access)."""
        if self._instagram is None:
            self._instagram = InstagramClient(
                progress_callback=self._progress, **self._options[Platform.INSTAGRAM]
            )
            self._owned.append(self._instagram)
        return self._instagram

    @property
    def pinterest(self) -> PinterestClient:
        """The Pinterest client (created on first access)."""
        if self._pinterest is None:
            self._pinterest = PinterestClient(
                progress_callback=self._progress, **self._options[Platform.PINTEREST]
            )
            self._owned.append(self._pinterest)
        return self._pinterest

    @property
    def tiktok(self) -> TikTokClient:
        """The TikTok client (created on first access)."""
        if self._tiktok is None:
            self._tiktok = TikTokClient(
                progress_callback=self._progress, **self._options[Platform.TIKTOK]
            )
            self._owned.append(self._tiktok)
        return self._tiktok

    @property
    def http(self) -> HttpClient:
        """A shared core :class:`HttpClient` for direct/static links.

        Static assets (images, thumbnails, CDN files) do not need a platform
        SDK, so this client is what backs :meth:`fetch` and :meth:`size_of` for
        every url that no platform client already owns.
        """
        if self._static_http is None:
            self._static_http = HttpClient()
            self._owned.append(self._static_http)
        return self._static_http

    @property
    def reddit(self) -> RedditDriver:
        """The Reddit driver (created on first access)."""
        if self._reddit is None:
            self._reddit = make_reddit_driver(
                progress_callback=self._progress, **self._options[Platform.REDDIT]
            )
            self._owned.append(self._reddit)
        return self._reddit

    def driver_for(self, platform: Platform) -> Any:
        """The driver handling ``platform``."""
        return {
            Platform.YOUTUBE: self.youtube,
            Platform.TWITTER: self.twitter,
            Platform.INSTAGRAM: self.instagram,
            Platform.PINTEREST: self.pinterest,
            Platform.TIKTOK: self.tiktok,
            Platform.REDDIT: self.reddit,
        }[platform]

    # -------------------------------------------------------------- routing
    @staticmethod
    def platform_of(url: str) -> Platform:
        """Which platform ``url`` belongs to (``Platform.UNKNOWN`` if none)."""
        if is_youtube_url(url):
            return Platform.YOUTUBE
        if is_twitter_url(url):
            return Platform.TWITTER
        if is_instagram_url(url):
            return Platform.INSTAGRAM
        if is_pinterest_url(url):
            return Platform.PINTEREST
        if is_tiktok_url(url):
            return Platform.TIKTOK
        if RedditDriver.supports(url):
            return Platform.REDDIT
        return Platform.UNKNOWN

    @classmethod
    def supports(cls, url: str) -> bool:
        """``True`` when any bundled SDK can handle ``url``."""
        return cls.platform_of(url) is not Platform.UNKNOWN

    def _resolve_platform(self, url: str, platform: Optional[Platform | str]) -> Platform:
        resolved = _as_platform(platform) if platform else self.platform_of(url)
        if resolved is Platform.UNKNOWN:
            raise UnsupportedURLError(f"no SDK knows how to handle {url!r}")
        if resolved not in self._enabled:
            raise UnsupportedURLError(f"the {resolved} SDK is disabled on this Downloader")
        return resolved

    # ------------------------------------------------------------- metadata
    async def get_metadata(
        self,
        url: str,
        *,
        platform: Optional[Platform | str] = None,
        **kwargs: Any,
    ) -> PostMetadata:
        """Fetch canonical metadata for any supported url."""
        resolved = self._resolve_platform(url, platform)
        return await self.driver_for(resolved).get_metadata(url, **kwargs)

    async def get_metadata_many(
        self,
        urls: Sequence[str],
        *,
        concurrency: int = 4,
        **kwargs: Any,
    ) -> list["PostMetadata | Exception"]:
        """Fetch several urls concurrently, returning errors in place."""
        semaphore = asyncio.Semaphore(max(1, concurrency))

        async def run(target: str) -> "PostMetadata | Exception":
            async with semaphore:
                try:
                    return await self.get_metadata(target, **kwargs)
                except Exception as exc:  # noqa: BLE001 - surfaced to the caller
                    return exc

        return list(await asyncio.gather(*(run(url) for url in urls)))

    # ------------------------------------------------------------- download
    async def download(self, source: "str | PostMetadata", **kwargs: Any) -> DownloadResult:
        """Download a url or an already fetched :class:`PostMetadata`."""
        if isinstance(source, PostMetadata):
            driver = self.driver_for(source.platform)
            driver_kwargs = _driver_kwargs(source.platform, kwargs)
            return await driver.download(source, **driver_kwargs)
        metadata = await self.get_metadata(source)
        return await self.download(metadata, **kwargs)

    async def save(
        self, source: "str | PostMetadata | DownloadResult", dest: Any, **kwargs: Any
    ) -> list[Any]:
        """Download (when needed) and write the media under ``dest``."""
        if isinstance(source, DownloadResult):
            return await source.save(dest, **kwargs)
        if isinstance(source, PostMetadata):
            metadata = source
        else:
            metadata = await self.get_metadata(source)
        return await self.download(metadata, dest=dest, **kwargs)

    # --------------------------------------------------------- static files
    async def fetch(
        self,
        url: str,
        *,
        max_bytes: Optional[int] = None,
        headers: Optional[dict[str, str]] = None,
    ) -> bytes:
        """Fetch a static file (photo, thumbnail) directly, with a size guard.

        This is the fast path for a Telegram bot: images and other static
        assets can be handed to the Bot API by url, but when the bytes are
        needed locally this returns them without spinning up a platform SDK.
        """
        http = self._http_for(url)
        async with http.open(url, headers=headers) as (info, chunks):
            if max_bytes is not None and info.size is not None and info.size > max_bytes:
                raise UnsupportedURLError(
                    f"{url} is {human_size(info.size)} which is above the {human_size(max_bytes)} limit"
                )
            buffer = bytearray()
            async for chunk in chunks:
                buffer.extend(chunk)
                if max_bytes is not None and len(buffer) > max_bytes:
                    raise UnsupportedURLError(
                        f"{url} exceeded the {human_size(max_bytes)} download limit"
                    )
        return bytes(buffer)

    async def size_of(
        self, url: str, *, headers: Optional[dict[str, str]] = None
    ) -> tuple[Optional[int], Optional[str]]:
        """``(size, mime_type)`` of any direct link, without downloading it."""
        return await self._http_for(url).probe_size(url, headers=headers)

    async def size_human_of(self, url: str, **kwargs: Any) -> Optional[str]:
        size, _ = await self.size_of(url, **kwargs)
        return human_size(size)

    # ------------------------------------------------------------- helpers
    def affordable(self, metadata: PostMetadata, max_bytes: int) -> tuple[bool, Optional[int]]:
        """``(allowed, size)`` - the premium gate for a Telegram bot."""
        size = metadata.size_bytes
        if size is None:
            return True, None
        return size <= max_bytes, size

    def _http_for(self, url: str) -> HttpClient:
        """The best :class:`HttpClient` for a direct link.

        Platform clients that own a core HTTP layer (YouTube, Twitter) are
        reused so their throttling/caching rules apply. Reddit keeps its own
        legacy client, so anything it claims - and every url no platform
        claims - falls back to the shared core client. That keeps
        :meth:`fetch`/:meth:`size_of` usable for *any* direct static file,
        which is the Telegram bot's fast path for images.
        """
        platform = self.platform_of(url)
        if platform is not Platform.UNKNOWN:
            try:
                driver = self.driver_for(platform)
            except KeyError:
                driver = None
            candidate = getattr(driver, "http", None) if driver is not None else None
            if isinstance(candidate, HttpClient):
                return candidate
        return self.http

    def summary(self) -> str:
        """Which SDKs are enabled and ready."""
        return "Downloader(platforms=" + ", ".join(str(p) for p in self._enabled) + ")"


# ------------------------------------------------------------------ helpers
_DOWNLOAD_KEYSET = (
    "quality",
    "target",
    "dest",
    "pattern",
    "progress",
    "only",
    "include_audio",
    "max_size_bytes",
    "temp_dir",
)


def _driver_kwargs(platform: Platform, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Trim kwargs to what the platform driver understands."""
    if platform is not Platform.REDDIT:
        return kwargs
    allowed = set(_DOWNLOAD_KEYSET)
    allowed.update({"overwrite", "album_dir", "mux", "include_sizes"})
    return {key: value for key, value in kwargs.items() if key in allowed}


def _as_platform(value: "Platform | str") -> Platform:
    if isinstance(value, Platform):
        return value
    try:
        return Platform(str(value).strip().lower())
    except ValueError as exc:
        raise UnsupportedURLError(f"unknown platform {value!r}") from exc


async def get_metadata(url: str, **kwargs: Any) -> PostMetadata:
    """One-shot metadata fetch through a throwaway :class:`Downloader`."""
    async with Downloader() as downloader:
        return await downloader.get_metadata(url, **kwargs)


async def download(url: str, **kwargs: Any) -> DownloadResult:
    """One-shot download through a throwaway :class:`Downloader`."""
    async with Downloader() as downloader:
        return await downloader.download(url, **kwargs)


async def save(url: str, dest: Any, **kwargs: Any) -> list[Any]:
    """One-shot download+save through a throwaway :class:`Downloader`."""
    async with Downloader() as downloader:
        return await downloader.save(url, dest, **kwargs)