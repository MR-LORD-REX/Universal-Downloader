"""Minimal client for the public ``api.fxtwitter.com`` fixup api.

yt-dlp is excellent at *video* tweets but raises ``No video could be found``
for image tweets, and Twitter's own v1.1/v2 endpoints need credentials. The
fxtwitter fixup api is key-less and returns the tweet's full media list -
photos, videos and gifs - so it is used to discover media, while yt-dlp is
still used to enumerate the video quality ladder.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from ..core.exceptions import MetadataError
from ..core.http import HttpClient
from .config import TwitterConfig
from .exceptions import MediaAPIError, TweetNotFoundError, TweetUnavailableError
from .urls import TwitterRef

_JSON_HEADERS = {"Accept": "application/json"}

_TOKEN_ALPHABET_RE = re.compile(r"[^a-z0-9]", re.I)


def _token(tweet_id: str) -> str:
    """The (unvalidated) ``token`` parameter fxtwitter asks for."""
    try:
        value = (int(tweet_id) / 1e15) * 3.141592653589793
    except ValueError:  # pragma: no cover - ids are numeric
        return "0"
    return re.sub(r"(0+|\.)", "", f"{value:.15f}".rstrip("0"))


class FxTwitterAPI:
    """Thin wrapper over ``api.fxtwitter.com``."""

    def __init__(self, http: HttpClient, config: TwitterConfig) -> None:
        self.http = http
        self.config = config
        self.base = (config.media_api_url or "https://api.fxtwitter.com").rstrip("/")

    @property
    def enabled(self) -> bool:
        return (self.config.media_api or "auto").lower() != "off"

    async def tweet(self, tweet_id: str) -> dict[str, Any]:
        """Fetch a tweet payload by id."""
        if not self.enabled:
            raise MediaAPIError("the fxtwitter media api is disabled in the config")
        url = f"{self.base}/i/status/{tweet_id}"
        try:
            response = await self.http.get(url, headers=_JSON_HEADERS, retries=1)
        except MetadataError as exc:
            raise MediaAPIError(f"fxtwitter request failed: {exc}") from exc
        if response.status == 404:
            raise TweetNotFoundError(f"tweet {tweet_id} does not exist (404)")
        if response.status == 403:
            raise TweetUnavailableError(f"tweet {tweet_id} is protected or age gated (403)")
        if not response.ok:
            raise MediaAPIError(f"fxtwitter returned HTTP {response.status} for {tweet_id}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise MediaAPIError("fxtwitter returned invalid json") from exc
        if int(payload.get("code") or 0) != 200:
            raise MediaAPIError(
                f"fxtwitter error {payload.get('code')}: {payload.get('message')}"
            )
        tweet = payload.get("tweet")
        if not isinstance(tweet, dict):
            raise MediaAPIError("fxtwitter payload has no tweet object")
        return tweet

    async def tweet_for(self, ref: TwitterRef) -> dict[str, Any]:
        """Fetch the tweet a :class:`TwitterRef` points at."""
        if not ref.tweet_id:
            raise MediaAPIError("the reference has no tweet id")
        return await self.tweet(ref.tweet_id)


async def resolve_short_url(http: HttpClient, url: str) -> Optional[str]:
    """Follow a ``t.co`` redirect and return the final url."""
    try:
        response = await http.get(url, retries=1)
    except MetadataError:
        return None
    if response.ok or response.history:
        return response.url or (response.history[-1] if response.history else None)
    return None