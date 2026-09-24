"""A cached, session-free view of the per platform settings.

Workers run outside an HTTP request, so they cannot hold an ORM object for
long. :class:`PlatformRegistry` snapshots the rows into plain dataclasses and is
refreshed whenever the admin edits something (and periodically, in case there
are several bot instances).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, Sequence

from bot.config import settings
from bot.db import repo
from bot.db.base import db

from .queues import QueueConfig

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PlatformSnapshot:
    """Immutable copy of a platform_settings row."""

    platform: str
    enabled: bool = True
    quality: str = "best"
    max_media_size_bytes: Optional[int] = None
    fetch_queue_size: int = 64
    fetch_concurrency: int = 2
    fetch_rate_per_second: float = 1.0
    processing_queue_size: int = 32
    processing_concurrency: int = 1
    processing_max_ram_bytes: int = 2 * 1024**3
    direct_url_video_limit: int = 20 * 1024 * 1024

    @classmethod
    def from_row(cls, row: object) -> "PlatformSnapshot":
        return cls(
            platform=str(getattr(row, "platform")),
            enabled=bool(getattr(row, "enabled", True)),
            quality=str(getattr(row, "quality", "best")),
            max_media_size_bytes=getattr(row, "max_media_size_bytes", None),
            fetch_queue_size=int(getattr(row, "fetch_queue_size", 64)),
            fetch_concurrency=int(getattr(row, "fetch_concurrency", 2)),
            fetch_rate_per_second=float(getattr(row, "fetch_rate_per_second", 1.0)),
            processing_queue_size=int(getattr(row, "processing_queue_size", 32)),
            processing_concurrency=int(getattr(row, "processing_concurrency", 1)),
            processing_max_ram_bytes=int(getattr(row, "processing_max_ram_bytes", 2 * 1024**3)),
            direct_url_video_limit=int(getattr(row, "direct_url_video_limit", 20 * 1024**2)),
        )


def _default_snapshot(platform: str) -> PlatformSnapshot:
    return PlatformSnapshot(
        platform=platform,
        fetch_queue_size=settings.fetch_queue_size,
        fetch_concurrency=settings.fetch_concurrency,
        fetch_rate_per_second=settings.fetch_rate_per_second,
        processing_queue_size=settings.processing_queue_size,
        processing_concurrency=settings.processing_concurrency,
        processing_max_ram_bytes=settings.processing_max_ram_bytes,
        direct_url_video_limit=settings.telegram_url_video_limit,
    )


class PlatformRegistry:
    """Keeps the current :class:`PlatformSnapshot` for every platform."""

    def __init__(self, platforms: Sequence[str] = repo.PLATFORMS) -> None:
        self._names = tuple(platforms)
        self._cache: dict[str, PlatformSnapshot] = {
            name: _default_snapshot(name) for name in self._names
        }

    async def refresh(self) -> dict[str, PlatformSnapshot]:
        async with db.session() as session:
            rows = await repo.ensure_platform_settings(session)
            snapshots = [PlatformSnapshot.from_row(row) for row in rows]
        for snapshot in snapshots:
            self._cache[snapshot.platform] = snapshot
        return dict(self._cache)

    def get(self, platform: str) -> PlatformSnapshot:
        return self._cache.get(platform) or _default_snapshot(platform)

    def all(self) -> list[PlatformSnapshot]:
        return [self._cache[name] for name in self._names if name in self._cache]

    def set(self, snapshot: PlatformSnapshot) -> None:
        self._cache[snapshot.platform] = snapshot

    @property
    def names(self) -> tuple[str, ...]:
        return self._names

    @property
    def enabled(self) -> tuple[str, ...]:
        return tuple(name for name, snap in self._cache.items() if snap.enabled)

    # ----------------------------------------------------------- derivation
    def queue_configs(self) -> dict[str, QueueConfig]:
        return {
            name: QueueConfig(
                queue_size=snap.fetch_queue_size,
                concurrency=snap.fetch_concurrency,
                rate_per_second=snap.fetch_rate_per_second,
            )
            for name, snap in self._cache.items()
        }

    def processing_limits(self) -> dict[str, int]:
        return {name: snap.processing_queue_size for name, snap in self._cache.items()}


__all__ = ["PlatformRegistry", "PlatformSnapshot"]
