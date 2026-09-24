"""Reddit's own OAuth API: the highest fidelity source."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

import aiohttp

from ..exceptions import AuthenticationError, ProviderError
from ..http import HttpClient
from ..urls import PostRef
from .base import MetadataProvider


class OAuthProvider(MetadataProvider):
    """Reads posts through ``oauth.reddit.com``.

    Supports three credential styles:

    * a ready made ``access_token`` (used as-is),
    * ``client_id`` + ``client_secret`` + ``refresh_token`` (script app),
    * ``client_id`` + ``client_secret`` + ``username`` + ``password``.
    """

    name = "oauth"

    def __init__(self, config) -> None:
        super().__init__(config)
        self._token: Optional[str] = config.access_token
        self._expires_at: float = 0.0 if not config.access_token else time.time() + 3600
        self._lock = asyncio.Lock()

    @property
    def available(self) -> bool:
        return self.config.has_oauth_credentials

    async def _token_value(self, http: HttpClient) -> str:
        async with self._lock:
            if self._token and time.time() < self._expires_at - 60:
                return self._token
            if not (self.config.client_id and self.config.client_secret):
                raise AuthenticationError("client_id/client_secret are required for the oauth provider")
            data: dict[str, Any] = {}
            if self.config.refresh_token:
                data = {
                    "grant_type": "refresh_token",
                    "refresh_token": self.config.refresh_token,
                }
            elif self.config.username and self.config.password:
                data = {
                    "grant_type": "password",
                    "username": self.config.username,
                    "password": self.config.password,
                }
            else:
                data = {"grant_type": "client_credentials"}

            auth = aiohttp.BasicAuth(self.config.client_id, self.config.client_secret)
            response = await http.request(
                "POST",
                self.config.token_url,
                data=data,
                auth=auth,
                api=True,
                retries=1,
                retry_statuses=(429, 500, 502, 503, 504),
            )
            if response.status != 200:
                raise AuthenticationError(
                    f"token request failed ({response.status}): {response.text[:200]}"
                )
            payload = response.json()
            token = payload.get("access_token")
            if not token:
                raise AuthenticationError("token response did not contain access_token")
            self._token = token
            self._expires_at = time.time() + float(payload.get("expires_in", 3600))
            return token

    async def _get(self, http: HttpClient, path: str, params: Optional[dict] = None) -> Any:
        token = await self._token_value(http)
        url = f"{self.config.oauth_url.rstrip('/')}{path}"
        response = await http.request(
            "GET",
            url,
            params=params,
            headers={"Authorization": f"bearer {token}"},
            api=True,
            retries=1,
            retry_statuses=(429, 500, 502, 503),
        )
        if response.status == 401:
            self._token = None
            self._expires_at = 0.0
            token = await self._token_value(http)
            response = await http.request(
                "GET",
                url,
                params=params,
                headers={"Authorization": f"bearer {token}"},
                api=True,
                retries=1,
            )
        if response.status == 404:
            return None
        if not response.ok:
            raise ProviderError(self.name, f"HTTP {response.status} for {path}")
        return response.json()

    async def fetch(self, ref: PostRef, http: HttpClient) -> Optional[dict[str, Any]]:
        if not ref.post_id:
            return None
        payload = await self._get(
            http, f"/comments/{ref.post_id}", params={"raw_json": 1, "limit": 0}
        )
        data = _first_post(payload)
        if data is None:
            return None
        if ref.subreddit and not data.get("subreddit"):
            data["subreddit"] = ref.subreddit
        return data

    async def fetch_by_id(self, post_id: str, http: HttpClient) -> Optional[dict[str, Any]]:
        """Fetch a bare post (used to expand crosspost parents)."""
        return await self.fetch(PostRef(kind="post", url="", post_id=post_id), http)


def _first_post(payload: Any) -> Optional[dict[str, Any]]:
    """Pull the submission out of ``/comments/<id>`` style listings."""
    if isinstance(payload, list):
        for listing in payload:
            found = _first_post(listing)
            if found:
                return found
        return None
    if isinstance(payload, dict):
        children = (payload.get("data") or {}).get("children")
        if isinstance(children, list):
            for child in children:
                item = child.get("data") if isinstance(child, dict) else None
                if item and item.get("id") and "title" in item:
                    return item
        if payload.get("id") and payload.get("title"):
            return payload
    return None