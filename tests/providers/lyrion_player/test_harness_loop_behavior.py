"""Loop/repeat synchronization tests for the Lyrion player harness."""

from __future__ import annotations

import pytest

from tests.providers.lyrion.lms_server_harness import LyrionTestEndpoint
from tests.providers.lyrion.scriptable_slimproto_player import ScriptableSlimProtoPlayer
from tests.providers.lyrion_player.harness_test_support import (
    EndpointRpcClient,
    ProviderStyleRpcClient,
    playlist_repeat,
    repeat_mode_name,
    wait_for_playlist_repeat,
)


@pytest.mark.asyncio
async def test_loop_mode_bidirectional_sync_fake_backend(
    lyrion_test_endpoint: LyrionTestEndpoint,
) -> None:
    """Loop mode should stay synchronized when MA and SlimProto alternate writes."""
    if lyrion_test_endpoint.fake_server is None:
        pytest.skip("loop SlimProto path assertions are specific to the fake LMS backend")

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
    if lyrion_test_endpoint.fake_server is None:
        pytest.skip("stress sequence assertions are specific to the fake LMS backend")

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
