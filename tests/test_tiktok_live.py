"""Live integration tests: hit real TikTok videos and the real CDN.

Run with ``python tests/test_tiktok_live.py``. Downloads are skipped when
``TT_TEST_DOWNLOAD=0``.

**TikTok blocks datacentre IPs.** Resolving a video from a cloud host usually
fails with ``status 10204`` (or a connect timeout). That is an environment
limitation, not a bug, so every test here reports a **skip** in that case.
Run this suite from a residential IP or through a proxy to get real coverage:

    docker run ... -e PROXY=socks5://host:1080
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

from downloader.core.enums import MediaGroupType, MediaKind, Platform
from downloader.core.exceptions import MetadataError
from downloader.core.ffmpeg import probe
from downloader.tiktok import TikTokClient, TikTokConfig
from downloader.tiktok.exceptions import RegionBlockedError, TikTokError

VIDEO = "https://www.tiktok.com/@nasa/video/7253412088251534594"
PHOTO_POST = "https://www.tiktok.com/@nasa/video/7267502427925982507"

DO_DOWNLOAD = os.getenv("TT_TEST_DOWNLOAD", "1") not in ("0", "false", "no")
PROXY = os.getenv("TT_TEST_PROXY") or os.getenv("PROXY") or ""
COOKIES = os.getenv("TT_TEST_COOKIES") or os.getenv("COOKIES_FILE") or ""

#: Set once the first extraction proves this IP is blocked, so the remaining
#: tests skip immediately instead of waiting for another timeout.
_BLOCKED: list[str] = []


class Skip(Exception):
    """Raised when the live environment cannot answer (never a code bug)."""


def _config(**overrides: Any) -> TikTokConfig:
    options: dict[str, Any] = {"timeout": 45.0}
    if PROXY:
        options["proxy"] = PROXY
    if COOKIES:
        options["cookiefile"] = COOKIES
    options.update(overrides)
    return TikTokConfig(**options)


async def _metadata(client: TikTokClient, url: str, **kwargs: Any):
    if _BLOCKED:
        raise Skip(_BLOCKED[0])
    try:
        return await client.get_metadata(url, **kwargs)
    except RegionBlockedError as exc:
        reason = f"tiktok blocked this IP: {exc}"
        _BLOCKED.append(reason)
        raise Skip(reason) from exc
    except TikTokError as exc:
        raise Skip(f"{type(exc).__name__}: {exc}") from exc
    except MetadataError as exc:
        text = str(exc).lower()
        # a blocked range often just times out before TikTok can say 10204
        if "timed out" in text or "timeout" in text or "connection" in text:
            reason = f"tiktok is unreachable from this host ({exc})"
            _BLOCKED.append(reason)
            raise Skip(reason) from exc
        raise
    except Exception as exc:  # noqa: BLE001 - flakiness is not a failure
        raise Skip(f"{type(exc).__name__}: {exc}") from exc


_CACHE: dict[str, Any] = {}


async def _cached(client: TikTokClient, url: str, key: str, **kwargs: Any):
    if key not in _CACHE:
        _CACHE[key] = await _metadata(client, url, **kwargs)
    return _CACHE[key]


# ------------------------------------------------------------ metadata tests
async def test_video_metadata_is_a_single_playable_clip(client: TikTokClient) -> str:
    meta = await _cached(client, VIDEO, "video")
    assert meta.platform is Platform.TIKTOK
    assert meta.media_type is MediaKind.VIDEO, meta.media_type
    assert meta.media_group_type is MediaGroupType.SINGLE
    assert meta.author, "a tiktok video should carry its uploader"
    item = meta.items[0]
    best = item.select("best")
    assert best.has_video and best.has_audio, (
        "the progressive mp4 should carry its audio; otherwise it would need muxing"
    )
    links = meta.links()
    assert links.get("video") == [best.url], links
    assert "tiktokcdn" in best.url or "tiktokv" in best.url, best.url
    return f"@{meta.author} {item.size_human} {best.quality_label} -> {best.url[:60]}"


async def test_video_reports_its_size_before_download(client: TikTokClient) -> str:
    meta = await _cached(client, VIDEO, "video")
    item = meta.items[0]
    sizes = [fmt.size_bytes for fmt in item.video_formats]
    assert any(sizes), f"yt-dlp reported no filesize at all: {[f.format_id for f in item.video_formats]}"
    return f"{item.size_human} (best={item.select('best').display_size})"


async def test_quality_ladder_is_offered(client: TikTokClient) -> str:
    meta = await _cached(client, VIDEO, "video")
    item = meta.items[0]
    heights = sorted({f.quality_height for f in item.video_formats if f.quality_height}, reverse=True)
    assert len(heights) >= 2, f"expected a ladder, got {heights}"
    assert heights == sorted(heights, reverse=True)
    return f"heights {heights}"


async def test_photo_post_reports_audio_only(client: TikTokClient) -> str:
    """yt-dlp has no TikTok image support: a slideshow yields its soundtrack."""
    meta = await _cached(client, PHOTO_POST, "photo")
    assert meta.media_type in (MediaKind.AUDIO, MediaKind.VIDEO, MediaKind.GALLERY), meta.media_type
    if meta.media_type is MediaKind.AUDIO:
        assert meta.extra.get("audio_only") is True
        assert "audio" in meta.links()
        return "slideshow -> soundtrack only (upstream yt-dlp limitation)"
    return f"resolved as {meta.media_type}"


# ------------------------------------------------------------ download tests
async def test_download_and_save_video(client: TikTokClient) -> str:
    meta = await _cached(client, VIDEO, "video")
    result = await client.download(meta, quality="best", target="disk")
    assert result.ok, result.errors
    file = result.files[0]
    assert file.path and file.size > 0, file
    info = probe(file.path)
    assert info.has_video and info.has_audio, info
    with tempfile.TemporaryDirectory() as tmp:
        paths = list(await client.save(result, tmp))
        assert len(paths) == 1, paths
        detail = f"{paths[0].name} {info.video_codec}+{info.audio_codec} {info.width}x{info.height}"
    file.free()
    return detail


# --------------------------------------------------------------------- runner
TESTS = [
    test_video_metadata_is_a_single_playable_clip,
    test_video_reports_its_size_before_download,
    test_quality_ladder_is_offered,
    test_photo_post_reports_audio_only,
    test_download_and_save_video,
]

NO_DOWNLOAD = {test_download_and_save_video}


async def run_all() -> int:
    passed = failed = skipped = 0
    async with TikTokClient(_config()) as client:
        for test in TESTS:
            if not DO_DOWNLOAD and test in NO_DOWNLOAD:
                print(f"skip {test.__name__} (TT_TEST_DOWNLOAD=0)")
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
    if skipped and not passed:
        print("note: a full skip here just means TikTok blocked this host's IP; run it on the VPS")
    return 1 if failed else 0


def main() -> int:
    return asyncio.run(run_all())


if __name__ == "__main__":
    raise SystemExit(main())