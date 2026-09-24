"""The bot's two work queues.

```
message -> PlatformQueue["youtube"] -> metadata -> plan_route()
                         |                +--> direct  -> MediaSender (by url)
                         |                +--> process -> ProcessingQueue -> MediaSender
```

* One :class:`PlatformQueue` per platform: it paces metadata requests through a
  token bucket and runs a *resizable* number of workers, so the platform is
  never hammered and the queue absorbs bursts.
* A single :class:`ProcessingQueue` for anything that has to be downloaded
  and/or muxed. Admission is gated on a RAM budget (`PROCESSING_MAX_RAM_BYTES`)
  and on a per-platform queue quota.

Both queues are deliberately in-process: the bot is a single container, the
jobs are short lived, and a restart simply drops queued requests (the user is
told to try again).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from downloader.core.models import PostMetadata

from bot.services.routing import PlannedItem, RoutePlan
from bot.utils.text import format_size

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ helpers
class TokenBucket:
    """Simple async token bucket: `rate` permits per second."""

    def __init__(self, rate: float, capacity: Optional[float] = None) -> None:
        self._rate = max(float(rate), 0.01)
        self._capacity = float(capacity) if capacity else max(1.0, self._rate)
        self._tokens = self._capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    def set_rate(self, rate: float) -> None:
        self._rate = max(float(rate), 0.01)
        self._capacity = max(1.0, self._rate)
        self._tokens = min(self._tokens, self._capacity)

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(
                    self._capacity, self._tokens + (now - self._updated) * self._rate
                )
                self._updated = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                await asyncio.sleep((1 - self._tokens) / self._rate)


class Gate:
    """A resizable concurrency gate (unlike `asyncio.Semaphore`)."""

    def __init__(self, limit: int) -> None:
        self._limit = max(1, int(limit))
        self._active = 0
        self._condition = asyncio.Condition()

    def resize(self, limit: int) -> None:
        self._limit = max(1, int(limit))

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def active(self) -> int:
        return self._active

    async def __aenter__(self) -> "Gate":
        async with self._condition:
            await self._condition.wait_for(lambda: self._active < self._limit)
            self._active += 1
        return self

    async def __aexit__(self, *exc: object) -> None:
        async with self._condition:
            self._active -= 1
            self._condition.notify_all()


@dataclass(slots=True)
class QueueConfig:
    """Live tunables of a :class:`PlatformQueue`."""

    queue_size: int = 64
    concurrency: int = 2
    rate_per_second: float = 1.0


@dataclass(slots=True)
class QueueStats:
    depth: int = 0
    active: int = 0
    accepted: int = 0
    completed: int = 0
    failed: int = 0
    rejected: int = 0
    reserved_ram: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "depth": self.depth,
            "active": self.active,
            "accepted": self.accepted,
            "completed": self.completed,
            "failed": self.failed,
            "rejected": self.rejected,
            "reserved_ram": self.reserved_ram,
        }


# --------------------------------------------------------------------- jobs
@dataclass(slots=True)
class FetchJob:
    """A user's link, waiting for metadata resolution."""

    platform: str
    url: str
    chat_id: int
    message_id: Optional[int]
    user_tg_id: int
    user_db_id: Optional[int] = None
    chat_db_id: Optional[int] = None
    thread_id: Optional[int] = None
    quality: Optional[str] = None
    status_message_id: Optional[int] = None
    caption_enabled: bool = True
    submitted_at: float = field(default_factory=time.monotonic)


@dataclass(slots=True)
class ProcessJob:
    """A post whose media must be downloaded (and muxed) before upload."""

    platform: str
    url: str
    meta: PostMetadata
    route: RoutePlan
    quality: str
    chat_id: int
    message_id: Optional[int]
    user_tg_id: int
    user_db_id: Optional[int] = None
    chat_db_id: Optional[int] = None
    thread_id: Optional[int] = None
    status_message_id: Optional[int] = None
    reserved_ram: int = 0
    caption_enabled: bool = True
    submitted_at: float = field(default_factory=time.monotonic)

    @property
    def items(self) -> list[PlannedItem]:
        return list(self.route.process)

    @property
    def estimate(self) -> int:
        return self.route.process_bytes


FetchWorker = Callable[[FetchJob], Awaitable[None]]
ProcessWorker = Callable[[ProcessJob], Awaitable[None]]


# ------------------------------------------------------------ platform queue
class PlatformQueue:
    """Metadata queue for exactly one platform."""

    WORKER_POOL = 8

    def __init__(
        self,
        platform: str,
        *,
        config: QueueConfig,
        worker: FetchWorker,
        name: Optional[str] = None,
    ) -> None:
        self.platform = platform
        self.config = config
        self._worker = worker
        self._name = name or platform
        self._pending: asyncio.Queue[Optional[FetchJob]] = asyncio.Queue()
        self._gate = Gate(config.concurrency)
        self._bucket = TokenBucket(config.rate_per_second)
        self._tasks: list[asyncio.Task[None]] = []
        self.stats = QueueStats()
        self._running = False

    # -------------------------------------------------------------- config
    def apply_config(self, config: QueueConfig) -> None:
        self.config = config
        self._gate.resize(config.concurrency)
        self._bucket.set_rate(config.rate_per_second)

    # ----------------------------------------------------------- lifecycle
    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._tasks = [
            asyncio.create_task(self._run(), name=f"fetch-{self._name}-{index}")
            for index in range(self.WORKER_POOL)
        ]

    async def stop(self) -> None:
        self._running = False
        while not self._pending.empty():
            self._pending.get_nowait()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutdown
                pass
        self._tasks.clear()

    # ------------------------------------------------------------- submit
    def submit(self, job: FetchJob) -> tuple[bool, str]:
        """Queue a job. Returns `(accepted, reason_when_rejected)`."""
        if self._pending.qsize() >= self.config.queue_size:
            self.stats.rejected += 1
            return False, "the fetch queue for this platform is full"
        self.stats.accepted += 1
        self._pending.put_nowait(job)
        return True, ""

    @property
    def depth(self) -> int:
        return self._pending.qsize()

    # -------------------------------------------------------------- worker
    async def _run(self) -> None:
        while self._running:
            job = await self._pending.get()
            if job is None:
                continue
            await self._bucket.acquire()
            async with self._gate:
                self.stats.active += 1
                try:
                    await self._worker(job)
                    self.stats.completed += 1
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - a bad link must not kill the worker
                    self.stats.failed += 1
                    logger.exception("fetch worker failed for %s", job.url)
                finally:
                    self.stats.active -= 1
                    self._pending.task_done()


# ---------------------------------------------------------- processing queue
class ProcessingQueue:
    """One global queue for downloads/muxing, bounded by RAM and per platform."""

    WORKER_POOL = 8

    def __init__(
        self,
        *,
        queue_size: int,
        concurrency: int,
        max_ram_bytes: int,
        worker: ProcessWorker,
    ) -> None:
        self.queue_size = queue_size
        self._gate = Gate(concurrency)
        self.max_ram_bytes = max(0, int(max_ram_bytes))
        self._worker = worker
        self._pending: asyncio.Queue[Optional[ProcessJob]] = asyncio.Queue()
        self._tasks: list[asyncio.Task[None]] = []
        self._platform_limits: dict[str, int] = {}
        self._platform_depth: dict[str, int] = {}
        self.reserved_ram = 0
        self.stats = QueueStats()
        self._running = False

    # -------------------------------------------------------------- config
    def apply_config(
        self,
        *,
        queue_size: int,
        concurrency: int,
        max_ram_bytes: int,
        platform_limits: dict[str, int],
    ) -> None:
        self.queue_size = queue_size
        self._gate.resize(concurrency)
        self.max_ram_bytes = max(0, int(max_ram_bytes))
        self._platform_limits = dict(platform_limits)

    # ----------------------------------------------------------- lifecycle
    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._tasks = [
            asyncio.create_task(self._run(), name=f"process-{index}")
            for index in range(self.WORKER_POOL)
        ]

    async def stop(self) -> None:
        self._running = False
        while not self._pending.empty():
            self._pending.get_nowait()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutdown
                pass
        self._tasks.clear()
        self.reserved_ram = 0
        self._platform_depth.clear()

    # ------------------------------------------------------------- submit
    def can_admit(self, platform: str, reserved_ram: int) -> tuple[bool, str]:
        """Whether a job fits the RAM budget and the per platform quota."""
        if self._pending.qsize() >= self.queue_size:
            return False, "the processing queue is full"
        limit = self._platform_limits.get(platform)
        if limit is not None and self._platform_depth.get(platform, 0) >= limit:
            return False, f"the {platform} processing queue is full"
        if self.max_ram_bytes and self.reserved_ram + reserved_ram > self.max_ram_bytes:
            return False, (
                f"not enough processing memory (needs {format_size(reserved_ram)}, "
                f"{format_size(max(0, self.max_ram_bytes - self.reserved_ram))} free)"
            )
        return True, ""

    def submit(self, job: ProcessJob) -> tuple[bool, str]:
        accepted, reason = self.can_admit(job.platform, job.reserved_ram)
        if not accepted:
            self.stats.rejected += 1
            return False, reason
        self.reserved_ram += job.reserved_ram
        self._platform_depth[job.platform] = self._platform_depth.get(job.platform, 0) + 1
        self.stats.accepted += 1
        self._pending.put_nowait(job)
        return True, ""

    @property
    def depth(self) -> int:
        return self._pending.qsize()

    @property
    def platform_depths(self) -> dict[str, int]:
        return {name: depth for name, depth in self._platform_depth.items() if depth}

    # -------------------------------------------------------------- worker
    async def _run(self) -> None:
        while self._running:
            job = await self._pending.get()
            if job is None:
                continue
            async with self._gate:
                self.stats.active += 1
                try:
                    await self._worker(job)
                    self.stats.completed += 1
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - keep the queue alive
                    self.stats.failed += 1
                    logger.exception("processing failed for %s", job.url)
                finally:
                    self.stats.active -= 1
                    if job.reserved_ram:
                        self.reserved_ram = max(0, self.reserved_ram - job.reserved_ram)
                    self._platform_depth[job.platform] = max(
                        0, self._platform_depth.get(job.platform, 1) - 1
                    )
                    self._pending.task_done()


# --------------------------------------------------------------- the manager
class PlatformQueueManager:
    """Owns the per platform queues plus the shared processing queue."""

    def __init__(
        self,
        *,
        fetch_worker: FetchWorker,
        process_worker: ProcessWorker,
        configs: dict[str, QueueConfig],
        processing_queue_size: int = 32,
        processing_concurrency: int = 1,
        processing_max_ram_bytes: int = 2 * 1024**3,
        platform_limits: Optional[dict[str, int]] = None,
    ) -> None:
        self._fetch_worker = fetch_worker
        self._process_worker = process_worker
        self.platforms: dict[str, PlatformQueue] = {
            name: PlatformQueue(name, config=config, worker=self._dispatch_fetch)
            for name, config in configs.items()
        }
        self.processing = ProcessingQueue(
            queue_size=processing_queue_size,
            concurrency=processing_concurrency,
            max_ram_bytes=processing_max_ram_bytes,
            worker=self._dispatch_process,
        )
        self.processing.apply_config(
            queue_size=processing_queue_size,
            concurrency=processing_concurrency,
            max_ram_bytes=processing_max_ram_bytes,
            platform_limits=dict(platform_limits or {}),
        )

    async def _dispatch_fetch(self, job: FetchJob) -> None:
        await self._fetch_worker(job)

    async def _dispatch_process(self, job: ProcessJob) -> None:
        await self._process_worker(job)

    # ----------------------------------------------------------- lifecycle
    async def start(self) -> None:
        for queue in self.platforms.values():
            await queue.start()
        await self.processing.start()

    async def stop(self) -> None:
        for queue in self.platforms.values():
            await queue.stop()
        await self.processing.stop()

    # -------------------------------------------------------------- routing
    def submit_fetch(self, job: FetchJob) -> tuple[bool, str]:
        queue = self.platforms.get(job.platform)
        if queue is None:
            return False, f"the {job.platform} queue is not available"
        return queue.submit(job)

    def submit_process(self, job: ProcessJob) -> tuple[bool, str]:
        return self.processing.submit(job)

    def depth_of(self, platform: str) -> int:
        queue = self.platforms.get(platform)
        return queue.depth if queue else 0

    # ------------------------------------------------------------- snapshot
    def snapshot(self) -> dict[str, Any]:
        return {
            "platforms": {
                name: {
                    **queue.stats.as_dict(),
                    "depth": queue.depth,
                    "queue_size": queue.config.queue_size,
                    "rate_per_second": queue.config.rate_per_second,
                }
                for name, queue in self.platforms.items()
            },
            "processing": {
                **self.processing.stats.as_dict(),
                "depth": self.processing.depth,
                "queue_size": self.processing.queue_size,
                "max_ram_bytes": self.processing.max_ram_bytes,
                "reserved_ram": self.processing.reserved_ram,
                "per_platform": self.processing.platform_depths,
            },
        }


__all__ = [
    "FetchJob",
    "Gate",
    "PlatformQueue",
    "PlatformQueueManager",
    "ProcessJob",
    "ProcessingQueue",
    "QueueConfig",
    "QueueStats",
    "TokenBucket",
]
