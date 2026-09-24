"""Async HTTP layer: pooled session, retries, size probing and streaming.

``HEAD`` is the cheapest way to learn a CDN file size, but not every CDN
answers it, and some (Twitter's ``video.twimg.com``) report a *different*
number than the metadata suggests. :meth:`HttpClient.probe_size` therefore
falls back to a one byte ``Range`` request and caches whatever it learns.
"""

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
from pydantic import BaseModel, ConfigDict

from .exceptions import DownloadError, MetadataError
from .urls import host_of

RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504, 522, 524})

DEFAULT_BROWSER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class HttpOptions(BaseModel):
    """Transport level knobs shared by every platform config."""

    model_config = ConfigDict(extra="forbid")

    user_agent: str = DEFAULT_BROWSER_AGENT
    timeout: float = 30.0
    connect_timeout: float = 10.0
    retries: int = 3
    backoff_factor: float = 0.6
    max_retry_wait: float = 15.0
    proxy: Optional[str] = None
    verify_ssl: bool = True
    max_concurrency: int = 8
    chunk_size: int = 256 * 1024
    ranged_download: bool = False
    range_chunk_size: int = 2 * 1024 * 1024
    request_interval: float = 0.0
    """Minimum seconds between two requests to the same host (0 disables)."""
    use_cache: bool = True
    cache_dir: Optional[Path] = None
    size_cache_ttl: float = 6 * 60 * 60
    cookies: dict[str, str] = field(default_factory=dict)


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
            raise MetadataError(f"HTTP {self.status} for {self.url}")
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
            delay = self.interval - (now - self._last.get(host, 0.0))
            if delay > 0:
                await asyncio.sleep(delay)
            self._last[host] = time.monotonic()


class _StreamInterrupted(Exception):
    """Internal marker: the body failed mid-transfer and may be resumed."""


class HttpClient:
    """Small aiohttp wrapper with retries, throttling and range support."""

    def __init__(self, options: Optional[HttpOptions] = None) -> None:
        self.options = options or HttpOptions()
        self._session: Optional[aiohttp.ClientSession] = None
        self._semaphore = asyncio.Semaphore(max(1, self.options.max_concurrency))
        self._throttle = _HostThrottle(self.options.request_interval)
        self._sizes = SizeCache(
            (self.options.cache_dir / "sizes.json") if self.options.cache_dir else None,
            ttl=self.options.size_cache_ttl,
        )

    # ------------------------------------------------------------- lifecycle
    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(
                limit=max(4, self.options.max_concurrency * 2),
                ssl=self.options.verify_ssl,
            )
            timeout = aiohttp.ClientTimeout(
                total=None,
                connect=self.options.connect_timeout,
                sock_read=self.options.timeout,
            )
            self._session = aiohttp.ClientSession(
                connector=connector,
                timeout=timeout,
                headers={"User-Agent": self.options.user_agent},
                cookies=self.options.cookies or None,
            )
        return self._session

    async def close(self) -> None:
        self._sizes.flush()
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def __aenter__(self) -> "HttpClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # --------------------------------------------------------------- helpers
    def _headers(self, extra: Optional[Mapping[str, str]] = None) -> dict[str, str]:
        headers = {"User-Agent": self.options.user_agent}
        if extra:
            headers.update({k: v for k, v in extra.items() if v})
        return headers

    def _sleep_for_attempt(self, attempt: int) -> float:
        base = self.options.backoff_factor * (2 ** attempt)
        return min(self.options.max_retry_wait, base + random.uniform(0, base / 2))

    @staticmethod
    def _retry_after(headers: Mapping[str, str]) -> Optional[float]:
        value = headers.get("retry-after")
        if not value:
            return None
        try:
            return min(30.0, float(value))
        except ValueError:
            return None

    @staticmethod
    def _method_allows_body(method: str) -> bool:
        return method.upper() not in ("HEAD", "GET", "DELETE", "OPTIONS")

    # -------------------------------------------------------------- requests
    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[Mapping[str, str]] = None,
        params: Optional[Mapping[str, Any]] = None,
        data: Any = None,
        json_body: Any = None,
        retries: Optional[int] = None,
        retry_statuses: Sequence[int] = tuple(RETRY_STATUSES),
        allow_redirects: bool = True,
    ) -> HttpResponse:
        """Perform a request and buffer the response body."""
        attempts = self.options.retries if retries is None else retries
        last_error: Optional[Exception] = None
        for attempt in range(attempts + 1):
            await self._throttle.wait(host_of(url))
            try:
                async with self._semaphore:
                    async with self.session.request(
                        method.upper(),
                        url,
                        headers=self._headers(headers),
                        params=params,
                        data=data if self._method_allows_body(method) else None,
                        json=json_body if self._method_allows_body(method) else None,
                        allow_redirects=allow_redirects,
                        proxy=self.options.proxy,
                    ) as response:
                        if response.status in retry_statuses and attempt < attempts:
                            await asyncio.sleep(
                                self._retry_after(response.headers)
                                or self._sleep_for_attempt(attempt)
                            )
                            last_error = MetadataError(f"HTTP {response.status}")
                            continue
                        content = await response.read()
                        history = tuple(str(r.url) for r in response.history)
                        return HttpResponse(
                            status=response.status,
                            url=str(response.url),
                            headers=lowercase_headers(response.headers),
                            content=content,
                            history=history,
                        )
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                last_error = exc
                if attempt >= attempts:
                    break
                await asyncio.sleep(self._sleep_for_attempt(attempt))
        raise MetadataError(f"{method} {url} failed after {attempts + 1} attempt(s): {last_error}")

    async def get(self, url: str, **kwargs: Any) -> HttpResponse:
        return await self.request("GET", url, **kwargs)

    async def head(self, url: str, **kwargs: Any) -> HttpResponse:
        kwargs.setdefault("retries", 1)
        kwargs.setdefault("retry_statuses", ())
        return await self.request("HEAD", url, **kwargs)

    async def get_json(self, url: str, **kwargs: Any) -> Any:
        response = await self.get(url, **kwargs)
        response.raise_for_status()
        try:
            return response.json()
        except ValueError as exc:
            raise MetadataError(f"{url} did not return json") from exc

    # ----------------------------------------------------------- size probes
    async def probe_size(
        self,
        url: str,
        *,
        headers: Optional[Mapping[str, str]] = None,
        use_cache: Optional[bool] = None,
    ) -> tuple[Optional[int], Optional[str]]:
        """Return ``(size, mime_type)`` for ``url`` without downloading it.

        Tries ``HEAD`` first and falls back to a one byte ranged ``GET`` when
        the CDN omits ``Content-Length``. Results are cached.
        """
        cached = use_cache if use_cache is not None else self.options.use_cache
        if cached:
            hit = self._sizes.get(url)
            if hit is not None:
                return hit
        size: Optional[int] = None
        mime: Optional[str] = None
        try:
            response = await self.head(url, headers=headers)
            if response.ok:
                size = _int_or_none(response.headers.get("content-length"))
                mime = response.headers.get("content-type")
        except MetadataError:
            pass
        if not size:
            size, mime = await self._probe_with_range(url, headers=headers, mime=mime)
        if cached:
            self._sizes.set(url, size, mime)
        return size, mime

    async def _probe_with_range(
        self,
        url: str,
        *,
        headers: Optional[Mapping[str, str]] = None,
        mime: Optional[str] = None,
    ) -> tuple[Optional[int], Optional[str]]:
        probe_headers = dict(headers or {})
        probe_headers["Range"] = "bytes=0-0"
        try:
            response = await self.get(url, headers=probe_headers, retries=1)
        except MetadataError:
            return None, mime
        if response.status == 206:
            content_range = response.headers.get("content-range", "")
            if "/" in content_range:
                return _int_or_none(content_range.rsplit("/", 1)[-1]), (
                    mime or response.headers.get("content-type")
                )
        if response.ok:
            return _int_or_none(response.headers.get("content-length")), (
                mime or response.headers.get("content-type")
            )
        return None, mime

    async def probe_sizes(
        self,
        urls: Sequence[str],
        *,
        headers: Optional[Mapping[str, str]] = None,
        concurrency: int = 6,
    ) -> dict[str, tuple[Optional[int], Optional[str]]]:
        """Probe many urls concurrently, preserving no particular order."""
        semaphore = asyncio.Semaphore(max(1, concurrency))

        async def run(target: str) -> tuple[str, tuple[Optional[int], Optional[str]]]:
            async with semaphore:
                return target, await self.probe_size(target, headers=headers)

        results = await asyncio.gather(*(run(u) for u in urls), return_exceptions=True)
        out: dict[str, tuple[Optional[int], Optional[str]]] = {}
        for entry in results:
            if isinstance(entry, BaseException):
                continue
            out[entry[0]] = entry[1]
        return out

    async def is_reachable(self, url: str, *, headers: Optional[Mapping[str, str]] = None) -> bool:
        """``True`` when a ``HEAD``/ranged ``GET`` confirms the url exists."""
        size, _ = await self.probe_size(url, headers=headers)
        if size is not None:
            return True
        try:
            response = await self.head(url, headers=headers)
        except MetadataError:
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
        attempts = self.options.retries if retries is None else retries
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
                        proxy=self.options.proxy,
                    ) as response:
                        if response.status in RETRY_STATUSES and attempt < attempts:
                            await asyncio.sleep(
                                self._retry_after(response.headers)
                                or self._sleep_for_attempt(attempt)
                            )
                            last_error = DownloadError(f"HTTP {response.status}")
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
                            size=total,
                            mime_type=response.headers.get("content-type"),
                            supports_range=response.status == 206
                            or response.headers.get("accept-ranges", "").lower() == "bytes",
                            offset=offset + received,
                            headers=lowercase_headers(response.headers),
                        )

                        async def iterator() -> AsyncIterator[bytes]:
                            nonlocal received
                            try:
                                async for chunk in response.content.iter_chunked(
                                    self.options.chunk_size
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
        raise DownloadError(
            f"streaming {url} failed after {attempts + 1} attempt(s): {last_error}"
        )


def lowercase_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Lower-case the header names so plain dict lookups stay case-insensitive."""
    return {key.lower(): value for key, value in headers.items()}


def _int_or_none(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None