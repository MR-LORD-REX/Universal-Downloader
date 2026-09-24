"""Small url helpers shared by the SDKs."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from urllib.parse import parse_qs, urlparse

_EXT_RE = re.compile(r"\.([A-Za-z0-9]{2,4})$")

_EXT_ALIASES = {
    "jpeg": "jpg",
    "jpe": "jpg",
    "m4v": "mp4",
    "mp4v": "mp4",
    "webp": "webp",
    "avif": "avif",
    "m3u8": "m3u8",
    "mpd": "mpd",
}

_VIDEO_EXT = frozenset({"mp4", "webm", "mkv", "mov", "m4v", "ts", "flv", "avi"})
_AUDIO_EXT = frozenset({"m4a", "mp3", "aac", "opus", "ogg", "oga", "wav", "flac", "weba"})
_IMAGE_EXT = frozenset({"jpg", "jpeg", "png", "gif", "webp", "avif", "bmp", "heic"})
_TEXT_EXT = frozenset({"vtt", "srt", "json", "txt", "ttml"})


def host_of(url: str) -> str:
    """Lower-cased hostname of ``url`` (``""`` when unparseable)."""
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:  # pragma: no cover - defensive
        return ""


def extension_of(url: str) -> str | None:
    """Best effort file extension for a url path (query string ignored)."""
    try:
        path = urlparse(url).path
    except ValueError:  # pragma: no cover - defensive
        return None
    match = _EXT_RE.search(path)
    if not match:
        return None
    ext = match.group(1).lower()
    return _EXT_ALIASES.get(ext, ext)


def mime_for_extension(ext: str | None) -> str | None:
    """Map a file extension to a mime type (``None`` when unknown)."""
    if not ext:
        return None
    ext = ext.lower().lstrip(".")
    if ext in _VIDEO_EXT:
        return "video/mp4" if ext in ("mp4", "m4v") else f"video/{ext}"
    if ext in _AUDIO_EXT:
        return {
            "m4a": "audio/mp4",
            "mp3": "audio/mpeg",
            "aac": "audio/aac",
            "opus": "audio/opus",
            "oga": "audio/ogg",
        }.get(ext, f"audio/{ext}")
    if ext in _IMAGE_EXT:
        return "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}"
    if ext == "gif":
        return "image/gif"
    if ext in _TEXT_EXT:
        return {"vtt": "text/vtt", "srt": "application/x-subrip", "json": "application/json"}.get(
            ext, "text/plain"
        )
    return None


def kind_for_extension(ext: str | None) -> str | None:
    """Coarse category of a file extension: ``video``/``audio``/``image``/``text``."""
    if not ext:
        return None
    ext = ext.lower().lstrip(".")
    if ext in _VIDEO_EXT:
        return "video"
    if ext in _AUDIO_EXT:
        return "audio"
    if ext in _IMAGE_EXT:
        return "image"
    if ext in _TEXT_EXT:
        return "text"
    return None


def filename_of(url: str, *, fallback: str = "media") -> str:
    """Last path segment of ``url`` (sanitised, without the query string)."""
    try:
        path = urlparse(url).path
    except ValueError:  # pragma: no cover - defensive
        return fallback
    name = PurePosixPath(path).name or fallback
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)[:150] or fallback


def query_value(url: str, key: str) -> str | None:
    """First value of the ``key`` query parameter (``None`` when absent)."""
    try:
        values = parse_qs(urlparse(url).query).get(key)
    except ValueError:  # pragma: no cover - defensive
        return None
    return values[0] if values else None