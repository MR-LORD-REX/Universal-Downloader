"""Multi-platform media downloader SDK (Reddit, YouTube, Twitter/X).

The public surface is deliberately small:

* :class:`Downloader` - one facade that routes a url to the right SDK and
  returns a canonical :class:`~downloader.core.models.PostMetadata` /
  :class:`~downloader.core.models.DownloadResult` for every platform.
* :class:`~downloader.reddit.RedditClient`, :class:`~downloader.youtube.YouTubeClient`,
  :class:`~downloader.twitter.TwitterClient` - the platform SDKs, each usable
  on its own.
* :mod:`downloader.core` - the shared models, HTTP layer, ffmpeg bridge,
  download engine and saver.

```python
import asyncio
from downloader import Downloader

async def main():
    async with Downloader() as dl:
        meta = await dl.get_metadata("https://youtu.be/FOUwd1h_jF4")
        print(meta.platform, meta.title, meta.size_human)
        print(meta.links())
        result = await dl.download(meta, quality="720p")
        await result.save("downloads")

asyncio.run(main())
```
"""

__version__ = "1.0.0"

from .client import Downloader, download, get_metadata, save
from .core import (
    DownloadResult,
    DownloadTarget,
    DownloaderError,
    DownloadedFile,
    FormatKind,
    FormatOrigin,
    HttpClient,
    MediaFormat,
    MediaGroup,
    MediaGroupType,
    MediaItem,
    MediaKind,
    NoMediaError,
    Platform,
    PlatformConfig,
    PostMetadata,
    ProgressEvent,
    ProgressPhase,
    SizeLimitExceededError,
    Thumbnail,
    UnsupportedURLError,
    build_plan,
    human_size,
    parse_quality,
    render_pattern,
)

__all__ = [
    "DownloadResult",
    "DownloadTarget",
    "DownloadedFile",
    "Downloader",
    "DownloaderError",
    "FormatKind",
    "FormatOrigin",
    "HttpClient",
    "MediaFormat",
    "MediaGroup",
    "MediaGroupType",
    "MediaItem",
    "MediaKind",
    "NoMediaError",
    "Platform",
    "PlatformConfig",
    "PostMetadata",
    "ProgressEvent",
    "ProgressPhase",
    "SizeLimitExceededError",
    "Thumbnail",
    "UnsupportedURLError",
    "__version__",
    "build_plan",
    "download",
    "get_metadata",
    "human_size",
    "parse_quality",
    "render_pattern",
    "save",
]