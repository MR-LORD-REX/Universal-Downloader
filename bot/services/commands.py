"""Automatic `/setMyCommands` registration (no BotFather trip needed)."""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

logger = logging.getLogger(__name__)

PUBLIC_COMMANDS: tuple[tuple[str, str], ...] = (
    ("start", "Start the bot and see how it works"),
    ("help", "How to use the bot"),
    ("settings", "Show your current preferences"),
    ("suggestion", "Send a suggestion or report a problem"),
)

ADMIN_COMMANDS: tuple[tuple[str, str], ...] = (
    ("admin", "Open the admin panel"),
    ("stats", "Usage analytics"),
    ("broadcast", "Broadcast a message"),
    ("users", "Browse users"),
    ("chats", "Browse groups"),
    ("platforms", "Per-platform limits and quality"),
    ("queues", "Live queue and RAM status"),
)


def _commands(pairs: tuple[tuple[str, str], ...]) -> list[BotCommand]:
    return [BotCommand(command=name, description=description) for name, description in pairs]


async def register_commands(bot: Bot, *, admin_ids: list[int]) -> None:
    """Publish the public command list and a richer list for each admin."""
    try:
        await bot.set_my_commands(_commands(PUBLIC_COMMANDS), scope=BotCommandScopeDefault())
    except TelegramAPIError as exc:  # pragma: no cover - network dependent
        logger.warning("could not publish default commands: %s", exc)
        return

    for admin_id in admin_ids:
        try:
            await bot.set_my_commands(
                _commands(PUBLIC_COMMANDS + ADMIN_COMMANDS),
                scope=BotCommandScopeChat(chat_id=admin_id),
            )
        except TelegramAPIError as exc:  # pragma: no cover - admin may not have started the bot
            logger.debug("could not publish admin commands for %s: %s", admin_id, exc)


async def clear_commands(bot: Bot) -> None:
    try:
        await bot.delete_my_commands(scope=BotCommandScopeDefault())
    except TelegramAPIError:  # pragma: no cover - best effort
        pass


__all__ = [
    "ADMIN_COMMANDS",
    "PUBLIC_COMMANDS",
    "clear_commands",
    "register_commands",
]
