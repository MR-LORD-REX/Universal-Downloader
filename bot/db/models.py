"""SQLAlchemy 2.0 models backing the bot.

Everything the bot remembers lives here: who talked to it, where, what they
asked for, what the admins changed and what was broadcast. Timestamps are
stored as naive UTC so SQLite round-trips them without surprises.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    """Naive UTC timestamp (SQLite friendly)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    """Declarative base shared by every model and by Alembic's autogenerate."""


class TimestampMixin:
    """Adds `created_at` / `updated_at` to a model."""

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class User(TimestampMixin, Base):
    """A Telegram user seen in a private chat or in a group."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(128))
    last_name: Mapped[str | None] = mapped_column(String(128))
    language_code: Mapped[str | None] = mapped_column(String(16))
    is_premium: Mapped[bool] = mapped_column(Boolean, default=False)

    is_owner: Mapped[bool] = mapped_column(Boolean, default=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    ban_reason: Mapped[str | None] = mapped_column(String(255))
    banned_at: Mapped[datetime | None] = mapped_column(DateTime)

    has_dm_access: Mapped[bool] = mapped_column(Boolean, default=False)
    """True once the user started the bot in private (so we may DM them)."""

    request_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime)

    memberships: Mapped[list["Membership"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    @property
    def display_name(self) -> str:
        parts = [self.first_name, self.last_name]
        name = " ".join(part for part in parts if part)
        return name or (f"@{self.username}" if self.username else str(self.tg_id))

    @property
    def mention(self) -> str:
        return f"@{self.username}" if self.username else self.display_name

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<User {self.tg_id} {self.display_name!r}>"


class Chat(TimestampMixin, Base):
    """A Telegram group, supergroup or channel the bot was added to."""

    __tablename__ = "chats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    type: Mapped[str] = mapped_column(String(16), default="group")
    title: Mapped[str | None] = mapped_column(String(255))
    username: Mapped[str | None] = mapped_column(String(64))

    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    ban_reason: Mapped[str | None] = mapped_column(String(255))
    banned_at: Mapped[datetime | None] = mapped_column(DateTime)

    can_delete_messages: Mapped[bool] = mapped_column(Boolean, default=False)
    request_count: Mapped[int] = mapped_column(Integer, default=0)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime)

    memberships: Mapped[list["Membership"]] = relationship(
        back_populates="chat", cascade="all, delete-orphan"
    )

    @property
    def label(self) -> str:
        return self.title or (f"@{self.username}" if self.username else str(self.tg_id))

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Chat {self.tg_id} {self.label!r}>"


class Membership(TimestampMixin, Base):
    """Which user was seen in which chat (and how often)."""

    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("user_id", "chat_id", name="uq_membership"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"), index=True)
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    request_count: Mapped[int] = mapped_column(Integer, default=0)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime)

    user: Mapped["User"] = relationship(back_populates="memberships")
    chat: Mapped["Chat"] = relationship(back_populates="memberships")


class PlatformSetting(TimestampMixin, Base):
    """Per platform tunables the owner edits from the admin panel."""

    __tablename__ = "platform_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    platform: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    quality: Mapped[str] = mapped_column(String(16), default="best")
    max_media_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    """Reject a single media item larger than this (None = no limit)."""

    fetch_queue_size: Mapped[int] = mapped_column(Integer, default=64)
    fetch_concurrency: Mapped[int] = mapped_column(Integer, default=2)
    fetch_rate_per_second: Mapped[float] = mapped_column(Float, default=1.0)
    processing_queue_size: Mapped[int] = mapped_column(Integer, default=32)
    processing_concurrency: Mapped[int] = mapped_column(Integer, default=1)
    processing_max_ram_bytes: Mapped[int] = mapped_column(BigInteger, default=2 * 1024**3)
    direct_url_video_limit: Mapped[int] = mapped_column(BigInteger, default=20 * 1024 * 1024)
    """Videos under this size are handed to Telegram by url (no processing)."""

    notes: Mapped[str | None] = mapped_column(String(255))

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<PlatformSetting {self.platform} q={self.quality}>"


class UsageEvent(Base):
    """One resolved request - the raw material for the analytics screen."""

    __tablename__ = "usage_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    chat_id: Mapped[int | None] = mapped_column(ForeignKey("chats.id", ondelete="SET NULL"), index=True)
    user_tg_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    chat_tg_id: Mapped[int | None] = mapped_column(BigInteger, index=True)

    platform: Mapped[str] = mapped_column(String(16), index=True, default="unknown")
    url: Mapped[str | None] = mapped_column(Text)
    media_type: Mapped[str | None] = mapped_column(String(24))
    media_group_type: Mapped[str | None] = mapped_column(String(24))
    quality: Mapped[str | None] = mapped_column(String(24))

    item_count: Mapped[int] = mapped_column(Integer, default=0)
    sent_count: Mapped[int] = mapped_column(Integer, default=0)
    bytes_total: Mapped[int] = mapped_column(BigInteger, default=0)

    outcome: Mapped[str] = mapped_column(String(16), default="ok", index=True)
    """One of `ok`, `partial`, `rejected`, `error`, `direct` or `queued`.

    `queued` marks a fetch that handed the work to the processing lane; that
    lane writes the terminal row, so `queued` rows are excluded from the request
    counts in :func:`bot.db.repo.analytics_overview`.
    """

    detail: Mapped[str | None] = mapped_column(String(255))
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    __table_args__ = (Index("ix_usage_platform_created", "platform", "created_at"),)


class Suggestion(Base):
    """A user submitted `/suggestion` forwarded to the owner."""

    __tablename__ = "suggestions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    user_tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="new", index=True)
    admin_note: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Broadcast(TimestampMixin, Base):
    """A message the owner copied to many users/chats."""

    __tablename__ = "broadcasts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    admin_tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    source_chat_id: Mapped[int] = mapped_column(BigInteger)
    source_message_id: Mapped[int] = mapped_column(Integer)
    audience: Mapped[str] = mapped_column(String(16), default="dm")
    """`dm` (users with DM access), `groups` or `all`."""

    status: Mapped[str] = mapped_column(String(16), default="running", index=True)
    total: Mapped[int] = mapped_column(Integer, default=0)
    sent: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    preview: Mapped[str | None] = mapped_column(String(255))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)

    targets: Mapped[list["BroadcastTarget"]] = relationship(
        back_populates="broadcast", cascade="all, delete-orphan"
    )


class BroadcastTarget(Base):
    """Delivery status of one recipient of a broadcast."""

    __tablename__ = "broadcast_targets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    broadcast_id: Mapped[int] = mapped_column(
        ForeignKey("broadcasts.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(8), default="user")
    tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    error: Mapped[str | None] = mapped_column(String(255))
    message_id: Mapped[int | None] = mapped_column(Integer)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)

    broadcast: Mapped["Broadcast"] = relationship(back_populates="targets")


__all__ = [
    "Base",
    "Broadcast",
    "BroadcastTarget",
    "Chat",
    "Membership",
    "PlatformSetting",
    "Suggestion",
    "TimestampMixin",
    "UsageEvent",
    "User",
    "utcnow",
]
