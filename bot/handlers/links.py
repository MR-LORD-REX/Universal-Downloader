"""The main handler: a supported link arrives, the work goes on a queue."""

from __future__ import annotations

import logging
import time
from typing import Optional

from aiogram import F, Router
from aiogram.types import Chat, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.app import AppContext
from bot.db import repo
from bot.db.models import Chat as ChatRow
from bot.db.models import User
from bot.filters.links import LinkFilter
from bot.services.queues import FetchJob
from bot.ui.descriptions import build_status
from bot.utils.text import escape

logger = logging.getLogger(__name__)
router = Router(name="links")

USER_COOLDOWN = 2.0
_last_request: dict[int, float] = {}


def _on_cooldown(user_id: int) -> bool:
    now = time.monotonic()
    previous = _last_request.get(user_id)
    _last_request[user_id] = now
    if previous is not None and now - previous < USER_COOLDOWN:
        return True
    if len(_last_request) > 5000:  # pragma: no cover - housekeeping
        for key in [k for k, v in _last_request.items() if now - v > 600]:
            _last_request.pop(key, None)
    return False


async def _delete_incoming(message: Message, ctx: AppContext, chat_row: Optional[ChatRow]) -> None:
    """Remove the user's link once it is accepted (best effort)."""
    if not ctx.settings.delete_incoming_links:
        return
    if message.chat.type == "private":
        return
    if chat_row is not None and chat_row.can_delete_messages is False:
        return
    try:
        await message.delete()
    except Exception as exc:  # noqa: BLE001 - the bot may not be allowed to
        logger.debug("could not delete message in %s: %s", message.chat.id, exc)
        if chat_row is not None:
            chat_row.can_delete_messages = False
    else:
        if chat_row is not None:
            chat_row.can_delete_messages = True


@router.message(F.text | F.caption, LinkFilter())
async def on_link(
    message: Message,
    links: list[tuple[str, str]],
    ctx: AppContext,
    session: AsyncSession,
    user: User,
    chat_row: Optional[ChatRow] = None,
) -> None:
    """Queue a metadata fetch for every supported link in the message."""
    if _on_cooldown(user.id):
        return

    chat: Chat = message.chat
    selected = links[: max(1, ctx.settings.max_links_per_message)]
    reply_to = None if ctx.settings.delete_incoming_links else message.message_id
    thread_id = getattr(message, "message_thread_id", None)

    for platform, url in selected:
        snapshot = ctx.registry.get(platform)
        if not snapshot.enabled:
            continue

        status_id: Optional[int] = None
        if ctx.settings.send_status_message:
            status_id = await ctx.status.create(
                chat.id,
                build_status(platform, "queued", queue_depth=ctx.queues.depth_of(platform)),
                reply_to=reply_to,
                thread_id=thread_id,
            )

        job = FetchJob(
            platform=platform,
            url=url,
            chat_id=chat.id,
            message_id=reply_to,
            thread_id=thread_id,
            user_tg_id=user.tg_id,
            user_db_id=user.id,
            chat_db_id=chat_row.id if chat_row is not None else None,
            status_message_id=status_id,
            caption_enabled=user.caption_enabled,
        )
        accepted, reason = ctx.queues.submit_fetch(job)
        if not accepted:
            if status_id is not None:
                await ctx.status.edit(
                    chat.id, status_id, build_status(platform, "rejected", detail=reason), force=True
                )
            else:
                await message.reply(f"\u25f7 {escape(reason)}\nTry again in a moment.")

    await _delete_incoming(message, ctx, chat_row)


@router.message()
async def on_unhandled(message: Message, ctx: AppContext) -> None:
    """Only speak up in private when the message is not a link at all."""
    if message.chat.type != "private":
        return
    if message.text and message.text.startswith("/"):
        return
    await message.reply(
        "Send me a link from " + ", ".join(name.title() for name in ctx.registry.enabled) + "."
    )


__all__ = ["router"]
