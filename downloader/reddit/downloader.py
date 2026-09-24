"""Streaming download engine with muxing and HLS support."""

from __future__ import annotations

import asyncio
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

from .config import RedditConfig
from .exceptions import DownloadError, MuxingError, RedditError
from .http import HttpClient
from .manifests import parse_hls_master, parse_hls_media
from .models import (
    DownloadTarget,
    DownloadedFile,
    FormatKind,
    FormatOrigin,
    MediaFormat,
    MediaItem,
    MediaKind,
    PostMetadata,
    ProgressEvent,
    ProgressPhase,
)
from .models.progress import emit
from .urls import media_extension


@dataclass(slots=True)
class DownloadPlanEntry:
    """Everything needed to fetch one medium."""

    item: MediaItem
    primary: MediaFormat
    audio: Optional[MediaFormat] = None
    position: int = 0


@dataclass(slots=True)
class DownloadOutcome:
    """Raw result of :meth:`Downloader.download`."""

    files: list[DownloadedFile] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


class Downloader:
    """Fetches media formats concurrently and muxes audio when needed."""

    def __init__(self, http: HttpClient, config: Optional[RedditConfig] = None) -> None:
        self.http = http
        self.config = config or http.config
        self._workdirs: list[Path] = []

    async def download(
        self,
        metadata: PostMetadata,
        *,
        quality: str | int = "best",
        include_audio: bool = True,
        mux: Optional[bool] = None,
        target: DownloadTarget | str = DownloadTarget.MEMORY,
        temp_dir: Optional[str | Path] = None,
        progress: Optional[Any] = None,
        only: Optional[Sequence[int]] = None,
    ) -> DownloadOutcome:
        """Download every media item of ``metadata``.

        ``target`` is ``"memory"`` (bytes in ``DownloadedFile.data``) or
        ``"disk"`` (streamed to managed temp files that :func:`save` moves into
        place). ``only`` restricts the download to specific item indexes.
        """
        target = DownloadTarget(target)
        mux = self.config.mux_audio if mux is None else mux
        plan = metadata.select_all(quality, include_audio=include_audio)
        if only is not None:
            wanted = set(only)
            plan = [entry for entry in plan if entry[0].index in wanted]

        outcome = DownloadOutcome()
        if not plan:
            return outcome

        entries = [
            DownloadPlanEntry(item=item, primary=primary, audio=audio, position=position)
            for position, (item, primary, audio) in enumerate(plan)
        ]
        semaphore = asyncio.Semaphore(max(1, self.config.max_download_concurrency))
        total = len(entries)

        async def run(entry: DownloadPlanEntry, work_dir: Optional[Path]) -> None:
            async with semaphore:
                try:
                    files = await self._download_entry(
                        entry,
                        metadata=metadata,
                        mux=mux,
                        target=target,
                        temp_dir=work_dir,
                        progress=progress,
                        total=total,
                    )
                    outcome.files.extend(files)
                except RedditError as exc:
                    outcome.errors[f"item[{entry.item.index}]"] = str(exc)
                except Exception as exc:  # pragma: no cover - defensive
                    outcome.errors[f"item[{entry.item.index}]"] = f"{type(exc).__name__}: {exc}"

        work_dir = self._work_directory(temp_dir)
        await asyncio.gather(*(run(entry, work_dir) for entry in entries))

        outcome.files.sort(key=lambda f: (f.item_index, f.kind.value))
        return outcome

    def _work_directory(self, temp_dir: Optional[str | Path]) -> Path:
        """Scratch dir for streamed downloads.

        A user supplied directory is used as-is; otherwise a private temp dir is
        created and kept until :meth:`cleanup` runs (files stay available for
        :func:`save`, which moves them into place).
        """
        if temp_dir is not None:
            directory = Path(temp_dir)
            directory.mkdir(parents=True, exist_ok=True)
            return directory
        directory = Path(tempfile.mkdtemp(prefix="reddit-sdk-"))
        self._workdirs.append(directory)
        return directory

    def cleanup(self) -> int:
        """Delete leftover scratch files (anything already saved is untouched)."""
        removed = 0
        for directory in list(self._workdirs):
            if not directory.exists():
                continue
            for path in directory.iterdir():
                if path.is_file() and path.name.startswith(_SCRATCH_PREFIXES):
                    path.unlink(missing_ok=True)
                    removed += 1
            try:
                directory.rmdir()
            except OSError:
                pass
        self._workdirs.clear()
        return removed

    # ------------------------------------------------------------------ entry
    async def _download_entry(
        self,
        entry: DownloadPlanEntry,
        *,
        metadata: PostMetadata,
        mux: bool,
        target: DownloadTarget,
        temp_dir: Optional[Path],
        progress: Optional[Any],
        total: int,
    ) -> list[DownloadedFile]:
        item = entry.item
        primary = entry.primary
        base_name = _filename(metadata, item, primary, index=entry.position)

        video_file = await self._fetch_format(
            primary,
            item_index=item.index,
            filename=base_name,
            target=target,
            temp_dir=temp_dir,
            progress=progress,
            position=entry.position,
            total=total,
        )

        audio_file: Optional[DownloadedFile] = None
        if entry.audio is not None:
            audio_name = f"{Path(base_name).stem}_audio.{entry.audio.extension or 'm4a'}"
            audio_file = await self._fetch_format(
                entry.audio,
                item_index=item.index,
                filename=audio_name,
                target=target,
                temp_dir=temp_dir,
                progress=progress,
                position=entry.position,
                total=total,
            )

        if audio_file is None:
            return [video_file]

        if not mux:
            video_file.note = "audio downloaded separately (muxing disabled)"
            return [video_file, audio_file]

        try:
            muxed = await self._mux(video_file, audio_file, item, base_name, target, temp_dir)
        except MuxingError as exc:
            video_file.note = f"audio not muxed ({exc}); audio kept as a separate file"
            return [video_file, audio_file]
        video_file.free()
        audio_file.free()
        return [muxed]

    async def _fetch_format(
        self,
        fmt: MediaFormat,
        *,
        item_index: int,
        filename: str,
        target: DownloadTarget,
        temp_dir: Optional[Path],
        progress: Optional[Any],
        position: int,
        total: int,
    ) -> DownloadedFile:
        if (fmt.container or "") == "m3u8" or (fmt.extension or "") == "m3u8":
            return await self._fetch_hls(
                fmt,
                item_index=item_index,
                filename=filename,
                target=target,
                temp_dir=temp_dir,
                progress=progress,
                position=position,
                total=total,
            )
        kind = MediaKind.VIDEO if fmt.kind == FormatKind.MUXED else _kind_for(fmt)
        if target == DownloadTarget.MEMORY:
            data, elapsed = await self._fetch_bytes(
                fmt,
                item_index=item_index,
                filename=filename,
                progress=progress,
                position=position,
                total=total,
            )
            return DownloadedFile(
                item_index=item_index,
                kind=kind,
                filename=filename,
                mime_type=fmt.mime_type,
                format=fmt,
                data=data,
                downloaded=len(data),
                elapsed=elapsed,
            )
        path, elapsed, size = await self._fetch_file(
            fmt,
            item_index=item_index,
            filename=filename,
            temp_dir=temp_dir,
            progress=progress,
            position=position,
            total=total,
        )
        return DownloadedFile(
            item_index=item_index,
            kind=kind,
            filename=filename,
            mime_type=fmt.mime_type,
            format=fmt,
            path=path,
            downloaded=size,
            elapsed=elapsed,
            note="temp",
        )

    async def _fetch_bytes(
        self,
        fmt: MediaFormat,
        *,
        item_index: int,
        filename: str,
        progress: Optional[Any],
        position: int,
        total: int,
    ) -> tuple[bytes, float]:
        started = time.perf_counter()
        buffer = bytearray()
        tick = _Ticker()
        async with self.http.open(fmt.url, headers=_headers_for(fmt)) as (info, chunks):
            async for chunk in chunks:
                buffer.extend(chunk)
                await tick.report(progress, info.size, len(buffer), item_index, filename, position, total)
        return bytes(buffer), time.perf_counter() - started

    async def _fetch_file(
        self,
        fmt: MediaFormat,
        *,
        item_index: int,
        filename: str,
        temp_dir: Optional[Path],
        progress: Optional[Any],
        position: int,
        total: int,
    ) -> tuple[Path, float, int]:
        started = time.perf_counter()
        path = _reserve_file(temp_dir, filename, prefix="reddit-dl-")
        written = 0
        tick = _Ticker()
        try:
            with open(path, "wb") as sink:
                async with self.http.open(fmt.url, headers=_headers_for(fmt)) as (info, chunks):
                    async for chunk in chunks:
                        sink.write(chunk)
                        written += len(chunk)
                        await tick.report(
                            progress, info.size, written, item_index, filename, position, total
                        )
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return path, time.perf_counter() - started, written

    async def _fetch_hls(
        self,
        fmt: MediaFormat,
        *,
        item_index: int,
        filename: str,
        target: DownloadTarget,
        temp_dir: Optional[Path],
        progress: Optional[Any],
        position: int,
        total: int,
    ) -> DownloadedFile:
        """Download every segment of an HLS rendition and concatenate it."""
        manifest = await self.http.get_text(fmt.url, retries=1)
        media = parse_hls_media(manifest, fmt.url)
        if media.is_master:
            variants = parse_hls_master(manifest, fmt.url)
            if not variants:
                raise DownloadError(f"no HLS variants in {fmt.url}")
            best = max(variants, key=lambda v: (v.height or 0, v.bandwidth or 0))
            fmt = fmt.model_copy(
                update={"url": best.url, "width": best.width, "height": best.height}
            )
            manifest = await self.http.get_text(fmt.url, retries=1)
            media = parse_hls_media(manifest, fmt.url)
        if not media.segments:
            raise DownloadError(f"HLS playlist {fmt.url} has no segments")

        urls = ([media.init_segment] if media.init_segment else []) + media.segments
        semaphore = asyncio.Semaphore(max(2, self.config.max_download_concurrency * 2))
        started = time.perf_counter()
        tick = _Ticker()

        async def segment(url: str) -> bytes:
            async with semaphore:
                response = await self.http.request("GET", url, retries=2)
                if not response.ok:
                    raise DownloadError(f"HTTP {response.status} for segment {url}")
                return response.content

        buffer = bytearray()
        for chunk in await asyncio.gather(*(segment(url) for url in urls)):
            buffer.extend(chunk)
            await tick.report(progress, None, len(buffer), item_index, filename, position, total)

        elapsed = time.perf_counter() - started
        payload = bytes(buffer)
        if target == DownloadTarget.MEMORY:
            return DownloadedFile(
                item_index=item_index,
                kind=MediaKind.VIDEO,
                filename=filename,
                mime_type="video/mp4",
                format=fmt,
                data=payload,
                downloaded=len(payload),
                elapsed=elapsed,
                note="assembled from HLS segments",
            )
        path = _reserve_file(temp_dir, filename, prefix="reddit-hls-")
        path.write_bytes(payload)
        return DownloadedFile(
            item_index=item_index,
            kind=MediaKind.VIDEO,
            filename=filename,
            mime_type="video/mp4",
            format=fmt,
            path=path,
            downloaded=len(payload),
            elapsed=elapsed,
            note="temp",
        )

    async def _mux(
        self,
        video: DownloadedFile,
        audio: DownloadedFile,
        item: MediaItem,
        filename: str,
        target: DownloadTarget,
        temp_dir: Optional[Path],
    ) -> DownloadedFile:
        """Combine a video-only stream with its audio track via ffmpeg."""
        await emit(
            None,
            ProgressEvent(
                phase=ProgressPhase.MUXING, message=f"muxing {filename}", item_index=item.index
            ),
        )
        started = time.perf_counter()
        video_path = await asyncio.to_thread(_materialise, video)
        audio_path = await asyncio.to_thread(_materialise, audio)
        muxed_path = _reserve_file(temp_dir, filename, prefix="reddit-mux-", extension=".mp4")
        try:
            await asyncio.to_thread(_run_ffmpeg, video_path, audio_path, muxed_path)
        except MuxingError:
            muxed_path.unlink(missing_ok=True)
            raise
        elapsed = time.perf_counter() - started
        size = muxed_path.stat().st_size
        if target == DownloadTarget.MEMORY:
            data = muxed_path.read_bytes()
            muxed_path.unlink(missing_ok=True)
            return DownloadedFile(
                item_index=item.index,
                kind=MediaKind.VIDEO,
                filename=filename,
                mime_type="video/mp4",
                format=video.format,
                data=data,
                downloaded=len(data),
                elapsed=elapsed,
                muxed=True,
                note="video+audio muxed with ffmpeg",
            )
        return DownloadedFile(
            item_index=item.index,
            kind=MediaKind.VIDEO,
            filename=filename,
            mime_type="video/mp4",
            format=video.format,
            path=muxed_path,
            downloaded=size,
            elapsed=elapsed,
            muxed=True,
            note="temp",
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
        item_index: int,
        filename: str,
        position: int,
        items_total: int,
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
                item_index=item_index,
                item_total=items_total,
                filename=filename,
                downloaded=downloaded,
                total=total,
                message=f"{position + 1}/{items_total} {filename}",
            ),
        )


_SCRATCH_PREFIXES = ("reddit-dl-", "reddit-hls-", "reddit-mux-", "reddit-mem-")


def _kind_for(fmt: MediaFormat) -> MediaKind:
    return {
        FormatKind.VIDEO: MediaKind.VIDEO,
        FormatKind.MUXED: MediaKind.VIDEO,
        FormatKind.AUDIO: MediaKind.AUDIO,
        FormatKind.GIF: MediaKind.GIF,
        FormatKind.IMAGE: MediaKind.IMAGE,
    }.get(fmt.kind, MediaKind.UNKNOWN)


def _reserve_file(
    directory: Optional[Path],
    filename: str,
    *,
    prefix: str = "reddit-",
    extension: Optional[str] = None,
) -> Path:
    """Create an empty, uniquely named file and return its path."""
    base = Path(directory) if directory else Path(tempfile.gettempdir())
    base.mkdir(parents=True, exist_ok=True)
    suffix = extension or Path(filename).suffix or ".bin"
    handle = tempfile.NamedTemporaryFile(delete=False, dir=str(base), prefix=prefix, suffix=suffix)
    handle.close()
    return Path(handle.name)


def _headers_for(fmt: MediaFormat) -> Optional[dict[str, str]]:
    if fmt.origin == FormatOrigin.PREVIEW:
        return {"Accept": "image/avif,image/webp,image/*,*/*;q=0.8"}
    return None


def _materialise(file: DownloadedFile) -> Path:
    """Return a path for ``file``, spilling to disk when it is in memory."""
    if file.path is not None and file.path.exists():
        return file.path
    path = _reserve_file(None, file.filename, prefix="reddit-mem-")
    path.write_bytes(file.data or b"")
    file.path = path
    file.note = "temp"
    return path


def _run_ffmpeg(video: Path, audio: Path, output: Path) -> None:
    """Mux ``video`` + ``audio`` into ``output`` using the bundled ffmpeg."""
    try:
        import imageio_ffmpeg  # type: ignore

        executable = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # pragma: no cover - ffmpeg is optional
        executable = "ffmpeg"
    command = [
        executable,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(video),
        "-i",
        str(audio),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(output),
    ]
    try:
        result = subprocess.run(command, capture_output=True, timeout=900)
    except FileNotFoundError as exc:
        raise MuxingError("ffmpeg executable not found") from exc
    except subprocess.TimeoutExpired as exc:  # pragma: no cover
        raise MuxingError("ffmpeg timed out") from exc
    if result.returncode != 0:
        raise MuxingError((result.stderr or b"").decode("utf-8", "replace")[:300] or "ffmpeg failed")


def _filename(metadata: PostMetadata, item: MediaItem, fmt: MediaFormat, *, index: int) -> str:
    extension = fmt.extension or media_extension(fmt.url) or "bin"
    quality = (fmt.quality_label or "media").replace("/", "-").replace(" ", "")
    stem = f"{metadata.id or 'reddit'}_{item.index + 1}_{quality}"
    return f"{slug(stem)}.{extension}"


def slug(value: str) -> str:
    """Filesystem safe slug used for generated filenames."""
    keep = [char if (char.isalnum() or char in ("-", "_", ".")) else "_" for char in value]
    return "".join(keep).strip("._")[:120] or "reddit"