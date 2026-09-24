"""Minimal client for Pinterest's public ``resource`` endpoints.

yt-dlp's Pinterest extractor only ever returns *videos*: an image pin resolves
to an empty format list, and image pins are the majority of Pinterest. The
site's own resource api returns the full pin object instead - images, title,
author, stats and the outbound link - so it is used to discover media (and to
build a usable fallback video format), while yt-dlp is still used to enumerate
the video quality ladder.

Both calls are key-less: Pinterest serves them to anonymous visitors as long as
the ``X-Pinterest-PWS-Handler`` header is present (that header is what the
website itself sends, and yt-dlp relies on the same trick).
"""

from __future__ import annotations

import json
from typing import Any, Optional

from ..core.exceptions import MetadataError
from ..core.http import HttpClient
from .config import PinterestConfig
from .exceptions import (
    BoardNotFoundError,
    PinNotFoundError,
    PinterestAPIError,
)

BASE_URL = "https://www.pinterest.com/resource"

_HEADERS = {
    "Accept": "application/json",
    "X-Pinterest-PWS-Handler": "www/[username].js",
}


class PinterestAPI:
    """Thin wrapper over ``/resource/<Name>Resource/get/``."""

    def __init__(self, http: HttpClient, config: PinterestConfig) -> None:
        self.http = http
        self.config = config
        self.base = BASE_URL.rstrip("/")

    # ----------------------------------------------------------- resources
    async def pin(self, pin_id: str) -> dict[str, Any]:
        """The full pin object (``unauth_react_main_pin`` field set)."""
        data = await self._get(
            "Pin",
            pin_id,
            {"field_set_key": "unauth_react_main_pin", "id": pin_id},
        )
        if not isinstance(data, dict):
            raise PinNotFoundError(f"pin {pin_id} does not exist")
        return data

    async def board(self, username: str, slug: str) -> dict[str, Any]:
        """Resolve a board slug into the board object (which carries its id)."""
        data = await self._get("Board", slug, {"slug": slug, "username": username})
        if not isinstance(data, dict) or not data.get("id"):
            raise BoardNotFoundError(f"board {username}/{slug} does not exist")
        return data

    async def board_feed(
        self, board_id: str, *, bookmark: Optional[str] = None
    ) -> dict[str, Any]:
        """One page of a board's pins (``data`` is a list, ``bookmark`` paged)."""
        options: dict[str, Any] = {"board_id": board_id, "page_size": 250}
        if bookmark:
            options["bookmarks"] = [bookmark]
        data = await self._get("BoardFeed", str(board_id), options)
        if isinstance(data, list):
            return {"data": data, "bookmark": None}
        return data if isinstance(data, dict) else {"data": [], "bookmark": None}

    # ------------------------------------------------------------- plumbing
    async def _get(
        self, resource: str, ref: str, options: dict[str, Any]
    ) -> Any:
        url = f"{self.base}/{resource}Resource/get/"
        params = {"data": json.dumps({"options": options})}
        try:
            response = await self.http.get(
                url, params=params, headers=_HEADERS, retries=1
            )
        except MetadataError as exc:
            raise PinterestAPIError(f"{resource} request failed: {exc}") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise PinterestAPIError(
                f"{resource} returned invalid json (HTTP {response.status})"
            ) from exc
        body = payload.get("resource_response") if isinstance(payload, dict) else None
        body = body if isinstance(body, dict) else {}
        error = body.get("error")
        if isinstance(error, dict):
            message = str(error.get("message") or "request failed")
            status = int(error.get("http_status") or error.get("code") or 0)
            if status == 404 or response.status == 404:
                if resource == "Board":
                    raise BoardNotFoundError(f"board {ref} not found")
                raise PinNotFoundError(f"pin {ref} not found")
            raise PinterestAPIError(f"{resource} error: {message}")
        if not response.ok:
            if response.status == 404:
                raise PinNotFoundError(f"{ref} not found")
            raise PinterestAPIError(f"{resource} returned HTTP {response.status}")
        data = body.get("data")
        if data is None:
            if resource == "Board":
                raise BoardNotFoundError(f"board {ref} not found")
            raise PinNotFoundError(f"pin {ref} not found")
        return data


async def resolve_short_url(http: HttpClient, url: str) -> Optional[str]:
    """Follow a ``pin.it`` redirect and return the final url."""
    try:
        response = await http.get(url, retries=1)
    except MetadataError:
        return None
    if response.ok or response.history:
        return response.url or (response.history[-1] if response.history else None)
    return None


__all__ = ["BASE_URL", "PinterestAPI", "resolve_short_url"]