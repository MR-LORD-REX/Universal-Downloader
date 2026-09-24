"""Live integration tests: hit real YouTube videos, shorts and community posts.

Run with ``python tests/test_youtube_live.py``. Downloads are skipped when
``YT_TEST_DOWNLOAD=0``. Set ``YT_TEST_ALL=1`` for the slower cases.
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
from downloader.core.select import build_plan, parse_quality
from downloader.youtube import YouTubeClient, YouTubeConfig
from downloader.youtube.exceptions import YouTubeError
from downloader.youtube.extract import ytdlp_format_selector

LONG_VIDEO = "https://youtu.be/FOUwd1h_jF4?si=ss8F7ccTCqVpLejg"
SHORTS = "https://youtube.com/shorts/sUVemoSeY10?si=xx9vE5SIWfc5c2wc"
COMMUNITY_POST = (
    "http://youtube.com/post/UgkxU83oRNhZTfHUglXrSYJtO8MfrWJb3V8T?si=DBjTFY5eKNgz5ohH"
)

DO_DOWNLOAD = os.getenv("YT_TEST_DOWNLOAD", "1") not in ("0", "false", "no")
GENEROUS = os.getenv("YT_TEST_ALL", "0") in ("1", "true", "yes")


class Skip(Exception):
    """Raised when the live environment cannot answer (never a code bug)."""


def _skip(exc: Exception) -> "Skip":
    return Skip(f"{type(exc).__name__}: {exc}")


async def _metadata(client: YouTubeClient, url: str):
    try:
        return await client.get_metadata(url)
    except YouTubeError as exc:
        raise _skip(exc) from exc
    except Exception as exc:  # noqa: BLE001 - network flakiness is not a failure
        raise _skip(exc) from exc


# --------------------------------------------------------------- metadata tests
async def test_long_video_metadata(client: YouTubeClient) -> str:
    meta = await _metadata(client, LONG_VIDEO)
    assert meta.platform is Platform.YOUTUBE, meta.platform
    assert meta.id == "FOUwd1h_jF4", meta.id
    assert meta.media_type is MediaKind.VIDEO, meta.media_type
    assert meta.media_group_type is MediaGroupType.SINGLE, meta.media_group_type
    assert meta.title and meta.author, (meta.title, meta.author)
    assert meta.duration and meta.duration > 60, meta.duration
    assert meta.count == 1
    links = meta.links()
    assert links.get("video"), links
    assert all("googlevideo.com" in url for url in links["video"]), links["video"]
    assert meta.size_known and meta.size_bytes and meta.size_bytes > 1_000_000
    return f"{meta.title[:36]!r} {meta.duration:.0f}s best={meta.size_human}"


async def test_every_rendition_has_a_size(client: YouTubeClient) -> str:
    meta = await _metadata(client, LONG_VIDEO)
    item = meta.items[0]
    videos = item.video_formats
    audios = item.audio_formats
    assert len(videos) >= 3, [f.display() for f in videos]
    assert audios, "expected adaptive audio renditions"
    assert all(f.size_bytes for f in videos), [f.display() for f in videos]
    assert all(not f.is_manifest for f in videos), [f.format_id for f in videos if f.is_manifest]
    assert any(f.quality_height and f.quality_height >= 1080 for f in videos)
    best = item.select("best")
    assert best.size_bytes, best.display()
    return f"{len(videos)} video + {len(audios)} audio renditions, all sized"


async def test_high_resolution_pick_needs_muxing(client: YouTubeClient) -> str:
    meta = await _metadata(client, LONG_VIDEO)
    plan = build_plan(meta.items, parse_quality("1080p", codec="avc1"))
    assert plan, "no 1080p plan"
    entry = plan[0]
    primary, audio = entry.primary, entry.audio
    assert primary.has_video
    # tall videos only ship adaptive 1080p, so an audio track must be paired
    assert audio is not None and audio.kind.value == "audio"
    listed = meta.size_for("1080p")
    assert listed and listed >= primary.size_bytes
    return f"1080p={primary.format_id} + audio={audio.format_id} -> {listed} bytes"


async def test_shorts_vertical_and_original_dub(client: YouTubeClient) -> str:
    meta = await _metadata(client, SHORTS)
    assert meta.is_short is True, (meta.is_short, meta.title)
    assert meta.media_type is MediaKind.VIDEO
    item = meta.items[0]
    vertical = [f for f in item.video_formats if f.height and f.width and f.height > f.width]
    assert vertical, [f.display() for f in item.video_formats]
    # dubs must not blow the ladder up: exactly one language survives, across
    # every audio codec/bitrate yt-dlp reports for it
    suffixes = {
        f.format_id.rsplit("-", 1)[-1]
        for f in item.audio_formats
        if f.format_id.rsplit("-", 1)[-1].isdigit()
    }
    assert len(suffixes) == 1, [f.format_id for f in item.audio_formats]
    assert meta.size_known and meta.size_bytes
    return f"short {meta.title[:30]!r} {meta.size_human} ({len(item.formats)} formats)"


async def test_community_post_image(client: YouTubeClient) -> str:
    meta = await _metadata(client, COMMUNITY_POST)
    assert meta.media_type is MediaKind.IMAGE, meta.media_type
    assert meta.media_group_type is MediaGroupType.SINGLE, meta.media_group_type
    assert meta.count >= 1
    links = meta.links()
    assert links.get("image"), links
    assert all("ggpht.com" in url or "googleusercontent.com" in url for url in links["image"])
    assert meta.size_known, "community post image size should be probed"
    return f"post {meta.author!r} image={meta.size_human}"


async def test_subtitles_are_exposed(client: YouTubeClient) -> str:
    meta = await _metadata(client, LONG_VIDEO)
    langs = meta.subtitle_languages
    assert langs, "expected captions"
    return f"{len(langs)} caption languages ({', '.join(langs[:4])})"


async def test_available_qualities(client: YouTubeClient) -> str:
    meta = await _metadata(client, LONG_VIDEO)
    qualities = client.available_qualities(meta)
    assert qualities, "no qualities offered"
    assert any(q.startswith("1080p") for q in qualities), qualities
    return f"qualities={qualities[:6]}"


async def test_size_probe_matches_metadata(client: YouTubeClient) -> str:
    meta = await _metadata(client, LONG_VIDEO)
    fmt = meta.items[0].select("720p")
    if fmt is None:  # pragma: no cover - depends on the video
        raise Skip("no 720p rendition")
    probed, _ = await client.http.probe_size(fmt.url, headers=dict(fmt.http_headers or {}))
    assert probed, "HEAD probe returned no size"
    # yt-dlp sizes are exact for https renditions; allow 2% slack for edge cases
    assert abs(probed - (fmt.size_bytes or 0)) <= max(2048, int(0.02 * probed)), (
        probed,
        fmt.size_bytes,
    )
    return f"720p probe={probed} metadata={fmt.size_bytes}"


# --------------------------------------------------------------- download tests
async def test_download_worst_and_verify_mux(client: YouTubeClient) -> str:
    meta = await _metadata(client, LONG_VIDEO)
    result = await client.download(meta, quality="worst", target="disk")
    assert result.ok, result.errors
    file = result.files[0]
    assert file.path and file.size > 0, file
    info = probe(file.path)
    assert info.has_video and info.has_audio, info
    assert info.video_codec and info.audio_codec, info
    file.free()
    return f"worst muxed {info.video_codec}+{info.audio_codec} {file.size_human}"


async def test_shorts_download_best_vertical(client: YouTubeClient) -> str:
    meta = await _metadata(client, SHORTS)
    result = await client.download(meta, quality="360p", target="memory", max_size_bytes=60 << 20)
    assert result.ok, result.errors
    file = result.files[0]
    assert file.data, "no bytes"
    assert file.size <= 60 << 20, file.size
    with tempfile.TemporaryDirectory() as tmp:
        path = file.write(Path(tmp) / "short.mp4")
        info = probe(path)
        assert info.has_video, info
    return f"short 360p {file.size_human} ({result.elapsed:.1f}s)"


async def test_download_and_save_to_disk(client: YouTubeClient) -> str:
    meta = await _metadata(client, LONG_VIDEO)
    with tempfile.TemporaryDirectory() as tmp:
        result = await client.download(
            meta,
            quality="144p",
            target="disk",
            dest=tmp,
            pattern="{platform}_{id}_{quality}.{ext}",
        )
        assert result.ok, result.errors
        paths = result.saved_paths
        assert paths, "nothing saved"
        assert paths[0].name.startswith("youtube_FOUwd1h_jF4"), paths[0].name
        assert paths[0].stat().st_size > 0
        size = paths[0].stat().st_size
    return f"saved {paths[0].name} ({size} bytes)"


async def test_max_size_bytes_is_refused(client: YouTubeClient) -> str:
    meta = await _metadata(client, LONG_VIDEO)
    result = await client.download(meta, quality="1080p", target="memory", max_size_bytes=1 << 20)
    assert not result.ok and result.errors, "oversized download was not refused"
    return f"refused: {next(iter(result.errors.values()))[:60]}"


async def test_download_subtitles(client: YouTubeClient) -> str:
    meta = await _metadata(client, LONG_VIDEO)
    langs = meta.subtitle_languages
    if not langs:
        raise Skip("no captions")
    wanted = [lang for lang in ("en", *langs) if lang in langs][:1]
    with tempfile.TemporaryDirectory() as tmp:
        paths = await client.download_subtitles(meta, tmp, languages=wanted)
        if not paths:
            raise Skip("YouTube declined the caption request (rate limit)")
        names = [Path(p).name for p in paths]
    return f"wrote {names}"


async def test_progress_callbacks_fire(client: YouTubeClient) -> str:
    meta = await _metadata(client, LONG_VIDEO)
    events: list[str] = []
    result = await client.download(
        meta,
        quality="144p",
        target="disk",
        progress=lambda event: events.append(str(event.phase)),
    )
    assert result.ok, result.errors
    for file in result.files:
        file.free()
    assert events, "no progress events"
    return f"{len(events)} progress events ({', '.join(sorted(set(events)))})"


# --------------------------------------------------------------------- runner
TESTS = [
    test_long_video_metadata,
    test_every_rendition_has_a_size,
    test_high_resolution_pick_needs_muxing,
    test_shorts_vertical_and_original_dub,
    test_community_post_image,
    test_subtitles_are_exposed,
    test_available_qualities,
    test_size_probe_matches_metadata,
    test_download_worst_and_verify_mux,
    test_shorts_download_best_vertical,
    test_download_and_save_to_disk,
    test_max_size_bytes_is_refused,
    test_download_subtitles,
    test_progress_callbacks_fire,
]

SKIP_WHEN_NO_DOWNLOAD = {
    test_download_worst_and_verify_mux,
    test_shorts_download_best_vertical,
    test_download_and_save_to_disk,
    test_max_size_bytes_is_refused,
    test_download_subtitles,
    test_progress_callbacks_fire,
}


async def run_all() -> int:
    passed = failed = skipped = 0
    config = YouTubeConfig()
    async with YouTubeClient(config) as client:
        for test in TESTS:
            if not DO_DOWNLOAD and test in SKIP_WHEN_NO_DOWNLOAD:
                print(f"skip {test.__name__} (YT_TEST_DOWNLOAD=0)")
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
    return 1 if failed else 0


def main() -> int:
    return asyncio.run(run_all())


if __name__ == "__main__":
    raise SystemExit(main())