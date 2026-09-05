"""Blackbox Lyrion provider tests runnable against fake or Docker LMS."""

from __future__ import annotations

import pytest
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import BrowseFolder

from music_assistant.providers.lyrion_music.provider import LyrionMusicProvider

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.live_lyrion_docker,
]


async def test_browse_root_has_expected_sections(
    lyrion_provider: LyrionMusicProvider,
) -> None:
    """Root browse should expose the known top-level folders."""
    root_items = await lyrion_provider.browse(f"{lyrion_provider.instance_id}://")
    root_ids = [item.item_id for item in root_items if isinstance(item, BrowseFolder)]
    assert root_ids == ["artists", "albums", "tracks", "playlists", "genres"]


async def test_library_iterators_return_seeded_catalog_shape(
    lyrion_provider: LyrionMusicProvider,
) -> None:
    """Seeded test catalog should be visible with expected entity counts."""
    artists = [artist async for artist in lyrion_provider.get_library_artists()]
    albums = [album async for album in lyrion_provider.get_library_albums()]
    tracks = [track async for track in lyrion_provider.get_library_tracks()]
    playlists = [playlist async for playlist in lyrion_provider.get_library_playlists()]
    genres = [genre async for genre in lyrion_provider.get_library_genres()]

    assert len(artists) == 4
    assert len(albums) == 6
    assert len(tracks) == 10
    assert len(playlists) == 2
    assert sorted(genres) == ["Blues", "Electro", "Lo-Fi"]


async def test_search_finds_seeded_records(
    lyrion_provider: LyrionMusicProvider,
) -> None:
    """Search should find known names from the seeded catalog."""
    results = await lyrion_provider.search(
        "async",
        lyrion_provider.supported_media_types,
        limit=10,
    )
    assert any(item.name == "The Async Awaiters" for item in results.artists)
    assert any(item.name == "Awaiting Sunrise" for item in results.albums)
    assert any(item.name == "Future Is Pending" for item in results.tracks)


async def test_playlist_tracks_are_resolved(
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


async def test_album_tracks_are_sorted_by_disc_and_track(
    lyrion_provider: LyrionMusicProvider,
) -> None:
    """Album tracks should be ordered by disc number and track number."""
    albums = [album async for album in lyrion_provider.get_library_albums()]
    album = next(item for item in albums if item.name == "Race Condition Blues")

    album_tracks = await lyrion_provider.get_album_tracks(album.item_id)
    assert [track.name for track in album_tracks] == [
        "Race You To The Lock",
        "Segfault Serenade",
    ]


async def test_get_playlist_raises_for_unknown_id(
    lyrion_provider: LyrionMusicProvider,
) -> None:
    """Unknown playlist id should raise MediaNotFoundError."""
    with pytest.raises(MediaNotFoundError):
        await lyrion_provider.get_playlist("does-not-exist")
