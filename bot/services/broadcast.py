"""Copy one message to many users and chats, politely.

Telegram allows roughly 30 messages per second; the bot stays below that so a
broadcast never trips a flood wait. Delivery status per recipient is written to
the database in batches so an interrupted run still shows partial progress.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional, Sequence

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter

from bot.db import repo
from bot.db.base import db

from .queues import TokenBucket

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int, int], Awaitable[None]]


class BroadcastService:
    """Sends a stored message to a list of `(kind, tg_id)` targets."""

    def __init__(self, bot: Bot, *, rate_per_second: float = 20.0) -> None:
        self._bot = bot
        self._bucket = TokenBucket(rate_per_second, capacity=max(1.0, rate_per_second))

    async def run(
        self,
        *,
        broadcast_id: int,
        source_chat_id: int,
        source_message_id: int,
        targets: Sequence[tuple[str, int]],
        on_progress: Optional[ProgressCallback] = None,
    ) -> tuple[int, int]:
        """Deliver the broadcast; returns `(sent, failed)`."""
        sent = 0
        failed = 0
        total = len(targets)

        async with db.session() as session:
            for position, (kind, tg_id) in enumerate(targets, start=1):
                await self._bucket.acquire()
                status = "sent"
                error: Optional[str] = None
                message_id: Optional[int] = None
                try:
                    message = await self._bot.copy_message(
                        chat_id=tg_id,
                        from_chat_id=source_chat_id,
                        message_id=source_message_id,
                    )
                    message_id = message.message_id
                    sent += 1
                except TelegramRetryAfter as exc:
                    await asyncio.sleep(min(getattr(exc, "retry_after", 5) or 5, 60))
                    try:
                        message = await self._bot.copy_message(
                            chat_id=tg_id,
                            from_chat_id=source_chat_id,
                            message_id=source_message_id,
                        )
                        message_id = message.message_id
                        sent += 1
                    except TelegramAPIError as inner:
                        status, error, failed = "failed", _short(inner), failed + 1
                except TelegramAPIError as exc:
                    status, error, failed = "failed", _short(exc), failed + 1

                await repo.mark_broadcast_target(
                    session,
                    broadcast_id,
                    tg_id,
                    status=status,
                    error=error,
                    message_id=message_id,
                )
                if position % 10 == 0:
                    await session.commit()
                    if on_progress is not None:
                        await on_progress(position, sent, failed)
            await session.commit()

        async with db.session() as session:
            await repo.finish_broadcast(session, broadcast_id, sent=sent, failed=failed)
        if on_progress is not None:
            await on_progress(total, sent, failed)
        return sent, failed


def _short(exc: BaseException, limit: int = 200) -> str:
    return " ".join(str(exc).split())[:limit]


__all__ = ["BroadcastService"]
