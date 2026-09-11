"""Contract tests for the Lyrion music provider across seeded LMS backends."""

from __future__ import annotations

from typing import Any

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import BrowseFolder

from music_assistant.providers.lyrion_music import client as lyrion_client
from music_assistant.providers.lyrion_music import provider as lyrion_provider_mod
from music_assistant.providers.lyrion_music.provider import LyrionMusicProvider
from tests.providers.lyrion.lms_server_harness import LyrionTestEndpoint


def _item_by_name(items: list[Any], name: str) -> Any:
    """Return the first item with the requested display name."""
    return next(item for item in items if getattr(item, "name", None) == name)


async def test_search_returns_expected_matches(
    lyrion_provider: LyrionMusicProvider,
) -> None:
    """Search should return matching artists, albums and tracks."""
    results = await lyrion_provider.search(
        "async",
        [MediaType.ARTIST, MediaType.ALBUM, MediaType.TRACK],
        limit=10,
    )

    assert results.artists
    assert results.albums
    assert results.tracks
    assert any(item.name == "The Async Awaiters" for item in results.artists)
    assert any(item.name == "Awaiting Sunrise" for item in results.albums)
    assert any(item.name == "Future Is Pending" for item in results.tracks)


async def test_get_album_tracks_returns_sorted_disc_track_order(
    lyrion_provider: LyrionMusicProvider,
) -> None:
    """Album track list should be sorted by disc and track numbers."""
    albums = [album async for album in lyrion_provider.get_library_albums()]
    album = _item_by_name(albums, "Race Condition Blues")

    tracks = await lyrion_provider.get_album_tracks(album.item_id)
    assert [track.name for track in tracks] == ["Race You To The Lock", "Segfault Serenade"]


async def test_get_artist_albums_returns_artist_discography(
    lyrion_provider: LyrionMusicProvider,
) -> None:
    """Artist albums should return the full discography for the given artist."""
    artists = [artist async for artist in lyrion_provider.get_library_artists()]
    artist = _item_by_name(artists, "DJ Home Azziztant")

    albums = await lyrion_provider.get_artist_albums(artist.item_id)

    assert sorted(album.name for album in albums) == ["Cache Me Outside", "Home Sweet Home Lab"]


async def test_direct_item_lookups_and_playlist_tracks(
    lyrion_provider: LyrionMusicProvider,
) -> None:
    """Direct item lookups should map fake ids to MA media objects."""
    artists = [artist async for artist in lyrion_provider.get_library_artists()]
    albums = [album async for album in lyrion_provider.get_library_albums()]
    tracks = [track async for track in lyrion_provider.get_library_tracks()]
    playlists = [playlist async for playlist in lyrion_provider.get_library_playlists()]

    artist = await lyrion_provider.get_artist(_item_by_name(artists, "The Async Awaiters").item_id)
    album = await lyrion_provider.get_album(_item_by_name(albums, "Awaiting Sunrise").item_id)
    track = await lyrion_provider.get_track(_item_by_name(tracks, "Await Me Maybe").item_id)
    playlist = await lyrion_provider.get_playlist(
        _item_by_name(playlists, "Debugging Bangers").item_id
    )
    playlist_tracks = await lyrion_provider.get_playlist_tracks(playlist.item_id)

    assert artist.name == "The Async Awaiters"
    assert album.name == "Awaiting Sunrise"
    assert track.name == "Await Me Maybe"
    assert playlist.name == "Debugging Bangers"
    assert [item.name for item in playlist_tracks] == [
        "Wake Up And Smell The Exceptions",
        "Cold Start Romance",
        "Future Is Pending",
        "Breadline Top 1",
    ]


async def test_browse_root_and_nested_sections(
    lyrion_provider: LyrionMusicProvider,
) -> None:
    """Browse should expose root sections and nested artist/album children."""
    root_items = await lyrion_provider.browse(f"{lyrion_provider.instance_id}://")
    root_ids = [item.item_id for item in root_items if isinstance(item, BrowseFolder)]

    assert root_ids == ["artists", "albums", "tracks", "playlists", "genres"]

    artist_items = await lyrion_provider.browse(f"{lyrion_provider.instance_id}://artists")
    artist_folder = _item_by_name(
        [item for item in artist_items if isinstance(item, BrowseFolder)],
        "DJ Home Azziztant",
    )

    artist_albums = await lyrion_provider.browse(artist_folder.path)
    assert sorted(item.name for item in artist_albums) == [
        "Cache Me Outside",
        "Home Sweet Home Lab",
    ]

    album_items = await lyrion_provider.browse(f"{lyrion_provider.instance_id}://albums")
    album_folder = _item_by_name(
        [item for item in album_items if isinstance(item, BrowseFolder)],
        "Awaiting Sunrise",
    )
    album_tracks = await lyrion_provider.browse(album_folder.path)
    assert [item.name for item in album_tracks] == ["Await Me Maybe", "Future Is Pending"]


async def test_get_playlist_tracks_respects_page_offset(
    lyrion_provider: LyrionMusicProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Paged playlist requests should use the LMS page offset instead of ignoring it."""
    captured: dict[str, Any] = {}

    async def fake_get_playlist_tracks_page(
        provider: LyrionMusicProvider,
        playlist_id: str,
        offset: int = 0,
        limit: int = 25,
    ) -> tuple[list[Any], bool]:
        captured["playlist_id"] = playlist_id
        captured["offset"] = offset
        captured["limit"] = limit
        return [], False

    monkeypatch.setattr(
        lyrion_provider_mod.client, "get_playlist_tracks_page", fake_get_playlist_tracks_page
    )

    await lyrion_provider.get_playlist_tracks("playlist-123", page=2)

    assert captured == {
        "playlist_id": "playlist-123",
        "offset": 2 * lyrion_provider_mod.BROWSE_PAGE_SIZE,
        "limit": lyrion_provider_mod.BROWSE_PAGE_SIZE,
    }


async def test_get_stream_details_prefers_absolute_url_and_builds_fallback(
    lyrion_provider: LyrionMusicProvider,
    lyrion_test_endpoint: LyrionTestEndpoint,
) -> None:
    """Stream details should use provided URL or build local fallback."""
    tracks = [track async for track in lyrion_provider.get_library_tracks()]
    absolute_track = _item_by_name(tracks, "Wake Up And Smell The Exceptions")
    fallback_track = _item_by_name(tracks, "Kiss My Cache")

    absolute = await lyrion_provider.get_stream_details(absolute_track.item_id, MediaType.TRACK)
    fallback = await lyrion_provider.get_stream_details(fallback_track.item_id, MediaType.TRACK)

    if lyrion_test_endpoint.fake_server is not None:
        assert absolute.path == "http://cdn.example.invalid/t1.mp3"
    else:
        assert absolute.path == (
            f"{lyrion_test_endpoint.base_url}/music/{absolute_track.item_id}/download"
        )
    assert (
        fallback.path == f"{lyrion_test_endpoint.base_url}/music/{fallback_track.item_id}/download"
    )


async def test_get_library_genres_and_genre_browse(
    lyrion_provider: LyrionMusicProvider,
) -> None:
    """Genres should be listable and browsable into matching albums."""
    genres = [genre async for genre in lyrion_provider.get_library_genres()]
    assert sorted(genres) == ["Blues", "Electro", "Lo-Fi"]

    genre_items = await lyrion_provider.browse(f"{lyrion_provider.instance_id}://genres")
    genre_folder = _item_by_name(
        [item for item in genre_items if isinstance(item, BrowseFolder)],
        "Lo-Fi",
    )

    genre_albums = await lyrion_provider.browse(genre_folder.path)
    assert [item.name for item in genre_albums] == [
        "Cache Me Outside",
        "Greatest Hit And That's It",
    ]


async def test_browse_playlist_folder_contains_tracks(
    lyrion_provider: LyrionMusicProvider,
) -> None:
    """Playlist browse folder should resolve into playlist track members."""
    playlist_items = await lyrion_provider.browse(f"{lyrion_provider.instance_id}://playlists")
    playlist_folder = _item_by_name(
        [item for item in playlist_items if isinstance(item, BrowseFolder)],
        "Guard Clauses Only",
    )

    playlist_tracks = await lyrion_provider.browse(playlist_folder.path)
    assert [item.name for item in playlist_tracks] == [
        "Kiss My Cache",
        "Await Me Maybe",
        "Race You To The Lock",
        "None Shall Dance",
        "Guard Clause Cha-Cha",
    ]


async def test_get_playlist_raises_for_unknown_id(
    lyrion_provider: LyrionMusicProvider,
) -> None:
    """Unknown playlist id should raise MediaNotFoundError."""
    with pytest.raises(MediaNotFoundError):
        await lyrion_provider.get_playlist("does-not-exist")


async def test_get_stream_details_rejects_non_track_media_type(
    lyrion_provider: LyrionMusicProvider,
) -> None:
    """Stream details endpoint should only allow track media type."""
    with pytest.raises(MediaNotFoundError):
        await lyrion_provider.get_stream_details("t1", MediaType.ALBUM)


async def test_browse_returns_empty_for_unknown_or_invalid_subpath(
    lyrion_provider: LyrionMusicProvider,
) -> None:
    """Unknown browse section and invalid third-level token return empty list."""
    unknown = await lyrion_provider.browse(f"{lyrion_provider.instance_id}://wat")
    invalid_subpath = await lyrion_provider.browse(
        f"{lyrion_provider.instance_id}://artists/a1/not-a-page-token"
    )
    assert unknown == []
    assert invalid_subpath == []


async def test_browse_artist_pagination_navigation_tokens(
    lyrion_provider: LyrionMusicProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Artist browse should emit Next/Previous page folders when paginated."""
    monkeypatch.setattr(lyrion_provider_mod, "BROWSE_PAGE_SIZE", 2)

    original_get_artists_page = lyrion_client.get_artists_page

    async def _paged_artists(
        provider: LyrionMusicProvider,
        offset: int = 0,
        limit: int = 250,
    ) -> tuple[list[Any], bool]:
        del limit
        return await original_get_artists_page(provider, offset=offset, limit=2)

    monkeypatch.setattr(lyrion_client, "get_artists_page", _paged_artists)

    expected_page_one, _ = await original_get_artists_page(lyrion_provider, offset=0, limit=2)
    expected_page_two, _ = await original_get_artists_page(lyrion_provider, offset=2, limit=2)

    page_one = await lyrion_provider.browse(f"{lyrion_provider.instance_id}://artists")
    assert [item.item_id for item in page_one if isinstance(item, BrowseFolder)] == [
        *[item.item_id for item in expected_page_one],
        "__page__2",
    ]

    page_two = await lyrion_provider.browse(f"{lyrion_provider.instance_id}://artists/__page__2")
    assert [item.item_id for item in page_two if isinstance(item, BrowseFolder)] == [
        *[item.item_id for item in expected_page_two],
        "__page__0",
    ]


async def test_browse_tracks_root_pagination_and_invalid_page_token(
    lyrion_provider: LyrionMusicProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tracks browse should support paging tokens and reject invalid track-level tokens."""
    monkeypatch.setattr(lyrion_provider_mod, "BROWSE_PAGE_SIZE", 2)

    original_get_tracks_page = lyrion_client.get_tracks_page

    async def _paged_tracks(
        provider: LyrionMusicProvider,
        filter_value: str | None = None,
        offset: int = 0,
        limit: int = 250,
    ) -> tuple[list[Any], bool]:
        del limit
        return await original_get_tracks_page(
            provider,
            filter_value=filter_value,
            offset=offset,
            limit=2,
        )

    monkeypatch.setattr(lyrion_client, "get_tracks_page", _paged_tracks)

    expected_page_one, _ = await original_get_tracks_page(lyrion_provider, offset=0, limit=2)
    expected_page_two, _ = await original_get_tracks_page(lyrion_provider, offset=2, limit=2)

    page_one = await lyrion_provider.browse(f"{lyrion_provider.instance_id}://tracks")
    track_ids_page_one = [item.item_id for item in page_one if not isinstance(item, BrowseFolder)]
    nav_ids_page_one = [item.item_id for item in page_one if isinstance(item, BrowseFolder)]
    assert track_ids_page_one == [item.item_id for item in expected_page_one]
    assert nav_ids_page_one == ["__page__2"]

    page_two = await lyrion_provider.browse(f"{lyrion_provider.instance_id}://tracks/__page__2")
    track_ids_page_two = [item.item_id for item in page_two if not isinstance(item, BrowseFolder)]
    nav_ids_page_two = [item.item_id for item in page_two if isinstance(item, BrowseFolder)]
    assert track_ids_page_two == [item.item_id for item in expected_page_two]
    assert "__page__0" in nav_ids_page_two

    invalid = await lyrion_provider.browse(f"{lyrion_provider.instance_id}://tracks/not-a-page")
    assert invalid == []
