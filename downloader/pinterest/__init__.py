"""Pinterest: pins, image pins, idea pins and boards.

```python
from downloader.pinterest import PinterestClient

async with PinterestClient() as pin:
    meta = await pin.get_metadata("https://www.pinterest.com/pin/1084663891475263837/")
    print(meta.links())
```

The package is re-exported by :class:`downloader.Downloader`, which routes any
``pinterest.<tld>`` or ``pin.it`` url here automatically.
"""

__version__ = "1.0.0"

from .client import PinterestClient
from .config import PinterestConfig
from .exceptions import (
    BoardNotFoundError,
    NoMediaFoundError,
    PinNotFoundError,
    PinUnavailableError,
    PinterestAPIError,
    PinterestError,
)
from .urls import PinterestRef, is_pinterest_url, parse_url, pin_id_of

__all__ = [
    "BoardNotFoundError",
    "NoMediaFoundError",
    "PinNotFoundError",
    "PinUnavailableError",
    "PinterestAPIError",
    "PinterestClient",
    "PinterestConfig",
    "PinterestError",
    "PinterestRef",
    "__version__",
    "is_pinterest_url",
    "parse_url",
    "pin_id_of",
]


async def get_metadata(url: str, **kwargs: object):
    """Fetch metadata with a throwaway client (see :meth:`PinterestClient.get_metadata`)."""
    async with PinterestClient() as client:
        return await client.get_metadata(url, **kwargs)


async def download(url: str, **kwargs: object):
    """Download a url with a throwaway client."""
    async with PinterestClient() as client:
        return await client.download_by_url(url, **kwargs)


async def save(url: str, dest: object, **kwargs: object) -> list:
    """Download and save a url with a throwaway client."""
    async with PinterestClient() as client:
        return list(await client.save(await client.get_metadata(url), dest, **kwargs))