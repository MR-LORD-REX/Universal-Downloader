"""The Twitter/X client: metadata, quality selection, downloading and saving."""

from __future__ import annotations

import time
from typing import Any, Optional, Sequence

from ..core.base import BaseClient
from ..core.enums import MediaGroupType, MediaKind, Platform
from ..core.exceptions import DownloaderError, MetadataError, UnsupportedURLError
from ..core.models import MediaFormat, MediaItem, PostMetadata, Thumbnail
from ..core.progress import ProgressPhase
from ..core.select import build_plan
from ..core.ytdlp import YtDlpExtractor
from .config import TwitterConfig
from .exceptions import (
    MediaAPIError,
    NoMediaFoundError,
    TweetNotFoundError,
    TweetUnavailableError,
    TwitterError,
)
from .fx import FxTwitterAPI, resolve_short_url
from .models import attach_video_formats, tweet_to_metadata, ytdlp_video_formats
from .urls import TwitterRef, is_twitter_url, parse_url


class TwitterClient(BaseClient):
    """Async Twitter/X SDK for videos, photos and multi-image tweets.

    Example
    -------
    >>> async with TwitterClient() as tw:
    ...     meta = await tw.get_metadata("https://x.com/GenshinUniverse/status/2102374550868521044")
    ...     print(meta.media_type, meta.media_group_type, meta.count)
    ...     print(meta.links())        # {'image': ['https://pbs.twimg.com/media/...?name=orig']}
    ...     result = await tw.download(meta)
    ...     await result.save("downloads")
    """

    platform = Platform.TWITTER
    config_class = TwitterConfig

    def __init__(
        self,
        config: Optional[TwitterConfig] = None,
        *,
        progress_callback: Optional[Any] = None,
        **overrides: Any,
    ) -> None:
        super().__init__(config, progress_callback=progress_callback, **overrides)
        self._fx = FxTwitterAPI(self._http, self.config)  # type: ignore[arg-type]
        self._extractor = YtDlpExtractor(self.config)

    @property
    def config(self) -> TwitterConfig:  # type: ignore[override]
        return self._config  # type: ignore[attr-defined]

    @config.setter
    def config(self, value: TwitterConfig) -> None:
        self._config = value

    # ------------------------------------------------------------- interface
    @classmethod
    def supports(cls, url: str) -> bool:
        """``True`` when ``url`` is a Twitter/X url this client can handle."""
        return is_twitter_url(url)

    async def get_metadata(
        self,
        url: str,
        *,
        include_video_formats: Optional[bool] = None,
        probe_sizes: Optional[bool] = None,
        photo_index: Optional[int] = None,
    ) -> PostMetadata:
        """Resolve everything known about a tweet without downloading media."""
        ref = await self._reference(url)
        await self._emit(ProgressPhase.RESOLVING, f"resolving tweet {ref.tweet_id}")
        metadata = await self._metadata_for(ref, url)
        await self._add_video_ladder(metadata, ref, include_video_formats)
        if photo_index is not None:
            metadata = _only_photo(metadata, photo_index)
        if probe_sizes is None:
            probe_sizes = self.config.probe_sizes
        if probe_sizes:
            await self._fill_sizes(metadata)
        return metadata

    # -------------------------------------------------------------- plumbing
    async def _reference(self, url: str) -> TwitterRef:
        ref = parse_url(url)
        if ref.kind == "short":
            if not self.config.resolve_tco_links:
                raise UnsupportedURLError(
                    "t.co links need resolve_tco_links=True to be followed"
                )
            resolved = await resolve_short_url(self._http, ref.canonical)
            if not resolved:
                raise UnsupportedURLError(f"could not resolve the t.co link {url!r}")
            ref = parse_url(resolved)
        if ref.kind != "status":
            raise UnsupportedURLError(
                "only tweet urls are supported (profiles/timelines are not exposed by "
                "any key-less API)"
            )
        return ref

    async def _metadata_for(self, ref: TwitterRef, url: str) -> PostMetadata:
        errors: list[str] = []
        if self._fx.enabled:
            try:
                tweet = await self._fx.tweet_for(ref)
            except (TweetNotFoundError, TweetUnavailableError):
                raise
            except TwitterError as exc:
                errors.append(str(exc))
                tweet = None
            if tweet is not None:
                metadata = tweet_to_metadata(
                    tweet, self.config, ref=ref, requested_url=url
                )
                if not metadata.items:
                    raise NoMediaFoundError("the tweet carries no photos or videos")
                return metadata
        if not self.config.resolve_video_formats:
            raise MediaAPIError(
                "no metadata source available: "
                + ("; ".join(errors) or "the media api is disabled")
            )
        return await self._video_only_metadata(ref, url, errors)

    async def _video_only_metadata(
        self, ref: TwitterRef, url: str, errors: Sequence[str]
    ) -> PostMetadata:
        """Fallback path: build metadata straight from yt-dlp."""
        try:
            info = await self._extractor.extract(ref.status_url)
        except MetadataError as exc:
            raise MediaAPIError(
                "no metadata source available: "
                + ("; ".join(list(errors) + [str(exc)]) or "unknown error")
            ) from exc
        formats = ytdlp_video_formats(info, self.config)
        item = _item_from_info(info, formats)
        if item is None:
            raise NoMediaFoundError("the tweet carries no downloadable media")
        thumbnails = [
            Thumbnail(url=str(info.get("thumbnail"))) if info.get("thumbnail") else None
        ]
        metadata = PostMetadata(
            platform=Platform.TWITTER,
            id=str(info.get("id") or ref.tweet_id),
            url=info.get("webpage_url") or ref.status_url,
            requested_url=url,
            permalink=info.get("webpage_url"),
            title=info.get("title"),
            description=info.get("description"),
            author=info.get("uploader") or info.get("channel"),
            author_id=info.get("uploader_id") or info.get("channel_id"),
            created_utc=float(info["timestamp"]) if info.get("timestamp") else None,
            timestamp=info.get("timestamp"),
            duration=info.get("duration"),
            thumbnail=info.get("thumbnail"),
            thumbnails=[t for t in thumbnails if t],
            media_type=MediaKind.VIDEO,
            media_group_type=MediaGroupType.SINGLE,
            items=[item],
            quality_hints={
                "container": self.config.prefer_container,
                "codec": self.config.prefer_video_codec,
                "audio_codec": self.config.prefer_audio_codec,
                "prefer_muxed": True,
            },
            providers=["ytdlp"],
            warnings=list(errors),
            extra={"media_api": "off", "extracted_at": time.time()},
            raw={k: v for k, v in info.items() if isinstance(v, (str, int, float, bool))},
        )
        return metadata.rebuild_groups()

    async def _add_video_ladder(
        self,
        metadata: PostMetadata,
        ref: TwitterRef,
        include_video_formats: Optional[bool],
    ) -> None:
        wanted = (
            self.config.resolve_video_formats
            if include_video_formats is None
            else include_video_formats
        )
        if not wanted:
            return
        has_video = any(item.kind in (MediaKind.VIDEO, MediaKind.GIF) for item in metadata.items)
        if not has_video:
            return
        try:
            info = await self._extractor.extract(ref.status_url)
        except MetadataError as exc:
            metadata.warnings.append(f"yt-dlp video ladder unavailable: {exc}")
            return
        ladder = ytdlp_video_formats(info, self.config)
        if not attach_video_formats(metadata, ladder):
            metadata.warnings.append("yt-dlp returned no video renditions")
            return
        metadata.extra["video_info"] = {
            "extractor": info.get("extractor"),
            "duration": info.get("duration"),
            "fps": info.get("fps"),
            "uploader": info.get("uploader"),
            "webpage_url": info.get("webpage_url"),
        }
        for item in metadata.items:
            if item.duration is None and info.get("duration"):
                item.duration = float(info["duration"])
            if item.width is None and info.get("width"):
                item.width = info.get("width")
            if item.height is None and info.get("height"):
                item.height = info.get("height")

    async def _fill_sizes(self, metadata: PostMetadata) -> None:
        formats = [
            fmt
            for item in metadata.items
            for fmt in item.formats
            if fmt.size_bytes is None and not fmt.is_manifest and fmt.url
        ]
        if formats:
            await self.probe_format_sizes(formats)
        for item in metadata.items:
            if item.formats:
                item.size_bytes = item.size_for("best", **metadata.spec_kwargs())
                if item.size_bytes is None:
                    item.size_bytes = item.total_size_bytes
        if metadata.thumbnail and metadata.thumbnails:
            size, _ = await self.http.probe_size(metadata.thumbnail)
            if size is not None:
                for thumb in metadata.thumbnails:
                    if thumb.url == metadata.thumbnail:
                        thumb.size_bytes = size
                        break

    # ------------------------------------------------------------ extras
    async def download_by_url(self, url: str, **kwargs: Any) -> Any:
        """Convenience: :meth:`get_metadata` followed by :meth:`download`."""
        metadata = await self.get_metadata(url)
        return await self.download(metadata, **kwargs)

    def preview_url(self, metadata: PostMetadata, quality: str = "best") -> Optional[str]:
        """A ready-to-upload preview link (photo original or video thumbnail)."""
        if metadata.items:
            try:
                return metadata.items[0].select(quality).url
            except DownloaderError:
                pass
        return metadata.thumbnail

    def plan_summary(self, metadata: PostMetadata, quality: Any = None) -> list[str]:
        """Human readable ``(item, format, size)`` list for one quality."""
        spec = self.quality_spec(quality)
        lines: list[str] = []
        for entry in build_plan(metadata.items, spec):
            audio = entry.audio.format_id if entry.audio else "-"
            size = entry.total_size
            lines.append(
                f"item[{entry.item.index}] {entry.primary.format_id} + {audio}"
                f" | {entry.primary.extension} | {size} bytes | mux={entry.needs_mux}"
            )
        return lines


def _item_from_info(info: dict[str, Any], formats: Sequence[MediaFormat]) -> Optional[MediaItem]:
    if not formats:
        return None
    best = next((f for f in formats if f.has_video), formats[0])
    return MediaItem(
        index=0,
        id=str(info.get("id") or ""),
        kind=MediaKind.VIDEO,
        caption=info.get("title"),
        source_url=info.get("webpage_url"),
        extension="mp4",
        mime_type="video/mp4",
        width=info.get("width") or best.width,
        height=info.get("height") or best.height,
        duration=info.get("duration"),
        has_audio=True,
        formats=list(formats),
        meta={"thumbnail": info.get("thumbnail")},
    )




def _only_photo(metadata: PostMetadata, photo_index: int) -> PostMetadata:
    """Restrict a gallery to the photo the url pointed at (1 based)."""
    imgs = [item for item in metadata.items if item.kind == MediaKind.IMAGE]
    if not imgs:
        return metadata
    target = imgs[0] if photo_index < 1 else imgs[min(photo_index, len(imgs)) - 1]
    metadata.items = [target]
    metadata.media_type = MediaKind.IMAGE
    metadata.media_group_type = MediaGroupType.SINGLE
    return metadata.rebuild_groups()
