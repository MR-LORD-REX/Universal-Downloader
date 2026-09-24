"""Shared yt-dlp bridge.

yt-dlp is used strictly as an *extractor*: it knows how to talk to the
platform APIs, de-obfuscate signed urls and enumerate rendition ladders. The
SDKs then download the resulting CDN urls themselves so they can report sizes,
stream with progress and mux with ffmpeg under one engine.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional, Sequence

from .exceptions import (
    DownloadError,
    InvalidURLError,
    MetadataError,
    MediaNotAvailableError,
)

UNAVAILABLE_MARKERS = (
    "private video",
    "video unavailable",
    "this video is not available",
    "removed by the uploader",
    "has been terminated",
    "sign in to confirm your age",
    "age-restricted",
    "not made this video available in your country",
    "blocked it on copyright grounds",
    "who has blocked it in your country",
    "no video could be found",
    "tweet not found",
    "does not exist",
)
LIVE_MARKERS = ("is not a live", "live stream", "premieres in", "live event will begin")


def yt_dlp() -> Any:
    """Import yt-dlp lazily so importing the SDK stays cheap."""
    try:
        import yt_dlp  # type: ignore
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise MetadataError("yt-dlp is required: pip install yt-dlp") from exc
    return yt_dlp


class CollectingLogger:
    """Keeps yt-dlp's chatter out of stdout while remembering the messages."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def debug(self, message: str) -> None:
        if message and not message.startswith("[debug]"):
            self.messages.append(message)

    def info(self, message: str) -> None:
        self.messages.append(message)

    def warning(self, message: str) -> None:
        self.messages.append(f"warning: {message}")

    def error(self, message: str) -> None:
        self.messages.append(f"error: {message}")


class YtDlpExtractor:
    """Async facade over :class:`yt_dlp.YoutubeDL`.

    The config object is duck typed: any of ``proxy``, ``cookiefile``,
    ``cookies_from_browser``, ``player_client``, ``extractor_args``,
    ``extra_ytdlp_options``, ``verify_ssl``, ``timeout``, ``retries`` and
    ``user_agent`` are honoured when present.
    """

    def __init__(self, config: Any) -> None:
        self.config = config
        self.last_messages: list[str] = []

    # ------------------------------------------------------------- options
    def options(self, **extra: Any) -> dict[str, Any]:
        """yt-dlp options derived from the SDK config (plus ``extra``)."""
        config = self.config
        options: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "nocolor": True,
            "skip_download": True,
            "nocheckcertificate": not getattr(config, "verify_ssl", True),
            "socket_timeout": getattr(config, "timeout", 30.0),
            "retries": max(1, int(getattr(config, "retries", 3))),
            "extractor_retries": max(1, int(getattr(config, "retries", 3))),
            "http_headers": {"User-Agent": getattr(config, "user_agent", "")},
            "logger": CollectingLogger(),
        }
        proxy = getattr(config, "proxy", None)
        if proxy:
            options["proxy"] = proxy
        cookiefile = getattr(config, "cookiefile", None)
        if cookiefile:
            options["cookiefile"] = str(cookiefile)
        browser = getattr(config, "cookies_from_browser", None)
        if browser:
            options["cookiesfrombrowser"] = (browser,)
        player_client = getattr(config, "player_client", None)
        if player_client:
            options["extractor_args"] = {"youtube": {"player_client": [player_client]}}
        extractor_args = getattr(config, "extractor_args", None)
        if extractor_args:
            merged = dict(options.get("extractor_args") or {})
            merged.update(extractor_args)
            options["extractor_args"] = merged
        options.update(getattr(config, "extra_ytdlp_options", None) or {})
        options.update(extra)
        return options

    # ----------------------------------------------------------- extraction
    def extract_sync(self, url: str, **extra: Any) -> dict[str, Any]:
        """Blocking extraction (prefer :meth:`extract` from async code)."""
        module = yt_dlp()
        options = self.options(**extra)
        logger = options.get("logger")
        try:
            with module.YoutubeDL(options) as downloader:
                info = downloader.extract_info(url, download=False)
        except module.utils.DownloadError as exc:
            raise translate_error(str(exc)) from exc
        except Exception as exc:  # pragma: no cover - yt-dlp internals
            raise MetadataError(f"{type(exc).__name__}: {exc}") from exc
        self.last_messages = list(getattr(logger, "messages", []))
        if not isinstance(info, dict):
            raise MetadataError(f"yt-dlp returned {type(info).__name__} for {url}")
        return info

    async def extract(self, url: str, **extra: Any) -> dict[str, Any]:
        """Extract ``url`` on a worker thread."""
        return await asyncio.to_thread(self.extract_sync, url, **extra)

    async def extract_flat(self, url: str, *, limit: Optional[int] = None) -> dict[str, Any]:
        """List playlist/channel entries without extracting each item."""
        extra: dict[str, Any] = {"extract_flat": "in_playlist", "skip_download": True}
        if limit is not None:
            extra["playlistend"] = limit
        return await self.extract(url, **extra)

    # ------------------------------------------------------ download (fallback)
    def download_sync(
        self,
        url: str,
        *,
        outtmpl: str,
        format_selector: Optional[str] = None,
        format_sort: Optional[Sequence[str]] = None,
        progress_hook: Optional[Any] = None,
        merge_output_format: Optional[str] = None,
        extra: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Fallback path: let yt-dlp do the whole download+merge itself."""
        extras: dict[str, Any] = {
            "skip_download": False,
            "outtmpl": outtmpl,
            "overwrites": True,
        }
        if format_selector:
            extras["format"] = format_selector
        if format_sort:
            extras["format_sort"] = list(format_sort)
        if progress_hook is not None:
            extras["progress_hooks"] = [progress_hook]
        if merge_output_format:
            extras["merge_output_format"] = merge_output_format
        if extra:
            extras.update(extra)
        return self.extract_sync(url, **extras)

    async def download(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return await asyncio.to_thread(self.download_sync, *args, **kwargs)


def translate_error(message: str) -> Exception:
    """Map a yt-dlp error string onto the SDK's exception hierarchy."""
    lowered = message.lower()
    if "private" in lowered or "unavailable" in lowered or "removed" in lowered:
        return DownloadError(message)
    if any(marker in lowered for marker in LIVE_MARKERS) and "live" in lowered:
        return DownloadError(message)
    if any(marker in lowered for marker in UNAVAILABLE_MARKERS):
        return MediaNotAvailableError(message)
    if "not a valid url" in lowered or "unsupported url" in lowered:
        return InvalidURLError(message)
    return MetadataError(message)


def output_path(info: dict[str, Any]) -> Optional[Any]:
    """The file yt-dlp actually wrote (``None`` when it did not report one)."""
    from pathlib import Path

    for entry in info.get("requested_downloads") or ():
        candidate = entry.get("filepath") or entry.get("_filename")
        if candidate and Path(candidate).exists():
            return Path(candidate)
    candidate = info.get("filepath") or info.get("_filename")
    if candidate and Path(candidate).exists():
        return Path(candidate)
    return None