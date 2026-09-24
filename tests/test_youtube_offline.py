"""Offline unit tests for the YouTube SDK (no network access required).

Run with ``python tests/test_youtube_offline.py`` or ``pytest tests``.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from downloader.core.enums import FormatKind, MediaGroupType, MediaKind, Platform
from downloader.core.models import human_size
from downloader.core.select import build_plan, parse_quality
from downloader.youtube.config import YouTubeConfig
from downloader.youtube.extract import ytdlp_format_selector, ytdlp_format_sort
from downloader.youtube.models import info_to_metadata, select_audio_tracks
from downloader.youtube.urls import is_youtube_url, parse_url

CDN = "https://rr1---sn-gwpa-o5be6.googlevideo.com/videoplayback"
CONFIG = YouTubeConfig(probe_sizes=False)


def fmt(
    format_id: str,
    *,
    vcodec: str | None = "avc1.640028",
    acodec: str | None = None,
    height: int | None = None,
    width: int | None = None,
    ext: str = "mp4",
    size: int | None = None,
    note: str | None = None,
    abr: float | None = None,
    tbr: float | None = None,
    language: str | None = None,
    preference: float | None = None,
    protocol: str = "https",
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "format_id": format_id,
        "url": f"{CDN}?id={format_id}",
        "vcodec": vcodec if vcodec is not None else "none",
        "acodec": acodec if acodec is not None else "none",
        "ext": ext,
        "protocol": protocol,
    }
    if height is not None:
        data["height"] = height
    if width is not None:
        data["width"] = width
    if size is not None:
        data["filesize"] = size
    if note is not None:
        data["format_note"] = note
    if abr is not None:
        data["abr"] = abr
    if tbr is not None:
        data["tbr"] = tbr
    if language is not None:
        data["language"] = language
    if preference is not None:
        data["language_preference"] = preference
    return data


def adaptive_info() -> dict[str, Any]:
    """A tall video: progressive 360p plus adaptive 1080p/720p video and audio."""
    return {
        "id": "FOUwd1h_jF4",
        "title": "Test video",
        "uploader": "Someone",
        "uploader_id": "@someone",
        "channel": "Some Channel",
        "channel_id": "UCabcdefghijklmnopqrstuv",
        "duration": 266.0,
        "upload_date": "20240901",
        "timestamp": 1725148800,
        "view_count": 12345,
        "like_count": 678,
        "comment_count": 90,
        "thumbnail": f"{CDN}?thumb=1",
        "webpage_url": "https://www.youtube.com/watch?v=FOUwd1h_jF4",
        "language": "en",
        "formats": [
            fmt("18", vcodec="avc1.42001E", acodec="mp4a.40.2", height=360, width=640, size=9_000_000, tbr=500),
            fmt("137", vcodec="avc1.640028", height=1080, width=1920, ext="mp4", size=57_000_000, tbr=2200),
            fmt("248", vcodec="vp9", height=1080, width=1920, ext="webm", size=40_000_000, tbr=1500),
            fmt("22", vcodec="avc1.64001F", acodec="mp4a.40.2", height=720, width=1280, size=18_000_000, tbr=1000),
            fmt("139", vcodec=None, acodec="mp4a.40.5", size=700_000, abr=48, note="low"),
            fmt("140", vcodec=None, acodec="mp4a.40.2", size=4_300_000, abr=128, note="medium"),
            fmt("251", vcodec=None, acodec="opus", ext="webm", size=5_000_000, abr=140, note="medium"),
        ],
        "thumbnails": [
            {"url": f"{CDN}?t=1", "width": 120, "height": 90},
            {"url": f"{CDN}?t=2", "width": 1280, "height": 720},
        ],
        "subtitles": {"en": [{"ext": "vtt", "url": f"{CDN}?sub=en"}]},
        "automatic_captions": {"ja": [{"ext": "vtt", "url": f"{CDN}?sub=ja"}]},
    }


# ------------------------------------------------------------------------ urls
def test_youtube_url_parsing() -> None:
    assert is_youtube_url("https://youtu.be/FOUwd1h_jF4")
    assert is_youtube_url("https://www.youtube.com/watch?v=FOUwd1h_jF4")
    assert is_youtube_url("https://youtube.com/shorts/sUVemoSeY10")
    assert is_youtube_url("https://www.youtube.com/post/UgkxU83oRNhZTfHUglXrSYJtO8MfrWJb3V8T")
    assert not is_youtube_url("https://vimeo.com/12345")

    assert parse_url("https://youtu.be/FOUwd1h_jF4?si=abc").kind == "video"
    assert parse_url("https://youtu.be/FOUwd1h_jF4").video_id == "FOUwd1h_jF4"
    short = parse_url("https://youtube.com/shorts/sUVemoSeY10?si=x")
    assert short.kind == "shorts" and short.is_short is True
    post = parse_url("http://youtube.com/post/UgkxU83oRNhZTfHUglXrSYJtO8MfrWJb3V8T?si=y")
    assert post.kind == "post" and post.post_id == "UgkxU83oRNhZTfHUglXrSYJtO8MfrWJb3V8T"
    assert parse_url("https://www.youtube.com/watch?v=FOUwd1h_jF4").watch_url.endswith(
        "watch?v=FOUwd1h_jF4"
    )


# ------------------------------------------------------------------- metadata
def test_info_to_metadata_full_video() -> None:
    meta = info_to_metadata(adaptive_info(), CONFIG, requested_url="https://youtu.be/FOUwd1h_jF4")
    assert meta.platform is Platform.YOUTUBE
    assert meta.id == "FOUwd1h_jF4"
    assert meta.title == "Test video"
    assert meta.author == "Someone"
    assert meta.channel == "Some Channel"
    assert meta.media_type is MediaKind.VIDEO
    assert meta.media_group_type is MediaGroupType.SINGLE
    assert meta.duration == 266.0
    assert meta.count == 1
    assert meta.is_short is False

    links = meta.links()
    assert "video" in links and links["video"]
    assert all("googlevideo.com" in url for url in links["video"])
    assert meta.items[0].has_audio is True  # audio is downloadable for the item


def test_every_rendition_keeps_its_size() -> None:
    meta = info_to_metadata(adaptive_info(), CONFIG)
    item = meta.items[0]
    assert item.format("137").size_bytes == 57_000_000
    assert item.format("140").size_bytes == 4_300_000
    assert item.format("137").size_is_approx is False
    assert item.select("1080p").format_id == "137"
    assert item.select("720p").format_id == "22"


def test_adaptive_pick_requires_muxing() -> None:
    meta = info_to_metadata(adaptive_info(), CONFIG)
    plan = build_plan(meta.items, parse_quality("1080p", codec="avc1", prefer_muxed=True))
    assert len(plan) == 1
    assert plan[0].primary.format_id == "137"
    assert plan[0].audio is not None and plan[0].audio.kind is FormatKind.AUDIO
    assert plan[0].needs_mux is True
    # the size of the plan is video + audio
    assert plan[0].total_size == 57_000_000 + int(plan[0].audio.size_bytes or 0)
    # a progressive rendition needs no muxing
    plan2 = build_plan(meta.items, parse_quality("360p"))
    assert plan2[0].needs_mux is False


def test_size_human_and_links_grouping() -> None:
    meta = info_to_metadata(adaptive_info(), CONFIG)
    assert meta.size_human == human_size(57_000_000 + 4_300_000)
    assert meta.size_known is True
    # subtitles are grouped separately and never mixed into the video links
    assert set(meta.links()) <= {"video", "image", "audio", "subtitle", "gif"}


def test_thumbnails_and_subtitles_are_exposed() -> None:
    meta = info_to_metadata(adaptive_info(), CONFIG)
    assert meta.thumbnail, meta.thumbnail
    assert meta.thumbnail == meta.thumbnails[-1].url, meta.thumbnail
    assert len(meta.thumbnails) == 2, meta.thumbnails
    assert "en" in meta.subtitle_languages, meta.subtitle_languages
    assert "ja" in meta.subtitle_languages, meta.subtitle_languages
    assert meta.subtitles_for("en")[0].kind is FormatKind.SUBTITLE


# ------------------------------------------------------------------ audio dubs
def test_only_the_original_dub_survives() -> None:
    infos = [
        fmt("139-0", vcodec=None, acodec="mp4a.40.5", language="ta", note="low", preference=-10),
        fmt("139-20", vcodec=None, acodec="mp4a.40.5", language="en", note="original (default)", preference=10),
        fmt("139-5", vcodec=None, acodec="mp4a.40.5", language="ar", note="low", preference=-5),
        fmt("137", vcodec="avc1.640028", height=1080, size=57_000_000),
    ]
    kept = select_audio_tracks(infos, CONFIG, default_language="en")
    audio = [d for d in kept if d["vcodec"] == "none"]
    assert len(audio) == 1
    assert audio[0]["format_id"] == "139-20"


def test_all_dubs_kept_when_asked() -> None:
    infos = [
        fmt("139-0", vcodec=None, acodec="mp4a.40.5", language="ta"),
        fmt("139-20", vcodec=None, acodec="mp4a.40.5", language="en", note="original (default)"),
    ]
    config = YouTubeConfig(include_all_audio_languages=True, probe_sizes=False)
    kept = select_audio_tracks(infos, config)
    assert len(kept) == 2
    translated = select_audio_tracks(infos, YouTubeConfig(audio_languages=["ta"], probe_sizes=False))
    assert [d["format_id"] for d in translated] == ["139-0"]


# -------------------------------------------------------------- ytdlp mapping
def test_ytdlp_format_selector_and_sort() -> None:
    selector = ytdlp_format_selector(parse_quality("1080p"), CONFIG)
    assert "bestvideo[height<=1080]" in selector
    assert "bestaudio[ext=m4a]" in selector
    assert ytdlp_format_selector(parse_quality("audio"), CONFIG).startswith("bestaudio")
    assert ytdlp_format_selector(parse_quality("worst"), CONFIG) == "worstvideo+worstaudio/worst"
    assert ytdlp_format_selector(parse_quality("137+140"), CONFIG) == "137+140"
    # a bare 3-4 digit id is read as a height, so short ids go through unchanged
    assert parse_quality("137").mode == "height"
    assert ytdlp_format_selector(parse_quality("18"), CONFIG) == "18"

    sort = ytdlp_format_sort(parse_quality("1080p"), CONFIG)
    assert "res:1080" in sort
    assert any(field.startswith("vcodec:") for field in sort)
    assert any(field.startswith("acodec:") for field in sort)


def test_manifest_duplicates_are_dropped() -> None:
    info = adaptive_info()
    info["formats"] = list(info["formats"]) + [
        fmt("137", vcodec="avc1.640028", height=1080, size=57_000_000, tbr=2200, protocol="m3u8_native"),
    ]
    meta = info_to_metadata(info, CONFIG)
    ids = [f.format_id for f in meta.items[0].formats]
    assert ids.count("137") == 1, ids


# ------------------------------------------------------------------ 403 retry
def test_403_refresh_retry_still_saves_to_dest() -> None:
    import tempfile
    import time as _time

    from downloader.core import base as base_mod
    from downloader.core.models import DownloadResult, DownloadedFile, MediaItem, PostMetadata
    from downloader.youtube import YouTubeClient

    calls = {"n": 0}
    original = base_mod.BaseClient.download

    async def fake_download(self, metadata, **kwargs):
        calls["n"] += 1
        # dest/pattern are owned by YouTubeClient.download, never forwarded
        assert "dest" not in kwargs and "pattern" not in kwargs, kwargs
        if calls["n"] == 1:
            return DownloadResult(
                metadata=metadata, quality="144p", errors={"item[0]": "HTTP 403: expired"}
            )
        return DownloadResult(
            metadata=metadata,
            quality="144p",
            files=[
                DownloadedFile(
                    item_index=0,
                    kind=MediaKind.VIDEO,
                    filename="x.mp4",
                    data=b"payload",
                    downloaded=7,
                )
            ],
        )

    base_mod.BaseClient.download = fake_download
    try:

        async def scenario() -> None:
            client = YouTubeClient()
            stale = PostMetadata(
                platform=Platform.YOUTUBE,
                id="FOUwd1h_jF4",
                requested_url="https://youtu.be/FOUwd1h_jF4",
                extra={"extracted_at": _time.time()},  # fresh enough: no re-extract
                items=[MediaItem(index=0, kind=MediaKind.VIDEO)],
            )

            async def fresh_forced(_metadata):
                return stale.model_copy(update={"id": "FOUwd1h_jF4"})

            client._fresh_forced = fresh_forced  # type: ignore[method-assign]
            with tempfile.TemporaryDirectory() as tmp:
                result = await client.download(
                    stale, quality="144p", dest=tmp, pattern="{platform}_{id}.{ext}"
                )
                assert result.ok, result.errors
                paths = result.saved_paths
                assert len(paths) == 1 and paths[0].exists(), paths
                assert paths[0].name == "youtube_FOUwd1h_jF4.mp4", paths[0].name
                assert paths[0].read_bytes() == b"payload"
            await client.close()

        asyncio.run(scenario())
    finally:
        base_mod.BaseClient.download = original
    assert calls["n"] == 2, f"expected one retry, saw {calls['n']} attempt(s)"


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