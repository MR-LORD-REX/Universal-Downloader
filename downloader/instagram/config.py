"""Runtime configuration for the Instagram SDK."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..core.config import PlatformConfig


class InstagramConfig(PlatformConfig):
    """Everything tunable about :class:`~downloader.instagram.InstagramClient`.

    Credentials
    -----------
    Instagram rate limits anonymous clients hard: the first few requests of a
    session usually succeed, then the API answers HTTP 401 with "Please wait a
    few minutes before you try again". A logged in session file makes the SDK
    reliable. Create one once with::

        instaloader --login=<your user> --sessionfile=instagram.session

    and point ``session_file`` at it (or set ``INSTAGRAM_SESSION_FILE``).
    """

    # --------------------------------------------------------- credentials
    session_file: Optional[Path] = None
    """A file written by ``instaloader --login`` (the reliable path)."""
    username: Optional[str] = None
    """Account to log in as when ``session_file`` does not exist yet."""
    password: Optional[str] = None
    """Password for ``username`` (used once, then cached in ``session_file``)."""
    custom_user_agent: Optional[str] = None
    """Override the user agent *for Instagram only* (instaloader picks a good
    default, so leave empty unless Instagram has started rejecting it)."""

    # -------------------------------------------------------------- media
    include_images: bool = True
    include_videos: bool = True
    include_carousels: bool = True
    """Expand sidecar (carousel) posts into one item per child."""
    include_stories: bool = True
    """Stories/highlights are attempted, but they need a session and expire
    after 24 hours - a clear error is raised when the resolve fails."""
    max_carousel_items: int = 20
    """Safety valve for very long carousels."""

    # ------------------------------------------------------------ formats
    include_dash: bool = False
    """Expose the DASH ladder (8 steps, up to 1080p).

    Off by default, deliberately. Rendition height dominates format ranking, so
    once the ladder is present a plain ``best`` would resolve to the 1080p DASH
    track - which is **VP9 with no audio**, i.e. it forces a mux for every reel.
    Leaving it off keeps the progressive H.264+AAC mp4 (720x1280, already muxed)
    as the pick, which a Telegram bot can hand to the Bot API as a url with no
    download at all.

    Turn it on when you want the higher quality tiers; expect the processing
    lane (and a VP9-in-mp4 mux) for those items. The progressive copy stays
    available either way.
    """
    include_progressive: bool = True
    """Expose Instagram's progressive mp4 (H.264 + AAC in one file)."""
    max_image_candidates: int = 4
    """How many ``image_versions2`` variants to expose per photo."""


__all__ = ["InstagramConfig"]
