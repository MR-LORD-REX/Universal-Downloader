"""Recognise supported links inside text, captions and entities.

Unrecognised links are intentionally ignored (the brief is explicit about it):
the filter simply does not match, so no handler runs and the bot stays quiet.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Optional, Sequence

from aiogram.types import Message
from aiogram.filters import BaseFilter

from downloader import Downloader
from downloader.core.enums import Platform

_URL_RE = re.compile(r"https?://[^\s<>\x22\x27]+", re.IGNORECASE)
_TRAILING = ".,;:!?)]}'\""

SUPPORTED_HOSTS = (
    "youtube.com",
    "youtu.be",
    "youtube-nocookie.com",
    "twitter.com",
    "x.com",
    "t.co",
    "reddit.com",
    "redd.it",
    "instagram.com",
    "instagr.am",
    "ig.me",
)


def extract_urls(text: Optional[str]) -> list[str]:
    """Every http(s) url in `text`, with trailing punctuation trimmed."""
    if not text:
        return []
    found = []
    for raw in _URL_RE.findall(text):
        url = raw.rstrip(_TRAILING)
        if url and url not in found:
            found.append(url)
    return found


def extract_supported(
    text: Optional[str], enabled: Optional[Iterable[str]] = None
) -> list[tuple[str, str]]:
    """`[(platform, url)]` for every link a bundled SDK can handle."""
    allowed = {str(name) for name in enabled} if enabled is not None else None
    out: list[tuple[str, str]] = []
    for url in extract_urls(text):
        platform = Downloader.platform_of(url)
        if platform is Platform.UNKNOWN:
            continue
        name = str(platform)
        if allowed is not None and name not in allowed:
            continue
        if (name, url) not in out:
            out.append((name, url))
    return out


class LinkFilter(BaseFilter):
    """Matches messages that carry at least one supported link."""

    async def __call__(self, message: Message, **data: Any) -> bool | dict[str, Any]:
        ctx = data.get("ctx")
        enabled: Optional[Sequence[str]] = getattr(
            getattr(ctx, "registry", None), "enabled", None
        )
        links = extract_supported(message.text or message.caption, enabled)
        if not links:
            return False
        return {"links": links}


__all__ = ["SUPPORTED_HOSTS", "LinkFilter", "extract_supported", "extract_urls"]
