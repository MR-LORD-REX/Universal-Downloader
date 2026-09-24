"""Progress reporting primitives shared by every SDK."""

from __future__ import annotations

from typing import Callable, Optional

from pydantic import BaseModel, ConfigDict

from .enums import StrEnum


class ProgressPhase(StrEnum):
    """Stage of a download pipeline a progress tick belongs to."""

    RESOLVING = "resolving"
    PROBING = "probing"
    DOWNLOADING = "downloading"
    MUXING = "muxing"
    SAVING = "saving"
    DONE = "done"


class ProgressEvent(BaseModel):
    """A single progress tick.

    ``total`` is ``None`` when the server did not send a ``Content-Length``.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    phase: ProgressPhase
    message: str = ""
    platform: Optional[str] = None
    item_index: Optional[int] = None
    item_total: Optional[int] = None
    filename: Optional[str] = None
    downloaded: int = 0
    total: Optional[int] = None

    @property
    def percent(self) -> Optional[float]:
        """Completion ratio in ``[0, 100]`` or ``None`` when unknown."""
        if not self.total:
            return None
        return round(self.downloaded / self.total * 100, 2)

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        pct = f"{self.percent:5.1f}%" if self.percent is not None else "  ?  "
        return f"{self.phase.value:<11} {pct} {self.message}".rstrip()


ProgressCallback = Callable[[ProgressEvent], None]
MaybeAsyncProgress = Callable[[ProgressEvent], object]


async def emit(callback: Optional[MaybeAsyncProgress], event: ProgressEvent) -> None:
    """Invoke a progress callback whether it is sync or async."""
    if callback is None:
        return
    result = callback(event)
    if hasattr(result, "__await__"):
        await result  # type: ignore[misc]