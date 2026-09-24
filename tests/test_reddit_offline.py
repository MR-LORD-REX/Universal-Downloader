"""Offline unit tests for the reddit SDK (no network access required).

Run with ``python tests/test_reddit_offline.py`` or ``pytest tests``.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from downloader.reddit.client import _is_rich, _merge_payloads
from downloader.reddit.formats import _mark_separate_audio, build_items, post_view
from downloader.reddit.manifests import parse_hls_master, parse_hls_media, parse_mpd
from downloader.reddit.models import (
    DownloadResult,
    DownloadedFile,
    FormatKind,
    FormatOrigin,
    MediaFormat,
    MediaGroupType,
    MediaItem,
    MediaKind,
    PostMetadata,
    human_size,
    pick_format,
)
from downloader.reddit.providers import normalize_payload
from downloader.reddit.saver import render_pattern, save_result
from downloader.reddit.urls import parse as parse_url

MPD_FIXTURE = """<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" mediaPresentationDuration="PT10S" type="static">
  <Period id="0">
    <AdaptationSet contentType="video" id="0">
      <Representation bandwidth="227060" codecs="avc1.4d401e" height="392" id="0" mimeType="video/mp4" width="220">
        <BaseURL>CMAF_220.mp4</BaseURL>
      </Representation>
      <Representation bandwidth="808920" codecs="avc1.4d401f" height="640" id="2" mimeType="video/mp4" width="360">
        <BaseURL>CMAF_360.mp4</BaseURL>
      </Representation>
    </AdaptationSet>
    <AdaptationSet contentType="audio" id="1">
      <Representation audioSamplingRate="48000" bandwidth="131404" codecs="mp4a.40.2" id="7" mimeType="audio/mp4">
        <BaseURL>CMAF_AUDIO_128.mp4</BaseURL>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>
"""

HLS_MASTER = """#EXTM3U
#EXT-X-VERSION:4
#EXT-X-MEDIA:URI="CMAF_AUDIO_128.m3u8",TYPE=AUDIO,GROUP-ID="6",NAME="audio 0",DEFAULT=YES
#EXT-X-STREAM-INF:PROGRAM-ID=0,BANDWIDTH=940324,RESOLUTION=360x640,FRAME-RATE=30,CODECS="avc1.4d401e,mp4a.40.2",AUDIO="6"
CMAF_360.m3u8
#EXT-X-STREAM-INF:PROGRAM-ID=0,BANDWIDTH=2430156,RESOLUTION=720x1280,FRAME-RATE=30,CODECS="avc1.4d401f,mp4a.40.2",AUDIO="6"
CMAF_720.m3u8
"""

HLS_MEDIA = """#EXTM3U
#EXT-X-VERSION:7
#EXT-X-TARGETDURATION:4
#EXT-X-MAP:URI="init.mp4"
#EXTINF:4.0,
seg-1.m4s
#EXTINF:4.0,
seg-2.m4s
"""

GALLERY_PAYLOAD = {
    "id": "1worqtp",
    "title": "Thanks for 14 amazing years Diego",
    "author": "funseeker1707",
    "subreddit": "pics",
    "permalink": "/r/pics/comments/1worqtp/thanks/",
    "is_gallery": True,
    "url": "https://www.reddit.com/gallery/1worqtp",
    "created_utc": 1790000000.0,
    "over_18": False,
    "gallery_data": {
        "items": [
            {"media_id": "eb2yy94x8erh1", "id": 1},
            {"media_id": "zyglyr4x8erh1", "id": 2},
        ]
    },
    "media_metadata": {
        "eb2yy94x8erh1": {
            "status": "valid",
            "e": "Image",
            "m": "image/jpg",
            "s": {"x": 3024, "y": 4032, "u": "https://preview.redd.it/eb2yy94x8erh1.jpg?width=3024&s=abc"},
            "p": [{"x": 108, "y": 144, "u": "https://preview.redd.it/eb2yy94x8erh1.jpg?width=108&s=def"}],
        },
        "zyglyr4x8erh1": {
            "status": "valid",
            "e": "AnimatedImage",
            "m": "image/gif",
            "s": {
                "x": 640,
                "y": 480,
                "mp4": "https://preview.redd.it/zyglyr4x8erh1.mp4?s=ghi",
                "gif": "https://preview.redd.it/zyglyr4x8erh1.gif?s=jkl",
            },
        },
    },
}

VIDEO_PAYLOAD = {
    "id": "1wot00d",
    "title": "Demanding playtime with tiny meows.",
    "author": "Xiu-xing",
    "subreddit": "aww",
    "permalink": "/r/aww/comments/1wot00d/demanding/",
    "is_video": True,
    "domain": "v.redd.it",
    "url": "https://v.redd.it/cxdv6o5tkerh1",
    "secure_media": {
        "reddit_video": {
            "bitrate_kbps": 5000,
            "fallback_url": "https://v.redd.it/cxdv6o5tkerh1/CMAF_1080.mp4?source=fallback",
            "has_audio": True,
            "height": 1920,
            "width": 1080,
            "duration": 42,
            "is_gif": False,
        }
    },
}

CROSSPOST_PAYLOAD = {
    "id": "1wotexr",
    "title": "crosspost",
    "author": "reposter",
    "subreddit": "pics",
    "crosspost_parent": "t3_1woteah",
    "url": "https://i.redd.it/vu0fwlo9perh1.jpeg",
    "crosspost_parent_list": [
        {
            "id": "1woteah",
            "title": "Golden light",
            "author": "original",
            "subreddit": "CityPorn",
            "url": "https://i.redd.it/vu0fwlo9perh1.jpeg",
            "permalink": "/r/CityPorn/comments/1woteah/golden/",
        }
    ],
}


# ------------------------------------------------------------------- url parsing
def test_parse_post_urls() -> None:
    ref = parse_url("https://www.reddit.com/r/pics/comments/1worqtp/thanks/")
    assert ref.kind == "post" and ref.post_id == "1worqtp" and ref.subreddit == "pics", ref
    assert ref.permalink == "/r/pics/comments/1worqtp/"
    assert ref.full_permalink == "https://www.reddit.com/r/pics/comments/1worqtp/"

    assert parse_url("1worqtp").post_id == "1worqtp"
    assert parse_url("t3_1worqtp").post_id == "1worqtp"
    assert parse_url("https://redd.it/1worqtp").post_id == "1worqtp"
    assert parse_url("https://www.reddit.com/comments/1worqtp").post_id == "1worqtp"
    assert parse_url("https://www.reddit.com/gallery/1worqtp").post_id == "1worqtp"


def test_parse_share_and_media_urls() -> None:
    share = parse_url("https://www.reddit.com/r/Endfield/s/ioO2w6NTSS")
    assert share.kind == "share" and share.token == "ioO2w6NTSS" and share.needs_resolution

    image = parse_url("https://i.redd.it/967qwrrqf7rh1.jpeg")
    assert image.kind == "media" and image.media_kind == "image"

    video = parse_url("https://v.redd.it/cxdv6o5tkerh1/CMAF_720.mp4")
    assert video.kind == "media" and video.media_kind == "video"

    listing = parse_url("https://www.reddit.com/r/pics/")
    assert listing.kind == "listing" and listing.subreddit == "pics"

    external = parse_url("https://i.imgur.com/uHwFTUa.jpg")
    assert external.kind == "external" and external.media_kind == "image"


# ------------------------------------------------------------------ format logic
def test_pick_format_quality_policies() -> None:
    formats = [
        MediaFormat(format_id="220", url="u/220.mp4", kind=FormatKind.VIDEO, quality_height=220, height=392, width=220, size_bytes=100),
        MediaFormat(format_id="360", url="u/360.mp4", kind=FormatKind.VIDEO, quality_height=360, height=640, width=360, size_bytes=300),
        MediaFormat(format_id="720", url="u/720.mp4", kind=FormatKind.VIDEO, quality_height=720, height=1280, width=720, size_bytes=600),
    ]
    assert pick_format(formats, "best").format_id == "720"
    assert pick_format(formats, "worst").format_id == "220"
    assert pick_format(formats, "smallest").format_id == "220"
    assert pick_format(formats, 360).format_id == "360"
    assert pick_format(formats, "480p").format_id == "360"
    assert pick_format(formats, 144).format_id == "220"  # nothing fits -> smallest


def test_pick_format_prefers_muxed_audio() -> None:
    formats = [
        MediaFormat(format_id="plain", url="u/a.mp4", kind=FormatKind.VIDEO, quality_height=720, size_bytes=500),
        MediaFormat(format_id="muxed", url="u/b.mp4", kind=FormatKind.MUXED, quality_height=720, size_bytes=600, has_audio=True),
    ]
    assert pick_format(formats, "best").format_id == "muxed"


def test_manifest_parsers() -> None:
    reps = parse_mpd(MPD_FIXTURE, "https://v.redd.it/abc/DASHPlaylist.mpd")
    assert [r.filename for r in reps] == ["CMAF_220.mp4", "CMAF_360.mp4", "CMAF_AUDIO_128.mp4"]
    assert [r.content_type for r in reps] == ["video", "video", "audio"]
    assert reps[0].url == "https://v.redd.it/abc/CMAF_220.mp4" and reps[0].height == 392

    variants = parse_hls_master(HLS_MASTER, "https://v.redd.it/abc/HLSPlaylist.m3u8")
    assert [(v.height, v.bandwidth) for v in variants] == [(640, 940324), (1280, 2430156)]
    assert variants[0].url.endswith("CMAF_360.m3u8")

    media = parse_hls_media(HLS_MEDIA, "https://v.redd.it/abc/CMAF_360.m3u8")
    assert media.is_master is False
    assert media.init_segment == "https://v.redd.it/abc/init.mp4"
    assert media.segments == ["https://v.redd.it/abc/seg-1.m4s", "https://v.redd.it/abc/seg-2.m4s"]
    assert media.duration == 8.0

    assert parse_hls_media(HLS_MASTER, "https://v.redd.it/abc/HLSPlaylist.m3u8").is_master is True


# ------------------------------------------------------------------- item building
def test_gallery_items() -> None:
    items = build_items(GALLERY_PAYLOAD)
    assert [i.index for i in items] == [0, 1]
    assert items[0].kind == MediaKind.IMAGE
    assert items[1].kind == MediaKind.GIF
    assert items[0].source_url == "https://i.redd.it/eb2yy94x8erh1.jpg"
    assert items[1].source_url == "https://i.redd.it/zyglyr4x8erh1.gif"

    view = post_view(GALLERY_PAYLOAD, items)
    assert view.media_type == MediaKind.GALLERY
    assert view.media_group_type == MediaGroupType.GALLERY
    assert items[0].width == 3024 and items[0].height == 4032
    ids = {f.format_id for f in items[1].formats}
    assert "source-mp4" in ids and "source-gif" in ids


def test_video_item_and_view() -> None:
    items = build_items(VIDEO_PAYLOAD)
    assert len(items) == 1
    item = items[0]
    assert item.kind == MediaKind.VIDEO and item.has_audio is True
    assert item.meta["base_url"] == "https://v.redd.it/cxdv6o5tkerh1"
    assert item.formats[0].quality_height == 1080
    assert item.formats[0].width == 1080 and item.formats[0].height == 1920
    view = post_view(VIDEO_PAYLOAD, items)
    assert view.media_type == MediaKind.VIDEO
    assert view.video is not None and view.video.duration == 42


def test_crosspost_view() -> None:
    items = build_items(CROSSPOST_PAYLOAD)
    view = post_view(CROSSPOST_PAYLOAD, items)
    assert view.media_group_type == MediaGroupType.CROSSPOST
    assert view.crosspost_id == "t3_1woteah"


def test_post_links_grouping_and_sizes() -> None:
    items = build_items(GALLERY_PAYLOAD)
    items[0].size_bytes = 1000
    items[1].size_bytes = 2000
    metadata = PostMetadata(
        id="1worqtp",
        title="t",
        author="a",
        media_type=MediaKind.GALLERY,
        media_group_type=MediaGroupType.GALLERY,
        items=items,
    ).rebuild_groups()
    grouped = metadata.links()
    assert set(grouped) == {"image", "gif"}
    assert grouped["image"] == ["https://i.redd.it/eb2yy94x8erh1.jpg"]
    flat = metadata.links(grouped=False)
    assert isinstance(flat, list) and len(flat) == 2
    assert metadata.size_bytes == 3000 and metadata.size_human == "2.93 KB"
    assert metadata.group("image").count == 1
    assert metadata.is_album is True


# ---------------------------------------------------------------- payload merging
def test_payload_normalisation_and_merge() -> None:
    listing = {"kind": "Listing", "data": {"children": [{"kind": "t3", "data": GALLERY_PAYLOAD}]}}
    assert normalize_payload(listing)["id"] == "1worqtp"
    assert normalize_payload([listing, listing])["id"] == "1worqtp"
    assert normalize_payload(GALLERY_PAYLOAD)["id"] == "1worqtp"
    assert normalize_payload({"data": {"children": []}}) is None

    partial = {"id": "x", "title": "from rss", "author": None, "url": None}
    rich = {"id": "x", "author": "real", "url": "https://i.redd.it/a.jpeg", "media_metadata": {"a": {}}}
    merged = _merge_payloads(partial, rich)
    assert merged["author"] == "real"
    assert merged["title"] == "from rss"
    assert _is_rich(merged) is True
    assert _is_rich({"id": "x", "title": "t", "url": "https://example.com/thing"}) is True
    assert _is_rich({"id": "x", "title": "t"}) is False


# ------------------------------------------------------------------ audio flags
def test_per_track_video_is_not_marked_as_having_audio() -> None:
    """Reddit's CMS/CMAF video files carry no audio; the payload flag lies."""
    item = MediaItem(
        index=0,
        kind=MediaKind.VIDEO,
        has_audio=True,
        formats=[
            MediaFormat(
                format_id="direct-1080",
                url="https://v.redd.it/abc/CMAF_1080.mp4?source=fallback",
                kind=FormatKind.VIDEO,
                origin=FormatOrigin.DIRECT,
                has_audio=True,
                quality_height=1080,
            ),
            MediaFormat(
                format_id="audio-128",
                url="https://v.redd.it/abc/CMAF_AUDIO_128.mp4",
                kind=FormatKind.AUDIO,
                origin=FormatOrigin.DIRECT,
                has_audio=True,
            ),
        ],
    )
    _mark_separate_audio(item)
    assert next(f for f in item.formats if f.format_id == "direct-1080").has_audio is False
    # the post itself still has audio, it is just served separately
    assert item.has_audio is True

    # a truly muxed single-file video (no separate audio) keeps the flag
    muxed = MediaItem(
        index=0,
        kind=MediaKind.VIDEO,
        formats=[
            MediaFormat(
                format_id="direct-720",
                url="https://v.redd.it/abc/DASH_720.mp4",
                kind=FormatKind.VIDEO,
                origin=FormatOrigin.DIRECT,
                has_audio=True,
            )
        ],
    )
    _mark_separate_audio(muxed)
    assert next(f for f in muxed.formats if f.format_id == "direct-720").has_audio is True


# ------------------------------------------------------------------------- saver
def test_render_pattern_and_sanitise() -> None:
    metadata = PostMetadata(id="abc123", author="Some/One", subreddit="pics", title="Hi: there?")
    name = render_pattern("{author}_{id}_{index}_{quality}.{ext}", metadata=metadata, index=2)
    assert name == "Some_One_abc123_2_media.bin", name
    assert "/" not in name and ":" not in name and "?" not in name


def test_save_result_writes_files() -> None:
    async def scenario() -> None:
        metadata = PostMetadata(id="abc", author="me", title="t", items=[])
        files = [
            DownloadedFile(item_index=0, kind=MediaKind.IMAGE, filename="a.jpg", data=b"hello"),
            DownloadedFile(item_index=1, kind=MediaKind.IMAGE, filename="b.jpg", data=b"world!"),
        ]
        result = DownloadResult(metadata=metadata, files=files)
        with tempfile.TemporaryDirectory() as tmp:
            paths = await save_result(result, tmp, pattern="{id}_{index}.{ext}", album_dir=False)
            assert [p.name for p in paths] == ["abc_1.jpg", "abc_2.jpg"]
            assert paths[0].read_bytes() == b"hello"
            again = await save_result(result, tmp, pattern="{id}_{index}.{ext}", album_dir=False)
            assert again[0].name == "abc_1_1.jpg"  # never clobbers
            overwritten = await save_result(
                result, tmp, pattern="{id}_{index}.{ext}", album_dir=False, overwrite=True
            )
            assert overwritten[0].name == "abc_1.jpg"

    asyncio.run(scenario())


def test_save_result_album_directory() -> None:
    async def scenario() -> None:
        metadata = PostMetadata(
            id="abc",
            author="me",
            items=[MediaItem(index=0, kind=MediaKind.IMAGE), MediaItem(index=1, kind=MediaKind.IMAGE)],
        )
        files = [
            DownloadedFile(item_index=0, kind=MediaKind.IMAGE, filename="a.jpg", data=b"x"),
            DownloadedFile(item_index=1, kind=MediaKind.IMAGE, filename="b.jpg", data=b"y"),
        ]
        result = DownloadResult(metadata=metadata, files=files)
        with tempfile.TemporaryDirectory() as tmp:
            paths = await save_result(result, tmp, pattern="{id}_{index}.{ext}")
            assert paths[0].parent.name == "me_abc", paths[0]
            assert paths[0].name == "abc_1.jpg"

    asyncio.run(scenario())


def test_human_size() -> None:
    assert human_size(None) is None
    assert human_size(0) == "0 B"
    assert human_size(474334) == "463.22 KB"
    assert human_size(19320004) == "18.42 MB"


def test_select_all_plan() -> None:
    formats = [
        MediaFormat(format_id="v", url="u/v.mp4", kind=FormatKind.VIDEO, quality_height=720, size_bytes=10),
        MediaFormat(format_id="a", url="u/a.mp4", kind=FormatKind.AUDIO, bitrate_kbps=128, size_bytes=2),
    ]
    item = MediaItem(index=0, kind=MediaKind.VIDEO, has_audio=True, formats=formats)
    metadata = PostMetadata(id="x", items=[item], media_type=MediaKind.VIDEO)
    plan = metadata.select_all("best")
    assert len(plan) == 1
    item_, primary, audio = plan[0]
    assert primary.format_id == "v" and audio is not None and audio.format_id == "a"
    # no audio wanted -> no second track
    assert metadata.select_all("best", include_audio=False)[0][2] is None


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    failures = 0
    for test in tests:
        try:
            test()
        except Exception as exc:  # noqa: BLE001 - tiny home grown runner
            failures += 1
            print(f"FAIL {test.__name__}: {type(exc).__name__}: {exc}")
        else:
            print(f"pass {test.__name__}")
    print(f"\n{len(tests) - failures}/{len(tests)} offline tests passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())