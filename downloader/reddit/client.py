"""The public entry point: :class:`RedditClient`."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Optional, Sequence

from .config import RedditConfig
from .downloader import Downloader
from .exceptions import InvalidURLError, NoMediaError, PostNotFoundError, ProviderError, RedditError
from .formats import build_items, enrich_items, post_view
from .http import HttpClient
from .models import (
    CrosspostInfo,
    DownloadResult,
    DownloadTarget,
    MediaKind,
    PostMetadata,
    ProgressEvent,
    ProgressPhase,
)
from .models.progress import emit
from .providers import build_chain, normalize_payload
from .saver import save_metadata, save_result
from .urls import PostRef, absolute, parse

MERGEABLE_FIELDS = (
    "title",
    "author",
    "subreddit",
    "permalink",
    "url",
    "url_overridden_by_dest",
    "thumbnail",
    "created_utc",
    "selftext",
    "link_flair_text",
    "domain",
    "post_hint",
    "score",
    "num_comments",
)
RICH_KEYS = ("media_metadata", "secure_media", "media", "preview", "gallery_data")


class RedditClient:
    """Async Reddit downloader.

    ``RedditClient`` needs no credentials: it falls back to public metadata
    sources (community archive, RSS, oEmbed) and downloads media straight from
    reddit's CDN. Configure ``client_id``/``client_secret`` (+ ``refresh_token``
    or ``username``/``password``) to switch on the official API, which is the
    only source that is complete for every post type.

    Example
    -------
    >>> async with RedditClient() as client:
    ...     meta = await client.get_metadata("https://redd.it/1basx0i")
    ...     print(meta.media_type, meta.media_group_type, meta.size_human)
    ...     result = await client.download(meta, quality="best")
    ...     await result.save("downloads")
    """

    def __init__(self, config: Optional[RedditConfig] = None, **overrides: Any) -> None:
        if config is None:
            config = RedditConfig.from_env()
        if overrides:
            config = config.with_overrides(**overrides)
        self.config = config
        self._http = HttpClient(config)
        self._downloader = Downloader(self._http, config)

    # ------------------------------------------------------------- lifecycle
    @property
    def http(self) -> HttpClient:
        return self._http

    async def __aenter__(self) -> "RedditClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        """Close the http pool and drop any unsaved scratch files."""
        self._downloader.cleanup()
        await self._http.close()

    # -------------------------------------------------------------- metadata
    async def get_metadata(
        self,
        url: Optional[str] = None,
        *,
        post_id: Optional[str] = None,
        data: Any = None,
        include_sizes: Optional[bool] = None,
        expand_formats: Optional[bool] = None,
        include_previews: Optional[bool] = None,
        providers: Optional[Sequence[str]] = None,
        progress: Optional[Any] = None,
    ) -> PostMetadata:
        """Resolve a post into :class:`PostMetadata` (no media downloaded).

        Parameters
        ----------
        url:
            Any reddit post url, share link, ``redd.it`` link, bare post id or
            direct ``i.redd.it``/``v.redd.it`` media url.
        data:
            Optional reddit payload you already have (dict or json string). When
            given, no provider is contacted for the post body.
        include_sizes:
            Resolve ``Content-Length`` for every format (default from config).
        expand_formats:
            Read DASH/HLS manifests to enumerate every video rendition.
        include_previews:
            Keep reddit's preview renditions in ``item.formats``.
        providers:
            Override the provider order for this call.
        """
        started = time.perf_counter()
        await emit(progress, ProgressEvent(phase=ProgressPhase.RESOLVING, message="resolving url"))
        ref: Optional[PostRef] = self._reference(url=url, post_id=post_id) if (url or post_id) else None
        warnings: list[str] = []

        payload: Optional[dict[str, Any]] = None
        used: list[str] = []

        if data is not None:
            payload = normalize_payload(data)
            if payload is None:
                raise PostNotFoundError("the supplied payload does not contain a reddit submission")
            used.append("manual")
            if ref is None:
                ref = self._reference_from_payload(payload)
        else:
            if ref is None:
                raise InvalidURLError("provide a url, a post_id or a reddit payload")
            if ref.needs_resolution:
                ref = await self._resolve_ref(ref)
            payload = await self._fetch_payload(ref, providers=providers, warnings=warnings, used=used)

        if payload is None:
            raise PostNotFoundError(
                f"no metadata source returned data for {url or post_id} "
                f"(tried: {', '.join(providers or self.config.resolved_providers())})"
            )

        metadata = await self._build_metadata(
            payload,
            ref=ref,
            used=used,
            warnings=warnings,
            include_sizes=include_sizes,
            expand_formats=expand_formats,
            include_previews=include_previews,
            providers=providers,
            progress=progress,
        )
        metadata.meta["elapsed"] = round(time.perf_counter() - started, 3)
        return metadata

    async def metadata_from_json(self, payload: Any, **kwargs: Any) -> PostMetadata:
        """Build metadata from a reddit json payload you already have."""
        return await self.get_metadata(data=payload, **kwargs)

    # -------------------------------------------------------------- download
    async def download(
        self,
        source: str | PostMetadata | DownloadResult,
        *,
        quality: str | int = "best",
        include_audio: bool = True,
        mux: Optional[bool] = None,
        target: DownloadTarget | str = DownloadTarget.MEMORY,
        temp_dir: Optional[str | Path] = None,
        dest: Optional[str | Path] = None,
        pattern: Optional[str] = None,
        overwrite: bool = False,
        album_dir: bool = True,
        include_sizes: Optional[bool] = None,
        progress: Optional[Any] = None,
        only: Optional[Sequence[int]] = None,
        **metadata_kwargs: Any,
    ) -> DownloadResult:
        """Download the media of a post (and optionally save it right away).

        ``quality`` accepts ``"best"``, ``"worst"``, ``720`` or ``"1080p"``.
        When ``dest`` is provided the files are written to disk before the
        result is returned, which is the one-liner path for bots.
        """
        started = time.perf_counter()
        if isinstance(source, DownloadResult):
            metadata = source.metadata
        elif isinstance(source, PostMetadata):
            metadata = source
        else:
            metadata = await self.get_metadata(
                source, include_sizes=include_sizes, progress=progress, **metadata_kwargs
            )

        if not metadata.items:
            raise NoMediaError(
                f"post {metadata.id} has no downloadable media "
                f"(media_type={metadata.media_type.value})"
            )
        if pattern:
            metadata.meta["pattern"] = pattern

        outcome = await self._downloader.download(
            metadata,
            quality=quality,
            include_audio=include_audio,
            mux=mux,
            target=target,
            temp_dir=temp_dir,
            progress=progress,
            only=only,
        )
        result = DownloadResult(
            metadata=metadata,
            files=outcome.files,
            quality=quality,
            errors=outcome.errors,
            warnings=outcome.warnings,
            elapsed=round(time.perf_counter() - started, 3),
        )
        if not result.files and not result.errors:
            raise NoMediaError(
                f"nothing downloadable in post {metadata.id} "
                f"(media_type={metadata.media_type.value}); "
                "external hosts such as youtube are not fetched by this SDK"
            )
        if dest is not None:
            await save_result(
                result, dest, pattern=pattern, overwrite=overwrite, album_dir=album_dir
            )
        await emit(
            progress,
            ProgressEvent(
                phase=ProgressPhase.DONE,
                message=f"{len(result.files)} file(s), {result.total_human}",
                downloaded=result.total_bytes,
                total=result.total_bytes,
            ),
        )
        return result

    # ------------------------------------------------------------------ save
    async def save(
        self,
        source: str | PostMetadata | DownloadResult,
        dest: str | Path,
        *,
        pattern: Optional[str] = None,
        overwrite: bool = False,
        album_dir: bool = True,
        quality: str | int = "best",
        save_info: bool = False,
        progress: Optional[Any] = None,
    ) -> list[Path]:
        """Download when needed and write everything under ``dest``."""
        if isinstance(source, DownloadResult):
            result = source
        elif isinstance(source, PostMetadata):
            result = await self.download(
                source, quality=quality, target=DownloadTarget.DISK, progress=progress
            )
        else:
            result = await self.download(
                source, quality=quality, target=DownloadTarget.DISK, progress=progress, pattern=pattern
            )
        paths = await save_result(
            result, dest, pattern=pattern, overwrite=overwrite, album_dir=album_dir
        )
        if save_info:
            await save_metadata(result.metadata, _album_root(dest, result.metadata, album_dir), overwrite=overwrite)
        return paths

    async def get_post(
        self,
        url: str,
        *,
        quality: str | int = "best",
        download: bool = True,
        **kwargs: Any,
    ) -> DownloadResult:
        """Shorthand: metadata + bytes in a single call."""
        metadata = await self.get_metadata(url, **kwargs)
        if not download:
            return DownloadResult(metadata=metadata)
        return await self._downloader_result(metadata, quality=quality, **kwargs)

    async def _downloader_result(self, metadata: PostMetadata, **kwargs: Any) -> DownloadResult:
        return await self.download(metadata, **kwargs)

    # ------------------------------------------------------------- internals
    def _reference(self, *, url: Optional[str], post_id: Optional[str]) -> PostRef:
        if post_id:
            return parse(post_id)
        if not url:
            raise InvalidURLError("either url or post_id is required")
        return parse(url)

    def _reference_from_payload(self, payload: dict[str, Any]) -> PostRef:
        """Derive a reference when the caller only handed us json."""
        post_id = str(payload.get("id") or "") or None
        permalink = payload.get("permalink")
        return PostRef(
            kind="post",
            url=payload.get("url") if str(payload.get("url", "")).startswith("http") else "",
            post_id=post_id,
            subreddit=payload.get("subreddit"),
            host="reddit.com",
        ).model_copy(update={"url": permalink or payload.get("url") or ""})

    async def _resolve_ref(self, ref: PostRef) -> PostRef:
        """Follow a share/short link to the real permalink."""
        try:
            response = await self._http.request("GET", ref.url, retries=1, retry_statuses=(429, 503))
        except RedditError as exc:
            raise InvalidURLError(f"could not resolve {ref.url}: {exc}") from exc
        resolved = parse(response.url) if response.url else None
        if resolved and resolved.kind == "post" and resolved.post_id:
            return resolved.model_copy(update={"url": ref.url})
        if response.status in (403, 429):
            raise InvalidURLError(
                f"reddit refused to expand {ref.url} (HTTP {response.status}). "
                "Pass the full permalink, a post id, or configure OAuth credentials."
            )
        raise InvalidURLError(f"could not resolve {ref.url} to a post")

    async def _fetch_payload(
        self,
        ref: PostRef,
        *,
        providers: Optional[Sequence[str]],
        warnings: list[str],
        used: list[str],
    ) -> Optional[dict[str, Any]]:
        names = list(providers) if providers else list(self.config.resolved_providers())
        if ref.kind in ("media", "external") and "direct" not in names:
            names = ["direct"] + names
        chain = build_chain(self.config, providers=names)
        if not chain:
            raise PostNotFoundError("no metadata provider is available")
        merged: dict[str, Any] = {}
        for provider in chain:
            if merged.get("subreddit") and not ref.subreddit:
                # enrich the reference so permalink based sources can build a
                # canonical /r/<sub>/comments/<id>/ url without guessing
                ref = ref.model_copy(update={"subreddit": merged["subreddit"]})
            try:
                result = await provider.fetch(ref, self._http)
            except ProviderError as exc:
                warnings.append(str(exc))
                continue
            except RedditError as exc:
                warnings.append(f"[{provider.name}] {exc}")
                continue
            if not result:
                continue
            merged = _merge_payloads(merged, result)
            used.append(provider.name)
            if _is_rich(merged):
                break
        if not used:
            return None
        if not merged.get("id"):
            merged["id"] = ref.post_id
        merged.setdefault("_sdk_provider", used[0] if used else "unknown")
        return merged

    async def _build_metadata(
        self,
        payload: dict[str, Any],
        *,
        ref: PostRef,
        used: list[str],
        warnings: list[str],
        include_sizes: Optional[bool],
        expand_formats: Optional[bool],
        include_previews: Optional[bool],
        providers: Optional[Sequence[str]],
        progress: Optional[Any],
    ) -> PostMetadata:
        hints = payload.get("_sdk_hints") or {}
        previews = self.config.include_previews if include_previews is None else include_previews
        items = build_items(
            payload,
            include_previews=previews,
            video_base=hints.get("video_base"),
        )
        view = post_view(payload, items)

        crosspost: Optional[CrosspostInfo] = None
        parent_payload = _crosspost_parent(payload)
        if view.crosspost_id and parent_payload is None and self.config.resolve_crossposts:
            parent_payload = await self._fetch_crosspost_parent(view.crosspost_id, providers, warnings)
        if view.crosspost_id or parent_payload:
            parent_items: list[Any] = []
            parent: Optional[PostMetadata] = None
            if parent_payload:
                parent_items = build_items(
                    parent_payload,
                    include_previews=previews,
                    video_base=(parent_payload.get("_sdk_hints") or {}).get("video_base"),
                )
                parent_view = post_view(parent_payload, parent_items)
                parent = self._plain_metadata(parent_payload, parent_items, parent_view, used)
            crosspost = CrosspostInfo(
                id=view.crosspost_id,
                fullname=view.crosspost_id,
                subreddit=(parent_payload or {}).get("subreddit"),
                author=(parent_payload or {}).get("author"),
                title=(parent_payload or {}).get("title"),
                permalink=(parent_payload or {}).get("permalink"),
                url=(parent_payload or {}).get("url"),
                parent=parent,
            )
            if not items and parent_items:
                items = [item.model_copy(deep=True) for item in parent_items]
                view = post_view(parent_payload or payload, items)
                warnings.append("crosspost: media inherited from the original post")
                for item in items:
                    if item.kind == MediaKind.VIDEO and item.meta.get("base_url"):
                        pass

        for item in items:
            _apply_hints(item, hints)

        warnings.extend(
            await enrich_items(
                items,
                self._http,
                self.config,
                expand=expand_formats,
                sizes=include_sizes,
                progress=progress,
            )
        )

        metadata = PostMetadata(
            id=str(payload.get("id") or ref.post_id or "unknown"),
            fullname=payload.get("name") or (f"t3_{payload.get('id')}" if payload.get("id") else None),
            url=absolute(payload.get("permalink")) or ref.full_permalink or payload.get("url"),
            permalink=payload.get("permalink") or ref.permalink,
            short_permalink=f"https://redd.it/{payload.get('id')}" if payload.get("id") else None,
            requested_url=ref.url,
            title=payload.get("title"),
            author=payload.get("author"),
            subreddit=payload.get("subreddit") or ref.subreddit,
            domain=payload.get("domain"),
            created_utc=payload.get("created_utc"),
            score=payload.get("score"),
            upvote_ratio=payload.get("upvote_ratio"),
            num_comments=payload.get("num_comments"),
            flair=payload.get("link_flair_text"),
            selftext=payload.get("selftext"),
            thumbnail=payload.get("thumbnail"),
            is_self=bool(payload.get("is_self")),
            is_video=bool(payload.get("is_video")),
            is_gallery=bool(payload.get("is_gallery")),
            is_nsfw=bool(payload.get("over_18")),
            is_spoiler=bool(payload.get("spoiler")),
            is_crosspost=bool(view.crosspost_id),
            is_original_content=bool(payload.get("is_original_content")),
            media_type=view.media_type,
            media_group_type=view.media_group_type,
            items=items,
            video=view.video,
            external_url=view.external_url,
            crosspost=crosspost,
            providers=used,
            warnings=warnings,
            raw=_trim_raw(payload),
            meta={"pattern": self.config.default_pattern},
        )
        metadata.rebuild_groups()
        if payload.get("_sdk_note"):
            metadata.warnings.append(str(payload["_sdk_note"]))
        if payload.get("_sdk_partial"):
            metadata.warnings.append(
                "metadata is partial (source: "
                + ", ".join(metadata.providers)
                + "); configure OAuth credentials for full fidelity"
            )
        return metadata

    def _plain_metadata(self, payload, items, view, used) -> PostMetadata:
        metadata = PostMetadata(
            id=str(payload.get("id") or "unknown"),
            fullname=payload.get("name"),
            url=absolute(payload.get("permalink")) or payload.get("url"),
            permalink=payload.get("permalink"),
            title=payload.get("title"),
            author=payload.get("author"),
            subreddit=payload.get("subreddit"),
            created_utc=payload.get("created_utc"),
            is_gallery=bool(payload.get("is_gallery")),
            is_video=bool(payload.get("is_video")),
            is_nsfw=bool(payload.get("over_18")),
            media_type=view.media_type,
            media_group_type=view.media_group_type,
            items=items,
            video=view.video,
            providers=list(used),
        )
        metadata.rebuild_groups()
        return metadata

    async def _fetch_crosspost_parent(
        self,
        parent_fullname: str,
        providers: Optional[Sequence[str]],
        warnings: list[str],
    ) -> Optional[dict[str, Any]]:
        parent_id = parent_fullname.replace("t3_", "")
        ref = parse(parent_id)
        used: list[str] = []
        payload = await self._fetch_payload(ref, providers=providers, warnings=warnings, used=used)
        return payload


def _apply_hints(item, hints: dict[str, Any]) -> None:
    if hints.get("video_base") and item.kind == MediaKind.VIDEO:
        item.meta["base_url"] = hints["video_base"]


def _merge_payloads(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    """Fill missing/empty values of ``base`` with the ones from ``extra``."""
    if not base:
        return dict(extra)
    for key, value in extra.items():
        if key.startswith("_sdk"):
            continue
        current = base.get(key)
        if current in (None, "", [], {}, 0, False) and value not in (None, "", [], {}):
            base[key] = value
    for key in ("media_metadata", "secure_media", "gallery_data", "preview", "post_hint"):
        if not base.get(key) and extra.get(key):
            base[key] = extra[key]
    return base


def _is_rich(payload: dict[str, Any]) -> bool:
    """``True`` when a payload already carries everything we can get."""
    if any(payload.get(key) for key in RICH_KEYS):
        return True
    if payload.get("is_self") or payload.get("selftext"):
        return True
    url = str(payload.get("url") or "")
    if not url:
        return False
    if any(host in url for host in ("i.redd.it", "v.redd.it", "imgur.com")):
        return True
    return "reddit.com" not in url and "redd.it" not in url


def _crosspost_parent(payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    parents = payload.get("crosspost_parent_list")
    if isinstance(parents, list) and parents:
        return parents[0]
    return None


def _trim_raw(payload: dict[str, Any], *, keep: int = 120) -> dict[str, Any]:
    """Keep a compact, json safe copy of the payload for debugging."""
    trimmed: dict[str, Any] = {}
    for key, value in payload.items():
        if key.startswith("_sdk"):
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            trimmed[key] = value
        elif isinstance(value, list):
            if len(value) <= keep and not isinstance(value[0] if value else None, dict):
                trimmed[key] = value
        elif isinstance(value, dict) and key in (
            "media_metadata",
            "gallery_data",
            "secure_media",
            "media",
            "preview",
            "poll_data",
        ):
            trimmed[key] = value
    return trimmed


def _album_root(dest: str | Path, metadata: PostMetadata, album_dir: bool) -> Path:
    from .saver import album_directory

    base = Path(dest)
    if album_dir and metadata.count > 1:
        return base / album_directory(metadata)
    return base


# --------------------------------------------------------------- one shot api
async def get_metadata(url: str, **kwargs: Any) -> PostMetadata:
    """One-shot :meth:`RedditClient.get_metadata` (opens and closes a client)."""
    async with RedditClient() as client:
        return await client.get_metadata(url, **kwargs)


async def download(url: str, **kwargs: Any) -> DownloadResult:
    """One-shot :meth:`RedditClient.download`."""
    async with RedditClient() as client:
        return await client.download(url, **kwargs)


async def save(url: str, dest: str | Path, **kwargs: Any) -> list[Path]:
    """One-shot :meth:`RedditClient.save`."""
    async with RedditClient() as client:
        return await client.save(url, dest, **kwargs)