"""Backwards compatible aliases for the previous hand rolled module.

``Reddit`` used to be a bare stub; it is now the full SDK client so existing
imports keep working::

    from downloader.reddit import Reddit

    async with Reddit() as reddit:
        metadata = await reddit.get_metadata(url)
        result = await reddit.download(metadata)
        await result.save("output")
"""

from __future__ import annotations

from .client import RedditClient


class Reddit(RedditClient):
    """Alias of :class:`~downloader.reddit.RedditClient`."""


__all__ = ["Reddit"]