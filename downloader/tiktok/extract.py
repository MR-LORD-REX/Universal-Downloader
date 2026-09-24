"""TikTok specific yt-dlp glue.

The extraction mechanics live in :mod:`downloader.core.ytdlp`; this module adds
the TikTok error translation (region blocks and private posts have their own
yt-dlp messages) and re-exports the shared helpers under the names the client
already uses.
"""

from __future__ import annotations

from ..core.exceptions import MetadataError
from ..core.ytdlp import (
    CollectingLogger,
    YtDlpExtractor,
    yt_dlp,
    ytdlp_format_selector,
    ytdlp_format_sort,
)
from .exceptions import (
    RegionBlockedError,
    TikTokError,
    VideoNotFoundError,
    VideoUnavailableError,
)

#: TikTok refuses these requests outright (``status 10204`` is its "your IP
#: address is blocked" response). Datacentre and VPN ranges hit it constantly.
REGION_MARKERS = (
    "ip address is blocked",
    "status code 10204",
    "status 10204",
    "not available in your country",
    "geo restrict",
    "unavailable in your location",
)
#: The post is private, friends-only or needs a logged in account.
PRIVATE_MARKERS = (
    "you do not have permission to view this post",
    "log into an account that has access",
    "private account",
    "friends only",
    "status 10216",
    "status 10222",
)
#: The extractor ran fine but found nothing to download.
MISSING_MARKERS = (
    "video not available",
    "unable to find video",
    "this post is unavailable",
    "no video formats found",
)


def translate_tiktok_error(message: str) -> MetadataError:
    """Map a yt-dlp error string onto the TikTok exception hierarchy.

    Only the messages TikTok itself produces become a :class:`TikTokError`.
    Anything else - a connect timeout, a DNS failure, a proxy error - is left
    as a plain :class:`~downloader.core.exceptions.MetadataError` so callers can
    tell "TikTok said no" apart from "the network died".

    Note that a blocked IP does not always announce itself politely: some
    ranges get ``status 10204``, others simply time out on connect.
    """
    lowered = (message or "").lower()
    if any(marker in lowered for marker in REGION_MARKERS):
        return RegionBlockedError(message)
    if any(marker in lowered for marker in PRIVATE_MARKERS):
        return VideoNotFoundError(message)
    if any(marker in lowered for marker in MISSING_MARKERS):
        return VideoUnavailableError(message)
    return MetadataError(message)


__all__ = [
    "CollectingLogger",
    "RegionBlockedError",
    "TikTokError",
    "VideoNotFoundError",
    "VideoUnavailableError",
    "YtDlpExtractor",
    "yt_dlp",
    "ytdlp_format_selector",
    "ytdlp_format_sort",
    "translate_tiktok_error",
]