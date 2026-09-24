"""Pinterest url parsing.

Pinterest addresses media three ways: a pin (``/pin/<id>``, optionally with a
slug prefix), a board (``/<username>/<board-slug>``) or a profile
(``/<username>``). Short links are served from ``pin.it`` and are resolved by
following the redirect.

Country domains are matched with a regex rather than a host set: Pinterest
mirrors itself on ~40 TLDs and new ones appear without warning.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

from ..core.exceptions import InvalidURLError

# Every country domain yt-dlp's Pinterest extractor knows about.
_TLDS = (
    r"com|fr|de|ch|jp|cl|ca|it|co\.uk|nz|ru|com\.au|at|pt|co\.kr|es|com\.mx|"
    r"dk|ph|th|com\.uy|co|nl|info|kr|ie|vn|com\.vn|ec|mx|in|pe|co\.at|hu|"
    r"co\.in|co\.nz|id|com\.ec|com\.py|tw|be|uk|com\.bo|com\.pe"
)
HOST_RE = re.compile(rf"^(?:[^./]+\.)*pinterest\.(?:{_TLDS})$", re.IGNORECASE)

SHORT_HOSTS = frozenset({"pin.it", "www.pin.it"})

HOSTS = frozenset({"pinterest.com", "www.pinterest.com", "pin.it", "www.pin.it"})
"""The hosts worth advertising; :data:`HOST_RE` decides what is accepted."""

PIN_ID_RE = re.compile(r"^\d{5,25}$")
_SLUG_ID_RE = re.compile(r"--(\d{5,25})$")
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{1,32}$")

_RESERVED = frozenset(
    {
        "about", "business", "categories", "discover", "explore", "ideas",
        "login", "logout", "news", "password", "pin", "resource", "search",
        "settings", "signup", "today", "topics", "video", "watch",
    }
)


@dataclass(slots=True)
class PinterestRef:
    """A parsed Pinterest reference."""

    kind: str
    """``pin``, ``board``, ``profile`` or ``short`` (a ``pin.it`` link)."""

    pin_id: Optional[str] = None
    username: Optional[str] = None
    board_slug: Optional[str] = None
    canonical: str = ""
    requested: str = ""

    @property
    def is_pin(self) -> bool:
        return self.kind == "pin"

    @property
    def is_board(self) -> bool:
        return self.kind == "board"

    @property
    def is_profile(self) -> bool:
        return self.kind == "profile"

    @property
    def is_short(self) -> bool:
        return self.kind == "short"

    @property
    def pin_url(self) -> str:
        if self.pin_id:
            return f"https://www.pinterest.com/pin/{self.pin_id}/"
        return self.canonical


def is_pinterest_url(url: str) -> bool:
    """``True`` when ``url`` points at a host this SDK understands."""
    try:
        parsed = urlparse(str(url).strip())
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https", ""):
        return False
    host = (parsed.hostname or "").lower()
    return host in SHORT_HOSTS or bool(HOST_RE.match(host))


def parse_url(url: str) -> PinterestRef:
    """Parse a pin/board/profile url into a :class:`PinterestRef`."""
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
    if host in SHORT_HOSTS:
        return PinterestRef(kind="short", canonical=text, requested=url)
    if not HOST_RE.match(host):
        raise InvalidURLError(f"{url!r} is not a Pinterest url")

    segments = [segment for segment in parsed.path.split("/") if segment]
    if not segments:
        raise InvalidURLError(f"unsupported Pinterest url: {url!r}")
    head = segments[0].lower()

    if head == "pin":
        pin_id = _pin_id(segments[1]) if len(segments) > 1 else None
        if not pin_id:
            raise InvalidURLError(f"{url!r} has no valid pin id")
        return PinterestRef(
            kind="pin",
            pin_id=pin_id,
            canonical=f"https://www.pinterest.com/pin/{pin_id}/",
            requested=url,
        )

    if head in _RESERVED or not USERNAME_RE.match(segments[0]):
        raise InvalidURLError(f"unsupported Pinterest url: {url!r}")

    if len(segments) == 1:
        return PinterestRef(
            kind="profile",
            username=segments[0],
            canonical=f"https://www.pinterest.com/{segments[0]}/",
            requested=url,
        )

    # /<username>/<board-slug>/ : the only multi segment form yt-dlp can
    # resolve without a login.
    return PinterestRef(
        kind="board",
        username=segments[0],
        board_slug=segments[1],
        canonical=f"https://www.pinterest.com/{segments[0]}/{segments[1]}/",
        requested=url,
    )


def _pin_id(segment: str) -> Optional[str]:
    """Extract the numeric id from ``1084663891475263837`` or ``slug--108...``."""
    if PIN_ID_RE.match(segment):
        return segment
    match = _SLUG_ID_RE.search(segment)
    return match.group(1) if match else None


def pin_id_of(url: str) -> Optional[str]:
    """Best effort pin id extraction (``None`` when the url has none)."""
    try:
        return parse_url(url).pin_id
    except InvalidURLError:
        return None


__all__ = [
    "HOSTS",
    "HOST_RE",
    "PIN_ID_RE",
    "SHORT_HOSTS",
    "PinterestRef",
    "is_pinterest_url",
    "parse_url",
    "pin_id_of",
]