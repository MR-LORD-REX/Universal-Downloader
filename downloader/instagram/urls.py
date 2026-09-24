"""Instagram url parsing.

Instagram addresses media three ways: a shortcode (``/p/<code>``, ``/reel/<code>``,
``/tv/<code>``), a story (``/stories/<username>/<media id>``) or a profile
(``/<username>``). Only the shortcode forms are resolvable without a login, so
this module reports the kind and lets the client explain the rest.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

from ..core.exceptions import InvalidURLError

HOSTS = frozenset(
    {
        "instagram.com",
        "www.instagram.com",
        "m.instagram.com",
        "l.instagram.com",
        "instagr.am",
        "www.instagr.am",
        "ig.me",
    }
)

SHORTCODE_RE = re.compile(r"^[A-Za-z0-9_-]{5,32}$")
USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")

POST_KINDS = frozenset({"p", "reel", "reels", "tv"})
_RESERVED = frozenset(
    {
        "about", "accounts", "api", "developer", "directory", "explore", "legal",
        "oauth", "privacy", "session", "terms", "web",
    }
)


@dataclass(slots=True)
class InstagramRef:
    """A parsed Instagram reference."""

    kind: str
    """``post`` (shortcode), ``story``, ``highlight`` or ``profile``."""
    shortcode: Optional[str] = None
    username: Optional[str] = None
    story_media_id: Optional[str] = None
    canonical: str = ""
    requested: str = ""

    @property
    def is_post(self) -> bool:
        return self.kind == "post"

    @property
    def is_story(self) -> bool:
        return self.kind in ("story", "highlight")

    @property
    def is_profile(self) -> bool:
        return self.kind == "profile"

    @property
    def post_url(self) -> str:
        if self.shortcode:
            return f"https://www.instagram.com/p/{self.shortcode}/"
        return self.canonical


def is_instagram_url(url: str) -> bool:
    """``True`` when ``url`` points at a host this SDK understands."""
    try:
        parsed = urlparse(str(url).strip())
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https", ""):
        return False
    return (parsed.hostname or "").lower() in HOSTS


def parse_url(url: str) -> InstagramRef:
    """Parse a post/story/profile url into an :class:`InstagramRef`."""
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
        raise InvalidURLError(f"{url!r} is not an Instagram url")

    segments = [segment for segment in parsed.path.split("/") if segment]

    # /stories/<username>/<media id> and /stories/highlights/<id>
    if segments and segments[0] == "stories":
        if len(segments) >= 3 and segments[1] == "highlights":
            return InstagramRef(
                kind="highlight",
                story_media_id=segments[2],
                canonical=f"https://www.instagram.com/stories/highlights/{segments[2]}/",
                requested=url,
            )
        if len(segments) >= 3:
            return InstagramRef(
                kind="story",
                username=segments[1],
                story_media_id=segments[2],
                canonical=f"https://www.instagram.com/stories/{segments[1]}/{segments[2]}/",
                requested=url,
            )
        raise InvalidURLError(f"unsupported Instagram stories url: {url!r}")

    # /p/<code>, /reel/<code>, /reels/<code>, /tv/<code>
    for index, segment in enumerate(segments):
        if segment.lower() in POST_KINDS and len(segments) > index + 1:
            shortcode = segments[index + 1]
            if not SHORTCODE_RE.match(shortcode):
                raise InvalidURLError(f"{url!r} has no valid shortcode")
            username = segments[0] if index > 0 and USERNAME_RE.match(segments[0]) else None
            return InstagramRef(
                kind="post",
                shortcode=shortcode,
                username=username,
                canonical=f"https://www.instagram.com/p/{shortcode}/",
                requested=url,
            )

    if (
        len(segments) == 1
        and USERNAME_RE.match(segments[0])
        and segments[0].lower() not in _RESERVED
    ):
        return InstagramRef(
            kind="profile",
            username=segments[0],
            canonical=f"https://www.instagram.com/{segments[0]}/",
            requested=url,
        )

    raise InvalidURLError(f"unsupported Instagram url: {url!r}")


def shortcode_of(url: str) -> Optional[str]:
    """Best effort shortcode extraction (``None`` when the url has none)."""
    try:
        return parse_url(url).shortcode
    except InvalidURLError:
        return None


__all__ = ["HOSTS", "InstagramRef", "is_instagram_url", "parse_url", "shortcode_of"]
