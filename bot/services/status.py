"""Throttled editing of the little "working on it" message."""

from __future__ import annotations

import logging
import time
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest

logger = logging.getLogger(__name__)


class StatusReporter:
    """Sends and edits status messages without tripping Telegram's rate limits."""

    def __init__(self, bot: Bot, *, min_interval: float = 3.0) -> None:
        self._bot = bot
        self.min_interval = min_interval
        self._last: dict[tuple[int, int], tuple[float, str]] = {}

    async def create(
        self,
        chat_id: int,
        text: str,
        *,
        reply_to: Optional[int] = None,
        thread_id: Optional[int] = None,
    ) -> Optional[int]:
        """Send the status message; returns its id (None when it could not be sent)."""
        kwargs: dict = {}
        if reply_to is not None:
            from aiogram.types import ReplyParameters

            kwargs["reply_parameters"] = ReplyParameters(
                message_id=reply_to, allow_sending_without_reply=True
            )
        if thread_id is not None:
            kwargs["message_thread_id"] = thread_id
        try:
            message = await self._bot.send_message(chat_id, text, **kwargs)
        except TelegramAPIError as exc:
            logger.debug("cannot send status message: %s", exc)
            return None
        self._last[(chat_id, message.message_id)] = (time.monotonic(), text)
        return message.message_id

    async def edit(
        self, chat_id: int, message_id: Optional[int], text: str, *, force: bool = False
    ) -> None:
        """Edit the status message, skipping writes that are too close together."""
        if message_id is None:
            return
        key = (chat_id, message_id)
        previous = self._last.get(key)
        now = time.monotonic()
        if previous is not None and not force:
            if previous[1] == text or now - previous[0] < self.min_interval:
                return
        try:
            await self._bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text)
            self._last[key] = (now, text)
        except TelegramBadRequest as exc:
            if "not modified" not in str(exc).lower():
                logger.debug("status edit failed: %s", exc)
        except TelegramAPIError as exc:
            logger.debug("status edit failed: %s", exc)

    async def drop(self, chat_id: int, message_id: Optional[int]) -> None:
        """Remove the status message once the media has been delivered."""
        if message_id is None:
            return
        self._last.pop((chat_id, message_id), None)
        try:
            await self._bot.delete_message(chat_id=chat_id, message_id=message_id)
        except TelegramAPIError as exc:
            logger.debug("status delete failed: %s", exc)


__all__ = ["StatusReporter"]
