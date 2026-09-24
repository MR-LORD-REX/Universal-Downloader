"""Conversion of yt-dlp TikTok payloads into the SDK's models.

What yt-dlp gives us (see ``yt_dlp.extractor.tiktok``):

* ``bitrateInfo[].PlayAddr`` renditions with a real ``format_id``
  (``h264_540p_941613`` / ``bytevc1_720p_...``), codec, resolution, ``tbr``,
  ``fps`` and a real ``filesize`` (``DataSize``),
* a ``play`` rendition (the default mp4 of the current player), and
* a ``download`` rendition that always carries TikTok's watermark.

Photo/slideshow posts are *not* supported upstream: yt-dlp parses only
``video.bitrateInfo``/``playAddr``, so a slideshow yields the soundtrack as an
``m4a`` and nothing else. :func:`info_to_metadata` reports that honestly (an
audio item plus a warning) instead of inventing an image path.
"""

from __future__ import annotations

import re
import time
from typing import Any, Mapping, Optional, Sequence
from urllib.parse import urlparse

from ..core.enums import FormatKind, FormatOrigin, MediaGroupType, MediaKind, Platform
from ..core.models import MediaFormat, MediaItem, PostMetadata, Thumbnail
from .config import TikTokConfig
from .urls import TikTokRef

_WATERMARK_NOTE = "watermarked"
_UNPLAYABLE_MARKERS = ("unplayable", "bytevc2", "h266")

_MIME_BY_EXT = {
    "mp4": "video/mp4",
    "webm": "video/webm",
    "m4a": "audio/mp4",
    "mp3": "audio/mpeg",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
}


def format_from_info(data: Mapping[str, Any], config: TikTokConfig) -> Optional[MediaFormat]:
    """Build a :class:`MediaFormat` from one yt-dlp TikTok format dict."""
    format_id = str(data.get("format_id") or "")
    url = data.get("url")
    if not format_id or not url:
        return None
    note = str(data.get("format_note") or "")
    lowered_note = note.lower()
    if _WATERMARK_NOTE in lowered_note and not config.include_watermarked:
        return None
    if any(marker in lowered_note for marker in _UNPLAYABLE_MARKERS) and not config.include_unplayable:
        return None

    vcodec = data.get("vcodec")
    acodec = data.get("acodec")
    has_video = bool(vcodec) and vcodec != "none"
    has_audio = bool(acodec) and acodec != "none"
    extension = data.get("ext")
    protocol = str(data.get("protocol") or "")
    is_manifest = "m3u8" in protocol or "dash" in protocol

    if has_video and has_audio:
        kind = FormatKind.MUXED
    elif is_manifest and has_video:
        # an HLS rendition listed without codec metadata is still a video track
        kind, has_audio = FormatKind.VIDEO, False
    elif has_video:
        kind = FormatKind.VIDEO
    elif has_audio:
        kind = FormatKind.AUDIO
    else:
        return None

    height = _quality_height(data, format_id)
    size = data.get("filesize")
    approx = data.get("filesize_approx")
    size_is_approx = False
    size_source: Optional[str] = None
    if size:
        size_source = "metadata"
    elif approx:
        size, size_is_approx, size_source = int(approx), True, "estimate"
    bitrate = data.get("tbr") or data.get("vbr") or data.get("abr")

    return MediaFormat(
        format_id=format_id,
        url=str(url),
        kind=kind,
        origin=FormatOrigin.YTDLP,
        container=data.get("container") or extension,
        extension=extension,
        mime_type=_MIME_BY_EXT.get(str(extension or "").lower()),
        protocol=data.get("protocol"),
        width=_int_or_none(data.get("width")),
        height=_int_or_none(data.get("height")),
        quality_height=height if has_video else None,
        fps=_float_or_none(data.get("fps")),
        bitrate_kbps=int(bitrate) if bitrate else None,
        audio_bitrate_kbps=int(data["abr"]) if data.get("abr") else None,
        codecs=", ".join(part for part in (vcodec, acodec) if part) or None,
        video_codec=vcodec if has_video else None,
        audio_codec=acodec if has_audio else None,
        size_bytes=int(size) if size else None,
        size_is_approx=size_is_approx,
        size_source=size_source,
        has_video=has_video,
        has_audio=has_audio,
        quality_label=_quality_label(kind, height, data),
        note=note or None,
        http_headers=dict(data.get("http_headers") or {}),
        meta={
            "ytdlp_format_note": note or None,
            "watermarked": _WATERMARK_NOTE in lowered_note,
            "source_preference": data.get("source_preference"),
        },
    )


def normalize_formats(
    infos: Sequence[Mapping[str, Any]], config: TikTokConfig
) -> list[MediaFormat]:
    """Convert every yt-dlp format, de-duplicating urls and pruning manifests.

    The same CDN url is routinely reported several times (once per
    ``bitrateInfo`` entry and once as ``play``), so urls are de-duplicated
    keeping the richest description, and the watermarked ``download`` rendition
    survives only when nothing else came back.
    """
    formats: list[MediaFormat] = []
    seen: dict[str, MediaFormat] = {}
    for data in infos:
        fmt = format_from_info(data, config)
        if fmt is None:
            continue
        existing = seen.get(fmt.url)
        if existing is not None:
            _merge_format(existing, fmt)
            continue
        seen[fmt.url] = fmt
        formats.append(fmt)

    if not config.include_manifests:
        progressive_heights = {
            fmt.quality_height
            for fmt in formats
            if not fmt.is_manifest and fmt.has_video and fmt.quality_height is not None
        }
        if any(fmt.has_video for fmt in formats if not fmt.is_manifest):
            formats = [
                fmt
                for fmt in formats
                if not (fmt.is_manifest and (fmt.quality_height in progressive_heights
                                             or fmt.quality_height is None))
            ]

    if not config.include_watermarked:
        clean = [fmt for fmt in formats if not fmt.meta.get("watermarked")]
        if any(fmt.has_video for fmt in clean):
            formats = clean
    return formats


def _merge_format(target: MediaFormat, other: MediaFormat) -> None:
    """Fill the gaps of ``target`` from a duplicate rendition of the same url."""
    for field in (
        "width",
        "height",
        "quality_height",
        "fps",
        "bitrate_kbps",
        "audio_bitrate_kbps",
        "video_codec",
        "audio_codec",
        "size_bytes",
    ):
        if getattr(target, field) in (None, 0) and getattr(other, field) not in (None, 0):
            setattr(target, field, getattr(other, field))
    if target.kind == FormatKind.VIDEO and other.kind == FormatKind.MUXED:
        target.kind = FormatKind.MUXED
        target.has_audio = True
    if target.quality_label is None and other.quality_label:
        target.quality_label = other.quality_label


#: yt-dlp maps a rendition to ``quality`` 0..3 for TikTok (its own ladder).
_QUALITY_SCORE_HEIGHTS = (360, 540, 720, 1080, 1440, 2160)


def _quality_height(data: Mapping[str, Any], format_id: str = "") -> Optional[int]:
    """The label of a rendition, in the order TikTok itself uses.

    Three sources, most trustworthy first:

    1. the ``<n>p`` token yt-dlp leaves in the format id
       (``h264_540p_941613`` -> ``540``), which is TikTok's own naming;
    2. yt-dlp's ``quality`` rank for renditions whose id carries no token
       (notably ``play``);
    3. the short side of the resolution, because TikTok videos are
       predominantly vertical: a ``1080x1920`` rendition is sold as ``1080p``
       while its raw height would read as ``1920p``.
    """
    match = re.search(r"(\d{3,4})p", format_id or "")
    if match:
        return int(match.group(1))
    score = data.get("quality")
    if isinstance(score, int) and 0 <= score < len(_QUALITY_SCORE_HEIGHTS):
        return _QUALITY_SCORE_HEIGHTS[score]
    width = _int_or_none(data.get("width"))
    height = _int_or_none(data.get("height"))
    if width and height:
        return min(width, height)
    return height or width


def _quality_label(kind: FormatKind, height: Optional[int], data: Mapping[str, Any]) -> Optional[str]:
    if kind == FormatKind.AUDIO:
        abr = data.get("abr")
        return f"audio{int(abr)}" if abr else "audio"
    if height:
        codec = str(data.get("vcodec") or "")
        suffix = ""
        if codec.startswith("hvc") or codec.startswith("hev") or codec == "h265":
            suffix = "_h265"
        return f"{height}p{suffix}"
    return None


def info_to_metadata(
    info: Mapping[str, Any],
    config: TikTokConfig,
    *,
    requested_url: Optional[str] = None,
    ref: Optional[TikTokRef] = None,
) -> PostMetadata:
    """Turn a yt-dlp TikTok video info dict into :class:`PostMetadata`."""
    formats = normalize_formats(info.get("formats") or [], config)
    video_formats = [fmt for fmt in formats if fmt.has_video]
    audio_formats = [fmt for fmt in formats if fmt.kind == FormatKind.AUDIO]
    thumbnails = thumbnails_from_info(info.get("thumbnails") or [])
    best_thumb = _best_thumbnail(thumbnails) or info.get("thumbnail")
    duration = _float_or_none(info.get("duration"))
    audio_only = not video_formats and bool(audio_formats)

    if audio_only:
        item = MediaItem(
            index=0,
            id=str(info.get("id") or ""),
            kind=MediaKind.AUDIO,
            caption=info.get("title") or info.get("description"),
            source_url=info.get("webpage_url") or requested_url,
            extension=audio_formats[0].extension or "m4a",
            mime_type=audio_formats[0].mime_type,
            duration=duration,
            has_audio=True,
            formats=audio_formats if config.include_audio_only else [],
            meta={"thumbnail": best_thumb},
        )
        media_kind, group = MediaKind.AUDIO, MediaGroupType.SINGLE
    else:
        best = max(
            video_formats or formats,
            key=lambda fmt: (fmt.quality_height or 0, fmt.bitrate_kbps or 0, fmt.size_bytes or 0),
            default=None,
        )
        item = MediaItem(
            index=0,
            id=str(info.get("id") or ""),
            kind=MediaKind.VIDEO,
            caption=info.get("title") or info.get("description"),
            source_url=info.get("webpage_url") or requested_url,
            extension=(best.extension if best else None) or "mp4",
            mime_type=(best.mime_type if best else None) or "video/mp4",
            width=_int_or_none(info.get("width")) or (best.width if best else None),
            height=_int_or_none(info.get("height")) or (best.height if best else None),
            duration=duration,
            has_audio=any(fmt.has_audio for fmt in formats),
            formats=formats,
            meta={"thumbnail": best_thumb},
        )
        media_kind, group = MediaKind.VIDEO, MediaGroupType.SINGLE

    metadata = PostMetadata(
        platform=Platform.TIKTOK,
        id=str(info.get("id") or (ref.video_id if ref else "") or ""),
        url=info.get("webpage_url") or requested_url,
        requested_url=requested_url,
        permalink=info.get("webpage_url"),
        title=_title(info),
        description=info.get("description"),
        author=info.get("uploader") or info.get("channel"),
        author_id=str(info.get("uploader_id") or "") or None,
        author_url=info.get("uploader_url"),
        channel=info.get("channel"),
        created_utc=float(info["timestamp"]) if info.get("timestamp") else None,
        upload_date=info.get("upload_date"),
        timestamp=_int_or_none(info.get("timestamp")),
        duration=duration,
        view_count=_int_or_none(info.get("view_count")),
        like_count=_int_or_none(info.get("like_count")),
        comment_count=_int_or_none(info.get("comment_count")),
        repost_count=_int_or_none(info.get("repost_count")),
        thumbnail=best_thumb,
        thumbnails=thumbnails,
        media_type=media_kind,
        media_group_type=group,
        items=[item],
        quality_hints=quality_hints(config),
        providers=["ytdlp"],
        extra={
            "extractor": info.get("extractor"),
            "extractor_key": info.get("extractor_key"),
            "track": info.get("track"),
            "album": info.get("album"),
            "artists": info.get("artists"),
            "save_count": _int_or_none(info.get("save_count")),
            "format_count": len(formats),
            "audio_only": audio_only,
            "extracted_at": time.time(),
        },
        raw=_trim_info(info),
    )
    if audio_only:
        metadata.warnings.append(
            "only an audio track was found: TikTok photo/slideshow posts are not "
            "supported by yt-dlp (it reads the soundtrack, not the images)"
        )
    for entry in metadata.items:
        entry.size_bytes = entry.size_for("best", **metadata.spec_kwargs())
        if entry.size_bytes is None:
            entry.size_bytes = entry.total_size_bytes
    return metadata.rebuild_groups()


def playlist_to_metadata(
    info: Mapping[str, Any],
    config: TikTokConfig,
    *,
    requested_url: Optional[str] = None,
    ref: Optional[TikTokRef] = None,
) -> PostMetadata:
    """Turn a (flat) profile/sound/tag/collection info dict into metadata."""
    entries = [entry for entry in (info.get("entries") or []) if isinstance(entry, Mapping)]
    items: list[MediaItem] = []
    for position, entry in enumerate(entries[: max(1, config.playlist_max_items)]):
        formats = normalize_formats(entry.get("formats") or [], config)
        thumbnails = thumbnails_from_info(entry.get("thumbnails") or [])
        href = entry.get("webpage_url") or entry.get("url") or entry.get("id")
        if formats and any(fmt.has_video for fmt in formats):
            best = max(formats, key=lambda fmt: (fmt.quality_height or 0, fmt.size_bytes or 0))
            items.append(
                MediaItem(
                    index=position,
                    id=str(entry.get("id") or ""),
                    kind=MediaKind.VIDEO,
                    caption=entry.get("title"),
                    source_url=href,
                    extension=best.extension or "mp4",
                    mime_type=best.mime_type,
                    width=best.width,
                    height=best.height,
                    duration=_float_or_none(entry.get("duration")),
                    has_audio=any(fmt.has_audio for fmt in formats),
                    formats=formats,
                    meta={"thumbnail": _best_thumbnail(thumbnails)},
                )
            )
            continue
        items.append(
            MediaItem(
                index=position,
                id=str(entry.get("id") or ""),
                kind=MediaKind.VIDEO,
                caption=entry.get("title"),
                source_url=href,
                duration=_float_or_none(entry.get("duration")),
                formats=formats,
                meta={"thumbnail": _best_thumbnail(thumbnails), "unresolved": not formats},
            )
        )

    metadata = PostMetadata(
        platform=Platform.TIKTOK,
        id=str(info.get("id") or (ref.username if ref else "") or ""),
        url=info.get("webpage_url") or requested_url,
        requested_url=requested_url,
        permalink=info.get("webpage_url"),
        title=info.get("title") or _list_title(ref),
        description=info.get("description"),
        author=info.get("uploader") or info.get("channel") or (ref.username if ref else None),
        author_id=str(info.get("uploader_id") or "") or None,
        author_url=info.get("uploader_url"),
        thumbnail=info.get("thumbnail"),
        media_type=MediaKind.GALLERY,
        media_group_type=MediaGroupType.PLAYLIST,
        items=items,
        is_playlist=True,
        quality_hints=quality_hints(config),
        providers=["ytdlp"],
        extra={
            "extractor": info.get("extractor"),
            "entry_count": len(entries),
            "list_kind": ref.kind if ref else None,
            "extracted_at": time.time(),
        },
        raw={},
    )
    if any(item.meta.get("unresolved") for item in items):
        metadata.warnings.append(
            "some entries were listed without their formats; open one to resolve it"
        )
    if config.playlist_max_items and len(entries) > config.playlist_max_items:
        metadata.warnings.append(
            f"only the first {config.playlist_max_items} entries were resolved"
        )
    return metadata.rebuild_groups()


# ---------------------------------------------------------------- helpers
def quality_hints(config: TikTokConfig) -> dict[str, Any]:
    """Defaults stored on the metadata so ``links()`` matches ``download()``."""
    return {
        "container": config.prefer_container,
        "codec": config.prefer_video_codec,
        "audio_codec": config.prefer_audio_codec,
        "prefer_muxed": True,
    }


def thumbnails_from_info(entries: Sequence[Mapping[str, Any]]) -> list[Thumbnail]:
    """Normalise yt-dlp thumbnail entries (they may use ``//`` scheme-relative urls)."""
    out: list[Thumbnail] = []
    for entry in entries or ():
        url = entry.get("url")
        if not url:
            continue
        url = str(url)
        if url.startswith("//"):
            url = f"https:{url}"
        out.append(
            Thumbnail(
                url=url,
                width=_int_or_none(entry.get("width")),
                height=_int_or_none(entry.get("height")),
                extension=_extension_of(url),
            )
        )
    return out


def _best_thumbnail(thumbnails: Sequence[Thumbnail]) -> Optional[str]:
    if not thumbnails:
        return None
    best = max(
        thumbnails,
        key=lambda t: (t.pixels, 1 if (t.extension or "") in ("jpg", "jpeg", "webp") else 0),
    )
    return best.url


def _title(info: Mapping[str, Any]) -> Optional[str]:
    text = str(info.get("description") or info.get("title") or "").strip()
    if not text:
        return None
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return first[:120] or None


def _list_title(ref: Optional[TikTokRef]) -> Optional[str]:
    if ref is None:
        return None
    if ref.kind == "profile":
        return f"@{ref.username}"
    if ref.kind == "tag":
        return f"#{ref.tag}"
    return ref.kind


def _extension_of(url: str) -> Optional[str]:
    path = urlparse(url).path
    if "." not in path:
        return None
    return path.rsplit(".", 1)[-1].lower()[:4] or None


def _int_or_none(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _trim_info(info: Mapping[str, Any], *, keep: int = 120) -> dict[str, Any]:
    """A json-serialisable subset of the yt-dlp info (formats are excluded)."""
    drop = {
        "formats",
        "thumbnails",
        "automatic_captions",
        "subtitles",
        "heatmap",
        "requested_formats",
        "requested_subtitles",
        "entries",
    }
    out: dict[str, Any] = {}
    for key, value in info.items():
        if key in drop:
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            out[key] = value
        elif isinstance(value, list) and len(value) <= keep and all(
            isinstance(item, (str, int, float)) for item in value
        ):
            out[key] = value
        elif isinstance(value, dict) and all(
            isinstance(item, (str, int, float, bool, type(None))) for item in value.values()
        ):
            out[key] = value
    return out


__all__ = [
    "format_from_info",
    "info_to_metadata",
    "normalize_formats",
    "playlist_to_metadata",
    "quality_hints",
    "thumbnails_from_info",
]