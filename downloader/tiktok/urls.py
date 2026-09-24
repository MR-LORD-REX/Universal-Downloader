"""TikTok url parsing.

TikTok addresses media six ways: a video (``/@user/video/<id>``, also served
from ``/share/video/<id>``, ``/@/video/<id>`` and ``/embed/<id>``), a short
link (``vm.tiktok.com/<code>``, ``vt.tiktok.com/<code>``, ``tiktok.com/t/<code>``),
a profile (``/@user``), a sound (``/music/<slug>-<id>``), a hashtag
(``/tag/<name>``) and a collection (``/@user/collection/<slug>-<id>``).

Short links are *not* resolved here: yt-dlp's own ``vm.tiktok`` extractor
follows the redirect, so the SDK hands the link over untouched and reads the
resolved ``webpage_url`` back out of the info dict.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

from ..core.exceptions import InvalidURLError

HOSTS = frozenset(
    {
        "tiktok.com",
        "www.tiktok.com",
        "m.tiktok.com",
        "tiktokv.com",
        "www.tiktokv.com",
        "m.tiktokv.com",
        "vm.tiktok.com",
        "vt.tiktok.com",
    }
)

VIDEO_ID_RE = re.compile(r"^\d{6,25}$")
SHORT_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{4,32}$")
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")
_COLLECTION_ID_RE = re.compile(r"-(\d{5,25})$")
_MUSIC_ID_RE = re.compile(r"-(\d{5,25})$")

_RESERVED = frozenset(
    {
        "about", "business", "coin", "discover", "explore", "foryou", "following",
        "legal", "live", "login", "messages", "music", "search", "settings",
        "share", "signup", "tag", "trending", "upload", "video",
    }
)


@dataclass(slots=True)
class TikTokRef:
    """A parsed TikTok reference."""

    kind: str
    """``video``, ``short``, ``profile``, ``sound``, ``tag``, ``collection`` or ``live``."""

    video_id: Optional[str] = None
    username: Optional[str] = None
    collection_id: Optional[str] = None
    sound_id: Optional[str] = None
    tag: Optional[str] = None
    short_code: Optional[str] = None
    canonical: str = ""
    requested: str = ""

    @property
    def is_video(self) -> bool:
        return self.kind == "video"

    @property
    def is_short(self) -> bool:
        return self.kind == "short"

    @property
    def is_profile(self) -> bool:
        return self.kind == "profile"

    @property
    def is_list(self) -> bool:
        """``True`` for the kinds that resolve to a list of videos."""
        return self.kind in ("profile", "sound", "tag", "collection")

    @property
    def video_url(self) -> str:
        if self.video_id:
            return f"https://www.tiktok.com/@{self.username or 'i'}/video/{self.video_id}"
        return self.canonical


def is_tiktok_url(url: str) -> bool:
    """``True`` when ``url`` points at a host this SDK understands."""
    try:
        parsed = urlparse(str(url).strip())
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https", ""):
        return False
    return (parsed.hostname or "").lower() in HOSTS


def parse_url(url: str) -> TikTokRef:
    """Parse a TikTok url into a :class:`TikTokRef`."""
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
        raise InvalidURLError(f"{url!r} is not a TikTok url")

    segments = [segment for segment in parsed.path.split("/") if segment]

    if host in ("vm.tiktok.com", "vt.tiktok.com"):
        if not segments or not SHORT_CODE_RE.match(segments[0]):
            raise InvalidURLError(f"{url!r} has no valid short code")
        return TikTokRef(
            kind="short",
            short_code=segments[0],
            canonical=f"https://{host}/{segments[0]}",
            requested=url,
        )

    if not segments:
        raise InvalidURLError(f"unsupported TikTok url: {url!r}")
    head = segments[0]

    # /t/<code>
    if head == "t" and len(segments) > 1:
        if not SHORT_CODE_RE.match(segments[1]):
            raise InvalidURLError(f"{url!r} has no valid short code")
        return TikTokRef(
            kind="short",
            short_code=segments[1],
            canonical=f"https://www.tiktok.com/t/{segments[1]}",
            requested=url,
        )

    # /music/<slug>-<id> and /tag/<name>
    if head == "music" and len(segments) > 1:
        match = _MUSIC_ID_RE.search(segments[1])
        if not match:
            raise InvalidURLError(f"{url!r} has no valid sound id")
        return TikTokRef(
            kind="sound",
            sound_id=match.group(1),
            canonical=f"https://www.tiktok.com/music/{segments[1]}",
            requested=url,
        )
    if head == "tag" and len(segments) > 1:
        return TikTokRef(
            kind="tag",
            tag=segments[1],
            canonical=f"https://www.tiktok.com/tag/{segments[1]}",
            requested=url,
        )

    # /embed/<id>, /embed/v2/<id>, /share/video/<id>, /video/<id>
    if head in ("embed", "share", "video"):
        video_id = None
        if head == "embed":
            tail = [segment for segment in segments[1:] if segment != "v2"]
            video_id = tail[0] if tail else None
        elif head == "share" and len(segments) > 2 and segments[1] == "video":
            video_id = segments[2]
        elif head == "video" and len(segments) > 1:
            video_id = segments[1]
        if video_id and VIDEO_ID_RE.match(video_id):
            return TikTokRef(
                kind="video",
                video_id=video_id,
                canonical=f"https://www.tiktok.com/@i/video/{video_id}",
                requested=url,
            )
        raise InvalidURLError(f"{url!r} has no valid video id")

    # /@<username>/...
    if head.startswith("@"):
        username = head[1:]
        if not username:
            # /@/video/<id>
            if len(segments) > 2 and segments[1] == "video" and VIDEO_ID_RE.match(segments[2]):
                return TikTokRef(
                    kind="video",
                    video_id=segments[2],
                    canonical=f"https://www.tiktok.com/@i/video/{segments[2]}",
                    requested=url,
                )
            raise InvalidURLError(f"{url!r} has no username")
        if not USERNAME_RE.match(username):
            raise InvalidURLError(f"{url!r} has an invalid username")
        if len(segments) > 2 and segments[1] == "video":
            video_id = segments[2]
            if not VIDEO_ID_RE.match(video_id):
                raise InvalidURLError(f"{url!r} has no valid video id")
            return TikTokRef(
                kind="video",
                video_id=video_id,
                username=username,
                canonical=f"https://www.tiktok.com/@{username}/video/{video_id}",
                requested=url,
            )
        if len(segments) > 1 and segments[1] == "live":
            return TikTokRef(
                kind="live",
                username=username,
                canonical=f"https://www.tiktok.com/@{username}/live",
                requested=url,
            )
        if len(segments) > 2 and segments[1] == "collection":
            match = _COLLECTION_ID_RE.search(segments[2])
            if not match:
                raise InvalidURLError(f"{url!r} has no valid collection id")
            return TikTokRef(
                kind="collection",
                username=username,
                collection_id=match.group(1),
                canonical=f"https://www.tiktok.com/@{username}/collection/{segments[2]}",
                requested=url,
            )
        if len(segments) == 1:
            return TikTokRef(
                kind="profile",
                username=username,
                canonical=f"https://www.tiktok.com/@{username}",
                requested=url,
            )
        raise InvalidURLError(f"unsupported TikTok url: {url!r}")

    raise InvalidURLError(f"unsupported TikTok url: {url!r}")


def video_id_of(url: str) -> Optional[str]:
    """Best effort video id extraction (``None`` when the url has none)."""
    try:
        return parse_url(url).video_id
    except InvalidURLError:
        return None


__all__ = [
    "HOSTS",
    "TikTokRef",
    "VIDEO_ID_RE",
    "is_tiktok_url",
    "parse_url",
    "video_id_of",
]