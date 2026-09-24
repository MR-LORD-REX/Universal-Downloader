"""Live integration tests: hit real tweets and the real twimg CDN.

Run with ``python tests/test_twitter_live.py``. Downloads are skipped when
``TW_TEST_DOWNLOAD=0``. Set ``TW_TEST_ALL=1`` for the slower cases.
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

from downloader.core.enums import MediaGroupType, MediaKind, Platform
from downloader.core.ffmpeg import probe
from downloader.twitter import TwitterClient, TwitterConfig
from downloader.twitter.exceptions import TwitterError

VIDEO_TWEET = "https://x.com/GenshinUniverse/status/2102736196065521695?s=20"
IMAGE_TWEET = "https://x.com/GenshinImpact/status/2102941090571485331?s=20"
GALLERY_TWEET = "https://x.com/GenshinUniverse/status/2102374550868521044?s=20"

DO_DOWNLOAD = os.getenv("TW_TEST_DOWNLOAD", "1") not in ("0", "false", "no")
GENEROUS = os.getenv("TW_TEST_ALL", "0") in ("1", "true", "yes")


class Skip(Exception):
    """Raised when the live environment cannot answer (never a code bug)."""


def _skip(exc: Exception) -> "Skip":
    return Skip(f"{type(exc).__name__}: {exc}")


async def _metadata(client: TwitterClient, url: str, **kwargs):
    try:
        return await client.get_metadata(url, **kwargs)
    except TwitterError as exc:
        raise _skip(exc) from exc
    except Exception as exc:  # noqa: BLE001 - flakiness is not a failure
        raise _skip(exc) from exc


# --------------------------------------------------------------- metadata tests
async def test_video_tweet_metadata(client: TwitterClient) -> str:
    meta = await _metadata(client, VIDEO_TWEET)
    assert meta.platform is Platform.TWITTER, meta.platform
    assert meta.id == "2102736196065521695", meta.id
    assert meta.media_type is MediaKind.VIDEO, meta.media_type
    assert meta.media_group_type is MediaGroupType.SINGLE, meta.media_group_type
    assert meta.title and meta.author, (meta.title, meta.author)
    assert meta.count == 1
    links = meta.links()
    assert links.get("video"), links
    assert all("video.twimg.com" in url for url in links["video"]), links["video"]
    assert meta.size_known and meta.size_bytes and meta.size_bytes > 1_000_000
    return f"{meta.title[:34]!r} best={meta.size_human}"


async def test_video_ladder_is_progressive_and_sized(client: TwitterClient) -> str:
    meta = await _metadata(client, VIDEO_TWEET)
    item = meta.items[0]
    videos = item.video_formats
    assert len(videos) >= 3, [f.display() for f in videos]
    # Twitter serves progressive muxed mp4s, not split audio/video
    assert all(f.is_muxed for f in videos), [f.display() for f in videos]
    assert all(f.size_bytes for f in videos), [f.display() for f in videos]
    heights = sorted({f.quality_height for f in videos if f.quality_height}, reverse=True)
    assert heights and heights[0] >= 720, heights
    return f"{len(videos)} progressive renditions {heights}"


async def test_image_tweet_metadata(client: TwitterClient) -> str:
    meta = await _metadata(client, IMAGE_TWEET)
    assert meta.media_type is MediaKind.IMAGE, meta.media_type
    assert meta.media_group_type is MediaGroupType.SINGLE, meta.media_group_type
    links = meta.links()
    assert links.get("image"), links
    assert links["image"][0].endswith("?name=orig"), links["image"][0]
    assert "pbs.twimg.com" in links["image"][0]
    assert meta.size_known and meta.size_bytes and meta.size_bytes > 100_000
    return f"{meta.author!r} image={meta.size_human}"


async def test_gallery_metadata(client: TwitterClient) -> str:
    meta = await _metadata(client, GALLERY_TWEET)
    assert meta.media_type is MediaKind.IMAGE, meta.media_type
    assert meta.media_group_type is MediaGroupType.GALLERY, meta.media_group_type
    assert meta.count >= 3, meta.count
    assert len(meta.links()["image"]) == meta.count
    assert all(item.size_bytes for item in meta.items), [i.size_bytes for i in meta.items]
    return f"gallery of {meta.count} images ({meta.size_human})"


async def test_size_probe_matches_metadata(client: TwitterClient) -> str:
    meta = await _metadata(client, IMAGE_TWEET)
    fmt = meta.items[0].select("best")
    probed, mime = await client.http.probe_size(fmt.url)
    assert probed, "HEAD probe returned no size"
    assert probed == fmt.size_bytes, (probed, fmt.size_bytes)
    assert mime and mime.startswith("image/"), mime
    return f"orig probe={probed} mime={mime}"


async def test_media_api_off_still_yields_video(client: TwitterClient) -> str:
    # media_api is a config knob: with it off the SDK must still resolve video
    # tweets through yt-dlp alone (photos are simply unavailable then)
    async with TwitterClient(TwitterConfig(media_api="off")) as video_only:
        meta = await _metadata(video_only, VIDEO_TWEET)
        assert meta.media_type is MediaKind.VIDEO, meta.media_type
        assert meta.items and meta.items[0].video_formats, "no video formats with media_api=off"
        assert meta.size_known, "video-only fallback should still probe sizes"
        size = meta.size_human
    return f"media_api=off -> {size}"


async def test_preview_and_plan_helpers(client: TwitterClient) -> str:
    meta = await _metadata(client, IMAGE_TWEET)
    preview = client.preview_url(meta)
    assert preview and preview.endswith("?name=orig"), preview
    video_meta = await _metadata(client, VIDEO_TWEET)
    plan = client.plan_summary(video_meta, "720p")
    assert plan, "no plan"
    return f"preview ok; plan: {plan[0][:60]}"


# --------------------------------------------------------------- download tests
async def test_download_image_and_verify_size(client: TwitterClient) -> str:
    meta = await _metadata(client, IMAGE_TWEET)
    result = await client.download(meta, target="memory")
    assert result.ok, result.errors
    file = result.files[0]
    assert file.data, "no bytes"
    expected = meta.items[0].select("best").size_bytes
    assert len(file.data) == expected, (len(file.data), expected)
    return f"image {file.size_human} matches probed size"


async def test_download_video_is_muxed_mp4(client: TwitterClient) -> str:
    meta = await _metadata(client, VIDEO_TWEET)
    result = await client.download(meta, quality="360p", target="disk")
    assert result.ok, result.errors
    file = result.files[0]
    assert file.path and file.size > 0, file
    assert not file.muxed, "twitter progressive mp4s need no muxing"
    info = probe(file.path)
    assert info.has_video and info.has_audio, info
    file.free()
    return f"360p {info.video_codec}+{info.audio_codec} {file.size_human} ({result.elapsed:.1f}s)"


async def test_download_gallery_as_album(client: TwitterClient) -> str:
    meta = await _metadata(client, GALLERY_TWEET)
    with tempfile.TemporaryDirectory() as tmp:
        result = await client.download(
            meta,
            target="memory",
            dest=tmp,
            pattern="{platform}_{id}_{index}.{ext}",
        )
        assert result.ok, result.errors
        assert len(result.files) == meta.count, len(result.files)
        paths = result.saved_paths
        assert len(paths) == meta.count, paths
        assert len({p.name for p in paths}) == meta.count, "filenames collided"
        assert all(p.stat().st_size > 0 for p in paths)
        assert paths[0].parent.name.endswith(meta.id), paths[0].parent.name
    return f"{len(paths)} images saved to an album folder"


async def test_max_size_bytes_is_refused(client: TwitterClient) -> str:
    meta = await _metadata(client, VIDEO_TWEET)
    result = await client.download(meta, quality="1080p", target="memory", max_size_bytes=1 << 20)
    assert not result.ok and result.errors, "oversized download was not refused"
    return f"refused: {next(iter(result.errors.values()))[:60]}"


# --------------------------------------------------------------------- runner
TESTS = [
    test_video_tweet_metadata,
    test_video_ladder_is_progressive_and_sized,
    test_image_tweet_metadata,
    test_gallery_metadata,
    test_size_probe_matches_metadata,
    test_media_api_off_still_yields_video,
    test_preview_and_plan_helpers,
    test_download_image_and_verify_size,
    test_download_video_is_muxed_mp4,
    test_download_gallery_as_album,
    test_max_size_bytes_is_refused,
]

SKIP_WHEN_NO_DOWNLOAD = {
    test_download_image_and_verify_size,
    test_download_video_is_muxed_mp4,
    test_download_gallery_as_album,
    test_max_size_bytes_is_refused,
}


async def run_all() -> int:
    passed = failed = skipped = 0
    config = TwitterConfig()
    async with TwitterClient(config) as client:
        for test in TESTS:
            if not DO_DOWNLOAD and test in SKIP_WHEN_NO_DOWNLOAD:
                print(f"skip {test.__name__} (TW_TEST_DOWNLOAD=0)")
                skipped += 1
                continue
            started = time.perf_counter()
            try:
                detail = await asyncio.wait_for(test(client), timeout=300 if GENEROUS else 180)
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
    print("note: non-video media comes from api.fxtwitter.com (key-less fixup api)")
    return 1 if failed else 0


def main() -> int:
    return asyncio.run(run_all())


if __name__ == "__main__":
    raise SystemExit(main())