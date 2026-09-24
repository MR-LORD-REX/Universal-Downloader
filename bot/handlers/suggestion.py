"""`/suggestion` - users write to the owner, the owner sees them in the panel."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.app import AppContext
from bot.db import repo
from bot.db.models import User
from bot.ui.admin_views import render_suggestion_notice
from bot.ui.style import header, rule, row, small_caps

logger = logging.getLogger(__name__)
router = Router(name="suggestion")

MAX_SUGGESTION = 1500


class SuggestionStates(StatesGroup):
    waiting_text = State()


@router.message(Command("suggestion", "feedback", "report"))
async def ask_suggestion(message: Message, state: FSMContext, ctx: AppContext) -> None:
    await state.set_state(SuggestionStates.waiting_text)
    await message.answer(
        "\n".join(
            [
                header("Suggestion"),
                rule(),
                row("Send", "your feedback in one message"),
                row("Cancel", "/cancel"),
                "",
                small_caps(ctx.settings.bot_name),
            ]
        )
    )


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext) -> None:
    if await state.get_state() is None:
        return
    await state.clear()
    await message.answer("\u2713 Cancelled.")


@router.message(SuggestionStates.waiting_text, F.text)
async def save_suggestion(
    message: Message, state: FSMContext, ctx: AppContext, session: AsyncSession, user: User
) -> None:
    text = (message.text or "").strip()
    if len(text) < 3:
        await message.reply("Please write a little more than that.")
        return
    row = await repo.create_suggestion(
        session,
        user_tg_id=user.tg_id,
        text=text[:MAX_SUGGESTION],
        user_id=user.id,
        username=user.username,
    )
    await state.clear()
    await message.answer("\u2713 Sent. Thank you!")

    label = user.display_name + (f" (@{user.username})" if user.username else "")
    for admin_id in ctx.settings.all_admin_ids:
        try:
            await ctx.bot.send_message(admin_id, render_suggestion_notice(row, user_label=label))
        except Exception as exc:  # noqa: BLE001 - admin may not have started the bot
            logger.debug("could not forward suggestion to %s: %s", admin_id, exc)


__all__ = ["router"]
