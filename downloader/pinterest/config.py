"""Runtime configuration for the Pinterest SDK."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from pydantic import Field

from ..core.config import PlatformConfig


class PinterestConfig(PlatformConfig):
    """Everything tunable about :class:`~downloader.pinterest.PinterestClient`.

    Pinterest needs no credentials for pins or boards, so this config is
    purely about *what* to expose: image renditions, the video ladder and how
    many pins of a board to resolve.
    """

    # -------------------------------------------------------------- media
    include_images: bool = True
    include_videos: bool = True
    photo_quality: str = "orig"
    """Which ``i.pinimg.com`` rendition counts as the original.

    ``orig`` (default) keeps the full resolution image. Every rendition is
    still exposed as a format - this only decides which one ranks best, so a
    bot can ask for ``736x`` to save bandwidth without losing the choice.
    """
    max_image_candidates: int = 5
    """How many ``images`` renditions to expose per photo (``orig`` first)."""
    include_story_pages: bool = True
    """Expand multi page "idea" (story) pins into one item per page."""
    max_story_pages: int = 20
    """Safety valve for very long story pins."""

    # --------------------------------------------------------- extraction
    include_manifest_formats: bool = False
    """Keep the HLS renditions when a progressive copy of the same size exists."""
    resolve_short_links: bool = True
    """Follow ``pin.it`` short links to the underlying pin."""
    resolve_boards: bool = True
    """Allow board urls (``/<user>/<board>/``); each pin becomes an item."""
    board_max_items: int = 25
    """How many pins of a board to list (the board feed is paginated)."""
    include_source_link: bool = True
    """Record the outbound ``link`` of a pin in ``metadata.extra``."""

    # ------------------------------------------------------- ytdlp tuning
    cookiefile: Optional[Path] = None
    cookies_from_browser: Optional[str] = None
    extractor_args: dict[str, Any] = Field(default_factory=dict)
    extra_ytdlp_options: dict[str, Any] = Field(default_factory=dict)

    # ------------------------------------------------------------ quality
    prefer_video_codec: Optional[str] = "avc1"
    prefer_audio_codec: str = "mp4a"


__all__ = ["PinterestConfig"]