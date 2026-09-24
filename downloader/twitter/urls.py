"""Twitter/X url parsing."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

from ..core.exceptions import InvalidURLError

HOSTS = frozenset(
    {
        "x.com",
        "www.x.com",
        "mobile.x.com",
        "twitter.com",
        "www.twitter.com",
        "mobile.twitter.com",
        "m.twitter.com",
        "fxtwitter.com",
        "www.fxtwitter.com",
        "vxtwitter.com",
        "www.vxtwitter.com",
        "nitter.net",
        "t.co",
    }
)

STATUS_ID_RE = re.compile(r"^\d{15,25}$")
HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")

_RESERVED_PATHS = frozenset(
    {"i", "home", "explore", "search", "settings", "messages", "notifications", "compose"}
)


@dataclass(slots=True)
class TwitterRef:
    """A parsed tweet reference."""

    kind: str
    """``status``, ``profile`` or ``short`` (a ``t.co`` link)."""
    tweet_id: Optional[str] = None
    handle: Optional[str] = None
    canonical: str = ""
    requested: str = ""
    photo_index: Optional[int] = None

    @property
    def is_status(self) -> bool:
        return self.kind == "status"

    @property
    def status_url(self) -> str:
        if not self.tweet_id:
            return self.canonical
        handle = self.handle or "i"
        return f"https://x.com/{handle}/status/{self.tweet_id}"

    @property
    def fx_url(self) -> str:
        return f"https://api.fxtwitter.com/i/status/{self.tweet_id}"


def is_twitter_url(url: str) -> bool:
    """``True`` when ``url`` points at a host this SDK understands."""
    try:
        parsed = urlparse(str(url).strip())
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https", ""):
        return False
    return (parsed.hostname or "").lower() in HOSTS


def parse_url(url: str) -> TwitterRef:
    """Parse a tweet/status url into a :class:`TwitterRef`."""
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
        raise InvalidURLError(f"{url!r} is not a Twitter/X url")
    if host == "t.co":
        return TwitterRef(kind="short", canonical=text, requested=url)

    segments = [segment for segment in parsed.path.split("/") if segment]
    handle: Optional[str] = None
    if segments and segments[0].lower() not in _RESERVED_PATHS and HANDLE_RE.match(segments[0]):
        handle = segments[0]
    if segments and segments[0] == "i" and len(segments) > 1 and segments[1] == "status":
        return _status_ref(segments[2] if len(segments) > 2 else None, handle=None, url=url, rest=segments[3:])

    if "status" in segments:
        position = segments.index("status")
        tweet_id = segments[position + 1] if len(segments) > position + 1 else None
        rest = segments[position + 2 :]
        return _status_ref(tweet_id, handle=handle, url=url, rest=rest)

    if handle:
        return TwitterRef(
            kind="profile",
            handle=handle,
            canonical=f"https://x.com/{handle}",
            requested=url,
        )

    raise InvalidURLError(f"unsupported Twitter/X url: {url!r}")


def _status_ref(
    tweet_id: Optional[str],
    *,
    handle: Optional[str],
    url: str,
    rest: list[str],
) -> TwitterRef:
    if not tweet_id or not STATUS_ID_RE.match(tweet_id):
        raise InvalidURLError(f"{url!r} has no valid tweet id")
    photo_index: Optional[int] = None
    if rest and rest[0] == "photo" and len(rest) > 1 and rest[1].isdigit():
        photo_index = int(rest[1])
    handle_slug = handle or "i"
    return TwitterRef(
        kind="status",
        tweet_id=tweet_id,
        handle=handle,
        photo_index=photo_index,
        canonical=f"https://x.com/{handle_slug}/status/{tweet_id}",
        requested=url,
    )


def tweet_id_of(url: str) -> Optional[str]:
    """Best effort tweet id extraction (``None`` when the url has none)."""
    try:
        return parse_url(url).tweet_id
    except InvalidURLError:
        return None