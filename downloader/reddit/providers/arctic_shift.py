"""Arctic Shift: a public, key-less archive of reddit posts.

Docs: https://arctic-shift.photon-reddit.com
Great for metadata (including ``media_metadata`` galleries on most posts) but
it is a third party mirror, so very fresh or deleted posts may be missing.
"""

from __future__ import annotations

from typing import Any, Optional

from ..exceptions import ProviderError
from ..http import HttpClient
from ..urls import PostRef
from .base import MetadataProvider


class ArcticShiftProvider(MetadataProvider):
    name = "arctic_shift"

    async def fetch(self, ref: PostRef, http: HttpClient) -> Optional[dict[str, Any]]:
        if not ref.post_id:
            return None
        url = f"{self.config.arctic_shift_url.rstrip('/')}/api/posts/ids"
        response = await http.request(
            "GET", url, params={"ids": ref.post_id}, retries=2
        )
        if response.status == 404:
            return None
        if not response.ok:
            raise ProviderError(self.name, f"HTTP {response.status} for {url}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError(self.name, f"invalid json: {exc}") from exc
        rows = payload.get("data") if isinstance(payload, dict) else payload
        if not rows:
            return None
        data = rows[0]
        if not isinstance(data, dict):
            return None
        data.setdefault("_sdk_provider", self.name)
        if ref.subreddit and not data.get("subreddit"):
            data["subreddit"] = ref.subreddit
        return data