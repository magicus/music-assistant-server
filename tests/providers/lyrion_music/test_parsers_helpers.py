"""Unit tests for Lyrion parser helper fallbacks."""

from __future__ import annotations

from typing import Any
from unittest.mock import Mock

from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import Album, ItemMapping, UniqueList

from music_assistant.providers.lyrion_music import artwork, parsers
from pylyrion.library import normalize_row


def test_to_lms_stream_url_brackets_ipv6_host() -> None:
    """Track stream URLs should use bracketed IPv6 host literals."""
    provider = Mock()

    def _get_setup_value(key: str, default: Any = None) -> Any:
        if key == "lms_host":
            return "2001:db8::1"
        if key == "port":
            return 9000
        return default

    provider.get_setup_value = Mock(side_effect=_get_setup_value)

    url = parsers.to_lms_stream_url(provider, "track-1", raw_url=None)

    assert url.startswith("http://[2001:db8::1]:9000/")


def test_extract_track_artists_fallback_unknown_artist(lyrion_provider: Any) -> None:
    """Tracks without any artist fields should get a deterministic Unknown Artist mapping."""
    artists = parsers.extract_track_artists(lyrion_provider, {})

    assert len(artists) == 1
    artist = artists[0]
    assert artist.name == "Unknown Artist"
    assert artist.item_id == "Unknown Artist"
    assert artist.provider == lyrion_provider.instance_id


def test_extract_values_and_ids_normalize_non_string_payloads() -> None:
    """The pylyrion boundary should normalize LMS scalars before parser helpers run."""
    normalized = normalize_row({"id": 123, "tracknum": 7, "name": "  Foo  "})
    assert normalized == {"id": "123", "tracknum": "7", "name": "Foo"}
    assert parsers.extract_item_id({"id": "123"}) == "123"
    assert parsers.extract_first_list_value("123") == "123"


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
