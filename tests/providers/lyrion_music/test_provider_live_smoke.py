# mypy: disable-error-code="arg-type"
"""
Live LMS smoke tests for lyrion_music provider.

These tests are intentionally data-agnostic and only verify that the provider
can talk to a configured real LMS endpoint without crashing.
"""

from __future__ import annotations

import pytest
from music_assistant_models.media_items import BrowseFolder

from music_assistant.providers.lyrion_music.provider import LyrionMusicProvider
from tests.providers.lyrion.fixtures import using_real_lms_from_env

pytestmark = pytest.mark.skipif(
    not using_real_lms_from_env(),
    reason="Set LYRION_TEST_LMS_URL or LYRION_TEST_LMS_HOST to run live LMS smoke tests",
)


async def test_live_browse_root_has_expected_sections(
    lyrion_provider: LyrionMusicProvider,
) -> None:
    """Root browse should always return the known top-level folders."""
    root_items = await lyrion_provider.browse(f"{lyrion_provider.instance_id}://")
    root_ids = [item.item_id for item in root_items if isinstance(item, BrowseFolder)]
    assert root_ids == ["artists", "albums", "tracks", "playlists", "genres"]


async def test_live_library_calls_do_not_crash(
    lyrion_provider: LyrionMusicProvider,
) -> None:
    """Library iterators should complete even with unknown real-world catalog shape."""
    artists = [artist async for artist in lyrion_provider.get_library_artists()]
    albums = [album async for album in lyrion_provider.get_library_albums()]
    tracks = [track async for track in lyrion_provider.get_library_tracks()]

    assert isinstance(artists, list)
    assert isinstance(albums, list)
    assert isinstance(tracks, list)


async def test_live_search_does_not_crash(
    lyrion_provider: LyrionMusicProvider,
) -> None:
    """Search endpoint should return a SearchResults object for generic query."""
    results = await lyrion_provider.search("a", lyrion_provider.supported_media_types, limit=3)
    assert results is not None
