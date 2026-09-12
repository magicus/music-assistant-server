"""Lyrion payload parsers and normalization helpers."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Literal

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

from music_assistant.providers.lyrion.client import get_configured_host, get_configured_port
from pylyrion.helpers import LyrionMediaParserFacade
from pylyrion.media_items import LyrionAlbum, LyrionArtist, LyrionTrack

from . import artwork

if TYPE_CHECKING:
    from music_assistant.providers.lyrion_music.provider import LyrionMusicProvider


PYLYRION_PARSERS = LyrionMediaParserFacade()


def parse_artist(provider: LyrionMusicProvider, row: Mapping[str, str]) -> Artist:
    """Parse LMS artist payload into an MA Artist model."""
    artwork_url = artwork.extract_artist_artwork_url(provider, row)
    try:
        lyrion_artist = PYLYRION_PARSERS.parse_artist(row, artwork_url=artwork_url)
    except ValueError as err:
        raise MediaNotFoundError("Artist payload without id") from err

    return _artist_from_lyrion(provider, lyrion_artist)


def _artist_from_lyrion(provider: LyrionMusicProvider, lyrion_artist: LyrionArtist) -> Artist:
    """Map pylyrion intermediate artist payload to MA Artist."""
    artist = Artist(
        item_id=lyrion_artist.item_id,
        provider=provider.instance_id,
        name=lyrion_artist.name,
        provider_mappings={
            ProviderMapping(
                item_id=lyrion_artist.item_id,
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
                details=_serialize_mapping_details(
                    lyrion_artist.mapping_details,
                ),
            )
        },
    )
    if lyrion_artist.artwork_url:
        artist.metadata.add_image(
            MediaItemImage(
                type=ImageType.THUMB,
                path=lyrion_artist.artwork_url,
                provider=provider.instance_id,
                remotely_accessible=False,
            )
        )
    return artist


def parse_album(provider: LyrionMusicProvider, row: Mapping[str, str]) -> Album:
    """Parse LMS album payload into an MA Album model."""
    artwork_url = artwork.extract_artwork_url(
        provider,
        row,
        fallback_id=extract_item_id(row),
    )
    try:
        lyrion_album = PYLYRION_PARSERS.parse_album(row, artwork_url=artwork_url)
    except ValueError as err:
        raise MediaNotFoundError("Album payload without id") from err

    return _album_from_lyrion(provider, lyrion_album)


def _album_from_lyrion(provider: LyrionMusicProvider, lyrion_album: LyrionAlbum) -> Album:
    """Map pylyrion intermediate album payload to MA Album."""
    artists: UniqueList[Artist | ItemMapping] = UniqueList(
        [
            ItemMapping(
                media_type=MediaType.ARTIST,
                item_id=artist.item_id,
                provider=provider.instance_id,
                name=artist.name,
            )
            for artist in lyrion_album.artists
        ]
    )
    album = Album(
        item_id=lyrion_album.item_id,
        provider=provider.instance_id,
        name=lyrion_album.name,
        artists=artists,
        provider_mappings={
            ProviderMapping(
                item_id=lyrion_album.item_id,
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
                details=_serialize_mapping_details(
                    lyrion_album.mapping_details,
                ),
            )
        },
    )
    if lyrion_album.artwork_url:
        album.metadata.add_image(
            MediaItemImage(
                type=ImageType.THUMB,
                path=lyrion_album.artwork_url,
                provider=provider.instance_id,
                remotely_accessible=False,
            )
        )
    return album


def parse_track(provider: LyrionMusicProvider, row: Mapping[str, str]) -> Track:
    """Parse LMS track payload into an MA Track model."""
    track_id = extract_item_id(row)
    artwork_url = artwork.extract_artwork_url(provider, row, fallback_id=track_id)
    try:
        lyrion_track = PYLYRION_PARSERS.parse_track(row, artwork_url=artwork_url)
    except ValueError as err:
        raise MediaNotFoundError("Track payload without id") from err

    return _track_from_lyrion(provider, lyrion_track)


def _track_from_lyrion(provider: LyrionMusicProvider, lyrion_track: LyrionTrack) -> Track:
    """Map pylyrion intermediate track payload to MA Track."""
    artists_items: list[Artist | ItemMapping] = [
        ItemMapping(
            media_type=MediaType.ARTIST,
            item_id=artist.item_id,
            provider=provider.instance_id,
            name=artist.name,
        )
        for artist in lyrion_track.artists
    ]
    artists = UniqueList(artists_items)

    album_mapping: ItemMapping | None = None
    if lyrion_track.album_name:
        album_mapping = ItemMapping(
            media_type=MediaType.ALBUM,
            item_id=lyrion_track.album_id or lyrion_track.album_name,
            provider=provider.instance_id,
            name=lyrion_track.album_name,
        )

    track = Track(
        item_id=lyrion_track.item_id,
        provider=provider.instance_id,
        name=lyrion_track.name,
        duration=lyrion_track.duration,
        disc_number=lyrion_track.disc_number,
        track_number=lyrion_track.track_number,
        artists=artists,
        album=album_mapping,
        provider_mappings={
            ProviderMapping(
                item_id=lyrion_track.item_id,
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
            )
        },
    )
    if lyrion_track.artwork_url:
        track.metadata.add_image(
            MediaItemImage(
                type=ImageType.THUMB,
                path=lyrion_track.artwork_url,
                provider=provider.instance_id,
                remotely_accessible=False,
            )
        )
    return track


def extract_track_artists(
    provider: LyrionMusicProvider, row: Mapping[str, str]
) -> UniqueList[Artist | ItemMapping]:
    """Extract track artists using LMS role precedence."""
    lyrion_artists = PYLYRION_PARSERS.extract_track_artists(row)
    return UniqueList(
        [
            ItemMapping(
                media_type=MediaType.ARTIST,
                item_id=artist.item_id,
                provider=provider.instance_id,
                name=artist.name,
            )
            for artist in lyrion_artists
        ]
    )


def parse_playlist(provider: LyrionMusicProvider, row: Mapping[str, str]) -> Playlist:
    """Parse LMS playlist payload into an MA Playlist model."""
    try:
        lyrion_playlist = PYLYRION_PARSERS.parse_playlist(row)
    except ValueError as err:
        raise MediaNotFoundError("Playlist payload without id or name") from err

    return Playlist(
        item_id=lyrion_playlist.item_id,
        provider=provider.instance_id,
        name=lyrion_playlist.name,
        provider_mappings={
            ProviderMapping(
                item_id=lyrion_playlist.item_id,
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
            )
        },
        is_editable=False,
    )


def extract_item_id(
    row: Mapping[str, str],
    id_keys: tuple[str, ...] = ("id", "track_id", "album_id", "artist_id"),
) -> str | None:
    """Extract the first available id field from a raw LMS payload."""
    return PYLYRION_PARSERS.extract_item_id(row, id_keys=id_keys)


def to_lms_stream_url(provider: LyrionMusicProvider, track_id: str, raw_url: str | None) -> str:
    """Resolve track stream URL."""
    host = get_configured_host(provider)
    if host is None:
        raise ProviderUnavailableError("Lyrion host is not configured")
    port = get_configured_port(provider, default=None)
    if port is None:
        raise ProviderUnavailableError("Lyrion port is not configured")
    return PYLYRION_PARSERS.to_lms_stream_url(
        track_id=track_id,
        host=host,
        port=port,
        raw_url=raw_url,
    )


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
    return PYLYRION_PARSERS.extract_artist_ref(
        row,
        preferred_name_keys=preferred_name_keys,
        preferred_id_keys=preferred_id_keys,
    )


def extract_first_list_value(value: str) -> str | None:
    """Extract first value from scalar or comma-separated LMS field."""
    return PYLYRION_PARSERS.extract_first_list_value(value)


def extract_values_for_keys(
    row: Mapping[str, str], keys: tuple[str, ...], split_mode: Literal["id", "name"]
) -> list[str]:
    """Extract scalar or list values from the first populated key."""
    return PYLYRION_PARSERS.extract_values_for_keys(row, keys, split_mode)


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


def parse_int(value: str | None, default: int = 0) -> int:
    """Parse int-like values from LMS payload, with safe fallback."""
    return PYLYRION_PARSERS.parse_int(value, default=default)


def _serialize_mapping_details(details: Mapping[str, str]) -> str | None:
    """Serialize selected LMS payload fields for later provider-local reuse."""
    if not details:
        return None
    return json.dumps(details, separators=(",", ":"))
