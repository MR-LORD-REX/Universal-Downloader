"""Offline unit tests for the Twitter/X SDK (no network access required).

Run with ``python tests/test_twitter_offline.py`` or ``pytest tests``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from downloader.core.enums import FormatKind, FormatOrigin, MediaGroupType, MediaKind, Platform
from downloader.core.select import build_plan, parse_quality
from downloader.twitter.config import TwitterConfig
from downloader.twitter.models import (
    photo_formats,
    tweet_to_items,
    tweet_to_metadata,
    ytdlp_video_formats,
)
from downloader.twitter.urls import is_twitter_url, parse_url, tweet_id_of

CONFIG = TwitterConfig(probe_sizes=False)
PBS = "https://pbs.twimg.com/media"


def photo(media_id: str, *, width: int = 2048, height: int = 1152) -> dict[str, Any]:
    return {
        "type": "photo",
        "id": media_id,
        "url": f"{PBS}/{media_id}.jpg",
        "width": width,
        "height": height,
    }


def video(media_id: str, *, height: int = 1080, width: int = 1920) -> dict[str, Any]:
    return {
        "type": "video",
        "id": media_id,
        "url": f"https://video.twimg.com/amplify_video/{media_id}/vid/avc1/1280x720/out.mp4",
        "width": width,
        "height": height,
        "duration": 12.5,
        "thumbnail_url": f"{PBS}/{media_id}.jpg",
        "formats": [
            {"url": f"https://video.twimg.com/amplify_video/{media_id}/vid/avc1/640x360/a.mp4",
             "container": "mp4", "bitrate": 832000, "codec": "h264"},
            {"url": f"https://video.twimg.com/amplify_video/{media_id}/vid/avc1/1280x720/b.mp4",
             "container": "mp4", "bitrate": 2176000, "codec": "h264"},
        ],
    }


def tweet(media: list[dict[str, Any]], *, text: str = "hello world") -> dict[str, Any]:
    return {
        "id": "2102736196065521695",
        "url": "https://x.com/GenshinUniverse/status/2102736196065521695",
        "text": text,
        "created_timestamp": 1726000000,
        "lang": "en",
        "likes": 100,
        "replies": 5,
        "retweets": 20,
        "views": 1000,
        "author": {"name": "Genshin Universe", "screen_name": "GenshinUniverse"},
        "media": {"all": media, "photos": [m for m in media if m["type"] == "photo"],
                  "videos": [m for m in media if m["type"] == "video"]},
    }


# ------------------------------------------------------------------------ urls
def test_twitter_url_parsing() -> None:
    assert is_twitter_url("https://x.com/GenshinUniverse/status/2102736196065521695?s=20")
    assert is_twitter_url("https://twitter.com/i/status/2102736196065521695")
    assert is_twitter_url("https://t.co/abc123")
    assert not is_twitter_url("https://tiktok.com/@a/video/1")

    ref = parse_url("https://x.com/GenshinUniverse/status/2102736196065521695?s=20")
    assert ref.kind == "status"
    assert ref.tweet_id == "2102736196065521695"
    assert ref.handle == "GenshinUniverse"
    assert ref.status_url == "https://x.com/GenshinUniverse/status/2102736196065521695"
    assert ref.fx_url == "https://api.fxtwitter.com/i/status/2102736196065521695"
    assert parse_url("https://t.co/abc123").kind == "short"
    assert parse_url("https://x.com/GenshinImpact").kind == "profile"
    assert tweet_id_of("https://x.com/i/status/2102736196065521695") == "2102736196065521695"


# ---------------------------------------------------------------------- photos
def test_photo_variant_ladder() -> None:
    formats = photo_formats(photo("HS4fTNiaMAAQh69", width=2048, height=1152), CONFIG)
    ids = [f.format_id for f in formats]
    assert ids == ["photo-orig", "photo-large", "photo-medium", "photo-small"], ids
    orig = next(f for f in formats if f.format_id == "photo-orig")
    assert orig.url == f"{PBS}/HS4fTNiaMAAQh69.jpg?name=orig"
    assert orig.kind is FormatKind.IMAGE
    assert orig.origin is FormatOrigin.DIRECT
    assert (orig.width, orig.height) == (2048, 1152)
    small = next(f for f in formats if f.format_id == "photo-small")
    assert small.origin is FormatOrigin.PREVIEW
    # a landscape photo keeps its aspect ratio, so the width shrinks too but stays wider
    assert small.height == 680 and small.width == int(680 * 2048 / 1152)


def test_single_photo_metadata() -> None:
    meta = tweet_to_metadata(tweet([photo("abc")]), CONFIG)
    assert meta.platform is Platform.TWITTER
    assert meta.media_type is MediaKind.IMAGE
    assert meta.media_group_type is MediaGroupType.SINGLE
    assert meta.count == 1
    assert meta.author == "Genshin Universe"
    assert meta.author_id == "GenshinUniverse"
    links = meta.links()
    assert links["image"] == [f"{PBS}/abc.jpg?name=orig"]
    assert meta.thumbnail


def test_gallery_metadata() -> None:
    meta = tweet_to_metadata(tweet([photo("a"), photo("b"), photo("c")]), CONFIG)
    assert meta.media_group_type is MediaGroupType.GALLERY
    assert meta.count == 3
    assert len(meta.links()["image"]) == 3
    assert [item.index for item in meta.items] == [0, 1, 2]
    assert meta.items[0].urls[0].endswith("?name=orig")


def test_video_tweet_metadata() -> None:
    meta = tweet_to_metadata(tweet([video("vid123")]), CONFIG)
    assert meta.media_type is MediaKind.VIDEO
    assert meta.media_group_type is MediaGroupType.SINGLE
    item = meta.items[0]
    assert item.duration == 12.5
    assert item.has_audio is True
    assert item.kind is MediaKind.VIDEO
    muxed = [f for f in item.formats if f.kind is FormatKind.MUXED]
    assert len(muxed) == 3  # two fx variants plus the source url
    assert all(f.has_audio and f.has_video for f in muxed)
    # no muxing needed: twitter serves progressive mp4s
    plan = build_plan(meta.items, parse_quality("best"))
    assert plan[0].needs_mux is False


def test_mixed_media_is_an_album() -> None:
    meta = tweet_to_metadata(tweet([photo("a"), video("v")]), CONFIG)
    assert meta.media_group_type is MediaGroupType.ALBUM
    assert meta.count == 2
    assert set(meta.links()) == {"image", "video"}


def test_media_config_toggles() -> None:
    only_first = TwitterConfig(include_all_media=False, probe_sizes=False)
    assert len(tweet_to_items(tweet([photo("a"), photo("b")]), only_first)) == 1
    no_photos = TwitterConfig(include_photos=False, probe_sizes=False)
    assert tweet_to_items(tweet([photo("a")]), no_photos) == []
    no_videos = TwitterConfig(include_videos=False, probe_sizes=False)
    assert tweet_to_items(tweet([video("v")]), no_videos) == []
    large = TwitterConfig(photo_quality="large", probe_sizes=False)
    assert tweet_to_metadata(tweet([photo("a")]), large).links()["image"][0].endswith("?name=large")


# ------------------------------------------------------------------- yt-dlp map
def test_ytdlp_formats_mark_progressive_mp4_as_muxed() -> None:
    info = {
        "formats": [
            {"format_id": "http-832", "url": "https://video.twimg.com/a.mp4", "ext": "mp4",
             "protocol": "https", "width": 640, "height": 360, "tbr": 832, "filesize_approx": 3000000},
            {"format_id": "http-2176", "url": "https://video.twimg.com/b.mp4", "ext": "mp4",
             "protocol": "https", "width": 1280, "height": 720, "tbr": 2176, "filesize_approx": 7000000},
            {"format_id": "hls-1080", "url": "https://video.twimg.com/c.m3u8", "ext": "mp4",
             "protocol": "m3u8_native", "width": 1920, "height": 1080, "tbr": 5000000},
            {"format_id": "hls-audio-128000", "url": "https://video.twimg.com/audio.m3u8",
             "ext": "m4a", "protocol": "m3u8_native", "abr": 128},
        ]
    }
    formats = ytdlp_video_formats(info, CONFIG)
    by_id = {f.format_id: f for f in formats}
    assert by_id["http-832"].kind is FormatKind.MUXED
    assert by_id["http-832"].has_video and by_id["http-832"].has_audio
    assert by_id["http-832"].quality_height == 360
    assert by_id["http-2176"].quality_height == 720
    # hls duplicates do not survive when a progressive ladder exists
    assert "hls-1080" not in by_id
    assert all(not f.is_manifest for f in formats)


def test_ytdlp_formats_none_codecs_are_not_dropped() -> None:
    info = {
        "formats": [
            {"format_id": "http-10368", "url": "https://video.twimg.com/hd.mp4", "ext": "mp4",
             "protocol": "https", "width": 1920, "height": 1080, "tbr": 10368,
             "vcodec": None, "acodec": None},
        ]
    }
    formats = ytdlp_video_formats(info, CONFIG)
    assert len(formats) == 1
    assert formats[0].kind is FormatKind.MUXED
    assert formats[0].size_is_approx is True


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