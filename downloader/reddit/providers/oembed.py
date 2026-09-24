"""oEmbed: the lowest common denominator fallback.

Reddit's oEmbed endpoint currently returns only ``title`` and ``author_name``
(the ``thumbnail_url`` field is normally absent), so this provider is used last,
when every richer source failed. It is however the most reliable endpoint of all
- it keeps answering when the json api, rss and old.reddit are blocked.
"""

from __future__ import annotations

from typing import Any, Optional
from urllib.parse import quote, urlparse

from ..exceptions import ProviderError
from ..http import HttpClient
from ..urls import PostRef
from .base import MetadataProvider, canonical_permalink


class OEmbedProvider(MetadataProvider):
    name = "oembed"

    def __init__(self, config) -> None:
        super().__init__(config)
        self.endpoint = "https://www.reddit.com/oembed"

    async def fetch(self, ref: PostRef, http: HttpClient) -> Optional[dict[str, Any]]:
        permalink = await canonical_permalink(ref, http)
        if not permalink:
            return None
        if permalink.startswith("http"):
            permalink = urlparse(permalink).path or permalink
        absolute_permalink = f"https://www.reddit.com{permalink}"
        url = f"{self.endpoint}?url={quote(absolute_permalink, safe='')}"
        response = await http.request("GET", url, retries=1, retry_statuses=(429, 500, 502, 503))
        if response.status in (400, 403, 404, 429):
            return None
        if not response.ok:
            raise ProviderError(self.name, f"HTTP {response.status} for {url}")
        try:
            payload = response.json()
        except ValueError:
            return None
        if not payload.get("title"):
            return None
        thumbnail = payload.get("thumbnail_url")
        return {
            "id": ref.post_id,
            "title": payload.get("title"),
            "author": payload.get("author_name"),
            "subreddit": ref.subreddit,
            "permalink": permalink,
            "thumbnail": thumbnail,
            "preview": {"images": [{"source": {"url": thumbnail}}]} if thumbnail else None,
            "_sdk_provider": self.name,
            "_sdk_partial": True,
            "_sdk_note": (
                "oEmbed exposes the title and author only; no media urls are "
                "available from this source"
            ),
        }