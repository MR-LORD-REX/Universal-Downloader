"""YouTube downloader SDK.

Async, fully typed YouTube extraction (via yt-dlp) plus a native streaming
downloader: video metadata, every rendition with its exact size *before* you
download, community posts, playlists, subtitles and ffmpeg muxing of the
separate video/audio renditions YouTube serves.

Quick start
-----------
>>> import asyncio
>>> from downloader.youtube import YouTubeClient
>>>
>>> async def main():
...     async with YouTubeClient() as yt:
...         meta = await yt.get_metadata("https://youtu.be/FOUwd1h_jF4")
...         print(meta.title, meta.duration, meta.size_human)
...         print(meta.links())            # direct googlevideo.com urls, grouped by kind
...         result = await yt.download(meta, quality="1080p")
...         await result.save("downloads")
>>>
>>> asyncio.run(main())
"""

__version__ = "1.0.0"

from .client import YouTubeClient
from .config import YouTubeConfig
from .exceptions import (
    CommunityPostError,
    ExtractionError,
    LiveStreamError,
    VideoUnavailableError,
    YouTubeError,
)
from .urls import YouTubeRef, is_youtube_url, parse_url

__all__ = [
    "CommunityPostError",
    "ExtractionError",
    "LiveStreamError",
    "VideoUnavailableError",
    "YouTubeClient",
    "YouTubeConfig",
    "YouTubeError",
    "YouTubeRef",
    "__version__",
    "is_youtube_url",
    "parse_url",
]


async def get_metadata(url: str, **kwargs: object):
    """Fetch metadata with a throwaway client (see :meth:`YouTubeClient.get_metadata`)."""
    async with YouTubeClient() as client:
        return await client.get_metadata(url, **kwargs)


async def download(url: str, **kwargs: object):
    """Download a url with a throwaway client (see :meth:`YouTubeClient.download`)."""
    async with YouTubeClient() as client:
        return await client.download_by_url(url, **kwargs)


async def save(url: str, dest: "str | object", **kwargs: object) -> list:
    """Download and save a url with a throwaway client."""
    async with YouTubeClient() as client:
        return list(await client.save(await client.get_metadata(url), dest, **kwargs))