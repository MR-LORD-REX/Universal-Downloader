"""Runtime configuration for the Twitter/X SDK."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from pydantic import Field

from ..core.config import PlatformConfig


class TwitterConfig(PlatformConfig):
    """Everything tunable about :class:`~downloader.twitter.TwitterClient`."""

    # ------------------------------------------------------------- metadata
    media_api: str = "auto"
    """Where non-video media metadata comes from.

    ``auto``/``fxtwitter`` use the public ``api.fxtwitter.com`` fixup api,
    which is the only key-less source that exposes *photo* media (yt-dlp only
    ever returns videos and fails outright on image-only tweets). ``off``
    restricts the SDK to video tweets via yt-dlp.
    """
    media_api_url: str = "https://api.fxtwitter.com"
    photo_quality: str = "orig"
    """Which ``pbs.twimg.com`` variant to prefer: ``orig``/``large``/``medium``/``small``."""
    include_photos: bool = True
    include_videos: bool = True
    include_all_media: bool = True
    """Keep every photo of a multi-image tweet (otherwise only the first)."""

    # ----------------------------------------------------------- extraction
    resolve_video_formats: bool = True
    """Use yt-dlp to enumerate the video quality ladder (recommended)."""
    include_manifest_formats: bool = False
    """Keep the HLS renditions; they are dropped when a progressive copy exists."""
    include_gif_as_video: bool = False
    """Download animated GIFs as mp4 (``False``) or flag them as gifs (``True``)."""
    resolve_tco_links: bool = True
    """Follow ``t.co`` short links to the underlying tweet."""

    # -------------------------------------------------------- ytdlp tuning
    cookiefile: Optional[Path] = None
    cookies_from_browser: Optional[str] = None
    extractor_args: dict[str, Any] = Field(default_factory=dict)
    extra_ytdlp_options: dict[str, Any] = Field(default_factory=dict)

    # ------------------------------------------------------------- quality
    prefer_video_codec: Optional[str] = "avc1"
    prefer_container: Optional[str] = "mp4"
    prefer_audio_codec: str = "mp4a"
    max_photos: int = 4