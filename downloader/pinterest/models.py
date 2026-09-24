"""Conversion of Pinterest payloads into the SDK's media models.

Two payload shapes feed this module:

* the **pin** object from ``PinResource/get/`` (images, title, author, stats,
  the story pin page structure and the video variant list), and
* the **yt-dlp** info dict, which is used for the video quality ladder.

A note on Pinterest's video variants, because it is not obvious from the
payload: the api reports ``width``/``height`` of ``640x1138`` for *every*
variant of a video, including the ``expMp4`` ones which ffprobe shows are
actually ``540x960``. The reported resolution is therefore useless for ranking
and only yt-dlp's ladder (which resolves the HLS renditions properly) is
trustworthy. What this module does with the api list is deliberately modest:
it picks the single best progressive rendition as a *fallback* so a pin is
still downloadable when yt-dlp fails, and lets
:func:`attach_video_formats` replace it with the real ladder.
"""

from __future__ import annotations

import time
from email.utils import parsedate_to_datetime
from typing import Any, Mapping, Optional, Sequence
from urllib.parse import urlparse

from ..core.enums import FormatKind, FormatOrigin, MediaGroupType, MediaKind, Platform
from ..core.models import MediaFormat, MediaItem, PostMetadata, Thumbnail
from .config import PinterestConfig
from .urls import PinterestRef

#: ``i.pinimg.com`` renditions, best first. ``600x315`` is a wide crop used for
#: video previews and ``60x60`` is an avatar-sized square, so neither is a real
#: rendition of the image and both are dropped.
RENDITION_ORDER = ("orig", "originals", "736x", "750x", "564x", "474x", "236x", "170x", "136x")
_SKIP_RENDITIONS = frozenset({"60x60", "136x136", "600x315"})

_EXTENSION_MIME = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
}


# --------------------------------------------------------------- images
def image_formats(images: Mapping[str, Any], config: PinterestConfig) -> list[MediaFormat]:
    """The ``i.pinimg.com`` rendition ladder for one image payload.

    Accepts both key styles Pinterest uses: the pin level ``images`` dict
    (``orig``, ``736x``, ...) and a story pin page's ``image['images']``
    (``originals``, ``750x``, ...).
    """
    candidates: dict[str, tuple[str, Optional[int], Optional[int]]] = {}
    for name, entry in (images or {}).items():
        if not isinstance(entry, dict):
            continue
        url = entry.get("url")
        if not url:
            continue
        key = str(name)
        if key in _SKIP_RENDITIONS:
            continue
        # first name wins per url so ``orig`` beats a duplicate ``originals``
        candidates.setdefault(str(url), (key, entry.get("width"), entry.get("height")))

    if not candidates:
        return []

    ordered = sorted(
        candidates.items(),
        key=lambda item: (
            _rendition_rank(item[1][0]),
            item[1][2] or 0,
            item[1][1] or 0,
        ),
    )
    preferred = (config.photo_quality or "orig").lower()
    chosen = ordered[: max(1, config.max_image_candidates)]

    formats: list[MediaFormat] = []
    best_url = ordered[0][0] if ordered else None
    for url, (name, width, height) in chosen:
        is_preferred = name in (preferred, "originals" if preferred == "orig" else preferred)
        if not any(entry[0] in (preferred,) for entry in chosen) and url == best_url:
            is_preferred = True
        extension = _extension_of(url)
        formats.append(
            MediaFormat(
                format_id=f"image-{name}",
                url=url,
                kind=FormatKind.GIF if extension == "gif" else FormatKind.IMAGE,
                origin=FormatOrigin.DIRECT if is_preferred else FormatOrigin.PREVIEW,
                container=extension,
                extension=extension,
                mime_type=_EXTENSION_MIME.get(extension or ""),
                width=width,
                height=height,
                quality_height=height,
                has_video=False,
                has_audio=False,
                quality_label=name,
                size_is_approx=True,
                note="pinterest image rendition",
                meta={"rendition": name},
            )
        )
    if preferred not in ("orig", "originals"):
        # keep the requested rendition ahead of the original without inventing
        # a resolution: nudge the others down by one pixel.
        for fmt in formats:
            if fmt.origin is not FormatOrigin.DIRECT and fmt.quality_height:
                fmt.quality_height = max(1, fmt.quality_height - 1)
    return formats


def image_formats_from_thumbnails(
    thumbnails: Sequence[Mapping[str, Any]], config: PinterestConfig
) -> list[MediaFormat]:
    """Build an image ladder from yt-dlp thumbnails (board feed entries).

    yt-dlp exposes the same ``i.pinimg.com`` renditions as ``thumbnails``; the
    full resolution one is recognisable because its path contains
    ``/originals/``. There the rendition *name* is unknown, so the name is
    derived from the url and the fixups in :func:`image_formats` are repeated
    on the derived mapping.
    """
    images: dict[str, Any] = {}
    for entry in thumbnails or ():
        url = entry.get("url")
        if not url:
            continue
        name = _rendition_of_url(str(url))
        if name in images:
            continue
        images[name] = {
            "url": url,
            "width": entry.get("width"),
            "height": entry.get("height"),
        }
    return image_formats(images, config)


def _rendition_of_url(url: str) -> str:
    path = urlparse(url).path
    for part in path.split("/"):
        if part in ("originals",):
            return "orig"
        if part.endswith("x") and part[:-1].isdigit():
            return part
    return "unknown"


def _extension_of(url: str) -> Optional[str]:
    path = urlparse(url).path
    if "." not in path:
        return None
    return path.rsplit(".", 1)[-1].lower()[:4] or None


def _rendition_rank(name: str) -> int:
    lowered = name.lower()
    if lowered in RENDITION_ORDER:
        return RENDITION_ORDER.index(lowered)
    return len(RENDITION_ORDER)


# --------------------------------------------------------------- videos
def best_variant(video_list: Mapping[str, Any]) -> Optional[tuple[str, dict[str, Any]]]:
    """The best *progressive* variant of a Pinterest ``video_list``.

    ``V_EXP7`` (served from ``/720p/``) is the highest quality rendition
    Pinterest publishes progressively; the ``V_EXP3``..``V_EXP6`` ones are the
    experimental ``expMp4`` ladder and the ``V_HLSV3_MOBILE`` entry is a
    manifest. Progressive wins, then the ``V_EXP`` family, then resolution.
    """
    entries = [
        (str(format_id), entry)
        for format_id, entry in (video_list or {}).items()
        if isinstance(entry, dict) and entry.get("url")
    ]
    if not entries:
        return None
    progressive = [
        entry
        for entry in entries
        if ".m3u8" not in str(entry[1].get("url", "")).lower()
    ]
    pool = progressive or entries

    def score(entry: tuple[str, dict[str, Any]]) -> tuple:
        format_id, data = entry
        url = str(data.get("url") or "").lower()
        return (
            1 if "/720p/" in url else 0,
            1 if format_id.upper().startswith("V_EXP") else 0,
            int(data.get("width") or 0),
            int(data.get("height") or 0),
        )

    return max(pool, key=score)


def api_video_formats(
    data: Mapping[str, Any],
    config: PinterestConfig,
    video_list: Optional[Mapping[str, Any]] = None,
) -> list[MediaFormat]:
    """A single-format fallback ladder for a video pin (see module docstring)."""
    variant = best_variant(_video_list(data) if video_list is None else video_list)
    if variant is None:
        return []
    format_id, entry = variant
    url = str(entry.get("url") or "")
    is_manifest = ".m3u8" in url.lower()
    height = _quality_height(entry)
    duration_ms = _int_or_none(entry.get("duration"))
    return [
        MediaFormat(
            format_id=format_id,
            url=url,
            kind=FormatKind.MUXED,
            origin=FormatOrigin.DIRECT,
            container="mp4",
            extension="mp4",
            mime_type="video/mp4",
            protocol="m3u8_native" if is_manifest else "https",
            width=_int_or_none(entry.get("width")),
            height=_int_or_none(entry.get("height")),
            quality_height=height,
            has_video=True,
            has_audio=True,
            quality_label=f"{height}p" if height else None,
            size_is_approx=True,
            note="pinterest progressive (yt-dlp unavailable)",
            meta={
                "variant": format_id,
                "duration": (duration_ms / 1000) if duration_ms else None,
                "hover_thumbnail": entry.get("thumbnail"),
            },
        )
    ]


def ytdlp_video_formats(info: Mapping[str, Any], config: PinterestConfig) -> list[MediaFormat]:
    """Normalise a yt-dlp Pinterest payload into the SDK's video ladder.

    yt-dlp reports the progressive ``https`` renditions with ``vcodec`` and
    ``acodec`` both ``None`` even though the mp4 really carries h264 video and
    aac audio (verified with ffprobe), so the kind is inferred from the
    protocol/extension instead of the codec fields. The ``m3u8_native``
    renditions do carry codecs and are genuinely muxed.
    """
    formats: list[MediaFormat] = []
    for entry in info.get("formats") or ():
        fmt = _one_ytdlp_format(entry, config)
        if fmt is not None:
            formats.append(fmt)
    return _prune_manifests(formats, config)


def _one_ytdlp_format(
    data: Mapping[str, Any], config: PinterestConfig
) -> Optional[MediaFormat]:
    format_id = str(data.get("format_id") or "")
    url = data.get("url")
    if not format_id or not url:
        return None
    protocol = str(data.get("protocol") or "").lower()
    extension = data.get("ext")
    vcodec = data.get("vcodec")
    acodec = data.get("acodec")
    has_video = bool(vcodec) and vcodec != "none"
    has_audio = bool(acodec) and acodec != "none"
    is_manifest = "m3u8" in protocol or "dash" in protocol

    if is_manifest and not has_audio and not has_video:
        kind, has_video, has_audio = FormatKind.MUXED, True, True
    elif is_manifest and not has_video:
        kind, has_video, has_audio = FormatKind.AUDIO, False, True
    elif has_video and has_audio:
        kind = FormatKind.MUXED
    elif extension == "mp4" and not has_video and not has_audio:
        kind, has_video, has_audio = FormatKind.MUXED, True, True
    elif has_video:
        kind = FormatKind.VIDEO
    elif has_audio:
        kind = FormatKind.AUDIO
    else:
        return None

    height = _quality_height(data)
    width = _int_or_none(data.get("width"))
    bitrate = data.get("tbr") or data.get("vbr")
    return MediaFormat(
        format_id=format_id,
        url=str(url),
        kind=kind,
        origin=FormatOrigin.YTDLP,
        container=data.get("container") or extension,
        extension=extension,
        mime_type="video/mp4" if extension == "mp4" else None,
        protocol=data.get("protocol"),
        width=width,
        height=_int_or_none(data.get("height")),
        quality_height=height if has_video else None,
        fps=data.get("fps"),
        bitrate_kbps=int(bitrate) if bitrate else None,
        codecs=", ".join(part for part in (vcodec, acodec) if part) or None,
        video_codec=vcodec if has_video else None,
        audio_codec=acodec if has_audio else None,
        size_is_approx=True,
        has_video=has_video,
        has_audio=has_audio,
        quality_label=f"{height}p" if height else None,
        http_headers=dict(data.get("http_headers") or {}),
        meta={"ytdlp_tbr": data.get("tbr")},
    )


def _prune_manifests(
    formats: Sequence[MediaFormat], config: PinterestConfig
) -> list[MediaFormat]:
    """Drop HLS renditions that duplicate a progressive one of the same size."""
    if config.include_manifest_formats:
        return list(formats)
    progressive = [f for f in formats if not f.is_manifest]
    heights = {f.quality_height for f in progressive if f.quality_height is not None}
    has_progressive_video = any(f.has_video for f in progressive)
    kept: list[MediaFormat] = []
    for fmt in formats:
        if fmt.is_manifest and has_progressive_video:
            if fmt.quality_height is None or fmt.quality_height in heights:
                continue
        kept.append(fmt)
    return kept


def attach_video_formats(metadata: PostMetadata, ladder: Sequence[MediaFormat]) -> int:
    """Replace the provisional video ladder with the yt-dlp one."""
    if not ladder:
        return 0
    applied = 0
    for item in metadata.items:
        if item.kind not in (MediaKind.VIDEO, MediaKind.GIF) or not item.formats:
            continue
        if not any(fmt.has_video for fmt in ladder):
            continue
        item.formats = list(ladder)
        best = max(
            (f for f in ladder if f.has_video),
            key=lambda f: (f.quality_height or 0, f.bitrate_kbps or 0),
            default=None,
        )
        if best is not None:
            if best.height:
                item.height = best.height
            if best.width:
                item.width = best.width
        item.meta["formats_source"] = "ytdlp"
        applied += 1
    if applied:
        metadata.providers = list(dict.fromkeys(metadata.providers + ["ytdlp"]))
    return applied


# ------------------------------------------------------------- pin payload
def _video_list(data: Mapping[str, Any]) -> dict[str, Any]:
    """Every video variant Pinterest lists for a pin, merged in one mapping."""
    merged: dict[str, Any] = {}
    videos = data.get("videos")
    if isinstance(videos, dict) and isinstance(videos.get("video_list"), dict):
        merged.update(videos["video_list"])
    for page in _pages(data):
        for block in page.get("blocks") or ():
            if not isinstance(block, dict):
                continue
            video = block.get("video")
            if isinstance(video, dict) and isinstance(video.get("video_list"), dict):
                merged.update(video["video_list"])
    return merged


def _pages(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    story = data.get("story_pin_data")
    if not isinstance(story, dict):
        return []
    pages = story.get("pages")
    if not isinstance(pages, list):
        return []
    return [page for page in pages if isinstance(page, dict)]


def _page_images(page: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The per page image payloads of a story pin (there is usually one)."""
    found: list[dict[str, Any]] = []
    for key in ("image_adjusted", "image"):
        entry = page.get(key)
        if isinstance(entry, dict) and isinstance(entry.get("images"), dict):
            found.append(entry["images"])
            break
    for block in page.get("blocks") or ():
        if not isinstance(block, dict):
            continue
        image = block.get("image")
        if isinstance(image, dict) and isinstance(image.get("images"), dict):
            found.append(image["images"])
    return found


def _has_video(data: Mapping[str, Any]) -> bool:
    if data.get("is_video") is True:
        return True
    return bool(_video_list(data))


def pin_items(data: Mapping[str, Any], config: PinterestConfig) -> list[MediaItem]:
    """Every downloadable item of a pin, in payload order.

    A pin is one of three things, and the distinction matters because the
    ``images`` of a *video* pin are nothing but its cover frames:

    * a multi page idea (story) pin - one item per page, and
    * a video pin, or
    * an image pin.

    Only the story branch can mix kinds; the other two are exclusive.
    """
    items: list[MediaItem] = []
    pages = _pages(data)
    if len(pages) > 1 and config.include_story_pages:
        for page in pages[: max(1, config.max_story_pages)]:
            items.extend(_page_items(page, data, config))
    elif config.include_videos and _has_video(data):
        items.extend(_video_items(data, config))
    elif config.include_images:
        items.extend(_image_items(data, config))
    for position, item in enumerate(items):
        item.index = position
    return items


def pin_to_metadata(
    data: Mapping[str, Any],
    config: PinterestConfig,
    *,
    ref: Optional[PinterestRef] = None,
    requested_url: Optional[str] = None,
) -> PostMetadata:
    """Turn a ``PinResource`` payload into :class:`PostMetadata`."""
    pin_id = str(data.get("id") or (ref.pin_id if ref else "") or "")
    attribution = data.get("closeup_attribution")
    attribution = attribution if isinstance(attribution, dict) else {}
    thumbnails = _thumbnails(data)
    items = pin_items(data, config)

    media_kind, group = _group_of(items)
    best_thumb = _best_thumbnail(thumbnails)
    pin_join = data.get("pin_join")
    pin_join = pin_join if isinstance(pin_join, dict) else {}
    hashtags = [str(tag) for tag in (data.get("hashtags") or []) if tag]
    created = _timestamp_of(data.get("created_at"))

    metadata = PostMetadata(
        platform=Platform.PINTEREST,
        id=pin_id,
        url=f"https://www.pinterest.com/pin/{pin_id}/" if pin_id else requested_url,
        requested_url=requested_url,
        permalink=requested_url,
        title=_title(data),
        description=_description(data),
        author=attribution.get("full_name") or attribution.get("username"),
        author_id=attribution.get("username"),
        author_url=(
            f"https://www.pinterest.com/{attribution['username']}/"
            if attribution.get("username")
            else None
        ),
        created_utc=created,
        timestamp=int(created) if created else None,
        repost_count=_int_or_none(data.get("repin_count")),
        comment_count=_int_or_none(data.get("comment_count")),
        thumbnail=best_thumb,
        thumbnails=thumbnails,
        media_type=media_kind,
        media_group_type=group,
        items=items,
        quality_hints=_quality_hints(config),
        providers=["pinterest"],
        extra={
            "repin_count": _int_or_none(data.get("repin_count")),
            "domain": data.get("domain"),
            "source_link": data.get("link") if config.include_source_link else None,
            "hashtags": hashtags,
            "categories": list(pin_join.get("visual_annotation") or []),
            "story_pages": len(_pages(data)),
            "video_variants": sorted(_video_list(data)) if _has_video(data) else [],
            "is_video": _has_video(data),
            "extracted_at": time.time(),
        },
        raw=_trim_pin(data),
    )
    for item in items:
        item.size_bytes = item.total_size_bytes
    return metadata.rebuild_groups()


def board_to_metadata(
    pins: Sequence[Mapping[str, Any]],
    board: Mapping[str, Any],
    config: PinterestConfig,
    *,
    ref: Optional[PinterestRef] = None,
    requested_url: Optional[str] = None,
) -> PostMetadata:
    """Turn a ``BoardFeed`` payload (a list of pins) into metadata.

    Each pin becomes one item. Video pins carry the api fallback ladder rather
    than the yt-dlp one: resolving the real quality ladder for every pin of a
    board would cost one extraction per pin. The warning on the metadata says
    so, and ``get_metadata`` on a pin url still returns the full ladder.
    """
    items: list[MediaItem] = []
    for pin in pins[: max(1, config.board_max_items) if config.board_max_items else None]:
        if not isinstance(pin, dict) or pin.get("type") != "pin":
            continue
        for item in pin_items(pin, config):
            item.index = len(items)
            item.source_url = item.source_url or requested_url
            items.append(item)
    thumbnails: list[Thumbnail] = []
    for item in items:
        thumb = item.meta.get("thumbnail")
        if thumb:
            thumbnails.append(Thumbnail(url=str(thumb), extension=_extension_of(str(thumb))))
    media_kind, group = _group_of(items)
    board_id = str(board.get("id") or "")
    metadata = PostMetadata(
        platform=Platform.PINTEREST,
        id=board_id or (f"{ref.username}/{ref.board_slug}" if ref else ""),
        url=board.get("url") or requested_url,
        requested_url=requested_url,
        permalink=board.get("url") or requested_url,
        title=board.get("name") or (ref.board_slug if ref else None),
        author=(board.get("owner") or {}).get("username") if isinstance(board.get("owner"), dict) else (ref.username if ref else None),
        thumbnail=thumbnails[0].url if thumbnails else None,
        thumbnails=thumbnails,
        media_type=media_kind,
        media_group_type=MediaGroupType.PLAYLIST if group is MediaGroupType.GALLERY else group,
        items=items,
        is_playlist=True,
        quality_hints=_quality_hints(config),
        providers=["pinterest"],
        extra={
            "board_id": board_id,
            "board_username": ref.username if ref else None,
            "board_slug": ref.board_slug if ref else None,
            "pin_count": _int_or_none(board.get("pin_count")),
            "resolved_items": len(items),
            "extracted_at": time.time(),
        },
        raw={},
    )
    metadata.warnings.append(
        "board pins carry the best format only; open a pin url for its full ladder"
    )
    for item in items:
        item.size_bytes = item.total_size_bytes
    return metadata.rebuild_groups()


def ytdlp_board_to_metadata(
    info: Mapping[str, Any],
    config: PinterestConfig,
    *,
    ref: Optional[PinterestRef] = None,
    requested_url: Optional[str] = None,
) -> PostMetadata:
    """Turn a yt-dlp ``PinterestCollection`` payload into metadata."""
    entries = [entry for entry in (info.get("entries") or []) if isinstance(entry, dict)]
    items: list[MediaItem] = []
    for entry in entries:
        item = _board_item(entry, config)
        if item is not None:
            items.append(item)
    media_kind, group = _group_of(items)
    thumbnails: list[Thumbnail] = []
    for item in items:
        thumb = item.meta.get("thumbnail")
        if thumb:
            thumbnails.append(Thumbnail(url=str(thumb), extension=_extension_of(str(thumb))))
    metadata = PostMetadata(
        platform=Platform.PINTEREST,
        id=str(info.get("id") or (f"{ref.username}/{ref.board_slug}" if ref else "")),
        url=info.get("webpage_url") or requested_url,
        requested_url=requested_url,
        permalink=info.get("webpage_url"),
        title=info.get("title") or (ref.board_slug if ref else None),
        author=ref.username if ref else None,
        thumbnail=thumbnails[0].url if thumbnails else None,
        thumbnails=thumbnails,
        media_type=media_kind,
        media_group_type=MediaGroupType.PLAYLIST if group is MediaGroupType.GALLERY else group,
        items=items,
        is_playlist=True,
        quality_hints=_quality_hints(config),
        providers=["ytdlp", "pinterest"],
        extra={
            "board_id": info.get("id"),
            "board_username": ref.username if ref else None,
            "board_slug": ref.board_slug if ref else None,
            "entry_count": len(entries),
            "extracted_at": time.time(),
        },
        raw={},
    )
    if config.board_max_items and len(entries) > config.board_max_items:
        metadata.warnings.append(
            f"only the first {config.board_max_items} pins of this board were resolved"
        )
    for item in items:
        item.size_bytes = item.total_size_bytes
    return metadata.rebuild_groups()


def _quality_height(data: Mapping[str, Any]) -> Optional[int]:
    """The short side of a rendition, i.e. the label Pinterest itself uses.

    Pinterest videos are overwhelmingly vertical, and its own HLS rendition
    names (``_240w`` ... ``_640w``) label a rendition by its *width*. Using the
    short side keeps ``quality_height``, ``quality_label`` and the HLS ladder
    consistent with each other instead of reporting a meaningless ``1138p``.
    """
    width = _int_or_none(data.get("width"))
    height = _int_or_none(data.get("height"))
    if width and height:
        return min(width, height)
    return height or width


# ---------------------------------------------------------------- helpers
def _page_items(
    page: Mapping[str, Any], data: Mapping[str, Any], config: PinterestConfig
) -> list[MediaItem]:
    """The item(s) of one story pin page (a video page or an image page)."""
    video_list: dict[str, Any] = {}
    for block in page.get("blocks") or ():
        if not isinstance(block, dict):
            continue
        video = block.get("video")
        if isinstance(video, dict) and isinstance(video.get("video_list"), dict):
            video_list.update(video["video_list"])
    if video_list and config.include_videos:
        return _video_items(data, config, video_list=video_list)
    if not config.include_images:
        return []
    return _image_items(data, config, payloads=_page_images(page))


def _video_items(
    data: Mapping[str, Any],
    config: PinterestConfig,
    *,
    video_list: Optional[Mapping[str, Any]] = None,
) -> list[MediaItem]:
    formats = api_video_formats(data, config, video_list=video_list)
    if not formats:
        return []
    variant = formats[0]
    duration = variant.meta.get("duration")
    return [
        MediaItem(
            index=0,
            id=str(data.get("id") or ""),
            kind=MediaKind.VIDEO,
            caption=_description(data),
            source_url=f"https://www.pinterest.com/pin/{data.get('id')}/",
            extension="mp4",
            mime_type="video/mp4",
            width=variant.width,
            height=variant.height,
            duration=float(duration) if duration else None,
            has_audio=True,
            formats=formats,
            meta={"thumbnail": _best_thumbnail(_thumbnails(data))},
        )
    ]


def _image_items(
    data: Mapping[str, Any],
    config: PinterestConfig,
    *,
    payloads: Optional[Sequence[Mapping[str, Any]]] = None,
) -> list[MediaItem]:
    if payloads is None:
        collected: list[Mapping[str, Any]] = []
        pages = _pages(data)
        if len(pages) > 1 and config.include_story_pages:
            for page in pages[: max(1, config.max_story_pages)]:
                collected.extend(_page_images(page))
        if not collected:
            top = data.get("images")
            if isinstance(top, dict):
                collected.append(top)
        payloads = collected
    items: list[MediaItem] = []
    for payload in payloads:
        formats = image_formats(payload, config)
        if not formats:
            continue
        best = max(formats, key=lambda fmt: fmt.quality_height or 0)
        is_gif = all(fmt.kind == FormatKind.GIF for fmt in formats)
        items.append(
            MediaItem(
                index=len(items),
                id=str(data.get("id") or ""),
                kind=MediaKind.GIF if is_gif else MediaKind.IMAGE,
                caption=_description(data),
                source_url=f"https://www.pinterest.com/pin/{data.get('id')}/",
                extension=best.extension,
                mime_type=best.mime_type,
                width=best.width,
                height=best.height,
                has_audio=False,
                formats=formats,
            )
        )
    return items


def _board_item(entry: Mapping[str, Any], config: PinterestConfig) -> Optional[MediaItem]:
    """One pin of a board feed, as a :class:`MediaItem`."""
    formats = ytdlp_video_formats(entry, config)
    thumbnails = _thumbnails_from_ytdlp(entry)
    thumb = _best_thumbnail(thumbnails)
    if formats and config.include_videos:
        best = max(formats, key=lambda fmt: fmt.quality_height or 0)
        return MediaItem(
            index=0,
            id=str(entry.get("id") or ""),
            kind=MediaKind.VIDEO,
            caption=entry.get("title") or entry.get("description"),
            source_url=entry.get("webpage_url"),
            extension="mp4",
            mime_type="video/mp4",
            width=best.width,
            height=best.height,
            duration=_float_or_none(entry.get("duration")),
            has_audio=any(fmt.has_audio for fmt in formats),
            formats=formats,
            meta={"thumbnail": thumb, "uploader": entry.get("uploader")},
        )
    if not config.include_images:
        return None
    image_formats_ = image_formats_from_thumbnails(entry.get("thumbnails") or [], config)
    if not image_formats_:
        return None
    best_image = max(image_formats_, key=lambda fmt: fmt.quality_height or 0)
    return MediaItem(
        index=0,
        id=str(entry.get("id") or ""),
        kind=MediaKind.IMAGE,
        caption=entry.get("title") or entry.get("description"),
        source_url=entry.get("webpage_url"),
        extension=best_image.extension,
        mime_type=best_image.mime_type,
        width=best_image.width,
        height=best_image.height,
        has_audio=False,
        formats=image_formats_,
        meta={"thumbnail": thumb, "uploader": entry.get("uploader")},
    )


def _group_of(items: Sequence[MediaItem]) -> tuple[MediaKind, MediaGroupType]:
    if not items:
        return MediaKind.TEXT, MediaGroupType.NONE
    kinds = {item.kind for item in items}
    if len(items) > 1:
        return MediaKind.GALLERY, MediaGroupType.GALLERY
    if kinds == {MediaKind.GIF}:
        return MediaKind.GIF, MediaGroupType.SINGLE
    if kinds == {MediaKind.IMAGE}:
        return MediaKind.IMAGE, MediaGroupType.SINGLE
    return MediaKind.VIDEO, MediaGroupType.SINGLE


def _quality_hints(config: PinterestConfig) -> dict[str, Any]:
    return {
        "container": config.prefer_container,
        "codec": config.prefer_video_codec,
        "audio_codec": config.prefer_audio_codec,
        "prefer_muxed": True,
    }


def _title(data: Mapping[str, Any]) -> Optional[str]:
    for key in ("title", "grid_title"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _description(data: Mapping[str, Any]) -> Optional[str]:
    for key in ("description", "seo_description"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _thumbnails(data: Mapping[str, Any]) -> list[Thumbnail]:
    images = data.get("images")
    if not isinstance(images, dict):
        return []
    out: list[Thumbnail] = []
    for name, entry in images.items():
        if not isinstance(entry, dict) or not entry.get("url"):
            continue
        url = str(entry["url"])
        out.append(
            Thumbnail(
                url=url,
                width=_int_or_none(entry.get("width")),
                height=_int_or_none(entry.get("height")),
                extension=_extension_of(url),
                id=str(name),
            )
        )
    return out


def _thumbnails_from_ytdlp(entry: Mapping[str, Any]) -> list[Thumbnail]:
    out: list[Thumbnail] = []
    for item in entry.get("thumbnails") or ():
        if not isinstance(item, dict) or not item.get("url"):
            continue
        url = str(item["url"])
        if url.startswith("//"):
            url = f"https:{url}"
        out.append(
            Thumbnail(
                url=url,
                width=_int_or_none(item.get("width")),
                height=_int_or_none(item.get("height")),
                extension=_extension_of(url),
            )
        )
    return out


def _best_thumbnail(thumbnails: Sequence[Thumbnail]) -> Optional[str]:
    if not thumbnails:
        return None
    real = [t for t in thumbnails if (t.height or 0) >= 200] or list(thumbnails)
    best = max(real, key=lambda t: (t.pixels, 1 if t.extension in ("jpg", "jpeg") else 0))
    return best.url


def _timestamp_of(value: Any) -> Optional[float]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return parsedate_to_datetime(value).timestamp()
    except (TypeError, ValueError):
        return None


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


def _trim_pin(data: Mapping[str, Any], *, keep: int = 40) -> dict[str, Any]:
    """A json-serialisable subset of the pin payload (media trees excluded)."""
    drop = {"images", "story_pin_data", "videos", "closeup_unified_metadata"}
    out: dict[str, Any] = {}
    for key, value in data.items():
        if key in drop:
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            out[key] = value
        elif isinstance(value, list) and len(value) <= keep and all(
            isinstance(item, (str, int, float)) for item in value
        ):
            out[key] = value
        elif isinstance(value, dict):
            flat = {
                k: v
                for k, v in value.items()
                if isinstance(v, (str, int, float, bool)) or v is None
            }
            if flat:
                out[key] = flat
    return out


__all__ = [
    "RENDITION_ORDER",
    "api_video_formats",
    "attach_video_formats",
    "best_variant",
    "board_to_metadata",
    "image_formats",
    "pin_items",
    "image_formats_from_thumbnails",
    "pin_to_metadata",
    "ytdlp_board_to_metadata",
    "ytdlp_video_formats",
]