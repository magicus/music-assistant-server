"""Lyrion payload parsers and normalization helpers."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import quote

from music_assistant_models.enums import ImageType, MediaType
from music_assistant_models.errors import MediaNotFoundError, ProviderUnavailableError
from music_assistant_models.media_items import (
    Album,
    Artist,
    ItemMapping,
    MediaItemImage,
    Playlist,
    ProviderMapping,
    Track,
    UniqueList,
)

from . import artwork
from .constants import STREAM_PATH_TEMPLATE

if TYPE_CHECKING:
    from music_assistant.providers.lyrion_music.provider import LyrionMusicProvider


def parse_artist(provider: LyrionMusicProvider, raw_artist: dict[str, Any]) -> Artist:
    """Parse LMS artist payload into an MA Artist model."""
    artist_id = extract_item_id(raw_artist)
    if artist_id is None:
        raise MediaNotFoundError("Artist payload without id")
    name = cast("str", raw_artist.get("artist") or raw_artist.get("name") or artist_id)
    artist = Artist(
        item_id=artist_id,
        provider=provider.instance_id,
        name=name,
        provider_mappings={
            ProviderMapping(
                item_id=artist_id,
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
                details=_serialize_mapping_details(
                    raw_artist,
                    keys=("portraitid", "artwork_url", "artwork", "icon"),
                ),
            )
        },
    )
    if artwork_url := artwork.extract_artist_artwork_url(provider, raw_artist):
        artist.metadata.add_image(
            MediaItemImage(
                type=ImageType.THUMB,
                path=artwork_url,
                provider=provider.instance_id,
                remotely_accessible=True,
            )
        )
    return artist


def parse_album(provider: LyrionMusicProvider, raw_album: dict[str, Any]) -> Album:
    """Parse LMS album payload into an MA Album model."""
    album_id = extract_item_id(raw_album)
    if album_id is None:
        raise MediaNotFoundError("Album payload without id")
    name = cast("str", raw_album.get("album") or raw_album.get("title") or album_id)

    artists: UniqueList[Artist | ItemMapping] = UniqueList()
    artist_name, artist_id = extract_artist_ref(
        raw_album,
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
    if artist_name:
        artists.append(
            ItemMapping(
                media_type=MediaType.ARTIST,
                item_id=artist_id or artist_name,
                provider=provider.instance_id,
                name=artist_name,
            )
        )

    album = Album(
        item_id=album_id,
        provider=provider.instance_id,
        name=name,
        artists=artists,
        provider_mappings={
            ProviderMapping(
                item_id=album_id,
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
                details=_serialize_mapping_details(
                    raw_album,
                    keys=("coverid", "artwork_track_id", "artwork_url", "artwork", "icon"),
                ),
            )
        },
    )
    if artwork_url := artwork.extract_artwork_url(provider, raw_album, fallback_id=album_id):
        album.metadata.add_image(
            MediaItemImage(
                type=ImageType.THUMB,
                path=artwork_url,
                provider=provider.instance_id,
                remotely_accessible=True,
            )
        )
    return album


def parse_track(provider: LyrionMusicProvider, raw_track: dict[str, Any]) -> Track:
    """Parse LMS track payload into an MA Track model."""
    track_id = extract_item_id(raw_track)
    if track_id is None:
        raise MediaNotFoundError("Track payload without id")

    title = cast("str", raw_track.get("title") or raw_track.get("track") or track_id)
    artists = extract_track_artists(provider, raw_track)

    album_id = extract_item_id(raw_track, id_keys=("album_id", "albumid"))
    album_name = cast("str | None", raw_track.get("album"))
    album_mapping: ItemMapping | None = None
    if album_name:
        album_mapping = ItemMapping(
            media_type=MediaType.ALBUM,
            item_id=album_id or album_name,
            provider=provider.instance_id,
            name=album_name,
        )

    duration: int | None = None
    raw_duration = raw_track.get("duration")
    if raw_duration is not None:
        try:
            duration = int(float(cast("str | int | float", raw_duration)))
        except TypeError, ValueError:
            duration = None

    track_number = parse_int(raw_track.get("tracknum"), default=0)
    disc_number = parse_int(raw_track.get("disc") or raw_track.get("discnum"), default=0)

    track = Track(
        item_id=track_id,
        provider=provider.instance_id,
        name=title,
        duration=duration or 0,
        disc_number=disc_number,
        track_number=track_number,
        artists=artists,
        album=album_mapping,
        provider_mappings={
            ProviderMapping(
                item_id=track_id,
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
            )
        },
    )
    if artwork_url := artwork.extract_artwork_url(provider, raw_track, fallback_id=album_id):
        track.metadata.add_image(
            MediaItemImage(
                type=ImageType.THUMB,
                path=artwork_url,
                provider=provider.instance_id,
                remotely_accessible=True,
            )
        )
    return track


def extract_track_artists(
    provider: LyrionMusicProvider, raw_track: dict[str, Any]
) -> UniqueList[Artist | ItemMapping]:
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
        names = extract_values_for_keys(raw_track, name_keys, split_mode="name")
        ids = extract_values_for_keys(raw_track, id_keys, split_mode="id")
        if not names and not ids:
            continue

        artists: UniqueList[Artist | ItemMapping] = UniqueList()
        max_len = max(len(names), len(ids), 1)
        for idx in range(max_len):
            artist_name = names[idx] if idx < len(names) else None
            artist_id = ids[idx] if idx < len(ids) else None
            if not artist_name and artist_id:
                artist_name = artist_id
            if not artist_name:
                continue
            artists.append(
                ItemMapping(
                    media_type=MediaType.ARTIST,
                    item_id=artist_id or artist_name,
                    provider=provider.instance_id,
                    name=artist_name,
                )
            )
        if artists:
            return artists

    return UniqueList(
        [
            ItemMapping(
                media_type=MediaType.ARTIST,
                item_id="Unknown Artist",
                provider=provider.instance_id,
                name="Unknown Artist",
            )
        ]
    )


def parse_playlist(provider: LyrionMusicProvider, raw_playlist: dict[str, str]) -> Playlist:
    """Parse LMS playlist payload into an MA Playlist model."""
    playlist_id = raw_playlist["id"]
    playlist_name = raw_playlist["name"]
    return Playlist(
        item_id=playlist_id,
        provider=provider.instance_id,
        name=playlist_name,
        provider_mappings={
            ProviderMapping(
                item_id=playlist_id,
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
            )
        },
        is_editable=False,
    )


def extract_item_id(
    raw: dict[str, Any], id_keys: tuple[str, ...] = ("id", "track_id", "album_id", "artist_id")
) -> str | None:
    """Extract the first available id field from a raw LMS payload."""
    for key in id_keys:
        value = raw.get(key)
        if value is None:
            continue
        value_str = str(value).split(",", 1)[0].strip()
        if value_str:
            return value_str
    return None


def to_lms_stream_url(provider: LyrionMusicProvider, track_id: str, raw_url: str | None) -> str:
    """Resolve track stream URL."""
    if raw_url and raw_url.startswith(("http://", "https://")):
        return raw_url
    host = provider.get_configured_host()
    if host is None:
        raise ProviderUnavailableError("Lyrion host is not configured")
    port = provider.get_configured_port()
    if port is None:
        raise ProviderUnavailableError("Lyrion port is not configured")
    encoded_track_id = quote(track_id, safe="")
    stream_path = STREAM_PATH_TEMPLATE.format(track_id=encoded_track_id)
    return f"http://{host}:{port}{stream_path}"


def extract_artist_ref(
    raw: dict[str, Any],
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
        value = raw.get(key)
        if value is None:
            continue
        candidate = extract_first_list_value(value)
        if candidate:
            artist_name = candidate
            break

    artist_id = extract_item_id(raw, id_keys=preferred_id_keys)
    if artist_name is None and artist_id is not None:
        artist_name = artist_id
    return artist_name, artist_id


def extract_first_list_value(value: Any) -> str:
    """Extract first value from scalar or comma-separated LMS field."""
    if isinstance(value, str):
        return value.split(", ", 1)[0].strip()
    return str(value).strip()


def extract_values_for_keys(
    raw: dict[str, Any], keys: tuple[str, ...], split_mode: str
) -> list[str]:
    """Extract scalar or list values from the first populated key."""
    for key in keys:
        value = raw.get(key)
        if value is None:
            continue
        if values := split_lms_values(value, split_mode=split_mode):
            return values
    return []


def split_lms_values(value: Any, split_mode: str) -> list[str]:
    """Split LMS value into scalar or multi-value list."""
    if isinstance(value, str):
        raw_value = value.strip()
        if not raw_value:
            return []
        if split_mode == "id":
            return [part.strip() for part in raw_value.split(",") if part.strip()]
        if ", " in raw_value:
            return [part.strip() for part in raw_value.split(", ") if part.strip()]
        return [raw_value]

    normalized = str(value).strip()
    return [normalized] if normalized else []


def album_metadata_needs_update(library_album: Album, provider_album: Album) -> bool:
    """Return True when synced album artist or artwork differs."""
    library_artist_refs = {(artist.item_id, artist.name) for artist in library_album.artists}
    provider_artist_refs = {(artist.item_id, artist.name) for artist in provider_album.artists}
    if library_artist_refs != provider_artist_refs:
        return True
    return artwork.get_thumb_path(library_album) != artwork.get_thumb_path(provider_album)


def artist_metadata_needs_update(library_artist: Artist, provider_artist: Artist) -> bool:
    """Return True when synced artist artwork differs."""
    return artwork.get_thumb_path(library_artist) != artwork.get_thumb_path(provider_artist)


def parse_int(value: Any, default: int = 0) -> int:
    """Parse int-like values from LMS payload, with safe fallback."""
    if value is None:
        return default
    try:
        return int(str(value).strip())
    except TypeError, ValueError:
        return default


def _serialize_mapping_details(raw: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    """Serialize selected LMS payload fields for later provider-local reuse."""
    details: dict[str, str] = {}
    for key in keys:
        value = raw.get(key)
        if value is None:
            continue
        value_str = str(value).strip()
        if not value_str:
            continue
        details[key] = value_str
    if not details:
        return None
    return json.dumps(details, separators=(",", ":"))
