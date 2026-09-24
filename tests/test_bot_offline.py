"""Offline tests for the Telegram bot layer (no network, no Telegram).

Run with ``python tests/test_bot_offline.py`` or ``pytest tests``.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from downloader.core.enums import FormatKind, FormatOrigin, MediaGroupType, MediaKind, Platform
from downloader.core.models import MediaFormat, MediaItem, PostMetadata
from downloader.core.select import build_plan, parse_quality

from bot.config import Settings
from bot.db import repo
from bot.db.base import Database
from bot.db.models import PlatformSetting
from bot.services.queues import (
    FetchJob,
    PlatformQueue,
    ProcessingQueue,
    QueueConfig,
    TokenBucket,
)
from bot.services.routing import Action, SendAs, choose_quality, plan_route
from bot.services.sender import is_url_failure
from bot.ui import keyboards as kb
from bot.ui.descriptions import build_caption, build_status
from bot.ui.style import bold_sans, header, row, rule, section, small_caps

KB = 1024
MB = 1024 * 1024
GB = 1024 * 1024 * 1024


# ------------------------------------------------------------------ fixtures
def _image(index: int, size: int, *, kind: MediaKind = MediaKind.IMAGE) -> MediaItem:
    fmt = MediaFormat(
        format_id=f"img{index}",
        url=f"https://i.redd.it/photo{index}.jpg",
        kind=FormatKind.IMAGE,
        extension="jpg",
        width=1080,
        height=1080,
        size_bytes=size,
        has_video=False,
        has_audio=False,
    )
    return MediaItem(index=index, kind=kind, formats=[fmt], size_bytes=size, extension="jpg")


def _split_video(index: int = 0, *, video_size: int = 20 * MB, audio_size: int = 2 * MB) -> MediaItem:
    video = MediaFormat(
        format_id="137",
        url="https://rr1---sn-x.googlevideo.com/videoplayback?itag=137",
        kind=FormatKind.VIDEO,
        extension="mp4",
        width=1920,
        height=1080,
        size_bytes=video_size,
        has_video=True,
        has_audio=False,
        video_codec="avc1",
    )
    audio = MediaFormat(
        format_id="140",
        url="https://rr1---sn-x.googlevideo.com/videoplayback?itag=140",
        kind=FormatKind.AUDIO,
        extension="m4a",
        size_bytes=audio_size,
        has_video=False,
        has_audio=True,
        audio_codec="mp4a",
        bitrate_kbps=128,
    )
    return MediaItem(
        index=index,
        kind=MediaKind.VIDEO,
        formats=[video, audio],
        has_audio=True,
        duration=30.0,
        extension="mp4",
    )


def _muxed_video(index: int = 0, size: int = 6 * MB) -> MediaItem:
    fmt = MediaFormat(
        format_id="18",
        url="https://video.twimg.com/amplify_video/1/vid/avc1/640x360/x.mp4",
        kind=FormatKind.MUXED,
        extension="mp4",
        width=640,
        height=360,
        size_bytes=size,
        has_video=True,
        has_audio=True,
    )
    return MediaItem(index=index, kind=MediaKind.VIDEO, formats=[fmt], has_audio=True, duration=12.0)


def _metadata(items, *, platform: Platform = Platform.YOUTUBE, **extra) -> PostMetadata:
    kind = items[0].kind if items else MediaKind.UNKNOWN
    group = extra.pop("media_group_type", None)
    if group is None:
        group = MediaGroupType.GALLERY if len(items) > 1 else MediaGroupType.SINGLE
    return PostMetadata(
        platform=platform,
        id="abc123",
        title="A test post",
        description="description",
        author="tester",
        channel="tester",
        duration=30.0,
        media_type=kind,
        media_group_type=group,
        items=list(items),
        view_count=12345,
        like_count=678,
        **extra,
    )


# -------------------------------------------------------------------- config
def test_settings_parse_comma_separated_admins() -> None:
    parsed = Settings(owner_id=1, admin_ids="2, 3 ,4", bot_token="x")
    assert parsed.admin_ids == [2, 3, 4]
    assert parsed.all_admin_ids == [1, 2, 3, 4]
    assert parsed.is_admin(3) is True
    assert parsed.is_admin(99) is False


def test_settings_parse_json_admins() -> None:
    parsed = Settings(owner_id=7, admin_ids="[5, 6]", bot_token="x")
    assert parsed.admin_ids == [5, 6]


def test_admin_handler_imports_database_session() -> None:
    from bot.handlers import admin

    assert hasattr(admin, "db")
    assert hasattr(admin.db, "session")


# ------------------------------------------------------------------- routing
def test_photos_are_sent_by_url() -> None:
    meta = _metadata([_image(0, MB)], platform=Platform.REDDIT)
    plan = plan_route(meta, quality="best")
    assert len(plan.direct) == 1 and not plan.process and not plan.rejected
    item = plan.direct[0]
    assert item.send_as == SendAs.PHOTO and item.action == Action.URL


def test_photo_above_the_url_limit_is_downloaded_by_the_server() -> None:
    meta = _metadata([_image(0, 8 * MB)], platform=Platform.REDDIT)
    plan = plan_route(meta, quality="best")
    assert not plan.direct
    assert len(plan.process) == 1
    assert plan.process[0].action == Action.SERVER
    assert plan.process[0].send_as == SendAs.PHOTO


def test_huge_photo_becomes_a_document() -> None:
    meta = _metadata([_image(0, 20 * MB)], platform=Platform.REDDIT)
    plan = plan_route(meta, quality="best")
    assert plan.process[0].send_as == SendAs.DOCUMENT


def test_item_over_the_configured_limit_is_rejected() -> None:
    meta = _metadata([_image(0, 40 * MB)], platform=Platform.REDDIT)
    plan = plan_route(meta, quality="best", max_item_bytes=10 * MB)
    assert not plan.direct and not plan.process
    assert len(plan.rejected) == 1
    assert "limit is" in plan.rejected[0].reason


def test_split_video_goes_to_the_processing_lane() -> None:
    """A video-only rendition must be muxed, never handed to Telegram as is."""
    item = _split_video()
    meta = _metadata([item], platform=Platform.YOUTUBE)
    plan = plan_route(meta, quality="1080p")
    assert not plan.direct
    assert len(plan.process) == 1
    assert plan.process[0].action == Action.MUX
    assert plan.process_bytes == item.size_for("1080p")

    # and the SDK agrees that this needs muxing
    entries = build_plan([item], parse_quality("1080p"))
    assert entries[0].needs_mux is True


def test_progressive_mp4_is_sent_by_url() -> None:
    meta = _metadata([_muxed_video()], platform=Platform.TWITTER)
    plan = plan_route(meta, quality="best")
    assert len(plan.direct) == 1 and not plan.process
    assert plan.direct[0].action == Action.URL
    assert plan.direct[0].send_as == SendAs.VIDEO


def test_silent_gif_is_marked_without_audio() -> None:
    fmt = MediaFormat(
        format_id="gif",
        url="https://video.twimg.com/tweet_video/x.mp4",
        kind=FormatKind.VIDEO,
        extension="mp4",
        size_bytes=2 * MB,
        has_video=True,
        has_audio=False,
    )
    item = MediaItem(index=0, kind=MediaKind.GIF, formats=[fmt], has_audio=False)
    meta = _metadata([item], platform=Platform.TWITTER)
    plan = plan_route(meta, quality="best")
    assert len(plan.direct) == 1 and not plan.process
    assert plan.direct[0].send_as == SendAs.ANIMATION


def test_muxing_video_above_the_upload_limit_is_rejected() -> None:
    meta = _metadata([_split_video(video_size=60 * MB)], platform=Platform.YOUTUBE)
    plan = plan_route(meta, quality="1080p", upload_limit=50 * MB)
    assert not plan.process and not plan.direct
    assert "upload limit" in plan.rejected[0].reason


def test_ram_estimate_scales_with_the_media() -> None:
    meta = _metadata([_split_video(video_size=20 * MB, audio_size=2 * MB)], platform=Platform.YOUTUBE)
    plan = plan_route(meta, quality="1080p")
    assert plan.process_ram_estimate(factor=2.5, fallback=MB) == int(22 * MB * 2.5)


def test_gallery_mixes_direct_and_processed_items() -> None:
    meta = _metadata(
        [_image(0, MB), _split_video(1)],
        platform=Platform.REDDIT,
        media_group_type=MediaGroupType.GALLERY,
    )
    plan = plan_route(meta, quality="1080p")
    assert [item.index for item in plan.direct] == [0]
    assert [item.index for item in plan.process] == [1]


# -------------------------------------------------------------------- queues
def test_token_bucket_paces_requests() -> None:
    async def scenario() -> float:
        bucket = TokenBucket(5.0, capacity=1.0)
        await bucket.acquire()
        started = time.perf_counter()
        for _ in range(2):
            await bucket.acquire()
        return time.perf_counter() - started

    elapsed = asyncio.run(scenario())
    assert elapsed >= 0.25, elapsed


def test_platform_queue_rejects_when_full() -> None:
    seen: list[FetchJob] = []

    async def worker(job: FetchJob) -> None:  # pragma: no cover - not started
        seen.append(job)

    async def scenario() -> None:
        queue = PlatformQueue("reddit", config=QueueConfig(queue_size=1, concurrency=1, rate_per_second=100), worker=worker)
        job = FetchJob(platform="reddit", url="https://redd.it/x", chat_id=1, message_id=1, user_tg_id=1)
        assert queue.submit(job)[0] is True
        accepted, reason = queue.submit(job)
        assert accepted is False and "full" in reason
        assert queue.depth == 1

    asyncio.run(scenario())


def test_processing_queue_respects_ram_budget() -> None:
    async def worker(job) -> None:  # pragma: no cover - never runs
        pass

    async def scenario() -> None:
        queue = ProcessingQueue(queue_size=10, concurrency=1, max_ram_bytes=100 * MB, worker=worker)
        big = type("J", (), {"platform": "youtube", "reserved_ram": 60 * MB, "url": "u", "reserved": 0})()
        small = type("J", (), {"platform": "youtube", "reserved_ram": 10 * MB, "url": "u", "reserved": 0})()
        assert queue.submit(big)[0] is True
        accepted, reason = queue.submit(big)
        assert accepted is False and "memory" in reason
        assert queue.submit(small)[0] is True
        assert queue.reserved_ram == 70 * MB

    asyncio.run(scenario())


def test_processing_queue_enforces_per_platform_quota() -> None:
    async def worker(job) -> None:  # pragma: no cover - never runs
        pass

    async def scenario() -> None:
        queue = ProcessingQueue(queue_size=10, concurrency=1, max_ram_bytes=10 * GB, worker=worker)
        queue.apply_config(queue_size=10, concurrency=1, max_ram_bytes=10 * GB, platform_limits={"reddit": 1})
        job = type("J", (), {"platform": "reddit", "reserved_ram": MB, "url": "u"})()
        assert queue.submit(job)[0] is True
        accepted, reason = queue.submit(job)
        assert accepted is False and "reddit" in reason

    asyncio.run(scenario())


# ----------------------------------------------------------------- database
def test_alembic_migrations_create_every_table() -> None:
    from bot.config import settings
    from bot.db.migrate import upgrade

    original = settings.database_url
    with tempfile.TemporaryDirectory() as tmp:
        settings.database_url = f"sqlite+aiosqlite:///{Path(tmp) / 'bot.db'}"
        try:
            asyncio.run(upgrade("head"))
            import sqlite3

            connection = sqlite3.connect(Path(tmp) / "bot.db")
            try:
                names = {
                    row[0]
                    for row in connection.execute(
                        "select name from sqlite_master where type='table'"
                    )
                }
            finally:
                connection.close()
        finally:
            settings.database_url = original
    expected = {
        "users",
        "chats",
        "memberships",
        "platform_settings",
        "usage_events",
        "suggestions",
        "broadcasts",
        "broadcast_targets",
        "alembic_version",
    }
    assert expected <= names, expected - names


def test_repository_roundtrip() -> None:
    async def scenario(database: Database) -> None:
        async with database.session() as session:
            user = await repo.get_or_create_user(
                session, 4242, username="tester", first_name="Test", has_dm_access=True
            )
            chat = await repo.get_or_create_chat(session, -100123, type="supergroup", title="Testers")
            await repo.touch_membership(session, user, chat)
            await repo.bump_user_requests(session, 4242)
            await repo.record_usage(
                session,
                platform="youtube",
                outcome="ok",
                user_tg_id=4242,
                chat_tg_id=-100123,
                user_id=user.id,
                chat_id=chat.id,
                item_count=2,
                sent_count=2,
                bytes_total=5 * MB,
            )
        async with database.session() as session:
            rows = await repo.ensure_platform_settings(session)
            assert {row.platform for row in rows} == {"reddit", "youtube", "twitter"}
            stored = await repo.get_user(session, 4242)
            assert stored is not None and stored.has_dm_access is True
            assert stored.request_count == 1
            assert await repo.dm_user_ids(session) == [4242]
            assert await repo.reachable_chat_ids(session) == [-100123]
            stats = await repo.analytics_overview(session, days=1)
            assert stats["requests"] == 1 and stats["bytes"] == 5 * MB
            await repo.set_user_banned(session, 4242, True, "spam")
            banned = await repo.get_user(session, 4242)
            assert banned is not None and banned.is_banned is True
            assert await repo.dm_user_ids(session) == []
            found, total = await repo.search_users(session, query="test")
            assert total == 1 and found[0].tg_id == 4242
            chat_row = await repo.get_chat(session, -100123)
            assert chat_row is not None and chat_row.label == "Testers"

    with tempfile.TemporaryDirectory() as tmp:
        database = Database(f"sqlite+aiosqlite:///{Path(tmp) / 'test.db'}")

        async def run() -> None:
            await database.create_all()
            await scenario(database)
            await database.dispose()

        asyncio.run(run())


def test_platform_setting_update() -> None:
    async def scenario(database: Database) -> None:
        async with database.session() as session:
            rows = await repo.ensure_platform_settings(session)
            assert len(rows) == 3
            row = await repo.update_platform_setting(session, "youtube", quality="720p", max_media_size_bytes=25 * MB)
            assert row is not None and row.quality == "720p"
        async with database.session() as session:
            fetched = await repo.get_platform_setting(session, "youtube")
            assert fetched is not None and fetched.quality == "720p"
            assert fetched.max_media_size_bytes == 25 * MB

    with tempfile.TemporaryDirectory() as tmp:
        database = Database(f"sqlite+aiosqlite:///{Path(tmp) / 'settings.db'}")

        async def run() -> None:
            await database.create_all()
            await scenario(database)
            await database.dispose()

        asyncio.run(run())


# ------------------------------------------------------------------ captions
def test_caption_fits_telegram_and_escapes_html() -> None:
    meta = _metadata([_split_video()], platform=Platform.YOUTUBE)
    meta.title = "A <script> & 'quoted' title"
    caption = build_caption(meta, quality="1080p", size_bytes=22 * MB, footer="@bot")
    assert len(caption) <= 1024
    assert "<script>" not in caption
    assert "&lt;script&gt;" in caption
    assert "1080p" in caption


def test_caption_for_gallery_mentions_the_item_count() -> None:
    meta = _metadata([_image(0, MB), _image(1, MB)], platform=Platform.REDDIT, media_group_type=MediaGroupType.GALLERY)
    caption = build_caption(meta, quality="best")
    assert row("Items", "2") in caption


def test_caption_mentions_a_quality_downgrade() -> None:
    meta = _metadata([_split_video()], platform=Platform.YOUTUBE)
    caption = build_caption(meta, quality="720p", note="1080p was too large, delivered 720p instead")
    assert "1080p was too large" in caption
    assert "720p" in caption


def test_status_text_uses_the_symbol_vocabulary() -> None:
    queued = build_status("twitter", "queued", queue_depth=3)
    assert "queue 3" in queued
    assert build_status("reddit", "error", detail="boom").endswith("boom")


# -------------------------------------------------------------------- styling
def test_font_helpers_convert_ascii_only() -> None:
    assert bold_sans("Ab1") == "\U0001d5d4\U0001d5ef\U0001d7ed"
    assert small_caps("YouTube") == "\u028f\u1d0f\u1d1c\u1d1b\u1d1c\u0299\u1d07"
    assert "\u00b7" in row("Key", "Value")
    assert header("Title").startswith("\u2756 ")
    assert section("info").startswith("\u2726 ")
    assert set(rule(6)) == {"\u2501"}


def test_keyboards_build_without_a_database() -> None:
    from bot.db.models import Chat, PlatformSetting, Suggestion, User

    user = User(tg_id=1, first_name="Ann", has_dm_access=True, request_count=3)
    chat = Chat(tg_id=-100, title="Group", type="supergroup", request_count=1)
    setting = PlatformSetting(platform="youtube", quality="1080p")
    suggestion = Suggestion(id=1, user_tg_id=1, text="please add soundcloud", status="new")

    assert kb.admin_home(is_owner=True).inline_keyboard
    assert kb.users_page([user], page=0, total=1).inline_keyboard
    assert kb.user_detail(user, page=0).inline_keyboard
    assert kb.chats_page([chat], page=0, total=1).inline_keyboard
    assert kb.chat_detail(chat, page=0).inline_keyboard
    assert kb.platforms_kb([setting]).inline_keyboard
    assert kb.platform_detail(setting).inline_keyboard
    assert kb.field_presets("youtube", "quality") is not None
    assert kb.suggestions_kb([suggestion], page=0, total=1).inline_keyboard
    assert kb.broadcast_audience_kb({"dm": 2, "groups": 1, "all": 3}).inline_keyboard


# -------------------------------------------------------------------- filters
def test_link_extraction_ignores_unknown_hosts() -> None:
    from bot.filters.links import extract_supported, extract_urls

    text = "look https://example.com/x and https://youtu.be/FOUwd1h_jF4?si=abc, plus https://x.com/a/status/1"
    urls = extract_urls(text)
    assert "https://youtu.be/FOUwd1h_jF4?si=abc" in urls
    found = extract_supported(text)
    assert [platform for platform, _ in found] == ["youtube", "twitter"]

    disabled = extract_supported(text, enabled=["reddit"])
    assert disabled == []


def test_url_failure_detection() -> None:
    assert is_url_failure(Exception("Bad Request: failed to get HTTP URL content"))
    assert is_url_failure(Exception("WEBPAGE_CURL_FAILED"))
    assert not is_url_failure(Exception("chat not found"))


def test_empty_cookies_file_means_no_cookies() -> None:
    """`COOKIES_FILE=` must not become Path('.'), which yt-dlp reads as a cookie file.

    A blank value used to parse to ``Path('.')``, so every yt-dlp extraction
    died with ``[Errno 13] Permission denied: '.'``.
    """
    blank = Settings(cookies_file="")
    assert blank.cookies_file is None
    missing = Settings()
    assert missing.cookies_file is None or missing.cookies_file.exists() is not None
    explicit = Settings(cookies_file="cookies.txt")
    assert explicit.cookies_file is not None and explicit.cookies_file.is_absolute()


def test_choose_quality_steps_down_when_heights_are_flat() -> None:
    """Twitter's fxtwitter fallback labels every rendition with the source height.

    No "720p"-style string can name the smaller copy, so the downgrade has to
    walk the real renditions by size instead of by label.
    """
    variants = [
        (256_000, 2_954_421),
        (832_000, 9_139_641),
        (2_176_000, 27_642_079),
        (10_368_000, 116_156_565),
    ]
    formats = [
        MediaFormat(
            format_id=f"fx-{bitrate}",
            url=f"https://video.twimg.com/{bitrate}.mp4",
            kind=FormatKind.MUXED,
            origin=FormatOrigin.FX,
            extension="mp4",
            width=1920,
            height=1080,
            quality_height=1080,
            bitrate_kbps=bitrate // 1000,
            has_video=True,
            has_audio=True,
            quality_label="1080p",
            size_bytes=size,
            size_is_approx=True,
        )
        for bitrate, size in variants
    ]
    item = MediaItem(
        index=0,
        kind=MediaKind.VIDEO,
        has_audio=True,
        size_bytes=variants[-1][1],
        formats=formats,
    )
    meta = PostMetadata(
        platform=Platform.TWITTER,
        id="1",
        url="https://x.com/i/status/1",
        title="flat ladder",
        media_type=MediaKind.VIDEO,
        media_group_type=MediaGroupType.SINGLE,
        items=[item],
    )
    quality, note = choose_quality(meta, requested="1080p", upload_limit=50 * MB)
    assert quality == "fx-2176000", quality
    assert note and "smaller rendition" in note, note

    route = plan_route(
        meta,
        quality=quality,
        upload_limit=50 * MB,
        video_url_limit=20 * MB,
        photo_url_limit=5 * MB,
        photo_upload_limit=10 * MB,
    )
    assert route.planned, route.rejected
    assert all(plan.size is None or plan.size <= 50 * MB for plan in route.planned)


def test_queued_hand_off_is_not_counted_twice() -> None:
    """A post finished by the processing lane is one request, not two."""

    async def scenario(database: Database) -> None:
        async with database.session() as session:
            user = await repo.get_or_create_user(session, 7, username="queued")
            await repo.record_usage(
                session,
                platform="twitter",
                outcome="queued",
                user_tg_id=7,
                user_id=user.id,
                item_count=1,
                sent_count=0,
                bytes_total=0,
                url="https://x.com/i/status/1",
            )
            await repo.record_usage(
                session,
                platform="twitter",
                outcome="ok",
                user_tg_id=7,
                user_id=user.id,
                item_count=1,
                sent_count=1,
                bytes_total=4 * MB,
                url="https://x.com/i/status/1",
            )
        async with database.session() as session:
            stats = await repo.analytics_overview(session, days=1)
            assert stats["requests"] == 1, stats["requests"]
            assert stats["bytes"] == 4 * MB
            assert stats["outcomes"].get("queued") == 1, stats["outcomes"]
            assert stats["per_platform"] == [
                {"platform": "twitter", "requests": 1, "bytes": 4 * MB}
            ], stats["per_platform"]

    with tempfile.TemporaryDirectory() as tmp:
        database = Database(f"sqlite+aiosqlite:///{Path(tmp) / 'queued.db'}")

        async def run() -> None:
            await database.create_all()
            await scenario(database)
            await database.dispose()

        asyncio.run(run())


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
    print(f"\n{passed}/{passed + failed} offline tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
