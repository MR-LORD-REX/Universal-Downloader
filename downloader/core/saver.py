"""Writing downloaded media to disk (naming patterns, albums, overwrite policy)."""

from __future__ import annotations

import asyncio
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from .exceptions import SaveError
from .models import DownloadedFile, MediaFormat, MediaItem, PostMetadata

if TYPE_CHECKING:  # pragma: no cover
    from .models import DownloadResult

PLACEHOLDERS = (
    "id",
    "platform",
    "author",
    "author_id",
    "channel",
    "title",
    "index",
    "count",
    "quality",
    "ext",
    "kind",
    "date",
    "media_id",
    "width",
    "height",
    "duration",
    "provider",
)

_UNSAFE = '<>:"/\\|?*'


def render_pattern(
    pattern: str,
    *,
    metadata: PostMetadata,
    item: Optional[MediaItem] = None,
    fmt: Optional[MediaFormat] = None,
    index: int = 1,
    extension: Optional[str] = None,
) -> str:
    """Expand ``{placeholder}`` tokens into a filename.

    Available placeholders: ``id``, ``platform``, ``author``, ``author_id``,
    ``channel``, ``title``, ``index``, ``count``, ``quality``, ``ext``,
    ``kind``, ``date``, ``media_id``, ``width``, ``height``, ``duration``,
    ``provider``.
    """
    created = metadata.created_at or metadata.upload_datetime or datetime.now(timezone.utc)
    height = (item.height if item and item.height else None) or (fmt.height if fmt else None)
    values = {
        "id": metadata.id or "post",
        "platform": str(metadata.platform),
        "author": metadata.author or "unknown",
        "author_id": metadata.author_id or "",
        "channel": metadata.channel or metadata.author or "",
        "title": metadata.title or "",
        "index": str(index),
        "count": str(metadata.count or 1),
        "quality": (fmt.quality_label if fmt and fmt.quality_label else "media"),
        "ext": extension or (fmt.extension if fmt and fmt.extension else "bin"),
        "kind": str(item.kind) if item else (str(fmt.kind) if fmt else "media"),
        "date": created.strftime("%Y-%m-%d"),
        "media_id": (item.id if item and item.id else (metadata.id or "post")),
        "width": str((item.width if item and item.width else "") or ""),
        "height": str(height or ""),
        "duration": str(int(metadata.duration)) if metadata.duration else "",
        "provider": (metadata.providers[0] if metadata.providers else str(metadata.platform)),
    }
    rendered = pattern
    for key in PLACEHOLDERS:
        rendered = rendered.replace("{" + key + "}", str(values[key]))
    rendered = _sanitize(rendered)
    return rendered or f"{values['id']}_{index}"


def album_directory(metadata: PostMetadata, pattern: str = "{author}_{id}") -> str:
    """Directory name used to group a multi item post into its own folder."""
    return render_pattern(pattern, metadata=metadata, index=1)


async def save_result(
    result: "DownloadResult",
    dest: "str | Path",
    *,
    pattern: Optional[str] = None,
    overwrite: bool = False,
    album_dir: bool = True,
) -> list[Path]:
    """Write every file of a :class:`DownloadResult` under ``dest``."""
    metadata = result.metadata
    base = Path(dest)
    pattern = pattern or metadata.extra.get("pattern") or "{platform}_{id}_{index}_{quality}.{ext}"
    root = base
    if album_dir and metadata.count > 1:
        root = base / album_directory(metadata)
    saved: list[Path] = []
    for position, file in enumerate(result.files, start=1):
        item = _item_for(metadata, file.item_index)
        extension = file.extension
        name = render_pattern(
            pattern,
            metadata=metadata,
            item=item,
            fmt=file.format,
            index=item.index + 1 if item else position,
            extension=extension,
        )
        if not name.lower().endswith(f".{extension.lower()}"):
            name = f"{name}.{extension}"
        saved.append(await _write(file, root / name, overwrite=overwrite))
    return saved


async def save_metadata(
    metadata: PostMetadata,
    dest: "str | Path",
    *,
    filename: Optional[str] = None,
    overwrite: bool = False,
    indent: int = 2,
) -> Path:
    """Write the metadata json next to the media (useful for bots/archives)."""
    base = Path(dest)
    name = filename or f"{render_pattern('{platform}_{id}', metadata=metadata)}.info.json"
    target = base / name
    if target.exists() and not overwrite:
        target = _unique(target)
    payload = json.dumps(metadata.to_dict(), indent=indent, ensure_ascii=False)

    def write() -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(payload, encoding="utf-8")

    await asyncio.to_thread(write)
    return target


async def _write(file: DownloadedFile, target: Path, *, overwrite: bool) -> Path:
    def write() -> Path:
        target.parent.mkdir(parents=True, exist_ok=True)
        destination = target
        if destination.exists() and not overwrite:
            destination = _unique(destination)
        if file.data is not None:
            destination.write_bytes(file.data)
        elif file.path is not None and file.path.exists():
            if destination.resolve() != file.path.resolve():
                try:
                    shutil.move(str(file.path), str(destination))
                except OSError:
                    shutil.copy2(file.path, destination)
                    file.path.unlink(missing_ok=True)
        else:
            raise SaveError(f"nothing to write for {file.filename}")
        file.path = destination
        if file.note == "temp":
            file.note = None
        return destination

    try:
        return await asyncio.to_thread(write)
    except OSError as exc:
        raise SaveError(f"could not write {target}: {exc}") from exc


def _item_for(metadata: PostMetadata, item_index: int) -> Optional[MediaItem]:
    for item in metadata.items:
        if item.index == item_index:
            return item
    return None


def _unique(path: Path) -> Path:
    stem, suffix, parent = path.stem, path.suffix, path.parent
    counter = 1
    candidate = path
    while candidate.exists():
        candidate = parent / f"{stem}_{counter}{suffix}"
        counter += 1
    return candidate


def _sanitize(value: str) -> str:
    cleaned = "".join("_" if char in _UNSAFE or ord(char) < 32 else char for char in value)
    cleaned = cleaned.strip().strip(".")
    while ".." in cleaned:
        cleaned = cleaned.replace("..", ".")
    return cleaned[:180]