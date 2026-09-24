"""Environment driven configuration for the bot.

Every setting is read from the process environment, falling back to a `.env`
file at the repository root. Complex values (id lists) accept a comma
separated list so `.env` stays readable.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


def _split(value: Any) -> list[Any]:
    """Accept `1,2,3`, `[1, 2]` and a bare scalar for list settings."""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    text = str(value).strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            decoded = json.loads(text)
        except ValueError:
            decoded = None
        if isinstance(decoded, list):
            return decoded
    return [part.strip() for part in text.split(",") if part.strip()]


IdList = Annotated[list[int], NoDecode]


class Settings(BaseSettings):
    """Strongly typed view of the bot's environment."""

    model_config = SettingsConfigDict(
        env_file=ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ------------------------------------------------------------ telegram
    bot_token: str = ""
    owner_id: int = 0
    admin_ids: IdList = Field(default_factory=list)
    bot_name: str = "Downloader"
    support_username: str = ""

    bot_mode: Literal["polling", "webhook"] = "polling"
    webhook_url: str = ""
    """Public base url, e.g. `https://bot.example.com` (webhook mode)."""
    webhook_path: str = "/telegram/webhook"
    webhook_secret: str = ""
    host: str = "0.0.0.0"
    port: int = 8080
    drop_pending_updates: bool = False
    register_commands: bool = True

    # ----------------------------------------------------------- behaviour
    delete_incoming_links: bool = True
    """Ask Telegram to remove the user's link message once it is accepted."""
    max_links_per_message: int = 1
    send_status_message: bool = True
    reject_banned_silently: bool = True

    telegram_url_photo_limit: int = 5 * 1024 * 1024
    """Bot API refuses to fetch a `photo` url larger than this."""
    telegram_url_video_limit: int = 20 * 1024 * 1024
    """Bot API refuses to fetch a `video` url larger than this."""
    telegram_upload_limit: int = 50 * 1024 * 1024
    """Bot API rejects uploaded files above this (2 GB on a local server)."""
    album_chunk_size: int = 10
    """Telegram media groups hold at most 10 items."""

    # -------------------------------------------------------------- queues
    fetch_queue_size: int = 64
    fetch_concurrency: int = 2
    fetch_rate_per_second: float = 1.0
    """Metadata requests per second, per platform (be a good citizen)."""

    processing_queue_size: int = 32
    processing_concurrency: int = 1
    processing_max_ram_bytes: int = 2 * 1024 * 1024 * 1024
    """RAM the processing queue may reserve at once (bytes)."""
    processing_ram_factor: float = 2.5
    """Peak extra RAM a muxing job needs, as a multiple of the media size."""
    processing_default_estimate_bytes: int = 128 * 1024 * 1024
    """Assumed size when metadata cannot report one."""

    # ---------------------------------------------------------- downloader
    download_dir: Path = ROOT / "data" / "downloads"
    temp_dir: Path = ROOT / "data" / "tmp"
    proxy: str = ""
    cookies_file: Optional[Path] = None
    request_timeout: float = 30.0

    # ------------------------------------------------------------ database
    database_url: str = "sqlite+aiosqlite:///./data/bot.db"
    auto_migrate: bool = True
    db_echo: bool = False

    # --------------------------------------------------------------- misc
    log_level: str = "INFO"
    broadcast_rate_per_second: float = 20.0

    # ---------------------------------------------------------- validators
    @field_validator("admin_ids", mode="before")
    @classmethod
    def _ids(cls, value: Any) -> Any:
        return [int(item) for item in _split(value)]

    @field_validator("cookies_file", mode="before")
    @classmethod
    def _blank_path(cls, value: Any) -> Any:
        """`COOKIES_FILE=` in .env means "no cookies", not the working directory.

        Without this an empty value parses to ``Path('.')``, which is then
        handed to yt-dlp as a cookie *file*; every extraction dies with
        ``[Errno 13] Permission denied: '.'``.
        """
        if value is None:
            return None
        if isinstance(value, Path):
            return None if str(value) == "." else value
        text = str(value).strip()
        return None if text in {"", "."} else text

    @field_validator("download_dir", "temp_dir", "cookies_file", mode="after")
    @classmethod
    def _absolute(cls, value: Optional[Path]) -> Optional[Path]:
        if value is None:
            return None
        return value if value.is_absolute() else (ROOT / value).resolve()

    # ------------------------------------------------------------ helpers
    @property
    def all_admin_ids(self) -> list[int]:
        """Owner first, then extra admins, without duplicates."""
        seen: list[int] = []
        for candidate in [self.owner_id, *self.admin_ids]:
            if candidate and candidate not in seen:
                seen.append(candidate)
        return seen

    def is_admin(self, user_id: Optional[int]) -> bool:
        return bool(user_id) and user_id in self.all_admin_ids

    def is_owner(self, user_id: Optional[int]) -> bool:
        return bool(user_id) and user_id == self.owner_id

    @property
    def webhook_target(self) -> str:
        base = self.webhook_url.rstrip("/")
        path = self.webhook_path if self.webhook_path.startswith("/") else "/" + self.webhook_path
        return f"{base}{path}"

    def ensure_directories(self) -> None:
        for directory in (self.download_dir, self.temp_dir):
            directory.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process wide cached settings instance."""
    return Settings()


settings = get_settings()

__all__ = ["ROOT", "Settings", "get_settings", "settings"]
