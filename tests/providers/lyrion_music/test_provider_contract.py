"""Contract tests for the Lyrion music provider against a fake LMS server."""

from __future__ import annotations

from typing import Any

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import MediaNotFoundError, ProviderUnavailableError
from music_assistant_models.media_items import BrowseFolder

from music_assistant.providers.lyrion_music import client as lyrion_client
from music_assistant.providers.lyrion_music import provider as lyrion_provider_mod
from music_assistant.providers.lyrion_music.provider import LyrionMusicProvider
from tests.providers.lyrion.fake_lms_server import FakeLmsServer
from tests.providers.lyrion.fixtures import LyrionTestEndpoint, using_real_lms_from_env

pytestmark = pytest.mark.skipif(
    using_real_lms_from_env(),
    reason="Requires deterministic fake LMS catalog",
)


async def test_handle_async_init_calls_serverstatus(
    lyrion_provider: LyrionMusicProvider,
    lyrion_fake_server: FakeLmsServer,
) -> None:
    """Provider init should validate LMS endpoint via serverstatus RPC."""
    del lyrion_provider
    assert any(call.command == "serverstatus" for call in lyrion_fake_server.rpc_calls)


async def test_library_iterators_return_seeded_catalog(
    lyrion_provider: LyrionMusicProvider,
    lyrion_fake_server: FakeLmsServer,
) -> None:
    """Library iterators should parse catalog items from fake LMS."""
    lyrion_fake_server.clear_history()

    artists = [artist async for artist in lyrion_provider.get_library_artists()]
    albums = [album async for album in lyrion_provider.get_library_albums()]
    tracks = [track async for track in lyrion_provider.get_library_tracks()]
    playlists = [playlist async for playlist in lyrion_provider.get_library_playlists()]

    assert len(artists) == 4
    assert len(albums) == 6
    assert len(tracks) == 10
    assert len(playlists) == 2
    assert any(artist.name == "DJ Home Azziztant" for artist in artists)
    assert sum(1 for album in albums if album.artists and album.artists[0].item_id == "a1") == 2
    assert sum(1 for album in albums if album.artists and album.artists[0].item_id == "a3") == 1
    assert any(call.command == "artists" for call in lyrion_fake_server.rpc_calls)
    assert any(call.command == "albums" for call in lyrion_fake_server.rpc_calls)
    assert any(call.command == "titles" for call in lyrion_fake_server.rpc_calls)


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
    tracks = await lyrion_provider.get_album_tracks("alb4")
    assert [track.item_id for track in tracks] == ["t6", "t7"]


async def test_direct_item_lookups_and_playlist_tracks(
    lyrion_provider: LyrionMusicProvider,
) -> None:
    """Direct item lookups should map fake ids to MA media objects."""
    artist = await lyrion_provider.get_artist("a2")
    album = await lyrion_provider.get_album("alb3")
    track = await lyrion_provider.get_track("t4")
    playlist = await lyrion_provider.get_playlist("pl1")
    playlist_tracks = await lyrion_provider.get_playlist_tracks("pl1")

    assert artist.name == "The Async Awaiters"
    assert album.name == "Awaiting Sunrise"
    assert track.name == "Await Me Maybe"
    assert playlist.name == "Debugging Bangers"
    assert [item.item_id for item in playlist_tracks] == [
        "t1",
        "t3",
        "t5",
        "t8",
    ]


async def test_browse_root_and_nested_sections(
    lyrion_provider: LyrionMusicProvider,
) -> None:
    """Browse should expose root sections and nested artist/album children."""
    root_items = await lyrion_provider.browse(f"{lyrion_provider.instance_id}://")
    root_ids = [item.item_id for item in root_items if isinstance(item, BrowseFolder)]

    assert root_ids == ["artists", "albums", "tracks", "playlists", "genres"]

    artist_items = await lyrion_provider.browse(f"{lyrion_provider.instance_id}://artists")
    artist_folder = next(
        item for item in artist_items if isinstance(item, BrowseFolder) and item.item_id == "a1"
    )

    artist_albums = await lyrion_provider.browse(artist_folder.path)
    assert [item.item_id for item in artist_albums] == ["alb1", "alb2"]

    album_items = await lyrion_provider.browse(f"{lyrion_provider.instance_id}://albums")
    album_folder = next(
        item for item in album_items if isinstance(item, BrowseFolder) and item.item_id == "alb3"
    )
    album_tracks = await lyrion_provider.browse(album_folder.path)
    assert [item.item_id for item in album_tracks] == ["t4", "t5"]


async def test_get_stream_details_prefers_absolute_url_and_builds_fallback(
    lyrion_provider: LyrionMusicProvider,
    lyrion_test_endpoint: LyrionTestEndpoint,
) -> None:
    """Stream details should use provided URL or build local fallback."""
    absolute = await lyrion_provider.get_stream_details("t1", MediaType.TRACK)
    fallback = await lyrion_provider.get_stream_details("t2", MediaType.TRACK)

    assert absolute.path == "http://cdn.example.invalid/t1.mp3"
    assert fallback.path == f"{lyrion_test_endpoint.base_url}/music/t2/download"


async def test_resolve_image_falls_back_to_300px(
    lyrion_provider: LyrionMusicProvider,
    lyrion_fake_server: FakeLmsServer,
    lyrion_test_endpoint: LyrionTestEndpoint,
) -> None:
    """Image resolver should probe 600px first and then fallback to 300px."""
    lyrion_fake_server.clear_history()
    image_path = f"{lyrion_test_endpoint.base_url}/contributor/a3/image_600x600_f"

    result = await lyrion_provider.resolve_image(image_path)

    assert isinstance(result, bytes)
    assert any(
        path.endswith("/contributor/a3/image_600x600_f")
        for path in lyrion_fake_server.image_requests
    )
    assert any(
        path.endswith("/contributor/a3/image_300x300_f")
        for path in lyrion_fake_server.image_requests
    )


async def test_get_library_genres_and_genre_browse(
    lyrion_provider: LyrionMusicProvider,
) -> None:
    """Genres should be listable and browsable into matching albums."""
    genres = [genre async for genre in lyrion_provider.get_library_genres()]
    assert sorted(genres) == ["Blues", "Electro", "Lo-Fi"]

    genre_items = await lyrion_provider.browse(f"{lyrion_provider.instance_id}://genres")
    genre_folder = next(
        item for item in genre_items if isinstance(item, BrowseFolder) and item.item_id == "g2"
    )

    genre_albums = await lyrion_provider.browse(genre_folder.path)
    assert [item.item_id for item in genre_albums] == ["alb2", "alb5"]


async def test_browse_playlist_folder_contains_tracks(
    lyrion_provider: LyrionMusicProvider,
) -> None:
    """Playlist browse folder should resolve into playlist track members."""
    playlist_items = await lyrion_provider.browse(f"{lyrion_provider.instance_id}://playlists")
    playlist_folder = next(
        item for item in playlist_items if isinstance(item, BrowseFolder) and item.item_id == "pl2"
    )

    playlist_tracks = await lyrion_provider.browse(playlist_folder.path)
    assert [item.item_id for item in playlist_tracks] == [
        "t2",
        "t4",
        "t6",
        "t9",
        "t10",
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

    page_one = await lyrion_provider.browse(f"{lyrion_provider.instance_id}://artists")
    assert [item.item_id for item in page_one if isinstance(item, BrowseFolder)] == [
        "a1",
        "a2",
        "__page__2",
    ]

    page_two = await lyrion_provider.browse(f"{lyrion_provider.instance_id}://artists/__page__2")
    assert [item.item_id for item in page_two if isinstance(item, BrowseFolder)] == [
        "a3",
        "a4",
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

    page_one = await lyrion_provider.browse(f"{lyrion_provider.instance_id}://tracks")
    track_ids_page_one = [item.item_id for item in page_one if not isinstance(item, BrowseFolder)]
    nav_ids_page_one = [item.item_id for item in page_one if isinstance(item, BrowseFolder)]
    assert track_ids_page_one == ["t1", "t2"]
    assert nav_ids_page_one == ["__page__2"]

    page_two = await lyrion_provider.browse(f"{lyrion_provider.instance_id}://tracks/__page__2")
    track_ids_page_two = [item.item_id for item in page_two if not isinstance(item, BrowseFolder)]
    nav_ids_page_two = [item.item_id for item in page_two if isinstance(item, BrowseFolder)]
    assert track_ids_page_two == ["t3", "t4"]
    assert "__page__0" in nav_ids_page_two

    invalid = await lyrion_provider.browse(f"{lyrion_provider.instance_id}://tracks/not-a-page")
    assert invalid == []


async def test_track_lookup_falls_back_from_broken_batch_responses(
    lyrion_provider: LyrionMusicProvider,
    lyrion_fake_server: FakeLmsServer,
) -> None:
    """When a batch track lookup is incomplete, provider should retry single lookups."""
    lyrion_fake_server.clear_history()
    lyrion_fake_server.incomplete_batch_for_entities.add("titles")

    tracks = [track async for track in lyrion_provider.get_library_tracks()]

    assert len(tracks) == 10
    track_id_filters = [
        call.args[4]
        for call in lyrion_fake_server.rpc_calls
        if call.command == "titles"
        and len(call.args) > 4
        and isinstance(call.args[4], str)
        and call.args[4].startswith("track_id:")
    ]
    assert any("," in value for value in track_id_filters)
    assert any("," not in value for value in track_id_filters)


async def test_rpc_error_is_translated_to_provider_unavailable(
    lyrion_provider: LyrionMusicProvider,
    lyrion_fake_server: FakeLmsServer,
) -> None:
    """JSON-RPC command errors should map to ProviderUnavailableError."""
    lyrion_fake_server.command_errors["artists"] = (-32001, "server exploded politely")
    with pytest.raises(ProviderUnavailableError):
        await lyrion_provider.search("async", [MediaType.ARTIST], limit=5)
