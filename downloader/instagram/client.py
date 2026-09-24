"""The Instagram client: metadata, quality selection, downloading and saving.

Instagram is the one platform here whose SDK is *synchronous* - ``instaloader``
uses ``requests`` underneath. Every call therefore runs on a worker thread
(``asyncio.to_thread``) behind a lock plus a minimum interval, so a burst of
links cannot trip Instagram's rate limiter. Once metadata is resolved the rest
of the pipeline is the usual one: CDN urls, the shared download engine, ffmpeg
muxing and ``save``.

Access is the other difference. Anonymous requests are allowed only for a short
while: the first few succeed, then Instagram answers HTTP 401 with *"Please wait
a few minutes before you try again"* (profiles fail immediately). Configure
``session_file`` for anything beyond occasional use.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Callable, Optional, TypeVar

from ..core.base import BaseClient
from ..core.enums import Platform
from ..core.exceptions import (
    DownloaderError,
    MetadataError,
    UnsupportedURLError,
)
from ..core.models import DownloadResult, MediaFormat, PostMetadata
from ..core.progress import ProgressPhase
from .config import InstagramConfig
from .exceptions import (
    InstagramNotFoundError,
    InstaloaderMissingError,
    LoginRequiredError,
    NoMediaFoundError,
    PrivateProfileError,
    ProfileNotFoundError,
    RateLimitedError,
    StoryUnavailableError,
)
from .models import node_to_item, post_to_metadata
from .urls import InstagramRef, is_instagram_url, parse_url

T = TypeVar("T")

_RATE_LIMIT_MARKERS = (
    "please wait a few minutes",
    "wait a few minutes",
    "too many requests",
    "rate limit",
    "try again later",
)


def instaloader_module() -> Any:
    """Import ``instaloader`` lazily (it is an optional dependency)."""
    try:
        import instaloader  # type: ignore
    except ImportError as exc:  # pragma: no cover - declared in requirements
        raise InstaloaderMissingError(
            "instaloader is required for Instagram support: pip install instaloader"
        ) from exc
    return instaloader


def translate_instagram_error(exc: BaseException) -> BaseException:
    """Map an ``instaloader`` failure onto this SDK's exception hierarchy."""
    if isinstance(exc, DownloaderError):
        return exc
    try:
        errors = instaloader_module().exceptions
    except DownloaderError:  # pragma: no cover - defensive
        return MetadataError(str(exc))

    message = " ".join(str(exc).split()) or type(exc).__name__
    lowered = message.lower()

    if isinstance(exc, errors.QueryReturnedNotFoundException):
        return InstagramNotFoundError(f"Instagram post not found: {message}")
    if isinstance(exc, errors.ProfileNotExistsException):
        return ProfileNotFoundError(f"Instagram profile not found: {message}")
    if isinstance(exc, errors.PrivateProfileNotFollowedException):
        return PrivateProfileError(
            f"{message} - private accounts need a logged in session (session_file)"
        )
    if isinstance(exc, errors.LoginRequiredException):
        return LoginRequiredError(
            f"{message} - set InstagramConfig(session_file=...) to log in"
        )
    if isinstance(
        exc,
        (
            errors.TooManyRequestsException,
            errors.QueryReturnedForbiddenException,
            errors.QueryReturnedBadRequestException,
        ),
    ) or any(marker in lowered for marker in _RATE_LIMIT_MARKERS):
        return RateLimitedError(
            "Instagram is rate limiting this client "
            f"({message}). Configure session_file and/or lower the Instagram "
            "queue rate (fetch_rate_per_second)."
        )
    if isinstance(exc, errors.ConnectionException):
        return MetadataError(f"Instagram request failed: {message}")
    if isinstance(exc, errors.LoginException):
        return LoginRequiredError(f"Instagram login failed: {message}")
    return MetadataError(f"{type(exc).__name__}: {message}")


class InstagramClient(BaseClient):
    """Async Instagram SDK for posts, reels, carousels and (logged in) stories.

    Example
    -------
    >>> async with InstagramClient(InstagramConfig(session_file="ig.session")) as ig:
    ...     meta = await ig.get_metadata("https://www.instagram.com/reel/DdhvW0GslGe/")
    ...     print(meta.media_type, meta.media_group_type, meta.author)
    ...     print(meta.links())          # {'video': ['https://instagram.ftxl1-1.fna.fbcdn.net/...']}
    ...     print(meta.size_human)       # known before downloading
    ...     result = await ig.download(meta, quality="best")
    ...     await result.save("downloads")
    """

    platform = Platform.INSTAGRAM
    config_class = InstagramConfig

    def __init__(
        self,
        config: Optional[InstagramConfig] = None,
        *,
        progress_callback: Optional[Any] = None,
        **overrides: Any,
    ) -> None:
        super().__init__(config, progress_callback=progress_callback, **overrides)
        self._loader: Any = None
        self._loader_lock = asyncio.Lock()
        self._request_lock = asyncio.Lock()
        self._last_request = 0.0

    @property
    def config(self) -> InstagramConfig:  # type: ignore[override]
        return self._config  # type: ignore[attr-defined]

    @config.setter
    def config(self, value: InstagramConfig) -> None:
        self._config = value

    # ------------------------------------------------------------- interface
    @classmethod
    def supports(cls, url: str) -> bool:
        """``True`` when ``url`` is an Instagram url."""
        return is_instagram_url(url)

    async def get_metadata(
        self,
        url: str,
        *,
        probe_sizes: Optional[bool] = None,
        **kwargs: Any,
    ) -> PostMetadata:
        """Resolve everything known about an Instagram post without downloading."""
        ref = parse_url(url)
        if ref.is_profile:
            raise UnsupportedURLError(
                "Instagram profiles/timelines are not exposed: send a post, reel or "
                "story link instead"
            )
        await self._emit(ProgressPhase.RESOLVING, f"resolving Instagram {ref.kind}")

        if ref.is_story:
            metadata = await self._story_metadata(ref, url)
        else:
            post = await self._post_for(ref)
            metadata = post_to_metadata(
                post,
                config=self.config,
                requested_url=url,
                canonical_url=ref.post_url,
            )

        if not metadata.items:
            raise NoMediaFoundError("this post carries no downloadable media")

        if probe_sizes is None:
            probe_sizes = self.config.probe_sizes
        if probe_sizes:
            await self._fill_sizes(metadata)
        return metadata

    async def download_by_url(self, url: str, **kwargs: Any) -> DownloadResult:
        """Convenience: resolve ``url`` and download it in one call."""
        metadata = await self.get_metadata(url)
        return await self.download(metadata, **kwargs)

    # ----------------------------------------------------------------- loader
    async def loader(self) -> Any:
        """The shared, lazily built ``instaloader.Instaloader`` instance."""
        async with self._loader_lock:
            if self._loader is None:
                self._loader = await asyncio.to_thread(self._build_loader)
            return self._loader

    def _build_loader(self) -> Any:
        instaloader = instaloader_module()
        config = self.config
        options: dict[str, Any] = {
            "quiet": True,
            "sleep": False,
            "max_connection_attempts": max(1, int(config.retries)),
            "request_timeout": float(config.timeout),
        }
        if config.custom_user_agent:
            options["user_agent"] = config.custom_user_agent
        loader = instaloader.Instaloader(**options)

        session = Path(config.session_file) if config.session_file else None
        if session is not None and session.exists():
            username = config.username or self._session_username(session)
            if not username:
                raise LoginRequiredError(
                    f"{session} does not record a username; set InstagramConfig(username=...)"
                )
            loader.load_session_from_file(username, str(session))
        elif config.username and config.password:
            loader.login(config.username, config.password)
            if session is not None:
                try:
                    loader.save_session_to_file(str(session))
                except Exception:  # noqa: BLE001 - caching the session is best effort
                    pass
        return loader

    @staticmethod
    def _session_username(session: Path) -> Optional[str]:
        """The first line of an instaloader session file is the username."""
        try:
            with session.open("r", encoding="utf-8") as handle:
                return handle.readline().strip() or None
        except OSError:
            return None

    # ------------------------------------------------------------------- call
    async def _call(self, function: Callable[[], T]) -> T:
        """Run one blocking instaloader call under the request lock."""
        async with self._request_lock:
            interval = max(0.0, float(self.config.request_interval))
            if interval:
                wait = self._last_request + interval - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
            try:
                return await asyncio.to_thread(function)
            except Exception as exc:  # noqa: BLE001 - translated for the caller
                raise translate_instagram_error(exc) from exc
            finally:
                self._last_request = time.monotonic()

    async def _post_for(self, ref: InstagramRef) -> Any:
        if not ref.shortcode:
            raise UnsupportedURLError(f"could not read a shortcode from {ref.requested!r}")
        instaloader = instaloader_module()
        loader = await self.loader()
        shortcode = ref.shortcode
        return await self._call(
            lambda: instaloader.Post.from_shortcode(loader.context, shortcode)
        )

    # ---------------------------------------------------------------- stories
    async def _story_metadata(self, ref: InstagramRef, url: str) -> PostMetadata:
        if not self.config.include_stories:
            raise StoryUnavailableError("stories are disabled (include_stories=False)")
        if not (self.config.session_file or (self.config.username and self.config.password)):
            raise StoryUnavailableError(
                "Instagram stories need a logged in session; set InstagramConfig("
                "session_file=...). They also expire 24 hours after posting."
            )
        if not ref.story_media_id or not ref.story_media_id.isdigit():
            raise StoryUnavailableError(f"unsupported story url: {url!r}")

        instaloader = instaloader_module()
        loader = await self.loader()
        media_id = int(ref.story_media_id)
        story = await self._call(
            lambda: instaloader.StoryItem.from_mediaid(loader.context, media_id)
        )
        metadata = self._story_to_metadata(story, url=url)
        if not metadata.items:
            raise StoryUnavailableError("the story carries no downloadable media")
        return metadata

    def _story_to_metadata(self, story: Any, *, url: str) -> PostMetadata:
        """Wrap a ``StoryItem`` in the canonical shape."""
        node: dict[str, Any] = {"pk": getattr(story, "mediaid", None)}
        width = getattr(story, "dimensions", None)
        item_node: dict[str, Any] = {"pk": node["pk"], "media_type": 1}
        if getattr(story, "is_video", False):
            item_node["media_type"] = 2
            item_node["video_versions"] = [
                {"url": story.video_url, "width": None, "height": None}
            ]
            item_node["video_url"] = story.video_url
            item_node["video_duration"] = getattr(story, "video_duration", None)
        if getattr(story, "url", None):
            item_node["image_versions2"] = {"candidates": [{"url": story.url}]}
            if isinstance(width, tuple) and len(width) == 2:
                item_node["image_versions2"]["candidates"][0]["width"] = width[0]
                item_node["image_versions2"]["candidates"][0]["height"] = width[1]
        node["carousel_media"] = [item_node]
        node["caption"] = getattr(story, "caption", None)
        node["owner"] = {
            "username": getattr(story, "owner_username", None),
            "id": getattr(story, "owner_id", None),
        }
        date_utc = getattr(story, "date_utc", None)
        if date_utc is not None:
            try:
                node["taken_at_timestamp"] = int(date_utc.timestamp())
            except (AttributeError, ValueError, OSError):  # pragma: no cover - defensive
                pass

        class _StoryPost:
            """Minimal stand-in so the shared mapper can be reused."""

            typename = "GraphSidecar" if False else "GraphImage"
            shortcode = str(node.get("pk") or "")

            def __init__(self, payload: dict[str, Any], url: str) -> None:
                self._node = payload
                self._url = url

        story_post = _StoryPost(node, url)
        # a story is a single medium, so map it directly rather than as a sidecar
        item = node_to_item(
            item_node,
            0,
            config=self.config,
            post_url=url,
            shortcode=story_post.shortcode,
            carousel=False,
        )
        metadata = post_to_metadata(
            story_post,
            config=self.config,
            requested_url=url,
            canonical_url=url,
        )
        if item is not None and not metadata.items:
            metadata.items = [item]
        metadata.media_group_type = metadata.media_group_type
        metadata.providers = ["instaloader:story"]
        return metadata

    # ------------------------------------------------------------------ sizes
    async def _fill_sizes(self, metadata: PostMetadata) -> None:
        """Probe the CDN for the sizes instagram does not report (images)."""
        pending: list[MediaFormat] = [
            fmt
            for item in metadata.items
            for fmt in item.formats
            if fmt.size_bytes is None and not fmt.is_manifest and fmt.url
        ]
        if not pending:
            return
        await self.probe_format_sizes(pending)
        for item in metadata.items:
            known = [fmt.size_bytes for fmt in item.formats if fmt.size_bytes]
            if known:
                item.size_bytes = max(known)
                item.size_is_approx = False

    # ------------------------------------------------------------- lifecycle
    async def close(self) -> None:
        self._loader = None
        await super().close()


__all__ = ["InstagramClient", "instaloader_module", "translate_instagram_error"]
