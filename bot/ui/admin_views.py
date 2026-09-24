"""Text rendering for the admin panel screens."""

from __future__ import annotations

from typing import Any, Optional, Sequence

from bot.db.models import Chat, PlatformSetting, Suggestion, User
from bot.services.platforms import PlatformSnapshot
from bot.utils.text import clip, escape, format_count, format_size, format_when

from .style import BAD, IMPORTANT, OK, PENDING, ROW, header, row, rule, section, small_caps


def _kv(rows: Sequence[tuple[str, object]]) -> str:
    lines = []
    for key, value in rows:
        if value in (None, ""):
            continue
        lines.append(row(escape(key), escape(value)))
    return "\n".join(lines)


def _flag(value: bool, when_true: str = "yes", when_false: str = "no") -> str:
    return when_true if value else when_false


def render_home(*, owner_id: int, platforms: Sequence[PlatformSnapshot], counts: dict[str, int]) -> str:
    lines = [
        header("Admin Panel"),
        rule(),
        section("overview"),
        _kv(
            [
                ("Owner", owner_id or "not set"),
                ("Users", format_count(counts.get("users", 0))),
                ("Groups", format_count(counts.get("chats", 0))),
                ("DM reach", format_count(counts.get("dm", 0))),
                ("Banned", format_count(counts.get("banned", 0))),
                ("New suggestions", format_count(counts.get("suggestions", 0))),
            ]
        ),
        "",
        section("platforms"),
    ]
    for snapshot in platforms:
        mark = OK if snapshot.enabled else BAD
        lines.append(
            f"{ROW} {escape(snapshot.platform.title())} {mark} \u00b7 {escape(snapshot.quality)}"
        )
    lines += [rule(), f"{IMPORTANT} Pick a section below."]
    return "\n".join(lines)


def render_users(users: Sequence[User], *, page: int, total: int) -> str:
    if not users:
        return "\n".join([header("Users"), rule(), "No users match."])
    pages = max(1, (total + 7) // 8)
    lines = [header(f"Users {page + 1}/{pages}"), rule(), section("registry")]
    for user in users:
        marks = ""
        if user.is_banned:
            marks += " " + BAD
        if user.has_dm_access:
            marks += " \u2709"
        if user.is_admin:
            marks += " " + IMPORTANT
        lines.append(f"{ROW} {escape(user.display_name[:30])}{marks}")
        lines.append(
            f"   {escape(str(user.tg_id))} \u00b7 {format_count(user.request_count)} req"
        )
    lines += [rule(), f"{IMPORTANT} Tap a user to manage them."]
    return "\n".join(lines)


def render_user(user: User) -> str:
    lines = [
        header("User"),
        rule(),
        _kv(
            [
                ("Name", user.display_name),
                ("Username", f"@{user.username}" if user.username else None),
                ("ID", user.tg_id),
                ("Language", user.language_code),
                ("Premium", _flag(user.is_premium)),
                ("DM access", _flag(user.has_dm_access)),
                ("Role", "owner" if user.is_owner else ("admin" if user.is_admin else "user")),
                ("Status", "banned" if user.is_banned else "active"),
                ("Ban reason", user.ban_reason),
            ]
        ),
        "",
        section("usage"),
        _kv(
            [
                ("Requests", format_count(user.request_count)),
                ("Failures", format_count(user.failed_count)),
                ("First seen", format_when(user.created_at)),
                ("Last seen", format_when(user.last_seen_at)),
            ]
        ),
        rule(),
    ]
    return "\n".join(lines)


def render_chats(chats: Sequence[Chat], *, page: int, total: int) -> str:
    if not chats:
        return "\n".join([header("Groups"), rule(), "No groups yet."])
    pages = max(1, (total + 7) // 8)
    lines = [header(f"Groups {page + 1}/{pages}"), rule(), section("registry")]
    for chat in chats:
        mark = BAD if chat.is_banned else OK
        lines.append(f"{ROW} {mark} {escape(chat.label[:32])}")
        lines.append(
            f"   {escape(str(chat.tg_id))} \u00b7 {escape(chat.type)} \u00b7 {format_count(chat.request_count)} req"
        )
    lines += [rule(), f"{IMPORTANT} Tap a group to manage it."]
    return "\n".join(lines)


def render_chat(chat: Chat) -> str:
    return "\n".join(
        [
            header("Group"),
            rule(),
            _kv(
                [
                    ("Title", chat.label),
                    ("Username", f"@{chat.username}" if chat.username else None),
                    ("ID", chat.tg_id),
                    ("Type", chat.type),
                    ("Status", "banned" if chat.is_banned else "active"),
                    ("Ban reason", chat.ban_reason),
                    ("Can delete", _flag(chat.can_delete_messages)),
                ]
            ),
            "",
            section("usage"),
            _kv(
                [
                    ("Requests", format_count(chat.request_count)),
                    ("First seen", format_when(chat.created_at)),
                    ("Last seen", format_when(chat.last_seen_at)),
                ]
            ),
            rule(),
        ]
    )


def render_analytics(data: dict[str, Any], *, queues: Optional[dict[str, Any]] = None) -> str:
    lines = [
        header(f"Analytics {data.get('window_days', 7)}d"),
        rule(),
        section("traffic"),
        _kv(
            [
                ("Requests", format_count(data.get("requests", 0))),
                ("Files sent", format_count(data.get("sent", 0))),
                ("Bytes", format_size(data.get("bytes", 0))),
            ]
        ),
        "",
        section("audience"),
        _kv(
            [
                ("Users", format_count(data.get("users", 0))),
                ("New 24h", format_count(data.get("new_users_24h", 0))),
                ("Active 24h", format_count(data.get("users_24h", 0))),
                ("DM reach", format_count(data.get("dm_reachable", 0))),
                ("Groups", format_count(data.get("chats", 0))),
                ("Banned", format_count(data.get("banned_users", 0) + data.get("banned_chats", 0))),
            ]
        ),
    ]
    per_platform = data.get("per_platform") or []
    if per_platform:
        lines += ["", section("by platform")]
        for entry in per_platform:
            lines.append(
                f"{ROW} {escape(str(entry['platform']).title())} \u00b7 "
                f"{format_count(entry['requests'])} req \u00b7 {format_size(entry['bytes'])}"
            )
    outcomes = data.get("outcomes") or {}
    if outcomes:
        lines += ["", section("outcomes")]
        for name, count in sorted(outcomes.items(), key=lambda item: -item[1]):
            icon = OK if name in ("ok", "direct") else (PENDING if name == "partial" else BAD)
            lines.append(f"{icon} {escape(name)} \u00b7 {format_count(count)}")
    if queues:
        lines += ["", section("live queues")]
        for name, stats in (queues.get("platforms") or {}).items():
            lines.append(
                f"{ROW} {escape(name.title())} \u00b7 depth {stats.get('depth', 0)}/{stats.get('queue_size', 0)} "
                f"\u00b7 active {stats.get('active', 0)}"
            )
        processing = queues.get("processing") or {}
        lines.append(
            f"{ROW} Processing \u00b7 depth {processing.get('depth', 0)}/{processing.get('queue_size', 0)} "
            f"\u00b7 RAM {format_size(processing.get('reserved_ram', 0))}/{format_size(processing.get('max_ram_bytes', 0))}"
        )
    lines.append(rule())
    return "\n".join(lines)


def render_platforms(rows: Sequence[PlatformSetting]) -> str:
    lines = [header("Platforms"), rule(), section("quality and limits")]
    for row in rows:
        mark = OK if row.enabled else BAD
        limit = format_size(row.max_media_size_bytes) if row.max_media_size_bytes else "no limit"
        lines.append(f"{ROW} {mark} {escape(row.platform.title())} \u00b7 {escape(row.quality)} \u00b7 {limit}")
    lines += [rule(), f"{IMPORTANT} Tap a platform to tune it."]
    return "\n".join(lines)


def render_platform(row: PlatformSetting) -> str:
    limit = format_size(row.max_media_size_bytes) if row.max_media_size_bytes else "none"
    return "\n".join(
        [
            header(f"{row.platform.title()} settings"),
            rule(),
            section("delivery"),
            _kv(
                [
                    ("Enabled", _flag(row.enabled)),
                    ("Default quality", row.quality),
                    ("Max item size", limit),
                    ("Direct url limit", format_size(row.direct_url_video_limit)),
                ]
            ),
            "",
            section("fetch queue"),
            _kv(
                [
                    ("Queue size", row.fetch_queue_size),
                    ("Workers", row.fetch_concurrency),
                    ("Rate/s", row.fetch_rate_per_second),
                ]
            ),
            "",
            section("processing"),
            _kv(
                [
                    ("Queue size", row.processing_queue_size),
                    ("Workers", row.processing_concurrency),
                    ("Max RAM", format_size(row.processing_max_ram_bytes)),
                ]
            ),
            rule(),
            f"{IMPORTANT} Tap a field to change it.",
        ]
    )


def render_runtime(*, queues: dict[str, Any], registry: Any, settings: Any) -> str:
    lines = [header("Runtime"), rule(), section("queues")]
    for name, stats in (queues.get("platforms") or {}).items():
        lines.append(
            f"{ROW} {escape(name.title())} \u00b7 depth {stats.get('depth', 0)}/{stats.get('queue_size', 0)} "
            f"\u00b7 workers {stats.get('active', 0)} \u00b7 {stats.get('rate_per_second', 0)}/s"
        )
        lines.append(
            f"   done {stats.get('completed', 0)} \u00b7 failed {stats.get('failed', 0)} "
            f"\u00b7 rejected {stats.get('rejected', 0)}"
        )
    processing = queues.get("processing") or {}
    lines += [
        "",
        section("processing"),
        _kv(
            [
                ("Depth", f"{processing.get('depth', 0)}/{processing.get('queue_size', 0)}"),
                ("Active", processing.get("active", 0)),
                ("RAM reserved", format_size(processing.get("reserved_ram", 0))),
                ("RAM budget", format_size(processing.get("max_ram_bytes", 0))),
                ("Completed", format_count(processing.get("completed", 0))),
                ("Failed", format_count(processing.get("failed", 0))),
            ]
        ),
        "",
        section("configuration"),
        _kv(
            [
                ("Mode", settings.bot_mode),
                ("Database", settings.database_url),
                ("Download dir", str(settings.download_dir)),
                ("Broadcast rate", f"{settings.broadcast_rate_per_second}/s"),
            ]
        ),
        rule(),
    ]
    return "\n".join(lines)


def render_suggestions(rows: Sequence[Suggestion], *, page: int, total: int) -> str:
    if not rows:
        return "\n".join([header("Suggestions"), rule(), "Nothing waiting."])
    lines = [header("Suggestions"), rule(), section(f"latest {len(rows)} of {total}")]
    for item in rows:
        icon = PENDING if item.status == "new" else OK
        lines.append(f"{icon} #{item.id} \u00b7 {escape(clip(item.text, 60))}")
        lines.append(f"   from {escape(item.username or str(item.user_tg_id))} \u00b7 {format_when(item.created_at)}")
    lines.append(rule())
    return "\n".join(lines)


def render_suggestion(item: Suggestion) -> str:
    return "\n".join(
        [
            header(f"Suggestion #{item.id}"),
            rule(),
            _kv(
                [
                    ("From", f"@{item.username}" if item.username else item.user_tg_id),
                    ("When", format_when(item.created_at)),
                    ("Status", item.status),
                ]
            ),
            "",
            escape(clip(item.text, 900)),
            rule(),
        ]
    )


def render_suggestion_notice(item: Suggestion, *, user_label: str) -> str:
    return "\n".join(
        [
            header("New Suggestion"),
            rule(),
            _kv([("From", user_label), ("When", format_when(item.created_at))]),
            "",
            escape(clip(item.text, 900)),
            rule(),
            small_caps("use /suggestions to reply"),
        ]
    )


__all__ = [
    "render_analytics",
    "render_chat",
    "render_chats",
    "render_home",
    "render_platform",
    "render_platforms",
    "render_runtime",
    "render_suggestion",
    "render_suggestion_notice",
    "render_user",
    "render_users",
]
