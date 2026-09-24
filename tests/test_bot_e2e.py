"""Real end-to-end test against Telegram (opt-in, sends real messages).

Drives the *live* aiogram dispatcher exactly as if the owner had pasted each
link into a private chat, then waits for both queues to drain. Every piece is
real: the link filter, the per-platform fetch queue, the downloader SDK, the
router, the RAM-bounded processing queue, the sender and the Telegram Bot API.

    $env:RUN_TELEGRAM_E2E = "1"; python tests/test_bot_e2e.py

Requires a real BOT_TOKEN in .env and OWNER_ID pointing at a chat the bot may
write to. Nothing runs (the script exits 0) unless RUN_TELEGRAM_E2E is truthy,
so it is safe to leave in a CI matrix.

Set E2E_CASES to a comma separated list of 1-based indexes to run a
subset, e.g. $env:E2E_CASES = "3,4".
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aiogram.types import Chat, Message, Update
from aiogram.types import User as TgUser

from bot.app import AppContext
from bot.config import get_settings
from bot.db import repo
from bot.db.base import db
from bot.db.migrate import upgrade
from main import build_bot, build_dispatcher

# The curated matrix: one case per delivery lane.
CASES: list[tuple[str, str]] = [
    ("twitter single image  -> url photo", "https://x.com/GenshinImpact/status/2102941090571485331"),
    ("twitter gallery       -> album by url", "https://x.com/GenshinUniverse/status/2102374550868521044"),
    ("twitter video 1080p   -> downgrade + upload", "https://x.com/GenshinUniverse/status/2102736196065521695"),
    ("youtube short 1080p   -> mux + upload", "https://youtube.com/shorts/sUVemoSeY10"),
    ("reddit video          -> mux + upload", "https://v.redd.it/cxdv6o5tkerh1"),
]

IDLE_CONFIRMATIONS = 4


def _busy(queues) -> int:
    snap = queues.snapshot()
    total = sum(st["depth"] + st["active"] for st in snap["platforms"].values())
    total += snap["processing"]["depth"] + snap["processing"]["active"]
    return total


async def drain(ctx: AppContext, *, timeout: float = 900.0) -> None:
    """Wait until both queues are idle for a few consecutive samples."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    idle = 0
    while loop.time() < deadline:
        await asyncio.sleep(1.0)
        if _busy(ctx.queues) == 0:
            idle += 1
            if idle >= IDLE_CONFIRMATIONS:
                return
        else:
            idle = 0
    raise TimeoutError("queues did not drain within the timeout")


def _selected_cases() -> list[tuple[int, tuple[str, str]]]:
    """All cases, or just the 1-based indexes named in E2E_CASES."""
    raw = os.getenv("E2E_CASES", "").strip()
    if not raw:
        return list(enumerate(CASES))
    wanted = {int(part) for part in raw.replace(" ", "").split(",") if part}
    return [(i, case) for i, case in enumerate(CASES, start=1) if i in wanted]


async def main() -> int:
    if os.getenv("RUN_TELEGRAM_E2E", "").lower() not in {"1", "true", "yes"}:
        print("skipped: set RUN_TELEGRAM_E2E=1 to send real messages")
        return 0

    selected = _selected_cases()
    if not selected:
        print("E2E_CASES selected nothing", file=sys.stderr)
        return 1

    settings = get_settings()
    if not settings.bot_token:
        print("BOT_TOKEN is not configured")
        return 1
    owner = settings.owner_id
    # Keep the owner's DM quiet: one message per delivery, not status churn.
    settings.send_status_message = False

    await upgrade("head")
    bot = build_bot(settings)
    ctx = AppContext(bot=bot, settings=settings)
    dispatcher = build_dispatcher(ctx)

    await ctx.start()
    me = await bot.get_me()
    print(f"driving @{me.username} (id {me.id}) -> chat {owner}\n")

    try:
        for index, (label, url) in selected:
            print(f"[{index + 1}/{len(CASES)}] {label}")
            print(f"    {url}")
            update = Update(
                update_id=10_000 + index,
                message=Message(
                    message_id=20_000 + index,
                    date=datetime.now(timezone.utc),
                    chat=Chat(id=owner, type="private"),
                    from_user=TgUser(id=owner, is_bot=False, first_name="Owner"),
                    text=url,
                ),
            )
            await dispatcher.feed_update(bot, update)
            await drain(ctx)
            print("    queues drained\n")

        async with db.session() as session:
            snapshot = ctx.queues.snapshot()
            analytics = await repo.analytics_overview(session, days=1)

        print("queue snapshot:")
        for name, stats in snapshot["platforms"].items():
            print(f"  {name:<9} {stats}")
        print(f"  processing {snapshot['processing']}")
        print(f"analytics: {analytics}")
    finally:
        await ctx.stop()
        await bot.session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
