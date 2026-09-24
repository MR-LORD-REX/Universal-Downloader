"""Parsers for the DASH and HLS manifests served by ``v.redd.it``."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urljoin
from xml.etree import ElementTree

from .exceptions import MediaNotAvailableError


@dataclass(slots=True)
class DashRepresentation:
    """One ``<Representation>`` of a DASH manifest."""

    url: str
    content_type: str  # "video" | "audio"
    mime_type: Optional[str] = None
    codecs: Optional[str] = None
    bandwidth: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None
    frame_rate: Optional[float] = None
    audio_sampling_rate: Optional[int] = None
    lang: Optional[str] = None
    representation_id: Optional[str] = None

    @property
    def filename(self) -> str:
        return self.url.rsplit("/", 1)[-1]

    @property
    def container(self) -> Optional[str]:
        if self.mime_type and "/" in self.mime_type:
            sub = self.mime_type.split("/", 1)[1]
            return sub.split(";")[0].strip()
        if "." in self.filename:
            return self.filename.rsplit(".", 1)[-1]
        return None


@dataclass(slots=True)
class HlsVariant:
    """One ``#EXT-X-STREAM-INF`` entry of an HLS master playlist."""

    url: str
    bandwidth: Optional[int] = None
    average_bandwidth: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None
    frame_rate: Optional[float] = None
    codecs: Optional[str] = None
    audio_group: Optional[str] = None
    name: Optional[str] = None


@dataclass(slots=True)
class HlsMediaPlaylist:
    """A media playlist: the segment list that makes up one rendition."""

    segments: list[str] = field(default_factory=list)
    init_segment: Optional[str] = None
    duration: Optional[float] = None
    is_master: bool = False


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _float(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    value = str(value).strip()
    try:
        if "/" in value:
            num, den = value.split("/", 1)
            denominator = float(den)
            return float(num) / denominator if denominator else None
        return float(value)
    except ValueError:
        return None


def _int(value: Optional[str]) -> Optional[int]:
    number = _float(value)
    return int(number) if number is not None else None


def parse_mpd(text: str, base_url: str) -> list[DashRepresentation]:
    """Parse a DASH MPD into a flat list of representations.

    ``base_url`` is the manifest url; relative ``<BaseURL>`` entries are
    resolved against it.
    """
    if not text or not text.strip():
        raise MediaNotAvailableError("empty DASH manifest")
    try:
        root = ElementTree.fromstring(text.strip())
    except ElementTree.ParseError as exc:
        raise MediaNotAvailableError(f"invalid DASH manifest: {exc}") from exc

    out: list[DashRepresentation] = []
    for adaptation in _iter(root, "AdaptationSet"):
        content_type = (adaptation.get("contentType") or "").lower()
        adaptation_mime = adaptation.get("mimeType")
        adaptation_lang = adaptation.get("lang")
        for representation in _iter(adaptation, "Representation"):
            mime_type = representation.get("mimeType") or adaptation_mime
            if not content_type and mime_type:
                content_type = mime_type.split("/", 1)[0].lower()
            resolved = content_type
            if resolved not in ("video", "audio"):
                resolved = "audio" if (mime_type or "").startswith("audio") else "video"
            base_urls = list(_iter(representation, "BaseURL"))
            target = base_urls[0].text.strip() if base_urls and base_urls[0].text else None
            if not target:
                continue
            out.append(
                DashRepresentation(
                    url=urljoin(base_url, target),
                    content_type=resolved,
                    mime_type=mime_type,
                    codecs=representation.get("codecs"),
                    bandwidth=_int(representation.get("bandwidth")),
                    width=_int(representation.get("width")),
                    height=_int(representation.get("height")),
                    frame_rate=_float(representation.get("frameRate")),
                    audio_sampling_rate=_int(representation.get("audioSamplingRate")),
                    lang=adaptation_lang,
                    representation_id=representation.get("id"),
                )
            )
    if not out:
        raise MediaNotAvailableError("DASH manifest contains no representations")
    return out


def _iter(element: ElementTree.Element, name: str):
    for child in element.iter():
        if _localname(child.tag) == name:
            yield child


def parse_hls_master(text: str, base_url: str) -> list[HlsVariant]:
    """Parse an HLS master playlist into its variants."""
    variants: list[HlsVariant] = []
    lines = [line.strip() for line in text.splitlines()]
    pending: dict[str, object] = {}
    for line in lines:
        if line.startswith("#EXT-X-STREAM-INF:"):
            attributes = _parse_attributes(line.split(":", 1)[1])
            resolution = str(attributes.get("RESOLUTION", ""))
            width = height = None
            if "x" in resolution:
                width_s, height_s = resolution.lower().split("x", 1)
                width, height = _int(width_s), _int(height_s)
            pending = {
                "bandwidth": _int(str(attributes.get("BANDWIDTH", ""))),
                "average_bandwidth": _int(str(attributes.get("AVERAGE-BANDWIDTH", ""))),
                "width": width,
                "height": height,
                "frame_rate": _float(str(attributes.get("FRAME-RATE", ""))),
                "codecs": attributes.get("CODECS"),
                "audio_group": attributes.get("AUDIO"),
                "name": attributes.get("NAME"),
            }
        elif line and not line.startswith("#") and pending:
            variants.append(HlsVariant(url=urljoin(base_url, line), **pending))  # type: ignore[arg-type]
            pending = {}
    return variants


def parse_hls_media(text: str, base_url: str) -> HlsMediaPlaylist:
    """Parse a media playlist (segment list) or detect a master playlist."""
    playlist = HlsMediaPlaylist()
    duration = 0.0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-STREAM-INF"):
            playlist.is_master = True
            continue
        if line.startswith("#EXTINF:"):
            value = _float(line.split(":", 1)[1].split(",")[0])
            duration += value or 0.0
            continue
        if line.startswith("#EXT-X-MAP:"):
            attributes = _parse_attributes(line.split(":", 1)[1])
            uri = attributes.get("URI")
            if uri:
                playlist.init_segment = urljoin(base_url, str(uri))
            continue
        if line.startswith("#"):
            continue
        playlist.segments.append(urljoin(base_url, line))
    playlist.duration = duration or None
    return playlist


def _parse_attributes(payload: str) -> dict[str, str]:
    """Parse ``KEY=VALUE,KEY="quoted,value"`` attribute lists."""
    attributes: dict[str, str] = {}
    pattern = re.compile(r'([A-Za-z0-9\-]+)=("[^"]*"|[^,]*)')
    for match in pattern.finditer(payload):
        key = match.group(1).upper()
        value = match.group(2)
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        attributes[key] = value
    return attributes