"""The owner's control panel: users, groups, analytics, limits, broadcasts."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import BaseFilter, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.app import AppContext
from bot.db import repo
from bot.db.models import User
from bot.services.platforms import PlatformSnapshot
from bot.ui import admin_views as views
from bot.ui import keyboards as kb
from bot.utils.text import escape

logger = logging.getLogger(__name__)
router = Router(name="admin")


class IsAdmin(BaseFilter):
    """Only the owner and the configured admins may use this router."""

    async def __call__(self, event: Message | CallbackQuery, ctx: AppContext) -> bool:
        user = getattr(event, "from_user", None)
        return bool(user and ctx.settings.is_admin(user.id))


router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


class AdminStates(StatesGroup):
    platform_value = State()
    user_search = State()
    user_message = State()
    broadcast_source = State()


# ------------------------------------------------------------------ helpers
async def _counts(ctx: AppContext) -> dict[str, int]:
    async with db.session() as session:
        data = await repo.analytics_overview(session, days=7)
    return {
        "users": data["users"],
        "chats": data["chats"],
        "dm": data["dm_reachable"],
        "banned": data["banned_users"],
        "suggestions": data["pending_suggestions"],
    }


async def _home_text(ctx: AppContext) -> str:
    return views.render_home(
        owner_id=ctx.settings.owner_id,
        platforms=ctx.registry.all(),
        counts=await _counts(ctx),
    )


async def _show(callback: CallbackQuery, text: str, markup: Any = None) -> None:
    try:
        await callback.message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=markup, disable_web_page_preview=True)


async def _users_page(session: AsyncSession, ctx: AppContext, page: int, query: Optional[str] = None):
    users, total = await repo.search_users(session, query=query, offset=page * kb.PER_PAGE, limit=kb.PER_PAGE)
    return views.render_users(users, page=page, total=total), kb.users_page(users, page=page, total=total), total


async def _chats_page(session: AsyncSession, page: int):
    chats, total = await repo.list_chats(session, offset=page * kb.PER_PAGE, limit=kb.PER_PAGE)
    return views.render_chats(chats, page=page, total=total), kb.chats_page(chats, page=page, total=total)


async def _platform_rows(session: AsyncSession):
    return await repo.ensure_platform_settings(session)


def _parse_value(field: str, raw: str) -> Any:
    text = raw.strip().lower()
    if field == "enabled":
        return text in ("1", "true", "yes", "on", "enable", "enabled")
    if field == "quality":
        return raw.strip()
    if field in ("max_media_size_bytes", "processing_max_ram_bytes", "direct_url_video_limit"):
        if text in ("none", "null", "off", "unlimited"):
            return None
        multiplier = 1
        for suffix, factor in (("gb", 1024**3), ("mb", 1024**2), ("kb", 1024)):
            if text.endswith(suffix):
                multiplier = factor
                text = text[: -len(suffix)].strip()
                break
        return int(float(text) * multiplier)
    if field == "fetch_rate_per_second":
        return float(text)
    return int(float(text))


# ----------------------------------------------------------------- commands
@router.message(Command("admin", "panel"))
async def cmd_admin(message: Message, ctx: AppContext) -> None:
    await message.answer(
        await _home_text(ctx), reply_markup=kb.admin_home(is_owner=ctx.settings.is_owner(message.from_user.id))
    )


@router.message(Command("stats"))
async def cmd_stats(message: Message, session: AsyncSession, ctx: AppContext) -> None:
    data = await repo.analytics_overview(session, days=7)
    await message.answer(
        views.render_analytics(data, queues=ctx.queues.snapshot()),
        reply_markup=kb.analytics_kb(window=7),
    )


@router.message(Command("users"))
async def cmd_users(message: Message, session: AsyncSession, ctx: AppContext) -> None:
    text, markup, _ = await _users_page(session, ctx, 0)
    await message.answer(text, reply_markup=markup)


@router.message(Command("chats"))
async def cmd_chats(message: Message, session: AsyncSession) -> None:
    text, markup = await _chats_page(session, 0)
    await message.answer(text, reply_markup=markup)


@router.message(Command("platforms"))
async def cmd_platforms(message: Message, session: AsyncSession) -> None:
    rows = await _platform_rows(session)
    await message.answer(views.render_platforms(rows), reply_markup=kb.platforms_kb(rows))


@router.message(Command("queues"))
async def cmd_queues(message: Message, ctx: AppContext) -> None:
    await message.answer(
        views.render_runtime(queues=ctx.queues.snapshot(), registry=ctx.registry, settings=ctx.settings),
        reply_markup=kb.admin_home(is_owner=True),
    )


@router.message(Command("suggestions"))
async def cmd_suggestions(message: Message, session: AsyncSession) -> None:
    rows = await repo.list_suggestions(session, status="new", limit=kb.PER_PAGE)
    total = len(rows)
    await message.answer(views.render_suggestions(rows, page=0, total=total), reply_markup=kb.suggestions_kb(rows, page=0, total=total))


# ---------------------------------------------------------------- callbacks
@router.callback_query(F.data == "adm:noop")
async def cb_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(F.data == "adm:home")
async def cb_home(callback: CallbackQuery, ctx: AppContext) -> None:
    await _show(
        callback,
        await _home_text(ctx),
        kb.admin_home(is_owner=ctx.settings.is_owner(callback.from_user.id)),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:users:"))
async def cb_users(callback: CallbackQuery, session: AsyncSession, ctx: AppContext, state: FSMContext) -> None:
    await state.clear()
    page = int(callback.data.split(":")[2])
    text, markup, _ = await _users_page(session, ctx, page)
    await _show(callback, text, markup)
    await callback.answer()


@router.callback_query(F.data == "adm:usearch")
async def cb_users_search(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminStates.user_search)
    await _show(callback, "\u25f7 Send a username, name or numeric id to search for.")
    await callback.answer()


@router.message(AdminStates.user_search, F.text)
async def on_users_search(message: Message, state: FSMContext, session: AsyncSession, ctx: AppContext) -> None:
    query = (message.text or "").strip()
    await state.clear()
    text, markup, total = await _users_page(session, ctx, 0, query=query or None)
    if total == 0:
        await message.answer(f"No user matches {escape(query)!s}.")
        return
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("adm:u:"))
async def cb_user(callback: CallbackQuery, session: AsyncSession) -> None:
    _, _, tg_id, page = callback.data.split(":")
    user = await repo.get_user(session, int(tg_id))
    if user is None:
        await callback.answer("User not found", show_alert=True)
        return
    await _show(callback, views.render_user(user), kb.user_detail(user, page=int(page)))
    await callback.answer()


@router.callback_query(F.data.startswith("adm:uban:") | F.data.startswith("adm:uunban:"))
async def cb_user_ban(callback: CallbackQuery, session: AsyncSession) -> None:
    _, action, tg_id, page = callback.data.split(":")
    banned = action == "uban"
    user = await repo.set_user_banned(session, int(tg_id), banned, "banned by admin")
    if user is None:
        await callback.answer("User not found", show_alert=True)
        return
    await _show(callback, views.render_user(user), kb.user_detail(user, page=int(page)))
    await callback.answer("Banned" if banned else "Unbanned")


@router.callback_query(F.data.startswith("adm:upurge:"))
async def cb_user_purge(callback: CallbackQuery, session: AsyncSession, ctx: AppContext) -> None:
    _, _, tg_id, page = callback.data.split(":")
    await repo.purge_user(session, int(tg_id))
    text, markup, _ = await _users_page(session, ctx, int(page))
    await _show(callback, text, markup)
    await callback.answer("Removed from the database")


@router.callback_query(F.data.startswith("adm:umsg:"))
async def cb_user_message(callback: CallbackQuery, state: FSMContext) -> None:
    _, _, tg_id, page = callback.data.split(":")
    await state.set_state(AdminStates.user_message)
    await state.update_data(tg_id=int(tg_id), page=int(page))
    await _show(callback, "\u2709 Send the message to deliver to this user (or /cancel).")
    await callback.answer()


@router.message(AdminStates.user_message, F.text)
async def on_user_message(message: Message, state: FSMContext, ctx: AppContext) -> None:
    data = await state.get_data()
    await state.clear()
    if (message.text or "").strip().startswith("/cancel"):
        await message.answer("\u2713 Cancelled.")
        return
    try:
        await ctx.bot.send_message(int(data["tg_id"]), message.html_text or message.text or "")
    except Exception as exc:  # noqa: BLE001 - user may have blocked the bot
        await message.answer(f"\u2717 Could not deliver: {escape(exc)}")
        return
    await message.answer("\u2713 Delivered.")


@router.callback_query(F.data.startswith("adm:chats:"))
async def cb_chats(callback: CallbackQuery, session: AsyncSession) -> None:
    page = int(callback.data.split(":")[2])
    text, markup = await _chats_page(session, page)
    await _show(callback, text, markup)
    await callback.answer()


@router.callback_query(F.data.startswith("adm:c:"))
async def cb_chat(callback: CallbackQuery, session: AsyncSession) -> None:
    _, _, tg_id, page = callback.data.split(":")
    chat = await repo.get_chat(session, int(tg_id))
    if chat is None:
        await callback.answer("Group not found", show_alert=True)
        return
    await _show(callback, views.render_chat(chat), kb.chat_detail(chat, page=int(page)))
    await callback.answer()


@router.callback_query(F.data.startswith("adm:cban:") | F.data.startswith("adm:cunban:"))
async def cb_chat_ban(callback: CallbackQuery, session: AsyncSession) -> None:
    _, action, tg_id, page = callback.data.split(":")
    banned = action == "cban"
    chat = await repo.set_chat_banned(session, int(tg_id), banned, "banned by admin")
    if chat is None:
        await callback.answer("Group not found", show_alert=True)
        return
    await _show(callback, views.render_chat(chat), kb.chat_detail(chat, page=int(page)))
    await callback.answer("Banned" if banned else "Unbanned")


@router.callback_query(F.data == "adm:stats")
async def cb_stats_default(callback: CallbackQuery, session: AsyncSession, ctx: AppContext) -> None:
    data = await repo.analytics_overview(session, days=7)
    await _show(callback, views.render_analytics(data, queues=ctx.queues.snapshot()), kb.analytics_kb(window=7))
    await callback.answer()


@router.callback_query(F.data.startswith("adm:stats:"))
async def cb_stats(callback: CallbackQuery, session: AsyncSession, ctx: AppContext) -> None:
    days = int(callback.data.split(":")[2])
    data = await repo.analytics_overview(session, days=days)
    await _show(callback, views.render_analytics(data, queues=ctx.queues.snapshot()), kb.analytics_kb(window=days))
    await callback.answer()


@router.callback_query(F.data == "adm:runtime")
async def cb_runtime(callback: CallbackQuery, ctx: AppContext) -> None:
    await _show(
        callback,
        views.render_runtime(queues=ctx.queues.snapshot(), registry=ctx.registry, settings=ctx.settings),
        kb.admin_home(is_owner=True),
    )
    await callback.answer()


@router.callback_query(F.data == "adm:plats")
async def cb_platforms(callback: CallbackQuery, session: AsyncSession) -> None:
    rows = await _platform_rows(session)
    await _show(callback, views.render_platforms(rows), kb.platforms_kb(rows))
    await callback.answer()


@router.callback_query(F.data.startswith("adm:p:"))
async def cb_platform(callback: CallbackQuery, session: AsyncSession) -> None:
    platform = callback.data.split(":")[2]
    row = await repo.get_platform_setting(session, platform)
    if row is None:
        await callback.answer("Unknown platform", show_alert=True)
        return
    await _show(callback, views.render_platform(row), kb.platform_detail(row))
    await callback.answer()


@router.callback_query(F.data.startswith("adm:pf:"))
async def cb_platform_field(callback: CallbackQuery, state: FSMContext) -> None:
    _, _, platform, field = callback.data.split(":", 3)
    presets = kb.field_presets(platform, field)
    await state.set_state(AdminStates.platform_value)
    await state.update_data(platform=platform, field=field)
    label = dict(kb.PLATFORM_FIELDS).get(field, field)
    lines = [f"\u25f7 Send a new value for {escape(label)}.", "\u2022 /cancel to abort"]
    if field in ("max_media_size_bytes", "processing_max_ram_bytes", "direct_url_video_limit"):
        lines.append("\u2022 accepts 50MB / 2GB / none")
    await _show(callback, "\n".join(lines), presets or kb.cancel_kb(f"adm:p:{platform}"))
    await callback.answer()


@router.callback_query(F.data.startswith("adm:psv:"))
async def cb_platform_preset(callback: CallbackQuery, session: AsyncSession, ctx: AppContext) -> None:
    _, _, platform, field, value = callback.data.split(":", 4)
    await _apply_platform_value(callback, session, ctx, platform, field, value)
    await callback.answer()


@router.message(AdminStates.platform_value, F.text)
async def on_platform_value(message: Message, state: FSMContext, session: AsyncSession, ctx: AppContext) -> None:
    data = await state.get_data()
    await state.clear()
    raw = (message.text or "").strip()
    if raw.startswith("/cancel"):
        await message.answer("\u2713 Cancelled.")
        return
    platform = data.get("platform")
    field = data.get("field")
    if not platform or not field:
        return
    try:
        value = _parse_value(field, raw)
    except Exception as exc:  # noqa: BLE001 - user input
        await message.answer(f"\u2717 Could not parse {escape(raw)!s}: {escape(exc)}")
        return
    row = await repo.update_platform_setting(session, platform, **{field: value})
    if row is None:
        await message.answer("\u2717 Unknown platform.")
        return
    await ctx.registry.refresh()
    ctx.apply_settings()
    await message.answer(views.render_platform(row), reply_markup=kb.platform_detail(row))


async def _apply_platform_value(
    callback: CallbackQuery, session: AsyncSession, ctx: AppContext, platform: str, field: str, raw: str
) -> None:
    try:
        value = _parse_value(field, raw)
    except Exception as exc:  # noqa: BLE001 - callback payload
        await callback.answer(f"Bad value: {exc}", show_alert=True)
        return
    row = await repo.update_platform_setting(session, platform, **{field: value})
    if row is None:
        await callback.answer("Unknown platform", show_alert=True)
        return
    await ctx.registry.refresh()
    ctx.apply_settings()
    await _show(callback, views.render_platform(row), kb.platform_detail(row))


@router.callback_query(F.data.startswith("adm:prst:"))
async def cb_platform_reset(callback: CallbackQuery, session: AsyncSession, ctx: AppContext) -> None:
    platform = callback.data.split(":")[2]
    snapshot = PlatformSnapshot(platform=platform)
    row = await repo.update_platform_setting(
        session,
        platform,
        max_media_size_bytes=snapshot.max_media_size_bytes,
        direct_url_video_limit=snapshot.direct_url_video_limit,
        fetch_queue_size=snapshot.fetch_queue_size,
        fetch_concurrency=snapshot.fetch_concurrency,
        fetch_rate_per_second=snapshot.fetch_rate_per_second,
        processing_queue_size=snapshot.processing_queue_size,
        processing_concurrency=snapshot.processing_concurrency,
        processing_max_ram_bytes=snapshot.processing_max_ram_bytes,
    )
    if row is None:
        await callback.answer("Unknown platform", show_alert=True)
        return
    await ctx.registry.refresh()
    ctx.apply_settings()
    await _show(callback, views.render_platform(row), kb.platform_detail(row))
    await callback.answer("Reset")


# -------------------------------------------------------------- suggestions
@router.callback_query(F.data.startswith("adm:sugs:"))
async def cb_suggestions(callback: CallbackQuery, session: AsyncSession) -> None:
    page = int(callback.data.split(":")[2])
    rows = await repo.list_suggestions(session, status="new", limit=100)
    total = len(rows)
    window = rows[page * kb.PER_PAGE : (page + 1) * kb.PER_PAGE]
    await _show(
        callback,
        views.render_suggestions(window, page=page, total=total),
        kb.suggestions_kb(window, page=page, total=total),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:sug:"))
async def cb_suggestion(callback: CallbackQuery, session: AsyncSession) -> None:
    suggestion_id = int(callback.data.split(":")[2])
    from bot.db.models import Suggestion

    item = await session.get(Suggestion, suggestion_id)
    if item is None:
        await callback.answer("Not found", show_alert=True)
        return
    item.status = "read"
    await _show(callback, views.render_suggestion(item), kb.cancel_kb("adm:sugs:0"))
    await callback.answer("Marked as read")


# ---------------------------------------------------------------- broadcast
@router.message(Command("broadcast"))
@router.callback_query(F.data == "adm:bc")
async def cb_broadcast(event: Message | CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    await state.clear()
    users = await repo.dm_user_ids(session)
    chats = await repo.reachable_chat_ids(session)
    counters = {"dm": len(users), "groups": len(chats), "all": len(users) + len(chats)}
    text = "\n".join(
        [
            "\u2756 Broadcast",
            "\u2501" * 18,
            f"\u25b8 DM users \u00b7 {counters['dm']}",
            f"\u25b8 Groups \u00b7 {counters['groups']}",
            "\u2501" * 18,
            "Pick an audience, then send or forward the message to deliver.",
        ]
    )
    markup = kb.broadcast_audience_kb(counters)
    if isinstance(event, CallbackQuery):
        await _show(event, text, markup)
        await event.answer()
    else:
        await event.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("adm:bca:"))
async def cb_broadcast_audience(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext
) -> None:
    audience = callback.data.split(":")[2]
    users = await repo.dm_user_ids(session)
    chats = await repo.reachable_chat_ids(session)
    if audience == "dm":
        total = len(users)
    elif audience == "groups":
        total = len(chats)
    else:
        total = len(users) + len(chats)
    await state.set_state(AdminStates.broadcast_source)
    await state.update_data(audience=audience)
    await _show(
        callback,
        "\u2709 Send or forward the message to broadcast to "
        f"{audience} ({total} recipient(s)).\n\u2022 /cancel to abort",
        kb.cancel_kb("adm:bc"),
    )
    await callback.answer()


@router.message(AdminStates.broadcast_source)
async def on_broadcast_source(message: Message, state: FSMContext, session: AsyncSession, ctx: AppContext) -> None:
    data = await state.get_data()
    audience = data.get("audience", "dm")
    if (message.text or "").startswith("/cancel"):
        await state.clear()
        await message.answer("\u2713 Cancelled.")
        return

    await state.clear()
    users = await repo.dm_user_ids(session)
    chats = await repo.reachable_chat_ids(session)
    targets: list[tuple[str, int]] = []
    if audience in ("dm", "all"):
        targets += [("user", tg_id) for tg_id in users]
    if audience in ("groups", "all"):
        targets += [("chat", tg_id) for tg_id in chats]

    if not targets:
        await message.answer("\u2717 No recipients in that audience.")
        return

    preview = message.text or message.caption or f"[{message.content_type}]"
    broadcast = await repo.create_broadcast(
        session,
        admin_tg_id=message.from_user.id,
        source_chat_id=message.chat.id,
        source_message_id=message.message_id,
        audience=audience,
        targets=targets,
        preview=preview,
    )
    broadcast_id = broadcast.id
    source_chat_id = message.chat.id
    source_message_id = message.message_id
    await session.commit()

    progress = await message.answer(f"\u25f7 Broadcasting to {len(targets)} recipient(s)\u2026")

    async def report(done: int, sent: int, failed: int) -> None:
        if done % 25 and done != len(targets):
            return
        try:
            await progress.edit_text(f"\u25f7 {done}/{len(targets)} \u00b7 sent {sent} \u00b7 failed {failed}")
        except TelegramBadRequest:
            pass

    asyncio.create_task(
        _run_broadcast(ctx, broadcast_id, source_chat_id, source_message_id, targets, report, progress)
    )


async def _run_broadcast(
    ctx: AppContext,
    broadcast_id: int,
    source_chat_id: int,
    source_message_id: int,
    targets: list[tuple[str, int]],
    report,
    progress,
) -> None:
    try:
        sent, failed = await ctx.broadcast.run(
            broadcast_id=broadcast_id,
            source_chat_id=source_chat_id,
            source_message_id=source_message_id,
            targets=targets,
            on_progress=report,
        )
    except Exception as exc:  # noqa: BLE001 - background task
        logger.exception("broadcast failed")
        try:
            await progress.edit_text(f"\u2717 Broadcast failed: {escape(exc)}")
        except TelegramBadRequest:
            pass
        return
    try:
        await progress.edit_text(
            f"\u2713 Broadcast finished \u00b7 {sent} sent \u00b7 {failed} failed"
        )
    except TelegramBadRequest:
        pass


__all__ = ["router"]
