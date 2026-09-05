"""On-demand live LMS integration tests using a Docker-managed LMS instance."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from music_assistant_models.media_items import BrowseFolder

from music_assistant.providers.lyrion_music.provider import LyrionMusicProvider
from tests.providers.lyrion.fixtures import LyrionTestEndpoint

if TYPE_CHECKING:
    from tests.providers.lyrion.live_docker import LiveLmsEndpoint

pytest_plugins = ("tests.providers.lyrion.live_docker",)
pytestmark = [
    pytest.mark.live_lyrion_docker,
    pytest.mark.timeout(900),
]


@pytest.fixture
def lyrion_test_endpoint(
    lyrion_live_lms_endpoint: LiveLmsEndpoint,
) -> LyrionTestEndpoint:
    """Route shared provider fixture to the Docker-managed LMS endpoint."""
    return LyrionTestEndpoint(
        host=lyrion_live_lms_endpoint.host,
        port=lyrion_live_lms_endpoint.port,
        base_url=lyrion_live_lms_endpoint.base_url,
        source="docker",
        fake_server=None,
    )


async def test_live_docker_browse_root_has_expected_sections(
    lyrion_provider: LyrionMusicProvider,
) -> None:
    """Root browse should return known top-level sections."""
    root_path = f"{lyrion_provider.instance_id}://"
    root_items = await lyrion_provider.browse(root_path)
    root_ids = [item.item_id for item in root_items if isinstance(item, BrowseFolder)]
    assert root_ids == ["artists", "albums", "tracks", "playlists", "genres"]


async def test_live_docker_catalog_counts_match_fixture(
    lyrion_provider: LyrionMusicProvider,
) -> None:
    """Docker-seeded library should expose fake-LMS-equivalent shape."""
    artists = [artist async for artist in lyrion_provider.get_library_artists()]
    albums = [album async for album in lyrion_provider.get_library_albums()]
    tracks = [track async for track in lyrion_provider.get_library_tracks()]
    playlists = [playlist async for playlist in lyrion_provider.get_library_playlists()]
    genres = [genre async for genre in lyrion_provider.get_library_genres()]

    assert len(artists) == 4
    assert len(albums) == 6
    assert len(tracks) == 10
    assert len(playlists) == 2
    assert genres == ["Electro", "Lo-Fi", "Blues"]


async def test_live_docker_search_finds_seeded_records(
    lyrion_provider: LyrionMusicProvider,
) -> None:
    """Search should find known seeded artist/album/track names."""
    results = await lyrion_provider.search(
        "async",
        lyrion_provider.supported_media_types,
        limit=10,
    )
    assert any(item.name == "The Async Awaiters" for item in results.artists)
    assert any(item.name == "Awaiting Sunrise" for item in results.albums)
    assert any(item.name == "Future Is Pending" for item in results.tracks)


async def test_live_docker_playlist_tracks_are_resolved(
    lyrion_provider: LyrionMusicProvider,
) -> None:
    """Playlist lookup should return members from seeded M3U playlists."""
    playlists = [playlist async for playlist in lyrion_provider.get_library_playlists()]
    debug_playlist = next(item for item in playlists if item.name == "Debugging Bangers")

    playlist_tracks = await lyrion_provider.get_playlist_tracks(debug_playlist.item_id)
    track_names = [track.name for track in playlist_tracks]
    assert track_names == [
        "Wake Up And Smell The Exceptions",
        "Cold Start Romance",
        "Future Is Pending",
        "Breadline Top 1",
    ]
