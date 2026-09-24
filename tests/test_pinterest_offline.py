"""Offline unit tests for the Pinterest SDK (no network access required).

Run with ``python tests/test_pinterest_offline.py`` or ``pytest tests``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from downloader import Downloader
from downloader.core.enums import FormatKind, FormatOrigin, MediaGroupType, MediaKind, Platform
from downloader.pinterest import PinterestClient
from downloader.pinterest.config import PinterestConfig
from downloader.pinterest.models import (
    api_video_formats,
    attach_video_formats,
    best_variant,
    board_to_metadata,
    image_formats,
    pin_to_metadata,
)
from downloader.pinterest.urls import is_pinterest_url, parse_url, pin_id_of

CONFIG = PinterestConfig(probe_sizes=False)

PIN_ID = "1145110643587112231"
VIDEO_ID = "1084663891475263837"
BOARD_SLUG = "cool-diys"
CDN = "https://i.pinimg.com"
VCDN = "https://v1.pinimg.com"
VIDEO_720 = f"{VCDN}/videos/mc/720p/c8/67/44/c86744113f242e233ac4470b394eddf4.mp4"
VIDEO_540 = f"{VCDN}/videos/mc/540p/c8/67/44/c86744113f242e233ac4470b394eddf4.mp4"
MANIFEST = f"{VCDN}/videos/iht/hls/abc/playlist.m3u8"


def images() -> dict[str, Any]:
    return {
        "orig": {"url": f"{CDN}/originals/aa/bb/cc.jpg", "width": 736, "height": 1307},
        "736x": {"url": f"{CDN}/736x/aa/bb/cc.jpg", "width": 736, "height": 1307},
        "474x": {"url": f"{CDN}/474x/aa/bb/cc.jpg", "width": 474, "height": 842},
        "236x": {"url": f"{CDN}/236x/aa/bb/cc.jpg", "width": 236, "height": 419},
    }


def video_list() -> dict[str, Any]:
    return {
        "V_EXP7": {
            "url": VIDEO_720,
            "width": 640,
            "height": 1138,
            "duration": 14900,
            "thumbnail": f"{CDN}/videos/thumb.jpg",
        },
        "V_EXP6": {"url": VIDEO_540, "width": 540, "height": 960, "duration": 14900},
        "V_HLSV3_MOBILE": {"url": MANIFEST, "width": 640, "height": 1138},
    }


def image_pin() -> dict[str, Any]:
    return {
        "id": PIN_ID,
        "type": "pin",
        "title": "Bird lover",
        "description": "a very good boy",
        "created_at": "Tue, 01 Aug 2023 12:00:00 +0000",
        "repin_count": 42,
        "comment_count": 3,
        "domain": "i.pinimg.com",
        "closeup_attribution": {"full_name": "Dog Lover", "username": "doglover"},
        "images": images(),
    }


def video_pin() -> dict[str, Any]:
    pin = image_pin()
    pin["id"] = VIDEO_ID
    pin["title"] = "Cool DIY"
    pin["videos"] = {"video_list": video_list()}
    return pin


# ------------------------------------------------------------------- routing
def test_pinterest_url_parsing() -> None:
    pin = parse_url(f"https://www.pinterest.com/pin/{PIN_ID}/")
    assert pin.kind == "pin" and pin.pin_id == PIN_ID
    assert pin_id_of(f"https://www.pinterest.com/pin/{PIN_ID}/") == PIN_ID

    # pinterest appends the slug to the id: ``--bird-lover--1234/``
    slug = parse_url(f"https://www.pinterest.com/pin/bird-lover--{PIN_ID}/")
    assert slug.kind == "pin" and slug.pin_id == PIN_ID

    short = parse_url("https://pin.it/3fJd2lQ")
    assert short.kind == "short"

    board = parse_url(f"https://www.pinterest.com/mashal0407/{BOARD_SLUG}/")
    assert board.kind == "board" and board.username == "mashal0407" and board.board_slug == BOARD_SLUG

    profile = parse_url("https://www.pinterest.com/mashal0407/")
    assert profile.kind == "profile" and profile.username == "mashal0407"

    for country in ("co.uk", "de", "fr", "com.mx", "ca"):
        assert is_pinterest_url(f"https://www.pinterest.{country}/pin/{PIN_ID}/"), country
    assert is_pinterest_url("https://pin.it/abc")
    assert not is_pinterest_url("https://example.com/pin/1/")


def test_unsupported_hosts_are_not_claimed() -> None:
    assert PinterestClient.supports(f"https://www.pinterest.com/pin/{PIN_ID}/") is True
    assert PinterestClient.supports("https://example.com/pin/1/") is False
    assert Downloader.platform_of("https://vimeo.com/12345") is Platform.UNKNOWN
    assert Platform.PINTEREST == "pinterest"


# -------------------------------------------------------------------- images
def test_image_rendition_ladder_prefers_the_original() -> None:
    formats = image_formats(images(), CONFIG)
    assert formats, "the rendition ladder should not be empty"
    best = formats[0]
    assert best.format_id == "image-orig"
    assert best.origin is FormatOrigin.DIRECT
    assert best.url == f"{CDN}/originals/aa/bb/cc.jpg"
    assert best.width == 736 and best.height == 1307
    assert all(fmt.kind is FormatKind.IMAGE for fmt in formats)
    assert best.size_bytes is None, "the api does not report image sizes"


def test_image_pin_metadata_points_at_the_cdn_not_the_post() -> None:
    meta = pin_to_metadata(
        image_pin(), CONFIG, requested_url=f"https://www.pinterest.com/pin/{PIN_ID}/"
    )
    assert meta.platform is Platform.PINTEREST
    assert meta.id == PIN_ID
    assert meta.media_type is MediaKind.IMAGE
    assert meta.media_group_type is MediaGroupType.SINGLE
    assert meta.title == "Bird lover"
    assert meta.author == "Dog Lover"
    assert meta.repost_count == 42

    links = meta.links()
    assert links == {"image": [f"{CDN}/originals/aa/bb/cc.jpg"]}, links
    assert "pinterest.com" not in links["image"][0]


def test_image_ladder_exposes_every_rendition_and_can_downgrade() -> None:
    meta = pin_to_metadata(image_pin(), CONFIG)
    urls = meta.items[0].urls
    assert f"{CDN}/originals/aa/bb/cc.jpg" in urls
    assert f"{CDN}/736x/aa/bb/cc.jpg" in urls
    # "worst" picks the smallest rendition so a bot can cap bandwidth usage.
    assert meta.links("worst")["image"] == [f"{CDN}/236x/aa/bb/cc.jpg"]


# -------------------------------------------------------------------- videos
def test_best_variant_prefers_the_progressive_720p_url() -> None:
    variant = best_variant(video_list())
    assert variant is not None
    assert variant[0] == "V_EXP7"
    assert variant[1]["url"] == VIDEO_720


def test_api_video_formats_are_muxed_and_skip_the_manifest() -> None:
    formats = api_video_formats(video_pin(), CONFIG)
    assert len(formats) == 1
    fmt = formats[0]
    assert fmt.kind is FormatKind.MUXED
    assert fmt.has_video and fmt.has_audio
    assert fmt.url == VIDEO_720
    assert fmt.quality_label == "640p"
    assert fmt.protocol == "https"


def test_video_pin_metadata_exposes_the_real_cdn_link() -> None:
    meta = pin_to_metadata(
        video_pin(), CONFIG, requested_url=f"https://www.pinterest.com/pin/{VIDEO_ID}/"
    )
    assert meta.media_type is MediaKind.VIDEO
    assert meta.media_group_type is MediaGroupType.SINGLE
    assert len(meta.items) == 1
    item = meta.items[0]
    assert item.select("best").url == VIDEO_720
    assert item.select("best").has_audio is True, "pinterest mp4s carry their audio track"
    assert meta.extra["is_video"] is True
    # the pin payload reports the duration per video variant, not per pin
    assert item.select("best").meta["duration"] == 14.9
    assert meta.links() == {"video": [VIDEO_720]}


def test_ytdlp_ladder_replaces_the_api_fallback() -> None:
    from downloader.core.models import MediaFormat

    meta = pin_to_metadata(video_pin(), CONFIG)
    ladder = [
        MediaFormat(
            format_id="hls-480",
            url=f"{VCDN}/videos/mc/480p/x.mp4",
            kind=FormatKind.MUXED,
            extension="mp4",
            container="mp4",
            width=480,
            height=852,
            quality_height=852,
            has_video=True,
            has_audio=True,
        ),
        MediaFormat(
            format_id="hls-1080",
            url=f"{VCDN}/videos/mc/1080p/x.mp4",
            kind=FormatKind.MUXED,
            extension="mp4",
            container="mp4",
            width=1080,
            height=1920,
            quality_height=1920,
            has_video=True,
            has_audio=True,
        ),
    ]
    assert attach_video_formats(meta, ladder) == 1
    assert "ytdlp" in meta.providers
    assert meta.items[0].select("best").url == f"{VCDN}/videos/mc/1080p/x.mp4"
    assert meta.items[0].meta["formats_source"] == "ytdlp"


# -------------------------------------------------------------------- boards
def test_board_payload_becomes_a_playlist() -> None:
    board = {"id": "585890301462791043", "name": "Cool DIYs", "owner": {"username": "mashal0407"}}
    meta = board_to_metadata(
        [image_pin(), video_pin()],
        board,
        CONFIG,
        requested_url=f"https://www.pinterest.com/mashal0407/{BOARD_SLUG}/",
    )
    assert meta.media_group_type is MediaGroupType.PLAYLIST
    assert meta.media_type is MediaKind.GALLERY
    assert meta.is_playlist is True
    assert len(meta.items) == 2
    assert [item.index for item in meta.items] == [0, 1]
    assert meta.extra["board_id"] == "585890301462791043"
    grouped = meta.links()
    assert set(grouped) == {"image", "video"}, grouped


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