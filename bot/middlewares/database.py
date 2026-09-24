"""One database session per update."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from bot.db.base import db


class DatabaseMiddleware(BaseMiddleware):
    """Opens a session for the update and commits it when the handler returns."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        async with db.session() as session:
            data["session"] = session
            return await handler(event, data)


__all__ = ["DatabaseMiddleware"]
