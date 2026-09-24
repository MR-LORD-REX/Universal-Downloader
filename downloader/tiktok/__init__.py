"""TikTok videos, photo-post soundtracks, profiles, sounds, tags and collections.

```python
from downloader.tiktok import TikTokClient

async with TikTokClient() as tt:
    meta = await tt.get_metadata("https://www.tiktok.com/@user/video/7253412088251534594")
    print(meta.links(), meta.size_human)
```

Everything is resolved through yt-dlp. See ``docs/TIKTOK_SDK.md`` for the two
upstream limitations that matter: photo (slideshow) posts expose their
soundtrack only, and datacentre IPs are usually blocked by TikTok.
"""

__version__ = "1.0.0"

from .client import TikTokClient
from .config import TikTokConfig
from .exceptions import (
    NoMediaFoundError,
    PhotoPostUnsupportedError,
    RegionBlockedError,
    TikTokError,
    VideoNotFoundError,
    VideoUnavailableError,
)
from .urls import TikTokRef, is_tiktok_url, parse_url, video_id_of

__all__ = [
    "NoMediaFoundError",
    "PhotoPostUnsupportedError",
    "RegionBlockedError",
    "TikTokClient",
    "TikTokConfig",
    "TikTokError",
    "TikTokRef",
    "VideoNotFoundError",
    "VideoUnavailableError",
    "__version__",
    "is_tiktok_url",
    "parse_url",
    "video_id_of",
]


async def get_metadata(url: str, **kwargs: object):
    """Fetch metadata with a throwaway client (see :meth:`TikTokClient.get_metadata`)."""
    async with TikTokClient() as client:
        return await client.get_metadata(url, **kwargs)


async def download(url: str, **kwargs: object):
    """Download a url with a throwaway client."""
    async with TikTokClient() as client:
        return await client.download_by_url(url, **kwargs)


async def save(url: str, dest: object, **kwargs: object) -> list:
    """Download and save a url with a throwaway client."""
    async with TikTokClient() as client:
        return list(await client.save(await client.get_metadata(url), dest, **kwargs))