"""Translate raw reddit post payloads into :class:`MediaItem` objects.

This module is the "brain" of the SDK: it knows how reddit exposes images,
gifs, hosted videos, galleries and external links, and how to turn each of
them into concrete, downloadable CDN urls.
"""

from __future__ import annotations

import asyncio
from typing import Any, Iterable, Optional, Sequence
from urllib.parse import urlparse

from .config import RedditConfig
from .exceptions import MediaNotAvailableError, RedditError
from .http import HttpClient
from .manifests import parse_hls_master, parse_mpd
from .models import (
    FormatKind,
    FormatOrigin,
    MediaFormat,
    MediaGroupType,
    MediaItem,
    MediaKind,
    ProgressEvent,
    ProgressPhase,
    VideoInfo,
)
from .models.progress import emit
from .urls import media_extension, strip_query

IMAGE_EXTS = {"jpg", "jpeg", "png", "webp", "bmp", "tiff", "avif"}
VIDEO_EXTS = {"mp4", "webm", "mov", "mkv", "m4v"}
GIF_EXTS = {"gif", "gifv", "apng"}
MIME_TO_EXT = {
    "image/jpg": "jpg",
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/apng": "apng",
    "video/mp4": "mp4",
    "video/webm": "webm",
    "video/quicktime": "mov",
    "audio/mp4": "m4a",
    "audio/mpeg": "mp3",
}
MIME_TO_KIND = {
    "image/jpg": FormatKind.IMAGE,
    "image/jpeg": FormatKind.IMAGE,
    "image/png": FormatKind.IMAGE,
    "image/webp": FormatKind.IMAGE,
    "image/gif": FormatKind.GIF,
    "video/mp4": FormatKind.VIDEO,
    "video/webm": FormatKind.VIDEO,
    "audio/mp4": FormatKind.AUDIO,
    "audio/mpeg": FormatKind.AUDIO,
}
VIDEO_LADDER = (144, 240, 360, 480, 720, 1080, 1440, 2160)
VIDEO_PATTERNS = ("CMAF_{height}.mp4", "DASH_{height}.mp4", "HLS_{height}.mp4", "DASH_{height}.webm")
AUDIO_PATTERNS = ("CMAF_AUDIO_{bitrate}.mp4", "DASH_AUDIO_{bitrate}.mp4", "audio")
AUDIO_BITRATES = (64, 128)
EXTERNAL_VIDEO_HOSTS = ("youtube.com", "youtu.be", "vimeo.com", "dailymotion.com", "streamable.com", "twitch.tv")


class PostView:
    """Post level facts derived from a reddit payload."""

    __slots__ = ("media_type", "media_group_type", "video", "external_url", "crosspost_id")

    def __init__(
        self,
        media_type: MediaKind,
        media_group_type: MediaGroupType,
        video: Optional[VideoInfo] = None,
        external_url: Optional[str] = None,
        crosspost_id: Optional[str] = None,
    ) -> None:
        self.media_type = media_type
        self.media_group_type = media_group_type
        self.video = video
        self.external_url = external_url
        self.crosspost_id = crosspost_id


# --------------------------------------------------------------------- helpers
def kind_from_url(url: str) -> MediaKind:
    """Guess the media kind of a direct url."""
    ext = (media_extension(url) or "").lower()
    if ext in GIF_EXTS:
        return MediaKind.GIF
    if ext in VIDEO_EXTS:
        return MediaKind.VIDEO
    if ext in IMAGE_EXTS:
        return MediaKind.IMAGE
    host = (urlparse(url).netloc or "").lower()
    if host.endswith("v.redd.it") or host.endswith("packaged-media.redd.it"):
        return MediaKind.VIDEO
    if any(h in host for h in EXTERNAL_VIDEO_HOSTS):
        return MediaKind.EXTERNAL_VIDEO
    if host.endswith("redd.it"):
        return MediaKind.IMAGE
    return MediaKind.UNKNOWN


def format_kind_for(media_kind: MediaKind) -> FormatKind:
    mapping = {
        MediaKind.IMAGE: FormatKind.IMAGE,
        MediaKind.EXTERNAL_IMAGE: FormatKind.IMAGE,
        MediaKind.GIF: FormatKind.GIF,
        MediaKind.VIDEO: FormatKind.VIDEO,
        MediaKind.EXTERNAL_VIDEO: FormatKind.VIDEO,
        MediaKind.AUDIO: FormatKind.AUDIO,
    }
    return mapping.get(media_kind, FormatKind.IMAGE)


def extension_for_mime(mime: Optional[str], fallback: Optional[str] = None) -> Optional[str]:
    if mime:
        key = mime.split(";")[0].strip().lower()
        if key in MIME_TO_EXT:
            return MIME_TO_EXT[key]
    return fallback


def _preview_formats(preview: Optional[dict], source_url: Optional[str]) -> list[MediaFormat]:
    """Build ``preview.redd.it`` renditions out of ``preview.images``."""
    formats: list[MediaFormat] = []
    if not preview:
        return formats
    for index, image in enumerate(preview.get("images") or []):
        source = image.get("source") or {}
        url = source.get("url")
        if url and url != source_url:
            formats.append(
                MediaFormat(
                    format_id=f"preview-source-{index}",
                    url=url,
                    kind=FormatKind.IMAGE,
                    origin=FormatOrigin.PREVIEW,
                    container="jpg",
                    mime_type="image/jpeg",
                    extension="jpg",
                    width=source.get("width"),
                    height=source.get("height"),
                    quality_label="preview-source",
                    note="reddit preview rendition (re-encoded)",
                )
            )
        for rendition in image.get("resolutions") or []:
            url = rendition.get("url")
            if not url:
                continue
            width = rendition.get("width")
            formats.append(
                MediaFormat(
                    format_id=f"preview-{index}-{width}",
                    url=url,
                    kind=FormatKind.IMAGE,
                    origin=FormatOrigin.PREVIEW,
                    container="jpg",
                    mime_type="image/jpeg",
                    extension="jpg",
                    width=width,
                    height=rendition.get("height"),
                    quality_label=f"{width}px",
                    note="reddit preview rendition (re-cropped)",
                )
            )
    return formats


# ------------------------------------------------------------------- builders
def build_items(
    data: dict[str, Any],
    *,
    include_previews: bool = False,
    video_base: Optional[str] = None,
) -> list[MediaItem]:
    """Build the media items of a post payload (no network access)."""
    if data.get("is_gallery") and data.get("media_metadata"):
        return _gallery_items(data, include_previews=include_previews)
    if _has_reddit_video(data):
        item = _video_item(data, video_base=video_base, include_previews=include_previews)
        return [item] if item else []
    if _has_video_preview(data):
        item = _video_preview_item(data)
        return [item] if item else []

    url = _media_url(data)
    if url:
        kind = kind_from_url(url)
        reddit_host = (urlparse(url).netloc or "").lower().endswith("redd.it")
        if kind in (MediaKind.IMAGE, MediaKind.GIF, MediaKind.VIDEO) and reddit_host:
            return [_direct_media_item(data, url, kind, include_previews=include_previews)]
        if kind == MediaKind.IMAGE:
            return [_external_image_item(data, url)]
        if kind == MediaKind.GIF:
            item = _external_image_item(data, url)
            item.kind = MediaKind.GIF
            return [item]
        if kind == MediaKind.VIDEO:
            return [_external_video_item(data, url)]
        if kind == MediaKind.EXTERNAL_VIDEO:
            return [_external_video_item(data, url)]
        if media_extension(url):
            return [_external_image_item(data, url)]

    # Nothing but a reddit rendition (oEmbed style payloads). Hand it over, but
    # label it so nobody mistakes a thumbnail for the original.
    preview = (((data.get("preview") or {}).get("images") or [{}])[0].get("source") or {})
    preview_url = preview.get("url")
    if preview_url and (include_previews or data.get("_sdk_partial")):
        return [_preview_only_item(data, preview_url, preview)]
    return []


def _preview_only_item(data: dict[str, Any], url: str, preview: dict[str, Any]) -> MediaItem:
    note = "reddit preview/thumbnail only - the original media url is unknown"
    return MediaItem(
        index=0,
        id=data.get("id"),
        kind=MediaKind.IMAGE,
        caption=data.get("title"),
        source_url=url,
        extension=media_extension(url),
        width=preview.get("width"),
        height=preview.get("height"),
        formats=[
            MediaFormat(
                format_id="thumbnail",
                url=url,
                kind=FormatKind.IMAGE,
                origin=FormatOrigin.PREVIEW,
                container=media_extension(url),
                extension=media_extension(url),
                width=preview.get("width"),
                height=preview.get("height"),
                quality_label="thumbnail",
                note=note,
            )
        ],
        meta={"note": note, "partial": True},
    ).merged()


def post_view(data: dict[str, Any], items: Sequence[MediaItem]) -> PostView:
    """Derive post level media facts from a payload plus its items."""
    video_info: Optional[VideoInfo] = None
    reddit_video = _reddit_video(data)
    if reddit_video:
        video_info = VideoInfo(
            base_url=_video_base_url(data),
            fallback_url=reddit_video.get("fallback_url"),
            dash_url=reddit_video.get("dash_url"),
            hls_url=reddit_video.get("hls_url"),
            duration=reddit_video.get("duration"),
            width=reddit_video.get("width"),
            height=reddit_video.get("height"),
            has_audio=reddit_video.get("has_audio"),
            is_gif=reddit_video.get("is_gif"),
            bitrate_kbps=reddit_video.get("bitrate_kbps"),
        )

    crosspost_id = data.get("crosspost_parent")
    if crosspost_id and not str(crosspost_id).startswith("t3_"):
        crosspost_id = f"t3_{crosspost_id}"

    external_url: Optional[str] = None
    url = _media_url(data)
    if url and not data.get("is_self"):
        kind = kind_from_url(url)
        if kind in (MediaKind.EXTERNAL_VIDEO, MediaKind.EXTERNAL_IMAGE):
            external_url = url

    kinds = {item.kind for item in items}
    if data.get("is_gallery") or len(items) > 1:
        media_type = MediaKind.GALLERY
        group_type = MediaGroupType.GALLERY
    elif MediaKind.VIDEO in kinds:
        media_type, group_type = MediaKind.VIDEO, MediaGroupType.SINGLE
    elif MediaKind.GIF in kinds:
        media_type, group_type = MediaKind.GIF, MediaGroupType.SINGLE
    elif MediaKind.IMAGE in kinds:
        media_type, group_type = MediaKind.IMAGE, MediaGroupType.SINGLE
    elif MediaKind.EXTERNAL_VIDEO in kinds:
        media_type, group_type = MediaKind.EXTERNAL_VIDEO, MediaGroupType.EXTERNAL
    elif MediaKind.EXTERNAL_IMAGE in kinds:
        media_type, group_type = MediaKind.EXTERNAL_IMAGE, MediaGroupType.EXTERNAL
    elif data.get("poll_data"):
        media_type, group_type = MediaKind.POLL, MediaGroupType.NONE
    elif data.get("is_self"):
        media_type, group_type = MediaKind.TEXT, MediaGroupType.NONE
    elif url:
        media_type, group_type = MediaKind.LINK, MediaGroupType.NONE
    else:
        media_type, group_type = MediaKind.UNKNOWN, MediaGroupType.NONE

    if crosspost_id and group_type in (MediaGroupType.NONE, MediaGroupType.SINGLE):
        group_type = MediaGroupType.CROSSPOST

    return PostView(
        media_type=media_type,
        media_group_type=group_type,
        video=video_info,
        external_url=external_url,
        crosspost_id=crosspost_id,
    )


def _media_url(data: dict[str, Any]) -> Optional[str]:
    return (
        data.get("url_overridden_by_dest")
        or data.get("url")
        or (data.get("secure_media") or {}).get("reddit_video", {}).get("fallback_url")
    )


def _reddit_video(data: dict[str, Any]) -> Optional[dict]:
    for key in ("secure_media", "media"):
        media = data.get(key) or {}
        video = media.get("reddit_video")
        if video:
            return video
    return None


def _has_reddit_video(data: dict[str, Any]) -> bool:
    return _reddit_video(data) is not None


def _has_video_preview(data: dict[str, Any]) -> bool:
    return bool((data.get("preview") or {}).get("reddit_video_preview"))


def _video_base_url(data: dict[str, Any]) -> Optional[str]:
    """``https://v.redd.it/<id>`` for hosted videos."""
    video = _reddit_video(data) or (data.get("preview") or {}).get("reddit_video_preview") or {}
    url = video.get("fallback_url") or data.get("url_overridden_by_dest") or data.get("url") or ""
    if "v.redd.it" in url or "packaged-media.redd.it" in url:
        base = strip_query(url)
        if "/" in base.rsplit("redd.it", 1)[-1]:
            base = base.rsplit("/", 1)[0]
        return base
    return None


def _direct_media_item(
    data: dict[str, Any],
    url: str,
    kind: MediaKind,
    *,
    include_previews: bool,
) -> MediaItem:
    extension = media_extension(url)
    preview_source = (((data.get("preview") or {}).get("images") or [{}])[0].get("source") or {})
    formats = [
        MediaFormat(
            format_id="original",
            url=url,
            kind=format_kind_for(kind),
            origin=FormatOrigin.DIRECT,
            container=extension,
            mime_type=None,
            extension=extension,
            width=preview_source.get("width"),
            height=preview_source.get("height"),
            quality_label="original",
            has_audio=True if kind == MediaKind.VIDEO else None,
        )
    ]
    item = MediaItem(
        index=0,
        id=data.get("id"),
        kind=kind,
        caption=(data.get("title") or None),
        source_url=url,
        mime_type=None,
        extension=extension,
        width=preview_source.get("width"),
        height=preview_source.get("height"),
        duration=None,
        has_audio=None,
        formats=formats,
    )
    if include_previews:
        item.add_formats(_preview_formats(data.get("preview"), url))
    return item.merged()


def _external_image_item(data: dict[str, Any], url: str) -> MediaItem:
    extension = media_extension(url)
    host = urlparse(url).netloc.lower()
    item = MediaItem(
        index=0,
        id=data.get("id"),
        kind=MediaKind.EXTERNAL_IMAGE,
        caption=data.get("title"),
        source_url=url,
        extension=extension,
        formats=[
            MediaFormat(
                format_id="external",
                url=url,
                kind=FormatKind.IMAGE,
                origin=FormatOrigin.EXTERNAL,
                container=extension,
                extension=extension,
                quality_label="original",
                note=f"hosted on {host}",
            )
        ],
    )
    return item.merged()


def _external_video_item(data: dict[str, Any], url: str) -> MediaItem:
    """External video: downloadable when the url is a direct file.

    Streaming pages (youtube, vimeo, ...) carry no formats on purpose - the SDK
    reports them in ``metadata.external_url`` instead of pretending to fetch
    them, which keeps the API honest. Use ``yt-dlp`` for those.
    """
    extension = media_extension(url)
    formats: list[MediaFormat] = []
    direct_file = (extension or "") in VIDEO_EXTS
    if direct_file:
        formats.append(
            MediaFormat(
                format_id="external",
                url=url,
                kind=FormatKind.MUXED,
                origin=FormatOrigin.EXTERNAL,
                container=extension,
                extension=extension,
                quality_label="original",
                has_audio=None,
                note=f"direct file on {urlparse(url).netloc}",
            )
        )
    return MediaItem(
        index=0,
        id=data.get("id"),
        kind=MediaKind.EXTERNAL_VIDEO,
        caption=data.get("title"),
        source_url=url,
        extension=extension,
        formats=formats,
        meta={"external_url": url, "downloadable": direct_file},
    )


def _video_preview_item(data: dict[str, Any]) -> Optional[MediaItem]:
    preview = (data.get("preview") or {}).get("reddit_video_preview") or {}
    url = preview.get("fallback_url")
    if not url:
        return None
    return MediaItem(
        index=0,
        id=data.get("id"),
        kind=MediaKind.VIDEO,
        caption=data.get("title"),
        source_url=url,
        extension="mp4",
        has_audio=preview.get("has_audio"),
        duration=preview.get("duration"),
        width=preview.get("width"),
        height=preview.get("height"),
        formats=[
            MediaFormat(
                format_id="video-preview",
                url=url,
                kind=FormatKind.VIDEO,
                origin=FormatOrigin.PREVIEW,
                container="mp4",
                mime_type="video/mp4",
                extension="mp4",
                width=preview.get("width"),
                height=preview.get("height"),
                bitrate_kbps=preview.get("bitrate_kbps"),
                has_audio=preview.get("has_audio"),
                quality_label="preview",
            )
        ],
    ).merged()


def _video_item(
    data: dict[str, Any],
    *,
    video_base: Optional[str],
    include_previews: bool,
) -> Optional[MediaItem]:
    video = _reddit_video(data) or {}
    base = video_base or _video_base_url(data)
    fallback = video.get("fallback_url")
    formats: list[MediaFormat] = []
    if fallback:
        height = _height_from_name(fallback) or video.get("height")
        formats.append(
            MediaFormat(
                format_id=f"direct-{height or 'video'}",
                url=fallback,
                kind=FormatKind.VIDEO,
                origin=FormatOrigin.DIRECT,
                container="mp4",
                mime_type="video/mp4",
                extension="mp4",
                width=video.get("width"),
                height=video.get("height"),
                quality_height=height,
                bitrate_kbps=video.get("bitrate_kbps"),
                has_audio=video.get("has_audio"),
                quality_label=f"{height}p" if height else "fallback",
                note="fallback url from the reddit payload",
            )
        )
    item = MediaItem(
        index=0,
        id=(base or "").rsplit("/", 1)[-1] or None,
        kind=MediaKind.VIDEO,
        caption=data.get("title"),
        source_url=base or fallback,
        extension="mp4",
        mime_type="video/mp4",
        width=video.get("width"),
        height=video.get("height"),
        duration=video.get("duration"),
        has_audio=video.get("has_audio"),
        formats=formats,
        meta={
            "base_url": base,
            "dash_url": video.get("dash_url"),
            "hls_url": video.get("hls_url"),
            "is_gif": video.get("is_gif"),
            "legacy": "DASH_" in (fallback or ""),
        },
    )
    if include_previews:
        item.add_formats(_preview_formats(data.get("preview"), base))
    return item.merged()


def _gallery_items(data: dict[str, Any], *, include_previews: bool) -> list[MediaItem]:
    metadata: dict[str, Any] = data.get("media_metadata") or {}
    gallery_data = data.get("gallery_data") or {}
    ordered: list[str] = [entry.get("media_id") for entry in gallery_data.get("items") or []]
    ordered = [media_id for media_id in ordered if media_id]
    if not ordered:
        ordered = list(metadata.keys())

    items: list[MediaItem] = []
    for index, media_id in enumerate(ordered):
        entry = metadata.get(media_id)
        if not entry:
            continue
        status = entry.get("status")
        if status and status != "valid":
            items.append(
                MediaItem(
                    index=index,
                    id=media_id,
                    kind=MediaKind.UNKNOWN,
                    meta={"status": status, "note": "gallery entry is not available"},
                )
            )
            continue
        mime = entry.get("m")
        kind = _gallery_kind(entry)
        extension = extension_for_mime(mime, "jpg")
        source = entry.get("s") or {}
        formats: list[MediaFormat] = []

        original = _gallery_original_url(media_id, mime, source)
        if original:
            formats.append(
                MediaFormat(
                    format_id="original",
                    url=original,
                    kind=format_kind_for(kind),
                    origin=FormatOrigin.DERIVED,
                    container=extension,
                    mime_type=mime,
                    extension=extension,
                    width=source.get("x"),
                    height=source.get("y"),
                    quality_label="original",
                    has_audio=True if kind == MediaKind.VIDEO else None,
                    note="derived from the gallery media id",
                )
            )
        if source.get("u") and source.get("u") != original:
            formats.append(
                MediaFormat(
                    format_id="source",
                    url=source["u"],
                    kind=FormatKind.IMAGE,
                    origin=FormatOrigin.PREVIEW,
                    container=extension_for_mime("image/jpg", "jpg"),
                    mime_type="image/jpeg",
                    extension="jpg",
                    width=source.get("x"),
                    height=source.get("y"),
                    quality_label="source-preview",
                    note="reddit source rendition",
                )
            )
        if source.get("mp4"):
            formats.append(
                MediaFormat(
                    format_id="source-mp4",
                    url=source["mp4"],
                    kind=FormatKind.MUXED,
                    origin=FormatOrigin.DIRECT,
                    container="mp4",
                    mime_type="video/mp4",
                    extension="mp4",
                    width=source.get("x"),
                    height=source.get("y"),
                    quality_label="mp4",
                    has_audio=None,
                    note="animated rendition provided by reddit",
                )
            )
        if source.get("gif") and kind == MediaKind.GIF:
            formats.append(
                MediaFormat(
                    format_id="source-gif",
                    url=source["gif"],
                    kind=FormatKind.GIF,
                    origin=FormatOrigin.DIRECT,
                    container="gif",
                    mime_type="image/gif",
                    extension="gif",
                    width=source.get("x"),
                    height=source.get("y"),
                    quality_label="gif",
                )
            )
        if include_previews:
            formats.extend(_preview_formats({"images": [{"resolutions": entry.get("p")}]}, original))

        item = MediaItem(
            index=index,
            id=media_id,
            kind=kind,
            caption=entry.get("caption"),
            source_url=original or source.get("u"),
            mime_type=mime,
            extension=extension,
            width=source.get("x"),
            height=source.get("y"),
            has_audio=None,
            formats=formats,
            meta={"outbound_url": entry.get("outbound_url")} if entry.get("outbound_url") else {},
        )
        items.append(item.merged())
    return items


def _gallery_kind(entry: dict[str, Any]) -> MediaKind:
    kind_label = (entry.get("e") or "").lower()
    mime = (entry.get("m") or "").lower()
    if kind_label in ("animatedimage",) or mime == "image/gif":
        return MediaKind.GIF
    if kind_label == "video" or mime.startswith("video/"):
        return MediaKind.VIDEO
    if kind_label == "image" or mime.startswith("image/"):
        return MediaKind.IMAGE
    return MediaKind.UNKNOWN


def _gallery_original_url(media_id: str, mime: Optional[str], source: dict[str, Any]) -> Optional[str]:
    """Gallery originals live at ``i.redd.it/<media_id>.<ext>``."""
    extension = extension_for_mime(mime)
    if extension and extension not in ("mp4",):
        return f"https://i.redd.it/{media_id}.{extension}"
    if mime and mime.startswith("video/"):
        return source.get("mp4") or f"https://i.redd.it/{media_id}.mp4"
    if source.get("u"):
        return source["u"]
    return None


def _height_from_name(url: str) -> Optional[int]:
    name = url.rsplit("/", 1)[-1]
    parts = name.replace(".", "_").split("_")
    for part in parts:
        if part.isdigit() and 100 <= int(part) <= 2160:
            return int(part)
    return None


# ----------------------------------------------------------------- enrichment
async def enrich_items(
    items: Sequence[MediaItem],
    http: HttpClient,
    config: RedditConfig,
    *,
    expand: Optional[bool] = None,
    sizes: Optional[bool] = None,
    progress: Optional[Any] = None,
) -> list[str]:
    """Resolve manifests, probe variants and sizes. Returns warnings."""
    warnings: list[str] = []
    expand = config.expand_formats if expand is None else expand
    sizes = config.probe_sizes if sizes is None else sizes

    if expand:
        results = await asyncio.gather(
            *(_expand_item(item, http, config) for item in items),
            return_exceptions=True,
        )
        for item, result in zip(items, results):
            if isinstance(result, BaseException):
                warnings.append(
                    f"format expansion failed for item {item.index}: "
                    f"{type(result).__name__}: {result}"
                )
    if sizes:
        await emit(progress, ProgressEvent(phase=ProgressPhase.PROBING, message="resolving media sizes"))
        await _resolve_sizes(items, http, config, warnings)
    return warnings


async def _expand_item(
    item: MediaItem,
    http: HttpClient,
    config: RedditConfig,
) -> None:
    try:
        if item.kind == MediaKind.VIDEO and item.meta.get("base_url"):
            await _expand_video(item, http, config, [])
    finally:
        item.merged()
        _finalize_item(item)


async def _expand_video(
    item: MediaItem,
    http: HttpClient,
    config: RedditConfig,
    warnings: list[str],
) -> None:
    base = item.meta["base_url"]
    manifest_url = item.meta.get("dash_url") or f"{base}/DASHPlaylist.mpd"
    got_video = False
    try:
        response = await http.get(strip_query(manifest_url), retries=1)
        if response.ok and response.content:
            representations = parse_mpd(response.text, strip_query(manifest_url))
            await _add_dash_formats(item, representations, http, config)
            got_video = any(f.kind in (FormatKind.VIDEO, FormatKind.MUXED) for f in item.formats)
    except (RedditError, MediaNotAvailableError):
        pass

    if not got_video:
        hls_url = strip_query(item.meta.get("hls_url") or f"{base}/HLSPlaylist.m3u8")
        try:
            response = await http.get(hls_url, retries=1)
            if response.ok and response.content:
                variants = parse_hls_master(response.text, hls_url)
                for variant in variants:
                    ladder = _height_from_name(variant.url) or variant.height
                    item.add_formats(
                        [
                            MediaFormat(
                                format_id=f"hls-{variant.height or variant.bandwidth}",
                                url=variant.url,
                                kind=FormatKind.VIDEO,
                                origin=FormatOrigin.HLS,
                                container="m3u8",
                                mime_type="application/vnd.apple.mpegurl",
                                extension="m3u8",
                                width=variant.width,
                                height=variant.height,
                                quality_height=ladder,
                                fps=variant.frame_rate,
                                bitrate_kbps=(variant.bandwidth // 1000) if variant.bandwidth else None,
                                codecs=variant.codecs,
                                has_audio=bool(variant.audio_group) or None,
                                quality_label=f"{ladder}p" if ladder else "hls",
                                note="HLS rendition (segments)",
                            )
                        ]
                    )
                got_video = bool(variants)
        except (RedditError, MediaNotAvailableError):
            pass

    if not got_video:
        probed = await _probe_variants(base, http, config, item)
        if not probed:
            warnings.append(
                f"no video renditions found on the CDN for item {item.index} ({base})"
            )
    item.merged()
    _finalize_item(item)


async def _add_dash_formats(
    item: MediaItem,
    representations: Iterable[Any],
    http: HttpClient,
    config: RedditConfig,
) -> None:
    for representation in representations:
        is_audio = representation.content_type == "audio"
        kind = FormatKind.AUDIO if is_audio else FormatKind.VIDEO
        ladder = _height_from_name(representation.filename) or representation.height
        kbps = f"{representation.bandwidth // 1000}kbps" if representation.bandwidth else None
        if is_audio:
            label = kbps or "audio"
        else:
            label = f"{ladder}p" if ladder else (kbps or "video")
        item.add_formats(
            [
                MediaFormat(
                    format_id=f"dash-{representation.representation_id or representation.filename}",
                    url=representation.url,
                    kind=kind,
                    origin=FormatOrigin.DASH,
                    container=representation.container,
                    mime_type=representation.mime_type,
                    extension=representation.container or ("m4a" if is_audio else "mp4"),
                    width=representation.width,
                    height=representation.height,
                    quality_height=ladder,
                    fps=representation.frame_rate,
                    bitrate_kbps=(representation.bandwidth // 1000) if representation.bandwidth else None,
                    codecs=representation.codecs,
                    has_audio=False if not is_audio else True,
                    quality_label=label,
                    note="from DASHPlaylist.mpd",
                )
            ]
        )


async def _probe_variants(
    base: str,
    http: HttpClient,
    config: RedditConfig,
    item: MediaItem,
) -> bool:
    """Probe well known ``v.redd.it`` filenames when no manifest is available."""
    found = False
    heights = list(item.meta.get("known_heights") or VIDEO_LADDER)
    guesses: list[tuple[str, FormatKind, Optional[int]]] = []
    for height in heights:
        for pattern in VIDEO_PATTERNS:
            guesses.append((pattern.format(height=height), FormatKind.VIDEO, height))
    for bitrate in AUDIO_BITRATES:
        for pattern in AUDIO_PATTERNS:
            guesses.append(
                (pattern.format(bitrate=bitrate), FormatKind.AUDIO, None)
                if "{bitrate}" in pattern
                else (pattern, FormatKind.AUDIO, None)
            )

    async def probe(name: str, kind: FormatKind, height: Optional[int]) -> Optional[MediaFormat]:
        url = f"{base}/{name}"
        size, mime = await http.head_size(url, use_cache=config.use_cache)
        if size is None:
            return None
        return MediaFormat(
            format_id=f"probe-{name}",
            url=url,
            kind=kind,
            origin=FormatOrigin.PROBE,
            container=name.rsplit(".", 1)[-1],
            mime_type=mime,
            extension=name.rsplit(".", 1)[-1],
            quality_height=height,
            size_bytes=size,
            has_audio=False if kind == FormatKind.VIDEO else True,
            quality_label=f"{height}p" if height else name,
            note="discovered by probing",
        )

    semaphore = asyncio.Semaphore(config.max_http_concurrency)

    async def bounded(name: str, kind: FormatKind, height: Optional[int]) -> Optional[MediaFormat]:
        async with semaphore:
            try:
                return await probe(name, kind, height)
            except RedditError:
                return None

    results = await asyncio.gather(*(bounded(*guess) for guess in guesses))
    for fmt in results:
        if fmt is not None:
            item.add_formats([fmt])
            found = True
    return found


async def _resolve_sizes(
    items: Sequence[MediaItem],
    http: HttpClient,
    config: RedditConfig,
    warnings: list[str],
) -> None:
    jobs: list[tuple[MediaItem, MediaFormat]] = [
        (item, fmt)
        for item in items
        for fmt in item.formats
        if fmt.size_bytes is None and fmt.origin != FormatOrigin.PREVIEW
    ]
    if not jobs:
        for item in items:
            _finalize_item(item)
        return

    semaphore = asyncio.Semaphore(max(2, config.max_http_concurrency))

    async def resolve(item: MediaItem, fmt: MediaFormat) -> None:
        async with semaphore:
            try:
                size, mime = await http.head_size(fmt.url, use_cache=config.use_cache)
            except RedditError:
                return
            if size is not None:
                fmt.size_bytes = size
            if mime and not fmt.mime_type:
                fmt.mime_type = mime
                fmt.extension = fmt.extension or extension_for_mime(mime)

    await asyncio.gather(*(resolve(item, fmt) for item, fmt in jobs))
    for item in items:
        _finalize_item(item)


def _mark_separate_audio(item: MediaItem) -> None:
    """Correct the payload's blanket ``has_audio`` flag on per-track video files.

    Reddit reports ``reddit_video.has_audio`` for the *video* as a whole, but
    serves DASH/CMAF audio as its own file (``CMAF_AUDIO_128.mp4``,
    ``DASH_AUDIO_128.mp4``) while the ``fallback_url`` video track
    (``CMAF_1080.mp4``/``DASH_720.mp4``) carries **no audio at all**. Leaving
    the flag True would make the format look "muxed" and could let a caller
    upload a silent video believing it is complete.
    """
    if not item.audio_formats:
        return
    for fmt in item.formats:
        if fmt.kind is not FormatKind.VIDEO:
            continue
        if fmt.origin not in (FormatOrigin.DIRECT, FormatOrigin.PROBE):
            continue
        name = (fmt.url or "").rsplit("/", 1)[-1].lower()
        if name.startswith(("cmaf_", "dash_")):
            fmt.has_audio = False


def _finalize_item(item: MediaItem) -> None:
    """Derive the item level conveniences (size, best format info)."""
    _mark_separate_audio(item)
    selectable = item.selectable
    if selectable:
        best = max(
            selectable,
            key=lambda f: (f.quality_height or f.height or f.width or 0, f.size_bytes or 0),
        )
        total = sum(f.size_bytes or 0 for f in selectable)
        item.size_bytes = best.size_bytes if best.size_bytes is not None else (total or None)
        if item.width is None:
            item.width = best.width
        if item.height is None:
            item.height = best.height
        if item.extension is None:
            item.extension = best.extension
        if item.mime_type is None:
            item.mime_type = best.mime_type
        if item.has_audio is None and best.kind == FormatKind.MUXED:
            item.has_audio = best.has_audio
    elif item.formats:
        first = item.formats[0]
        item.size_bytes = item.size_bytes or first.size_bytes