"""Instagram downloader SDK: posts, reels, carousels and (logged in) stories.

Async front end for `instaloader <https://github.com/instaloader/instaloader>`_
that plugs Instagram into the shared downloader pipeline, so an Instagram post
comes back as the same canonical :class:`~downloader.core.models.PostMetadata`
as Reddit, YouTube and Twitter.

What you get before downloading anything:

* ``title`` / ``description`` (the caption), ``author``, timestamps and counts
* ``media_type`` (``image`` / ``video`` / ``gallery``) and ``media_group_type``
  (``single`` / ``album``)
* the *direct* CDN links - ``image_versions2`` candidates and the progressive
  ``video_versions`` mp4s - not the post permalink
* the size of each rendition, straight from Instagram (video) or from a cheap
  ``HEAD`` probe (image)

Quick start
-----------
>>> import asyncio
>>> from downloader.instagram import InstagramClient, InstagramConfig
>>>
>>> async def main():
...     async with InstagramClient(InstagramConfig(session_file="ig.session")) as ig:
...         meta = await ig.get_metadata("https://www.instagram.com/reel/DdhvW0GslGe/")
...         print(meta.media_type, meta.media_group_type, meta.author)
...         print(meta.links())       # {'video': ['https://...fbcdn.net/...mp4']}
...         print(meta.size_human)    # known before downloading
...         result = await ig.download(meta, quality="best")
...         await result.save("downloads")
>>>
>>> asyncio.run(main())

Notes
-----
* Anonymous access is rate limited hard by Instagram (HTTP 401, "Please wait a
  few minutes"). Configure ``session_file`` for anything beyond occasional use;
  create it once with ``instaloader --login=<username>``.
* Instagram's CDN urls are signed and expire (roughly 24-48h), so deliver or
  download promptly.
* The DASH ladder is opt-in (``include_dash=True``): it is higher quality but
  VP9 video-only, which forces an ffmpeg mux for every item.
"""

__version__ = "1.0.0"

from .client import InstagramClient, instaloader_module, translate_instagram_error
from .config import InstagramConfig
from .exceptions import (
    InstagramError,
    InstagramNotFoundError,
    InstaloaderMissingError,
    LoginRequiredError,
    NoMediaFoundError,
    PrivateProfileError,
    ProfileNotFoundError,
    RateLimitedError,
    StoryUnavailableError,
)
from .models import ladder_height
from .urls import InstagramRef, is_instagram_url, parse_url, shortcode_of

__all__ = [
    "InstagramClient",
    "InstagramConfig",
    "InstagramError",
    "InstagramNotFoundError",
    "InstagramRef",
    "InstaloaderMissingError",
    "LoginRequiredError",
    "NoMediaFoundError",
    "PrivateProfileError",
    "ProfileNotFoundError",
    "RateLimitedError",
    "StoryUnavailableError",
    "__version__",
    "instaloader_module",
    "is_instagram_url",
    "ladder_height",
    "parse_url",
    "shortcode_of",
    "translate_instagram_error",
]


async def get_metadata(url: str, **kwargs: object):
    """Fetch metadata with a throwaway client (see :meth:`InstagramClient.get_metadata`)."""
    async with InstagramClient() as client:
        return await client.get_metadata(url, **kwargs)


async def download(url: str, **kwargs: object):
    """Download a url with a throwaway client (see :meth:`InstagramClient.download`)."""
    async with InstagramClient() as client:
        return await client.download_by_url(url, **kwargs)


async def save(url: str, dest: object, **kwargs: object) -> list:
    """Download and save a url with a throwaway client."""
    async with InstagramClient() as client:
        return list(await client.save(await client.get_metadata(url), dest, **kwargs))
