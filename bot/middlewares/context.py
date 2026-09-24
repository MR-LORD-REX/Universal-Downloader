"""Injects the shared :class:`bot.app.AppContext` into every handler."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject


class ContextMiddleware(BaseMiddleware):
    """Puts ``ctx`` in the handler data (outer middleware, so filters see it)."""

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        data["ctx"] = self.ctx
        return await handler(event, data)


__all__ = ["ContextMiddleware"]
