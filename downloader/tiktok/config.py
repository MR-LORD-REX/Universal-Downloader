"""Runtime configuration for the TikTok SDK."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from pydantic import Field

from ..core.config import PlatformConfig


class TikTokConfig(PlatformConfig):
    """Everything tunable about :class:`~downloader.tiktok.TikTokClient`.

    Notes
    -----
    **Photo (slideshow) posts.** yt-dlp's TikTok extractor has no image support
    at all: a slideshow post yields only its *soundtrack* as an ``m4a`` format
    (an upstream limitation, verified against yt-dlp's own test fixtures). The
    SDK surfaces that audio and warns, rather than pretending to have the
    images. See ``docs/TIKTOK_SDK.md``.

    **Codec.** TikTok publishes H.264, H.265/bytevc1 (``play_addr_bytevc1``) and
    bytevc2 renditions. ``h264`` is the default preference because H.265 in
    mp4 is not reliably playable in every Telegram client.
    """

    # -------------------------------------------------------------- media
    include_videos: bool = True
    include_audio_only: bool = True
    """Keep an audio-only result (a slideshow's soundtrack, or a music post)."""
    include_watermarked: bool = False
    """Keep the ``download`` rendition yt-dlp reports (it carries the TikTok logo).

    It is dropped when any non-watermarked rendition exists, and only kept as a
    last resort when it is the only thing the extractor returned.
    """
    include_manifests: bool = False
    include_unplayable: bool = False
    """Keep bytevc2/h266 renditions, which yt-dlp itself marks as unplayable."""

    # --------------------------------------------------------- extraction
    resolve_profiles: bool = True
    """Allow profile/sound/hashtag/collection urls (they resolve to a list)."""
    playlist_max_items: int = 25
    """How many videos to resolve for a profile/sound/tag/collection."""
    extract_flat: bool = True
    """List profile entries without resolving every video's formats."""

    # ------------------------------------------------------- ytdlp tuning
    cookiefile: Optional[Path] = None
    cookies_from_browser: Optional[str] = None
    app_info: list[str] = Field(default_factory=list)
    """Values for yt-dlp's ``tiktok`` ``app_info`` extractor argument.

    TikTok's web endpoint is heavily rate limited and, on many datacentre IPs,
    refuses outright ("Your IP address is blocked from accessing this post").
    Supplying one or more observed mobile ``app_info`` strings lets yt-dlp use
    the app endpoint - a ``device_id`` usually has to be supplied with them.
    """
    device_id: Optional[str] = None
    """``tiktok`` ``device_id`` extractor argument (normally used with ``app_info``)."""
    extractor_args: dict[str, Any] = Field(default_factory=dict)
    extra_ytdlp_options: dict[str, Any] = Field(default_factory=dict)

    # ------------------------------------------------------------ quality
    prefer_video_codec: Optional[str] = "h264"
    prefer_audio_codec: str = "aac"


__all__ = ["TikTokConfig"]