"""Reddit downloader SDK.

A typed, async toolkit for pulling media off Reddit: images, gifs, hosted
videos, galleries and external links, with full format enumeration and media
sizes available *before* downloading.

Quick start
-----------
>>> import asyncio
>>> from downloader.reddit import RedditClient
>>>
>>> async def main():
...     async with RedditClient() as client:
...         meta = await client.get_metadata("https://redd.it/1basx0i")
...         print(meta.media_type, meta.media_group_type)
...         print(meta.links())            # CDN urls grouped by media type
...         print(meta.size_human)         # total size, before downloading
...         result = await client.download(meta, quality="best")
...         await result.save("downloads")
>>>
>>> asyncio.run(main())
"""

__version__ = "1.0.0"

from .client import RedditClient, download, get_metadata, save
from .config import RedditConfig
from .exceptions import (
    AuthenticationError,
    DownloadError,
    InvalidURLError,
    MediaNotAvailableError,
    MuxingError,
    NoMediaError,
    PostNotFoundError,
    ProviderError,
    RedditError,
    SaveError,
)
from .models import (
    CrosspostInfo,
    DownloadResult,
    DownloadTarget,
    DownloadedFile,
    FormatKind,
    FormatOrigin,
    MediaFormat,
    MediaGroup,
    MediaGroupType,
    MediaItem,
    MediaKind,
    PostMetadata,
    ProgressEvent,
    ProgressPhase,
    VideoInfo,
)
from .reddit import Reddit

__all__ = [
    "AuthenticationError",
    "CrosspostInfo",
    "DownloadError",
    "DownloadResult",
    "DownloadTarget",
    "DownloadedFile",
    "FormatKind",
    "FormatOrigin",
    "InvalidURLError",
    "MediaFormat",
    "MediaGroup",
    "MediaGroupType",
    "MediaItem",
    "MediaKind",
    "MediaNotAvailableError",
    "MuxingError",
    "NoMediaError",
    "PostMetadata",
    "PostNotFoundError",
    "ProgressEvent",
    "ProgressPhase",
    "ProviderError",
    "Reddit",
    "RedditClient",
    "RedditConfig",
    "RedditError",
    "SaveError",
    "VideoInfo",
    "__version__",
    "download",
    "get_metadata",
    "save",
]