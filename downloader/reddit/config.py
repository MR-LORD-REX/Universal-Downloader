"""Runtime configuration for the Reddit SDK."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import __version__

DEFAULT_BROWSER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
DEFAULT_API_AGENT = f"windows:reddit-downloader-sdk:{__version__} (by /u/unknown)"
DEFAULT_PROVIDERS: tuple[str, ...] = ("oauth", "arctic_shift", "rss", "oembed")
ALL_PROVIDERS: tuple[str, ...] = (
    "oauth",
    "arctic_shift",
    "rss",
    "oembed",
    "direct",
    "manual",
)


class RedditConfig(BaseModel):
    """Everything tunable about the client.

    All fields have sane defaults: ``RedditConfig()`` works with no
    credentials at all (anonymous metadata sources + direct CDN downloads).
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    # ------------------------------------------------------------- identity
    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    refresh_token: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    access_token: Optional[str] = None

    # --------------------------------------------------------------- agents
    user_agent: str = DEFAULT_BROWSER_AGENT
    api_user_agent: str = DEFAULT_API_AGENT

    # ------------------------------------------------------------ providers
    providers: list[str] = Field(default_factory=lambda: list(DEFAULT_PROVIDERS))
    arctic_shift_url: str = "https://arctic-shift.photon-reddit.com"
    oauth_url: str = "https://oauth.reddit.com"
    token_url: str = "https://www.reddit.com/api/v1/access_token"
    request_interval: float = 0.0
    """Minimum seconds between two requests to the same host (0 disables)."""

    # ----------------------------------------------------------------- http
    timeout: float = 30.0
    connect_timeout: float = 10.0
    retries: int = 3
    backoff_factor: float = 0.6
    max_retry_wait: float = 15.0
    proxy: Optional[str] = None
    verify_ssl: bool = True
    max_http_concurrency: int = 8
    max_download_concurrency: int = 6
    chunk_size: int = 256 * 1024

    # ---------------------------------------------------------- media logic
    expand_formats: bool = True
    """Fetch DASH/HLS manifests and probe CDN variants to list every format."""
    probe_sizes: bool = True
    """Resolve ``Content-Length`` for every format before downloading."""
    include_previews: bool = False
    """Keep the reddit preview renditions (smaller re-encodes) in the results."""
    max_quality: int = 2160
    audio_bitrate_preference: int = 128
    mux_audio: bool = True
    """Mux separate video+audio tracks with ffmpeg when the CDN needs it."""
    resolve_crossposts: bool = True
    """Fetch the original post of a crosspost when its payload is not inline."""

    # ---------------------------------------------------------------- cache
    cache_dir: Optional[Path] = None
    size_cache_ttl: float = 6 * 60 * 60
    use_cache: bool = True

    # -------------------------------------------------------------- naming
    default_pattern: str = "{author}_{id}_{index}_{quality}.{ext}"

    @field_validator("providers")
    @classmethod
    def _check_providers(cls, value: Sequence[str]) -> list[str]:
        cleaned: list[str] = []
        for name in value:
            key = str(name).strip().lower()
            if key not in ALL_PROVIDERS:
                raise ValueError(
                    f"unknown provider {name!r}; choose from {', '.join(ALL_PROVIDERS)}"
                )
            if key not in cleaned:
                cleaned.append(key)
        if not cleaned:
            raise ValueError("at least one provider must be configured")
        return cleaned

    # ------------------------------------------------------------- helpers
    @classmethod
    def from_env(cls, **overrides: Any) -> "RedditConfig":
        """Build a config from ``REDDIT_*`` environment variables."""
        env_map = {
            "client_id": "REDDIT_CLIENT_ID",
            "client_secret": "REDDIT_CLIENT_SECRET",
            "refresh_token": "REDDIT_REFRESH_TOKEN",
            "username": "REDDIT_USERNAME",
            "password": "REDDIT_PASSWORD",
            "access_token": "REDDIT_ACCESS_TOKEN",
            "user_agent": "REDDIT_USER_AGENT",
            "api_user_agent": "REDDIT_API_USER_AGENT",
        }
        data: dict[str, Any] = {}
        for field, var in env_map.items():
            value = os.getenv(var)
            if value:
                data[field] = value
        proxy = os.getenv("HTTPS_PROXY") or os.getenv("https_proxy")
        if proxy:
            data.setdefault("proxy", proxy)
        cache = os.getenv("REDDIT_CACHE_DIR")
        if cache:
            data.setdefault("cache_dir", Path(cache))
        data.update(overrides)
        return cls(**data)

    @property
    def has_oauth_credentials(self) -> bool:
        """``True`` when a token or refresh credentials are configured."""
        if self.access_token:
            return True
        return bool(self.client_id and self.client_secret and (self.refresh_token or (self.username and self.password)))

    def resolved_providers(self) -> list[str]:
        """Provider order with unusable providers removed."""
        out: list[str] = []
        for name in self.providers:
            if name == "oauth" and not self.has_oauth_credentials:
                continue
            out.append(name)
        return out or ["arctic_shift"]

    def with_overrides(self, **overrides: Any) -> "RedditConfig":
        """Return a copy with ``None`` values ignored (handy for wrappers)."""
        data = {k: v for k, v in overrides.items() if v is not None}
        return self.model_copy(update=data)