"""Offline unit tests for the Instagram SDK (no network access required).

Run with ``python tests/test_instagram_offline.py``.

The DASH fixture below is a trimmed copy of a real Instagram
``video_dash_manifest``: same namespace, same attribute names, byte-exact
``FBContentLength`` values. It is deliberately *not* pretty printed, because the
real payload is not either and the reader has to cope with namespaced tags.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from downloader.core.enums import FormatKind, FormatOrigin, MediaGroupType, MediaKind, Platform
from downloader.core.exceptions import InvalidURLError, MetadataError, UnsupportedURLError
from downloader.core.manifests import parse_dash_tracks
from downloader.core.select import build_plan, parse_quality
from downloader.instagram.client import translate_instagram_error
from downloader.instagram.config import InstagramConfig
from downloader.instagram.exceptions import (
    InstagramNotFoundError,
    InstaloaderMissingError,
    LoginRequiredError,
    PrivateProfileError,
    ProfileNotFoundError,
    RateLimitedError,
)
from downloader.instagram.models import (
    dash_formats,
    image_formats,
    ladder_height,
    post_to_metadata,
    progressive_video_formats,
)
from downloader.instagram.urls import is_instagram_url, parse_url, shortcode_of

try:  # instaloader is optional: skip the cases that need it when absent
    import instaloader.exceptions as ig_errors
except ImportError:  # pragma: no cover - declared in requirements
    ig_errors = None

CONFIG = InstagramConfig(probe_sizes=False)
CDN = "https://scontent.cdninstagram.com/v/t51.2885-15"

# --------------------------------------------------------------- DASH fixture
DASH_FIXTURE = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
    ' xsi:schemaLocation="urn:mpeg:dash:schema:mpd:2011 DASH-MPD.xsd"'
    ' profiles="urn:mpeg:dash:profile:isoff-on-demand:2011" minBufferTime="PT2S" type="static"'
    ' mediaPresentationDuration="PT6.500136S" FBManifestTimestamp="1790268725">'
    '<Period id="0" duration="PT6.500136S">'
    '<AdaptationSet id="0" contentType="video" subsegmentAlignment="true" par="9:16"'
    ' FBUnifiedUploadResolutionMos="360:84.5">'
    '<Representation id="2088627142021665v" bandwidth="259902" codecs="vp09.00.31.08.00.01.01.01.00"'
    ' mimeType="video/mp4" sar="1:1" FBEncodingTag="dash_r2evevp9-r1gen2vp9_q20" FBContentLength="211171"'
    ' FBAbrPolicyTags="" width="720" height="1280" frameRate="15360/512" FBQualityClass="hd"'
    ' FBQualityLabel="240p">'
    '<BaseURL>https://instagram.fnag1-7.fna.fbcdn.net/o1/v/t2/f2/m367/240.mp4?_nc_cat=109&amp;_nc_sid=9ca052'
    '&amp;_nc_ht=instagram.fnag1-7.fna.fbcdn.net&amp;oe=68D1AA11</BaseURL>'
    '<SegmentBase indexRange="818-873" timescale="15360"><Initialization range="0-817"/></SegmentBase>'
    '</Representation>'
    '<Representation id="2088627128688333v" bandwidth="1966807" codecs="vp09.00.31.08.00.01.01.01.00"'
    ' mimeType="video/mp4" sar="1:1" FBEncodingTag="dash_r2evevp9-r1gen2vp9_q80" FBContentLength="1598031"'
    ' FBAbrPolicyTags="" width="720" height="1280" frameRate="15360/512" FBQualityClass="hd"'
    ' FBQualityLabel="720p">'
    '<BaseURL>https://instagram.fnag1-4.fna.fbcdn.net/o1/v/t2/f2/m367/720.mp4?_nc_cat=108&amp;_nc_sid=9ca052'
    '&amp;_nc_ht=instagram.fnag1-4.fna.fbcdn.net&amp;oe=68D1AA22</BaseURL>'
    '<SegmentBase indexRange="818-873" timescale="15360"><Initialization range="0-817"/></SegmentBase>'
    '</Representation>'
    '<Representation id="2088627145354998v" bandwidth="2911320" codecs="vp09.00.40.08.00.01.01.01.00"'
    ' mimeType="video/mp4" sar="1:1" FBEncodingTag="dash_r2evevp9-r1gen2vp9_q90" FBContentLength="2365448"'
    ' FBAbrPolicyTags="" width="1080" height="1920" frameRate="15360/512" FBQualityClass="hd"'
    ' FBQualityLabel="1080p">'
    '<BaseURL>https://instagram.fnag6-2.fna.fbcdn.net/o1/v/t2/f2/m367/1080.mp4?_nc_cat=110&amp;_nc_sid=9ca052'
    '&amp;_nc_ht=instagram.fnag6-2.fna.fbcdn.net&amp;oe=68D1AA33</BaseURL>'
    '<SegmentBase indexRange="818-873" timescale="15360"><Initialization range="0-817"/></SegmentBase>'
    '</Representation>'
    '</AdaptationSet>'
    '<AdaptationSet id="1" contentType="audio" subsegmentStartsWithSAP="1" subsegmentAlignment="true">'
    '<Representation id="2088623268688719a" bandwidth="44077" codecs="mp4a.40.5" mimeType="audio/mp4"'
    ' FBAvgBitrate="44077" audioSamplingRate="44100" FBEncodingTag="dash_ln_heaac_vbr3_audio"'
    ' FBContentLength="36717" FBAbrPolicyTags="">'
    '<AudioChannelConfiguration schemeIdUri="urn:mpeg:dash:23003:3:audio_channel_configuration:2011"'
    ' value="2"/>'
    '<BaseURL>https://instagram.fnag1-2.fna.fbcdn.net/o1/v/t2/f2/m78/audio.mp4?_nc_cat=104&amp;_nc_sid=9ca052'
    '&amp;_nc_ht=instagram.fnag1-2.fna.fbcdn.net&amp;oe=68D1AA44</BaseURL>'
    '<SegmentBase indexRange="824-903" timescale="44100"><Initialization range="0-823"/></SegmentBase>'
    '</Representation>'
    '</AdaptationSet>'
    '</Period>'
    '</MPD>'
)


class raises:
    """Tiny ``pytest.raises`` stand-in (the suite has no pytest dependency)."""

    def __init__(self, exc: type) -> None:
        self.exc = exc

    def __enter__(self) -> "raises":
        return self

    def __exit__(self, kind, value, traceback) -> bool:
        assert kind is not None and issubclass(kind, self.exc), (
            f"expected {self.exc.__name__}, got {kind and kind.__name__}"
        )
        return True


class Post:
    """Minimal ``instaloader.Post`` stand-in: only what the mapper reads."""

    def __init__(
        self,
        node: dict[str, Any],
        *,
        typename: str = "GraphImage",
        shortcode: str = "TESTCODE1",
        owner_username: str = "tester",
        owner_id: str = "42",
    ) -> None:
        self._node = node
        self.typename = typename
        self.shortcode = shortcode
        self.owner_username = owner_username
        self.owner_id = owner_id


# ------------------------------------------------------------------ node builders
def image_node(pk: int = 11, *, width: int = 1080, height: int = 1350) -> dict[str, Any]:
    candidates = [
        {"url": f"{CDN}/{pk}_n.jpg?stp=dst-jpg&oe=68D1AA55", "width": width, "height": height},
        {"url": f"{CDN}/{pk}_n.jpg?stp=dst-jpg&oe=68D1AA55&w=480", "width": 480, "height": 600},
        {"url": f"{CDN}/{pk}_n.jpg?stp=dst-jpg&oe=68D1AA55&w=320", "width": 320, "height": 400},
    ]
    return {
        "pk": pk,
        "media_type": 1,
        "code": "TESTCODE1",
        "original_width": width,
        "original_height": height,
        "image_versions2": {"candidates": candidates},
    }


def video_node(pk: int = 13, *, dash: bool = True) -> dict[str, Any]:
    node: dict[str, Any] = {
        "pk": pk,
        "media_type": 2,
        "code": "TESTCODE1",
        "original_width": 1080,
        "original_height": 1920,
        "video_duration": 6.61,
        "video_view_count": 1234,
        "is_dash_eligible": "1",
        "number_of_qualities": 8,
        "video_versions": [
            {"url": f"{CDN}/{pk}_progress.mp4?oe=68D1AA66", "width": 720, "height": 1280, "type": 101},
            {"url": f"{CDN}/{pk}_progress_low.mp4?oe=68D1AA66", "width": 480, "height": 854, "type": 101},
        ],
    }
    if dash:
        node["video_dash_manifest"] = DASH_FIXTURE
    return node


def carousel_node(*children: dict[str, Any]) -> dict[str, Any]:
    return {
        "pk": 1,
        "media_type": 8,
        "code": "CAROUSEL1",
        "carousel_media_count": len(children),
        "carousel_media": list(children),
    }


# -------------------------------------------------------------------------- urls
def test_instagram_url_parsing() -> None:
    for url in (
        "https://www.instagram.com/p/DdPEofvmpPd/",
        "https://instagram.com/reel/DdhvW0GslGe/",
        "https://www.instagram.com/reels/DdSA4hPs3IZ/",
        "https://www.instagram.com/tv/ABC123xyz/",
        "https://instagr.am/p/DdPEofvmpPd/",
        "https://ig.me/p/DdPEofvmpPd/",
        "https://m.instagram.com/reel/DdhvW0GslGe/?utm_source=ig_web_copy_link",
        "https://www.instagram.com/someuser/reel/DdhvW0GslGe/",
    ):
        assert is_instagram_url(url), url

    ref = parse_url("https://www.instagram.com/p/DdPEofvmpPd/")
    assert ref.kind == "post" and ref.shortcode == "DdPEofvmpPd"
    assert ref.post_url == "https://www.instagram.com/p/DdPEofvmpPd/"
    assert ref.is_post and not ref.is_story and not ref.is_profile
    assert ref.canonical == ref.post_url
    # every shortcode form canonicalises onto /p/<code>/
    assert parse_url("https://www.instagram.com/reel/DdhvW0GslGe/").post_url.endswith(
        "/p/DdhvW0GslGe/"
    )
    # a profile prefix is recorded but does not change the shortcode
    assert parse_url("https://www.instagram.com/someuser/reel/DdhvW0GslGe/").username == "someuser"

    story = parse_url("https://www.instagram.com/stories/someuser/3301234567890/")
    assert story.kind == "story" and story.is_story
    assert story.username == "someuser" and story.story_media_id == "3301234567890"
    highlight = parse_url("https://www.instagram.com/stories/highlights/17900000000000000/")
    assert highlight.kind == "highlight" and highlight.is_story

    profile = parse_url("https://www.instagram.com/someuser/")
    assert profile.kind == "profile" and profile.is_profile

    assert shortcode_of("https://www.instagram.com/p/DdPEofvmpPd/") == "DdPEofvmpPd"
    assert shortcode_of("https://www.instagram.com/someuser/") is None


def test_instagram_url_rejects_foreign_and_unknown_forms() -> None:
    for url in (
        "https://x.com/GenshinImpact/status/2102941090571485331",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://instgram.com/p/DdPEofvmpPd/",
        "https://example.com/p/DdPEofvmpPd/",
        "not-a-url",
        "",
    ):
        assert not is_instagram_url(url), url

    with raises(InvalidURLError):
        parse_url("https://x.com/GenshinImpact/status/2102941090571485331")
    with raises(InvalidURLError):  # bare host, nothing to resolve
        parse_url("https://www.instagram.com/")
    with raises(InvalidURLError):  # the /l/ redirector carries no shortcode
        parse_url("https://l.instagram.com/?u=https%3A%2F%2Fexample.com")
    with raises(InvalidURLError):  # stories need a media id
        parse_url("https://www.instagram.com/stories/someuser/")


# ------------------------------------------------------------------ ladder height
def test_ladder_height_is_the_short_side_or_the_label() -> None:
    # portrait reel: Instagram calls 720x1280 "720p", i.e. the *short* side
    assert ladder_height(720, 1280) == 720
    assert ladder_height(1080, 1920) == 1080
    # the FBQualityLabel wins when present (it is authoritative)
    assert ladder_height(720, 1280, "1080p") == 1080
    # square carousel frames
    assert ladder_height(1080, 1080) == 1080
    assert ladder_height(None, None) is None


# --------------------------------------------------------------------- manifests
def test_dash_manifest_parses_with_exact_sizes() -> None:
    tracks = parse_dash_tracks(DASH_FIXTURE, "https://instagram.fnag1-7.fna.fbcdn.net/x")
    assert len(tracks) == 4, [t.track_id for t in tracks]
    video = [t for t in tracks if t.is_video]
    audio = [t for t in tracks if t.is_audio]
    assert len(video) == 3 and len(audio) == 1

    ladder = {t.quality_label: t for t in video}
    assert set(ladder) == {"240p", "720p", "1080p"}
    # every video rendition is the same 720x1280 frame except the 1080p tier
    assert (ladder["240p"].width, ladder["240p"].height) == (720, 1280)
    assert (ladder["1080p"].width, ladder["1080p"].height) == (1080, 1920)
    assert ladder["1080p"].size_bytes == 2365448
    assert ladder["1080p"].bandwidth == 2911320
    assert ladder["1080p"].frame_rate == 30.0
    assert ladder["1080p"].codecs.startswith("vp09")
    assert ladder["240p"].extension == "mp4"

    track = audio[0]
    assert track.codecs == "mp4a.40.5"
    assert track.size_bytes == 36717
    assert track.audio_sampling_rate == 44100 and track.audio_channels == 2
    # namespaced tags are found and XML entities are decoded in the BaseURL
    assert track.url.startswith("https://instagram.fnag1-2.fna.fbcdn.net/")
    assert "&amp;" not in track.url and "_nc_cat=104" in track.url


def test_dash_manifest_is_opt_in() -> None:
    node = video_node()
    assert dash_formats(node, CONFIG) == []
    formats = dash_formats(node, InstagramConfig(include_dash=True, probe_sizes=False))
    assert len(formats) == 4
    video = [f for f in formats if f.kind is FormatKind.VIDEO]
    audio = [f for f in formats if f.kind is FormatKind.AUDIO]
    assert len(video) == 3 and len(audio) == 1
    assert all(f.origin is FormatOrigin.DASH for f in formats)
    assert all(f.has_video and not f.has_audio for f in video)
    top = max(video, key=lambda f: f.quality_height or 0)
    assert top.quality_height == 1080 and top.size_source == "manifest"
    # the audio track is an m4a even though instagram serves it as .mp4
    assert audio[0].extension == "m4a" and audio[0].has_audio


def test_dash_manifest_survives_a_broken_payload() -> None:
    assert parse_dash_tracks("", "https://x/") == []
    assert dash_formats({"video_dash_manifest": "<not xml"}, InstagramConfig(include_dash=True)) == []


# ----------------------------------------------------------------------- formats
def test_image_candidate_ladder_is_largest_first() -> None:
    formats = image_formats(image_node(), CONFIG)
    assert [f.format_id for f in formats] == ["image-1080x1350", "image-480x600", "image-320x400"]
    assert all(f.kind is FormatKind.IMAGE and f.has_video is False for f in formats)
    assert formats[0].quality_height == 1080
    assert formats[0].extension == "jpg" and formats[0].mime_type == "image/jpeg"
    assert formats[0].url.startswith(CDN)
    # the candidate cap is honoured
    capped = image_formats(image_node(), InstagramConfig(max_image_candidates=1, probe_sizes=False))
    assert len(capped) == 1 and capped[0].format_id == "image-1080x1350"


def test_progressive_video_is_already_muxed() -> None:
    formats = progressive_video_formats(video_node())
    assert [f.format_id for f in formats] == ["progressive-720x1280", "progressive-480x854"]
    assert all(f.kind is FormatKind.MUXED for f in formats)
    assert all(f.has_video and f.has_audio for f in formats)
    assert all(f.container == "mp4" for f in formats)
    # quality comes from the short side, not the height
    assert formats[0].quality_height == 720 and formats[0].quality_label == "720p"


# ---------------------------------------------------------------------- metadata
def test_single_reel_metadata() -> None:
    node = video_node()
    node["caption"] = {"text": "Earvin N'Gapeth \U0001f3d0\nsecond line ignored"}
    node["owner"] = {"username": "volleyball", "id": "77"}
    node["taken_at_timestamp"] = 1790268725
    node["like_count"] = 1390947
    node["comment_count"] = 3968
    node["product_type"] = "clips"
    meta = post_to_metadata(Post(node, typename="GraphVideo"), config=CONFIG)

    assert meta.platform is Platform.INSTAGRAM
    assert meta.id == "TESTCODE1"
    assert meta.media_type is MediaKind.VIDEO
    assert meta.media_group_type is MediaGroupType.SINGLE
    assert meta.title == "Earvin N'Gapeth \U0001f3d0"
    assert meta.description.startswith("Earvin")
    assert meta.author == "volleyball" and meta.author_id == "77"
    assert meta.author_url == "https://www.instagram.com/volleyball/"
    assert meta.channel == "volleyball"
    assert meta.duration == 6.61
    assert meta.view_count == 1234 and meta.like_count == 1390947
    assert meta.is_short is True
    assert meta.count == 1
    assert meta.quality_hints == {"prefer_muxed": True, "container": "mp4", "codec": "avc1"}
    assert meta.extra["product_type"] == "clips"

    # the direct CDN url, not the permalink
    links = meta.links()
    assert list(links) == ["video"]
    assert links["video"][0].startswith(CDN)
    assert "instagram.com/p/" not in links["video"][0]
    # no size is reported by video_versions, and probing was disabled
    assert meta.size_bytes is None and meta.size_is_approx is False


def test_single_photo_metadata() -> None:
    node = image_node()
    node["caption"] = {"text": "just a photo"}
    meta = post_to_metadata(Post(node, typename="GraphImage"), config=CONFIG)
    assert meta.media_type is MediaKind.IMAGE
    assert meta.media_group_type is MediaGroupType.SINGLE
    assert meta.count == 1
    assert meta.duration is None
    assert meta.title == "just a photo"
    assert set(meta.links()) == {"image"}
    assert len(meta.thumbnails) == 3
    assert meta.thumbnail == meta.thumbnails[0].url


def test_carousel_metadata_is_an_album() -> None:
    node = carousel_node(image_node(11), video_node(13), image_node(12))
    node["caption"] = {"text": "mixed carousel"}
    meta = post_to_metadata(
        Post(node, typename="GraphSidecar", shortcode="CAROUSEL1"), config=CONFIG
    )
    assert meta.media_type is MediaKind.GALLERY
    assert meta.media_group_type is MediaGroupType.ALBUM
    assert meta.id == "CAROUSEL1"
    assert meta.count == 3
    assert meta.extra["carousel_count"] == 3
    assert [item.index for item in meta.items] == [0, 1, 2]
    assert [str(item.kind) for item in meta.items] == ["image", "video", "image"]
    # carousel children are addressable individually
    assert meta.items[1].source_url.endswith("?img_index=2")
    assert meta.items[1].meta["carousel_index"] == 1
    links = meta.links()
    assert len(links["image"]) == 2 and len(links["video"]) == 1


def test_carousel_item_cap() -> None:
    node = carousel_node(*[image_node(20 + index) for index in range(5)])
    meta = post_to_metadata(
        Post(node, typename="GraphSidecar"),
        config=InstagramConfig(max_carousel_items=2, probe_sizes=False),
    )
    assert meta.count == 2


def test_media_toggles() -> None:
    no_images = InstagramConfig(include_images=False, probe_sizes=False)
    meta = post_to_metadata(Post(image_node(), typename="GraphImage"), config=no_images)
    assert meta.count == 0
    # videos survive include_images=False (they are muxed mp4s, not stills)
    meta = post_to_metadata(
        Post(video_node(), typename="GraphVideo"),
        config=InstagramConfig(include_images=False, include_videos=True, probe_sizes=False),
    )
    assert meta.count == 1 and meta.media_type is MediaKind.VIDEO
    meta = post_to_metadata(
        Post(video_node(dash=False), typename="GraphVideo"),
        config=InstagramConfig(include_videos=False, include_images=False, probe_sizes=False),
    )
    assert meta.count == 0
    # a carousel keeps only its first child when carousels are turned off
    meta = post_to_metadata(
        Post(carousel_node(image_node(11), image_node(12)), typename="GraphSidecar"),
        config=InstagramConfig(include_carousels=False, probe_sizes=False),
    )
    assert meta.count == 1
    assert meta.media_group_type is MediaGroupType.SINGLE
    # and video children can be dropped from a carousel without losing the photos
    meta = post_to_metadata(
        Post(carousel_node(image_node(11), video_node(13)), typename="GraphSidecar"),
        config=InstagramConfig(include_videos=False, probe_sizes=False),
    )
    assert [str(item.kind) for item in meta.items] == ["image"]


# ------------------------------------------------------------------- selection
def test_best_prefers_the_muxed_progressive_copy() -> None:
    meta = post_to_metadata(Post(video_node(), typename="GraphVideo"), config=CONFIG)
    entry = build_plan([meta.items[0]], parse_quality("best"))[0]
    assert entry.primary.format_id == "progressive-720x1280"
    assert entry.primary.is_muxed and entry.primary.has_audio
    assert entry.needs_mux is False, "the progressive mp4 already carries audio"

    # asking for more than the progressive ladder offers clamps instead of failing
    clamped = build_plan([meta.items[0]], parse_quality("1080p"))[0]
    assert clamped.primary.quality_height == 720
    assert clamped.needs_mux is False


def test_enabling_dash_changes_what_best_resolves_to() -> None:
    config = InstagramConfig(include_dash=True, probe_sizes=False)
    meta = post_to_metadata(Post(video_node(), typename="GraphVideo"), config=config)
    entry = build_plan([meta.items[0]], parse_quality("best"))[0]
    assert entry.primary.origin is FormatOrigin.DASH
    assert entry.primary.quality_height == 1080
    assert entry.primary.has_video and not entry.primary.has_audio
    assert entry.needs_mux is True, "the DASH ladder is video only"
    # and the audio side comes from the same manifest, so the size is known
    assert entry.audio is not None and entry.audio.size_bytes == 36717
    assert entry.total_size == 2365448 + 36717


# --------------------------------------------------------------------- errors
def test_error_translation() -> None:
    if ig_errors is None:  # pragma: no cover - instaloader is a requirement
        return
    not_found = translate_instagram_error(ig_errors.QueryReturnedNotFoundException("deleted"))
    assert isinstance(not_found, InstagramNotFoundError)
    profile = translate_instagram_error(ig_errors.ProfileNotExistsException("nope"))
    assert isinstance(profile, ProfileNotFoundError)
    private = translate_instagram_error(ig_errors.PrivateProfileNotFollowedException("private"))
    assert isinstance(private, PrivateProfileError)
    login = translate_instagram_error(ig_errors.LoginRequiredException("login"))
    assert isinstance(login, LoginRequiredError)
    # the anonymous HTTP 401 arrives as a BadResponseException carrying the text
    limited = translate_instagram_error(
        ig_errors.BadResponseException("Please wait a few minutes before you try again.")
    )
    assert isinstance(limited, RateLimitedError)
    assert limited.retry_after is None
    throttled = translate_instagram_error(ig_errors.TooManyRequestsException("slow down"))
    assert isinstance(throttled, RateLimitedError)
    # anything unmapped still becomes an SDK error, not a raw instaloader one
    assert isinstance(translate_instagram_error(RuntimeError("boom")), MetadataError)
    # already-translated errors pass through untouched
    original = InstagramNotFoundError("already mine")
    assert translate_instagram_error(original) is original


def test_instagram_exceptions_join_the_shared_hierarchy() -> None:
    from downloader.core.exceptions import AuthenticationError, DownloaderError, NoMediaError, PostNotFoundError

    from downloader.instagram.exceptions import NoMediaFoundError, PrivateProfileError

    assert issubclass(InstagramNotFoundError, DownloaderError)
    assert issubclass(InstagramNotFoundError, PostNotFoundError)
    assert issubclass(InstagramNotFoundError, UnsupportedURLError) is False
    assert issubclass(LoginRequiredError, AuthenticationError)
    assert issubclass(PrivateProfileError, AuthenticationError)
    assert issubclass(NoMediaFoundError, NoMediaError)
    assert issubclass(InstaloaderMissingError, DownloaderError)


# --------------------------------------------------------------------- client
def test_profile_urls_are_refused_with_a_clear_message() -> None:
    import asyncio

    from downloader.instagram import InstagramClient

    async def run() -> None:
        async with InstagramClient(CONFIG) as client:
            try:
                await client.get_metadata("https://www.instagram.com/someuser/")
            except UnsupportedURLError as exc:
                assert "profiles" in str(exc)
            else:  # pragma: no cover - must not happen
                raise AssertionError("a profile url should be refused")

    asyncio.run(run())


def test_supports_only_claims_instagram_urls() -> None:
    from downloader.instagram import InstagramClient

    assert InstagramClient.supports("https://www.instagram.com/reel/DdhvW0GslGe/")
    assert not InstagramClient.supports("https://x.com/i/status/1")
    assert not InstagramClient.supports("https://www.youtube.com/watch?v=dQw4w9WgXcQ")


def test_facade_routes_instagram() -> None:
    from downloader import Downloader
    from downloader.core.enums import Platform as P

    assert Downloader.platform_of("https://www.instagram.com/reel/DdhvW0GslGe/") is P.INSTAGRAM
    assert Downloader.platform_of("https://instagr.am/p/DdPEofvmpPd/") is P.INSTAGRAM
    assert Downloader.supports("https://www.instagram.com/p/DdPEofvmpPd/")
    assert P.INSTAGRAM in Downloader()._enabled
    assert Downloader().platform_of("https://example.com/x") is P.UNKNOWN


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
