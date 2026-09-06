"""Queue synchronization behavior tests for the Lyrion player harness."""

from __future__ import annotations

from collections import Counter

import pytest

from tests.providers.lyrion.lms_server_harness import LyrionTestEndpoint
from tests.providers.lyrion.scriptable_slimproto_player import ScriptableSlimProtoPlayer
from tests.providers.lyrion_player.harness_test_support import (
    EndpointRpcClient,
    ProviderStyleRpcClient,
    playlist_index,
    playlist_tracks,
    playlist_values,
    wait_for_playlist_index,
)


@pytest.mark.asyncio
@pytest.mark.live_lyrion_docker
async def test_queue_state_stays_consistent_when_three_sources_alternate_aggressively(
    lyrion_test_endpoint: LyrionTestEndpoint,
) -> None:
    """Queue transitions should remain coherent when MA/provider, direct JSON-RPC and SlimProto updates interleave."""
    player = ScriptableSlimProtoPlayer(
        endpoint=lyrion_test_endpoint,
        player_id="fake-player-queue-adversarial",
        name="Fake Queue Adversarial",
        model="test",
    )
    provider_client: ProviderStyleRpcClient | None = None
    direct_rpc_client: EndpointRpcClient | None = None
    try:
        await player.connect()
        provider_client = ProviderStyleRpcClient(lyrion_test_endpoint, player.rpc_player_id)
        direct_rpc_client = EndpointRpcClient(lyrion_test_endpoint, player.rpc_player_id)

        await provider_client.send_player_command(["playlist", "clear"])
        await direct_rpc_client.send(["playlist", "add", "http://queue.local/track-a.mp3"])
        await provider_client.send_player_command(
            ["playlist", "add", "http://queue.local/track-b.mp3"]
        )
        await direct_rpc_client.send(["playlist", "add", "http://queue.local/track-c.mp3"])

        status = await direct_rpc_client.send(["status", 0, 100])
        assert playlist_tracks(status) == 3
        assert playlist_index(status) == 0

        await provider_client.send_player_command(["playlist", "index", 1])
        status = await direct_rpc_client.send(["status", 0, 100])
        assert playlist_index(status) == 1

        await direct_rpc_client.send(["playlist", "delete", 0])
        status = await direct_rpc_client.send(["status", 0, 100])
        assert playlist_tracks(status) == 2
        assert playlist_index(status) == 0

        await player.next_track()
        status = await wait_for_playlist_index(direct_rpc_client, 1)
        assert playlist_tracks(status) == 2
        assert 0 <= playlist_index(status) <= 1

        await provider_client.send_player_command(
            ["playlist", "add", "http://queue.local/track-d.mp3"]
        )
        await direct_rpc_client.send(["playlist", "index", 2])
        await player.previous_track()
        status = await wait_for_playlist_index(direct_rpc_client, 1)
        assert playlist_tracks(status) == 3
        assert 0 <= playlist_index(status) <= 2

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
async def test_queue_duplicate_track_reordering_survives_cross_source_churn(  # noqa: PLR0915
    lyrion_test_endpoint: LyrionTestEndpoint,
) -> None:
    """A duplicate-heavy queue should keep coherent ordering/index while updates alternate across all control paths."""
    player = ScriptableSlimProtoPlayer(
        endpoint=lyrion_test_endpoint,
        player_id="fake-player-queue-dupes",
        name="Fake Queue Dupes",
        model="test",
    )

    track_a = "http://queue.local/track-a.mp3"
    track_b = "http://queue.local/track-b.mp3"

    provider_client: ProviderStyleRpcClient | None = None
    direct_rpc_client: EndpointRpcClient | None = None
    try:
        await player.connect()
        provider_client = ProviderStyleRpcClient(lyrion_test_endpoint, player.rpc_player_id)
        direct_rpc_client = EndpointRpcClient(lyrion_test_endpoint, player.rpc_player_id)

        await provider_client.send_player_command(["playlist", "clear"])

        await provider_client.send_player_command(["playlist", "add", track_a])
        await direct_rpc_client.send(["playlist", "add", track_a])
        await provider_client.send_player_command(["playlist", "add", track_a])
        await direct_rpc_client.send(["playlist", "add", track_b])
        await provider_client.send_player_command(["playlist", "add", track_a])
        await direct_rpc_client.send(["playlist", "add", track_a])

        status = await direct_rpc_client.send(["status", 0, 100])
        values = playlist_values(status)
        assert len(values) == 6
        assert values[0] == values[1] == values[2] == values[4] == values[5]
        assert values[3] != values[0]
        assert playlist_tracks(status) == 6
        assert playlist_index(status) == 0

        await player.next_track()
        await direct_rpc_client.send(["playlist", "move", 5, 2])
        await provider_client.send_player_command(["playlist", "move", 0, 4])
        await player.next_track()
        await direct_rpc_client.send(["playlist", "delete", 3])

        status = await direct_rpc_client.send(["status", 0, 100])
        values = playlist_values(status)
        assert len(values) == 5
        assert len(set(values)) == 1
        assert playlist_tracks(status) == 5
        assert 0 <= playlist_index(status) <= 4

        await provider_client.send_player_command(["playlist", "move", 4, 1])
        await player.previous_track()
        await provider_client.send_player_command(["playlist", "move", 2, 3])

        status = await direct_rpc_client.send(["status", 0, 100])
        values = playlist_values(status)
        assert len(values) == 5
        assert len(set(values)) == 1
        assert playlist_tracks(status) == 5
        assert 0 <= playlist_index(status) <= 4
    finally:
        if provider_client is not None:
            await provider_client.close()
        if direct_rpc_client is not None:
            await direct_rpc_client.close()
        await player.close()


@pytest.mark.asyncio
@pytest.mark.live_lyrion_docker
async def test_queue_duplicate_swap_and_bridge_hops_keep_index_valid(
    lyrion_test_endpoint: LyrionTestEndpoint,
) -> None:
    """Moving duplicate items across a middle sentinel should not corrupt queue length or active index bounds."""
    player = ScriptableSlimProtoPlayer(
        endpoint=lyrion_test_endpoint,
        player_id="fake-player-queue-bridge",
        name="Fake Queue Bridge",
        model="test",
    )

    track_a = "http://queue.local/track-a.mp3"
    track_b = "http://queue.local/track-b.mp3"

    provider_client: ProviderStyleRpcClient | None = None
    direct_rpc_client: EndpointRpcClient | None = None
    try:
        await player.connect()
        provider_client = ProviderStyleRpcClient(lyrion_test_endpoint, player.rpc_player_id)
        direct_rpc_client = EndpointRpcClient(lyrion_test_endpoint, player.rpc_player_id)

        await provider_client.send_player_command(["playlist", "clear"])

        await provider_client.send_player_command(["playlist", "add", track_a])
        await direct_rpc_client.send(["playlist", "add", track_a])
        await provider_client.send_player_command(["playlist", "add", track_b])
        await direct_rpc_client.send(["playlist", "add", track_a])
        await provider_client.send_player_command(["playlist", "add", track_a])

        status = await direct_rpc_client.send(["status", 0, 100])
        values = playlist_values(status)
        assert len(values) == 5
        assert values[0] == values[1] == values[3] == values[4]
        assert values[2] != values[0]
        assert playlist_tracks(status) == 5

        await provider_client.send_player_command(["playlist", "index", 2])
        await player.next_track()
        await direct_rpc_client.send(["playlist", "move", 0, 4])
        await provider_client.send_player_command(["playlist", "move", 3, 1])
        await player.previous_track()
        await direct_rpc_client.send(["playlist", "move", 2, 0])

        status = await direct_rpc_client.send(["status", 0, 100])
        values = playlist_values(status)
        counts = Counter(values)
        assert len(values) == 5
        assert sorted(counts.values()) == [1, 4]
        assert 0 <= playlist_index(status) <= 4
    finally:
        if provider_client is not None:
            await provider_client.close()
        if direct_rpc_client is not None:
            await direct_rpc_client.close()
        await player.close()
