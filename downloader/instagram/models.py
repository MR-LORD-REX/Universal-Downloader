"""Turn Instaloader payloads into the SDK's canonical models.

Instaloader hands back a ``Post`` whose ``_node`` is the raw GraphQL dict. This
module reads that raw dict - not the convenience wrappers - because it is the
only place that carries the full image candidate ladder, the progressive video
mp4s *and* the DASH manifest.

Instagram expresses quality against the **short side** of the frame: a portrait
reel is ``720x1280`` and Instagram calls it "720p", and the DASH manifest labels
every representation on that same frame 240p...720p. So ``quality_height`` comes
from the label when there is one and falls back to ``min(width, height)``.
"""

from __future__ import annotations

from typing import Any, Optional

from ..core.enums import (
    FormatKind,
    FormatOrigin,
    MediaGroupType,
    MediaKind,
    Platform,
)
from ..core.manifests import parse_dash_tracks
from ..core.models import MediaFormat, MediaItem, PostMetadata, Thumbnail, parse_height
from ..core.urls import extension_of, filename_of, mime_for_extension
from .config import InstagramConfig

GRAPH_SIDECAR = "GraphSidecar"
MEDIA_TYPE_IMAGE = 1
MEDIA_TYPE_VIDEO = 2
MEDIA_TYPE_CAROUSEL = 8

__all__ = [
    "GRAPH_SIDECAR",
    "dash_formats",
    "image_formats",
    "ladder_height",
    "node_to_item",
    "post_to_metadata",
    "progressive_video_formats",
]


def ladder_height(
    width: Optional[int], height: Optional[int], label: Optional[str] = None
) -> Optional[int]:
    """Instagram's quality class for a frame: the label, else the short side."""
    if label:
        parsed = parse_height(label)
        if parsed:
            return parsed
    sides = [side for side in (width, height) if side]
    return min(sides) if sides else None


def _int(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def raw_nodes(post: Any, config: Optional[InstagramConfig] = None) -> list[dict[str, Any]]:
    """The raw media nodes of a post: one per carousel child, else the post.

    ``config.include_carousels=False`` keeps only the first child, which is how
    the other SDKs read "do not expand this album".
    """
    node = getattr(post, "_node", None) or {}
    if getattr(post, "typename", None) == GRAPH_SIDECAR:
        children = [child for child in (node.get("carousel_media") or []) if isinstance(child, dict)]
        if children:
            if config is not None and not config.include_carousels:
                return children[:1]
            return children
    return [node] if node else []


# --------------------------------------------------------------------- formats
def image_formats(node: dict[str, Any], config: InstagramConfig) -> list[MediaFormat]:
    """The ``image_versions2`` candidates, largest first and de-duplicated."""
    candidates = (node.get("image_versions2") or {}).get("candidates") or []
    formats: list[MediaFormat] = []
    seen: set[tuple[Optional[int], Optional[int]]] = set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        url = candidate.get("url")
        if not url:
            continue
        width = _int(candidate.get("width"))
        height = _int(candidate.get("height"))
        key = (width, height)
        if key in seen:
            continue
        seen.add(key)
        extension = extension_of(url) or "jpg"
        formats.append(
            MediaFormat(
                format_id=f"image-{width}x{height}",
                url=str(url),
                kind=FormatKind.IMAGE,
                origin=FormatOrigin.DIRECT,
                container=extension,
                mime_type=mime_for_extension(extension) or "image/jpeg",
                extension=extension,
                width=width,
                height=height,
                quality_height=ladder_height(width, height),
                quality_label=f"{width}x{height}" if width and height else None,
                has_video=False,
                has_audio=False,
                note="image candidate",
                meta={"candidate": candidate.get("type")},
            )
        )
    formats.sort(key=lambda fmt: fmt.pixels, reverse=True)
    return formats[: max(1, config.max_image_candidates)]


def progressive_video_formats(node: dict[str, Any]) -> list[MediaFormat]:
    """Instagram's progressive mp4s: H.264 + AAC in a single file."""
    versions = node.get("video_versions") or []
    formats: list[MediaFormat] = []
    seen: set[tuple[Optional[int], Optional[int]]] = set()
    for version in versions:
        if not isinstance(version, dict):
            continue
        url = version.get("url")
        if not url:
            continue
        width = _int(version.get("width"))
        height = _int(version.get("height"))
        key = (width, height)
        if key in seen:
            continue
        seen.add(key)
        quality = ladder_height(width, height)
        formats.append(
            MediaFormat(
                format_id=f"progressive-{width}x{height}",
                url=str(url),
                kind=FormatKind.MUXED,
                origin=FormatOrigin.DIRECT,
                container="mp4",
                mime_type="video/mp4",
                extension="mp4",
                width=width,
                height=height,
                quality_height=quality,
                quality_label=f"{quality}p" if quality else None,
                bitrate_kbps=_int(version.get("bitrate")) or None,
                has_video=True,
                has_audio=True,
                note="progressive mp4 (h264 + aac)",
                meta={"video_version_type": version.get("type")},
            )
        )
    return formats


def dash_formats(node: dict[str, Any], config: InstagramConfig, base_url: str = "") -> list[MediaFormat]:
    """The DASH ladder (video-only) plus its single audio track."""
    if not config.include_dash:
        return []
    manifest = node.get("video_dash_manifest")
    if not manifest:
        return []
    try:
        tracks = parse_dash_tracks(str(manifest), base_url or str(node.get("video_url") or ""))
    except ValueError:
        return []
    formats: list[MediaFormat] = []
    for track in tracks:
        if track.is_video:
            quality = ladder_height(track.width, track.height, track.quality_label)
            formats.append(
                MediaFormat(
                    format_id=f"dash-{track.quality_label or track.track_id or len(formats)}",
                    url=track.url,
                    kind=FormatKind.VIDEO,
                    origin=FormatOrigin.DASH,
                    container=track.extension,
                    mime_type=track.mime_type,
                    extension=track.extension,
                    width=track.width,
                    height=track.height,
                    quality_height=quality,
                    quality_label=track.quality_label,
                    fps=track.frame_rate,
                    bitrate_kbps=(track.bandwidth // 1000) if track.bandwidth else None,
                    codecs=track.codecs,
                    video_codec=None,
                    size_bytes=track.size_bytes,
                    size_source="manifest" if track.size_bytes else None,
                    has_video=True,
                    has_audio=False,
                    note="DASH video only (needs the audio track muxed in)",
                    meta={"track_id": track.track_id},
                )
            )
        elif track.is_audio:
            formats.append(
                MediaFormat(
                    format_id=f"dash-audio-{track.track_id or len(formats)}",
                    url=track.url,
                    kind=FormatKind.AUDIO,
                    origin=FormatOrigin.DASH,
                    container="mp4",
                    mime_type=track.mime_type or "audio/mp4",
                    extension="m4a",
                    bitrate_kbps=(track.bandwidth // 1000) if track.bandwidth else None,
                    audio_bitrate_kbps=(track.bandwidth // 1000) if track.bandwidth else None,
                    codecs=track.codecs,
                    audio_codec=track.codecs,
                    language=track.lang,
                    size_bytes=track.size_bytes,
                    size_source="manifest" if track.size_bytes else None,
                    has_audio=True,
                    has_video=False,
                    note="DASH audio (muxed into the video)",
                    meta={
                        "track_id": track.track_id,
                        "sampling_rate": track.audio_sampling_rate,
                        "channels": track.audio_channels,
                    },
                )
            )
    return formats


# --------------------------------------------------------------------- items
def node_to_item(
    node: dict[str, Any],
    index: int,
    *,
    config: InstagramConfig,
    post_url: str,
    shortcode: str,
    carousel: bool,
) -> Optional[MediaItem]:
    """One carousel child (or the whole post) as a canonical :class:`MediaItem`."""
    is_video = _int(node.get("media_type")) == MEDIA_TYPE_VIDEO or bool(node.get("video_versions"))
    if is_video and not config.include_videos:
        return None
    media_id = node.get("pk") or node.get("id") or node.get("code")

    formats: list[MediaFormat] = []
    if is_video:
        if config.include_progressive:
            formats.extend(progressive_video_formats(node))
        formats.extend(dash_formats(node, config))
    if not formats and config.include_images:
        formats.extend(image_formats(node, config))
    if not formats and is_video:
        # A video post whose ladder we could not read, but which still exposes
        # a plain display_url: better a poster frame than nothing.
        formats.extend(image_formats(node, config))
    if not formats:
        return None

    width = _int(node.get("original_width"))
    height = _int(node.get("original_height"))
    if width is None or height is None:
        primary = formats[0]
        width = width or primary.width
        height = height or primary.height

    kind = MediaKind.VIDEO if is_video else MediaKind.IMAGE
    if is_video and not any(fmt.has_video for fmt in formats):
        kind = MediaKind.IMAGE

    known_sizes = [fmt.size_bytes for fmt in formats if fmt.size_bytes]
    duration = node.get("video_duration")
    return MediaItem(
        index=index,
        id=str(media_id) if media_id else None,
        kind=kind,
        source_url=f"{post_url}?img_index={index + 1}" if carousel else post_url,
        mime_type=formats[0].mime_type,
        extension=formats[0].extension,
        width=width,
        height=height,
        duration=float(duration) if duration else None,
        has_audio=True if kind is MediaKind.VIDEO else None,
        size_bytes=max(known_sizes) if known_sizes else None,
        size_is_approx=not known_sizes,
        formats=formats,
        meta={
            "media_type": _int(node.get("media_type")),
            "carousel": carousel,
            "carousel_index": index,
            "accessibility_caption": node.get("accessibility_caption"),
        },
    )


def post_to_metadata(
    post: Any,
    *,
    config: InstagramConfig,
    requested_url: Optional[str] = None,
    canonical_url: Optional[str] = None,
) -> PostMetadata:
    """Everything the SDK knows about an Instagram post, before any download."""
    node = getattr(post, "_node", None) or {}
    shortcode = str(getattr(post, "shortcode", "") or node.get("code") or node.get("shortcode") or "")
    post_url = canonical_url or f"https://www.instagram.com/p/{shortcode}/"
    nodes = raw_nodes(post, config)
    carousel = len(nodes) > 1

    items: list[MediaItem] = []
    for raw in nodes[: max(1, config.max_carousel_items)]:
        item = node_to_item(
            raw,
            len(items),
            config=config,
            post_url=post_url,
            shortcode=shortcode,
            carousel=carousel,
        )
        if item is not None:
            items.append(item)

    caption = node.get("caption")
    if isinstance(caption, dict):
        caption = caption.get("text")
    caption = str(caption).strip() if caption else ""
    title = next((line.strip() for line in caption.splitlines() if line.strip()), "") or None

    owner = node.get("owner") or {}
    username = owner.get("username") or getattr(post, "owner_username", None)
    owner_id = owner.get("id") or getattr(post, "owner_id", None)
    taken_at = _int(node.get("taken_at_timestamp"))
    product_type = node.get("product_type")

    kinds = {item.kind for item in items}
    if len(items) > 1:
        media_type = MediaKind.GALLERY
        group = MediaGroupType.ALBUM
    elif kinds == {MediaKind.VIDEO}:
        media_type = MediaKind.VIDEO
        group = MediaGroupType.SINGLE
    else:
        media_type = MediaKind.IMAGE
        group = MediaGroupType.SINGLE

    thumbnails = [
        Thumbnail(
            url=fmt.url,
            width=fmt.width,
            height=fmt.height,
            extension=fmt.extension,
            size_bytes=fmt.size_bytes,
            note="image candidate",
        )
        for item in items
        for fmt in item.formats
        if fmt.kind is FormatKind.IMAGE
    ][:4]

    return PostMetadata(
        platform=Platform.INSTAGRAM,
        id=shortcode,
        url=post_url,
        requested_url=requested_url or post_url,
        permalink=post_url,
        title=title,
        description=caption or None,
        author=str(username) if username else None,
        author_id=str(owner_id) if owner_id else None,
        author_url=f"https://www.instagram.com/{username}/" if username else None,
        channel=str(username) if username else None,
        created_utc=float(taken_at) if taken_at else None,
        duration=float(node.get("video_duration")) if node.get("video_duration") else None,
        view_count=_int(node.get("video_view_count") or node.get("play_count")),
        like_count=_int(node.get("like_count")),
        comment_count=_int(node.get("comment_count")),
        thumbnail=thumbnails[0].url if thumbnails else None,
        thumbnails=thumbnails,
        media_type=media_type,
        media_group_type=group,
        items=items,
        is_short=product_type == "clips",
        is_playlist=False,
        providers=["instaloader"],
        quality_hints={"prefer_muxed": True, "container": "mp4", "codec": "avc1"},
        extra={
            "product_type": product_type,
            "typename": getattr(post, "typename", None),
            "is_dash_eligible": node.get("is_dash_eligible"),
            "number_of_qualities": node.get("number_of_qualities"),
            "carousel_count": node.get("carousel_media_count"),
            "signed_url_warning": (
                "Instagram CDN urls are signed and expire (typically 24-48h): "
                "deliver or download promptly."
            ),
        },
        raw=_trim(node),
    )


def _trim(node: dict[str, Any], *, keep: int = 60) -> dict[str, Any]:
    """A small, json-safe slice of the payload for debugging."""
    trimmed: dict[str, Any] = {}
    for position, (key, value) in enumerate(node.items()):
        if position >= keep:
            break
        if key in ("carousel_media", "edge_media_to_caption", "edge_media_to_parent_comment"):
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            trimmed[key] = value
    return trimmed
