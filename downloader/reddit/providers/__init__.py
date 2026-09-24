"""Metadata provider registry."""

from __future__ import annotations

from typing import Optional

from ..config import RedditConfig
from .arctic_shift import ArcticShiftProvider
from .base import MetadataProvider
from .direct import DirectProvider
from .manual import ManualProvider, normalize_payload
from .oauth import OAuthProvider
from .oembed import OEmbedProvider
from .rss import RssProvider

PROVIDERS: dict[str, type[MetadataProvider]] = {
    "oauth": OAuthProvider,
    "arctic_shift": ArcticShiftProvider,
    "rss": RssProvider,
    "oembed": OEmbedProvider,
    "direct": DirectProvider,
    "manual": ManualProvider,
}

__all__ = [
    "ArcticShiftProvider",
    "DirectProvider",
    "ManualProvider",
    "MetadataProvider",
    "OAuthProvider",
    "OEmbedProvider",
    "PROVIDERS",
    "RssProvider",
    "build_provider",
    "build_chain",
    "normalize_payload",
]


def build_provider(name: str, config: RedditConfig, **kwargs) -> MetadataProvider:
    """Instantiate a provider by name."""
    key = name.strip().lower()
    if key not in PROVIDERS:
        raise KeyError(f"unknown provider {name!r}")
    return PROVIDERS[key](config, **kwargs)


def build_chain(
    config: RedditConfig,
    *,
    providers: Optional[list[str]] = None,
    payload: object = None,
) -> list[MetadataProvider]:
    """Build the ordered provider chain for a request."""
    names = providers or config.resolved_providers()
    chain: list[MetadataProvider] = []
    for name in names:
        if name == "manual":
            if payload is None:
                continue
            chain.append(ManualProvider(config, payload))
            continue
        provider = build_provider(name, config)
        if provider.available:
            chain.append(provider)
    return chain