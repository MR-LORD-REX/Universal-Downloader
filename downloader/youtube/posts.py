"""Scraper for YouTube *community posts* (``youtube.com/post/<id>``).

yt-dlp cannot handle community posts - its ``youtube:tab`` extractor reads
``/post/<id>`` as a channel tab and fails. The post page, however, embeds its
full payload in ``ytInitialData``, which is stable, structured and needs no
authentication. The attachment is a ``backstage*Renderer`` that can be an
image, an image grid, a poll or a video, so the extractor walks the subtree
instead of hard coding one shape.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable, Optional
from urllib.parse import urlparse

from ..core.enums import FormatKind, FormatOrigin, MediaGroupType, MediaKind, Platform
from ..core.exceptions import MetadataError
from ..core.http import HttpClient
from ..core.models import MediaFormat, MediaItem, PostMetadata, Thumbnail
from .config import YouTubeConfig
from .exceptions import CommunityPostError
from .urls import VIDEO_ID_RE, YouTubeRef

_INITIAL_DATA_RE = re.compile(r"(?:var\s+ytInitialData\s*=|window\[\"ytInitialData\"\]\s*=)\s*(\{.+?\});", re.S)
_SIZE_SUFFIX_RE = re.compile(r"=s\d+(?:-[a-z0-9-]+)?$", re.I)
_MULTIPLIER_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([kmb])$", re.I)

_IMAGE_KEYS = ("image", "images", "thumbnails")
_VIDEO_ID_KEYS = ("videoId", "video_id", "clipId")


async def fetch_community_post(
    http: HttpClient,
    config: YouTubeConfig,
    ref: YouTubeRef,
) -> PostMetadata:
    """Fetch and parse a community post into :class:`PostMetadata`."""
    url = ref.canonical or f"https://www.youtube.com/post/{ref.post_id}"
    headers = {
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    response = await http.get(url, headers=headers, retries=max(1, config.retries))
    if not response.ok:
        raise CommunityPostError(f"HTTP {response.status} fetching {url}")
    data = extract_initial_data(response.text)
    renderer = find_backstage_post(data)
    if not renderer:
        raise CommunityPostError(f"no community post payload found in {url}")
    return parse_post_renderer(
        renderer, config, post_id=ref.post_id or "", requested_url=ref.requested or url
    )


def extract_initial_data(html: str) -> dict[str, Any]:
    """Pull ``ytInitialData`` out of a YouTube page."""
    match = _INITIAL_DATA_RE.search(html)
    if not match:
        raise CommunityPostError("ytInitialData not found in the page")
    try:
        return json.loads(match.group(1))
    except ValueError as exc:
        raise CommunityPostError("ytInitialData is not valid json") from exc


def find_backstage_post(data: Any) -> Optional[dict[str, Any]]:
    """Locate the ``backstagePostRenderer`` anywhere in a payload."""
    found = _find_first(data, "backstagePostRenderer")
    return found if isinstance(found, dict) else None


def parse_post_renderer(
    renderer: dict[str, Any],
    config: YouTubeConfig,
    *,
    post_id: str,
    requested_url: Optional[str] = None,
) -> PostMetadata:
    """Turn a ``backstagePostRenderer`` into :class:`PostMetadata`."""
    text = "".join(run.get("text", "") for run in (renderer.get("contentText") or {}).get("runs", []))
    author = _run_text(renderer.get("authorText"))
    published = _run_text(renderer.get("publishedTimeText"))
    likes = _parse_count((renderer.get("voteCount") or {}).get("simpleText"))
    attachment = renderer.get("backstageAttachment") or {}

    images = _collect_image_groups(attachment)
    video_ids = _collect_video_ids(attachment)
    polls = _collect_polls(attachment)

    items: list[MediaItem] = []
    for position, group in enumerate(images):
        item = _image_item(position, group, config)
        if item:
            items.append(item)
    for poll in polls:
        items.append(_poll_item(len(items), poll))
    for video_id in video_ids:
        items.append(
            MediaItem(
                index=len(items),
                id=video_id,
                kind=MediaKind.VIDEO,
                caption=text[:200] or None,
                source_url=f"https://www.youtube.com/watch?v={video_id}",
                meta={"attached_video": True, "video_id": video_id},
            )
        )

    kind = _post_media_kind(items)
    group_type = MediaGroupType.NONE
    if len(items) > 1:
        group_type = MediaGroupType.GALLERY if kind == MediaKind.IMAGE else MediaGroupType.ALBUM
    elif items:
        group_type = MediaGroupType.SINGLE

    title = _first_line(text) or f"Community post by {author or 'unknown'}"
    metadata = PostMetadata(
        platform=Platform.YOUTUBE,
        id=post_id or str(renderer.get("postId") or ""),
        url=requested_url or f"https://www.youtube.com/post/{post_id}",
        requested_url=requested_url,
        permalink=f"https://www.youtube.com/post/{post_id}",
        title=title,
        description=text or None,
        author=author,
        author_url=_channel_url(renderer.get("authorEndpoint")),
        thumbnail=(items[0].formats[-1].url if items and items[0].formats else None),
        media_type=kind,
        media_group_type=group_type,
        items=items,
        providers=["scrape"],
        quality_hints={
            "container": config.prefer_container,
            "codec": config.prefer_video_codec,
            "audio_codec": config.prefer_audio_codec,
        },
        extra={
            "post_type": "community",
            "published_text": published,
            "like_count_text": (renderer.get("voteCount") or {}).get("simpleText"),
            "attachment_keys": sorted(attachment.keys()),
            "image_count": len(images),
            "video_ids": video_ids,
            "poll_count": len(polls),
            "extracted_at": __import__("time").time(),
        },
        raw={"backstagePostRenderer": _trim(renderer)},
    )
    if likes is not None:
        metadata.like_count = likes
    if video_ids:
        metadata.warnings.append(
            "community post has an attached video: call get_metadata on its watch url "
            "to resolve formats before downloading"
        )
    for item in items:
        item.size_bytes = item.size_for("best", **metadata.spec_kwargs()) or item.total_size_bytes
    return metadata.rebuild_groups()


# --------------------------------------------------------------------- images
def _image_item(position: int, group: list[dict[str, Any]], config: YouTubeConfig) -> Optional[MediaItem]:
    thumbnails = [
        Thumbnail(
            url=_absolute(entry.get("url") or ""),
            width=entry.get("width"),
            height=entry.get("height"),
            extension=_extension_of(_absolute(entry.get("url") or "")),
        )
        for entry in group
        if entry.get("url")
    ]
    if not thumbnails:
        return None
    thumbnails.sort(key=lambda t: t.pixels)
    best = thumbnails[-1]
    formats: list[MediaFormat] = []
    seen: set[str] = set()
    for thumb in thumbnails:
        if thumb.url in seen:
            continue
        seen.add(thumb.url)
        formats.append(
            MediaFormat(
                format_id=f"thumb-{thumb.width or 0}",
                url=thumb.url,
                kind=FormatKind.IMAGE,
                origin=FormatOrigin.PREVIEW,
                extension=thumb.extension or "jpg",
                mime_type="image/webp" if (thumb.extension or "") == "webp" else None,
                width=thumb.width,
                height=thumb.height,
                has_video=False,
                has_audio=False,
                size_source=None,
                quality_label=f"{thumb.height or 0}p",
            )
        )
    original_url = _original_image_url(best.url)
    if original_url and original_url not in seen:
        formats.append(
            MediaFormat(
                format_id="original",
                url=original_url,
                kind=FormatKind.IMAGE,
                origin=FormatOrigin.DERIVED,
                extension="png",
                width=best.width,
                height=best.height,
                has_video=False,
                has_audio=False,
                quality_label="original",
                size_is_approx=True,
                size_source="estimate",
                note="full resolution (yt3.ggpht.com =s0 variant)",
            )
        )
    item = MediaItem(
        index=position,
        id=None,
        kind=MediaKind.IMAGE,
        caption=None,
        source_url=formats[-1].url if formats else best.url,
        extension=formats[-1].extension if formats else "jpg",
        mime_type=formats[-1].mime_type if formats else None,
        width=best.width,
        height=best.height,
        has_audio=False,
        formats=formats,
    )
    return item


def _collect_image_groups(node: Any) -> list[list[dict[str, Any]]]:
    """Every distinct thumbnail ladder inside an attachment subtree."""
    groups: list[list[dict[str, Any]]] = []

    def visit(current: Any) -> None:
        if isinstance(current, dict):
            thumbs = current.get("thumbnails")
            if isinstance(thumbs, list) and thumbs and all(isinstance(t, dict) for t in thumbs):
                if any(t.get("url") for t in thumbs):
                    groups.append([t for t in thumbs if t.get("url")])
            for key, value in current.items():
                if key == "thumbnails":
                    continue
                visit(value)
        elif isinstance(current, list):
            for entry in current:
                visit(entry)

    visit(node)
    deduped: list[list[dict[str, Any]]] = []
    seen: set[str] = set()
    for group in groups:
        key = _image_key(str(group[-1].get("url") or ""))
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(group)
    return deduped


def _collect_video_ids(node: Any) -> list[str]:
    found: list[str] = []
    for key, value in _walk_items(node):
        if key in _VIDEO_ID_KEYS and isinstance(value, str) and VIDEO_ID_RE.match(value):
            if value not in found:
                found.append(value)
    return found


def _collect_polls(node: Any) -> list[dict[str, Any]]:
    polls: list[dict[str, Any]] = []
    for key, value in _walk_items(node):
        if key == "choices" and isinstance(value, list):
            entries: list[str] = []
            for choice in value:
                text = _run_text((choice or {}).get("text")) if isinstance(choice, dict) else None
                if text:
                    entries.append(text)
            if entries:
                polls.append({"choices": entries})
    return polls


def _poll_item(position: int, poll: dict[str, Any]) -> MediaItem:
    choices = poll.get("choices") or []
    return MediaItem(
        index=position,
        kind=MediaKind.POLL,
        caption=" / ".join(choices)[:300] or None,
        meta={"choices": choices, "downloadable": False},
    )


# -------------------------------------------------------------------- helpers
def _image_key(url: str) -> str:
    if not url:
        return ""
    return _SIZE_SUFFIX_RE.sub("", url.split("?")[0])


def _original_image_url(url: str) -> Optional[str]:
    if not url:
        return None
    if _SIZE_SUFFIX_RE.search(url):
        return _SIZE_SUFFIX_RE.sub("=s0", url)
    return f"{url}=s0" if "yt3.ggpht.com" in url or "ytimg.com" in url else None


def _absolute(url: str) -> str:
    if url.startswith("//"):
        return f"https:{url}"
    return url


def _extension_of(url: str) -> Optional[str]:
    path = urlparse(url).path
    if "." not in path:
        return None
    return path.rsplit(".", 1)[-1].lower()[:5] or None


def _run_text(node: Any) -> Optional[str]:
    if not isinstance(node, dict):
        return None
    runs = node.get("runs")
    if isinstance(runs, list):
        text = "".join(run.get("text", "") for run in runs if isinstance(run, dict))
        return text or None
    simple = node.get("simpleText")
    return simple or None


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        cleaned = line.strip()
        if cleaned:
            return cleaned[:120]
    return ""


def _parse_count(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    match = _MULTIPLIER_RE.match(value.strip().replace(",", ""))
    if not match:
        try:
            return int(value)
        except ValueError:
            return None
    number, suffix = float(match.group(1)), match.group(2).lower()
    return int(number * {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}[suffix])


def _channel_url(node: Any) -> Optional[str]:
    if not isinstance(node, dict):
        return None
    base = ((node.get("commandMetadata") or {}).get("webCommandMetadata") or {}).get("url")
    if base:
        return f"https://www.youtube.com{base}" if base.startswith("/") else base
    return None


def _post_media_kind(items: list[MediaItem]) -> MediaKind:
    if not items:
        return MediaKind.TEXT
    kinds = {item.kind for item in items}
    if kinds == {MediaKind.IMAGE}:
        return MediaKind.IMAGE
    if MediaKind.VIDEO in kinds and len(kinds) == 1:
        return MediaKind.VIDEO
    if kinds <= {MediaKind.POLL, MediaKind.TEXT}:
        return MediaKind.POLL
    return MediaKind.GALLERY


def _walk_items(node: Any) -> Iterable[tuple[str, Any]]:
    if isinstance(node, dict):
        for key, value in node.items():
            yield key, value
            yield from _walk_items(value)
    elif isinstance(node, list):
        for entry in node:
            yield from _walk_items(entry)


def _find_first(node: Any, key: str) -> Any:
    for found_key, value in _walk_items(node):
        if found_key == key:
            return value
    return None


def _trim(node: Any, *, depth: int = 0) -> Any:
    """Shrink a payload for ``raw`` storage (thumbnails/tracking dropped)."""
    if depth > 6:
        return None
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key in ("trackingParams", "loggingDirectives", "clickTrackingParams"):
                continue
            trimmed = _trim(value, depth=depth + 1)
            if trimmed not in (None, {}, []):
                out[key] = trimmed
        return out
    if isinstance(node, list):
        return [entry for entry in (_trim(v, depth=depth + 1) for v in node) if entry not in (None, {}, [])]
    if isinstance(node, (str, int, float, bool)) or node is None:
        return node
    return None