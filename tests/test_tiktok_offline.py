"""Offline unit tests for the TikTok SDK (no network access required).

TikTok blocks datacentre IPs, so these tests use a synthetic yt-dlp payload
instead of hitting the network. Live extraction is covered by
``tests/test_tiktok_live.py``, which skips itself when the IP is blocked.

Run with ``python tests/test_tiktok_offline.py`` or ``pytest tests``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from downloader import Downloader
from downloader.core.enums import FormatKind, MediaGroupType, MediaKind, Platform
from downloader.core.exceptions import MetadataError
from downloader.tiktok import TikTokClient
from downloader.tiktok.config import TikTokConfig
from downloader.tiktok.exceptions import RegionBlockedError, TikTokError, VideoNotFoundError
from downloader.tiktok.extract import translate_tiktok_error
from downloader.tiktok.models import info_to_metadata, normalize_formats, playlist_to_metadata
from downloader.tiktok.urls import is_tiktok_url, parse_url, video_id_of

VIDEO_ID = "7253412088251534594"
CONFIG = TikTokConfig(probe_sizes=False)
CDN = "https://v16.tiktokcdn.com"
MP4_540 = f"{CDN}/540.mp4"
MP4_720 = f"{CDN}/720.mp4"
MP4_720_H265 = f"{CDN}/720_h265.mp4"
SOUND = f"{CDN}/sound.m4a"


def formats() -> list[dict[str, Any]]:
    return [
        {
            "format_id": "h264_540p_941613",
            "url": MP4_540,
            "ext": "mp4",
            "container": "mp4",
            "protocol": "https",
            "vcodec": "h264",
            "acodec": "aac",
            "width": 576,
            "height": 1024,
            "tbr": 941.6,
            "filesize": 1_820_000,
            "format_note": "540p",
        },
        {
            "format_id": "h264_720p_1244300",
            "url": MP4_720,
            "ext": "mp4",
            "container": "mp4",
            "protocol": "https",
            "vcodec": "h264",
            "acodec": "aac",
            "width": 720,
            "height": 1280,
            "tbr": 1244.3,
            "filesize": 2_410_000,
            "format_note": "720p",
        },
        {
            "format_id": "bytevc1_720p_999",
            "url": MP4_720_H265,
            "ext": "mp4",
            "container": "mp4",
            "protocol": "https",
            "vcodec": "bytevc1",
            "acodec": "aac",
            "width": 720,
            "height": 1280,
            "format_note": "720p",
        },
        # the watermarked rendition yt-dlp reports as ``download``
        {
            "format_id": "download",
            "url": MP4_720,
            "ext": "mp4",
            "vcodec": "h264",
            "acodec": "aac",
            "format_note": "watermarked",
        },
        # the same url again, reported as ``play`` with no note
        {
            "format_id": "play",
            "url": MP4_720,
            "ext": "mp4",
            "vcodec": "h264",
            "acodec": "aac",
            "width": 720,
            "height": 1280,
        },
        {
            "format_id": "audio",
            "url": SOUND,
            "ext": "m4a",
            "vcodec": "none",
            "acodec": "aac",
            "abr": 128,
            "filesize": 240_000,
            "format_note": "audio",
        },
    ]


def video_info(**overrides: Any) -> dict[str, Any]:
    info: dict[str, Any] = {
        "id": VIDEO_ID,
        "title": "when the beat drops",
        "description": "when the beat drops",
        "uploader": "nasa",
        "channel": "nasa",
        "uploader_id": "nasa",
        "uploader_url": "https://www.tiktok.com/@nasa",
        "webpage_url": f"https://www.tiktok.com/@nasa/video/{VIDEO_ID}",
        "duration": 15.4,
        "timestamp": 1726000000,
        "view_count": 1_200_000,
        "like_count": 98_000,
        "comment_count": 1_200,
        "repost_count": 4_300,
        "track": "original sound - nasa",
        "artist": "nasa",
        "album": "TikTok",
        "thumbnail": f"{CDN}/cover.jpg",
        "thumbnails": [
            {"url": f"{CDN}/cover-360.jpg", "width": 360, "height": 640, "id": "0"},
            {"url": f"{CDN}/cover-1080.jpg", "width": 1080, "height": 1920, "id": "1"},
        ],
        "formats": formats(),
        "extractor": "tiktok",
        "extractor_key": "TikTok",
    }
    info.update(overrides)
    return info


# ------------------------------------------------------------------- routing
def test_tiktok_url_parsing() -> None:
    video = parse_url(f"https://www.tiktok.com/@nasa/video/{VIDEO_ID}")
    assert video.kind == "video" and video.video_id == VIDEO_ID and video.username == "nasa"
    assert video_id_of(f"https://www.tiktok.com/@nasa/video/{VIDEO_ID}") == VIDEO_ID

    assert parse_url(f"https://www.tiktok.com/@/video/{VIDEO_ID}").video_id == VIDEO_ID
    assert parse_url(f"https://www.tiktok.com/embed/{VIDEO_ID}").video_id == VIDEO_ID
    assert parse_url(f"https://www.tiktok.com/embed/v2/{VIDEO_ID}").video_id == VIDEO_ID
    assert parse_url(f"https://www.tiktok.com/share/video/{VIDEO_ID}").video_id == VIDEO_ID
    assert parse_url("https://vm.tiktok.com/ZMabc123/").kind == "short"
    assert parse_url("https://vt.tiktok.com/ZMabc123/").kind == "short"

    sound = parse_url("https://www.tiktok.com/music/original-sound-7253412088251534594")
    assert sound.kind == "sound"
    assert parse_url("https://www.tiktok.com/tag/dance").kind == "tag"
    collection = parse_url(
        "https://www.tiktok.com/@nasa/collection/cool-videos-7253412088251534594"
    )
    assert collection.kind == "collection"
    assert collection.collection_id == "7253412088251534594"
    assert parse_url("https://www.tiktok.com/@nasa").kind == "profile"
    assert parse_url("https://www.tiktok.com/@nasa/live").kind == "live"

    assert is_tiktok_url("https://www.tiktok.com/@nasa/video/1")
    assert is_tiktok_url("https://www.tiktokv.com/@nasa/video/1")
    assert not is_tiktok_url("https://example.com/@nasa/video/1")


def test_unsupported_hosts_are_not_claimed() -> None:
    assert TikTokClient.supports(f"https://www.tiktok.com/@nasa/video/{VIDEO_ID}") is True
    assert TikTokClient.supports("https://example.com/@nasa/video/1") is False
    assert Downloader.platform_of("https://www.tiktok.com/@nasa/video/1") is Platform.TIKTOK
    assert Downloader.platform_of("https://vimeo.com/1") is Platform.UNKNOWN


# ------------------------------------------------------------------- formats
def test_watermarked_and_duplicate_formats_are_pruned() -> None:
    result = normalize_formats(formats(), CONFIG)
    urls = [fmt.url for fmt in result]
    assert urls == [MP4_540, MP4_720, MP4_720_H265, SOUND], urls
    assert len(urls) == len(set(urls)), "the same CDN url must not appear twice"
    # ``play`` folded its width/height into the richer ``h264_720p`` entry
    merged = next(fmt for fmt in result if fmt.url == MP4_720)
    assert merged.format_id == "h264_720p_1244300"
    assert merged.width == 720 and merged.height == 1280
    assert merged.kind is FormatKind.MUXED
    assert merged.size_bytes == 2_410_000 and merged.size_is_approx is False
    assert all(not fmt.meta.get("watermarked") for fmt in result)


def test_unplayable_renditions_are_dropped_by_default() -> None:
    payload = formats() + [
        {
            "format_id": "bytevc2_1080p_1",
            "url": f"{CDN}/1080_h266.mp4",
            "ext": "mp4",
            "vcodec": "bytevc2",
            "acodec": "aac",
            "width": 1080,
            "height": 1920,
            "format_note": "1080p unplayable",
        }
    ]
    assert all("1080_h266" not in fmt.url for fmt in normalize_formats(payload, CONFIG))
    kept = normalize_formats(payload, TikTokConfig(include_unplayable=True, probe_sizes=False))
    assert any("1080_h266" in fmt.url for fmt in kept)


def test_quality_selection_prefers_h264_and_can_downgrade() -> None:
    meta = info_to_metadata(video_info(), CONFIG)
    item = meta.items[0]
    best = item.select("best")
    assert best.url == MP4_720
    # H.264 wins the tie against the H.265 rendition of the same height
    assert best.video_codec == "h264"
    assert item.select("540p").url == MP4_540
    assert item.select("720p").url == MP4_720
    # the soundtrack is a first class format, not a muxing by-product
    assert item.audio_formats[0].url == SOUND


# ------------------------------------------------------------------ metadata
def test_video_metadata_maps_stats_track_and_sizes() -> None:
    meta = info_to_metadata(
        video_info(), CONFIG, requested_url=f"https://www.tiktok.com/@nasa/video/{VIDEO_ID}"
    )
    assert meta.platform is Platform.TIKTOK
    assert meta.id == VIDEO_ID
    assert meta.media_type is MediaKind.VIDEO
    assert meta.media_group_type is MediaGroupType.SINGLE
    assert meta.author == "nasa"
    assert meta.duration == 15.4
    assert meta.like_count == 98_000 and meta.view_count == 1_200_000
    assert meta.extra["track"] == "original sound - nasa"
    assert meta.extra["audio_only"] is False
    # the best progressive mp4 already carries its audio track: no muxing needed
    assert meta.links()["video"] == [MP4_720]
    assert meta.items[0].size_bytes == 2_410_000


def test_audio_only_post_is_surfaced_as_audio() -> None:
    payload = video_info(formats=[formats()[-1]], duration=None)
    meta = info_to_metadata(payload, CONFIG)
    assert meta.media_type is MediaKind.AUDIO
    assert meta.extra["audio_only"] is True
    assert meta.items[0].kind is MediaKind.AUDIO
    assert "audio" in meta.links()


def test_flat_playlist_becomes_a_playlist() -> None:
    entry = {"id": "1", "title": "first", "webpage_url": f"https://www.tiktok.com/@nasa/video/1",
             "formats": formats(), "thumbnails": []}
    info = {
        "id": "sound-1",
        "title": "original sound - nasa",
        "uploader": "nasa",
        "webpage_url": "https://www.tiktok.com/music/original-sound-1",
        "entries": [entry],
    }
    ref = parse_url("https://www.tiktok.com/music/original-sound-7253412088251534594")
    meta = playlist_to_metadata(info, CONFIG, ref=ref, requested_url=info["webpage_url"])
    assert meta.media_group_type is MediaGroupType.PLAYLIST
    assert meta.media_type is MediaKind.GALLERY
    assert meta.is_playlist is True
    assert meta.extra["list_kind"] == "sound"
    assert [item.index for item in meta.items] == [0]
    assert meta.items[0].select("best").url == MP4_720


# ------------------------------------------------------------------- errors
def test_error_translation_separates_blocks_from_network_failures() -> None:
    blocked = translate_tiktok_error("ERROR: [TikTok] Unable to download webpage: status code 10204")
    assert isinstance(blocked, RegionBlockedError)
    assert isinstance(blocked, TikTokError)

    private = translate_tiktok_error("You do not have permission to view this post")
    assert isinstance(private, VideoNotFoundError)

    # a timeout is NOT TikTok saying no: it has to stay a plain MetadataError
    timeout = translate_tiktok_error("ERROR: Connection to www.tiktok.com timed out")
    assert isinstance(timeout, MetadataError)
    assert not isinstance(timeout, TikTokError)


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