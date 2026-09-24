"""Post level models: metadata, video info and download results."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from .enums import MediaGroupType, MediaKind
from .media import DownloadedFile, MediaFormat, MediaGroup, MediaItem, human_size, pick_format, group_items


class VideoInfo(BaseModel):
    """Reddit hosted video details (``secure_media.reddit_video``)."""

    model_config = ConfigDict(extra="ignore")

    base_url: Optional[str] = None
    fallback_url: Optional[str] = None
    dash_url: Optional[str] = None
    hls_url: Optional[str] = None
    duration: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None
    has_audio: Optional[bool] = None
    is_gif: Optional[bool] = None
    bitrate_kbps: Optional[int] = None


class CrosspostInfo(BaseModel):
    """Details about the post a crosspost points at."""

    model_config = ConfigDict(extra="ignore")

    id: Optional[str] = None
    fullname: Optional[str] = None
    subreddit: Optional[str] = None
    author: Optional[str] = None
    title: Optional[str] = None
    permalink: Optional[str] = None
    url: Optional[str] = None
    parent: Optional["PostMetadata"] = None


class PostMetadata(BaseModel):
    """Everything the SDK knows about a post before/without downloading it."""

    model_config = ConfigDict(extra="ignore")

    # identity
    id: str
    fullname: Optional[str] = None
    url: Optional[str] = None
    permalink: Optional[str] = None
    short_permalink: Optional[str] = None
    requested_url: Optional[str] = None

    # descriptive
    title: Optional[str] = None
    author: Optional[str] = None
    subreddit: Optional[str] = None
    domain: Optional[str] = None
    created_utc: Optional[float] = None
    score: Optional[int] = None
    upvote_ratio: Optional[float] = None
    num_comments: Optional[int] = None
    flair: Optional[str] = None
    selftext: Optional[str] = None
    thumbnail: Optional[str] = None

    # flags
    is_self: bool = False
    is_video: bool = False
    is_gallery: bool = False
    is_nsfw: bool = False
    is_spoiler: bool = False
    is_crosspost: bool = False
    is_original_content: bool = False

    # media
    media_type: MediaKind = MediaKind.UNKNOWN
    media_group_type: MediaGroupType = MediaGroupType.NONE
    items: list[MediaItem] = Field(default_factory=list)
    groups: dict[str, MediaGroup] = Field(default_factory=dict)
    video: Optional[VideoInfo] = None
    external_url: Optional[str] = None
    crosspost: Optional[CrosspostInfo] = None

    # bookkeeping
    providers: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)
    meta: dict[str, Any] = Field(default_factory=dict)

    # ------------------------------------------------------------------ sizes
    @property
    def size_bytes(self) -> Optional[int]:
        """Bytes a default download would transfer.

        This is the sum of every format in the default download plan, so a
        video with a separate audio track reports video **plus** audio.
        Returns ``None`` until at least one size is known.
        """
        plan = self.select_all("best")
        if not plan:
            return None
        total = 0
        known = False
        for _, primary, audio in plan:
            for fmt in (primary, audio):
                if fmt is not None and fmt.size_bytes is not None:
                    total += fmt.size_bytes
                    known = True
        if known:
            return total
        fallback = [i.size_bytes for i in self.items if i.size_bytes is not None]
        return sum(fallback) if fallback else None

    @property
    def known_size_bytes(self) -> int:
        """Sum of every size we managed to resolve (0 when unknown)."""
        return sum(
            f.size_bytes or 0 for item in self.items for f in item.formats
        )

    @property
    def size_human(self) -> Optional[str]:
        return human_size(self.size_bytes)

    @property
    def sizes_known(self) -> bool:
        return all(item.size_bytes is not None for item in self.items) and bool(self.items)

    # ------------------------------------------------------------- accessors
    @property
    def created_at(self) -> Optional[datetime]:
        if self.created_utc is None:
            return None
        return datetime.fromtimestamp(self.created_utc, tz=timezone.utc)

    @property
    def count(self) -> int:
        return len(self.items)

    @property
    def is_album(self) -> bool:
        return self.media_group_type == MediaGroupType.GALLERY or self.count > 1

    @property
    def has_media(self) -> bool:
        return bool(self.items)

    @property
    def downloadable_items(self) -> list[MediaItem]:
        return [i for i in self.items if i.is_downloadable and i.formats]

    @property
    def group_list(self) -> list[MediaGroup]:
        """Groups ordered by item count (largest first)."""
        return sorted(self.groups.values(), key=lambda g: g.count, reverse=True)

    def group(self, kind: MediaKind | str) -> Optional[MediaGroup]:
        return self.groups.get(str(kind))

    def items_of(self, kind: MediaKind | str) -> list[MediaItem]:
        group = self.group(kind)
        return list(group.items) if group else []

    def best(self, kind: Optional[MediaKind | str] = None, quality: str | int = "best") -> MediaItem:
        """Return the "main" media item (optionally of a given kind)."""
        pool = self.items_of(kind) if kind is not None else list(self.items)
        pool = [i for i in pool if i.selectable] or pool
        if not pool:
            raise ValueError("post has no media items")
        def rank(item):
            fmt = item.select(quality)
            return (fmt.quality_height or fmt.height or fmt.width or 0, item.size_bytes or 0)

        return max(pool, key=rank)

    def links(
        self,
        *,
        grouped: bool = True,
        all_formats: bool = False,
        quality: str | int = "best",
    ) -> dict[str, list[str]] | list[str]:
        """CDN urls, grouped by media kind.

        Parameters
        ----------
        grouped:
            ``True`` returns ``{"image": [...], "video": [...]}``, ``False``
            returns a flat list (still de-duplicated, order preserved).
        all_formats:
            Include every known representation instead of the best one per
            item. Useful for building a multi-quality download menu.
        quality:
            Quality policy used to pick the "best one" per item.
        """
        out: dict[str, list[str]] = {}
        for item in self.items:
            bucket = out.setdefault(str(item.kind), [])
            if all_formats:
                bucket.extend(f.url for f in item.formats)
            elif item.formats:
                bucket.append(item.select(quality).url)
            elif item.source_url:
                bucket.append(item.source_url)
        for key, urls in out.items():
            out[key] = list(dict.fromkeys(urls))
        if grouped:
            return out
        flat: list[str] = []
        for urls in out.values():
            flat.extend(urls)
        return list(dict.fromkeys(flat))

    def select_all(
        self,
        quality: str | int = "best",
        *,
        prefer_muxed: bool = True,
        include_audio: bool = True,
    ) -> list[tuple[MediaItem, MediaFormat, Optional[MediaFormat]]]:
        """Resolve the ``(item, primary_format, audio_format)`` plan to fetch.

        ``audio_format`` is only returned when the primary format is a
        video-only stream and a separate audio track must be muxed in.
        """
        plan: list[tuple[MediaItem, MediaFormat, Optional[MediaFormat]]] = []
        for item in self.items:
            if not item.formats:
                continue
            primary = pick_format(item.selectable or item.formats, quality, prefer_muxed=prefer_muxed)
            audio: Optional[MediaFormat] = None
            if (
                include_audio
                and primary.kind.value == "video"
                and item.has_audio is not False
            ):
                audio = item.best_audio()
            plan.append((item, primary, audio))
        return plan

    # ------------------------------------------------------------ serialising
    def to_dict(self, *, include_raw: bool = False, include_formats: bool = True) -> dict[str, Any]:
        data = self.model_dump(mode="json", exclude={"raw"})
        if not include_formats:
            for item in data.get("items", []):
                item.pop("formats", None)
        if include_raw:
            data["raw"] = self.raw
        return data

    def summary(self) -> str:
        """One line human readable overview (handy for logs)."""
        parts = [
            f"{self.media_type.value}",
            f"group={self.media_group_type.value}",
            f"{self.count} item(s)",
        ]
        if self.size_human:
            parts.append(self.size_human)
        if self.author:
            parts.append(f"u/{self.author}")
        if self.subreddit:
            parts.append(f"r/{self.subreddit}")
        head = " | ".join(parts)
        return f"{head}\n  {(self.title or '')[:120]}"

    def describe(self) -> str:
        """Multi-line dump of every item and format (debug helper)."""
        lines = [self.summary(), f"  providers: {', '.join(self.providers) or '-'}"]
        for item in self.items:
            lines.append(f"  [{item.index}] {item.kind.value} {item.resolution or ''} {item.size_human or ''}")
            for fmt in item.formats:
                lines.append(f"       - {fmt.display()}")
        for warning in self.warnings:
            lines.append(f"  ! {warning}")
        return "\n".join(lines)

    def rebuild_groups(self) -> "PostMetadata":
        self.groups = group_items(self.items)
        return self


class DownloadResult(BaseModel):
    """Outcome of :meth:`RedditClient.download`."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="ignore")

    metadata: PostMetadata
    files: list[DownloadedFile] = Field(default_factory=list)
    quality: str | int = "best"
    elapsed: float = 0.0
    errors: dict[str, str] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.files) and not self.errors

    @property
    def total_bytes(self) -> int:
        return sum(f.size for f in self.files)

    @property
    def total_human(self) -> str:
        return human_size(self.total_bytes) or "0 B"

    @property
    def saved_paths(self) -> list[Path]:
        return [f.path for f in self.files if f.path is not None]

    def files_of(self, kind: MediaKind | str) -> list[DownloadedFile]:
        return [f for f in self.files if str(f.kind) == str(kind)]

    def free(self) -> None:
        """Release in-memory buffers (and temp files) of every file."""
        for file in self.files:
            file.free()

    async def save(
        self,
        dest: str | Path,
        *,
        pattern: Optional[str] = None,
        overwrite: bool = False,
        album_dir: bool = True,
    ) -> list[Path]:
        """Write the downloaded files to ``dest``.

        See :func:`downloader.reddit.saver.save_result` for the pattern grammar.
        """
        from ..saver import save_result

        return await save_result(
            self,
            dest,
            pattern=pattern,
            overwrite=overwrite,
            album_dir=album_dir,
        )

    def summary(self) -> str:
        lines = [
            f"downloaded {len(self.files)} file(s) in {self.elapsed:.2f}s ({self.total_human})",
        ]
        for file in self.files:
            lines.append(f"  - {file.filename} ({file.size_human})")
        for key, message in self.errors.items():
            lines.append(f"  ! {key}: {message}")
        return "\n".join(lines)


CrosspostInfo.model_rebuild()