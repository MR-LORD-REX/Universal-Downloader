"""Conversion of fxtwitter / yt-dlp payloads into the SDK's models."""

from __future__ import annotations

import time
from typing import Any, Optional, Sequence
from urllib.parse import urlparse

from ..core.enums import FormatKind, FormatOrigin, MediaGroupType, MediaKind, Platform
from ..core.models import MediaFormat, MediaItem, PostMetadata, Thumbnail
from .config import TwitterConfig
from .urls import TwitterRef

_PHOTO_VARIANTS = (
    ("orig", None),
    ("large", 2048),
    ("medium", 1200),
    ("small", 680),
)


def photo_formats(entry: dict[str, Any], config: TwitterConfig) -> list[MediaFormat]:
    """Build the ``pbs.twimg.com`` variant ladder for one photo."""
    base_url = _photo_base(str(entry.get("url") or ""))
    if not base_url:
        return []
    width, height = entry.get("width"), entry.get("height")
    preferred = (config.photo_quality or "orig").lower()
    formats: list[MediaFormat] = []
    for name, cap in _PHOTO_VARIANTS:
        if name == "orig":
            variant_height = height
            variant_width = width
        else:
            variant_height = min(height, cap) if height else None
            ratio = (width / height) if width and height else None
            variant_width = int(variant_height * ratio) if variant_height and ratio else None
        is_preferred = name == preferred
        formats.append(
            MediaFormat(
                format_id=f"photo-{name}",
                url=f"{base_url}?name={name}",
                kind=FormatKind.IMAGE,
                origin=FormatOrigin.DIRECT if is_preferred else FormatOrigin.PREVIEW,
                extension="jpg",
                mime_type="image/jpeg",
                width=variant_width,
                height=variant_height,
                quality_height=variant_height if is_preferred else variant_height,
                has_video=False,
                has_audio=False,
                quality_label=name,
                size_is_approx=True,
            )
        )
    if preferred != "orig":
        for fmt in formats:
            if fmt.format_id == f"photo-{preferred}":
                continue
            if fmt.format_id == "photo-orig":
                fmt.quality_height = (fmt.height or 0) - 1 if fmt.height else None
    return formats


def fx_video_formats(entry: dict[str, Any], config: TwitterConfig) -> list[MediaFormat]:
    """The video ladder fxtwitter reports (used when yt-dlp is unavailable)."""
    height = entry.get("height")
    width = entry.get("width")
    formats: list[MediaFormat] = []
    for variant in entry.get("formats") or ():
        url = variant.get("url")
        container = (variant.get("container") or "").lower()
        if not url or container != "mp4":
            continue
        bitrate = variant.get("bitrate")
        formats.append(
            MediaFormat(
                format_id=f"fx-{int(bitrate) if bitrate else len(formats)}",
                url=url,
                kind=FormatKind.MUXED,
                origin=FormatOrigin.FX,
                extension="mp4",
                mime_type="video/mp4",
                width=width,
                height=height,
                quality_height=height,
                bitrate_kbps=int(bitrate) // 1000 if bitrate else None,
                video_codec=variant.get("codec"),
                has_video=True,
                has_audio=True,
                quality_label=f"{height}p" if height else None,
                size_is_approx=True,
                note="fxtwitter variant",
            )
        )
    main = entry.get("url")
    if main and not any(fmt.url == main for fmt in formats):
        formats.append(
            MediaFormat(
                format_id="fx-source",
                url=str(main),
                kind=FormatKind.MUXED,
                origin=FormatOrigin.FX,
                extension="mp4",
                mime_type="video/mp4",
                width=width,
                height=height,
                quality_height=height,
                has_video=True,
                has_audio=True,
                quality_label=f"{height}p" if height else None,
                size_is_approx=True,
                note="fxtwitter source",
            )
        )
    return formats


def ytdlp_video_formats(info: dict[str, Any], config: TwitterConfig) -> list[MediaFormat]:
    """Normalise a yt-dlp twitter payload into Twitter's video ladder.

    yt-dlp's twitter extractor reports the progressive ``http-*`` renditions
    with ``vcodec``/``acodec`` both ``None`` even though the mp4 really does
    carry h264 video **and** aac audio (verified with ffprobe), so the kind is
    inferred from the format id/protocol instead of the codec fields.
    """
    formats: list[MediaFormat] = []
    for data in info.get("formats") or ():
        fmt = _one_format(data, config)
        if fmt is not None:
            formats.append(fmt)
    return _prune_manifests(formats, config)


def _one_format(data: dict[str, Any], config: TwitterConfig) -> Optional[MediaFormat]:
    format_id = str(data.get("format_id") or "")
    url = data.get("url")
    if not format_id or not url:
        return None
    protocol = (data.get("protocol") or "").lower()
    extension = data.get("ext")
    vcodec = data.get("vcodec")
    acodec = data.get("acodec")
    has_video = bool(vcodec) and vcodec != "none"
    has_audio = bool(acodec) and acodec != "none"
    is_manifest = "m3u8" in protocol or "dash" in protocol

    if format_id.startswith("hls-audio") or (is_manifest and has_audio and not has_video):
        kind, has_video, has_audio = FormatKind.AUDIO, False, True
    elif is_manifest:
        kind, has_video, has_audio = FormatKind.VIDEO, True, False
    elif extension == "mp4" and not has_video and not has_audio:
        kind, has_video, has_audio = FormatKind.MUXED, True, True
    elif has_video and has_audio:
        kind = FormatKind.MUXED
    elif has_video:
        kind = FormatKind.VIDEO
    elif has_audio:
        kind = FormatKind.AUDIO
    else:
        return None

    height = data.get("height")
    bitrate_kbps = _bitrate_of(data, format_id)
    return MediaFormat(
        format_id=format_id,
        url=str(url),
        kind=kind,
        origin=FormatOrigin.YTDLP,
        container=data.get("container") or extension,
        extension=extension,
        mime_type="video/mp4" if extension == "mp4" else None,
        protocol=data.get("protocol"),
        width=data.get("width"),
        height=height,
        quality_height=height if has_video else None,
        fps=data.get("fps"),
        bitrate_kbps=bitrate_kbps,
        audio_bitrate_kbps=int(data["abr"]) if data.get("abr") else None,
        codecs=", ".join(p for p in (vcodec, acodec) if p) or None,
        video_codec=vcodec if has_video else None,
        audio_codec=acodec if has_audio else None,
        size_bytes=None,
        size_is_approx=True,
        has_video=has_video,
        has_audio=has_audio,
        quality_label=f"{height}p" if height else ("audio" if kind == FormatKind.AUDIO else None),
        http_headers=dict(data.get("http_headers") or {}),
        meta={
            "ytdlp_filesize_approx": data.get("filesize_approx"),
            "ytdlp_tbr": data.get("tbr"),
            "ytdlp_vbr": data.get("vbr"),
        },
    )


def _prune_manifests(formats: Sequence[MediaFormat], config: TwitterConfig) -> list[MediaFormat]:
    """Drop HLS renditions that duplicate a progressive one."""
    progressive = [f for f in formats if not f.is_manifest]
    heights = {f.quality_height for f in progressive if f.quality_height is not None}
    has_progressive_video = any(f.has_video for f in progressive)
    kept: list[MediaFormat] = []
    for fmt in formats:
        if fmt.is_manifest:
            if config.include_manifest_formats:
                kept.append(fmt)
                continue
            if has_progressive_video:
                continue
            if fmt.kind == FormatKind.AUDIO and not any(
                other.has_video for other in formats if other.is_manifest
            ):
                continue
            if fmt.quality_height is not None and fmt.quality_height in heights:
                continue
        kept.append(fmt)
    return kept


def _bitrate_of(data: dict[str, Any], format_id: str) -> Optional[int]:
    for key in ("tbr", "vbr", "abr"):
        value = data.get(key)
        if value:
            return int(value)
    tail = format_id.rsplit("-", 1)[-1]
    if tail.isdigit():
        return int(tail)
    return None


def _photo_base(url: str) -> Optional[str]:
    if not url:
        return None
    parsed = urlparse(url)
    if not parsed.scheme:
        return None
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}" if _is_photo(parsed.path) else None


def _is_photo(path: str) -> bool:
    lowered = path.lower()
    return lowered.endswith((".jpg", ".jpeg", ".png", ".webp")) or "/media/" in lowered


def tweet_media_entries(tweet: dict[str, Any]) -> list[dict[str, Any]]:
    """Every media entry of a tweet, in tweet order."""
    media = tweet.get("media") or {}
    entries = media.get("all")
    if isinstance(entries, list) and entries:
        return [e for e in entries if isinstance(e, dict)]
    collected: list[dict[str, Any]] = []
    for key in ("videos", "photos"):
        value = media.get(key)
        if isinstance(value, list):
            collected.extend(e for e in value if isinstance(e, dict))
        elif isinstance(value, dict):
            collected.append(value)
    return collected


def tweet_to_items(tweet: dict[str, Any], config: TwitterConfig) -> list[MediaItem]:
    """Build one :class:`MediaItem` per tweet attachment."""
    items: list[MediaItem] = []
    for entry in tweet_media_entries(tweet):
        kind = str(entry.get("type") or "").lower()
        if kind == "photo":
            if not config.include_photos:
                continue
            formats = photo_formats(entry, config)
            if not formats:
                continue
            items.append(
                MediaItem(
                    index=len(items),
                    id=str(entry.get("id") or ""),
                    kind=MediaKind.IMAGE,
                    caption=tweet.get("text"),
                    source_url=tweet.get("url"),
                    extension="jpg",
                    mime_type="image/jpeg",
                    width=entry.get("width"),
                    height=entry.get("height"),
                    has_audio=False,
                    formats=formats,
                )
            )
            continue
        if kind in ("video", "gif", "amplify"):
            if not config.include_videos:
                continue
            # Twitter "gifs" are silent looping mp4s: claiming audio would make a
            # bot promise sound and then upload a muted clip.
            is_gif = kind == "gif"
            formats = fx_video_formats(entry, config)
            if is_gif:
                for fmt in formats:
                    fmt.kind = FormatKind.VIDEO
                    fmt.has_audio = False
                    fmt.has_video = True
            items.append(
                MediaItem(
                    index=len(items),
                    id=str(entry.get("id") or ""),
                    kind=MediaKind.GIF if is_gif and not config.include_gif_as_video else MediaKind.VIDEO,
                    caption=tweet.get("text"),
                    source_url=tweet.get("url"),
                    extension="mp4",
                    mime_type="video/mp4",
                    width=entry.get("width"),
                    height=entry.get("height"),
                    duration=entry.get("duration"),
                    has_audio=False if is_gif else True,
                    formats=formats,
                    meta={"thumbnail": entry.get("thumbnail_url"), "media_type": kind},
                )
            )
    if not config.include_all_media and items:
        items = items[:1]
    return items


def tweet_to_metadata(
    tweet: dict[str, Any],
    config: TwitterConfig,
    *,
    ref: Optional[TwitterRef] = None,
    requested_url: Optional[str] = None,
) -> PostMetadata:
    """Turn an fxtwitter tweet payload into :class:`PostMetadata`."""
    items = tweet_to_items(tweet, config)
    author = tweet.get("author") or {}
    create_ts = tweet.get("created_timestamp")
    media_kind = _media_kind(items)
    group = MediaGroupType.NONE
    if len(items) > 1:
        group = MediaGroupType.GALLERY if media_kind == MediaKind.IMAGE else MediaGroupType.ALBUM
    elif items:
        group = MediaGroupType.SINGLE
    thumbnails = [
        Thumbnail(url=url, note="tweet media preview")
        for url in _thumbnails(tweet, items)
        if url
    ]
    metadata = PostMetadata(
        platform=Platform.TWITTER,
        id=str(tweet.get("id") or (ref.tweet_id if ref else "")),
        url=tweet.get("url") or (ref.status_url if ref else requested_url),
        requested_url=requested_url,
        permalink=tweet.get("url"),
        title=_title(tweet),
        description=tweet.get("text"),
        author=author.get("name") or author.get("screen_name"),
        author_id=author.get("screen_name"),
        author_url=f"https://x.com/{author.get('screen_name')}" if author.get("screen_name") else None,
        created_utc=float(create_ts) if create_ts else None,
        timestamp=int(create_ts) if create_ts else None,
        language=tweet.get("lang"),
        like_count=tweet.get("likes"),
        comment_count=tweet.get("replies"),
        repost_count=tweet.get("retweets"),
        view_count=tweet.get("views"),
        thumbnail=thumbnails[0].url if thumbnails else None,
        thumbnails=thumbnails,
        media_type=media_kind,
        media_group_type=group,
        items=items,
        quality_hints={
            "container": config.prefer_container,
            "codec": config.prefer_video_codec,
            "audio_codec": config.prefer_audio_codec,
            "prefer_muxed": True,
        },
        providers=["fxtwitter"],
        extra={
            "quotes": tweet.get("quotes"),
            "bookmarks": tweet.get("bookmarks"),
            "created_at": tweet.get("created_at"),
            "twitter_card": tweet.get("twitter_card"),
            "source": (tweet.get("source") or {}).get("name") if isinstance(tweet.get("source"), dict) else tweet.get("source"),
            "media_api": "fxtwitter",
            "media_count": len(items),
            "extracted_at": time.time(),
        },
        raw=_trim_tweet(tweet),
    )
    for item in items:
        item.size_bytes = item.total_size_bytes
    return metadata.rebuild_groups()


def attach_video_formats(
    metadata: PostMetadata, ladder: Sequence[MediaFormat]
) -> int:
    """Replace the video items' provisional ladders with the yt-dlp one."""
    if not ladder:
        return 0
    applied = 0
    for item in metadata.items:
        if item.kind not in (MediaKind.VIDEO, MediaKind.GIF) or not item.formats:
            continue
        if not any(fmt.has_video for fmt in ladder):
            continue
        item.formats = list(ladder)
        best_video = max(
            (f for f in ladder if f.has_video),
            key=lambda f: (f.quality_height or 0, f.bitrate_kbps or 0),
            default=None,
        )
        if best_video is not None:
            if best_video.height:
                item.height = best_video.height
            if best_video.width:
                item.width = best_video.width
        item.meta["formats_source"] = "ytdlp"
        applied += 1
    if applied:
        metadata.providers = list(dict.fromkeys(metadata.providers + ["ytdlp"]))
    return applied


def _media_kind(items: Sequence[MediaItem]) -> MediaKind:
    if not items:
        return MediaKind.TEXT
    kinds = {item.kind for item in items}
    if kinds == {MediaKind.IMAGE}:
        return MediaKind.IMAGE
    if kinds == {MediaKind.GIF}:
        return MediaKind.GIF
    if kinds == {MediaKind.VIDEO}:
        return MediaKind.VIDEO
    return MediaKind.GALLERY


def _title(tweet: dict[str, Any]) -> str:
    text = (tweet.get("text") or "").strip()
    if text:
        first = next((line.strip() for line in text.splitlines() if line.strip()), "")
        return first[:120]
    author = (tweet.get("author") or {}).get("screen_name") or "unknown"
    return f"Tweet by {author}"


def _thumbnails(tweet: dict[str, Any], items: Sequence[MediaItem]) -> list[str]:
    """Preview links: the video poster frame, else the best photo rendition."""
    urls: list[str] = []
    for item in items:
        thumb = item.meta.get("thumbnail")
        if thumb:
            urls.append(str(thumb))
        elif item.formats:
            try:
                urls.append(item.select("best").url)
            except Exception:  # pragma: no cover - defensive
                continue
    author = tweet.get("author") or {}
    if not urls and author.get("avatar_url"):
        urls.append(str(author["avatar_url"]))
    return urls


def _trim_tweet(tweet: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in tweet.items():
        if key in ("media",):
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            out[key] = value
        elif isinstance(value, dict):
            out[key] = {
                k: v for k, v in value.items() if isinstance(v, (str, int, float, bool)) or v is None
            }
    return out