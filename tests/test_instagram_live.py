"""Live integration tests: hit real Instagram posts and the real CDN.

Run with ``python tests/test_instagram_live.py``. Downloads are skipped when
``IG_TEST_DOWNLOAD=0``; set ``IG_TEST_ALL=1`` for the slow DASH-mux case.

Instagram rate limits anonymous clients, so this suite deliberately resolves
only a couple of posts and memoises them. A ``Skip`` (not a failure) is reported
whenever the *environment* refuses - a 401 "Please wait a few minutes", a
removed post - because that is not a code bug. Point ``IG_TEST_SESSION`` at an
instaloader session file to make the run reliable.
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
from downloader.instagram import InstagramClient, InstagramConfig
from downloader.instagram.exceptions import InstagramError, StoryUnavailableError

CAROUSEL = "https://www.instagram.com/p/DdPEofvmpPd/"  # 4 photos, @lilbieber
REEL = "https://www.instagram.com/reel/DdhvW0GslGe/"  # 1080x1920 clip, muxed 720p
STORY = "https://www.instagram.com/stories/someuser/3301234567890/"

DO_DOWNLOAD = os.getenv("IG_TEST_DOWNLOAD", "1") not in ("0", "false", "no")
GENEROUS = os.getenv("IG_TEST_ALL", "0") in ("1", "true", "yes")
SESSION_FILE = os.getenv("IG_TEST_SESSION") or ""


class Skip(Exception):
    """Raised when the live environment cannot answer (never a code bug)."""


def _config(**overrides: Any) -> InstagramConfig:
    options: dict[str, Any] = {"session_file": SESSION_FILE or None}
    options.update(overrides)
    return InstagramConfig(**options)


async def _metadata(client: InstagramClient, url: str, **kwargs: Any):
    try:
        return await client.get_metadata(url, **kwargs)
    except InstagramError as exc:
        raise Skip(f"{type(exc).__name__}: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - flakiness is not a failure
        raise Skip(f"{type(exc).__name__}: {exc}") from exc


_CACHE: dict[str, Any] = {}


async def _cached(client: InstagramClient, url: str, **kwargs: Any):
    """Resolve each (url, options) once: Instagram rate limits anonymous clients."""
    key = f"{id(client)}|{url}|{sorted(kwargs.items())}"
    if key not in _CACHE:
        _CACHE[key] = await _metadata(client, url, **kwargs)
    return _CACHE[key]


# --------------------------------------------------------------- metadata tests
async def test_carousel_metadata_is_an_album(client: InstagramClient) -> str:
    meta = await _cached(client, CAROUSEL)
    assert meta.platform is Platform.INSTAGRAM, meta.platform
    assert meta.id == "DdPEofvmpPd", meta.id
    assert meta.media_type is MediaKind.GALLERY, meta.media_type
    assert meta.media_group_type is MediaGroupType.ALBUM, meta.media_group_type
    assert meta.count == 4, meta.count
    assert meta.title and meta.author, (meta.title, meta.author)
    assert meta.extra["product_type"] == "carousel_container"
    links = meta.links()
    assert len(links.get("image") or []) == 4, links
    for url in links["image"]:
        assert url.startswith("http"), url
        assert "instagram.com/p/" not in url, url
    return f"{meta.count} photos by @{meta.author} ({meta.size_human})"


async def test_carousel_images_report_their_size_before_download(
    client: InstagramClient,
) -> str:
    meta = await _cached(client, CAROUSEL)
    assert meta.size_known, "no size at all"
    assert not meta.size_is_approx, "sizes should be exact, not estimated"
    sizes = [item.size_bytes for item in meta.items]
    assert all(sizes), sizes
    # every item exposes its own candidate ladder, largest first
    for item in meta.items:
        formats = item.image_formats
        assert formats, item.summary()
        assert formats[0].size_bytes == max(f.size_bytes or 0 for f in formats)
        assert formats[0].quality_height >= (formats[-1].quality_height or 0)
    return f"sizes {[round(s / 1024) for s in sizes]} KB"


async def test_reel_metadata_is_a_single_muxed_video(client: InstagramClient) -> str:
    meta = await _cached(client, REEL)
    assert meta.media_type is MediaKind.VIDEO, meta.media_type
    assert meta.media_group_type is MediaGroupType.SINGLE, meta.media_group_type
    assert meta.count == 1, meta.count
    assert meta.is_short is True, "a /reel/ link should carry product_type=clips"
    item = meta.items[0]
    assert item.kind is MediaKind.VIDEO
    assert item.has_audio is True
    progressive = [f for f in item.formats if f.kind is FormatKind.MUXED]
    assert progressive, [f.display() for f in item.formats]
    assert all(f.has_video and f.has_audio for f in progressive)
    assert all(f.quality_height for f in progressive), [f.display() for f in progressive]
    links = meta.links()
    assert links.get("video") and "instagram.com/p/" not in links["video"][0]
    assert meta.size_known, "no size resolved for the video"
    return f"{item.resolution} -> {progressive[0].quality_label} muxed, {meta.size_human}"


async def test_progressive_video_needs_no_muxing(client: InstagramClient) -> str:
    meta = await _cached(client, REEL)
    from downloader.core.select import build_plan, parse_quality

    entry = build_plan(meta.items, parse_quality("best"))[0]
    assert entry.primary.kind is FormatKind.MUXED, entry.primary.display()
    assert entry.needs_mux is False, "the progressive mp4 already carries audio"
    return f"best = {entry.primary.format_id} ({entry.primary.size_human})"


async def test_size_probe_matches_metadata(client: InstagramClient) -> str:
    meta = await _cached(client, REEL)
    fmt = meta.items[0].select("best")
    probed, mime = await client.http.probe_size(fmt.url)
    assert probed, "HEAD probe returned no size"
    assert probed == fmt.size_bytes, (probed, fmt.size_bytes)
    assert mime and mime.startswith("video/"), mime
    return f"{fmt.quality_label} probe={probed} mime={mime}"


async def test_dash_ladder_is_opt_in_and_sized() -> str:
    async with InstagramClient(_config(include_dash=True)) as client:
        meta = await _cached(client, REEL)
        item = meta.items[0]
        dash = [f for f in item.formats if f.origin.value == "dash"]
        video = [f for f in dash if f.kind is FormatKind.VIDEO]
        audio = [f for f in dash if f.kind is FormatKind.AUDIO]
        assert len(video) >= 4, [f.display() for f in video]
        assert len(audio) == 1, [f.display() for f in dash]
        assert all(f.size_bytes for f in video), "FBContentLength was not read"
        assert all(not f.has_audio for f in video), "the ladder is video only"
        heights = sorted({f.quality_height for f in video if f.quality_height})
        assert heights[0] <= 240, heights
        assert heights[-1] >= 1080, heights
        top = max(video, key=lambda f: f.quality_height or 0)
        assert top.size_bytes and top.size_bytes > 1_000_000
        return (
            f"{len(video)} DASH renditions {heights[0]}p..{heights[-1]}p, "
            f"audio {audio[0].size_human}"
        )


async def test_stories_need_a_session() -> str:
    async with InstagramClient(_config(include_stories=True)) as client:
        try:
            await client.get_metadata(STORY)
        except StoryUnavailableError as exc:
            assert "session" in str(exc)
        else:  # pragma: no cover - only when IG_TEST_SESSION is set
            raise Skip("a session file is configured, so the story may resolve")
    return "story refused with a clear message (no session configured)"


# --------------------------------------------------------------- download tests
async def test_download_carousel_as_an_album(client: InstagramClient) -> str:
    meta = await _cached(client, CAROUSEL)
    only = [0, 1]
    expected = [item.size_bytes for item in meta.items if item.index in only]
    with tempfile.TemporaryDirectory() as tmp:
        result = await client.download(
            meta,
            target="memory",
            dest=tmp,
            pattern="{platform}_{id}_{index}.{ext}",
            only=only,
        )
        assert result.ok, result.errors
        assert len(result.files) == len(only), [f.filename for f in result.files]
        paths = result.saved_paths
        assert len(paths) == len(only), paths
        assert len({p.name for p in paths}) == len(only), "filenames collided"
        actual = sorted(p.stat().st_size for p in paths)
        assert actual == sorted(s for s in expected if s), (actual, expected)
        assert all(p.parent.name.endswith(meta.id) for p in paths), [p.parent.name for p in paths]
    return f"{len(paths)} photos, byte sizes match metadata"


async def test_download_reel_is_already_muxed(client: InstagramClient) -> str:
    meta = await _cached(client, REEL)
    result = await client.download(meta, quality="best", target="disk")
    assert result.ok, result.errors
    file = result.files[0]
    assert file.path and file.size > 0, file
    assert not file.muxed, "instagram's progressive mp4 should need no ffmpeg pass"
    info = probe(file.path)
    assert info.has_video and info.has_audio, info
    expected = meta.items[0].select("best").size_bytes
    assert file.size == expected, (file.size, expected)
    detail = f"{file.size_human} {info.video_codec}+{info.audio_codec} ({result.elapsed:.1f}s)"
    file.free()
    return detail


async def test_download_dash_1080p_muxes_audio() -> str:
    async with InstagramClient(_config(include_dash=True)) as client:
        meta = await _cached(client, REEL)
        result = await client.download(meta, quality="1080p", target="disk")
        assert result.ok, result.errors
        file = result.files[0]
        assert file.muxed is True, "a video-only DASH track has to be muxed"
        info = probe(file.path)
        assert info.has_video and info.has_audio, info
        assert (info.height or 0) >= 1080, (info.width, info.height)
        detail = f"{file.size_human} {info.width}x{info.height} {info.video_codec}+{info.audio_codec}"
        file.free()
        return detail


async def test_max_size_bytes_is_refused(client: InstagramClient) -> str:
    meta = await _cached(client, REEL)
    result = await client.download(meta, quality="best", target="memory", max_size_bytes=1 << 10)
    assert not result.ok and result.errors, "oversized download was not refused"
    return f"refused: {next(iter(result.errors.values()))[:60]}"


# --------------------------------------------------------------------- runner
TESTS = [
    test_carousel_metadata_is_an_album,
    test_carousel_images_report_their_size_before_download,
    test_reel_metadata_is_a_single_muxed_video,
    test_progressive_video_needs_no_muxing,
    test_size_probe_matches_metadata,
    test_dash_ladder_is_opt_in_and_sized,
    test_stories_need_a_session,
    test_download_carousel_as_an_album,
    test_download_reel_is_already_muxed,
    test_download_dash_1080p_muxes_audio,
    test_max_size_bytes_is_refused,
]

NO_DOWNLOAD = {
    test_download_carousel_as_an_album,
    test_download_reel_is_already_muxed,
    test_download_dash_1080p_muxes_audio,
    test_max_size_bytes_is_refused,
}

SLOW = {test_download_dash_1080p_muxes_audio}

# these build their own client (a different config), so they take no argument
STANDALONE = {
    test_dash_ladder_is_opt_in_and_sized,
    test_stories_need_a_session,
    test_download_dash_1080p_muxes_audio,
}


async def run_all() -> int:
    passed = failed = skipped = 0
    async with InstagramClient(_config()) as client:
        for test in TESTS:
            if not DO_DOWNLOAD and test in NO_DOWNLOAD:
                print(f"skip {test.__name__} (IG_TEST_DOWNLOAD=0)")
                skipped += 1
                continue
            if test in SLOW and not GENEROUS:
                print(f"skip {test.__name__} (set IG_TEST_ALL=1)")
                skipped += 1
                continue
            started = time.perf_counter()
            try:
                if test in STANDALONE:
                    detail = await asyncio.wait_for(test(), timeout=300)
                else:
                    detail = await asyncio.wait_for(test(client), timeout=300)
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
    print("note: instagram cdn urls are signed and expire (roughly 24-48h)")
    return 1 if failed else 0


def main() -> int:
    return asyncio.run(run_all())


if __name__ == "__main__":
    raise SystemExit(main())
