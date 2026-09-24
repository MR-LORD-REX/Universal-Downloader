"""Async HTTP layer: pooled session, retries, streaming and size probing."""

from __future__ import annotations

import asyncio
import json
import random
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Mapping, Optional, Sequence

import aiohttp

from .config import RedditConfig
from .exceptions import DownloadError, RedditError
from .urls import host_of

RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504, 522, 524})


@dataclass(slots=True)
class HttpResponse:
    """A fully buffered response."""

    status: int
    url: str
    headers: Mapping[str, str]
    content: bytes
    history: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def text(self) -> str:
        charset = "utf-8"
        ctype = self.headers.get("content-type", "")
        if "charset=" in ctype:
            charset = ctype.split("charset=", 1)[1].split(";")[0].strip() or "utf-8"
        try:
            return self.content.decode(charset, errors="replace")
        except LookupError:  # pragma: no cover - exotic charset
            return self.content.decode("utf-8", errors="replace")

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "")

    def json(self) -> Any:
        return json.loads(self.text)

    def raise_for_status(self) -> "HttpResponse":
        if not self.ok:
            raise RedditError(f"HTTP {self.status} for {self.url}")
        return self


@dataclass(slots=True)
class StreamInfo:
    """Metadata about an open byte stream."""

    status: int
    url: str
    size: Optional[int]
    mime_type: Optional[str]
    supports_range: bool
    offset: int = 0
    headers: Mapping[str, str] = field(default_factory=dict)


class _HostThrottle:
    """Keeps a minimum delay between requests to the same host."""

    def __init__(self, interval: float) -> None:
        self.interval = interval
        self._locks: dict[str, asyncio.Lock] = {}
        self._last: dict[str, float] = {}

    async def wait(self, host: str) -> None:
        if self.interval <= 0 or not host:
            return
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            last = self._last.get(host, 0.0)
            delay = self.interval - (now - last)
            if delay > 0:
                await asyncio.sleep(delay)
            self._last[host] = time.monotonic()


class SizeCache:
    """Caches ``Content-Length`` lookups (in memory + optional json file)."""

    def __init__(self, path: Optional[Path] = None, ttl: float = 21600.0) -> None:
        self.path = path
        self.ttl = ttl
        self._data: dict[str, tuple[Optional[int], Optional[str], float]] = {}
        self._loaded = False
        self._dirty = False

    def _load(self) -> None:
        if self._loaded or self.path is None:
            self._loaded = True
            return
        self._loaded = True
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for url, entry in raw.items():
            try:
                size, mime, stamp = entry
                self._data[url] = (size, mime, float(stamp))
            except (TypeError, ValueError):
                continue

    def get(self, url: str) -> Optional[tuple[Optional[int], Optional[str]]]:
        self._load()
        entry = self._data.get(url)
        if entry is None:
            return None
        size, mime, stamp = entry
        if self.ttl and time.time() - stamp > self.ttl:
            self._data.pop(url, None)
            return None
        return size, mime

    def set(self, url: str, size: Optional[int], mime: Optional[str]) -> None:
        self._load()
        self._data[url] = (size, mime, time.time())
        self._dirty = True

    def flush(self) -> None:
        if not self._dirty or self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._data), encoding="utf-8")
            self._dirty = False
        except OSError:  # pragma: no cover - cache is best effort
            pass


class HttpClient:
    """Small aiohttp wrapper with retries, throttling and range support."""

    def __init__(self, config: Optional[RedditConfig] = None) -> None:
        self.config = config or RedditConfig()
        self._session: Optional[aiohttp.ClientSession] = None
        self._semaphore = asyncio.Semaphore(max(1, self.config.max_http_concurrency))
        self._throttle = _HostThrottle(self.config.request_interval)
        self._sizes = SizeCache(
            (self.config.cache_dir / "sizes.json") if self.config.cache_dir else None,
            ttl=self.config.size_cache_ttl,
        )

    # ------------------------------------------------------------- lifecycle
    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(
                limit=max(4, self.config.max_http_concurrency * 4),
                limit_per_host=max(4, self.config.max_http_concurrency),
                ssl=self.config.verify_ssl,
                ttl_dns_cache=300,
            )
            timeout = aiohttp.ClientTimeout(
                total=self.config.timeout, connect=self.config.connect_timeout
            )
            self._session = aiohttp.ClientSession(
                connector=connector, timeout=timeout, trust_env=True
            )
        return self._session

    async def __aenter__(self) -> "HttpClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        self._sizes.flush()
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    # ------------------------------------------------------------- utilities
    def _headers(self, extra: Optional[Mapping[str, str]] = None, *, api: bool = False) -> dict[str, str]:
        base = {
            "User-Agent": self.config.api_user_agent if api else self.config.user_agent,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
        }
        if extra:
            base.update({k: v for k, v in extra.items() if v is not None})
        return base

    def _sleep_for_attempt(self, attempt: int) -> float:
        return min(
            self.config.max_retry_wait,
            self.config.backoff_factor * (2 ** attempt) + random.uniform(0, 0.25),
        )

    @staticmethod
    def _retry_after(headers: Mapping[str, str]) -> Optional[float]:
        value = headers.get("retry-after")
        if not value:
            return None
        try:
            return float(value)
        except ValueError:
            return None

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[Mapping[str, str]] = None,
        params: Optional[Mapping[str, Any]] = None,
        data: Optional[Any] = None,
        auth: Optional[aiohttp.BasicAuth] = None,
        retries: Optional[int] = None,
        api: bool = False,
        allow_redirects: bool = True,
        retry_statuses: Sequence[int] = tuple(RETRY_STATUSES),
        read: bool = True,
    ) -> HttpResponse:
        """Perform an HTTP request with exponential backoff."""
        attempts = self.config.retries if retries is None else retries
        last_error: Optional[Exception] = None
        for attempt in range(attempts + 1):
            await self._throttle.wait(host_of(url))
            try:
                async with self._semaphore:
                    async with self.session.request(
                        method,
                        url,
                        headers=self._headers(headers, api=api),
                        params=params,
                        data=data,
                        auth=auth,
                        allow_redirects=allow_redirects,
                        proxy=self.config.proxy,
                    ) as response:
                        if response.status in retry_statuses and attempt < attempts:
                            wait = self._retry_after(response.headers) or self._sleep_for_attempt(attempt)
                            await asyncio.sleep(wait)
                            last_error = RedditError(f"HTTP {response.status}")
                            continue
                        body = await response.read() if read else b""
                        return HttpResponse(
                            status=response.status,
                            url=str(response.url),
                            headers=_lower_headers(response.headers),
                            content=body,
                            history=tuple(str(r.url) for r in response.history),
                        )
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                last_error = exc
                if attempt >= attempts:
                    break
                await asyncio.sleep(self._sleep_for_attempt(attempt))
        raise DownloadError(f"{method} {url} failed after {attempts + 1} attempt(s): {last_error}")

    async def get(self, url: str, **kwargs: Any) -> HttpResponse:
        return await self.request("GET", url, **kwargs)

    async def get_text(self, url: str, **kwargs: Any) -> str:
        return (await self.request("GET", url, **kwargs)).text

    async def get_json(self, url: str, **kwargs: Any) -> Any:
        response = await self.request("GET", url, **kwargs)
        if not response.ok:
            raise RedditError(f"HTTP {response.status} for {url}")
        try:
            return response.json()
        except ValueError as exc:
            raise RedditError(f"invalid json from {url}: {exc}") from exc

    # ----------------------------------------------------------------- sizes
    async def head_size(
        self,
        url: str,
        *,
        use_cache: Optional[bool] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> tuple[Optional[int], Optional[str]]:
        """Resolve ``(size_bytes, mime_type)`` for ``url`` without downloading.

        Uses ``HEAD`` and falls back to a one-byte ranged ``GET`` for CDNs that
        do not answer ``HEAD`` (reddit's ``v.redd.it`` does).
        """
        cache_enabled = self.config.use_cache if use_cache is None else use_cache
        if cache_enabled:
            cached = self._sizes.get(url)
            if cached is not None:
                return cached

        size: Optional[int] = None
        mime: Optional[str] = None
        try:
            response = await self.request(
                "HEAD", url, headers=headers, retries=1, retry_statuses=()
            )
            if response.ok:
                size = _int_or_none(response.headers.get("content-length"))
                mime = response.headers.get("content-type")
        except DownloadError:
            pass

        if size is None:
            size, mime = await self._range_size(url, headers=headers, fallback_mime=mime)

        if cache_enabled and size is not None:
            self._sizes.set(url, size, mime)
        return size, mime

    async def _range_size(
        self,
        url: str,
        *,
        headers: Optional[Mapping[str, str]] = None,
        fallback_mime: Optional[str] = None,
    ) -> tuple[Optional[int], Optional[str]]:
        merged = {"Range": "bytes=0-0"}
        if headers:
            merged.update(headers)
        try:
            response = await self.request(
                "GET", url, headers=merged, retries=0, retry_statuses=()
            )
        except DownloadError:
            return None, fallback_mime
        if response.status not in (200, 206):
            return None, fallback_mime
        content_range = response.headers.get("content-range")
        if content_range and "/" in content_range:
            size = _int_or_none(content_range.rsplit("/", 1)[-1])
            if size:
                return size, response.headers.get("content-type") or fallback_mime
        return _int_or_none(response.headers.get("content-length")), response.headers.get(
            "content-type"
        ) or fallback_mime

    async def exists(self, url: str, *, headers: Optional[Mapping[str, str]] = None) -> bool:
        """Cheap existence check (used when probing CDN variants)."""
        try:
            response = await self.request(
                "HEAD", url, headers=headers, retries=0, retry_statuses=()
            )
        except DownloadError:
            return False
        return response.ok

    # ------------------------------------------------------------ streaming
    @asynccontextmanager
    async def open(
        self,
        url: str,
        *,
        offset: int = 0,
        headers: Optional[Mapping[str, str]] = None,
        retries: Optional[int] = None,
    ) -> AsyncIterator[tuple[StreamInfo, AsyncIterator[bytes]]]:
        """Open a byte stream, transparently resuming after transport errors.

        Yields ``(info, chunks)``; ``info.size`` is the **total** size of the
        resource even when a range request was used.
        """
        attempts = self.config.retries if retries is None else retries
        received = 0
        last_error: Optional[Exception] = None
        for attempt in range(attempts + 1):
            request_headers = dict(headers or {})
            if offset + received > 0:
                request_headers["Range"] = f"bytes={offset + received}-"
            await self._throttle.wait(host_of(url))
            try:
                async with self._semaphore:
                    async with self.session.get(
                        url,
                        headers=self._headers(request_headers),
                        allow_redirects=True,
                        proxy=self.config.proxy,
                    ) as response:
                        if response.status in RETRY_STATUSES and attempt < attempts:
                            wait = self._retry_after(response.headers) or self._sleep_for_attempt(attempt)
                            await asyncio.sleep(wait)
                            last_error = RedditError(f"HTTP {response.status}")
                            continue
                        if response.status not in (200, 206):
                            raise DownloadError(f"HTTP {response.status} for {url}")

                        total = _int_or_none(response.headers.get("content-length"))
                        if response.status == 206:
                            content_range = response.headers.get("content-range", "")
                            if "/" in content_range:
                                total = _int_or_none(content_range.rsplit("/", 1)[-1])
                        elif offset + received > 0:
                            received = 0  # server ignored Range, start over

                        info = StreamInfo(
                            status=response.status,
                            url=str(response.url),
                            size=None if total is None else total,
                            mime_type=response.headers.get("content-type"),
                            supports_range=response.status == 206
                            or response.headers.get("accept-ranges", "").lower() == "bytes",
                            offset=offset + received,
                            headers=dict(response.headers),
                        )

                        async def iterator() -> AsyncIterator[bytes]:
                            nonlocal received
                            try:
                                async for chunk in response.content.iter_chunked(
                                    self.config.chunk_size
                                ):
                                    received += len(chunk)
                                    yield chunk
                            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                                raise _StreamInterrupted(exc) from exc

                        yield info, iterator()
                        return
            except _StreamInterrupted as exc:
                last_error = exc.__cause__ or exc
                if attempt >= attempts:
                    break
                await asyncio.sleep(self._sleep_for_attempt(attempt))
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                last_error = exc
                if attempt >= attempts:
                    break
                await asyncio.sleep(self._sleep_for_attempt(attempt))
        raise DownloadError(f"streaming {url} failed after {attempts + 1} attempt(s): {last_error}")


class _StreamInterrupted(Exception):
    """Internal marker: the body failed mid-transfer and may be resumed."""


def _lower_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """aiohttp headers are case-insensitive, plain dicts are not."""
    return {key.lower(): value for key, value in headers.items()}


def _int_or_none(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None