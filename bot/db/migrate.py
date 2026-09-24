"""Programmatic Alembic entry point used by the app at start-up."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[2]


def sync_url() -> str:
    """The migrations run through a *sync* driver against the same file."""
    from bot.config import settings

    return (
        settings.database_url.replace("+aiosqlite", "")
        .replace("+asyncpg", "+psycopg")
        .replace("+aiomysql", "+pymysql")
    )


def alembic_config() -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "bot" / "alembic"))
    return config


def upgrade_sync(revision: str = "head") -> None:
    """Blocking `alembic upgrade` - call it through :func:`upgrade`."""
    command.upgrade(alembic_config(), revision)


async def upgrade(revision: str = "head") -> None:
    """Run migrations without blocking the event loop."""
    await asyncio.to_thread(upgrade_sync, revision)


def current_revision() -> Optional[str]:
    """The revision currently applied to the database (None when fresh)."""
    engine = create_engine(sync_url())
    try:
        with engine.connect() as connection:
            row = connection.execute(text("select version_num from alembic_version")).first()
            return row[0] if row else None
    except Exception:
        return None
    finally:
        engine.dispose()


__all__ = ["alembic_config", "current_revision", "sync_url", "upgrade", "upgrade_sync"]
