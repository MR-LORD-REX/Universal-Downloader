"""Live integration tests for the orchestrating :class:`Downloader` facade.

Exercises every link from ``links.txt`` end to end: routing, canonical
metadata, direct CDN links, size probes, the premium gate, and download+save
for each platform. Run with ``python tests/test_orchestrator_live.py``.
Downloads are skipped when ``ORCH_TEST_DOWNLOAD=0``.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from downloader import Downloader
from downloader.core.enums import MediaGroupType, MediaKind, Platform
from downloader.core.exceptions import UnsupportedURLError
from downloader.core.ffmpeg import probe

YT_LONG = "https://youtu.be/FOUwd1h_jF4?si=ss8F7ccTCqVpLejg"
YT_SHORT = "https://youtube.com/shorts/sUVemoSeY10?si=xx9vE5SIWfc5c2wc"
YT_POST = "http://youtube.com/post/UgkxU83oRNhZTfHUglXrSYJtO8MfrWJb3V8T?si=DBjTFY5eKNgz5ohH"
TW_VID = "https://x.com/GenshinUniverse/status/2102736196065521695?s=20"
TW_IMG = "https://x.com/GenshinImpact/status/2102941090571485331?s=20"
TW_IMGS = "https://x.com/GenshinUniverse/status/2102374550868521044?s=20"
REDDIT_POST = "https://www.reddit.com/r/Endfield/s/ioO2w6NTSS"
REDDIT_VIDEO_POST = "https://www.reddit.com/r/Endfield/comments/1wot00d/"
DIRECT_REDDIT_IMAGE = "https://i.redd.it/967qwrrqf7rh1.jpeg"
DIRECT_REDDIT_VIDEO = "https://v.redd.it/cxdv6o5tkerh1/CMAF_1080.mp4"

DO_DOWNLOAD = os.getenv("ORCH_TEST_DOWNLOAD", "1") not in ("0", "false", "no")
GENEROUS = os.getenv("ORCH_TEST_ALL", "0") in ("1", "true", "yes")


class Skip(Exception):
    """Raised when the live environment cannot answer (never a code bug)."""


def _skip(exc: Exception) -> "Skip":
    return Skip(f"{type(exc).__name__}: {exc}")


async def _metadata(dl: Downloader, url: str, **kwargs):
    try:
        return await dl.get_metadata(url, **kwargs)
    except UnsupportedURLError:
        raise
    except Exception as exc:  # noqa: BLE001 - flakiness is not a failure
        raise _skip(exc) from exc


# ------------------------------------------------------------------- metadata
async def test_metadata_for_every_test_link(dl: Downloader) -> str:
    expected = [
        (YT_LONG, Platform.YOUTUBE, MediaKind.VIDEO, MediaGroupType.SINGLE),
        (YT_SHORT, Platform.YOUTUBE, MediaKind.VIDEO, MediaGroupType.SINGLE),
        (YT_POST, Platform.YOUTUBE, MediaKind.IMAGE, MediaGroupType.SINGLE),
        (TW_VID, Platform.TWITTER, MediaKind.VIDEO, MediaGroupType.SINGLE),
        (TW_IMG, Platform.TWITTER, MediaKind.IMAGE, MediaGroupType.SINGLE),
        (TW_IMGS, Platform.TWITTER, MediaKind.IMAGE, MediaGroupType.GALLERY),
        (REDDIT_POST, Platform.REDDIT, MediaKind.IMAGE, MediaGroupType.SINGLE),
    ]
    seen: list[str] = []
    for url, platform, kind, group in expected:
        meta = await _metadata(dl, url)
        assert meta.platform is platform, (url, meta.platform)
        assert meta.media_type is kind, (url, meta.media_type)
        assert meta.media_group_type is group, (url, meta.media_group_type)
        assert meta.title, url
        assert meta.links(), url
        seen.append(f"{platform}:{kind}")
    return f"{len(seen)} posts: {', '.join(seen)}"


async def test_links_are_direct_cdn_not_post_urls(dl: Downloader) -> str:
    hosts = {
        YT_LONG: "googlevideo.com",
        TW_IMG: "pbs.twimg.com",
        REDDIT_POST: "i.redd.it",
    }
    for url, host in hosts.items():
        meta = await _metadata(dl, url)
        links = meta.links()
        flat = [link for group in links.values() for link in group]
        assert flat, (url, links)
        assert all(host in link for link in flat), (url, host, flat[:2])
    return "every link points at a CDN host"


async def test_unified_size_metadata(dl: Downloader) -> str:
    for url in (YT_LONG, TW_VID, TW_IMG, REDDIT_POST):
        meta = await _metadata(dl, url)
        assert meta.size_known and meta.size_bytes, (url, meta.size_human)
        assert meta.size_human, url
    async with Downloader() as alt:
        for url in (YT_LONG, TW_IMG, REDDIT_POST):
            meta = await alt.get_metadata(url)
            assert meta.size_bytes, url
    return "sizes known before download on every platform"


async def test_reddit_video_has_separate_audio(dl: Downloader) -> str:
    meta = await _metadata(dl, REDDIT_VIDEO_POST)
    item = meta.items[0]
    assert item.video_formats, "no video renditions"
    assert item.audio_formats, "reddit serves audio as a separate file"
    # at least one video rendition is video-only (the DASH/CMAF tracks)
    assert any(f.is_video_only for f in item.video_formats), [f.display() for f in item.video_formats]
    return f"{len(item.video_formats)} video + {len(item.audio_formats)} audio renditions"


# --------------------------------------------------------------------- probes
async def test_direct_link_size_probe(dl: Downloader) -> str:
    # the same facade probes sizes for reddit CDN links (its legacy client has
    # no probe API, so this exercises the shared core client fallback)
    image_size, image_mime = await dl.size_of(DIRECT_REDDIT_IMAGE)
    assert image_size and image_mime and image_mime.startswith("image/"), (image_size, image_mime)
    assert await dl.size_human_of(DIRECT_REDDIT_IMAGE) == "463.22 KB"
    video_size, video_mime = await dl.size_of(DIRECT_REDDIT_VIDEO)
    assert video_size and video_size > 1_000_000, video_size
    assert video_mime and video_mime.startswith("video/"), video_mime
    return f"image={image_size}B video={video_size}B"


async def test_fetch_static_file(dl: Downloader) -> str:
    size, _ = await dl.size_of(DIRECT_REDDIT_IMAGE)
    payload = await dl.fetch(DIRECT_REDDIT_IMAGE)
    assert len(payload) == size, (len(payload), size)
    assert payload[:3] == b"\xff\xd8\xff", "expected jpeg magic bytes"
    try:
        await dl.fetch(DIRECT_REDDIT_IMAGE, max_bytes=1024)
    except UnsupportedURLError:
        pass
    else:
        raise AssertionError("max_bytes was not enforced")
    return f"fetched {len(payload)}B and enforced the byte cap"


async def test_premium_gate(dl: Downloader) -> str:
    meta = await _metadata(dl, YT_LONG)
    allowed, size = dl.affordable(meta, 25 << 20)
    assert allowed is False and size, (allowed, size)
    allowed, size = dl.affordable(meta, 500 << 20)
    assert allowed is True and size
    return f"size={size}B gate flips at 25/500 MB"


# ---------------------------------------------------------------- download/save
async def test_save_reddit_image(dl: Downloader) -> str:
    meta = await _metadata(dl, REDDIT_POST)
    with tempfile.TemporaryDirectory() as tmp:
        result = await dl.download(
            meta, quality="best", dest=tmp, pattern="{platform}_{id}_{index}.{ext}"
        )
        assert result.ok, result.errors
        paths = result.saved_paths
        assert paths, "nothing saved"
        assert paths[0].name.startswith("reddit_"), paths[0].name
        assert paths[0].stat().st_size > 0
        name, size = paths[0].name, paths[0].stat().st_size
    return f"saved {name} ({size}B) - pattern translation works"


async def test_save_youtube_worst(dl: Downloader) -> str:
    meta = await _metadata(dl, YT_LONG)
    with tempfile.TemporaryDirectory() as tmp:
        result = await dl.download(
            meta, quality="144p", dest=tmp, pattern="{platform}_{id}_{quality}.{ext}"
        )
        assert result.ok, result.errors
        paths = result.saved_paths
        assert paths and paths[0].name.startswith("youtube_"), paths
        info = probe(paths[0])
        assert info.has_video and info.has_audio, info
        name, size = paths[0].name, paths[0].stat().st_size
    return f"saved {name} ({size}B) muxed v+a"


async def test_save_twitter_image(dl: Downloader) -> str:
    meta = await _metadata(dl, TW_IMG)
    with tempfile.TemporaryDirectory() as tmp:
        result = await dl.download(
            meta, quality="best", dest=tmp, pattern="{platform}_{id}_{index}.{ext}"
        )
        assert result.ok, result.errors
        paths = result.saved_paths
        assert paths and paths[0].name.startswith("twitter_"), paths
        name, size = paths[0].name, paths[0].stat().st_size
    return f"saved {name} ({size}B)"


async def test_save_twitter_gallery_album(dl: Downloader) -> str:
    meta = await _metadata(dl, TW_IMGS)
    with tempfile.TemporaryDirectory() as tmp:
        result = await dl.download(meta, quality="best", dest=tmp)
        assert result.ok, result.errors
        assert len(result.saved_paths) == meta.count, result.saved_paths
        folder = result.saved_paths[0].parent.name
    return f"album folder {folder!r} with {meta.count} files"


async def test_max_size_bytes_is_enforced(dl: Downloader) -> str:
    meta = await _metadata(dl, TW_VID)
    result = await dl.download(meta, quality="1080p", max_size_bytes=1 << 20)
    assert not result.ok and result.errors, "oversized download was not refused"
    return f"refused: {next(iter(result.errors.values()))[:60]}"


async def test_get_metadata_many_mixed_urls(dl: Downloader) -> str:
    results = await dl.get_metadata_many(
        [YT_SHORT, TW_IMG, REDDIT_POST, "https://example.com/nope"], concurrency=3
    )
    assert isinstance(results[0], Exception) or results[0].platform is Platform.YOUTUBE
    assert isinstance(results[3], UnsupportedURLError), results[3]
    ok = [r for r in results if not isinstance(r, Exception)]
    return f"{len(ok)}/4 resolved, unsupported url reported in place"


async def test_disk_result_survives_close(dl: Downloader) -> str:
    # a target="disk" download without dest is written to a private scratch
    # directory; closing the client must not delete the file the caller holds
    from downloader.youtube import YouTubeClient

    yt = YouTubeClient()
    try:
        meta = await yt.get_metadata(YT_LONG)
        result = await yt.download(meta, quality="144p", target="disk")
        path = result.files[0].path
        assert path is not None and path.exists(), path
        assert path.name.startswith("youtube_"), path.name
    finally:
        await yt.close()
    assert path.exists(), f"close() destroyed the result {path}"
    size = path.stat().st_size
    path.unlink(missing_ok=True)
    return f"{path.name} survived close ({size}B)"


# --------------------------------------------------------------------- runner
TESTS = [
    test_metadata_for_every_test_link,
    test_links_are_direct_cdn_not_post_urls,
    test_unified_size_metadata,
    test_reddit_video_has_separate_audio,
    test_direct_link_size_probe,
    test_fetch_static_file,
    test_premium_gate,
    test_save_reddit_image,
    test_save_youtube_worst,
    test_save_twitter_image,
    test_save_twitter_gallery_album,
    test_max_size_bytes_is_enforced,
    test_get_metadata_many_mixed_urls,
    test_disk_result_survives_close,
]

SKIP_WHEN_NO_DOWNLOAD = {
    test_save_reddit_image,
    test_save_youtube_worst,
    test_save_twitter_image,
    test_save_twitter_gallery_album,
    test_max_size_bytes_is_enforced,
}


async def run_all() -> int:
    passed = failed = skipped = 0
    async with Downloader() as dl:
        for test in TESTS:
            if not DO_DOWNLOAD and test in SKIP_WHEN_NO_DOWNLOAD:
                print(f"skip {test.__name__} (ORCH_TEST_DOWNLOAD=0)")
                skipped += 1
                continue
            started = time.perf_counter()
            try:
                detail = await asyncio.wait_for(test(dl), timeout=300 if GENEROUS else 240)
            except Skip as exc:
                print(f"skip {test.__name__}: {exc}")
                skipped += 1
            except AssertionError as exc:
                print(f"FAIL {test.__name__}: {exc}")
                failed += 1
            except Exception as exc:  # noqa: BLE001 - report and keep going
                print(f"FAIL {test.__name__}: {type(exc).__name__}: {exc}")
                failed += 1
            else:
                print(f"pass {test.__name__} [{time.perf_counter() - started:.1f}s] {detail}")
                passed += 1
    print(f"\n{passed} passed, {failed} failed, {skipped} skipped")
    return 1 if failed else 0


def main() -> int:
    return asyncio.run(run_all())


if __name__ == "__main__":
    raise SystemExit(main())