"""Provider contract shared by every metadata source."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar, Optional
from urllib.parse import urlparse

from ..config import RedditConfig
from ..http import HttpClient
from ..urls import PostRef


async def canonical_permalink(ref: PostRef, http: HttpClient) -> Optional[str]:
    """Return ``/r/<sub>/comments/<id>/`` for a reference, if needed.

    Several reddit endpoints (oEmbed in particular) reject the short
    ``/comments/<id>/`` form and demand the full permalink. ``redd.it/<id>``
    still redirects to it, so we follow that one hop - it works even on
    networks where the json api is blocked.
    """
    permalink = ref.permalink
    if permalink and "/r/" in permalink:
        return permalink
    if ref.subreddit and ref.post_id:
        # no request needed when we already know where the post lives
        return f"/r/{ref.subreddit}/comments/{ref.post_id}/"
    if not ref.post_id:
        return permalink
    try:
        response = await http.request(
            "GET", f"https://redd.it/{ref.post_id}", retries=1, retry_statuses=(429, 503)
        )
    except Exception:  # noqa: BLE001 - best effort helper
        return permalink
    parsed = urlparse(response.url or "")
    if "/comments/" in parsed.path:
        return parsed.path
    return permalink


class MetadataProvider(ABC):
    """Fetches a reddit post payload.

    Implementations must return a mapping shaped like reddit's own post json
    (``id``, ``title``, ``author``, ``url``, ``media_metadata``, ...) so the
    rest of the SDK can treat every source identically. Returning ``None``
    means "this source does not have the post" and the client moves on to the
    next provider.
    """

    name: ClassVar[str] = "base"

    def __init__(self, config: RedditConfig) -> None:
        self.config = config

    @property
    def available(self) -> bool:
        """``False`` disables the provider for the current configuration."""
        return True

    @abstractmethod
    async def fetch(self, ref: PostRef, http: HttpClient) -> Optional[dict[str, Any]]:
        """Return the post payload or ``None`` when unavailable."""

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"<{type(self).__name__} name={self.name}>"