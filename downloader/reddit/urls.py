"""Reddit URL parsing, normalisation and short-link resolution."""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict

from .exceptions import InvalidURLError

POST_ID_RE = re.compile(r"^(?:t3_)?([a-z0-9]{5,8})$", re.I)
REDD_IT_RE = re.compile(r"^https?://(?:www\.)?redd\.it/([A-Za-z0-9]+)/?", re.I)
MEDIA_HOSTS = {
    "i.redd.it": "image",
    "v.redd.it": "video",
    "preview.redd.it": "preview",
    "external-preview.redd.it": "preview",
    "packaged-media.redd.it": "video",
}
EXTERNAL_VIDEO_HOSTS = (
    "youtube.com",
    "youtu.be",
    "youtube-nocookie.com",
    "vimeo.com",
    "dailymotion.com",
    "streamable.com",
    "twitch.tv",
)


class PostRef(BaseModel):
    """A parsed reference to something downloadable.

    ``kind`` is one of ``post`` (a reddit submission), ``share`` (a
    ``/r/sub/s/token`` share link that still needs resolving), ``media`` (a
    direct CDN url) or ``external`` (a non-reddit url).
    """

    model_config = ConfigDict(extra="ignore")

    kind: str
    url: str
    post_id: Optional[str] = None
    subreddit: Optional[str] = None
    token: Optional[str] = None
    host: Optional[str] = None
    media_kind: Optional[str] = None

    @property
    def needs_resolution(self) -> bool:
        return self.kind == "share" or (self.kind == "post" and not self.post_id)

    @property
    def permalink(self) -> Optional[str]:
        if self.kind == "post" and self.post_id:
            if self.subreddit:
                return f"/r/{self.subreddit}/comments/{self.post_id}/"
            return f"/comments/{self.post_id}/"
        return None

    @property
    def full_permalink(self) -> Optional[str]:
        permalink = self.permalink
        return f"https://www.reddit.com{permalink}" if permalink else None


def parse(target: str) -> PostRef:
    """Parse a url, short url, permalink or bare post id into a :class:`PostRef`."""
    if target is None:
        raise InvalidURLError("no url given")

    raw = str(target).strip()
    if not raw:
        raise InvalidURLError("empty url")

    # bare id / t3_xxx
    bare = POST_ID_RE.match(raw)
    if bare and "://" not in raw and "/" not in raw:
        return PostRef(kind="post", url=f"https://www.reddit.com/comments/{bare.group(1)}/", post_id=bare.group(1))

    if "://" not in raw:
        raw = "https://" + raw.lstrip("/")

    parsed = urlparse(raw)
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host_no_www = host[4:]
    else:
        host_no_www = host

    parts = [p for p in parsed.path.split("/") if p]

    # ---------------------------------------------------------- media hosts
    if host_no_www in MEDIA_HOSTS:
        media_kind = MEDIA_HOSTS[host_no_www]
        post_id = None
        if media_kind == "video" and parts:
            post_id = parts[0]
        return PostRef(
            kind="media",
            url=raw,
            host=host_no_www,
            media_kind=media_kind,
            post_id=post_id,
        )

    # ------------------------------------------------------- redd.it / share
    redd_it = REDD_IT_RE.match(raw)
    if redd_it:
        return PostRef(kind="post", url=f"https://www.reddit.com/comments/{redd_it.group(1)}/", post_id=redd_it.group(1))

    if host_no_www not in ("reddit.com", "redditmedia.com", "redditstatic.com"):
        lowered = host_no_www
        media_kind = "video" if any(h in lowered for h in EXTERNAL_VIDEO_HOSTS) else "image"
        return PostRef(kind="external", url=raw, host=host_no_www, media_kind=media_kind)

    # -------------------------------------------------- /r/<sub>/s/<token>
    if "s" in parts:
        index = parts.index("s")
        if index + 1 < len(parts):
            subreddit = parts[1] if parts[0] == "r" and len(parts) > 1 else None
            return PostRef(
                kind="share",
                url=raw,
                token=parts[index + 1],
                subreddit=subreddit,
                host=host_no_www,
            )

    # ------------------------------------------------------------ galleries
    if "gallery" in parts:
        index = parts.index("gallery")
        if index + 1 < len(parts):
            post_id = parts[index + 1]
            subreddit = parts[1] if parts[0] == "r" and len(parts) > 1 else None
            return PostRef(kind="post", url=raw, post_id=post_id, subreddit=subreddit, host=host_no_www)

    # -------------------------------------------------- /r/<sub>/comments/<id>
    if "comments" in parts:
        index = parts.index("comments")
        if index + 1 < len(parts):
            post_id = parts[index + 1]
            subreddit = parts[1] if parts[0] == "r" and len(parts) > 1 else None
            return PostRef(kind="post", url=raw, post_id=post_id, subreddit=subreddit, host=host_no_www)

    # ------------------------------------------------------------ /r/<sub>/
    if "r" in parts:
        index = parts.index("r")
        if index + 1 < len(parts):
            return PostRef(kind="listing", url=raw, subreddit=parts[index + 1], host=host_no_www)

    raise InvalidURLError(f"cannot parse a post reference out of {target!r}")


def absolute(permalink: str | None) -> Optional[str]:
    """Turn ``/r/x/comments/y/`` into a full url."""
    if not permalink:
        return None
    if permalink.startswith("http"):
        return permalink
    return f"https://www.reddit.com{permalink}"


def short_permalink(post_id: str | None) -> Optional[str]:
    return f"https://redd.it/{post_id}" if post_id else None


def media_extension(url: str) -> Optional[str]:
    """Best-effort extension extraction from a CDN url."""
    path = urlparse(url).path
    tail = path.rsplit("/", 1)[-1]
    if "." not in tail:
        return None
    ext = tail.rsplit(".", 1)[-1].lower()
    if len(ext) > 5 or not ext.isalnum():
        return None
    return ext


def strip_query(url: str) -> str:
    parsed = urlparse(url)
    return parsed._replace(query="", fragment="").geturl()


def host_of(url: str) -> str:
    return (urlparse(url).netloc or "").lower()