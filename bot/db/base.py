"""Async engine/session plumbing shared by the bot and its handlers."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Optional

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from bot.config import settings

from .models import Base


def _prepare_sqlite(url: str) -> str:
    """Make sure the sqlite file's parent directory exists before connecting."""
    prefix = "sqlite+aiosqlite:///"
    if not url.startswith(prefix):
        return url
    raw = url[len(prefix) :]
    if raw in (":memory:", "") or raw.startswith("file:"):
        return url
    Path(raw).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    return url


class Database:
    """Owns the async engine and hands out sessions."""

    def __init__(self, url: Optional[str] = None, *, echo: bool = False) -> None:
        self.url = _prepare_sqlite(url or settings.database_url)
        self.engine: AsyncEngine = create_async_engine(
            self.url,
            echo=echo,
            future=True,
            pool_pre_ping=True,
        )
        self.session_factory = async_sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )

    async def create_all(self) -> None:
        """Create every table (used by tests; production uses Alembic)."""
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def drop_all(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)

    async def dispose(self) -> None:
        await self.engine.dispose()

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Transactional session scope: commit on success, rollback on error."""
        session = self.session_factory()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


db = Database()

__all__ = ["Database", "db"]
