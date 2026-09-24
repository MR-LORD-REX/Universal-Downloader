"""Offline unit tests for the orchestrating :class:`Downloader` facade.

Run with ``python tests/test_orchestrator_offline.py`` or ``pytest tests``.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from downloader import Downloader
from downloader.adapters import (
    REDDIT_HOSTS,
    RedditDriver,
    reddit_pattern,
)
from downloader.client import _driver_kwargs
from downloader.core.enums import FormatKind, MediaKind, Platform
from downloader.core.exceptions import UnsupportedURLError
from downloader.core.http import HttpClient
from downloader.core.models import MediaFormat, MediaItem, PostMetadata
from downloader.core.saver import render_pattern

YT_LONG = "https://youtu.be/FOUwd1h_jF4?si=ss8F7ccTCqVpLejg"
YT_SHORT = "https://youtube.com/shorts/sUVemoSeY10?si=xx9vE5SIWfc5c2wc"
YT_POST = "http://youtube.com/post/UgkxU83oRNhZTfHUglXrSYJtO8MfrWJb3V8T?si=DBjTFY5eKNgz5ohH"
TW_VID = "https://x.com/GenshinUniverse/status/2102736196065521695?s=20"
TW_IMG = "https://x.com/GenshinImpact/status/2102941090571485331?s=20"
TW_IMGS = "https://x.com/GenshinUniverse/status/2102374550868521044?s=20"
REDDIT_POST = "https://www.reddit.com/r/Endfield/s/ioO2w6NTSS"
REDDIT_IMAGE = "https://i.redd.it/967qwrrqf7rh1.jpeg"
REDDIT_VIDEO = "https://v.redd.it/lgup3p9me2rh1/CMAF_1080.mp4?source=fallback"
IG_REEL = "https://www.instagram.com/reel/DdhvW0GslGe/"
IG_CAROUSEL = "https://www.instagram.com/p/DdPEofvmpPd/"


# --------------------------------------------------------------------- routing
def test_platform_routing_for_every_test_link() -> None:
    expected = {
        YT_LONG: Platform.YOUTUBE,
        YT_SHORT: Platform.YOUTUBE,
        YT_POST: Platform.YOUTUBE,
        TW_VID: Platform.TWITTER,
        TW_IMG: Platform.TWITTER,
        TW_IMGS: Platform.TWITTER,
        REDDIT_POST: Platform.REDDIT,
        "https://i.redd.it/967qwrrqf7rh1.jpeg": Platform.REDDIT,
        REDDIT_VIDEO: Platform.REDDIT,
        IG_REEL: Platform.INSTAGRAM,
        IG_CAROUSEL: Platform.INSTAGRAM,
    }
    for url, platform in expected.items():
        assert Downloader.platform_of(url) is platform, (url, Downloader.platform_of(url))
        assert Downloader.supports(url) is True


def test_unknown_hosts_are_not_claimed_by_reddit() -> None:
    # reddit.urls.parse happily classifies any url as ``external``, so the
    # driver has to refuse foreign hosts or it would swallow the internet.
    for url in (
        "https://example.com/not-supported",
        "https://i.imgur.com/abc123.jpg",
        "https://vimeo.com/12345",
        "https://tiktok.com/@a/video/1",
    ):
        assert Downloader.platform_of(url) is Platform.UNKNOWN, url
        assert Downloader.supports(url) is False, url
        assert RedditDriver.supports(url) is False, url
    assert RedditDriver.supports(REDDIT_POST) is True
    assert "reddit.com" in REDDIT_HOSTS and "v.redd.it" in REDDIT_HOSTS


def test_disabled_platform_is_refused() -> None:
    async def scenario() -> None:
        async with Downloader(platforms=[Platform.YOUTUBE]) as dl:
            assert "youtube" in dl.summary()
            assert "twitter" not in dl.summary()
            await dl.get_metadata(YT_LONG)  # ok
            try:
                await dl.get_metadata(TW_IMG)
            except UnsupportedURLError:
                pass
            else:
                raise AssertionError("twitter should be disabled")
            try:
                await dl.get_metadata("https://example.com/x")
            except UnsupportedURLError:
                pass
            else:
                raise AssertionError("unknown urls should raise")

    asyncio.run(scenario())


# ------------------------------------------------------------------- reddit glue
def test_reddit_pattern_translation() -> None:
    # core placeholders translate, unknown ones vanish, no literal braces remain
    assert reddit_pattern("{platform}_{id}_{index}.{ext}") == "reddit_{id}_{index}.{ext}"
    assert reddit_pattern("{platform}_{status_id}.{ext}") == "reddit_.{ext}"
    assert reddit_pattern("{author}_{subreddit}_{id}") == "{author}_{subreddit}_{id}"
    assert reddit_pattern("") == ""
    assert "{" not in reddit_pattern("{platform}_{nope}_{also_nope}.mp4")


def test_reddit_driver_kwargs_are_trimmed() -> None:
    kwargs = {
        "quality": "best",
        "target": "disk",
        "dest": "out",
        "pattern": "{platform}.{ext}",
        "progress": object(),
        "only": [0],
        "include_audio": True,
        "max_size_bytes": 10,
        "temp_dir": "tmp",
        # reddit specific
        "overwrite": True,
        "album_dir": True,
        "mux": True,
        "include_sizes": True,
        # unknown to reddit -> dropped
        "backend": "ytdlp",
        "refresh": "auto",
    }
    trimmed = _driver_kwargs(Platform.REDDIT, kwargs)
    assert "backend" not in trimmed and "refresh" not in trimmed
    assert trimmed["max_size_bytes"] == 10 and trimmed["pattern"] == "{platform}.{ext}"
    # other platforms get everything unchanged
    assert _driver_kwargs(Platform.YOUTUBE, kwargs) == kwargs


def test_http_for_returns_core_clients() -> None:
    async def scenario() -> None:
        async with Downloader() as dl:
            # youtube/twitter own a core client, reddit/unknown fall back to the shared one
            yt = dl._http_for(YT_LONG)
            reddit = dl._http_for(REDDIT_IMAGE)
            unknown = dl._http_for("https://i.imgur.com/a.jpg")
            assert isinstance(yt, HttpClient)
            assert isinstance(reddit, HttpClient) and reddit is dl.http
            assert isinstance(unknown, HttpClient) and unknown is dl.http
            assert dl.http is dl.http  # memoised

    asyncio.run(scenario())


# ------------------------------------------------------------------- the static
def test_render_pattern_placeholders() -> None:
    metadata = PostMetadata(
        platform=Platform.YOUTUBE,
        id="FOUwd1h_jF4",
        title="A Video: Part 2?",
        author="Some/One",
        duration=266.0,
    )
    fmt = MediaFormat(
        format_id="137",
        url="u/v.mp4",
        kind=FormatKind.VIDEO,
        quality_height=1080,
        quality_label="1080p",
        extension="mp4",
    )
    name = render_pattern(
        "{platform}_{author}_{id}_{index}_{quality}.{ext}",
        metadata=metadata,
        fmt=fmt,
        index=2,
        extension="mp4",
    )
    assert name == "youtube_Some_One_FOUwd1h_jF4_2_1080p.mp4", name
    assert "/" not in name and ":" not in name and "?" not in name


def test_affordable_premium_gate() -> None:
    async def scenario() -> None:
        item = MediaItem(
            index=0,
            kind=MediaKind.VIDEO,
            size_bytes=60_000_000,
            formats=[
                MediaFormat(
                    format_id="137",
                    url="u/v.mp4",
                    kind=FormatKind.VIDEO,
                    quality_height=1080,
                    size_bytes=60_000_000,
                )
            ],
        )
        meta = PostMetadata(
            platform=Platform.YOUTUBE,
            id="x",
            media_type=MediaKind.VIDEO,
            items=[item],
        )
        async with Downloader() as dl:
            allowed, size = dl.affordable(meta, 100 << 20)
            assert allowed is True and size == 60_000_000
            allowed, size = dl.affordable(meta, 50 << 20)
            assert allowed is False and size == 60_000_000

    asyncio.run(scenario())


# ---------------------------------------------------------------- error surface
def test_get_metadata_many_keeps_errors_in_place() -> None:
    class StubYouTube:
        async def get_metadata(self, url: str, **kwargs: Any) -> PostMetadata:
            if "bad" in url:
                raise RuntimeError("boom")
            return PostMetadata(platform=Platform.YOUTUBE, id=url, media_type=MediaKind.VIDEO)

        async def close(self) -> None:
            return None

    async def scenario() -> None:
        async with Downloader(youtube=StubYouTube()) as dl:
            results = await dl.get_metadata_many(
                [YT_LONG, "https://youtu.be/badbadbadba"], concurrency=2
            )
            assert isinstance(results[0], PostMetadata)
            assert isinstance(results[1], RuntimeError)

    asyncio.run(scenario())


# ------------------------------------------------------- engine housekeeping
def test_reserve_file_honours_the_requested_name() -> None:
    import tempfile

    from downloader.core.engine import _reserve_file

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        first = _reserve_file(root, "youtube_1_720p.mp4", prefix="x-", extension=".mp4")
        assert first.name == "youtube_1_720p.mp4", first.name
        second = _reserve_file(root, "youtube_1_720p.mp4", prefix="x-", extension=".mp4")
        assert second.name == "youtube_1_720p_1.mp4", second.name
        assert first.exists() and second.exists()


def test_cleanup_prunes_scratch_but_keeps_results() -> None:
    import tempfile

    from downloader.core.engine import DownloadEngine, _reserve_file
    from downloader.core.http import HttpClient

    async def scenario() -> None:
        http = HttpClient()
        engine = DownloadEngine(http)
        workdir = engine._work_directory(None)
        result = _reserve_file(workdir, "keep.mp4", prefix="x-", extension=".mp4")
        result.write_bytes(b"payload")
        orphan = _reserve_file(workdir, "orphan.m4a", prefix="x-", extension=".m4a")
        orphan.write_bytes(b"junk")
        engine._keep.add(result)  # simulate a file handed back to the caller
        removed = engine.cleanup()
        assert result.exists(), "a handed-out result must survive cleanup"
        assert not orphan.exists(), "an orphaned scratch file must be pruned"
        assert removed == 1, removed
        await http.close()

    with tempfile.TemporaryDirectory():
        asyncio.run(scenario())


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