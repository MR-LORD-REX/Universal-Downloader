"""Accept reddit payloads that the caller already has (tests, bots, offline)."""

from __future__ import annotations

import json
from typing import Any, Optional

from ..exceptions import ProviderError
from ..http import HttpClient
from ..urls import PostRef
from .base import MetadataProvider


def normalize_payload(payload: Any) -> Optional[dict[str, Any]]:
    """Accept the many shapes reddit json comes in and return one submission.

    Handles:

    * ``{"data": {"children": [{"data": {...}}]}}`` listings
    * ``[listing, listing]`` (what ``/comments/<id>.json`` returns)
    * ``{"kind": "t3", "data": {...}}`` things
    * a plain post dict
    * a json string of any of the above
    """
    if payload is None:
        return None
    if isinstance(payload, (str, bytes)):
        try:
            payload = json.loads(payload)
        except ValueError as exc:
            raise ProviderError("manual", f"payload is not valid json: {exc}") from exc

    if isinstance(payload, list):
        for entry in payload:
            found = normalize_payload(entry)
            if found:
                return found
        return None

    if isinstance(payload, dict):
        if payload.get("kind") in ("t3", "t1", "Listing", "more") and isinstance(payload.get("data"), dict):
            return normalize_payload(payload["data"])
        children = payload.get("children")
        if not isinstance(children, list) and isinstance(payload.get("data"), dict):
            children = payload["data"].get("children")
        if isinstance(children, list):
            for child in children:
                found = normalize_payload(child)
                if found:
                    return found
            return None
        if payload.get("id") and ("title" in payload or "media_metadata" in payload or "url" in payload):
            return payload
    return None


class ManualProvider(MetadataProvider):
    """Serves a payload handed over by the caller."""

    name = "manual"

    def __init__(self, config, payload: Any = None) -> None:
        super().__init__(config)
        self.payload = payload

    async def fetch(self, ref: PostRef, http: HttpClient) -> Optional[dict[str, Any]]:
        data = normalize_payload(self.payload)
        if data is None:
            return None
        data.setdefault("_sdk_provider", self.name)
        return data