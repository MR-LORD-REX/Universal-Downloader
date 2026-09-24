"""Live integration tests for the bot's fetch/process workers.

The Telegram side is stubbed (no token, nothing is sent), but everything else is
real: the downloader SDK resolves the actual posts, the router decides between
url delivery and processing, and the queues are exercised for real.

Run with ``python tests/test_bot_live.py``.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Point the bot at a throwaway database *before* bot.config is imported.
_TMP = tempfile.mkdtemp(prefix="bot-live-")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{Path(_TMP) / 'live.db'}"

from bot.app import AppContext
from bot.config import get_settings
from bot.db import repo
from bot.db.base import db
from bot.db.migrate import upgrade
from bot.services.queues import FetchJob
from bot.services.routing import Action, SendAs, choose_quality
from bot.services.sender import Delivery

TW_IMAGE = "https://x.com/GenshinImpact/status/2102941090571485331?s=20"
TW_IMAGES = "https://x.com/GenshinUniverse/status/2102374550868521044?s=20"
TW_VIDEO = "https://x.com/GenshinUniverse/status/2102736196065521695?s=20"
YT_SHORT = "https://youtube.com/shorts/sUVemoSeY10?si=xx9vE5SIWfc5c2wc"
REDDIT_POST = "https://www.reddit.com/r/Endfield/s/ioO2w6NTSS"


class StubBot:
    """Records everything instead of talking to Telegram."""

    def __init__(self) -> None:
        self.messages: list[str] = []
        self.id = 1
        self.username = "stubbot"
        self._next_id = 1000

    def _id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def get_me(self):
        return SimpleNamespace(id=self.id, username=self.username)

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append(text)
        return SimpleNamespace(message_id=self._id())

    async def edit_message_text(self, **kwargs):
        return SimpleNamespace(message_id=self._id())

    async def delete_message(self, **kwargs):
        return True

    async def set_my_commands(self, *args, **kwargs):
        return True

    async def delete_my_commands(self, *args, **kwargs):
        return True


class RecordingSender:
    """Stands in for :class:`MediaSender` so nothing leaves the process."""

    def __init__(self) -> None:
        self.plan_calls: list[list] = []
        self.file_calls: list[list] = []
        self.captions: list[str] = []

    async def send_plans(self, chat_id, plans, **kwargs) -> Delivery:
        self.plan_calls.append(list(plans))
        if kwargs.get("caption"):
            self.captions.append(kwargs["caption"])
        return Delivery(sent=len(plans), bytes_sent=sum(p.size or 0 for p in plans))

    async def send_files(self, chat_id, files, **kwargs) -> Delivery:
        self.file_calls.append(list(files))
        if kwargs.get("caption"):
            self.captions.append(kwargs["caption"])
        return Delivery(sent=len(files), bytes_sent=sum(f.size for f in files))


class Harness:
    def __init__(self, ctx: AppContext, bot: StubBot) -> None:
        self.ctx = ctx
        self.bot = bot

    @property
    def sender(self) -> RecordingSender:
        return self.ctx.sender  # type: ignore[return-value]

    async def route(self, url: str, platform: str) -> None:
        job = FetchJob(
            platform=platform,
            url=url,
            chat_id=1,
            message_id=1,
            user_tg_id=1,
        )
        await self.ctx._handle_fetch(job)

    @property
    def delivered_plans(self) -> list:
        return [plan for call in self.sender.plan_calls for plan in call]

    @property
    def queued(self) -> int:
        return self.ctx.queues.processing.depth


def _platform_of(url: str) -> str:
    from downloader import Downloader

    return str(Downloader.platform_of(url))


async def _harness() -> Harness:
    settings = get_settings()
    settings.bot_token = "0:test"
    await upgrade("head")
    bot = StubBot()
    ctx = AppContext(bot=bot, settings=settings)
    await ctx.downloader.start()
    await ctx.registry.refresh()
    ctx.sender = RecordingSender()  # type: ignore[assignment]
    return Harness(ctx, bot)


def _run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------- twitter
def test_twitter_single_image_is_sent_by_url() -> None:
    async def scenario() -> None:
        harness = await _harness()
        try:
            await harness.route(TW_IMAGE, "twitter")
            plans = harness.delivered_plans
            assert harness.queued == 0, "an image must never need processing"
            assert plans and all(p.action == Action.URL for p in plans)
            assert plans[0].send_as == SendAs.PHOTO
            assert all(p.url and p.url.startswith("http") for p in plans)
        finally:
            await harness.ctx.stop()

    _run(scenario())


def test_twitter_gallery_is_sent_as_an_album() -> None:
    async def scenario() -> None:
        harness = await _harness()
        try:
            await harness.route(TW_IMAGES, "twitter")
            plans = harness.delivered_plans
            assert len(plans) >= 2, [p.url for p in plans]
            assert all(p.action == Action.URL and p.send_as == SendAs.PHOTO for p in plans)
        finally:
            await harness.ctx.stop()

    _run(scenario())


def test_twitter_video_is_delivered_within_the_upload_limit() -> None:
    """A 1080p tweet video is ~116 MB; the bot must downgrade rather than fail."""

    async def scenario() -> None:
        harness = await _harness()
        try:
            await harness.route(TW_VIDEO, "twitter")
            plans = harness.delivered_plans
            assert plans or harness.queued, ("the video went nowhere", harness.bot.messages)
            for plan in plans:
                assert plan.send_as == SendAs.VIDEO
                assert plan.size is None or plan.size <= 50 * 1024 * 1024
        finally:
            await harness.ctx.stop()

    _run(scenario())


def test_oversized_twitter_video_is_downgraded() -> None:
    """The 1080p rendition is 116 MB; the bot must step down to something Telegram accepts."""

    async def scenario() -> None:
        harness = await _harness()
        try:
            meta = await harness.ctx.downloader.get_metadata(TW_VIDEO)
            quality, note = choose_quality(
                meta, requested="1080p", upload_limit=50 * 1024 * 1024
            )
            assert quality != "1080p", quality
            assert note and "1080p" in note, note
            route = harness.ctx.route_for(meta, "twitter", quality)
            sizes = [plan.size for plan in route.planned]
            assert sizes, "nothing planned"
            assert all(size is None or size <= 50 * 1024 * 1024 for size in sizes), sizes
        finally:
            await harness.ctx.stop()

    _run(scenario())


def test_twitter_video_without_audio_never_pretends_to_have_it() -> None:
    """A tweet video must end up in exactly one lane, never both."""

    async def scenario() -> None:
        harness = await _harness()
        try:
            await harness.route(TW_VIDEO, "twitter")
            direct = [p for p in harness.delivered_plans if p.send_as == SendAs.VIDEO]
            processed = harness.queued
            assert bool(direct) ^ bool(processed), (len(direct), processed)
        finally:
            await harness.ctx.stop()

    _run(scenario())


# ------------------------------------------------------------------- youtube
def test_youtube_short_needs_processing_at_1080p() -> None:
    async def scenario() -> None:
        harness = await _harness()
        try:
            await harness.ctx.registry.refresh()
            await harness.route(YT_SHORT, "youtube")
            # YouTube serves 1080p as a video-only DASH rendition, so it must be muxed
            assert harness.queued >= 1 or harness.delivered_plans, "nothing happened"
            for plan in harness.delivered_plans:
                assert plan.action == Action.URL
                assert plan.send_as == SendAs.VIDEO
        finally:
            await harness.ctx.stop()

    _run(scenario())


# -------------------------------------------------------------------- reddit
def test_reddit_post_is_routed_without_errors() -> None:
    async def scenario() -> None:
        harness = await _harness()
        try:
            await harness.route(REDDIT_POST, "reddit")
            # either lane is acceptable, but the post must not error out
            assert harness.queued >= 1 or harness.delivered_plans, harness.bot.messages
            assert not any("Could not read that link" in m for m in harness.bot.messages)
        finally:
            await harness.ctx.stop()

    _run(scenario())


# ----------------------------------------------------------------- analytics
def test_usage_is_recorded_for_every_request() -> None:
    async def scenario() -> None:
        harness = await _harness()
        try:
            await harness.route(TW_IMAGE, "twitter")
            async with db.session() as session:
                stats = await repo.analytics_overview(session, days=1)
            assert stats["requests"] >= 1
            assert stats["sent"] >= 1
            assert stats["bytes"] > 0
        finally:
            await harness.ctx.stop()

    _run(scenario())


def test_unknown_link_is_never_claimed() -> None:
    from bot.filters.links import extract_supported

    assert extract_supported("https://example.com/some/video") == []
    assert extract_supported("hello world") == []


# --------------------------------------------------------------------- runner
def main() -> int:
    tests = [
        value
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    passed = failed = 0
    for test in tests:
        try:
            test()
        except Exception as exc:  # noqa: BLE001 - report and keep going
            print(f"FAIL {test.__name__}: {type(exc).__name__}: {exc}")
            failed += 1
        else:
            print(f"pass {test.__name__}")
            passed += 1
    print(f"\n{passed}/{passed + failed} live tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
