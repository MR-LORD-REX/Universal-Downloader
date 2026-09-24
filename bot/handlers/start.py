"""`/start`, `/help`, `/settings` and group lifecycle notices."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, ChatMemberUpdated, Message

from bot.app import AppContext
from bot.db.models import User
from bot.db.base import db
from bot.db import repo
from bot.ui.keyboards import (
    CAPTION_TOGGLE_CALLBACK,
    caption_of_markup,
    post_markup,
    settings_markup,
)
from bot.ui.style import header, row, rule, section, small_caps
from bot.utils.text import escape, format_count, format_size

router = Router(name="start")


def _intro(ctx: AppContext, *, is_admin: bool) -> str:
    platforms = ", ".join(name.title() for name in ctx.registry.enabled) or "none"
    lines = [
        header(ctx.settings.bot_name),
        rule(),
        row("Platforms", escape(platforms)),
        row("Delivery", "direct link when possible, processed otherwise"),
        "",
        section("how to use"),
        "Send me a link to a post and I will fetch its media.",
        "Images and ready-to-play videos are sent straight from the source CDN.",
        "Anything that needs muxing is queued and uploaded after processing.",
        "",
        section("commands"),
        row("/help", "this message"),
        row("/settings", "your current preferences"),
        row("/suggestion", "send feedback to the owner"),
    ]
    if is_admin:
        lines += [
            "",
            section("admin"),
            row("/admin", "open the control panel"),
            row("/stats", "usage analytics"),
        ]
    lines += [rule(), small_caps(ctx.settings.bot_name)]
    return "\n".join(lines)


@router.message(CommandStart())
async def on_start(message: Message, ctx: AppContext, user: User) -> None:
    await message.answer(_intro(ctx, is_admin=user.is_admin), disable_web_page_preview=True)


@router.message(Command("help"))
async def on_help(message: Message, ctx: AppContext, user: User) -> None:
    await message.answer(_intro(ctx, is_admin=user.is_admin), disable_web_page_preview=True)


@router.message(Command("settings"))
async def on_settings(message: Message, ctx: AppContext, user: User) -> None:
    lines = [
        header("Your Settings"),
        rule(),
        row("Name", escape(user.display_name)),
        row("User ID", user.tg_id),
        row("DM access", "yes" if user.has_dm_access else "no"),
        row("Captions", "on" if user.caption_enabled else "off"),
        row("Requests", format_count(user.request_count)),
        "",
        section("platform defaults"),
    ]
    for snapshot in ctx.registry.all():
        mark = "\u2713" if snapshot.enabled else "\u2717"
        limit = format_size(snapshot.max_media_size_bytes) if snapshot.max_media_size_bytes else "none"
        lines.append(f"{mark} {escape(snapshot.platform.title())} \u00b7 {escape(snapshot.quality)} \u00b7 {limit}")
    lines.append(rule())
    await message.answer(
        "\n".join(lines),
        reply_markup=settings_markup(user.caption_enabled),
        disable_web_page_preview=True,
    )


@router.callback_query(F.data.startswith(CAPTION_TOGGLE_CALLBACK))
async def on_toggle_caption(query: CallbackQuery, user: User) -> None:
    """Flip the caption preference of whoever pressed the button."""
    user.caption_enabled = not user.caption_enabled
    on_post = str(query.data).endswith(":post")
    message = query.message
    markup = (
        post_markup(
            caption_of_markup(getattr(message, "reply_markup", None)),
            caption_enabled=user.caption_enabled,
        )
        if on_post
        else settings_markup(user.caption_enabled)
    )
    if message is not None:
        try:
            await message.edit_reply_markup(reply_markup=markup)
        except Exception:  # noqa: BLE001 - the markup may be too old to edit
            pass
    await query.answer(
        "Captions on \u00b7 new posts show the full description"
        if user.caption_enabled
        else "Captions off \u00b7 new posts show only the bot username"
    )


@router.my_chat_member()
async def on_my_chat_member(event: ChatMemberUpdated, ctx: AppContext) -> None:
    """Record whether we may delete messages in this chat."""
    if event.chat.type == "private":
        return
    member = event.new_chat_member
    can_delete = bool(getattr(member, "can_delete_messages", False))
    try:
        async with db.session() as session:
            await repo.get_or_create_chat(
                session,
                event.chat.id,
                type=str(event.chat.type),
                title=event.chat.title,
                username=getattr(event.chat, "username", None),
                can_delete_messages=can_delete,
            )
    except Exception:  # noqa: BLE001 - non critical bookkeeping
        pass


__all__ = ["router"]
