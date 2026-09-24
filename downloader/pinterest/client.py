"""The Pinterest client: metadata, quality selection, downloading and saving.

Pinterest needs no credentials, but it does need two voices: the site's own
resource api (which sees images *and* everything descriptive about a pin) and
yt-dlp (which resolves the video quality ladder). The two are combined the same
way the Twitter SDK combines fxtwitter and yt-dlp - the api decides *what* the
pin holds, yt-dlp decides *which renditions* the video offers.
"""

from __future__ import annotations

import time
from typing import Any, Optional

from ..core.base import BaseClient
from ..core.enums import MediaKind, Platform
from ..core.exceptions import MetadataError, UnsupportedURLError
from ..core.models import PostMetadata
from ..core.progress import ProgressPhase
from ..core.ytdlp import YtDlpExtractor
from .api import PinterestAPI, resolve_short_url
from .config import PinterestConfig
from .exceptions import NoMediaFoundError, PinterestError
from .models import (
    attach_video_formats,
    board_to_metadata,
    pin_to_metadata,
    ytdlp_video_formats,
)
from .urls import PinterestRef, is_pinterest_url, parse_url


class PinterestClient(BaseClient):
    """Async Pinterest SDK for pins, image pins, idea pins and boards.

    Example
    -------
    >>> async with PinterestClient() as pin:
    ...     meta = await pin.get_metadata("https://www.pinterest.com/pin/1084663891475263837/")
    ...     print(meta.media_type, meta.media_group_type, meta.count)
    ...     print(meta.links())       # {'video': ['https://v1.pinimg.com/videos/mc/720p/....mp4']}
    ...     print(meta.size_human)    # known before downloading
    ...     result = await pin.download(meta)
    ...     await result.save("downloads")
    """

    platform = Platform.PINTEREST
    config_class = PinterestConfig

    def __init__(
        self,
        config: Optional[PinterestConfig] = None,
        *,
        progress_callback: Optional[Any] = None,
        **overrides: Any,
    ) -> None:
        super().__init__(config, progress_callback=progress_callback, **overrides)
        self._api = PinterestAPI(self._http, self.config)  # type: ignore[arg-type]
        self._extractor = YtDlpExtractor(self.config)

    @property
    def config(self) -> PinterestConfig:  # type: ignore[override]
        return self._config  # type: ignore[attr-defined]

    @config.setter
    def config(self, value: PinterestConfig) -> None:
        self._config = value

    # ------------------------------------------------------------- interface
    @classmethod
    def supports(cls, url: str) -> bool:
        """``True`` when ``url`` is a Pinterest url this client can handle."""
        return is_pinterest_url(url)

    async def get_metadata(
        self,
        url: str,
        *,
        include_video_formats: Optional[bool] = None,
        probe_sizes: Optional[bool] = None,
    ) -> PostMetadata:
        """Resolve everything known about a pin (or a board) without downloading."""
        ref = await self._reference(url)
        await self._emit(ProgressPhase.RESOLVING, f"resolving pinterest {ref.kind}")

        if ref.kind == "pin":
            metadata = await self._pin_metadata(ref, url)
            await self._add_video_ladder(metadata, ref, include_video_formats)
        elif ref.kind == "board":
            metadata = await self._board_metadata(ref, url)
        else:  # pragma: no cover - guarded by _reference
            raise UnsupportedURLError(f"unsupported Pinterest url: {url!r}")

        if not metadata.items:
            raise NoMediaFoundError("this pin carries no downloadable media")
        if probe_sizes is None:
            probe_sizes = self.config.probe_sizes
        if probe_sizes:
            await self._fill_sizes(metadata)
        return metadata

    # -------------------------------------------------------------- plumbing
    async def _reference(self, url: str) -> PinterestRef:
        ref = parse_url(url)
        if ref.kind == "short":
            if not self.config.resolve_short_links:
                raise UnsupportedURLError(
                    "pin.it links need resolve_short_links=True to be followed"
                )
            resolved = await resolve_short_url(self._http, ref.canonical)
            if not resolved:
                raise UnsupportedURLError(f"could not resolve the pin.it link {url!r}")
            ref = parse_url(resolved)
        if ref.kind == "profile":
            raise UnsupportedURLError(
                "profiles are not supported: Pinterest only serves the pins of a "
                "board without a login, so open a pin or a board url instead"
            )
        return ref

    async def _pin_metadata(self, ref: PinterestRef, url: str) -> PostMetadata:
        data = await self._api.pin(str(ref.pin_id))
        return pin_to_metadata(data, self.config, ref=ref, requested_url=url)

    async def _board_metadata(self, ref: PinterestRef, url: str) -> PostMetadata:
        if not self.config.resolve_boards:
            raise UnsupportedURLError("board urls are disabled (resolve_boards=False)")
        board = await self._api.board(str(ref.username), str(ref.board_slug))
        pins: list[dict[str, Any]] = []
        bookmark: Optional[str] = None
        limit = max(1, self.config.board_max_items)
        while len(pins) < limit:
            feed = await self._api.board_feed(str(board.get("id")), bookmark=bookmark)
            page = feed.get("data") or []
            pins.extend(entry for entry in page if isinstance(entry, dict))
            bookmark = feed.get("bookmark")
            if not bookmark or not page:
                break
        return board_to_metadata(
            pins[:limit], board, self.config, ref=ref, requested_url=url
        )

    async def _add_video_ladder(
        self,
        metadata: PostMetadata,
        ref: PinterestRef,
        include_video_formats: Optional[bool],
    ) -> None:
        """Replace the provisional video ladder with yt-dlp's rendition list."""
        wanted = (
            self.config.include_videos
            if include_video_formats is None
            else include_video_formats
        )
        if not wanted:
            return
        if not any(item.kind in (MediaKind.VIDEO, MediaKind.GIF) for item in metadata.items):
            return
        try:
            info = await self._extractor.extract(ref.pin_url)
        except MetadataError as exc:
            metadata.warnings.append(
                f"yt-dlp video ladder unavailable, using the best progressive "
                f"rendition instead: {exc}"
            )
            return
        ladder = ytdlp_video_formats(info, self.config)
        if not attach_video_formats(metadata, ladder):
            metadata.warnings.append("yt-dlp returned no video renditions")
            return
        metadata.extra["video_info"] = {
            "extractor": info.get("extractor"),
            "duration": info.get("duration"),
            "fps": info.get("fps"),
            "uploader": info.get("uploader"),
            "webpage_url": info.get("webpage_url"),
            "variant_count": len(info.get("formats") or []),
        }
        for item in metadata.items:
            if item.duration is None and info.get("duration"):
                item.duration = float(info["duration"])
            if item.width is None and info.get("width"):
                item.width = info.get("width")
            if item.height is None and info.get("height"):
                item.height = info.get("height")

    async def _fill_sizes(self, metadata: PostMetadata) -> None:
        formats = [
            fmt
            for item in metadata.items
            for fmt in item.formats
            if fmt.size_bytes is None and not fmt.is_manifest and fmt.url
        ]
        if formats:
            await self.probe_format_sizes(formats)
        for item in metadata.items:
            if item.formats:
                item.size_bytes = item.size_for("best", **metadata.spec_kwargs())
                if item.size_bytes is None:
                    item.size_bytes = item.total_size_bytes
        if metadata.thumbnail and metadata.thumbnails:
            size, _ = await self.http.probe_size(metadata.thumbnail)
            if size is not None:
                for thumb in metadata.thumbnails:
                    if thumb.url == metadata.thumbnail:
                        thumb.size_bytes = size
                        break

    # ---------------------------------------------------------------- extras
    async def download_by_url(self, url: str, **kwargs: Any) -> Any:
        """Convenience: :meth:`get_metadata` followed by :meth:`download`."""
        metadata = await self.get_metadata(url)
        return await self.download(metadata, **kwargs)

    def preview_url(self, metadata: PostMetadata, quality: str = "best") -> Optional[str]:
        """A ready-to-upload preview link (image original or video still)."""
        if metadata.items:
            try:
                return metadata.items[0].select(quality).url
            except PinterestError:  # pragma: no cover - defensive
                pass
        return metadata.thumbnail

    def plan_summary(self, metadata: PostMetadata, quality: Any = None) -> list[str]:
        """Human readable ``(item, format, size)`` list for one quality."""
        from ..core.select import build_plan

        spec = self.quality_spec(quality)
        lines: list[str] = []
        for entry in build_plan(metadata.items, spec):
            audio = entry.audio.format_id if entry.audio else "-"
            size = entry.total_size
            lines.append(
                f"item[{entry.item.index}] {entry.primary.format_id} + {audio}"
                f" | {entry.primary.extension} | {size} bytes | mux={entry.needs_mux}"
            )
        return lines


__all__ = ["PinterestClient"]