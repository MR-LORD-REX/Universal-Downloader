"""YouTube specific yt-dlp glue: option tuning and format-selector translation.

The extraction mechanics live in :mod:`downloader.core.ytdlp` so Twitter and
YouTube share one implementation; this module only adds the YouTube ``-f`` /
``-S`` expressions.
"""

from __future__ import annotations

from typing import Sequence

from ..core.select import MODE_AUDIO, MODE_FORMAT, MODE_PAIR, MODE_WORST, QualitySpec
from ..core.ytdlp import CollectingLogger, YtDlpExtractor, yt_dlp
from .config import YouTubeConfig
from .exceptions import ExtractionError, LiveStreamError, VideoUnavailableError

__all__ = [
    "CollectingLogger",
    "YtDlpExtractor",
    "yt_dlp",
    "ytdlp_format_selector",
    "ytdlp_format_sort",
]


def ytdlp_format_selector(spec: QualitySpec, config: YouTubeConfig) -> str:
    """Translate a :class:`QualitySpec` into a yt-dlp ``-f`` expression."""
    container = spec.container or config.prefer_container or "mp4"
    audio_container = "m4a" if container in ("mp4", "m4v", "mov") else "webm"
    if spec.mode == MODE_AUDIO:
        return f"bestaudio[ext={audio_container}]/bestaudio/best"
    if spec.mode == MODE_PAIR and spec.format_id and spec.audio_id:
        return f"{spec.format_id}+{spec.audio_id}"
    if spec.mode == MODE_FORMAT and spec.format_id:
        return spec.format_id
    if spec.mode == MODE_WORST:
        return "worstvideo+worstaudio/worst"
    height = spec.height
    if height:
        return (
            f"bestvideo[height<={height}][ext={container}]+bestaudio[ext={audio_container}]/"
            f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best"
        )
    return (
        f"bestvideo[ext={container}]+bestaudio[ext={audio_container}]/"
        "bestvideo+bestaudio/best"
    )


def ytdlp_format_sort(spec: QualitySpec, config: YouTubeConfig) -> list[str]:
    """``-S`` sort fields expressing the "proper quality, Telegram safe" policy."""
    fields: list[str] = []
    if spec.height:
        fields.append(f"res:{spec.height}")
    codec = spec.codec or config.prefer_video_codec
    if codec:
        fields.append(f"vcodec:{'h264' if codec.startswith(('avc', 'h264')) else codec}")
    audio_codec = config.prefer_audio_codec or "mp4a"
    fields.append(f"acodec:{'aac' if audio_codec.startswith(('mp4a', 'aac')) else audio_codec}")
    container = spec.container or config.prefer_container
    if container == "mp4":
        fields.append("ext:mp4:m4a")
    fields.append("res")
    fields.append("br")
    return fields