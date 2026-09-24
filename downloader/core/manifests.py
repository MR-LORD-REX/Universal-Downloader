"""Reader for DASH manifests whose representations are single byte-ranged files.

Instagram (and some Meta endpoints) publish a ``video_dash_manifest`` in which
every ``<Representation>`` is a *whole* MP4 addressed by byte range
(``<SegmentBase indexRange="...">`` plus a ``<BaseURL>``) rather than a list of
segments. That is a different shape from ``v.redd.it`` (which the reddit SDK
parses in :mod:`downloader.reddit.manifests`), so it gets its own small reader
here in ``core`` where any SDK may reuse it.

Instagram also annotates each representation with ``FBContentLength`` (exact
size, so no probing needed) and ``FBQualityLabel`` (the human "720p" label).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from urllib.parse import urljoin
from xml.etree import ElementTree

__all__ = ["DashTrack", "parse_dash_tracks", "parse_frame_rate"]


@dataclass(slots=True)
class DashTrack:
    """One ``<Representation>`` of a (byte-ranged) DASH manifest."""

    url: str
    content_type: str  # "video" | "audio"
    mime_type: Optional[str] = None
    codecs: Optional[str] = None
    bandwidth: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None
    frame_rate: Optional[float] = None
    audio_sampling_rate: Optional[int] = None
    audio_channels: Optional[int] = None
    lang: Optional[str] = None
    track_id: Optional[str] = None
    size_bytes: Optional[int] = None
    quality_label: Optional[str] = None

    @property
    def is_video(self) -> bool:
        return self.content_type == "video"

    @property
    def is_audio(self) -> bool:
        return self.content_type == "audio"

    @property
    def extension(self) -> str:
        """Container extension implied by the mime type (mp4 for both tracks)."""
        if self.mime_type and "/" in self.mime_type:
            sub = self.mime_type.split("/", 1)[1].split(";")[0].strip()
            if sub in ("mp4", "webm", "m4a"):
                return sub
        return "mp4"


def parse_frame_rate(value: Optional[str]) -> Optional[float]:
    """``"15360/512"`` -> ``30.0``; ``"30"`` -> ``30.0``; anything else -> ``None``."""
    if not value:
        return None
    text = value.strip()
    try:
        if "/" in text:
            numerator, denominator = text.split("/", 1)
            denominator_value = float(denominator)
            if denominator_value == 0:
                return None
            return round(float(numerator) / denominator_value, 3)
        return float(text)
    except (TypeError, ValueError):
        return None


def _int(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _text(element: Optional[ElementTree.Element], name: str) -> Optional[str]:
    """Text of the first direct child with this local name.

    ``Element.find`` cannot be used: manifests are namespaced, so ``<BaseURL>``
    is really ``{urn:mpeg:dash:...}BaseURL``. Matching on the local name keeps
    the reader namespace agnostic.
    """
    if element is None:
        return None
    for child in element:
        if _localname(child.tag) == name and child.text:
            return child.text.strip() or None
    return None


def parse_dash_tracks(text: str, base_url: str) -> list[DashTrack]:
    """Every representation in ``text``, resolved against ``base_url``.

    Raises :class:`ValueError` when the payload is not parseable XML - callers
    treat that as "no extra formats", never as a failed post.
    """
    if not text or not text.strip():
        return []
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise ValueError(f"invalid DASH manifest: {exc}") from exc

    tracks: list[DashTrack] = []
    for adaptation in root.iter():
        if _localname(adaptation.tag) != "AdaptationSet":
            continue
        adaptation_type = (adaptation.get("contentType") or "").lower() or None
        adaptation_lang = adaptation.get("lang")
        for representation in adaptation:
            if _localname(representation.tag) != "Representation":
                continue
            url = _text(representation, "BaseURL")
            if not url:
                continue
            mime_type = representation.get("mimeType") or adaptation.get("mimeType")
            content_type = adaptation_type or _content_type(mime_type)
            channels = None
            for child in representation:
                if _localname(child.tag) == "AudioChannelConfiguration":
                    channels = _int(child.get("value"))
            tracks.append(
                DashTrack(
                    url=urljoin(base_url, url),
                    content_type=content_type or "video",
                    mime_type=mime_type,
                    codecs=representation.get("codecs") or adaptation.get("codecs"),
                    bandwidth=_int(representation.get("bandwidth"))
                    or _int(adaptation.get("bandwidth")),
                    width=_int(representation.get("width")) or _int(adaptation.get("width")),
                    height=_int(representation.get("height")) or _int(adaptation.get("height")),
                    frame_rate=parse_frame_rate(
                        representation.get("frameRate") or adaptation.get("frameRate")
                    ),
                    audio_sampling_rate=_int(
                        representation.get("audioSamplingRate")
                        or adaptation.get("audioSamplingRate")
                    ),
                    audio_channels=channels,
                    lang=representation.get("lang") or adaptation_lang,
                    track_id=representation.get("id"),
                    size_bytes=_int(
                        representation.get("FBContentLength")
                        or adaptation.get("FBContentLength")
                    ),
                    quality_label=(
                        representation.get("FBQualityLabel")
                        or adaptation.get("FBQualityLabel")
                    ),
                )
            )
    return tracks


def _content_type(mime_type: Optional[str]) -> Optional[str]:
    if not mime_type or "/" not in mime_type:
        return None
    head = mime_type.split("/", 1)[0].strip().lower()
    return head if head in ("video", "audio") else None


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag
