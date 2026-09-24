"""Keep the users/chats registry fresh and enforce bans."""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, ChatMemberUpdated, Message, TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo

logger = logging.getLogger(__name__)


def _is_private(chat_type: str) -> bool:
    return chat_type == "private"


class UserMiddleware(BaseMiddleware):
    """Upsert the sender and the chat, record `has_dm_access`, then apply bans."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        session: AsyncSession | None = data.get("session")
        if session is None:
            return await handler(event, data)

        message = event if isinstance(event, Message) else None
        callback = event if isinstance(event, CallbackQuery) else None
        if callback is not None:
            message = callback.message if isinstance(callback.message, Message) else None

        tg_user = None
        chat = None
        if message is not None:
            tg_user = message.from_user
            chat = message.chat
        elif callback is not None:
            tg_user = callback.from_user
            chat = message.chat if message is not None else None

        if tg_user is None or getattr(tg_user, "is_bot", False):
            return await handler(event, data)

        has_dm = bool(chat is not None and _is_private(str(chat.type)))
        user = await repo.get_or_create_user(
            session,
            tg_user.id,
            username=tg_user.username,
            first_name=tg_user.first_name,
            last_name=tg_user.last_name,
            language_code=tg_user.language_code,
            is_premium=getattr(tg_user, "is_premium", None),
            has_dm_access=has_dm,
        )
        data["user"] = user

        if chat is not None and not _is_private(str(chat.type)):
            chat_row = await repo.get_or_create_chat(
                session,
                chat.id,
                type=str(chat.type),
                title=chat.title,
                username=getattr(chat, "username", None),
            )
            data["chat_row"] = chat_row
            await repo.touch_membership(session, user, chat_row)
            if chat_row.is_banned and not user.is_admin:
                return None

        if user.is_banned and not user.is_admin:
            if _is_private(str(chat.type) if chat else "private"):
                await _notify_once(message, user)
            return None

        return await handler(event, data)


async def _notify_once(message: Message | None, user: Any) -> None:
    """Tell a banned user once per session, then stay quiet."""
    if message is None or getattr(user, "_notified", False):
        return
    try:
        await message.reply("\u2717 You are not allowed to use this bot.")
    except Exception:  # noqa: BLE001 - best effort
        pass


__all__ = ["UserMiddleware"]
