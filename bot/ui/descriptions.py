"""Per platform captions and progress text.

Captions are the "description" the bot attaches to a delivered album or file.
They follow the project's Telegram formatting rules and are kept under
Telegram's 1024 character caption limit.
"""

from __future__ import annotations

from typing import Optional, Sequence

from downloader.core.enums import MediaGroupType, MediaKind, Platform
from downloader.core.models import PostMetadata

from bot.utils.text import clip, escape, format_count, format_duration, format_size, format_when

from .style import (
    BAD,
    IMPORTANT,
    OK,
    PENDING,
    ROW,
    header,
    row,
    rule,
    section,
    small_caps,
)

CAPTION_LIMIT = 1024
_PLATFORM_TITLES = {"youtube": "YouTube", "twitter": "Twitter", "reddit": "Reddit"}
_KIND_TITLES = {
    MediaKind.IMAGE: "Photo",
    MediaKind.GIF: "GIF",
    MediaKind.VIDEO: "Video",
    MediaKind.AUDIO: "Audio",
    MediaKind.GALLERY: "Album",
    MediaKind.EXTERNAL_IMAGE: "Photo",
    MediaKind.EXTERNAL_VIDEO: "Video",
}


def platform_title(platform: object) -> str:
    return _PLATFORM_TITLES.get(str(platform), str(platform).title() or "Media")


def kind_title(kind: object) -> str:
    try:
        return _KIND_TITLES[MediaKind(str(kind))]
    except (ValueError, KeyError):
        return str(kind).title()


class CaptionBuilder:
    """Compose a caption section by section, then squeeze it into the limit."""

    def __init__(self) -> None:
        self._blocks: list[str] = []

    def head(self, text: str) -> "CaptionBuilder":
        self._blocks.append(header(text))
        self._blocks.append(rule())
        return self

    def title(self, text: Optional[str], *, limit: int = 160) -> "CaptionBuilder":
        text = clip(text, limit)
        if text:
            self._blocks.append(f"{IMPORTANT} {escape(text)}")
        return self

    def para(self, text: Optional[str], *, limit: int = 300) -> "CaptionBuilder":
        text = clip(text, limit)
        if text:
            self._blocks.append(escape(text))
        return self

    def group(self, name: str, rows: Sequence[tuple[str, object]]) -> "CaptionBuilder":
        rows = [(key, value) for key, value in rows if value not in (None, "", "\u2014")]
        if not rows:
            return self
        lines = [section(name), ""]
        lines.extend(row(escape(key), escape(value)) for key, value in rows)
        self._blocks.append("\n".join(lines))
        return self

    def footer(self, text: Optional[str] = None) -> "CaptionBuilder":
        if text:
            self._blocks.append(f"{rule()}\n{KV_MARK} {escape(text)}")
        return self

    def build(self, *, limit: int = CAPTION_LIMIT) -> str:
        text = "\n\n".join(block for block in self._blocks if block)
        text = text.strip()
        if len(text) <= limit:
            return text
        return text[: limit - 1].rstrip() + "\u2026"


KV_MARK = "\u00b7"


def _quality_row(quality: Optional[str]) -> Optional[str]:
    return str(quality) if quality else None


def build_caption(
    meta: PostMetadata,
    *,
    quality: Optional[str] = None,
    size_bytes: Optional[int] = None,
    footer: Optional[str] = None,
    extra_rows: Sequence[tuple[str, object]] = (),
    note: Optional[str] = None,
) -> str:
    """The description sent with a post, tailored per platform."""
    platform = meta.platform
    items = len(meta.items) or 1
    group = meta.media_group_type
    headline = kind_title(meta.media_type)
    if group in (MediaGroupType.ALBUM, MediaGroupType.GALLERY) or items > 1:
        headline = f"Album {KV_MARK} {items} items"

    builder = CaptionBuilder().head(f"{platform_title(platform)} {KV_MARK} {headline}")
    builder.title(meta.title or meta.description)
    if meta.title and meta.description:
        builder.para(meta.description, limit=180)

    size = size_bytes if size_bytes is not None else meta.size_bytes
    details: list[tuple[str, object]] = []
    if str(platform) == "youtube":
        details += [
            ("Channel", meta.channel or meta.author),
            ("Duration", format_duration(meta.duration)),
            ("Quality", _quality_row(quality)),
        ]
    elif str(platform) == "twitter":
        details += [
            ("Author", meta.author),
            ("Duration", format_duration(meta.duration) if meta.duration else None),
            ("Quality", _quality_row(quality)),
        ]
    elif str(platform) == "reddit":
        details += [
            ("Subreddit", meta.channel or meta.extra.get("subreddit")),
            ("Author", f"u/{meta.author}" if meta.author else None),
            ("Duration", format_duration(meta.duration) if meta.duration else None),
            ("Quality", _quality_row(quality)),
        ]
    else:
        details += [("Author", meta.author), ("Quality", _quality_row(quality))]
    if items > 1:
        details.append(("Items", items))
    details.append(("Size", format_size(size)))
    details.extend(extra_rows)
    builder.group("Details", details)

    stats: list[tuple[str, object]] = []
    if str(platform) == "youtube":
        stats += [
            ("Views", format_count(meta.view_count)),
            ("Likes", format_count(meta.like_count)),
            ("Uploaded", format_when(meta.timestamp or meta.created_utc)),
        ]
    elif str(platform) == "twitter":
        stats += [
            ("Likes", format_count(meta.like_count)),
            ("Reposts", format_count(meta.repost_count)),
            ("Replies", format_count(meta.comment_count)),
            ("Posted", format_when(meta.created_utc or meta.timestamp)),
        ]
    elif str(platform) == "reddit":
        stats += [
            ("Upvotes", format_count(meta.like_count)),
            ("Comments", format_count(meta.comment_count)),
            ("Posted", format_when(meta.created_utc)),
        ]
    if meta.is_short:
        stats.append(("Type", "Short"))
    if meta.is_live:
        stats.append(("Type", "Live"))
    builder.group("Stats", stats)

    if note:
        builder.para(note, limit=200)
    builder.footer(footer)
    return builder.build()


_STATUS_ICONS = {
    "queued": PENDING,
    "fetching": PENDING,
    "processing": PENDING,
    "sending": PENDING,
    "done": OK,
    "error": BAD,
    "rejected": BAD,
}

_STATUS_LABELS = {
    "queued": "queued",
    "fetching": "fetching metadata",
    "processing": "processing media",
    "sending": "uploading to Telegram",
    "done": "done",
    "error": "failed",
    "rejected": "rejected",
}


def build_status(
    platform: object,
    stage: str,
    *,
    detail: Optional[str] = None,
    queue_depth: Optional[int] = None,
) -> str:
    """The small message the bot edits while a request is in flight."""
    icon = _STATUS_ICONS.get(stage, PENDING)
    label = _STATUS_LABELS.get(stage, stage)
    line = f"{icon} {small_caps(platform_title(platform))} {KV_MARK} {label}"
    if queue_depth:
        line += f" {KV_MARK} queue {queue_depth}"
    if detail:
        line += f"\n{ROW} {escape(clip(detail, 160))}"
    return line


__all__ = [
    "CAPTION_LIMIT",
    "CaptionBuilder",
    "build_caption",
    "build_status",
    "kind_title",
    "platform_title",
]
