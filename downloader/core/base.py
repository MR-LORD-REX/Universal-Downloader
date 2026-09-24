"""Shared client plumbing: lifecycle, download orchestration and size probes."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Optional, Sequence

from .config import PlatformConfig
from .engine import DownloadEngine
from .enums import DownloadTarget, Platform
from .exceptions import MediaNotAvailableError
from .http import HttpClient
from .models import DownloadResult, MediaFormat, PostMetadata
from .progress import ProgressEvent, ProgressPhase, emit
from .saver import save_metadata, save_result
from .select import QualitySpec, build_plan, parse_quality


class BaseClient(ABC):
    """Common behaviour of every platform client.

    Subclasses only have to implement :meth:`get_metadata` (and optionally
    :meth:`probe_format_sizes`); everything else - quality resolution, the
    download engine, ``save`` and progress plumbing - lives here.
    """

    platform: Platform = Platform.UNKNOWN
    config_class: type[PlatformConfig] = PlatformConfig

    def __init__(
        self,
        config: Optional[PlatformConfig] = None,
        *,
        progress_callback: Optional[Any] = None,
        **overrides: Any,
    ) -> None:
        if config is None:
            config = self.config_class().with_overrides(**overrides)  # type: ignore[assignment]
        elif overrides:
            config = config.with_overrides(**overrides)  # type: ignore[assignment]
        self.config: PlatformConfig = config
        self._http = HttpClient(self.config.http_options())
        self._engine = DownloadEngine(
            self._http,
            platform=str(self.platform),
            max_concurrency=self.config.max_download_concurrency,
            max_size_bytes=self.config.max_size_bytes,
            use_ffmpeg=self.config.mux_audio,
            verify_audio=self.config.verify_audio,
        )
        self._progress = progress_callback

    # ------------------------------------------------------------- lifecycle
    @property
    def http(self) -> HttpClient:
        """The shared :class:`HttpClient` (also usable for your own requests)."""
        return self._http

    async def __aenter__(self) -> "BaseClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        """Flush caches, drop scratch files and close the connection pool."""
        self._engine.cleanup()
        await self._http.close()

    # ------------------------------------------------------------- interface
    @abstractmethod
    async def get_metadata(self, url: str, **kwargs: Any) -> PostMetadata:
        """Resolve everything known about ``url`` without downloading media."""

    @classmethod
    def supports(cls, url: str) -> bool:
        """``True`` when ``url`` looks like it belongs to this platform."""
        return False

    # -------------------------------------------------------------- progress
    async def _emit(
        self, phase: ProgressPhase, message: str = "", **fields: Any
    ) -> None:
        await emit(
            self._progress,
            ProgressEvent(
                phase=phase, message=message, platform=str(self.platform), **fields
            ),
        )

    # --------------------------------------------------------------- quality
    def quality_spec(
        self,
        quality: "str | int | QualitySpec | None" = None,
        *,
        container: Optional[str] = None,
        codec: Optional[str] = None,
        audio_bitrate: Optional[int] = None,
    ) -> QualitySpec:
        """Resolve ``quality`` against the config defaults."""
        if isinstance(quality, QualitySpec):
            return quality
        return parse_quality(
            self.config.default_quality if quality is None else quality,
            container=container or self.config.prefer_container,
            codec=codec or self.config.prefer_video_codec,
            audio_codec=self.config.prefer_audio_codec,
            audio_bitrate=audio_bitrate or self.config.audio_bitrate_preference,
        )

    # -------------------------------------------------------------- download
    async def download(
        self,
        metadata: PostMetadata,
        *,
        quality: "str | int | QualitySpec | None" = None,
        target: "DownloadTarget | str" = DownloadTarget.MEMORY,
        dest: Optional["str | Path"] = None,
        pattern: Optional[str] = None,
        progress: Optional[Any] = None,
        only: Optional[Sequence[int]] = None,
        include_audio: Optional[bool] = None,
        max_size_bytes: Optional[int] = None,
        temp_dir: Optional["str | Path"] = None,
        container: Optional[str] = None,
        codec: Optional[str] = None,
    ) -> DownloadResult:
        """Download the media described by ``metadata``.

        ``metadata`` must come from :meth:`get_metadata` on the same client
        class (the platform payload is reused to avoid a second round trip).
        """
        spec = self.quality_spec(quality, container=container, codec=codec)
        entries = build_plan(
            metadata.items,
            spec,
            include_audio=self.config.mux_audio if include_audio is None else include_audio,
            only=only,
        )
        limit = max_size_bytes if max_size_bytes is not None else self.config.max_size_bytes
        engine = self._engine
        temporary_engine = False
        if limit != engine.max_size_bytes:
            engine = DownloadEngine(
                self._http,
                platform=str(self.platform),
                max_concurrency=self.config.max_download_concurrency,
                max_size_bytes=limit,
                use_ffmpeg=self.config.mux_audio,
                verify_audio=self.config.verify_audio,
            )
            temporary_engine = True
        started = time.perf_counter()
        outcome = await engine.run(
            entries, target=target, temp_dir=temp_dir, progress=progress or self._progress
        )
        result = DownloadResult(
            metadata=metadata,
            files=outcome.files,
            quality=spec.label,
            elapsed=time.perf_counter() - started,
            errors=outcome.errors,
            warnings=list(metadata.warnings) + list(outcome.warnings),
        )
        if dest is not None:
            await result.save(dest, pattern=pattern or self.config.default_pattern)
        if temporary_engine:
            # the one-off engine is not tracked by ``close``; prune its scratch
            # directory now (kept result files are protected by the engine)
            engine.cleanup()
        await self._emit(ProgressPhase.DONE, result.summary().splitlines()[0])
        return result

    # ------------------------------------------------------------------ save
    async def save(
        self,
        result: "DownloadResult | PostMetadata",
        dest: "str | Path",
        *,
        pattern: Optional[str] = None,
        overwrite: bool = False,
        album_dir: bool = True,
    ) -> list[Path]:
        """Write a download result (or just its metadata json) to ``dest``."""
        if isinstance(result, PostMetadata):
            return [await save_metadata(result, dest, overwrite=overwrite)]
        return await save_result(
            result,
            dest,
            pattern=pattern or self.config.default_pattern,
            overwrite=overwrite,
            album_dir=album_dir,
        )

    # ------------------------------------------------------------- probing
    async def probe_format_sizes(
        self,
        formats: Sequence[MediaFormat],
        *,
        only_missing: bool = True,
        concurrency: int = 6,
    ) -> dict[str, Optional[int]]:
        """Fill in missing format sizes with ``HEAD``/ranged ``GET`` probes."""
        targets = [
            fmt
            for fmt in formats
            if not fmt.is_manifest
            and (fmt.size_bytes is None or not only_missing)
            and fmt.url
        ]
        if not targets:
            return {fmt.format_id: fmt.size_bytes for fmt in formats}
        by_url: dict[str, list[MediaFormat]] = {}
        for fmt in targets:
            by_url.setdefault(fmt.url, []).append(fmt)
        await self._emit(
            ProgressPhase.PROBING, f"probing {len(by_url)} url(s) for sizes"
        )
        results = await self._http.probe_sizes(
            list(by_url), concurrency=concurrency
        )
        for url, (size, mime) in results.items():
            for fmt in by_url.get(url, ()):
                if size is not None and (fmt.size_bytes is None or not only_missing):
                    fmt.size_bytes = size
                    fmt.size_source = "head"
                    fmt.size_is_approx = False
                if mime and not fmt.mime_type:
                    fmt.mime_type = mime.split(";")[0].strip()
        return {fmt.format_id: fmt.size_bytes for fmt in formats}

    # ------------------------------------------------------------- helpers
    @staticmethod
    def _require_media(metadata: PostMetadata) -> None:
        if not metadata.items:
            raise MediaNotAvailableError("the post carries no downloadable media")