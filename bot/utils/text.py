"""Formatting helpers used to render captions and status messages."""

from __future__ import annotations

import html
from datetime import datetime, timezone
from typing import Optional

from downloader.core.models import human_size


def escape(value: object) -> str:
    """HTML-escape a value for `parse_mode="HTML"` messages."""
    return html.escape("" if value is None else str(value), quote=False)


def clip(text: Optional[str], limit: int, *, suffix: str = "\u2026") -> str:
    """Truncate `text` to `limit` characters on a word boundary when possible."""
    if not text:
        return ""
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    cut = text[: max(0, limit - len(suffix))]
    if " " in cut[limit // 2 :]:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip() + suffix


def format_size(num: Optional[int]) -> str:
    """``6.19 MB`` (or an em dash when the size is unknown)."""
    return human_size(num) or "\u2014"


def format_count(num: Optional[int]) -> str:
    """Compact counts: ``1234`` -> ``1.2K``, ``1234567`` -> ``1.2M``."""
    if num is None:
        return "\u2014"
    value = float(num)
    for unit in ("", "K", "M", "B"):
        if abs(value) < 1000 or unit == "B":
            if unit == "":
                return f"{int(value)}"
            return f"{value:.1f}{unit}"
        value /= 1000
    return f"{value:.1f}B"  # pragma: no cover - unreachable


def format_duration(seconds: Optional[float]) -> str:
    """``3:15`` / ``1:02:03`` for anything from a few seconds upwards."""
    if not seconds or seconds <= 0:
        return "\u2014"
    total = int(round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_when(timestamp: Optional[float | datetime]) -> str:
    """Relative time ("3h ago", "2d ago") for a unix timestamp or datetime."""
    if timestamp is None:
        return "\u2014"
    if isinstance(timestamp, datetime):
        moment = timestamp if timestamp.tzinfo else timestamp.replace(tzinfo=timezone.utc)
    else:
        try:
            moment = datetime.fromtimestamp(float(timestamp), tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            return "\u2014"
    delta = datetime.now(timezone.utc) - moment
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "just now"
    for limit, divisor, unit in ((60, 1, "s"), (3600, 60, "m"), (86400, 3600, "h"), (2592000, 86400, "d")):
        if seconds < limit:
            return f"{max(1, seconds // divisor)}{unit} ago"
    for limit, divisor, unit in ((31536000, 2592000, "mo"), (10**12, 31536000, "y")):
        if seconds < limit:
            return f"{max(1, seconds // divisor)}{unit} ago"
    return "long ago"


def short_id(value: Optional[object], *, length: int = 8) -> str:
    """Shorten an opaque id for display: ``2100652253917098215`` -> ``…2158215``."""
    if value is None:
        return "\u2014"
    text = str(value)
    return text if len(text) <= length else "\u2026" + text[-length:]


__all__ = [
    "clip",
    "escape",
    "format_count",
    "format_duration",
    "format_size",
    "format_when",
    "short_id",
]
