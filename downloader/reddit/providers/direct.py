"""Direct url "provider": no post lookup, the url *is* the media."""

from __future__ import annotations

from typing import Any, Optional
from urllib.parse import urlparse

from ..http import HttpClient
from ..urls import PostRef
from .base import MetadataProvider


class DirectProvider(MetadataProvider):
    """Handles ``i.redd.it``/``v.redd.it``/external media urls."""

    name = "direct"

    async def fetch(self, ref: PostRef, http: HttpClient) -> Optional[dict[str, Any]]:
        if ref.kind not in ("media", "external"):
            return None
        url = ref.url
        stem = urlparse(url).path.strip("/")
        hints: dict[str, Any] = {"media_kind": ref.media_kind, "direct": True}

        if ref.media_kind == "video":
            base = url.rsplit("/", 1)[0] if "/" in stem else url.rstrip("/")
            hints["video_base"] = base
            return {
                "id": ref.post_id or stem.rsplit("/", 1)[-1],
                "title": stem.rsplit("/", 1)[-1],
                "domain": urlparse(url).netloc,
                "url": url,
                "url_overridden_by_dest": url,
                "is_video": True,
                "secure_media": {
                    "reddit_video": {
                        "fallback_url": url,
                        "has_audio": None,
                        "width": None,
                        "height": None,
                    }
                },
                "_sdk_provider": self.name,
                "_sdk_hints": hints,
            }

        filename = stem.rsplit("/", 1)[-1]
        stem_id = filename.rsplit(".", 1)[0] if "." in filename else filename
        return {
            "id": ref.post_id or stem_id,
            "title": stem.rsplit("/", 1)[-1],
            "domain": urlparse(url).netloc,
            "url": url,
            "url_overridden_by_dest": url,
            "post_hint": "image",
            "_sdk_provider": self.name,
            "_sdk_hints": hints,
        }