"""Thin ffmpeg wrapper: locating the binary, muxing, remuxing and probing.

``imageio-ffmpeg`` ships a static ffmpeg build, so the SDK works out of the box
without the user installing anything. Everything here is synchronous and must
be called through :func:`asyncio.to_thread` from async code.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

from .exceptions import MuxingError

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_STREAM_RE = re.compile(r"Stream\s+#(\d+):(\d+)(?:\[[^\]]*\])?(?:\(([^)]*)\))?:\s*(\w+):\s*([^,\s]+)")
_SIZE_RE = re.compile(r"(\d{2,5})x(\d{2,5})")

_CACHED_EXE: Optional[str] = None


def ffmpeg_exe() -> str:
    """Absolute path (or ``PATH`` name) of the ffmpeg binary to use."""
    global _CACHED_EXE
    if _CACHED_EXE:
        return _CACHED_EXE
    if not _CACHED_EXE:
        try:
            import imageio_ffmpeg  # type: ignore

            _CACHED_EXE = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:  # pragma: no cover - ffmpeg is optional
            _CACHED_EXE = shutil.which("ffmpeg") or "ffmpeg"
    return _CACHED_EXE


def ffmpeg_available() -> bool:
    """``True`` when the configured ffmpeg binary can be executed."""
    try:
        result = subprocess.run(
            [ffmpeg_exe(), "-version"], capture_output=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


@dataclass(slots=True)
class ProbeResult:
    """What ffmpeg could learn about a container without decoding it."""

    has_video: bool = False
    has_audio: bool = False
    duration: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None
    video_codec: Optional[str] = None
    audio_codec: Optional[str] = None
    bitrate_kbps: Optional[int] = None
    streams: list[dict[str, object]] = field(default_factory=list)
    raw: str = ""

    @property
    def is_muxed(self) -> bool:
        return self.has_video and self.has_audio


def probe(source: "str | Path", *, headers: Optional[Sequence[str]] = None) -> ProbeResult:
    """Inspect ``source`` (a path or url) with ffmpeg and summarise the streams."""
    command = [ffmpeg_exe(), "-hide_banner"]
    for header in headers or ():
        command += ["-headers", header]
    command += ["-i", str(source)]
    try:
        result = subprocess.run(command, capture_output=True, timeout=120)
    except FileNotFoundError as exc:  # pragma: no cover - defensive
        raise MuxingError("ffmpeg executable not found") from exc
    except subprocess.TimeoutExpired as exc:  # pragma: no cover - defensive
        raise MuxingError("ffmpeg probe timed out") from exc
    stderr = (result.stderr or b"").decode("utf-8", errors="replace")
    return parse_probe(stderr)


def parse_probe(stderr: str) -> ProbeResult:
    """Parse the ``ffmpeg -i`` banner into a :class:`ProbeResult`."""
    info = ProbeResult(raw=stderr[-4000:])
    duration = _DURATION_RE.search(stderr)
    if duration:
        hours, minutes, seconds = duration.groups()
        info.duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)

    for match in _STREAM_RE.finditer(stderr):
        index, sub, _lang, kind, codec = match.groups()
        tail = stderr[match.end() : match.end() + 260]
        entry: dict[str, object] = {
            "index": f"{index}:{sub}",
            "type": kind.lower(),
            "codec": codec,
        }
        size = _SIZE_RE.search(tail)
        if size and kind.lower() == "video":
            entry["width"], entry["height"] = int(size.group(1)), int(size.group(2))
            info.width, info.height = int(size.group(1)), int(size.group(2))
        bitrate = re.search(r"(\d+)\s*kb/s", tail)
        if bitrate:
            entry["bitrate_kbps"] = int(bitrate.group(1))
        info.streams.append(entry)
        if kind.lower() == "video":
            info.has_video = True
            info.video_codec = codec
        elif kind.lower() == "audio":
            info.has_audio = True
            info.audio_codec = codec
    total = re.search(r"bitrate:\s*(\d+)\s*kb/s", stderr)
    if total:
        info.bitrate_kbps = int(total.group(1))
    return info


_AUDIO_CODECS = {"mp4": "aac", "mkv": "copy", "webm": "opus", "any": "aac"}


def mux(
    video: "str | Path",
    audio: "str | Path",
    output: "str | Path",
    *,
    container: str = "mp4",
    timeout: int = 1800,
) -> Path:
    """Mux separate ``video`` and ``audio`` tracks into ``output`` (stream copy)."""
    return _run(
        [
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
            *_container_flags(container),
            str(output),
        ],
        output=output,
        timeout=timeout,
    )


def remux(
    source: "str | Path",
    output: "str | Path",
    *,
    container: str = "mp4",
    timeout: int = 1800,
) -> Path:
    """Rewrap ``source`` into ``container`` without re-encoding."""
    return _run(
        ["-i", str(source), "-c", "copy", *_container_flags(container), str(output)],
        output=output,
        timeout=timeout,
    )


def download_manifest(
    url: str,
    output: "str | Path",
    *,
    container: str = "mp4",
    headers: Optional[dict[str, str]] = None,
    timeout: int = 3600,
) -> Path:
    """Download an HLS/DASH manifest end to end using ffmpeg."""
    command: list[str] = ["-i", url]
    if headers:
        command = ["-headers", "".join(f"{k}: {v}\r\n" for k, v in headers.items()), *command]
    command += ["-c", "copy", "-bsf:a", "aac_adtstoasc", *_container_flags(container), str(output)]
    return _run(command, output=output, timeout=timeout)


def _container_flags(container: str) -> list[str]:
    key = (container or "mp4").lower().lstrip(".")
    if key in ("mp4", "m4v", "mov"):
        return ["-movflags", "+faststart"]
    return []


def _run(args: Sequence[str], *, output: "str | Path", timeout: int) -> Path:
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    command = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", *args]
    try:
        result = subprocess.run(command, capture_output=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise MuxingError("ffmpeg executable not found") from exc
    except subprocess.TimeoutExpired as exc:  # pragma: no cover - defensive
        raise MuxingError(f"ffmpeg timed out after {timeout}s") from exc
    if result.returncode != 0 or not target.exists():
        message = (result.stderr or b"").decode("utf-8", "replace")[:400]
        raise MuxingError(message or "ffmpeg failed")
    return target