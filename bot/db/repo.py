"""Repository helpers: every query the handlers need, in one place.

Functions take an `AsyncSession` and never commit - the caller's session scope
(:meth:`bot.db.base.Database.session`) owns the transaction.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Iterable, Optional, Sequence

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings

from .models import (
    Broadcast,
    BroadcastTarget,
    Chat,
    Membership,
    PlatformSetting,
    Suggestion,
    UsageEvent,
    User,
    utcnow,
)

PLATFORMS: tuple[str, ...] = ("reddit", "youtube", "twitter")


# ------------------------------------------------------------------- users
async def get_user(session: AsyncSession, tg_id: int) -> Optional[User]:
    result = await session.execute(select(User).where(User.tg_id == tg_id))
    return result.scalar_one_or_none()


async def get_or_create_user(
    session: AsyncSession,
    tg_id: int,
    *,
    username: Optional[str] = None,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    language_code: Optional[str] = None,
    is_premium: Optional[bool] = None,
    has_dm_access: Optional[bool] = None,
) -> User:
    """Upsert the user and refresh the profile fields Telegram just gave us."""
    user = await get_user(session, tg_id)
    if user is None:
        user = User(tg_id=tg_id, request_count=0, failed_count=0)
        session.add(user)
    if username is not None:
        user.username = username
    if first_name is not None:
        user.first_name = first_name
    if last_name is not None:
        user.last_name = last_name
    if language_code is not None:
        user.language_code = language_code
    if is_premium is not None:
        user.is_premium = is_premium
    if has_dm_access:
        user.has_dm_access = True
    user.is_owner = tg_id == settings.owner_id
    user.is_admin = settings.is_admin(tg_id)
    user.last_seen_at = utcnow()
    await session.flush()
    return user


async def get_or_create_chat(
    session: AsyncSession,
    tg_id: int,
    *,
    type: str = "group",
    title: Optional[str] = None,
    username: Optional[str] = None,
    can_delete_messages: Optional[bool] = None,
) -> Chat:
    result = await session.execute(select(Chat).where(Chat.tg_id == tg_id))
    chat = result.scalar_one_or_none()
    if chat is None:
        chat = Chat(tg_id=tg_id, type=type, request_count=0)
        session.add(chat)
    if title is not None:
        chat.title = title
    if username is not None:
        chat.username = username
    if can_delete_messages is not None:
        chat.can_delete_messages = can_delete_messages
    chat.type = type or chat.type
    chat.last_seen_at = utcnow()
    await session.flush()
    return chat


async def touch_membership(session: AsyncSession, user: User, chat: Chat) -> Membership:
    """Remember that `user` was active in `chat`."""
    result = await session.execute(
        select(Membership).where(Membership.user_id == user.id, Membership.chat_id == chat.id)
    )
    membership = result.scalar_one_or_none()
    if membership is None:
        membership = Membership(user_id=user.id, chat_id=chat.id)
        session.add(membership)
    membership.last_seen_at = utcnow()
    return membership


async def bump_user_requests(session: AsyncSession, tg_id: int, *, failed: bool = False) -> None:
    values: dict[str, Any] = {"request_count": User.request_count + 1}
    if failed:
        values["failed_count"] = User.failed_count + 1
    await session.execute(update(User).where(User.tg_id == tg_id).values(**values))


async def bump_chat_requests(session: AsyncSession, tg_id: int) -> None:
    await session.execute(
        update(Chat).where(Chat.tg_id == tg_id).values(request_count=Chat.request_count + 1)
    )


async def set_user_banned(
    session: AsyncSession, tg_id: int, banned: bool, reason: Optional[str] = None
) -> Optional[User]:
    user = await get_user(session, tg_id)
    if user is None:
        return None
    user.is_banned = banned
    user.ban_reason = reason if banned else None
    user.banned_at = utcnow() if banned else None
    return user


async def get_chat(session: AsyncSession, tg_id: int) -> Optional[Chat]:
    result = await session.execute(select(Chat).where(Chat.tg_id == tg_id))
    return result.scalar_one_or_none()


async def set_chat_banned(
    session: AsyncSession, tg_id: int, banned: bool, reason: Optional[str] = None
) -> Optional[Chat]:
    result = await session.execute(select(Chat).where(Chat.tg_id == tg_id))
    chat = result.scalar_one_or_none()
    if chat is None:
        return None
    chat.is_banned = banned
    chat.ban_reason = reason if banned else None
    chat.banned_at = utcnow() if banned else None
    return chat


async def search_users(
    session: AsyncSession, *, query: Optional[str] = None, offset: int = 0, limit: int = 10
) -> tuple[list[User], int]:
    statement = select(User)
    counter = select(func.count(User.id))
    if query:
        text = query.strip().lstrip("@")
        pattern = f"%{text}%"
        clause = or_(
            User.username.ilike(pattern),
            User.first_name.ilike(pattern),
            User.last_name.ilike(pattern),
        )
        if text.isdigit():
            clause = or_(clause, User.tg_id == int(text))
        statement = statement.where(clause)
        counter = counter.where(clause)
    statement = statement.order_by(User.request_count.desc(), User.tg_id).offset(offset).limit(limit)
    rows = (await session.execute(statement)).scalars().all()
    total = (await session.execute(counter)).scalar_one()
    return list(rows), int(total)


async def list_chats(
    session: AsyncSession, *, offset: int = 0, limit: int = 10
) -> tuple[list[Chat], int]:
    statement = (
        select(Chat).order_by(Chat.request_count.desc(), Chat.tg_id).offset(offset).limit(limit)
    )
    rows = (await session.execute(statement)).scalars().all()
    total = (await session.execute(select(func.count(Chat.id)))).scalar_one()
    return list(rows), int(total)


async def dm_user_ids(session: AsyncSession) -> list[int]:
    """Every non-banned user that can be reached in private."""
    statement = select(User.tg_id).where(User.has_dm_access.is_(True), User.is_banned.is_(False))
    return [int(row) for row in (await session.execute(statement)).scalars().all()]


async def reachable_chat_ids(session: AsyncSession) -> list[int]:
    statement = select(Chat.tg_id).where(Chat.is_banned.is_(False))
    return [int(row) for row in (await session.execute(statement)).scalars().all()]


# -------------------------------------------------------- platform settings
DEFAULT_PLATFORM_SETTINGS: dict[str, dict[str, Any]] = {
    "youtube": {"quality": "1080p", "fetch_rate_per_second": 0.5},
    "twitter": {"quality": "1080p", "fetch_rate_per_second": 1.0},
    "reddit": {"quality": "1080p", "fetch_rate_per_second": 1.0},
}


async def ensure_platform_settings(session: AsyncSession) -> list[PlatformSetting]:
    """Create a row per platform (idempotent) and return them all."""
    existing = {
        row.platform: row
        for row in (await session.execute(select(PlatformSetting))).scalars().all()
    }
    for platform in PLATFORMS:
        if platform in existing:
            continue
        defaults: dict[str, Any] = {
            "platform": platform,
            "max_media_size_bytes": None,
            "fetch_queue_size": settings.fetch_queue_size,
            "fetch_concurrency": settings.fetch_concurrency,
            "fetch_rate_per_second": settings.fetch_rate_per_second,
            "processing_queue_size": settings.processing_queue_size,
            "processing_concurrency": settings.processing_concurrency,
            "processing_max_ram_bytes": settings.processing_max_ram_bytes,
            "direct_url_video_limit": settings.telegram_url_video_limit,
        }
        defaults.update(DEFAULT_PLATFORM_SETTINGS.get(platform, {}))
        row = PlatformSetting(**defaults)
        session.add(row)
        existing[platform] = row
    await session.flush()
    return [existing[name] for name in PLATFORMS]


async def get_platform_setting(
    session: AsyncSession, platform: str, *, create: bool = True
) -> Optional[PlatformSetting]:
    result = await session.execute(
        select(PlatformSetting).where(PlatformSetting.platform == platform)
    )
    row = result.scalar_one_or_none()
    if row is None and create:
        await ensure_platform_settings(session)
        result = await session.execute(
            select(PlatformSetting).where(PlatformSetting.platform == platform)
        )
        row = result.scalar_one_or_none()
    return row


async def update_platform_setting(
    session: AsyncSession, platform: str, **values: Any
) -> Optional[PlatformSetting]:
    row = await get_platform_setting(session, platform)
    if row is None:
        return None
    for key, value in values.items():
        if hasattr(row, key):
            setattr(row, key, value)
    return row


# --------------------------------------------------------------- analytics
async def record_usage(
    session: AsyncSession,
    *,
    platform: str,
    outcome: str,
    user_tg_id: Optional[int] = None,
    chat_tg_id: Optional[int] = None,
    user_id: Optional[int] = None,
    chat_id: Optional[int] = None,
    url: Optional[str] = None,
    media_type: Optional[str] = None,
    media_group_type: Optional[str] = None,
    quality: Optional[str] = None,
    item_count: int = 0,
    sent_count: int = 0,
    bytes_total: int = 0,
    detail: Optional[str] = None,
    duration_ms: int = 0,
) -> UsageEvent:
    event = UsageEvent(
        platform=platform,
        outcome=outcome,
        user_tg_id=user_tg_id,
        chat_tg_id=chat_tg_id,
        user_id=user_id,
        chat_id=chat_id,
        url=url,
        media_type=media_type,
        media_group_type=media_group_type,
        quality=quality,
        item_count=item_count,
        sent_count=sent_count,
        bytes_total=bytes_total,
        detail=(detail or None) and detail[:255],
        duration_ms=duration_ms,
    )
    session.add(event)
    return event


async def analytics_overview(session: AsyncSession, *, days: int = 7) -> dict[str, Any]:
    """Everything the analytics screen shows, in one round trip."""
    since = utcnow() - timedelta(days=days)
    day_ago = utcnow() - timedelta(days=1)

    # "queued" rows mark a fetch that handed the work to the processing lane;
    # the processing worker records the real outcome, so counting both would
    # report one download request as two.
    terminal = UsageEvent.outcome != "queued"

    totals = (
        await session.execute(
            select(
                func.count(UsageEvent.id),
                func.coalesce(func.sum(UsageEvent.bytes_total), 0),
                func.coalesce(func.sum(UsageEvent.sent_count), 0),
            ).where(UsageEvent.created_at >= since, terminal)
        )
    ).one()

    per_platform = (
        await session.execute(
            select(
                UsageEvent.platform,
                func.count(UsageEvent.id),
                func.coalesce(func.sum(UsageEvent.bytes_total), 0),
            )
            .where(UsageEvent.created_at >= since, terminal)
            .group_by(UsageEvent.platform)
            .order_by(func.count(UsageEvent.id).desc())
        )
    ).all()

    outcomes = (
        await session.execute(
            select(UsageEvent.outcome, func.count(UsageEvent.id))
            .where(UsageEvent.created_at >= since)
            .group_by(UsageEvent.outcome)
        )
    ).all()

    return {
        "window_days": days,
        "requests": int(totals[0] or 0),
        "bytes": int(totals[1] or 0),
        "sent": int(totals[2] or 0),
        "users": int((await session.execute(select(func.count(User.id)))).scalar_one()),
        "users_24h": int(
            (
                await session.execute(
                    select(func.count(User.id)).where(User.last_seen_at >= day_ago)
                )
            ).scalar_one()
        ),
        "dm_reachable": int(
            (
                await session.execute(
                    select(func.count(User.id)).where(User.has_dm_access.is_(True))
                )
            ).scalar_one()
        ),
        "banned_users": int(
            (
                await session.execute(
                    select(func.count(User.id)).where(User.is_banned.is_(True))
                )
            ).scalar_one()
        ),
        "chats": int((await session.execute(select(func.count(Chat.id)))).scalar_one()),
        "banned_chats": int(
            (
                await session.execute(select(func.count(Chat.id)).where(Chat.is_banned.is_(True)))
            ).scalar_one()
        ),
        "new_users_24h": int(
            (
                await session.execute(
                    select(func.count(User.id)).where(User.created_at >= day_ago)
                )
            ).scalar_one()
        ),
        "per_platform": [
            {"platform": row[0], "requests": int(row[1]), "bytes": int(row[2])}
            for row in per_platform
        ],
        "outcomes": {row[0]: int(row[1]) for row in outcomes},
        "pending_suggestions": int(
            (
                await session.execute(
                    select(func.count(Suggestion.id)).where(Suggestion.status == "new")
                )
            ).scalar_one()
        ),
    }


# -------------------------------------------------------------- suggestions
async def create_suggestion(
    session: AsyncSession,
    *,
    user_tg_id: int,
    text: str,
    user_id: Optional[int] = None,
    username: Optional[str] = None,
) -> Suggestion:
    row = Suggestion(user_tg_id=user_tg_id, text=text, user_id=user_id, username=username)
    session.add(row)
    await session.flush()
    return row


async def list_suggestions(
    session: AsyncSession, *, status: Optional[str] = "new", limit: int = 10
) -> list[Suggestion]:
    statement = select(Suggestion).order_by(Suggestion.created_at.desc()).limit(limit)
    if status:
        statement = statement.where(Suggestion.status == status)
    return list((await session.execute(statement)).scalars().all())


# --------------------------------------------------------------- broadcasts
async def create_broadcast(
    session: AsyncSession,
    *,
    admin_tg_id: int,
    source_chat_id: int,
    source_message_id: int,
    audience: str,
    targets: Sequence[tuple[str, int]],
    preview: Optional[str] = None,
) -> Broadcast:
    broadcast = Broadcast(
        admin_tg_id=admin_tg_id,
        source_chat_id=source_chat_id,
        source_message_id=source_message_id,
        audience=audience,
        total=len(targets),
        preview=(preview or None) and preview[:255],
    )
    session.add(broadcast)
    await session.flush()
    for kind, tg_id in targets:
        session.add(BroadcastTarget(broadcast_id=broadcast.id, kind=kind, tg_id=tg_id))
    await session.flush()
    return broadcast


async def mark_broadcast_target(
    session: AsyncSession,
    broadcast_id: int,
    tg_id: int,
    *,
    status: str,
    error: Optional[str] = None,
    message_id: Optional[int] = None,
) -> None:
    await session.execute(
        update(BroadcastTarget)
        .where(BroadcastTarget.broadcast_id == broadcast_id, BroadcastTarget.tg_id == tg_id)
        .values(status=status, error=error, message_id=message_id, sent_at=utcnow())
    )


async def finish_broadcast(
    session: AsyncSession, broadcast_id: int, *, sent: int, failed: int
) -> None:
    await session.execute(
        update(Broadcast)
        .where(Broadcast.id == broadcast_id)
        .values(status="done", sent=sent, failed=failed, finished_at=utcnow())
    )


async def purge_user(session: AsyncSession, tg_id: int) -> None:
    """Delete a user and every row that references it."""
    user = await get_user(session, tg_id)
    if user is None:
        return
    await session.execute(delete(Membership).where(Membership.user_id == user.id))
    await session.execute(delete(User).where(User.id == user.id))


__all__ = [
    "DEFAULT_PLATFORM_SETTINGS",
    "PLATFORMS",
    "analytics_overview",
    "bump_chat_requests",
    "bump_user_requests",
    "create_broadcast",
    "create_suggestion",
    "dm_user_ids",
    "ensure_platform_settings",
    "finish_broadcast",
    "get_chat",
    "get_or_create_chat",
    "get_or_create_user",
    "get_platform_setting",
    "get_user",
    "list_chats",
    "list_suggestions",
    "mark_broadcast_target",
    "purge_user",
    "reachable_chat_ids",
    "record_usage",
    "search_users",
    "set_chat_banned",
    "set_user_banned",
    "touch_membership",
    "update_platform_setting",
]
