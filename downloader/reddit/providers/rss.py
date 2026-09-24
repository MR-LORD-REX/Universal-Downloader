"""Reddit's Atom/RSS endpoint.

Unusual but valuable: ``/comments/<id>/.rss`` is served by a different edge
path than ``.json`` and stays available in networks where the json API answers
with a block page. It yields the title, the author, the post time and - for
single media posts - the direct CDN url (the ``[link]`` anchor).
"""

from __future__ import annotations

import re
from typing import Any, Optional
from xml.etree import ElementTree

from ..exceptions import ProviderError, RedditError
from ..http import HttpClient
from ..urls import PostRef
from .base import MetadataProvider, canonical_permalink

ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}
AUTHOR_RE = re.compile(r"/user/([A-Za-z0-9_\-]{3,20})")
LINK_RE = re.compile(r"<a href=\"([^\"]+)\">\[link\]</a>", re.I)
IMG_RE = re.compile(r"<img src=\"([^\"]+)\"", re.I)


class RssProvider(MetadataProvider):
    name = "rss"

    async def fetch(self, ref: PostRef, http: HttpClient) -> Optional[dict[str, Any]]:
        if not ref.post_id:
            return None
        candidates = []
        permalink = await canonical_permalink(ref, http)
        if permalink:
            candidates.append(f"https://www.reddit.com{permalink.rstrip('/')}/.rss?limit=1")
        if ref.subreddit:
            candidates.append(
                f"https://www.reddit.com/r/{ref.subreddit}/comments/{ref.post_id}/.rss?limit=1"
            )
        candidates.append(f"https://www.reddit.com/comments/{ref.post_id}/.rss?limit=1")

        rate_limited = False
        for url in candidates:
            try:
                response = await http.request(
                    "GET", url, retries=1, retry_statuses=(429, 500, 502, 503)
                )
            except RedditError:
                continue
            if response.status == 429:
                # try the next url shape before giving up on rss
                rate_limited = True
                continue
            if response.status in (403, 404) or not response.ok:
                continue
            parsed = self._parse(response.text, ref)
            if parsed:
                return parsed
        if rate_limited:
            raise ProviderError(self.name, "rate limited by reddit rss (HTTP 429)")
        return None

    def _parse(self, text: str, ref: PostRef) -> Optional[dict[str, Any]]:  # noqa: C901
        if not text.strip():
            return None
        try:
            root = ElementTree.fromstring(text)
        except ElementTree.ParseError as exc:
            raise ProviderError(self.name, f"invalid feed: {exc}") from exc

        entry = None
        for candidate in root.findall("a:entry", ATOM_NS):
            identifier = (candidate.findtext("a:id", default="", namespaces=ATOM_NS) or "")
            if identifier.endswith(ref.post_id or "\0"):
                entry = candidate
                break
        if entry is None:
            entries = root.findall("a:entry", ATOM_NS)
            entry = entries[0] if entries else None
        if entry is None:
            return None

        title = entry.findtext("a:title", default=None, namespaces=ATOM_NS)
        published = entry.findtext("a:published", default=None, namespaces=ATOM_NS)
        permalink = None
        for link in entry.findall("a:link", ATOM_NS):
            permalink = link.get("href")
            break
        content = entry.findtext("a:content", default="", namespaces=ATOM_NS) or ""
        subreddit = None
        for category in entry.findall("a:category", ATOM_NS):
            subreddit = category.get("term")
            break

        author = None
        match = AUTHOR_RE.search(content)
        if match:
            author = match.group(1)

        media_url = None
        link_match = LINK_RE.search(content)
        if link_match:
            media_url = _unescape(link_match.group(1))

        thumbnail = None
        img_match = IMG_RE.search(content)
        if img_match:
            thumbnail = _unescape(img_match.group(1))

        data: dict[str, Any] = {
            "id": ref.post_id,
            "title": title,
            "author": author,
            "subreddit": subreddit or ref.subreddit,
            "permalink": permalink or ref.permalink,
            "created_utc": _parse_time(published),
            "thumbnail": thumbnail,
            "url": media_url,
            "url_overridden_by_dest": media_url,
            "_sdk_provider": self.name,
            "_sdk_partial": True,
        }
        if thumbnail:
            data["preview"] = {"images": [{"source": {"url": thumbnail}}]}
        return data


def _unescape(value: str) -> str:
    return (
        value.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&#32;", " ")
    )


def _parse_time(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    from datetime import datetime

    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None