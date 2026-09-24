"""Live integration tests: hit real Pinterest pins, boards and the real CDN.

Run with ``python tests/test_pinterest_live.py``. Downloads are skipped when
``PIN_TEST_DOWNLOAD=0``.

Pins get deleted, so an unavailable pin is reported as a *skip*: the live
suite must not fail because the internet moved on.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from downloader.core.enums import FormatKind, MediaGroupType, MediaKind, Platform
from downloader.core.ffmpeg import probe
from downloader.pinterest import PinterestClient, PinterestConfig
from downloader.pinterest.exceptions import PinterestError

IMAGE_PIN = "https://www.pinterest.com/pin/1145110643587112231/"  # single photo
VIDEO_PIN = "https://www.pinterest.com/pin/1084663891475263837/"  # ~15s mp4, h264+aac
BOARD = "https://www.pinterest.com/mashal0407/cool-diys/"

DO_DOWNLOAD = os.getenv("PIN_TEST_DOWNLOAD", "1") not in ("0", "false", "no")


class Skip(Exception):
    """Raised when the live environment cannot answer (never a code bug)."""


def _config(**overrides: Any) -> PinterestConfig:
    return PinterestConfig(**overrides)


async def _metadata(client: PinterestClient, url: str, **kwargs: Any):
    try:
        return await client.get_metadata(url, **kwargs)
    except PinterestError as exc:
        raise Skip(f"{type(exc).__name__}: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - flakiness is not a failure
        raise Skip(f"{type(exc).__name__}: {exc}") from exc


_CACHE: dict[str, Any] = {}


async def _cached(client: PinterestClient, url: str, key: str, **kwargs: Any):
    if key not in _CACHE:
        _CACHE[key] = await _metadata(client, url, **kwargs)
    return _CACHE[key]


# ------------------------------------------------------------ metadata tests
async def test_image_pin_exposes_the_cdn_link(client: PinterestClient) -> str:
    meta = await _cached(client, IMAGE_PIN, "image")
    assert meta.platform is Platform.PINTEREST
    assert meta.media_type is MediaKind.IMAGE, meta.media_type
    assert meta.media_group_type is MediaGroupType.SINGLE
    assert meta.title, "a pin should carry a title"
    links = meta.links()
    assert links.get("image"), links
    for url in links["image"]:
        assert url.startswith("http"), url
        assert "i.pinimg.com" in url, f"not a CDN url: {url}"
        assert "/pin/" not in url, f"a post link leaked into the media links: {url}"
    return f"{meta.title[:40]!r} -> {links['image'][0][:70]}"


async def test_image_pin_reports_its_size_before_download(client: PinterestClient) -> str:
    meta = await _cached(client, IMAGE_PIN, "image")
    item = meta.items[0]
    assert item.size_bytes, "the image size should be probed before downloading"
    # every rendition of the ladder carries its own probed size, so a bot can
    # pick a cheaper one without downloading first
    for fmt in item.image_formats:
        assert fmt.size_bytes, f"{fmt.format_id} has no size"
        assert fmt.size_is_approx is False, f"{fmt.format_id} is still an estimate"
    assert item.size_bytes == item.select("best").size_bytes
    return f"{item.size_human} ({item.resolution}, {len(item.image_formats)} renditions)"


async def test_video_pin_is_a_single_muxed_video(client: PinterestClient) -> str:
    meta = await _cached(client, VIDEO_PIN, "video")
    assert meta.media_type is MediaKind.VIDEO, meta.media_type
    assert meta.media_group_type is MediaGroupType.SINGLE
    item = meta.items[0]
    fmt = item.select("best")
    assert fmt.has_video and fmt.has_audio, "pinterest mp4s are progressive h264+aac"
    assert fmt.kind is FormatKind.MUXED, fmt.kind
    assert "/720p/" in fmt.url or "/1080p/" in fmt.url, fmt.url
    links = meta.links()
    assert links.get("video") == [fmt.url], links
    return f"{item.size_human} {item.resolution} -> {fmt.url[:70]}"


async def test_video_pin_reports_its_size_before_download(client: PinterestClient) -> str:
    meta = await _cached(client, VIDEO_PIN, "video")
    item = meta.items[0]
    assert item.size_bytes, "the video size should be probed before downloading"
    assert item.size_bytes > 100_000, item.size_bytes
    return f"{item.size_human}"


async def test_board_becomes_a_playlist(client: PinterestClient) -> str:
    meta = await _cached(client, BOARD, "board")
    assert meta.media_group_type is MediaGroupType.PLAYLIST, meta.media_group_type
    assert meta.media_type is MediaKind.GALLERY, meta.media_type
    assert meta.count >= 2, f"expected a few pins, got {meta.count}"
    grouped = meta.links()
    assert grouped, "a board should expose at least one media link"
    for urls in grouped.values():
        for url in urls:
            assert "i.pinimg.com" in url or "pinimg.com" in url, url
    return f"{meta.count} items: {sorted(grouped)}"


# ------------------------------------------------------------ download tests
async def test_download_and_save_video(client: PinterestClient) -> str:
    meta = await _cached(client, VIDEO_PIN, "video")
    result = await client.download(meta, quality="best", target="disk")
    assert result.ok, result.errors
    file = result.files[0]
    assert file.path and file.size > 0, file
    info = probe(file.path)
    assert info.has_video and info.has_audio, info
    assert not file.muxed, "pinterest serves progressive mp4s: no ffmpeg pass needed"
    expected = meta.items[0].size_bytes
    assert expected and abs(file.size - expected) < 1024, (file.size, expected)
    with tempfile.TemporaryDirectory() as tmp:
        paths = list(await client.save(result, tmp))
        assert len(paths) == 1, paths
        assert paths[0].stat().st_size == file.size
        detail = f"{paths[0].name} {info.video_codec}+{info.audio_codec} {info.width}x{info.height}"
    file.free()
    return detail


async def test_download_and_save_image(client: PinterestClient) -> str:
    meta = await _cached(client, IMAGE_PIN, "image")
    result = await client.download(meta, quality="best", target="disk")
    assert result.ok, result.errors
    file = result.files[0]
    assert file.path and file.size > 0, file
    expected = meta.items[0].select("best").size_bytes
    assert expected and abs(file.size - expected) < 1024, (file.size, expected)
    with tempfile.TemporaryDirectory() as tmp:
        paths = list(await client.save(result, tmp))
        assert len(paths) == 1, paths
        saved = paths[0]
        assert saved.suffix.lower() in (".jpg", ".jpeg", ".png", ".gif", ".webp"), saved.name
        detail = f"{saved.name} {saved.stat().st_size} B"
    file.free()
    return detail


# --------------------------------------------------------------------- runner
TESTS = [
    test_image_pin_exposes_the_cdn_link,
    test_image_pin_reports_its_size_before_download,
    test_video_pin_is_a_single_muxed_video,
    test_video_pin_reports_its_size_before_download,
    test_board_becomes_a_playlist,
    test_download_and_save_video,
    test_download_and_save_image,
]

NO_DOWNLOAD = {test_download_and_save_video, test_download_and_save_image}


async def run_all() -> int:
    passed = failed = skipped = 0
    async with PinterestClient(_config()) as client:
        for test in TESTS:
            if not DO_DOWNLOAD and test in NO_DOWNLOAD:
                print(f"skip {test.__name__} (PIN_TEST_DOWNLOAD=0)")
                skipped += 1
                continue
            started = time.perf_counter()
            try:
                detail = await asyncio.wait_for(test(client), timeout=180)
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
    print("note: pinterest cdn urls are stable; pins themselves get deleted")
    return 1 if failed else 0


def main() -> int:
    return asyncio.run(run_all())


if __name__ == "__main__":
    raise SystemExit(main())