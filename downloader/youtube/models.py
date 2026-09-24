"""Conversion of yt-dlp info dicts into the SDK's platform agnostic models."""

from __future__ import annotations

import time
from typing import Any, Optional, Sequence
from urllib.parse import urlparse

from ..core.enums import FormatKind, FormatOrigin, MediaGroupType, MediaKind, Platform
from ..core.models import MediaFormat, MediaItem, PostMetadata, Thumbnail
from .config import YouTubeConfig
from .urls import YouTubeRef

_STORYBOARD_EXT = frozenset({"mhtml", "jpg", "png", "webp"})
_MANIFEST_PROTOCOLS = ("m3u8", "dash", "http_dash_segments", "ism")

_CODEC_FAMILIES = (
    ("avc1", ("avc1", "h264")),
    ("vp9", ("vp9", "vp09")),
    ("av01", ("av01", "av1")),
    ("mp4a", ("mp4a", "aac")),
    ("opus", ("opus",)),
    ("vorbis", ("vorbis",)),
)


def codec_family(codec: Optional[str]) -> str:
    """Collapse a codec string (``avc1.640028``) into its family (``avc1``)."""
    if not codec:
        return ""
    lowered = codec.lower()
    for family, prefixes in _CODEC_FAMILIES:
        if any(lowered.startswith(prefix) for prefix in prefixes):
            return family
    return lowered.split(".")[0]


def is_manifest_protocol(protocol: Optional[str]) -> bool:
    proto = (protocol or "").lower()
    return any(marker in proto for marker in _MANIFEST_PROTOCOLS)


def select_audio_tracks(
    infos: Sequence[dict[str, Any]],
    config: YouTubeConfig,
    *,
    default_language: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Filter the yt-dlp format list down to the audio track(s) wanted.

    Dubbed videos report one audio rendition per language (``140-0`` for Tamil,
    ``140-1`` for Arabic, ...). Keeping all of them would multiply the size of
    the ladder for no benefit, so only the original/default track survives
    unless :attr:`YouTubeConfig.include_all_audio_languages` is set or
    ``audio_languages`` picks specific dubs.
    """
    wanted = {lang.lower() for lang in config.audio_languages}
    groups: dict[str, list[dict[str, Any]]] = {}
    passthrough: list[dict[str, Any]] = []
    for data in infos:
        if not _is_audio_only(data):
            passthrough.append(data)
            continue
        groups.setdefault(_audio_base_id(data), []).append(data)

    chosen: list[dict[str, Any]] = []
    for entries in groups.values():
        if wanted:
            picks = [e for e in entries if _language_matches(e, wanted)]
            chosen.extend(picks or [entries[0]])
        elif config.include_all_audio_languages or len(entries) == 1:
            chosen.extend(entries)
        else:
            chosen.append(_default_audio(entries, default_language))
    return passthrough + chosen


def quality_hints(config: YouTubeConfig) -> dict[str, Any]:
    """Defaults stored on the metadata so ``links()``/``size_bytes`` match ``download()``."""
    return {
        "container": config.prefer_container,
        "codec": config.prefer_video_codec,
        "audio_codec": config.prefer_audio_codec,
        "prefer_muxed": True,
    }


def _is_audio_only(data: dict[str, Any]) -> bool:
    vcodec = data.get("vcodec")
    acodec = data.get("acodec")
    return (not vcodec or vcodec == "none") and bool(acodec) and acodec != "none"


def _audio_base_id(data: dict[str, Any]) -> str:
    format_id = str(data.get("format_id") or "")
    head, separator, tail = format_id.rpartition("-")
    return head if separator and tail.isdigit() else format_id


def _language_matches(entry: dict[str, Any], wanted: set[str]) -> bool:
    language = str(entry.get("language") or "").lower()
    return bool(language) and (language in wanted or language.split("-")[0] in wanted)


def _default_audio(
    entries: Sequence[dict[str, Any]], default_language: Optional[str]
) -> dict[str, Any]:
    target = (default_language or "").lower()

    def score(entry: dict[str, Any]) -> tuple:
        note = str(entry.get("format_note") or "").lower()
        preference = entry.get("language_preference")
        matches_default = 1 if target and _language_matches(entry, {target}) else 0
        return (
            1 if ("default" in note or "original" in note) else 0,
            float(preference) if isinstance(preference, (int, float)) else -100.0,
            matches_default,
        )

    return max(entries, key=score)


def quality_height_of(data: dict[str, Any], config: YouTubeConfig) -> Optional[int]:
    """Rendition label of a yt-dlp format (``1080`` for both 1920x1080 and 1080x1920)."""
    note = str(data.get("format_note") or "")
    digits = ""
    for char in note:
        if char.isdigit():
            digits += char
        elif digits:
            break
    if digits:
        return int(digits)
    height, width = data.get("height"), data.get("width")
    if not height and not width:
        return None
    if config.prefer_short_side and width and height and height > width:
        return int(width)
    return int(height or width)


def thumbnails_from_info(entries: Sequence[dict[str, Any]]) -> list[Thumbnail]:
    """Normalise yt-dlp thumbnail entries (they may use ``//`` scheme-relative urls)."""
    out: list[Thumbnail] = []
    for entry in entries or ():
        url = entry.get("url")
        if not url:
            continue
        if url.startswith("//"):
            url = f"https:{url}"
        out.append(
            Thumbnail(
                url=url,
                width=entry.get("width"),
                height=entry.get("height"),
                extension=_extension_of(url),
            )
        )
    return out


def format_from_info(data: dict[str, Any], config: YouTubeConfig) -> Optional[MediaFormat]:
    """Build a :class:`MediaFormat` from one yt-dlp format dict."""
    format_id = str(data.get("format_id") or "")
    if not format_id:
        return None
    url = data.get("url")
    vcodec = data.get("vcodec")
    acodec = data.get("acodec")
    has_video = bool(vcodec) and vcodec != "none"
    has_audio = bool(acodec) and acodec != "none"
    note = str(data.get("format_note") or "")
    extension = data.get("ext")

    if "storyboard" in note.lower() or format_id.startswith("sb") or extension == "mhtml":
        if not config.include_storyboards:
            return None
        kind = FormatKind.STORYBOARD
        has_video, has_audio = False, False
    elif has_video and has_audio:
        kind = FormatKind.MUXED
    elif has_video:
        kind = FormatKind.VIDEO
    elif has_audio:
        kind = FormatKind.AUDIO
    else:
        return None
    if not url:
        return None

    size = data.get("filesize")
    approx = data.get("filesize_approx")
    size_is_approx = False
    size_source: Optional[str] = None
    if size:
        size_source = "metadata"
    elif approx:
        size, size_is_approx, size_source = int(approx), True, "estimate"

    bitrate = data.get("tbr") or data.get("vbr") or data.get("abr")
    quality_height = quality_height_of(data, config) if has_video else None
    label = _quality_label(data, kind, quality_height)

    return MediaFormat(
        format_id=format_id,
        url=url,
        kind=kind,
        origin=FormatOrigin.YTDLP,
        container=data.get("container") or extension,
        extension=extension,
        mime_type=None,
        protocol=data.get("protocol"),
        width=data.get("width"),
        height=data.get("height"),
        quality_height=quality_height,
        fps=data.get("fps"),
        bitrate_kbps=int(bitrate) if bitrate else None,
        audio_bitrate_kbps=int(data["abr"]) if data.get("abr") else None,
        codecs=", ".join(
            part for part in (vcodec if has_video else None, acodec if has_audio else None) if part
        ) or None,
        video_codec=vcodec if has_video else None,
        audio_codec=acodec if has_audio else None,
        size_bytes=int(size) if size else None,
        size_is_approx=size_is_approx,
        size_source=size_source,
        has_video=has_video if kind != FormatKind.STORYBOARD else None,
        has_audio=has_audio if kind != FormatKind.STORYBOARD else None,
        language=data.get("language"),
        quality_label=label,
        http_headers=dict(data.get("http_headers") or {}),
        meta={
            key: data[key]
            for key in (
                "abr",
                "vbr",
                "tbr",
                "asr",
                "audio_channels",
                "aspect_ratio",
                "dynamic_range",
                "language_preference",
                "source_preference",
                "quality",
            )
            if data.get(key) is not None
        },
    )


def normalize_formats(
    infos: Sequence[dict[str, Any]], config: YouTubeConfig
) -> list[MediaFormat]:
    """Convert every yt-dlp format, dropping the HLS duplicates by default.

    yt-dlp reports most renditions twice: once as a progressive ``https``
    stream (with an exact ``filesize``) and once as an ``m3u8_native``
    manifest (with no size at all). The manifest copy is only dropped when a
    progressive stream of the *same* resolution and codec family exists, so
    manifest-only renditions (live streams, DASH-only videos) survive.
    """
    formats = [fmt for fmt in (format_from_info(d, config) for d in infos) if fmt]
    if config.include_manifest_formats or config.include_storyboards:
        return formats
    direct = [fmt for fmt in formats if not fmt.is_manifest]
    exact = {_duplicate_key(fmt) for fmt in direct}
    coarse = {_resolution_key(fmt) for fmt in direct}
    kept: list[MediaFormat] = []
    for fmt in formats:
        if fmt.kind == FormatKind.STORYBOARD:
            continue
        if fmt.is_manifest and not fmt.is_muxed:
            if _duplicate_key(fmt) in exact or _resolution_key(fmt) in coarse:
                continue
        kept.append(fmt)
    return kept


def subtitles_from_info(
    info: dict[str, Any], config: YouTubeConfig
) -> dict[str, list[MediaFormat]]:
    """Normalise ``subtitles``/``automatic_captions`` into subtitle formats."""
    if not config.include_subtitles:
        return {}
    wanted = {lang.lower() for lang in config.subtitle_languages}
    out: dict[str, list[MediaFormat]] = {}
    sources: list[tuple[str, dict[str, Any]]] = [("manual", info.get("subtitles") or {})]
    if config.include_auto_captions:
        sources.append(("auto", info.get("automatic_captions") or {}))
    for origin, table in sources:
        for language, entries in (table or {}).items():
            if wanted and language.lower() not in wanted and language.split("-")[0].lower() not in wanted:
                continue
            bucket = out.setdefault(language, [])
            for entry in entries or ():
                url = entry.get("url")
                if not url:
                    continue
                extension = entry.get("ext") or "vtt"
                bucket.append(
                    MediaFormat(
                        format_id=f"{language}.{extension}.{origin}",
                        url=url,
                        kind=FormatKind.SUBTITLE,
                        origin=FormatOrigin.YTDLP,
                        extension=extension,
                        mime_type="text/vtt" if extension == "vtt" else None,
                        language=language,
                        quality_label=language,
                        note=entry.get("name"),
                        has_video=False,
                        has_audio=False,
                        meta={"automatic": origin == "auto"},
                    )
                )
    return {lang: fmts for lang, fmts in out.items() if fmts}


def info_to_metadata(
    info: dict[str, Any],
    config: YouTubeConfig,
    *,
    requested_url: Optional[str] = None,
    ref: Optional[YouTubeRef] = None,
) -> PostMetadata:
    """Turn a yt-dlp video/playlist info dict into :class:`PostMetadata`."""
    if info.get("_type") == "playlist" or "entries" in info:
        return playlist_to_metadata(info, config, requested_url=requested_url, ref=ref)

    raw_formats = select_audio_tracks(
        info.get("formats") or [], config, default_language=info.get("language")
    )
    formats = normalize_formats(raw_formats, config)
    subtitles = subtitles_from_info(info, config)
    thumbnails = thumbnails_from_info(info.get("thumbnails") or [])
    best_thumb = _best_thumbnail(thumbnails) or info.get("thumbnail")
    video_formats = [f for f in formats if f.kind in (FormatKind.VIDEO, FormatKind.MUXED)]
    live_status = info.get("live_status")
    is_live = live_status in ("is_live", "is_upcoming", "post_live")

    item = MediaItem(
        index=0,
        id=str(info.get("id") or ""),
        kind=MediaKind.VIDEO,
        caption=info.get("title"),
        source_url=info.get("webpage_url") or requested_url,
        extension=(video_formats[0].extension if video_formats else info.get("ext")),
        width=info.get("width"),
        height=info.get("height"),
        duration=info.get("duration"),
        has_audio=any(f.has_audio for f in video_formats) or bool(
            [f for f in formats if f.kind == FormatKind.AUDIO]
        ),
        size_bytes=None,
        formats=formats,
        meta={
            "definition": info.get("format_note"),
            "live_status": live_status,
            "playable_in_embed": info.get("playable_in_embed"),
            "age_limit": info.get("age_limit"),
            "availability": info.get("availability"),
        },
    )
    item.size_bytes = item.size_for("best") or _best_declared_size(formats)

    metadata = PostMetadata(
        platform=Platform.YOUTUBE,
        id=str(info.get("id") or ""),
        url=info.get("webpage_url") or requested_url,
        requested_url=requested_url,
        permalink=info.get("webpage_url"),
        title=info.get("title"),
        description=info.get("description"),
        author=info.get("uploader") or info.get("channel"),
        author_id=info.get("uploader_id") or info.get("channel_id"),
        author_url=info.get("uploader_url") or info.get("channel_url"),
        channel=info.get("channel"),
        created_utc=float(info["timestamp"]) if info.get("timestamp") else None,
        upload_date=info.get("upload_date"),
        timestamp=info.get("timestamp"),
        duration=info.get("duration"),
        view_count=info.get("view_count"),
        like_count=info.get("like_count"),
        comment_count=info.get("comment_count"),
        language=info.get("language"),
        thumbnail=best_thumb,
        thumbnails=thumbnails,
        media_type=MediaKind.VIDEO,
        media_group_type=MediaGroupType.SINGLE,
        items=[item],
        subtitles=subtitles,
        quality_hints=quality_hints(config),
        is_live=is_live,
        is_short=bool(ref and ref.is_short) or _looks_like_short(info),
        is_age_restricted=bool(info.get("age_limit")),
        providers=["ytdlp"],
        extra={
            "extractor": info.get("extractor"),
            "extractor_key": info.get("extractor_key"),
            "chapters": info.get("chapters") or [],
            "categories": info.get("categories") or [],
            "tags": info.get("tags") or [],
            "thumbnail_count": len(thumbnails),
            "subtitle_count": len(subtitles),
            "format_count": len(formats),
            "extracted_at": time.time(),
            "is_manifest_protocols_dropped": not config.include_manifest_formats,
        },
        raw=_trim_info(info),
    )
    if is_live:
        metadata.warnings.append("this is a live stream: durations/sizes are provisional")
    if metadata.is_short:
        metadata.extra["aspect"] = "vertical"
    return metadata.rebuild_groups()


def playlist_to_metadata(
    info: dict[str, Any],
    config: YouTubeConfig,
    *,
    requested_url: Optional[str] = None,
    ref: Optional[YouTubeRef] = None,
) -> PostMetadata:
    """Turn a (possibly flat) playlist/channel info dict into metadata."""
    entries = [e for e in (info.get("entries") or []) if e]
    items: list[MediaItem] = []
    videos: list[MediaItem] = []
    for position, entry in enumerate(entries):
        url = entry.get("webpage_url") or entry.get("url") or entry.get("id")
        thumbnail = entry.get("thumbnails") or []
        thumb = _best_thumbnail(thumbnails_from_info(thumbnail)) or entry.get("thumbnail")
        item = MediaItem(
            index=position,
            id=str(entry.get("id") or ""),
            kind=MediaKind.VIDEO,
            caption=entry.get("title"),
            source_url=url,
            duration=entry.get("duration"),
            formats=normalize_formats(
                select_audio_tracks(
                    entry.get("formats") or [],
                    config,
                    default_language=entry.get("language"),
                ),
                config,
            ),
            meta={"thumbnail": thumb, "uploader": entry.get("uploader") or entry.get("channel")},
        )
        if item.formats:
            item.size_bytes = item.size_for("best") or _best_declared_size(item.formats)
            videos.append(item)
        items.append(item)

    kind = MediaGroupType.CHANNEL if (ref and ref.kind == "channel") else MediaGroupType.PLAYLIST
    metadata = PostMetadata(
        platform=Platform.YOUTUBE,
        id=str(info.get("id") or ""),
        url=info.get("webpage_url") or requested_url,
        requested_url=requested_url,
        permalink=info.get("webpage_url"),
        title=info.get("title"),
        description=info.get("description"),
        author=info.get("uploader") or info.get("channel"),
        author_id=info.get("uploader_id") or info.get("channel_id"),
        author_url=info.get("uploader_url") or info.get("channel_url"),
        channel=info.get("channel"),
        media_type=MediaKind.GALLERY,
        media_group_type=kind,
        items=items,
        quality_hints=quality_hints(config),
        is_playlist=True,
        providers=["ytdlp"],
        extra={
            "entry_count": len(entries),
            "resolved_videos": len(videos),
            "extractor": info.get("extractor"),
            "extracted_at": time.time(),
        },
        raw=_trim_info(info),
    )
    if len(videos) < len(items):
        metadata.warnings.append(
            "playlist entries were listed flat; call get_metadata(entry.url) to "
            "resolve each video's formats before downloading it"
        )
    return metadata.rebuild_groups()


def _quality_label(data: dict[str, Any], kind: FormatKind, height: Optional[int]) -> Optional[str]:
    if kind == FormatKind.AUDIO:
        abr = data.get("abr")
        return f"audio{int(abr)}" if abr else "audio"
    if kind == FormatKind.STORYBOARD:
        return "storyboard"
    if height:
        label = f"{height}p"
        fps = data.get("fps")
        if fps and fps >= 50:
            label += f"{int(fps)}"
        family = codec_family(data.get("vcodec"))
        return f"{label}_{family}" if family else label
    return None


def _duplicate_key(fmt: MediaFormat) -> tuple:
    family = codec_family(fmt.video_codec or fmt.audio_codec)
    bucket = round((fmt.bitrate_kbps or 0) / 32)
    return (str(fmt.kind), fmt.quality_height or 0, family, bucket)


def _resolution_key(fmt: MediaFormat) -> tuple:
    """Resolution + codec family, ignoring the bitrate.

    HLS copies of a rendition routinely report a different bitrate than the
    progressive stream, so the exact key alone would let them through even
    though they carry no size and are useless for a downloader.
    """
    family = codec_family(fmt.video_codec or fmt.audio_codec)
    return (str(fmt.kind), fmt.quality_height or 0, family)


def _best_thumbnail(thumbnails: Sequence[Thumbnail]) -> Optional[str]:
    if not thumbnails:
        return None
    best = max(thumbnails, key=lambda t: (t.pixels, 1 if (t.extension or "") in ("jpg", "jpeg", "webp") else 0))
    return best.url


def _best_declared_size(formats: Sequence[MediaFormat]) -> Optional[int]:
    known = [f.size_bytes for f in formats if f.size_bytes is not None]
    return max(known) if known else None


def _looks_like_short(info: dict[str, Any]) -> bool:
    width, height = info.get("width"), info.get("height")
    if not width or not height:
        return False
    return height > width and "shorts" in str(info.get("webpage_url") or "")


def _extension_of(url: str) -> Optional[str]:
    path = urlparse(url).path
    if "." not in path:
        return None
    return path.rsplit(".", 1)[-1].lower()[:5] or None


def _trim_info(info: dict[str, Any], *, keep: int = 120) -> dict[str, Any]:
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
            isinstance(v, (str, int, float)) for v in value
        ):
            out[key] = value
        elif isinstance(value, dict) and all(
            isinstance(v, (str, int, float, bool, type(None))) for v in value.values()
        ):
            out[key] = value
    return out