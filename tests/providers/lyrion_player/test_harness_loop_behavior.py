"""Loop/repeat synchronization tests for the Lyrion player harness."""

from __future__ import annotations

import pytest

from tests.providers.lyrion.lms_server_harness import LyrionTestEndpoint
from tests.providers.lyrion.scriptable_slimproto_player import ScriptableSlimProtoPlayer
from tests.providers.lyrion_player.harness_test_support import (
    EndpointRpcClient,
    ProviderStyleRpcClient,
    playlist_repeat,
    playlist_shuffle,
    playlist_values,
    repeat_mode_name,
    wait_for_playlist_repeat,
    wait_for_playlist_shuffle,
)


@pytest.mark.asyncio
async def test_loop_mode_bidirectional_sync_fake_backend(
    lyrion_test_endpoint: LyrionTestEndpoint,
) -> None:
    """Loop mode should stay synchronized when MA and SlimProto alternate writes."""
    player = ScriptableSlimProtoPlayer(
        endpoint=lyrion_test_endpoint,
        player_id="fake-player-loop-sync",
        name="Fake Loop Sync",
        model="test",
    )
    provider_client: ProviderStyleRpcClient | None = None
    direct_rpc_client: EndpointRpcClient | None = None
    try:
        await player.connect()
        provider_client = ProviderStyleRpcClient(lyrion_test_endpoint, player.rpc_player_id)
        direct_rpc_client = EndpointRpcClient(lyrion_test_endpoint, player.rpc_player_id)

        status = await direct_rpc_client.send(["status", 0, 100])
        assert playlist_repeat(status) == 0
        assert repeat_mode_name(playlist_repeat(status)) == "none"

        await provider_client.send_player_command(["playlist", "repeat", 1])
        status = await direct_rpc_client.send(["status", 0, 100])
        assert playlist_repeat(status) == 1
        assert repeat_mode_name(playlist_repeat(status)) == "track"

        await player.toggle_repeat()
        status = await wait_for_playlist_repeat(direct_rpc_client, 2)
        assert playlist_repeat(status) == 2
        assert repeat_mode_name(playlist_repeat(status)) == "playlist"

        await provider_client.send_player_command(["playlist", "repeat", 0])
        status = await direct_rpc_client.send(["status", 0, 100])
        assert playlist_repeat(status) == 0
        assert repeat_mode_name(playlist_repeat(status)) == "none"

        await player.toggle_repeat()
        status = await wait_for_playlist_repeat(direct_rpc_client, 1)
        assert playlist_repeat(status) == 1

        await provider_client.send_player_command(["playlist", "repeat", 2])
        status = await direct_rpc_client.send(["status", 0, 100])
        assert playlist_repeat(status) == 2

        await player.toggle_repeat()
        status = await wait_for_playlist_repeat(direct_rpc_client, 0)
        assert playlist_repeat(status) == 0

        await provider_client.send_player_command(["playlist", "repeat", 1])
        status = await direct_rpc_client.send(["status", 0, 100])
        assert playlist_repeat(status) == 1

        await player.toggle_repeat()
        status = await wait_for_playlist_repeat(direct_rpc_client, 2)
        assert playlist_repeat(status) == 2
        assert repeat_mode_name(playlist_repeat(status)) == "playlist"
    finally:
        if provider_client is not None:
            await provider_client.close()
        if direct_rpc_client is not None:
            await direct_rpc_client.close()
        await player.close()


@pytest.mark.asyncio
@pytest.mark.live_lyrion_docker
async def test_loop_mode_last_writer_wins_live_backend(
    lyrion_test_endpoint: LyrionTestEndpoint,
) -> None:
    """Live LMS should keep repeat mode coherent as MA and SlimProto alternate repeat writes."""
    player = ScriptableSlimProtoPlayer(
        endpoint=lyrion_test_endpoint,
        player_id="fake-player-loop-sync-live",
        name="Fake Loop Sync Live",
        model="test",
    )
    provider_client: ProviderStyleRpcClient | None = None
    direct_rpc_client: EndpointRpcClient | None = None
    try:
        await player.connect()
        provider_client = ProviderStyleRpcClient(lyrion_test_endpoint, player.rpc_player_id)
        direct_rpc_client = EndpointRpcClient(lyrion_test_endpoint, player.rpc_player_id)

        await provider_client.send_player_command(["playlist", "repeat", 0])
        status = await direct_rpc_client.send(["status", 0, 100])
        assert playlist_repeat(status) == 0

        await provider_client.send_player_command(["playlist", "repeat", 1])
        status = await direct_rpc_client.send(["status", 0, 100])
        assert playlist_repeat(status) == 1

        await player.toggle_repeat()
        status = await wait_for_playlist_repeat(direct_rpc_client, 2)
        assert playlist_repeat(status) == 2
        assert repeat_mode_name(playlist_repeat(status)) == "playlist"

        await provider_client.send_player_command(["playlist", "repeat", 0])
        status = await direct_rpc_client.send(["status", 0, 100])
        assert playlist_repeat(status) == 0

        await player.toggle_repeat()
        status = await wait_for_playlist_repeat(direct_rpc_client, 1)
        assert playlist_repeat(status) == 1

        await player.toggle_repeat()
        status = await wait_for_playlist_repeat(direct_rpc_client, 2)
        assert playlist_repeat(status) == 2
    finally:
        if provider_client is not None:
            await provider_client.close()
        if direct_rpc_client is not None:
            await direct_rpc_client.close()
        await player.close()


@pytest.mark.asyncio
async def test_loop_mode_stress_sequence_fake_backend(
    lyrion_test_endpoint: LyrionTestEndpoint,
) -> None:
    """Repeat state should remain coherent under an adversarial mixed-source write sequence."""
    player = ScriptableSlimProtoPlayer(
        endpoint=lyrion_test_endpoint,
        player_id="fake-player-loop-stress",
        name="Fake Loop Stress",
        model="test",
    )
    provider_client: ProviderStyleRpcClient | None = None
    direct_rpc_client: EndpointRpcClient | None = None
    try:
        await player.connect()
        provider_client = ProviderStyleRpcClient(lyrion_test_endpoint, player.rpc_player_id)
        direct_rpc_client = EndpointRpcClient(lyrion_test_endpoint, player.rpc_player_id)

        steps: list[tuple[str, int]] = [
            ("ma:0", 0),
            ("sp", 1),
            ("rpc:2", 2),
            ("sp", 0),
            ("ma:1", 1),
            ("sp", 2),
            ("rpc:0", 0),
            ("sp", 1),
            ("ma:2", 2),
            ("sp", 0),
            ("rpc:1", 1),
            ("sp", 2),
        ]

        for action, expected in steps:
            if action == "sp":
                await player.toggle_repeat()
            elif action.startswith("ma:"):
                await provider_client.send_player_command(
                    ["playlist", "repeat", int(action.split(":", 1)[1])]
                )
            else:
                await direct_rpc_client.send(["playlist", "repeat", int(action.split(":", 1)[1])])

            status = await wait_for_playlist_repeat(direct_rpc_client, expected)
            assert playlist_repeat(status) == expected

        final_status = await direct_rpc_client.send(["status", 0, 100])
        assert playlist_repeat(final_status) == 2
        assert repeat_mode_name(playlist_repeat(final_status)) == "playlist"
    finally:
        if provider_client is not None:
            await provider_client.close()
        if direct_rpc_client is not None:
            await direct_rpc_client.close()
        await player.close()


@pytest.mark.asyncio
@pytest.mark.live_lyrion_docker
async def test_loop_mode_slimproto_triple_toggle_live_backend(
    lyrion_test_endpoint: LyrionTestEndpoint,
) -> None:
    """Three consecutive SlimProto repeat presses should cycle 0 -> 1 -> 2 -> 0 on live LMS."""
    player = ScriptableSlimProtoPlayer(
        endpoint=lyrion_test_endpoint,
        player_id="fake-player-loop-triple-live",
        name="Fake Loop Triple Live",
        model="test",
    )
    provider_client: ProviderStyleRpcClient | None = None
    direct_rpc_client: EndpointRpcClient | None = None
    try:
        await player.connect()
        provider_client = ProviderStyleRpcClient(lyrion_test_endpoint, player.rpc_player_id)
        direct_rpc_client = EndpointRpcClient(lyrion_test_endpoint, player.rpc_player_id)

        await provider_client.send_player_command(["playlist", "repeat", 0])
        status = await wait_for_playlist_repeat(direct_rpc_client, 0)
        assert playlist_repeat(status) == 0

        await player.toggle_repeat()
        status = await wait_for_playlist_repeat(direct_rpc_client, 1)
        assert playlist_repeat(status) == 1

        await player.toggle_repeat()
        status = await wait_for_playlist_repeat(direct_rpc_client, 2)
        assert playlist_repeat(status) == 2

        await player.toggle_repeat()
        status = await wait_for_playlist_repeat(direct_rpc_client, 0)
        assert playlist_repeat(status) == 0
    finally:
        if provider_client is not None:
            await provider_client.close()
        if direct_rpc_client is not None:
            await direct_rpc_client.close()
        await player.close()


@pytest.mark.asyncio
async def test_shuffle_mode_bidirectional_sync_fake_backend(
    lyrion_test_endpoint: LyrionTestEndpoint,
) -> None:
    """Shuffle mode should stay coherent across MA writes and SlimProto toggle presses."""
    player = ScriptableSlimProtoPlayer(
        endpoint=lyrion_test_endpoint,
        player_id="fake-player-shuffle-sync",
        name="Fake Shuffle Sync",
        model="test",
    )
    provider_client: ProviderStyleRpcClient | None = None
    direct_rpc_client: EndpointRpcClient | None = None
    try:
        await player.connect()
        provider_client = ProviderStyleRpcClient(lyrion_test_endpoint, player.rpc_player_id)
        direct_rpc_client = EndpointRpcClient(lyrion_test_endpoint, player.rpc_player_id)

        await provider_client.send_player_command(["playlist", "clear"])
        for suffix in ("a", "b", "c", "d"):
            await provider_client.send_player_command(
                ["playlist", "add", f"http://queue.local/shuffle-{suffix}.mp3"]
            )

        await provider_client.send_player_command(["playlist", "shuffle", 0])
        status = await wait_for_playlist_shuffle(direct_rpc_client, 0)
        assert playlist_shuffle(status) == 0

        await provider_client.send_player_command(["playlist", "shuffle", 1])
        status = await wait_for_playlist_shuffle(direct_rpc_client, 1)
        assert playlist_shuffle(status) == 1
        order_tracks = playlist_values(status)

        await player.toggle_shuffle()
        status = await wait_for_playlist_shuffle(direct_rpc_client, 2)
        assert playlist_shuffle(status) == 2
        order_albums = playlist_values(status)
        assert order_albums != order_tracks

        await player.toggle_shuffle()
        status = await wait_for_playlist_shuffle(direct_rpc_client, 0)
        assert playlist_shuffle(status) == 0

        await player.toggle_shuffle()
        status = await wait_for_playlist_shuffle(direct_rpc_client, 1)
        assert playlist_shuffle(status) == 1

        if player.endpoint.fake_server is not None:
            assert player.endpoint.fake_server.slimproto_events[-1][0] == b"IR  "
    finally:
        if provider_client is not None:
            await provider_client.close()
        if direct_rpc_client is not None:
            await direct_rpc_client.close()
        await player.close()


@pytest.mark.asyncio
@pytest.mark.live_lyrion_docker
async def test_shuffle_mode_tracks_album_transition_live_backend(
    lyrion_test_endpoint: LyrionTestEndpoint,
) -> None:
    """Live LMS should expose 1<->2 shuffle transitions via SlimProto and keep shuffle enabled semantics."""
    player = ScriptableSlimProtoPlayer(
        endpoint=lyrion_test_endpoint,
        player_id="fake-player-shuffle-live",
        name="Fake Shuffle Live",
        model="test",
    )
    provider_client: ProviderStyleRpcClient | None = None
    direct_rpc_client: EndpointRpcClient | None = None
    try:
        await player.connect()
        provider_client = ProviderStyleRpcClient(lyrion_test_endpoint, player.rpc_player_id)
        direct_rpc_client = EndpointRpcClient(lyrion_test_endpoint, player.rpc_player_id)

        await provider_client.send_player_command(["playlist", "clear"])
        for suffix in ("a", "b", "c", "d"):
            await provider_client.send_player_command(
                ["playlist", "add", f"http://queue.local/live-shuffle-{suffix}.mp3"]
            )

        await provider_client.send_player_command(["playlist", "shuffle", 1])
        status = await wait_for_playlist_shuffle(direct_rpc_client, 1)
        order_tracks = playlist_values(status)
        timestamp_tracks = float(status.get("playlist_timestamp", 0.0) or 0.0)

        await provider_client.send_player_command(["playlist", "shuffle", 2])
        status = await wait_for_playlist_shuffle(direct_rpc_client, 2)
        order_albums = playlist_values(status)
        timestamp_albums = float(status.get("playlist_timestamp", 0.0) or 0.0)

        # Both LMS modes 1 and 2 represent MA shuffle enabled, but 1<->2 may reshuffle entries.
        assert playlist_shuffle(status) in (1, 2)
        assert len(order_tracks) == len(order_albums) >= 4
        assert sorted(order_tracks) == sorted(order_albums)
        assert order_albums != order_tracks or timestamp_albums != timestamp_tracks

        await player.toggle_shuffle()
        status = await wait_for_playlist_shuffle(direct_rpc_client, 0)
        assert playlist_shuffle(status) == 0
    finally:
        if provider_client is not None:
            await provider_client.close()
        if direct_rpc_client is not None:
            await direct_rpc_client.close()
        await player.close()
