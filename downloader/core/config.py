"""Shared configuration base for the platform clients."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from .http import DEFAULT_BROWSER_AGENT, HttpOptions


class PlatformConfig(BaseModel):
    """Transport + download defaults shared by every platform config.

    Platform configs subclass this and add their own fields, so
    ``YouTubeConfig(...)`` accepts everything here plus the YouTube specific
    knobs.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    # ----------------------------------------------------------- transport
    user_agent: str = DEFAULT_BROWSER_AGENT
    timeout: float = 30.0
    connect_timeout: float = 10.0
    retries: int = 3
    backoff_factor: float = 0.6
    max_retry_wait: float = 15.0
    proxy: Optional[str] = None
    verify_ssl: bool = True
    max_http_concurrency: int = 8
    max_download_concurrency: int = 4
    chunk_size: int = 256 * 1024
    ranged_download: bool = False
    """Fetch in bounded ``Range`` requests instead of one long stream.

    YouTube throttles a single un-ranged ``GET`` down to a few KB/s, while
    ranged requests run at full speed. Enabling this trades a few extra
    requests for orders of magnitude more throughput on such CDNs.
    """
    range_chunk_size: int = 2 * 1024 * 1024
    """Bytes per ranged request when ``ranged_download`` is enabled."""
    request_interval: float = 0.0
    cookies: dict[str, str] = Field(default_factory=dict)

    # -------------------------------------------------------------- cache
    cache_dir: Optional[Path] = None
    size_cache_ttl: float = 6 * 60 * 60
    use_cache: bool = True

    # ------------------------------------------------------------ quality
    default_quality: str = "best"
    prefer_container: Optional[str] = "mp4"
    """Container the muxed output prefers (``mp4`` keeps Telegram happy)."""
    prefer_video_codec: Optional[str] = "avc1"
    prefer_audio_codec: str = "mp4a"
    audio_bitrate_preference: Optional[int] = None
    audio_bitrate_preference_ytdlp: str = "best"
    max_quality: int = 4320
    mux_audio: bool = True
    """Merge separate video/audio renditions with ffmpeg."""
    verify_audio: bool = True
    """Confirm downloaded video files really carry audio (ffprobe) and warn
    when they do not. Cheap insurance against a silent upload."""
    probe_sizes: bool = True
    """Confirm/repair sizes with a ``HEAD`` probe when metadata is missing."""
    max_size_bytes: Optional[int] = None
    """Refuse to download anything larger than this (premium gating)."""

    # ------------------------------------------------------------- naming
    default_pattern: str = "{platform}_{id}_{index}_{quality}.{ext}"

    # ------------------------------------------------------------ helpers
    def http_options(self) -> HttpOptions:
        """Build the :class:`HttpOptions` this config describes."""
        return HttpOptions(
            user_agent=self.user_agent,
            timeout=self.timeout,
            connect_timeout=self.connect_timeout,
            retries=self.retries,
            backoff_factor=self.backoff_factor,
            max_retry_wait=self.max_retry_wait,
            proxy=self.proxy,
            verify_ssl=self.verify_ssl,
            max_concurrency=self.max_http_concurrency,
            chunk_size=self.chunk_size,
            ranged_download=self.ranged_download,
            range_chunk_size=self.range_chunk_size,
            request_interval=self.request_interval,
            use_cache=self.use_cache,
            cache_dir=self.cache_dir,
            size_cache_ttl=self.size_cache_ttl,
            cookies=dict(self.cookies),
        )

    @classmethod
    def from_env(cls, prefix: str, **overrides: Any) -> "PlatformConfig":
        """Build a config from ``<PREFIX>_*`` environment variables."""
        env_map = {
            "user_agent": "USER_AGENT",
            "proxy": "PROXY",
            "cache_dir": "CACHE_DIR",
            "default_quality": "QUALITY",
            "default_pattern": "PATTERN",
            "max_size_bytes": "MAX_SIZE_BYTES",
        }
        data: dict[str, Any] = {}
        for field, suffix in env_map.items():
            value = os.getenv(f"{prefix}_{suffix}")
            if not value:
                continue
            data[field] = Path(value) if field == "cache_dir" else value
        proxy = data.get("proxy") or os.getenv("HTTPS_PROXY") or os.getenv("https_proxy")
        if proxy:
            data["proxy"] = proxy
        data.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**data)

    def with_overrides(self, **overrides: Any) -> "PlatformConfig":
        """Return a copy with ``None`` values ignored (handy for wrappers)."""
        data = {k: v for k, v in overrides.items() if v is not None}
        return self.model_copy(update=data)