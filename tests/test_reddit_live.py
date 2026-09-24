"""Live integration tests: hit real reddit posts and the real CDN.

Run with ``python tests/test_reddit_live.py`` (or ``pytest tests -k live``).

Every post below is a real submission that was verified on 2026-09-24. Reddit
content is volatile - if a post gets deleted, replace the id and the metadata
assertions will tell you which one.

Set ``REDDIT_TEST_DOWNLOAD=0`` to run metadata-only (no bytes transferred).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from downloader.reddit import RedditClient, RedditConfig, RedditError
from downloader.reddit.exceptions import NoMediaError
from downloader.reddit.models import DownloadTarget, MediaGroupType, MediaKind

DO_DOWNLOAD = os.getenv("REDDIT_TEST_DOWNLOAD", "1") not in ("0", "false", "no")
GENEROUS = os.getenv("REDDIT_TEST_ALL", "0") in ("1", "true", "yes")

# id -> (label, expected media kind, expected group type)
POSTS: dict[str, tuple[str, MediaKind, MediaGroupType]] = {
    "1bak5ea": ("image (jpeg)", MediaKind.IMAGE, MediaGroupType.SINGLE),
    "1bb0cz8": ("gif", MediaKind.GIF, MediaGroupType.SINGLE),
    "1wot00d": ("video (CMAF, portrait)", MediaKind.VIDEO, MediaGroupType.SINGLE),
    "1bawbqq": ("video (legacy DASH, landscape)", MediaKind.VIDEO, MediaGroupType.SINGLE),
    "1worqtp": ("gallery (6 images)", MediaKind.GALLERY, MediaGroupType.GALLERY),
    "1basx0i": ("gallery (2 png)", MediaKind.GALLERY, MediaGroupType.GALLERY),
    "1wotexr": ("crosspost", MediaKind.IMAGE, MediaGroupType.CROSSPOST),
    "xbzjbv": ("external image (imgur)", MediaKind.EXTERNAL_IMAGE, MediaGroupType.EXTERNAL),
    "xi89wf": ("external video (youtube)", MediaKind.EXTERNAL_VIDEO, MediaGroupType.EXTERNAL),
    "1wot6ta": ("self post", MediaKind.TEXT, MediaGroupType.NONE),
}

SHARE_URL = "https://www.reddit.com/r/Endfield/s/ioO2w6NTSS"
DIRECT_IMAGE = "https://i.redd.it/967qwrrqf7rh1.jpeg"
DIRECT_VIDEO_BASE = "https://v.redd.it/cxdv6o5tkerh1"


class Skip(Exception):
    """Raised when the network/reddit refuses to cooperate."""


async def _metadata(client: RedditClient, target: str):
    try:
        return await client.get_metadata(target, include_sizes=True)
    except NoMediaError as exc:
        raise Skip(str(exc)) from exc
    except RedditError as exc:
        raise Skip(f"metadata unavailable: {exc}") from exc


# --------------------------------------------------------------- metadata tests
async def test_share_link_resolves(client: RedditClient) -> str:
    metadata = await _metadata(client, SHARE_URL)
    assert metadata.id == "1wnwxze", metadata.id
    assert metadata.subreddit == "Endfield", metadata.subreddit
    assert metadata.media_type == MediaKind.IMAGE, metadata.media_type
    assert metadata.title and "Endfield" in metadata.title
    links = metadata.links()
    assert links["image"][0].startswith("https://i.redd.it/"), links
    return f"resolved share link -> {metadata.id} ({metadata.size_human})"


async def test_metadata_for_every_post_type(client: RedditClient) -> str:
    checked = 0
    for post_id, (label, kind, group) in POSTS.items():
        metadata = await _metadata(client, post_id)
        assert metadata.media_type == kind, f"{label}: {metadata.media_type} != {kind}"
        assert metadata.media_group_type == group, f"{label}: {metadata.media_group_type} != {group}"
        assert metadata.title, f"{label}: missing title"
        if kind == MediaKind.TEXT:
            assert metadata.items == []
        else:
            assert metadata.items, f"{label}: no media items"
            links = metadata.links()
            assert links, f"{label}: no media links"
        checked += 1
    return f"{checked} post types classified correctly"


async def test_video_renditions_are_enumerated(client: RedditClient) -> str:
    metadata = await _metadata(client, "1wot00d")
    item = metadata.items[0]
    videos = item.video_formats
    audios = item.audio_formats
    assert len(videos) >= 4, [f.display() for f in videos]
    assert audios, "expected separate audio renditions from the DASH manifest"
    assert all(f.size_bytes for f in videos + audios), "sizes should be resolved"
    assert all(f.origin.value in ("dash", "direct", "hls", "probe") for f in videos)
    assert item.best_audio() is not None
    return f"{len(videos)} video renditions + {len(audios)} audio renditions with sizes"


async def test_gallery_items_are_individual_media(client: RedditClient) -> str:
    metadata = await _metadata(client, "1worqtp")
    assert metadata.count >= 2, metadata.count
    assert metadata.media_group_type == MediaGroupType.GALLERY
    assert all(item.source_url and "i.redd.it" in item.source_url for item in metadata.items)
    ids = [item.id for item in metadata.items]
    assert len(ids) == len(set(ids)), "gallery ids must be unique"
    assert metadata.size_bytes and metadata.size_bytes > 0
    return f"{metadata.count} gallery items, {metadata.size_human} total before download"


async def test_sizes_known_before_download(client: RedditClient) -> str:
    metadata = await _metadata(client, "1basx0i")
    assert metadata.sizes_known, [(i.size_bytes, i.formats) for i in metadata.items]
    assert metadata.size_bytes and metadata.size_bytes > 1000
    return f"size known up front: {metadata.size_human}"


async def test_direct_cdn_url(client: RedditClient) -> str:
    metadata = await client.get_metadata(DIRECT_IMAGE, include_sizes=True)
    assert metadata.media_type == MediaKind.IMAGE
    assert metadata.items[0].source_url == DIRECT_IMAGE
    assert metadata.size_bytes and metadata.size_bytes > 0
    return f"direct media url -> {metadata.size_human}"


async def test_crosspost_inherits_original(client: RedditClient) -> str:
    metadata = await _metadata(client, "1wotexr")
    assert metadata.media_group_type == MediaGroupType.CROSSPOST
    assert metadata.crosspost is not None and metadata.crosspost.parent is not None
    assert metadata.crosspost.parent.author, "parent metadata should be resolved"
    return f"crosspost parent = u/{metadata.crosspost.parent.author}"


async def test_rss_fallback_provider(client: RedditClient) -> str:
    """RSS alone must still produce a usable direct media link."""
    try:
        metadata = await client.get_metadata("1bak5ea", providers=["rss"], include_sizes=False)
    except RedditError as exc:
        raise Skip(f"rss provider unavailable: {exc}") from exc
    assert metadata.providers == ["rss"]
    assert metadata.title
    if metadata.items:
        assert metadata.links()["image"][0].startswith("https://i.redd.it/")
    return f"rss fallback -> {metadata.title[:40]!r}"


# --------------------------------------------------------------- download tests
async def test_download_image_and_verify_size(client: RedditClient) -> str:
    metadata = await _metadata(client, "1basx0i")
    result = await client.download(metadata, quality="best", target="memory")
    assert result.ok, result.summary()
    assert len(result.files) == 2
    for file in result.files:
        assert file.data and len(file.data) > 1000
        assert file.data[:8].startswith(b"\x89PNG") or file.data[:2] in (b"\xff\xd8", b"\x89P")
    expected = metadata.size_bytes or 0
    assert abs(result.total_bytes - expected) == 0, (result.total_bytes, expected)
    return f"2 png downloads match the pre-flight sizes ({result.total_human})"


async def test_download_gif(client: RedditClient) -> str:
    metadata = await _metadata(client, "1bb0cz8")
    result = await client.download(metadata, target="memory")
    assert result.files and result.files[0].data[:6] == b"GIF89a", result.summary()
    return f"gif bytes verified ({result.total_human})"


async def test_download_video_with_audio_muxing(client: RedditClient) -> str:
    metadata = await _metadata(client, "1wot00d")
    result = await client.download(metadata, quality=360, target="memory", mux=True)
    assert result.ok, result.summary()
    file = result.files[0]
    assert file.muxed is True, [f.filename for f in result.files]
    payload = file.read()
    assert b"ftyp" in payload[:64], "not an mp4 container"
    assert b"mp4a" in payload and b"avc1" in payload, "muxed file has no audio track"
    return f"muxed 360p video+audio ({result.total_human})"


async def test_download_legacy_video_and_save_to_disk(client: RedditClient) -> str:
    metadata = await _metadata(client, "1bawbqq")
    tmp = Path(tempfile.mkdtemp(prefix="reddit-live-"))
    try:
        result = await client.download(
            metadata,
            quality=360,
            target=DownloadTarget.DISK,
            temp_dir=tmp,
        )
        assert result.ok, result.summary()
        paths = await result.save(tmp / "out", pattern="{author}_{id}_{quality}.{ext}")
        assert paths and all(p.exists() and p.stat().st_size > 1000 for p in paths)
        head = paths[0].read_bytes()[:64]
        assert b"ftyp" in head
        return f"legacy DASH video muxed and saved as {paths[0].name}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def test_download_gallery_as_album(client: RedditClient) -> str:
    metadata = await _metadata(client, "1basx0i")
    tmp = Path(tempfile.mkdtemp(prefix="reddit-live-"))
    try:
        paths = await client.save(metadata, tmp, pattern="{id}_{index}.{ext}")
        assert len(paths) == 2
        assert paths[0].parent.name == f"{metadata.author}_{metadata.id}", paths[0].parent
        assert all(p.stat().st_size > 1000 for p in paths)
        return f"gallery saved into {paths[0].parent.name}/ ({len(paths)} files)"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def test_download_direct_media_url(client: RedditClient) -> str:
    metadata = await client.get_metadata(DIRECT_IMAGE, include_sizes=True)
    result = await client.download(metadata, target="memory")
    assert result.files and result.files[0].data[:2] == b"\xff\xd8"
    assert result.total_bytes == metadata.size_bytes
    return f"direct url download ({result.total_human})"


async def test_no_media_post_raises(client: RedditClient) -> str:
    try:
        await client.download("1wot6ta")
    except NoMediaError:
        return "self post correctly reports NoMediaError"
    raise AssertionError("a self post should not download")


async def test_progress_callbacks(client: RedditClient) -> str:
    events: list = []
    metadata = await _metadata(client, "1bak5ea")
    await client.download(metadata, target="memory", progress=events.append)
    phases = {event.phase.value for event in events}
    assert "downloading" in phases and "done" in phases, phases
    return f"{len(events)} progress events, phases={sorted(phases)}"


async def test_hls_manifest_is_reachable(client: RedditClient) -> str:
    response = await client.http.get(f"{DIRECT_VIDEO_BASE}/HLSPlaylist.m3u8", retries=1)
    if response.status != 200:
        raise Skip(f"HLS playlist not served (HTTP {response.status})")
    assert "#EXTM3U" in response.text
    return f"HLS master playlist served ({len(response.content)} bytes)"


# --------------------------------------------------------------------- runner
TESTS = [
    test_share_link_resolves,
    test_metadata_for_every_post_type,
    test_video_renditions_are_enumerated,
    test_gallery_items_are_individual_media,
    test_sizes_known_before_download,
    test_direct_cdn_url,
    test_crosspost_inherits_original,
    test_rss_fallback_provider,
    test_download_image_and_verify_size,
    test_download_gif,
    test_download_video_with_audio_muxing,
    test_download_legacy_video_and_save_to_disk,
    test_download_gallery_as_album,
    test_download_direct_media_url,
    test_no_media_post_raises,
    test_progress_callbacks,
    test_hls_manifest_is_reachable,
]

SKIP_WHEN_NO_DOWNLOAD = {
    test_download_image_and_verify_size,
    test_download_gif,
    test_download_video_with_audio_muxing,
    test_download_legacy_video_and_save_to_disk,
    test_download_gallery_as_album,
    test_download_direct_media_url,
    test_no_media_post_raises,
    test_progress_callbacks,
}


async def run_all() -> int:
    passed = failed = skipped = 0
    config = RedditConfig.from_env()
    async with RedditClient(config) as client:
        for test in TESTS:
            if not DO_DOWNLOAD and test in SKIP_WHEN_NO_DOWNLOAD:
                print(f"skip {test.__name__} (REDDIT_TEST_DOWNLOAD=0)")
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
    print(
        f"\n{passed} passed, {failed} failed, {skipped} skipped "
        f"(providers: {', '.join(config.resolved_providers())}; "
        f"oauth credentials: {'yes' if config.has_oauth_credentials else 'no'})"
    )
    print("note: without OAuth credentials the gallery/archive source is a third party")
    print("      mirror, so a random subset of posts will report partial metadata.")
    return 1 if failed else 0


def main() -> int:
    return asyncio.run(run_all())


if __name__ == "__main__":
    raise SystemExit(main())