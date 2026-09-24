"""Bridges that expose the Reddit SDK through the shared core models.

The Reddit package predates :mod:`downloader.core` and keeps its own copy of
the models, so the orchestrator adapts it rather than rewriting working code:
Reddit metadata in, canonical :class:`~downloader.core.models.PostMetadata`
out (and the reverse for download results). The native object is stashed on
``metadata.native`` so a download never has to re-fetch anything.
"""

from __future__ import annotations

from typing import Any, Optional

from .core.enums import (
    DownloadTarget,
    FormatKind,
    FormatOrigin,
    MediaGroupType,
    MediaKind,
    Platform,
)
from .core.exceptions import SizeLimitExceededError
from .core.models import (
    DownloadResult,
    DownloadedFile,
    MediaFormat,
    MediaItem,
    PostMetadata,
    Thumbnail,
)
from .core.select import parse_quality
from .core.urls import host_of

REDDIT_HOSTS = frozenset(
    {
        "reddit.com",
        "www.reddit.com",
        "old.reddit.com",
        "new.reddit.com",
        "m.reddit.com",
        "np.reddit.com",
        "redd.it",
        "www.redd.it",
        "i.redd.it",
        "v.redd.it",
        "preview.redd.it",
        "external-preview.redd.it",
        "packaged-media.redd.it",
        "redditmedia.com",
        "www.redditmedia.com",
        "redditstatic.com",
    }
)

REDDIT_PATTERN_KEYS = frozenset(
    {
        "id",
        "author",
        "subreddit",
        "title",
        "index",
        "count",
        "quality",
        "ext",
        "kind",
        "date",
        "media_id",
        "width",
        "height",
        "provider",
    }
)

REDDIT_QUALITY_HINTS = {
    "container": "mp4",
    "codec": "avc1",
    "audio_codec": "mp4a",
    "prefer_muxed": True,
}


def reddit_format_to_core(fmt: Any) -> MediaFormat:
    """Convert one ``downloader.reddit`` media format."""
    return MediaFormat(
        format_id=fmt.format_id,
        url=fmt.url,
        kind=FormatKind(str(fmt.kind)),
        origin=FormatOrigin(str(fmt.origin)),
        container=fmt.container,
        mime_type=fmt.mime_type,
        extension=fmt.extension,
        protocol=("m3u8" if fmt.origin is not None and str(fmt.origin) == "hls" else None),
        width=fmt.width,
        height=fmt.height,
        quality_height=fmt.quality_height or fmt.height,
        fps=fmt.fps,
        bitrate_kbps=fmt.bitrate_kbps,
        codecs=fmt.codecs,
        video_codec=fmt.codecs if str(fmt.kind) in ("video", "muxed") else None,
        audio_codec=fmt.codecs if str(fmt.kind) == "audio" else None,
        size_bytes=fmt.size_bytes,
        has_audio=fmt.has_audio,
        has_video=str(fmt.kind) in ("video", "muxed", "gif"),
        quality_label=fmt.quality_label or fmt.note,
        note=fmt.note,
    )


def reddit_item_to_core(item: Any) -> MediaItem:
    """Convert one ``downloader.reddit`` media item."""
    return MediaItem(
        index=item.index,
        id=item.id,
        kind=MediaKind(str(item.kind)),
        caption=item.caption,
        source_url=item.source_url,
        mime_type=item.mime_type,
        extension=item.extension,
        width=item.width,
        height=item.height,
        duration=item.duration,
        has_audio=item.has_audio,
        size_bytes=item.size_bytes,
        formats=[reddit_format_to_core(fmt) for fmt in item.formats],
        meta=dict(item.meta or {}),
    )


def reddit_to_core(metadata: Any) -> PostMetadata:
    """Convert a ``reddit.models.PostMetadata`` into the shared model."""
    items = [reddit_item_to_core(item) for item in metadata.items]
    duration = metadata.video.duration if metadata.video else None
    thumbnails: list[Thumbnail] = []
    if metadata.thumbnail:
        thumbnails.append(Thumbnail(url=metadata.thumbnail, note="reddit thumbnail"))
    core_meta = PostMetadata(
        platform=Platform.REDDIT,
        id=metadata.id,
        url=metadata.url,
        requested_url=metadata.requested_url,
        permalink=metadata.permalink,
        title=metadata.title,
        description=metadata.selftext,
        author=metadata.author,
        author_id=metadata.author,
        author_url=f"https://www.reddit.com/user/{metadata.author}" if metadata.author else None,
        channel=metadata.subreddit,
        created_utc=metadata.created_utc,
        duration=duration,
        like_count=metadata.score,
        comment_count=metadata.num_comments,
        thumbnail=metadata.thumbnail,
        thumbnails=thumbnails,
        media_type=MediaKind(str(metadata.media_type)),
        media_group_type=MediaGroupType(str(metadata.media_group_type)),
        items=items,
        external_url=metadata.external_url,
        is_nsfw=metadata.is_nsfw,
        is_spoiler=metadata.is_spoiler,
        providers=list(metadata.providers or []),
        warnings=list(metadata.warnings or []),
        extra={"subreddit": metadata.subreddit, "domain": metadata.domain, **(metadata.meta or {})},
        raw=metadata.raw,
        native=metadata,
        quality_hints=dict(REDDIT_QUALITY_HINTS),
    )
    for core_item, source in zip(core_meta.items, metadata.items):
        if core_item.size_bytes is None:
            core_item.size_bytes = core_item.size_for("best", **core_meta.spec_kwargs())
        if core_item.size_bytes is None:
            core_item.size_bytes = core_item.total_size_bytes
        if core_item.duration is None and source.duration:
            core_item.duration = source.duration
    return core_meta.rebuild_groups()


def core_format_from_reddit_download(fmt: Any) -> Optional[MediaFormat]:
    return reddit_format_to_core(fmt) if fmt is not None else None


def reddit_result_to_core(result: Any, metadata: PostMetadata) -> DownloadResult:
    """Convert a ``reddit.models.DownloadResult`` into the shared result."""
    files = [
        DownloadedFile(
            item_index=file.item_index,
            kind=MediaKind(str(file.kind)),
            filename=file.filename,
            mime_type=file.mime_type,
            format=core_format_from_reddit_download(file.format),
            path=file.path,
            data=file.data,
            downloaded=file.downloaded,
            elapsed=file.elapsed,
            muxed=file.muxed,
            note=file.note,
        )
        for file in result.files
    ]
    return DownloadResult(
        metadata=metadata,
        files=files,
        quality=result.quality,
        elapsed=result.elapsed,
        errors=dict(result.errors or {}),
        warnings=list(result.warnings or []),
    )


class RedditDriver:
    """Adapts :class:`~downloader.reddit.RedditClient` to the driver protocol."""

    platform = Platform.REDDIT

    def __init__(self, client: Any) -> None:
        self.client = client

    @staticmethod
    def supports(url: str) -> bool:
        """``True`` only for real Reddit hosts.

        ``reddit.urls.parse`` happily classifies *any* url as ``external``, so
        the host has to be checked first or the Reddit SDK would claim the
        whole internet (and every other platform's CDN links).
        """
        if host_of(url) not in REDDIT_HOSTS:
            return False
        from .reddit.urls import parse as parse_reddit_url

        try:
            parse_reddit_url(url)
        except Exception:
            return False
        return True

    async def get_metadata(self, url: str, **kwargs: Any) -> PostMetadata:
        native = await self.client.get_metadata(url, **kwargs)
        return reddit_to_core(native)

    async def download(
        self,
        metadata: PostMetadata,
        *,
        quality: Any = None,
        max_size_bytes: Optional[int] = None,
        container: Optional[str] = None,
        codec: Optional[str] = None,
        **kwargs: Any,
    ) -> DownloadResult:
        native = metadata.native if metadata.native is not None else metadata
        spec = parse_quality(quality if quality is not None else "best")
        if max_size_bytes is not None:
            size = metadata.size_for(spec.raw)
            if size is not None and size > max_size_bytes:
                error = SizeLimitExceededError(size, max_size_bytes)
                return DownloadResult(
                    metadata=metadata, quality=spec.label, errors={"item[0]": str(error)}
                )
        if "pattern" in kwargs:
            kwargs["pattern"] = reddit_pattern(kwargs["pattern"])
        result = await self.client.download(native, quality=quality or "best", **kwargs)
        return reddit_result_to_core(result, metadata)

    async def close(self) -> None:
        await self.client.close()


def reddit_pattern(pattern: str) -> str:
    """Translate the core filename grammar into the Reddit saver's grammar.

    The Reddit package predates :mod:`downloader.core`, so it does not know
    ``{platform}`` and would leave unknown placeholders verbatim in the
    filename. Known tokens are kept, ``{platform}`` is substituted and anything
    else is dropped.
    """
    if not pattern:
        return pattern
    import re

    out = pattern.replace("{platform}", "reddit")
    for token in re.findall(r"\{(\w+)\}", out):
        if token not in REDDIT_PATTERN_KEYS:
            out = out.replace("{" + token + "}", "")
    return out


def make_reddit_driver(**kwargs: Any) -> RedditDriver:
    """Build a driver around a fresh :class:`RedditClient`."""
    from .reddit import RedditClient

    progress = kwargs.pop("progress_callback", None)
    return RedditDriver(RedditClient(progress_callback=progress, **kwargs))