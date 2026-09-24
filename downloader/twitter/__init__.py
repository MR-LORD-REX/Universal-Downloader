"""Twitter/X downloader SDK.

Async, fully typed Twitter extraction: video tweets (multi-bitrate ladder via
yt-dlp), photo tweets and multi-image galleries, with every direct
``pbs.twimg.com`` / ``video.twimg.com`` link and its real size available
*before* you download.

Why two back ends? yt-dlp refuses image-only tweets (``No video could be
found``) and never returns photo media, while Twitter's own API needs
credentials. The SDK therefore reads the tweet's media list from the key-less
``api.fxtwitter.com`` fixup api and uses yt-dlp for the video quality ladder.

Quick start
-----------
>>> import asyncio
>>> from downloader.twitter import TwitterClient
>>>
>>> async def main():
...     async with TwitterClient() as tw:
...         meta = await tw.get_metadata("https://x.com/GenshinImpact/status/2102941090571485331")
...         print(meta.media_type, meta.media_group_type, meta.count)
...         print(meta.links())        # {'image': ['https://pbs.twimg.com/media/...?name=orig']}
...         print(meta.size_human)     # known before downloading
...         result = await tw.download(meta)
...         await result.save("downloads")
>>>
>>> asyncio.run(main())
"""

__version__ = "1.0.0"

from .client import TwitterClient
from .config import TwitterConfig
from .exceptions import (
    MediaAPIError,
    NoMediaFoundError,
    TweetNotFoundError,
    TweetUnavailableError,
    TwitterError,
)
from .urls import TwitterRef, is_twitter_url, parse_url, tweet_id_of

__all__ = [
    "MediaAPIError",
    "NoMediaFoundError",
    "TweetNotFoundError",
    "TweetUnavailableError",
    "TwitterClient",
    "TwitterConfig",
    "TwitterError",
    "TwitterRef",
    "__version__",
    "is_twitter_url",
    "parse_url",
    "tweet_id_of",
]


async def get_metadata(url: str, **kwargs: object):
    """Fetch metadata with a throwaway client (see :meth:`TwitterClient.get_metadata`)."""
    async with TwitterClient() as client:
        return await client.get_metadata(url, **kwargs)


async def download(url: str, **kwargs: object):
    """Download a url with a throwaway client (see :meth:`TwitterClient.download`)."""
    async with TwitterClient() as client:
        return await client.download_by_url(url, **kwargs)


async def save(url: str, dest: object, **kwargs: object) -> list:
    """Download and save a url with a throwaway client."""
    async with TwitterClient() as client:
        return list(await client.save(await client.get_metadata(url), dest, **kwargs))