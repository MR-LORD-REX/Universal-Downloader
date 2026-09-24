"""YouTube specific yt-dlp glue.

The extraction mechanics *and* the ``-f`` / ``-S`` translation live in
:mod:`downloader.core.ytdlp` so every yt-dlp backed platform (YouTube, Twitter,
Pinterest, TikTok) shares one implementation. This module re-exports them
under the names the YouTube client and its tests already use.
"""

from __future__ import annotations

from ..core.ytdlp import (
    CollectingLogger,
    YtDlpExtractor,
    yt_dlp,
    ytdlp_format_selector,
    ytdlp_format_sort,
)
from .exceptions import ExtractionError, LiveStreamError, VideoUnavailableError

__all__ = [
    "CollectingLogger",
    "ExtractionError",
    "LiveStreamError",
    "VideoUnavailableError",
    "YtDlpExtractor",
    "yt_dlp",
    "ytdlp_format_selector",
    "ytdlp_format_sort",
]