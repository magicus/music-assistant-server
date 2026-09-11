"""Provider-neutral parsing helpers for Lyrion library payloads."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal
from urllib.parse import quote

from pylyrion.media_items import (
    LyrionAlbum,
    LyrionArtist,
    LyrionArtistRef,
    LyrionPlaylist,
    LyrionTrack,
)
from pylyrion.session import build_lms_url


def parse_artist(row: Mapping[str, str], artwork_url: str | None = None) -> LyrionArtist:
    """Parse a normalized LMS artist row into a LyrionArtist."""
    artist_id = extract_item_id(row)
    if artist_id is None:
        raise ValueError("Artist payload without id")
    name = row.get("artist") or row.get("name") or artist_id
    return LyrionArtist(
        item_id=artist_id,
        name=name,
        artwork_url=artwork_url,
        mapping_details=extract_mapping_details(
            row, ("portraitid", "artwork_url", "artwork", "icon")
        ),
    )


def parse_album(
    row: Mapping[str, str],
    artwork_url: str | None = None,
) -> LyrionAlbum:
    """Parse a normalized LMS album row into a LyrionAlbum."""
    album_id = extract_item_id(row)
    if album_id is None:
        raise ValueError("Album payload without id")
    name = row.get("album") or row.get("title") or album_id

    artist_name, artist_id = extract_artist_ref(
        row,
        preferred_name_keys=(
            "albumartist",
            "album_artist",
            "artist",
            "albumartist_name",
            "artist_name",
            "contributor",
        ),
        preferred_id_keys=(
            "albumartist_ids",
            "album_artist_ids",
            "albumartist_id",
            "album_artist_id",
            "artist_ids",
            "artist_id",
            "contributor_id",
        ),
    )
    artists = [
        LyrionArtistRef(item_id=artist_id or artist_name, name=artist_name)
        for artist_name, artist_id in [(artist_name, artist_id)]
        if artist_name
    ]

    return LyrionAlbum(
        item_id=album_id,
        name=name,
        artists=artists,
        artwork_url=artwork_url,
        mapping_details=extract_mapping_details(
            row,
            ("coverid", "artwork_track_id", "artwork_url", "artwork", "icon"),
        ),
    )


def parse_track(
    row: Mapping[str, str],
    artwork_url: str | None = None,
) -> LyrionTrack:
    """Parse a normalized LMS track row into a LyrionTrack."""
    track_id = extract_item_id(row)
    if track_id is None:
        raise ValueError("Track payload without id")

    title = row.get("title") or row.get("track") or track_id
    artists = extract_track_artists(row)
    album_id = extract_item_id(row, id_keys=("album_id", "albumid"))
    album_name = row.get("album")

    duration = 0
    raw_duration = row.get("duration")
    if raw_duration is not None:
        try:
            duration = int(float(raw_duration))
        except TypeError, ValueError:
            duration = 0

    return LyrionTrack(
        item_id=track_id,
        name=title,
        duration=duration,
        disc_number=parse_int(row.get("disc") or row.get("discnum"), default=0),
        track_number=parse_int(row.get("tracknum"), default=0),
        artists=artists,
        album_id=album_id,
        album_name=album_name,
        artwork_url=artwork_url,
    )


def parse_playlist(row: Mapping[str, str]) -> LyrionPlaylist:
    """Parse a normalized LMS playlist row into a LyrionPlaylist."""
    playlist_id = row.get("id")
    playlist_name = row.get("name")
    if playlist_id is None or playlist_name is None:
        raise ValueError("Playlist payload without id or name")
    return LyrionPlaylist(item_id=playlist_id, name=playlist_name)


def extract_item_id(
    row: Mapping[str, str],
    id_keys: tuple[str, ...] = ("id", "track_id", "album_id", "artist_id"),
) -> str | None:
    """Extract the first available id field from a raw LMS payload."""
    for key in id_keys:
        value = row.get(key)
        if value is None:
            continue
        value_str = value.split(",", 1)[0].strip()
        if value_str:
            return value_str
    return None


def extract_artist_ref(
    row: Mapping[str, str],
    preferred_name_keys: tuple[str, ...] = (
        "artist",
        "albumartist",
        "album_artist",
        "artist_name",
        "albumartist_name",
        "contributor",
    ),
    preferred_id_keys: tuple[str, ...] = (
        "artist_ids",
        "artist_id",
        "albumartist_ids",
        "albumartist_id",
        "album_artist_id",
        "contributor_id",
    ),
) -> tuple[str | None, str | None]:
    """Extract artist display name and provider id from LMS payload."""
    artist_name: str | None = None
    for key in preferred_name_keys:
        value = row.get(key)
        if value is None:
            continue
        candidate = extract_first_list_value(value)
        if candidate:
            artist_name = candidate
            break

    artist_id = extract_item_id(row, id_keys=preferred_id_keys)
    if artist_name is None and artist_id is not None:
        artist_name = artist_id
    return artist_name, artist_id


def extract_track_artists(row: Mapping[str, str]) -> list[LyrionArtistRef]:
    """Extract track artists using LMS role precedence."""
    role_groups: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
        (
            ("trackartist", "trackartist_name"),
            ("trackartist_ids", "track_artist_ids", "trackartist_id", "track_artist_id"),
        ),
        (("artist", "artist_name"), ("artist_ids", "artist_id")),
        (
            ("albumartist", "album_artist", "albumartist_name"),
            (
                "albumartist_ids",
                "album_artist_ids",
                "albumartist_id",
                "album_artist_id",
            ),
        ),
        (("contributor",), ("contributor_id",)),
    )

    for name_keys, id_keys in role_groups:
        names = extract_values_for_keys(row, name_keys, split_mode="name")
        ids = extract_values_for_keys(row, id_keys, split_mode="id")
        if not names and not ids:
            continue

        artists: list[LyrionArtistRef] = []
        max_len = max(len(names), len(ids), 1)
        for idx in range(max_len):
            artist_name = names[idx] if idx < len(names) else None
            artist_id = ids[idx] if idx < len(ids) else None
            if not artist_name and artist_id:
                artist_name = artist_id
            if not artist_name:
                continue
            artists.append(LyrionArtistRef(item_id=artist_id or artist_name, name=artist_name))
        if artists:
            return artists

    return [LyrionArtistRef(item_id="Unknown Artist", name="Unknown Artist")]


def extract_first_list_value(value: str) -> str | None:
    """Extract first value from scalar or comma-separated LMS field."""
    normalized_value = value.strip()
    if not normalized_value:
        return None
    return normalized_value.split(", ", 1)[0].strip() or None


def extract_values_for_keys(
    row: Mapping[str, str], keys: tuple[str, ...], split_mode: Literal["id", "name"]
) -> list[str]:
    """Extract scalar or list values from the first populated key."""
    for key in keys:
        value = row.get(key)
        if value is None:
            continue
        if values := split_lms_values(value, split_mode=split_mode):
            return values
    return []


def split_lms_values(value: str, split_mode: Literal["id", "name"]) -> list[str]:
    """Split LMS value into scalar or multi-value list."""
    raw_value = value.strip()
    if not raw_value:
        return []
    if split_mode == "id":
        return [part.strip() for part in raw_value.split(",") if part.strip()]
    if ", " in raw_value:
        return [part.strip() for part in raw_value.split(", ") if part.strip()]
    return [raw_value]


def parse_int(value: str | None, default: int = 0) -> int:
    """Parse int-like values from LMS payload, with safe fallback."""
    if value is None:
        return default
    try:
        return int(value.strip())
    except (TypeError, ValueError):
        return default


def to_lms_stream_url(
    *,
    track_id: str,
    host: str,
    port: int,
    stream_path_template: str,
    raw_url: str | None = None,
) -> str:
    """Resolve a track stream URL from raw URL or LMS stream path template."""
    if raw_url and raw_url.startswith(("http://", "https://")):
        return raw_url
    encoded_track_id = quote(track_id, safe="")
    stream_path = stream_path_template.format(track_id=encoded_track_id)
    return build_lms_url(host, port, stream_path)


def extract_mapping_details(row: Mapping[str, str], keys: tuple[str, ...]) -> dict[str, str]:
    """Extract selected payload fields for provider-specific mapping metadata."""
    details: dict[str, str] = {}
    for key in keys:
        value = row.get(key)
        if value is None:
            continue
        details[key] = value
    return details
