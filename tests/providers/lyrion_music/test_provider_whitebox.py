"""Whitebox-only Lyrion music provider tests requiring fake LMS internals."""

from __future__ import annotations

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import ProviderUnavailableError

from music_assistant.providers.lyrion_music.provider import LyrionMusicProvider
from tests.providers.lyrion.fake_lms_server import FakeLmsServer
from tests.providers.lyrion.lms_server_harness import LyrionTestEndpoint

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.usefixtures("lyrion_fake_server"),
]


async def test_handle_async_init_calls_serverstatus(
    lyrion_provider: LyrionMusicProvider,
    lyrion_fake_server: FakeLmsServer,
) -> None:
    """Provider init should validate LMS endpoint via serverstatus RPC."""
    del lyrion_provider
    assert any(call.command == "serverstatus" for call in lyrion_fake_server.rpc_calls)


async def test_library_iterators_issue_expected_entity_rpcs(
    lyrion_provider: LyrionMusicProvider,
    lyrion_fake_server: FakeLmsServer,
) -> None:
    """Library iterator calls should issue the expected LMS entity RPCs."""
    lyrion_fake_server.clear_history()

    artists = [artist async for artist in lyrion_provider.get_library_artists()]
    albums = [album async for album in lyrion_provider.get_library_albums()]
    tracks = [track async for track in lyrion_provider.get_library_tracks()]
    playlists = [playlist async for playlist in lyrion_provider.get_library_playlists()]

    assert len(artists) == 4
    assert len(albums) == 6
    assert len(tracks) == 10
    assert len(playlists) == 2
    assert any(call.command == "artists" for call in lyrion_fake_server.rpc_calls)
    assert any(call.command == "albums" for call in lyrion_fake_server.rpc_calls)
    assert any(call.command == "titles" for call in lyrion_fake_server.rpc_calls)


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
