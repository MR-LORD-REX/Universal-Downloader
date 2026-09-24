"""The shared download engine: streaming, resuming, muxing and size guards.

Every platform client funnels its resolved plan through :class:`DownloadEngine`
so behaviour (progress events, ``max_size_bytes`` guard, ffmpeg muxing, memory
vs disk targets) is identical across Reddit, YouTube and Twitter.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

from . import ffmpeg
from .enums import DownloadTarget, FormatKind, MediaKind
from .exceptions import (
    DownloadError,
    DownloaderError,
    MuxingError,
    SizeLimitExceededError,
)
from .http import HttpClient
from .models import DownloadedFile, MediaFormat, MediaItem
from .progress import ProgressEvent, ProgressPhase, emit
from .select import PlanEntry
from .urls import extension_of, mime_for_extension

_SCRATCH_PREFIX = "sdlsdk-"


@dataclass(slots=True)
class DownloadOutcome:
    """Raw result of :meth:`DownloadEngine.run`."""

    files: list[DownloadedFile] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


class DownloadEngine:
    """Fetches a :class:`PlanEntry` list concurrently, muxing when needed."""

    def __init__(
        self,
        http: HttpClient,
        *,
        platform: str = "sdk",
        max_concurrency: int = 4,
        max_size_bytes: Optional[int] = None,
        use_ffmpeg: bool = True,
        verify_audio: bool = True,
        ranged_download: Optional[bool] = None,
        range_chunk_size: Optional[int] = None,
    ) -> None:
        self.http = http
        self.platform = platform
        self.max_concurrency = max(1, max_concurrency)
        self.max_size_bytes = max_size_bytes
        self.use_ffmpeg = use_ffmpeg
        self.verify_audio = verify_audio
        self.ranged_download = (
            http.options.ranged_download if ranged_download is None else ranged_download
        )
        self.range_chunk_size = (
            http.options.range_chunk_size if range_chunk_size is None else range_chunk_size
        )
        self._workdirs: list[Path] = []
        self._keep: set[Path] = set()

    # ------------------------------------------------------------------ run
    async def run(
        self,
        entries: Sequence[PlanEntry],
        *,
        target: "DownloadTarget | str" = DownloadTarget.MEMORY,
        temp_dir: Optional["str | Path"] = None,
        progress: Optional[Any] = None,
    ) -> DownloadOutcome:
        """Download every entry, collecting per item errors instead of raising."""
        target = DownloadTarget(target)
        outcome = DownloadOutcome()
        if not entries:
            return outcome
        semaphore = asyncio.Semaphore(self.max_concurrency)
        workdir = self._work_directory(temp_dir)
        total = len(entries)

        async def run(entry: PlanEntry) -> None:
            async with semaphore:
                try:
                    outcome.files.append(
                        await self._download_entry(
                            entry, target=target, workdir=workdir, progress=progress, items_total=total
                        )
                    )
                except DownloaderError as exc:
                    outcome.errors[f"item[{entry.item.index}]"] = str(exc)
                except Exception as exc:  # pragma: no cover - defensive
                    outcome.errors[f"item[{entry.item.index}]"] = f"{type(exc).__name__}: {exc}"

        await asyncio.gather(*(run(entry) for entry in entries))
        outcome.files.sort(key=lambda f: (f.item_index, f.kind.value))
        # Remember the files handed back to the caller so cleanup() never
        # deletes a ``target="disk"`` result that has not been saved yet.
        for file in outcome.files:
            if file.path is not None:
                self._keep.add(file.path)
        await self._audit_audio(entries, outcome)
        return outcome

    def cleanup(self) -> int:
        """Delete leftover scratch files and prune empty work directories.

        Files that were handed back in a :class:`DownloadedFile` are kept, so a
        ``target="disk"`` download without a ``dest`` survives closing the
        client; everything else (mux inputs, discarded temporaries) is removed
        and empty directories are pruned.
        """
        removed = 0
        for directory in list(self._workdirs):
            if not directory.exists():
                self._workdirs.remove(directory)
                continue
            for path in directory.iterdir():
                if path.is_file() and path not in self._keep:
                    path.unlink(missing_ok=True)
                    removed += 1
            try:
                directory.rmdir()
            except OSError:  # pragma: no cover - still holds a result
                continue
            self._workdirs.remove(directory)
        return removed

    def _work_directory(self, temp_dir: Optional["str | Path"]) -> Path:
        if temp_dir is not None:
            directory = Path(temp_dir)
            directory.mkdir(parents=True, exist_ok=True)
            return directory
        import tempfile

        directory = Path(tempfile.mkdtemp(prefix=f"{self.platform}-{_SCRATCH_PREFIX}"))
        self._workdirs.append(directory)
        return directory

    # -------------------------------------------------------------- entries
    async def _download_entry(
        self,
        entry: PlanEntry,
        *,
        target: DownloadTarget,
        workdir: Path,
        progress: Optional[Any],
        items_total: int,
    ) -> DownloadedFile:
        declared = entry.total_size
        if self.max_size_bytes is not None and declared is not None and declared > self.max_size_bytes:
            raise SizeLimitExceededError(declared, self.max_size_bytes)

        primary = entry.primary
        # Muxing takes priority over the manifest shortcut: an HLS/DASH video
        # rendition still needs its separate audio track merged in, otherwise
        # the caller gets a silent file.
        if entry.needs_mux and self.use_ffmpeg:
            return await self._download_and_mux(
                entry, target=target, workdir=workdir, progress=progress, items_total=items_total
            )
        if primary.is_manifest:
            return await self._download_manifest(
                entry, target=target, workdir=workdir, progress=progress, items_total=items_total
            )
        return await self._fetch(
            primary,
            entry=entry,
            target=target,
            workdir=workdir,
            progress=progress,
            items_total=items_total,
            kind=_kind_for(primary),
        )

    async def _fetch(
        self,
        fmt: MediaFormat,
        *,
        entry: PlanEntry,
        target: DownloadTarget,
        workdir: Path,
        progress: Optional[Any],
        items_total: int,
        kind: MediaKind,
        filename: Optional[str] = None,
    ) -> DownloadedFile:
        extension = fmt.extension or extension_of(fmt.url) or "bin"
        name = filename or self._filename(entry, fmt, extension)
        started = time.perf_counter()
        ticker = _Ticker()
        in_memory = target == DownloadTarget.MEMORY
        path = (
            None
            if in_memory
            else _reserve_file(workdir, name, prefix=f"{self.platform}-", extension=f".{extension}")
        )
        handle = None if in_memory else path.open("wb")  # type: ignore[union-attr]
        buffer = bytearray() if in_memory else None
        state = {"written": 0, "total": fmt.size_bytes}

        async def consume(chunks: Any, total: Optional[int]) -> None:
            if total:
                state["total"] = total
            await ticker.report(
                progress, state["total"], state["written"], entry, name, items_total, self.platform
            )
            async for chunk in chunks:
                if handle is not None:
                    handle.write(chunk)
                else:
                    buffer.extend(chunk)  # type: ignore[union-attr]
                state["written"] += len(chunk)
                self._guard(state["written"])
                await ticker.report(
                    progress,
                    state["total"],
                    state["written"],
                    entry,
                    name,
                    items_total,
                    self.platform,
                )

        try:
            if self._use_ranges(fmt):
                await self._consume_ranges(fmt, consume)
            else:
                async with self.http.open(fmt.url, headers=fmt.http_headers or None) as (
                    info,
                    chunks,
                ):
                    await consume(chunks, info.size)
        finally:
            if handle is not None:
                handle.close()

        written = state["written"]
        await self._finish(progress, entry, name, items_total, self.platform, written)
        return DownloadedFile(
            item_index=entry.item.index,
            kind=kind,
            filename=name,
            mime_type=fmt.mime_type or mime_for_extension(extension),
            format=fmt,
            path=path,
            data=bytes(buffer) if buffer is not None else None,
            downloaded=written,
            elapsed=time.perf_counter() - started,
            note=None if in_memory else "temp",
        )

    def _use_ranges(self, fmt: MediaFormat) -> bool:
        """``True`` when this format should be fetched as bounded byte ranges."""
        if not self.ranged_download or self.range_chunk_size <= 0:
            return False
        return bool(fmt.size_bytes) and fmt.size_bytes > 0

    async def _consume_ranges(self, fmt: MediaFormat, consume: Any) -> None:
        """Walk ``fmt`` in ``range_chunk_size`` slices to dodge CDN throttling."""
        total = int(fmt.size_bytes or 0)
        size = self.range_chunk_size
        start = 0
        while start < total:
            end = min(start + size, total) - 1
            headers = dict(fmt.http_headers or {})
            headers["Range"] = f"bytes={start}-{end}"
            async with self.http.open(fmt.url, headers=headers, retries=0) as (info, chunks):
                await consume(chunks, info.size or (end + 1))
            start = end + 1

    async def _download_and_mux(
        self,
        entry: PlanEntry,
        *,
        target: DownloadTarget,
        workdir: Path,
        progress: Optional[Any],
        items_total: int,
    ) -> DownloadedFile:
        assert entry.audio is not None  # guarded by PlanEntry.needs_mux
        video_file = await self._track(
            entry.primary,
            entry=entry,
            workdir=workdir,
            progress=progress,
            items_total=items_total,
            kind=MediaKind.VIDEO,
        )
        audio_file = await self._track(
            entry.audio,
            entry=entry,
            workdir=workdir,
            progress=progress,
            items_total=items_total,
            kind=MediaKind.AUDIO,
        )
        return await self._finish_mux(
            entry,
            video_file,
            audio_file,
            target=target,
            workdir=workdir,
            progress=progress,
            items_total=items_total,
        )

    async def _track(
        self,
        fmt: MediaFormat,
        *,
        entry: PlanEntry,
        workdir: Path,
        progress: Optional[Any],
        items_total: int,
        kind: MediaKind,
    ) -> DownloadedFile:
        """Fetch one track to disk, following a manifest when the url is one.

        Audio is sometimes only published as its own HLS playlist (Twitter), so
        a track cannot just be streamed as bytes when ``fmt`` is a manifest.
        """
        suffix = "_v" if kind is MediaKind.VIDEO else "_a"
        if fmt.is_manifest:
            return await self._download_manifest(
                PlanEntry(item=entry.item, primary=fmt, audio=None, position=entry.position),
                target=DownloadTarget.DISK,
                workdir=workdir,
                progress=progress,
                items_total=items_total,
                suffix=suffix,
            )
        return await self._fetch(
            fmt,
            entry=entry,
            target=DownloadTarget.DISK,
            workdir=workdir,
            progress=progress,
            items_total=items_total,
            kind=kind,
            filename=self._filename(
                entry, fmt, fmt.extension or "bin", suffix=suffix
            ),
        )

    async def _finish_mux(
        self,
        entry: PlanEntry,
        video_file: DownloadedFile,
        audio_file: DownloadedFile,
        *,
        target: DownloadTarget,
        workdir: Path,
        progress: Optional[Any],
        items_total: int,
    ) -> DownloadedFile:
        video = entry.primary
        audio = entry.audio
        assert audio is not None
        assert video_file.path is not None and audio_file.path is not None
        video_ext = video_file.extension or video.extension or "mp4"
        audio_ext = audio_file.extension or audio.extension or "m4a"
        started = time.perf_counter()
        container = _mux_container(video_ext, audio_ext)
        output = _reserve_file(
            workdir,
            self._filename(entry, video, container),
            prefix=f"{self.platform}-mux-",
            extension=f".{container}",
        )
        await emit(
            progress,
            ProgressEvent(
                phase=ProgressPhase.MUXING,
                platform=self.platform,
                item_index=entry.item.index,
                item_total=items_total,
                filename=output.name,
                message=f"muxing {video_ext}+{audio_ext} -> {container}",
            ),
        )
        try:
            await asyncio.to_thread(
                ffmpeg.mux, video_file.path, audio_file.path, output, container=container
            )
        finally:
            _discard(video_file.path)
            _discard(audio_file.path)
        size = output.stat().st_size
        data: Optional[bytes] = None
        path: Optional[Path] = output
        if target == DownloadTarget.MEMORY:
            data = await asyncio.to_thread(output.read_bytes)
            _discard(output)
            path = None
        name = self._filename(entry, video, container)
        return DownloadedFile(
            item_index=entry.item.index,
            kind=MediaKind.VIDEO,
            filename=name,
            mime_type="video/mp4" if container == "mp4" else f"video/{container}",
            format=video,
            path=path,
            data=data,
            downloaded=size,
            elapsed=time.perf_counter() - started,
            muxed=True,
            note=None if data is None else "memory",
        )

    def _silent_reason(self, entry: PlanEntry) -> str:
        if entry.audio is not None and not self.use_ffmpeg:
            return "muxing is disabled (mux_audio=False)"
        if entry.audio is None:
            return "no audio rendition was available"
        return "the selected rendition is video-only"

    async def _audit_audio(
        self, entries: Sequence[PlanEntry], outcome: DownloadOutcome
    ) -> None:
        """Warn when a video that should have sound comes out silent.

        This is the safety net behind the user visible symptom "the downloaded
        video has no audio": a rendition may be video-only with no counterpart,
        muxing may be off, or the CDN file itself may simply be silent (a
        Twitter ``gif``, a muted upload). Whenever the item claims audio the
        produced file is verified with ffprobe on disk.
        """
        by_index = {entry.item.index: entry for entry in entries}
        for file in outcome.files:
            if file.kind is not MediaKind.VIDEO or file.muxed:
                continue
            entry = by_index.get(file.item_index)
            if entry is None or entry.item.has_audio is False:
                continue
            if self.verify_audio and file.path is not None and ffmpeg.ffmpeg_available():
                try:
                    info = await asyncio.to_thread(ffmpeg.probe, file.path)
                except Exception:  # pragma: no cover - probe is best effort
                    info = None
                if info is not None:
                    if info.has_audio:
                        continue
                    outcome.warnings.append(
                        f"item[{file.item_index}] was downloaded without audio "
                        "(the file has no audio stream); it will play silently"
                    )
                    continue
            declared = file.format is not None and file.format.has_audio is True
            if declared:
                continue
            outcome.warnings.append(
                f"item[{file.item_index}] was downloaded without audio "
                f"({self._silent_reason(entry)}); it will play silently"
            )

    async def _download_manifest(
        self,
        entry: PlanEntry,
        *,
        target: DownloadTarget,
        workdir: Path,
        progress: Optional[Any],
        items_total: int,
        suffix: str = "",
    ) -> DownloadedFile:
        fmt = entry.primary
        started = time.perf_counter()
        container = "mp4" if (fmt.extension or "mp4") in ("mp4", "m4v", "ts", "bin") else fmt.extension or "mp4"
        output = _reserve_file(
            workdir,
            self._filename(entry, fmt, container, suffix=suffix),
            prefix=f"{self.platform}-hls-",
            extension=f".{container}",
        )
        await emit(
            progress,
            ProgressEvent(
                phase=ProgressPhase.DOWNLOADING,
                platform=self.platform,
                item_index=entry.item.index,
                item_total=items_total,
                filename=output.name,
                message=f"fetching manifest ({fmt.protocol})",
            ),
        )
        await asyncio.to_thread(
            ffmpeg.download_manifest,
            fmt.url,
            output,
            container=container,
            headers=fmt.http_headers or None,
        )
        size = output.stat().st_size
        data: Optional[bytes] = None
        path: Optional[Path] = output
        if target == DownloadTarget.MEMORY:
            data = await asyncio.to_thread(output.read_bytes)
            _discard(output)
            path = None
        return DownloadedFile(
            item_index=entry.item.index,
            kind=_kind_for(fmt),
            filename=self._filename(entry, fmt, container, suffix=suffix),
            mime_type=fmt.mime_type or mime_for_extension(container),
            format=fmt,
            path=path,
            data=data,
            downloaded=size,
            elapsed=time.perf_counter() - started,
            note="temp" if path is not None else None,
        )

    # -------------------------------------------------------------- helpers
    def _filename(
        self,
        entry: PlanEntry,
        fmt: MediaFormat,
        extension: str,
        *,
        suffix: str = "",
    ) -> str:
        quality = (fmt.quality_label or _quality_label(fmt) or "media").replace("/", "-")
        stem = _slug(f"{self.platform}_{entry.item.index + 1}_{quality}") + suffix
        return f"{stem}.{extension}"

    def _guard(self, written: int) -> None:
        if self.max_size_bytes is not None and written > self.max_size_bytes:
            raise SizeLimitExceededError(written, self.max_size_bytes)

    async def _finish(
        self,
        progress: Optional[Any],
        entry: PlanEntry,
        name: str,
        items_total: int,
        platform: str,
        size: int,
    ) -> None:
        await emit(
            progress,
            ProgressEvent(
                phase=ProgressPhase.DOWNLOADING,
                platform=platform,
                item_index=entry.item.index,
                item_total=items_total,
                filename=name,
                downloaded=size,
                total=size,
                message=f"done {name}",
            ),
        )


class _Ticker:
    """Throttles progress callbacks to ~3 per second (or every 512 KB)."""

    __slots__ = ("_last_time", "_last_bytes")

    def __init__(self) -> None:
        self._last_time = 0.0
        self._last_bytes = 0

    async def report(
        self,
        progress: Optional[Any],
        total: Optional[int],
        downloaded: int,
        entry: PlanEntry,
        filename: str,
        items_total: int,
        platform: str,
    ) -> None:
        if progress is None:
            return
        now = time.perf_counter()
        finished = total is not None and downloaded >= total
        if not finished and now - self._last_time < 0.35 and downloaded - self._last_bytes < 512 * 1024:
            return
        self._last_time = now
        self._last_bytes = downloaded
        await emit(
            progress,
            ProgressEvent(
                phase=ProgressPhase.DOWNLOADING,
                platform=platform,
                item_index=entry.item.index,
                item_total=items_total,
                filename=filename,
                downloaded=downloaded,
                total=total,
                message=f"{entry.position + 1}/{items_total} {filename}",
            ),
        )


def _kind_for(fmt: MediaFormat) -> MediaKind:
    return {
        FormatKind.VIDEO: MediaKind.VIDEO,
        FormatKind.MUXED: MediaKind.VIDEO,
        FormatKind.AUDIO: MediaKind.AUDIO,
        FormatKind.GIF: MediaKind.GIF,
        FormatKind.IMAGE: MediaKind.IMAGE,
        FormatKind.SUBTITLE: MediaKind.SUBTITLE,
    }.get(fmt.kind, MediaKind.UNKNOWN)


def _quality_label(fmt: MediaFormat) -> Optional[str]:
    if fmt.kind == FormatKind.AUDIO:
        return f"audio{fmt.bitrate_kbps}" if fmt.bitrate_kbps else "audio"
    height = fmt.quality_height or fmt.height
    if height:
        return f"{height}p"
    return None


def _mux_container(video_ext: str, audio_ext: str) -> str:
    if video_ext in ("webm", "mkv") and audio_ext in ("webm", "opus", "ogg", "oga", "mkv"):
        return "webm"
    if video_ext in ("mp4", "m4v", "mov") and audio_ext in ("mp4", "m4a", "aac", "mov"):
        return "mp4"
    if video_ext == "mp4" and audio_ext == "m4a":
        return "mp4"
    return "mkv"


def _reserve_file(directory: Path, filename: str, *, prefix: str, extension: str) -> Path:
    """Create an empty, uniquely named file inside ``directory``.

    The requested ``filename`` is honoured when it is free so a
    ``target="disk"`` result keeps a readable name; on collision a numeric
    suffix is added. ``prefix`` is only used if the name cannot be used at all.
    """
    import tempfile

    directory.mkdir(parents=True, exist_ok=True)
    suffix = extension if extension.startswith(".") else f".{extension}"
    if filename:
        stem = Path(filename).stem or "media"
        candidate = directory / f"{stem}{suffix}"
        counter = 1
        while True:
            try:
                candidate.touch(exist_ok=False)
                return candidate
            except FileExistsError:
                candidate = directory / f"{stem}_{counter}{suffix}"
                counter += 1
            except OSError:  # pragma: no cover - unusable name, fall back
                break
    handle = tempfile.NamedTemporaryFile(
        delete=False, dir=str(directory), prefix=prefix, suffix=suffix
    )
    handle.close()
    return Path(handle.name)


def _discard(path: Optional[Path]) -> None:
    if path is not None:
        path.unlink(missing_ok=True)


def _slug(value: str) -> str:
    keep = [char if (char.isalnum() or char in ("-", "_", ".")) else "_" for char in value]
    return "".join(keep).strip("._")[:120] or "media"