"""Inline keyboards for the admin panel.

Callback payloads stay short (``adm:<action>:<args>``) so they fit inside
Telegram's 64 byte limit even with a page number appended.
"""

from __future__ import annotations

from typing import Optional

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.db.models import Chat, PlatformSetting, Suggestion, User

CALLBACK_PREFIX = "adm"
PER_PAGE = 8


def _nav(builder: InlineKeyboardBuilder, *, page: int, total: int, action: str) -> None:
    """Append prev/next buttons when there is more than one page."""
    pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    if pages <= 1:
        return
    rows = []
    if page > 0:
        rows.append(InlineKeyboardButton(text="\u00ab Prev", callback_data=f"{action}:{page - 1}"))
    rows.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="adm:noop"))
    if page < pages - 1:
        rows.append(InlineKeyboardButton(text="Next \u00bb", callback_data=f"{action}:{page + 1}"))
    builder.row(*rows)


def home_button(label: str = "\u2756 Menu") -> InlineKeyboardButton:
    return InlineKeyboardButton(text=label, callback_data="adm:home")


def admin_home(*, is_owner: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="\u25c8 Users", callback_data="adm:users:0"),
        InlineKeyboardButton(text="\u25c8 Chats", callback_data="adm:chats:0"),
    )
    builder.row(
        InlineKeyboardButton(text="\u2726 Analytics", callback_data="adm:stats"),
        InlineKeyboardButton(text="\u2726 Platforms", callback_data="adm:plats"),
    )
    builder.row(
        InlineKeyboardButton(text="\u2b07 Broadcast", callback_data="adm:bc"),
        InlineKeyboardButton(text="\u2709 Suggestions", callback_data="adm:sugs:0"),
    )
    if is_owner:
        builder.row(InlineKeyboardButton(text="\u2699 Runtime", callback_data="adm:runtime"))
    return builder.as_markup()


def users_page(users: list[User], *, page: int, total: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for user in users:
        flags = ""
        if user.is_banned:
            flags += " \u2717"
        if user.has_dm_access:
            flags += " \u2709"
        label = f"{user.display_name[:28]}{flags}"
        builder.row(
            InlineKeyboardButton(text=label, callback_data=f"adm:u:{user.tg_id}:{page}")
        )
    _nav(builder, page=page, total=total, action="adm:users")
    builder.row(
        InlineKeyboardButton(text="\u2315 Search", callback_data="adm:usearch"),
        home_button(),
    )
    return builder.as_markup()


def user_detail(user: User, *, page: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if user.is_banned:
        builder.row(
            InlineKeyboardButton(text="\u2713 Unban", callback_data=f"adm:uunban:{user.tg_id}:{page}")
        )
    else:
        builder.row(
            InlineKeyboardButton(text="\u2717 Ban", callback_data=f"adm:uban:{user.tg_id}:{page}")
        )
    if user.has_dm_access:
        builder.row(
            InlineKeyboardButton(text="\u2709 Message", callback_data=f"adm:umsg:{user.tg_id}:{page}")
        )
    builder.row(
        InlineKeyboardButton(text="\u2717 Purge", callback_data=f"adm:upurge:{user.tg_id}:{page}"),
        InlineKeyboardButton(text="\u00ab Users", callback_data=f"adm:users:{page}"),
    )
    builder.row(home_button())
    return builder.as_markup()


def chats_page(chats: list[Chat], *, page: int, total: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for chat in chats:
        flags = " \u2717" if chat.is_banned else ""
        label = f"{chat.type[:1].upper()} \u00b7 {chat.label[:26]}{flags}"
        builder.row(InlineKeyboardButton(text=label, callback_data=f"adm:c:{chat.tg_id}:{page}"))
    _nav(builder, page=page, total=total, action="adm:chats")
    builder.row(home_button())
    return builder.as_markup()


def chat_detail(chat: Chat, *, page: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if chat.is_banned:
        builder.row(
            InlineKeyboardButton(text="\u2713 Unban", callback_data=f"adm:cunban:{chat.tg_id}:{page}")
        )
    else:
        builder.row(
            InlineKeyboardButton(text="\u2717 Ban", callback_data=f"adm:cban:{chat.tg_id}:{page}")
        )
    builder.row(InlineKeyboardButton(text="\u00ab Chats", callback_data=f"adm:chats:{page}"))
    builder.row(home_button())
    return builder.as_markup()


def analytics_kb(*, window: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for days, label in ((1, "24h"), (7, "7d"), (30, "30d")):
        mark = "\u25c6 " if days == window else ""
        builder.button(text=f"{mark}{label}", callback_data=f"adm:stats:{days}")
    builder.adjust(3)
    builder.row(InlineKeyboardButton(text="\u21bb Refresh", callback_data=f"adm:stats:{window}"))
    builder.row(home_button())
    return builder.as_markup()


def platforms_kb(rows: list[PlatformSetting]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for row in rows:
        mark = "\u2713" if row.enabled else "\u2717"
        builder.row(
            InlineKeyboardButton(
                text=f"{mark} {row.platform.title()} \u00b7 {row.quality}",
                callback_data=f"adm:p:{row.platform}",
            )
        )
    builder.row(home_button())
    return builder.as_markup()


PLATFORM_FIELDS: tuple[tuple[str, str], ...] = (
    ("enabled", "Enabled"),
    ("quality", "Quality"),
    ("max_media_size_bytes", "Max item size"),
    ("fetch_queue_size", "Fetch queue"),
    ("fetch_concurrency", "Fetch workers"),
    ("fetch_rate_per_second", "Fetch rate/s"),
    ("processing_queue_size", "Process queue"),
    ("processing_concurrency", "Process workers"),
    ("processing_max_ram_bytes", "Process RAM"),
    ("direct_url_video_limit", "Direct url limit"),
)


def platform_detail(row: PlatformSetting) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    at = InlineKeyboardBuilder()
    for field, label in PLATFORM_FIELDS:
        value = getattr(row, field, None)
        shown = value if not isinstance(value, bool) else ("on" if value else "off")
        at.button(text=f"{label} \u00b7 {shown}", callback_data=f"adm:pf:{row.platform}:{field}")
    at.adjust(1)
    builder.attach(at)
    builder.row(
        InlineKeyboardButton(text="\u21bb Reset sizes", callback_data=f"adm:prst:{row.platform}"),
        InlineKeyboardButton(text="\u00ab Platforms", callback_data="adm:plats"),
    )
    builder.row(home_button())
    return builder.as_markup()


def field_presets(platform: str, field: str) -> Optional[InlineKeyboardMarkup]:
    """Quick-pick buttons for well known fields."""
    options: list[tuple[str, str]] = []
    if field == "enabled":
        options = [("Enable", "true"), ("Disable", "false")]
    elif field == "quality":
        options = [("best", "best"), ("1080p", "1080p"), ("720p", "720p"), ("480p", "480p"), ("audio", "audio")]
    elif field == "max_media_size_bytes":
        options = [("50 MB", str(50 * 1024**2)), ("100 MB", str(100 * 1024**2)), ("250 MB", str(250 * 1024**2)), ("None", "none")]
    elif field == "direct_url_video_limit":
        options = [("20 MB", str(20 * 1024**2)), ("50 MB", str(50 * 1024**2)), ("2 GB", str(2 * 1024**3))]
    elif field == "processing_max_ram_bytes":
        options = [("1 GB", str(1024**3)), ("2 GB", str(2 * 1024**3)), ("4 GB", str(4 * 1024**3))]
    elif field in ("fetch_concurrency", "processing_concurrency"):
        options = [("1", "1"), ("2", "2"), ("4", "4"), ("8", "8")]
    elif field in ("fetch_queue_size", "processing_queue_size"):
        options = [("16", "16"), ("32", "32"), ("64", "64"), ("128", "128")]
    elif field == "fetch_rate_per_second":
        options = [("0.5", "0.5"), ("1", "1"), ("2", "2"), ("5", "5")]
    if not options:
        return None
    builder = InlineKeyboardBuilder()
    for label, value in options:
        builder.button(text=label, callback_data=f"adm:psv:{platform}:{field}:{value}")
    builder.adjust(3)
    builder.row(InlineKeyboardButton(text="\u00ab Back", callback_data=f"adm:p:{platform}"))
    return builder.as_markup()


def broadcast_audience_kb(counters: dict[str, int]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text=f"\u2709 DM users ({counters.get('dm', 0)})", callback_data="adm:bca:dm"
        )
    )
    builder.row(
        InlineKeyboardButton(
            text=f"\u25c8 Groups ({counters.get('groups', 0)})", callback_data="adm:bca:groups"
        )
    )
    builder.row(
        InlineKeyboardButton(
            text=f"\u2756 Both ({counters.get('all', 0)})", callback_data="adm:bca:all"
        )
    )
    builder.row(home_button())
    return builder.as_markup()


def suggestions_kb(rows: list[Suggestion], *, page: int, total: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for row in rows:
        mark = "\u25f7" if row.status == "new" else "\u2713"
        builder.row(
            InlineKeyboardButton(
                text=f"{mark} #{row.id} \u00b7 {row.text[:34]}", callback_data=f"adm:sug:{row.id}"
            )
        )
    _nav(builder, page=page, total=total, action="adm:sugs")
    builder.row(home_button())
    return builder.as_markup()


def confirm_kb(action: str, cancel: str = "adm:home") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="\u2713 Confirm", callback_data=action),
        InlineKeyboardButton(text="\u2717 Cancel", callback_data=cancel),
    )
    return builder.as_markup()


def cancel_kb(callback: str = "adm:home") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="\u2717 Cancel", callback_data=callback))
    return builder.as_markup()


__all__ = [
    "CALLBACK_PREFIX",
    "PER_PAGE",
    "PLATFORM_FIELDS",
    "admin_home",
    "analytics_kb",
    "broadcast_audience_kb",
    "cancel_kb",
    "chat_detail",
    "chats_page",
    "confirm_kb",
    "field_presets",
    "home_button",
    "platform_detail",
    "platforms_kb",
    "suggestions_kb",
    "user_detail",
    "users_page",
]
