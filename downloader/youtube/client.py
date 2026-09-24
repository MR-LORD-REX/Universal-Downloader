"""The YouTube client: metadata, quality selection, downloading and saving."""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Optional, Sequence

from ..core.base import BaseClient
from ..core.enums import DownloadTarget, Platform
from ..core.exceptions import MetadataError, NoMediaError
from ..core.models import DownloadResult, DownloadedFile, MediaFormat, PostMetadata
from ..core.progress import ProgressEvent, ProgressPhase
from ..core.saver import save_metadata
from ..core.select import build_plan
from .config import YouTubeConfig
from .exceptions import CommunityPostError, ExtractionError, VideoUnavailableError
from .extract import YtDlpExtractor, ytdlp_format_selector, ytdlp_format_sort
from .models import info_to_metadata
from .posts import fetch_community_post
from .urls import YouTubeRef, is_youtube_url, parse_url


class YouTubeClient(BaseClient):
    """Async YouTube SDK built on yt-dlp extraction plus a native downloader.

    Example
    -------
    >>> async with YouTubeClient() as yt:
    ...     meta = await yt.get_metadata("https://youtu.be/FOUwd1h_jF4")
    ...     print(meta.title, meta.duration, meta.size_human)
    ...     print(meta.links())                  # {'video': ['https://...googlevideo.com/...']}
    ...     result = await yt.download(meta, quality="1080p", target="memory")
    ...     await result.save("downloads")
    """

    platform = Platform.YOUTUBE
    config_class = YouTubeConfig

    def __init__(
        self,
        config: Optional[YouTubeConfig] = None,
        *,
        progress_callback: Optional[Any] = None,
        **overrides: Any,
    ) -> None:
        super().__init__(config, progress_callback=progress_callback, **overrides)
        self._extractor = YtDlpExtractor(self.config)  # type: ignore[arg-type]

    @property
    def config(self) -> YouTubeConfig:  # type: ignore[override]
        return self._config  # type: ignore[attr-defined]

    @config.setter
    def config(self, value: YouTubeConfig) -> None:
        self._config = value

    # ------------------------------------------------------------- interface
    @classmethod
    def supports(cls, url: str) -> bool:
        """``True`` when ``url`` is a YouTube url this client can handle."""
        return is_youtube_url(url)

    def parse(self, url: str) -> YouTubeRef:
        """Parse ``url`` into a :class:`YouTubeRef`."""
        return parse_url(url)

    async def get_metadata(
        self,
        url: str,
        *,
        playlist_limit: Optional[int] = None,
        probe_sizes: Optional[bool] = None,
    ) -> PostMetadata:
        """Resolve everything known about ``url`` without downloading media.

        Works for videos, shorts, playlists, channels and community posts.
        """
        ref = parse_url(url)
        await self._emit(ProgressPhase.RESOLVING, f"resolving YouTube {ref.kind}")
        if ref.kind == "post":
            metadata = await self._post_metadata(ref)
        elif ref.kind in ("playlist", "channel"):
            metadata = await self._playlist_metadata(ref, playlist_limit)
        else:
            metadata = await self._video_metadata(ref)
        if probe_sizes is None:
            probe_sizes = self.config.probe_sizes
        if probe_sizes:
            await self._fill_sizes(metadata)
        return metadata

    # ------------------------------------------------------------ extraction
    async def _video_metadata(self, ref: YouTubeRef) -> PostMetadata:
        info = await self._extractor.extract(ref.watch_url)
        metadata = info_to_metadata(
            info,
            self.config,
            requested_url=ref.requested or ref.canonical,
            ref=ref,
        )
        if metadata.is_live:
            metadata.warnings.append(
                f"live stream ({info.get('live_status')}): use yt-dlp to record it"
            )
        return metadata

    async def _playlist_metadata(
        self, ref: YouTubeRef, playlist_limit: Optional[int]
    ) -> PostMetadata:
        limit = playlist_limit if playlist_limit is not None else self.config.playlist_max_items
        if self.config.resolve_playlists:
            info = await self._extractor.extract(
                ref.canonical, playlistend=limit, noplaylist=False
            )
        else:
            info = await self._extractor.extract_flat(ref.canonical, limit=limit)
        return info_to_metadata(
            info, self.config, requested_url=ref.requested or ref.canonical, ref=ref
        )

    async def _post_metadata(self, ref: YouTubeRef) -> PostMetadata:
        if not self.config.community_post_extraction:
            raise CommunityPostError("community post extraction is disabled in the config")
        metadata = await fetch_community_post(self.http, self.config, ref)
        await self._resolve_attached_videos(metadata)
        return metadata

    async def _resolve_attached_videos(self, metadata: PostMetadata) -> None:
        """Replace attached-video placeholders with their real formats."""
        for item in metadata.items:
            video_id = item.meta.get("video_id")
            if not video_id or item.formats:
                continue
            try:
                video = await self._video_metadata(parse_url(f"https://youtu.be/{video_id}"))
            except (ExtractionError, VideoUnavailableError, MetadataError) as exc:
                metadata.warnings.append(f"could not resolve attached video {video_id}: {exc}")
                continue
            source = video.items[0] if video.items else None
            if source is None:
                continue
            item.formats = source.formats
            item.duration = source.duration
            item.width, item.height = source.width, source.height
            item.has_audio = source.has_audio
            item.extension = source.extension
            item.size_bytes = source.size_bytes
            item.meta.update({"resolved": True, "title": video.title})
            metadata.providers = list(dict.fromkeys(metadata.providers + ["ytdlp"]))

    # -------------------------------------------------------------- plumbing
    async def _fill_sizes(self, metadata: PostMetadata) -> None:
        """Probe any format whose size is still unknown (images, thumbnails)."""
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
        if metadata.thumbnail:
            size, _ = await self.http.probe_size(metadata.thumbnail)
            if size is not None:
                for thumb in metadata.thumbnails:
                    if thumb.url == metadata.thumbnail:
                        thumb.size_bytes = size
                        break

    async def _fresh(self, metadata: PostMetadata) -> PostMetadata:
        """Re-extract when the signed CDN urls are about to expire."""
        if metadata.platform is not Platform.YOUTUBE:
            return metadata
        if not metadata.requested_url or metadata.is_playlist:
            return metadata
        extracted = metadata.extra.get("extracted_at")
        if extracted and time.time() - float(extracted) < self.config.url_ttl:
            return metadata
        try:
            return await self.get_metadata(metadata.requested_url, probe_sizes=False)
        except MetadataError:
            return metadata

    # -------------------------------------------------------------- download
    async def download(
        self,
        metadata: PostMetadata,
        *,
        backend: Optional[str] = None,
        refresh: "bool | str" = "auto",
        **kwargs: Any,
    ) -> DownloadResult:
        """Download the media of ``metadata``.

        ``backend`` selects the downloader: ``native`` streams the resolved CDN
        urls through the shared engine (with ffmpeg muxing), ``ytdlp`` delegates
        the whole download to yt-dlp, and ``auto`` prefers native but retries
        with a freshly extracted url when a signature has expired.
        """
        backend = (backend or self.config.download_backend or "auto").lower()
        await self._emit(
            ProgressPhase.RESOLVING,
            f"downloading {metadata.count} item(s) with the {backend} backend",
        )
        active = await self._fresh(metadata) if refresh is not False else metadata
        if backend == "ytdlp":
            return await self._download_with_ytdlp(active, **kwargs)
        # ``dest``/``pattern`` are handled here, once, on whichever attempt ends
        # up succeeding. A stale signature (HTTP 403) triggers a re-extract and
        # a retry, and the refreshed result must still land in ``dest``.
        dest = kwargs.pop("dest", None)
        pattern = kwargs.pop("pattern", None)
        result = await super().download(active, **kwargs)
        if backend == "auto" and result.errors and refresh is not False:
            if any("403" in message for message in result.errors.values()):
                fresh = await self._fresh_forced(metadata)
                if fresh is not active:
                    result = await super().download(fresh, **kwargs)
        if dest is not None:
            await result.save(dest, pattern=pattern or self.config.default_pattern)
        return result

    async def _fresh_forced(self, metadata: PostMetadata) -> PostMetadata:
        if not metadata.requested_url or metadata.is_playlist:
            return metadata
        try:
            return await self.get_metadata(metadata.requested_url, probe_sizes=False)
        except MetadataError:
            return metadata

    async def _download_with_ytdlp(
        self,
        metadata: PostMetadata,
        *,
        quality: Any = None,
        dest: Optional["str | Path"] = None,
        pattern: Optional[str] = None,
        temp_dir: Optional["str | Path"] = None,
        target: "DownloadTarget | str" = DownloadTarget.DISK,
        progress: Optional[Any] = None,
        **kwargs: Any,
    ) -> DownloadResult:
        spec = self.quality_spec(quality)
        entries = build_plan(metadata.items, spec, include_audio=self.config.mux_audio)
        started = time.perf_counter()
        files: list[DownloadedFile] = []
        errors: dict[str, str] = {}
        hook = _progress_hook(progress or self._progress, self.platform)
        scratch = temp_dir is None
        workdir = Path(temp_dir) if temp_dir else Path(tempfile.mkdtemp(prefix="youtube-ytdlp-"))
        for entry in entries:
            source = entry.item.meta.get("resolved_url") or metadata.requested_url or metadata.url
            if not source:
                errors[f"item[{entry.item.index}]"] = "no source url for the yt-dlp backend"
                continue
            try:
                info = await self._extractor.download(
                    source,
                    outtmpl=str(workdir / "%(id)s.%(ext)s"),
                    format_selector=ytdlp_format_selector(spec, self.config),
                    format_sort=ytdlp_format_sort(spec, self.config),
                    progress_hook=hook,
                    merge_output_format=(spec.container or self.config.prefer_container),
                )
            except Exception as exc:
                errors[f"item[{entry.item.index}]"] = str(exc)
                continue
            path = _output_path(info)
            if path is None:
                errors[f"item[{entry.item.index}]"] = "yt-dlp did not report an output file"
                continue
            files.append(
                DownloadedFile(
                    item_index=entry.item.index,
                    kind=entry.item.kind,
                    filename=path.name,
                    mime_type="video/mp4",
                    format=entry.primary,
                    path=path,
                    downloaded=path.stat().st_size,
                    elapsed=time.perf_counter() - started,
                    note="temp",
                )
            )
        result = DownloadResult(
            metadata=metadata,
            files=files,
            quality=spec.label,
            elapsed=time.perf_counter() - started,
            errors=errors,
        )
        if dest is not None:
            await result.save(dest, pattern=pattern or self.config.default_pattern)
            if scratch:
                # everything was moved into ``dest``: drop our private work dir
                shutil.rmtree(workdir, ignore_errors=True)
        return result

    # ------------------------------------------------------------ subtitles
    async def download_subtitles(
        self,
        metadata: PostMetadata,
        dest: Optional["str | Path"] = None,
        *,
        languages: Optional[Sequence[str]] = None,
        convert_to_srt: bool = False,
        overwrite: bool = False,
    ) -> list[Path]:
        """Fetch caption files as ``.vtt`` (or ``.srt``) onto disk."""
        wanted = list(languages or self.config.subtitle_languages or metadata.subtitle_languages)
        if not wanted:
            raise NoMediaError("the video exposes no subtitles for the requested languages")
        root = Path(dest) if dest is not None else None
        saved: list[Path] = []
        for language in wanted:
            formats = metadata.subtitles_for(language)
            if not formats:
                continue
            chosen = _pick_subtitle(formats)
            response = await self.http.get(chosen.url)
            if not response.ok:
                continue
            extension = "vtt" if not convert_to_srt else "srt"
            filename = f"{metadata.id}.{language}.{extension}"
            if root is None:
                continue
            path = root / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and not overwrite:
                path = _unique(path)
            payload = response.content
            if convert_to_srt and chosen.extension == "vtt":
                payload = _vtt_to_srt(response.text).encode("utf-8")
            path.write_bytes(payload)
            saved.append(path)
        return saved

    # ------------------------------------------------------------ extras
    async def download_by_url(self, url: str, **kwargs: Any) -> DownloadResult:
        """Convenience: :meth:`get_metadata` followed by :meth:`download`."""
        metadata = await self.get_metadata(url)
        return await self.download(metadata, **kwargs)

    async def save_metadata_json(
        self, metadata: PostMetadata, dest: "str | Path", **kwargs: Any
    ) -> Path:
        """Write ``<file>.info.json`` next to the media."""
        return await save_metadata(metadata, dest, **kwargs)

    def available_qualities(self, metadata: PostMetadata) -> list[str]:
        """Every rendition label of the first video item, best first."""
        from ..core.models import rank_formats

        if not metadata.items:
            return []
        labels = [
            fmt.quality_label
            for fmt in rank_formats(metadata.items[0].video_formats)
            if fmt.quality_label
        ]
        return list(dict.fromkeys(labels))


# module level helpers -------------------------------------------------------
def _output_path(info: dict[str, Any]) -> Optional[Path]:
    downloads = info.get("requested_downloads") or []
    for entry in downloads:
        path = entry.get("filepath") or entry.get("_filename")
        if path:
            candidate = Path(path)
            if candidate.exists():
                return candidate
    path = info.get("filepath") or info.get("_filename")
    if path and Path(path).exists():
        return Path(path)
    return None


def _progress_hook(callback: Optional[Any], platform: Platform):
    def hook(data: dict[str, Any]) -> None:
        if callback is None:
            return
        status = data.get("status")
        if status not in ("downloading", "finished"):
            return
        event = ProgressEvent(
            phase=ProgressPhase.DOWNLOADING if status == "downloading" else ProgressPhase.DONE,
            platform=str(platform),
            filename=str(data.get("filename") or ""),
            downloaded=int(data.get("downloaded_bytes") or 0),
            total=data.get("total_bytes") or data.get("total_bytes_estimate"),
            message=f"yt-dlp {status}",
        )
        result = callback(event)
        if hasattr(result, "__await__"):  # pragma: no cover - async hook
            asyncio.ensure_future(result)  # type: ignore[arg-type]

    return hook


def _pick_subtitle(formats: Sequence[MediaFormat]) -> MediaFormat:
    preference = {"vtt": 0, "srv3": 1, "srv1": 2, "ttml": 3, "json3": 4}
    manual = [f for f in formats if not f.meta.get("automatic")]
    pool = manual or list(formats)
    return min(pool, key=lambda f: preference.get((f.extension or "").lower(), 9))


def _vtt_to_srt(text: str) -> str:
    lines: list[str] = []
    counter = 0
    for block in text.replace("\r\n", "\n").split("\n\n"):
        block = block.strip()
        if not block or block.upper().startswith("WEBVTT"):
            continue
        block_lines = [line for line in block.splitlines() if line.strip()]
        if not block_lines:
            continue
        timing: Optional[str] = None
        payload: list[str] = []
        for line in block_lines:
            if "-->" in line and timing is None:
                timing = line.replace(".", ",")
            else:
                payload.append(line)
        if timing is None:
            continue
        counter += 1
        lines.append(str(counter))
        lines.append(timing)
        lines.extend(payload)
        lines.append("")
    return "\n".join(lines)


def _unique(path: Path) -> Path:
    stem, suffix, parent = path.stem, path.suffix, path.parent
    counter = 1
    candidate = path
    while candidate.exists():
        candidate = parent / f"{stem}_{counter}{suffix}"
        counter += 1
    return candidate