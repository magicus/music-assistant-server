"""Provider-neutral Lyrion media item models."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class LyrionArtistRef:
    """Lightweight artist reference from Lyrion payloads."""

    item_id: str
    name: str


@dataclass(frozen=True, slots=True)
class LyrionArtist:
    """Normalized artist payload from Lyrion."""

    item_id: str
    name: str
    artwork_url: str | None = None
    mapping_details: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LyrionAlbum:
    """Normalized album payload from Lyrion."""

    item_id: str
    name: str
    artists: list[LyrionArtistRef] = field(default_factory=list)
    artwork_url: str | None = None
    mapping_details: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LyrionTrack:
    """Normalized track payload from Lyrion."""

    item_id: str
    name: str
    duration: int = 0
    disc_number: int = 0
    track_number: int = 0
    artists: list[LyrionArtistRef] = field(default_factory=list)
    album_id: str | None = None
    album_name: str | None = None
    artwork_url: str | None = None


@dataclass(frozen=True, slots=True)
class LyrionPlaylist:
    """Normalized playlist payload from Lyrion."""

    item_id: str
    name: str
