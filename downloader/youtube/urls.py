"""YouTube url parsing: videos, shorts, playlists, community posts, channels."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qs, urlparse

from ..core.exceptions import InvalidURLError

HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtube-nocookie.com",
        "www.youtube-nocookie.com",
        "youtu.be",
    }
)

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
POST_ID_RE = re.compile(r"^Ug[a-zA-Z0-9_-]{10,}$")
CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
PLAYLIST_ID_RE = re.compile(r"^(PL|UU|LL|FL|OL|RD|WL)[A-Za-z0-9_-]+$")

PATH_KINDS = {
    "shorts": "shorts",
    "live": "video",
    "embed": "video",
    "v": "video",
    "watch": "video",
    "post": "post",
    "playlist": "playlist",
    "clip": "clip",
}


@dataclass(slots=True)
class YouTubeRef:
    """A parsed YouTube reference."""

    kind: str
    """``video``, ``shorts``, ``clip``, ``playlist``, ``post``, ``channel`` or ``unknown``."""
    video_id: Optional[str] = None
    post_id: Optional[str] = None
    playlist_id: Optional[str] = None
    channel: Optional[str] = None
    handle: Optional[str] = None
    canonical: str = ""
    requested: str = ""

    @property
    def is_video(self) -> bool:
        return self.kind in ("video", "shorts", "clip")

    @property
    def is_short(self) -> bool:
        return self.kind == "shorts"

    @property
    def watch_url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}" if self.video_id else self.canonical


def is_youtube_url(url: str) -> bool:
    """``True`` when ``url`` points at a host this SDK understands."""
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https", ""):
        return False
    return (parsed.hostname or "").lower() in HOSTS


def parse_url(url: str) -> YouTubeRef:
    """Parse any YouTube url into a :class:`YouTubeRef`.

    Raises :class:`~downloader.core.exceptions.InvalidURLError` when the url is
    not a YouTube url or does not describe a supported resource.
    """
    text = (url or "").strip()
    if not text:
        raise InvalidURLError("empty url")
    if "://" not in text:
        text = f"https://{text}"
    try:
        parsed = urlparse(text)
    except ValueError as exc:  # pragma: no cover - defensive
        raise InvalidURLError(f"could not parse {url!r}") from exc
    host = (parsed.hostname or "").lower()
    if host not in HOSTS:
        raise InvalidURLError(f"{url!r} is not a YouTube url")

    segments = [segment for segment in parsed.path.split("/") if segment]
    query = parse_qs(parsed.query)

    if host.endswith("youtu.be"):
        if not segments:
            raise InvalidURLError(f"{url!r} has no video id")
        return _video_ref(segments[0], url, kind="shorts" if len(segments) > 1 and segments[-1] == "shorts" else "video")

    if segments[:1] == ["post"] and len(segments) > 1:
        post_id = segments[1]
        if not POST_ID_RE.match(post_id):
            raise InvalidURLError(f"{post_id!r} is not a community post id")
        return YouTubeRef(kind="post", post_id=post_id, canonical=_post_url(post_id), requested=url)

    if segments[:1] == ["playlist"]:
        playlist_id = (query.get("list") or [None])[0]
        if not playlist_id:
            raise InvalidURLError(f"{url!r} has no playlist id")
        return YouTubeRef(
            kind="playlist",
            playlist_id=playlist_id,
            canonical=f"https://www.youtube.com/playlist?list={playlist_id}",
            requested=url,
        )

    if segments[:1] == ["watch"]:
        video_id = (query.get("v") or [None])[0]
        if video_id:
            kind = "shorts" if _looks_like_short(video_id, query) else "video"
            return _video_ref(video_id, url, kind=kind)
        playlist_id = (query.get("list") or [None])[0]
        if playlist_id:
            return YouTubeRef(
                kind="playlist",
                playlist_id=playlist_id,
                canonical=f"https://www.youtube.com/playlist?list={playlist_id}",
                requested=url,
            )
        raise InvalidURLError(f"{url!r} has neither a video nor a playlist id")

    if segments[:1] == ["channel"] and len(segments) > 1:
        return YouTubeRef(
            kind="channel",
            channel=segments[1],
            canonical=f"https://www.youtube.com/channel/{segments[1]}",
            requested=url,
        )

    if segments[:1] == ["c"] and len(segments) > 1:
        return YouTubeRef(
            kind="channel",
            handle=segments[1],
            canonical=f"https://www.youtube.com/c/{segments[1]}",
            requested=url,
        )

    if segments[:1] == ["user"] and len(segments) > 1:
        return YouTubeRef(
            kind="channel",
            handle=segments[1],
            canonical=f"https://www.youtube.com/user/{segments[1]}",
            requested=url,
        )

    if segments and segments[0].startswith("@"):
        handle = segments[0]
        if len(segments) == 1:
            return YouTubeRef(
                kind="channel",
                handle=handle,
                canonical=f"https://www.youtube.com/{handle}",
                requested=url,
            )
        sub = segments[1]
        if sub == "shorts" and len(segments) > 2:
            return _video_ref(segments[2], url, kind="shorts", handle=handle)
        if sub in ("live", "v") and len(segments) > 2:
            return _video_ref(segments[2], url, kind="video", handle=handle)
        if sub == "playlists" or sub == "videos":
            return YouTubeRef(
                kind="channel",
                handle=handle,
                canonical=f"https://www.youtube.com/{handle}",
                requested=url,
            )

    if len(segments) > 1 and segments[0] in PATH_KINDS:
        kind = PATH_KINDS[segments[0]]
        if kind in ("video", "shorts", "clip"):
            return _video_ref(segments[1], url, kind=kind)

    if len(segments) == 1 and VIDEO_ID_RE.match(segments[0]):
        return _video_ref(segments[0], url, kind="video")

    raise InvalidURLError(f"unsupported YouTube url: {url!r}")


def _video_ref(
    video_id: str,
    url: str,
    *,
    kind: str = "video",
    handle: Optional[str] = None,
) -> YouTubeRef:
    if not VIDEO_ID_RE.match(video_id):
        raise InvalidURLError(f"{video_id!r} is not a YouTube video id")
    return YouTubeRef(
        kind=kind,
        video_id=video_id,
        handle=handle,
        canonical=f"https://www.youtube.com/watch?v={video_id}",
        requested=url,
    )


def _post_url(post_id: str) -> str:
    return f"https://www.youtube.com/post/{post_id}"


def _looks_like_short(video_id: str, query: dict[str, list[str]]) -> bool:
    return "shorts" in (query.get("app") or [])