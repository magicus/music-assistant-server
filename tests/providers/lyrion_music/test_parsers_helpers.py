"""Unit tests for Lyrion parser helper fallbacks."""

from __future__ import annotations

from typing import Any

from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import Album, ItemMapping, UniqueList

from music_assistant.providers.lyrion_music import artwork, parsers


def test_extract_track_artists_fallback_unknown_artist(lyrion_provider: Any) -> None:
    """Tracks without any artist fields should get a deterministic Unknown Artist mapping."""
    artists = parsers.extract_track_artists(lyrion_provider, {})

    assert len(artists) == 1
    artist = artists[0]
    assert artist.name == "Unknown Artist"
    assert artist.item_id == "Unknown Artist"
    assert artist.provider == lyrion_provider.instance_id


def _album(*, album_id: str, provider: str, artist_id: str, artist_name: str) -> Album:
    album = Album(
        item_id=album_id, provider=provider, name=f"Album {album_id}", provider_mappings=set()
    )
    album.artists = UniqueList(
        [
            ItemMapping(
                media_type=MediaType.ARTIST,
                item_id=artist_id,
                provider=provider,
                name=artist_name,
            )
        ]
    )
    return album


def test_album_metadata_needs_update_artist_diff() -> None:
    """Different artist refs should trigger album metadata sync update."""
    library_album = _album(
        album_id="alb1",
        provider="library",
        artist_id="a1",
        artist_name="Artist One",
    )
    provider_album = _album(
        album_id="alb1",
        provider="lyrion_music--test",
        artist_id="a2",
        artist_name="Artist Two",
    )

    assert parsers.album_metadata_needs_update(library_album, provider_album) is True


def test_album_metadata_needs_update_artwork_diff() -> None:
    """Same artist refs but different thumbs should trigger metadata update."""
    library_album = _album(
        album_id="alb1",
        provider="library",
        artist_id="a1",
        artist_name="Artist One",
    )
    provider_album = _album(
        album_id="alb1",
        provider="lyrion_music--test",
        artist_id="a1",
        artist_name="Artist One",
    )

    artwork.set_thumb_path(library_album, "http://img/one.png")
    artwork.set_thumb_path(provider_album, "http://img/two.png")

    assert parsers.album_metadata_needs_update(library_album, provider_album) is True


def test_album_metadata_needs_update_no_diff() -> None:
    """Same artist refs and same artwork should not trigger metadata update."""
    library_album = _album(
        album_id="alb1",
        provider="library",
        artist_id="a1",
        artist_name="Artist One",
    )
    provider_album = _album(
        album_id="alb1",
        provider="lyrion_music--test",
        artist_id="a1",
        artist_name="Artist One",
    )

    artwork.set_thumb_path(library_album, "http://img/same.png")
    artwork.set_thumb_path(provider_album, "http://img/same.png")

    assert parsers.album_metadata_needs_update(library_album, provider_album) is False
