"""Runtime configuration for the YouTube SDK."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from pydantic import Field

from ..core.config import PlatformConfig


class YouTubeConfig(PlatformConfig):
    """Everything tunable about :class:`~downloader.youtube.YouTubeClient`.

    Inherits the transport/download knobs from
    :class:`~downloader.core.config.PlatformConfig` and adds YouTube specifics.
    """

    # ------------------------------------------------------------- formats
    include_manifest_formats: bool = False
    """Keep the ``m3u8_native`` duplicates of the ``https`` renditions.

    yt-dlp reports every YouTube rendition twice: once as a progressive
    ``https`` stream (with an exact ``filesize``) and once as an HLS manifest
    URL (no size). The HLS copies are dropped by default because they add
    nothing for a downloader and make the quality ladder twice as long.
    """
    include_storyboards: bool = False
    include_all_audio_languages: bool = False
    """Keep every dubbed audio track.

    Videos with multiple audio tracks report one audio format per language
    (``140-0``, ``140-1``, ...). By default only the original/default track is
    kept so the quality ladder stays readable; set this to ``True`` to expose
    every dub.
    """
    audio_languages: list[str] = Field(default_factory=list)
    """Only keep these audio languages when the video is dubbed (e.g. ``["en", "ja"]``)."""
    include_progressive_muxed_only_when_best: bool = True
    prefer_short_side: bool = True
    """Treat the shorter side as the rendition label (vertical/shorts videos)."""

    # ----------------------------------------------------------- subtitles
    include_subtitles: bool = True
    include_auto_captions: bool = True
    subtitle_languages: list[str] = Field(default_factory=list)
    """Requested caption languages; empty means "all the video offers"."""

    # ---------------------------------------------------- extractor tuning
    player_client: Optional[str] = None
    """Force a yt-dlp ``youtube`` player client (``web``, ``tv``, ``ios``...)."""
    cookiefile: Optional[Path] = None
    cookies_from_browser: Optional[str] = None
    extra_ytdlp_options: dict[str, Any] = Field(default_factory=dict)
    """Escape hatch: merged verbatim into the yt-dlp options dict."""

    # ------------------------------------------------------------ fetching
    download_backend: str = "auto"
    """``auto`` (native streaming, yt-dlp fallback), ``native`` or ``ytdlp``."""
    url_ttl: float = 1800.0
    """Re-extract before downloading when the metadata is older than this.

    YouTube CDN urls are signed and expire after a few hours; re-extracting
    guarantees a fresh signature instead of a 403.
    """
    playlist_max_items: int = 25
    resolve_playlists: bool = True
    community_post_extraction: bool = True
    """Scrape ``youtube.com/post/<id>`` pages (yt-dlp cannot handle them)."""

    # ------------------------------------------------------------- quality
    ranged_download: bool = True
    """YouTube throttles an un-ranged ``GET`` to a few KB/s; ranges run at full speed."""
    range_chunk_size: int = 4 * 1024 * 1024
    prefer_video_codec: Optional[str] = "avc1"
    prefer_container: Optional[str] = "mp4"
    prefer_audio_codec: str = "mp4a"