"""The TikTok client: metadata, quality selection, downloading and saving.

Everything TikTok related goes through yt-dlp: it owns the (signed, obfuscated)
CDN urls, the ``app_info`` handshake and the short-link redirects. The SDK then
downloads the resulting urls itself, so sizes are known before downloading and
the download runs through the shared engine with progress and muxing.

**Testing note.** TikTok refuses most datacentre and VPN ranges with
``status 10204`` ("Your IP address is blocked from accessing this post"), which
raises :class:`~downloader.tiktok.exceptions.RegionBlockedError`. The offline
tests cover the payload translation; the live tests skip themselves when the
extractor reports a block. Configure ``proxy`` (or ``app_info``/``device_id``
plus ``cookiefile``) to run it from a blocked host.
"""

from __future__ import annotations

from typing import Any, Optional

from ..core.base import BaseClient
from ..core.enums import MediaKind, Platform
from ..core.exceptions import MetadataError, UnsupportedURLError
from ..core.models import PostMetadata
from ..core.progress import ProgressPhase
from ..core.ytdlp import YtDlpExtractor
from .config import TikTokConfig
from .exceptions import NoMediaFoundError, RegionBlockedError, TikTokError, VideoUnavailableError
from .extract import translate_tiktok_error
from .models import info_to_metadata, playlist_to_metadata
from .urls import TikTokRef, is_tiktok_url, parse_url


class TikTokClient(BaseClient):
    """Async TikTok SDK for videos, photo-post soundtracks and collections.

    Example
    -------
    >>> async with TikTokClient() as tt:
    ...     meta = await tt.get_metadata("https://www.tiktok.com/@user/video/7253412088251534594")
    ...     print(meta.media_type, meta.count, meta.size_human)
    ...     print(meta.links())        # {'video': ['https://v16-webapp-prime.tiktok.com/...']}
    ...     result = await tt.download(meta, quality="720p")
    ...     await result.save("downloads")
    """

    platform = Platform.TIKTOK
    config_class = TikTokConfig

    def __init__(
        self,
        config: Optional[TikTokConfig] = None,
        *,
        progress_callback: Optional[Any] = None,
        **overrides: Any,
    ) -> None:
        super().__init__(config, progress_callback=progress_callback, **overrides)
        self._extractor = YtDlpExtractor(self.config)

    @property
    def config(self) -> TikTokConfig:  # type: ignore[override]
        return self._config  # type: ignore[attr-defined]

    @config.setter
    def config(self, value: TikTokConfig) -> None:
        self._config = value

    # ------------------------------------------------------------- interface
    @classmethod
    def supports(cls, url: str) -> bool:
        """``True`` when ``url`` is a TikTok url this client can handle."""
        return is_tiktok_url(url)

    async def get_metadata(
        self, url: str, *, probe_sizes: Optional[bool] = None
    ) -> PostMetadata:
        """Resolve everything known about a video (or a collection)."""
        ref = parse_url(url)
        await self._emit(ProgressPhase.RESOLVING, f"resolving tiktok {ref.kind}")

        if ref.kind == "live":
            raise UnsupportedURLError(
                "TikTok live streams are not supported: yt-dlp can only follow a "
                "live stream while it is running, and the SDK needs a finished "
                "post to report sizes and renditions"
            )
        if ref.is_list:
            metadata = await self._list_metadata(ref, url)
        else:
            metadata = await self._video_metadata(ref, url)

        if not metadata.items:
            raise NoMediaFoundError("this post carries no downloadable media")
        if probe_sizes is None:
            probe_sizes = self.config.probe_sizes
        if probe_sizes:
            await self._fill_sizes(metadata)
        return metadata

    # -------------------------------------------------------------- plumbing
    async def _video_metadata(self, ref: TikTokRef, url: str) -> PostMetadata:
        info = await self._extract(ref.canonical or url)
        metadata = info_to_metadata(info, self.config, requested_url=url, ref=ref)
        resolved_id = str(info.get("id") or "")
        if resolved_id and ref.video_id is None:
            metadata.extra["resolved_id"] = resolved_id
        return metadata

    async def _list_metadata(self, ref: TikTokRef, url: str) -> PostMetadata:
        if not self.config.resolve_profiles:
            raise UnsupportedURLError(
                f"{ref.kind} urls are disabled (resolve_profiles=False); pass a "
                "direct video url instead"
            )
        info = await self._extract(
            ref.canonical or url, extract_flat=self.config.extract_flat
        )
        return playlist_to_metadata(info, self.config, requested_url=url, ref=ref)

    async def _extract(self, url: str, **extra: Any) -> dict[str, Any]:
        try:
            return await self._extractor.extract(url, **extra)
        except MetadataError as exc:
            raise translate_tiktok_error(str(exc)) from exc

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

    # ---------------------------------------------------------------- extras
    async def download_by_url(self, url: str, **kwargs: Any) -> Any:
        """Convenience: :meth:`get_metadata` followed by :meth:`download`."""
        metadata = await self.get_metadata(url)
        return await self.download(metadata, **kwargs)

    def preview_url(self, metadata: PostMetadata, quality: str = "best") -> Optional[str]:
        """A ready-to-upload preview link, or the poster frame when there is none."""
        if metadata.items:
            try:
                return metadata.items[0].select(quality).url
            except TikTokError:  # pragma: no cover - defensive
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

    @staticmethod
    def region_blocked(exc: BaseException) -> bool:
        """``True`` when ``exc`` is TikTok's ``IP address is blocked`` refusal."""
        return isinstance(exc, RegionBlockedError) or "ip address is blocked" in str(exc).lower()


__all__ = ["TikTokClient", "VideoUnavailableError"]