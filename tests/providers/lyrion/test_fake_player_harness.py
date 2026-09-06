"""Tests for fake Lyrion players being backend-agnostic across fake and Docker LMS."""

from __future__ import annotations

from typing import Any

import aiohttp
import pytest

from tests.providers.lyrion.fake_lms_player import FakeSlimProtoPlayer
from tests.providers.lyrion.fake_lms_server import FakeLmsServer
from tests.providers.lyrion.lms_server_harness import LyrionTestEndpoint


class FakeMAProvider:
    """Minimal MA provider stub used to prove bidirectional play/pause sync."""

    def __init__(self, server: FakeLmsServer, player_id: str) -> None:
        """Initialize the provider stub state."""
        self.server = server
        self.player_id = player_id
        self.latest_status: dict[str, Any] = {"playerid": player_id, "mode": "stop"}
        self.server.add_player_state_listener(self._on_player_state_changed)

    def _on_player_state_changed(self, player_id: str, state: dict[str, Any]) -> None:
        """Track server-side updates as MA would do when LMS status changes."""
        if player_id != self.player_id:
            return
        self.latest_status = state

    async def send_player_command(self, command: list[str]) -> dict[str, Any]:
        """Send a command to the server, mirroring the MA provider JSON-RPC call path."""
        return await self.server.handle_jsonrpc_command(self.player_id, command)


class EndpointRpcClient:
    """Small JSON-RPC client used for backend-agnostic command injection."""

    def __init__(self, endpoint: LyrionTestEndpoint, player_id: str) -> None:
        """Initialize endpoint and player identity."""
        self._endpoint = endpoint
        self._player_id = player_id
        self._session = aiohttp.ClientSession()

    async def send(self, command: list[Any]) -> dict[str, Any]:
        """Send a raw LMS JSON-RPC command for the configured player."""
        payload = {
            "id": 1,
            "method": "slim.request",
            "params": [self._player_id, command],
        }
        async with self._session.post(
            f"{self._endpoint.base_url}/jsonrpc.js",
            json=payload,
        ) as response:
            response.raise_for_status()
            data = await response.json()
        result = data.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("JSON-RPC response is missing result payload")
        return result

    async def close(self) -> None:
        """Close the underlying HTTP session."""
        await self._session.close()


class ProviderStyleRpcClient:
    """Fake MA/provider command path through LMS JSON-RPC."""

    def __init__(self, endpoint: LyrionTestEndpoint, player_id: str) -> None:
        """Initialize provider-style client."""
        self._rpc = EndpointRpcClient(endpoint, player_id)

    async def send_player_command(self, command: list[Any]) -> dict[str, Any]:
        """Send a command as MA/provider would do."""
        return await self._rpc.send(command)

    async def close(self) -> None:
        """Close resources held by the provider-style client."""
        await self._rpc.close()


def _playlist_index(status: dict[str, Any]) -> int:
    """Read current queue index from LMS status across key-name variants."""
    if "playlist_cur_index" in status:
        return int(status["playlist_cur_index"])
    return int(status.get("playlist index", 0))


def _playlist_tracks(status: dict[str, Any]) -> int:
    """Read queue length from LMS status across key-name variants."""
    if "playlist_tracks" in status:
        return int(status["playlist_tracks"])
    return int(status.get("playlist tracks", 0))


@pytest.mark.asyncio
async def test_fake_player_registers_on_fake_lms(
    lyrion_test_endpoint: LyrionTestEndpoint,
) -> None:
    """A fake slimproto player should show up as a connected LMS player on the active backend."""
    player = FakeSlimProtoPlayer(
        endpoint=lyrion_test_endpoint,
        player_id="fake-player-1",
        name="Fake Bedroom",
        model="test",
    )

    try:
        await player.connect()
        state = (
            player.endpoint.fake_server.players["fake-player-1"]
            if player.endpoint.fake_server
            else None
        )
        if state is not None:
            assert state["connected"] == 1
            assert state["name"] == "Fake Bedroom"

        player_status = await player.request_status()
        assert player_status["playerid"] == "fake-player-1"
        assert player_status["connected"] == 1
        assert player_status["mode"] == "stop"

        await player.disconnect()
    finally:
        await player.close()


@pytest.mark.asyncio
async def test_fake_player_detects_connect_and_disconnect_events(
    lyrion_test_endpoint: LyrionTestEndpoint,
) -> None:
    """The active LMS backend should report when a slimproto player connects and disconnects."""
    player = FakeSlimProtoPlayer(
        endpoint=lyrion_test_endpoint,
        player_id="fake-player-2",
        name="Fake Office",
        model="test",
    )

    try:
        await player.connect()
        if player.endpoint.fake_server is not None:
            assert (
                player.endpoint.fake_server.slimproto_players["fake-player-2"]["connected"] is True
            )

        await player.disconnect()
        if player.endpoint.fake_server is not None:
            assert (
                player.endpoint.fake_server.slimproto_players["fake-player-2"]["connected"] is False
            )
    finally:
        await player.close()


@pytest.mark.asyncio
async def test_fake_player_and_ma_provider_sync_play_and_pause_states(
    lyrion_test_endpoint: LyrionTestEndpoint,
) -> None:
    """Both MA commands and slimproto player commands should update the same underlying LMS state."""
    player = FakeSlimProtoPlayer(
        endpoint=lyrion_test_endpoint,
        player_id="fake-player-3",
        name="Fake Kitchen",
        model="test",
    )
    await player.connect()
    try:
        if player.endpoint.fake_server is None:
            pytest.skip("This sync check specifically validates the fake LMS state model")

        provider = FakeMAProvider(player.endpoint.fake_server, "fake-player-3")

        await provider.send_player_command(["play"])
        assert player.endpoint.fake_server.players["fake-player-3"]["mode"] == "play"
        assert player.mode == "play"
        assert provider.latest_status["mode"] == "play"

        await player.pause()
        assert player.endpoint.fake_server.players["fake-player-3"]["mode"] == "pause"
        assert provider.latest_status["mode"] == "pause"
        assert player.mode == "pause"

        await player.disconnect()
    finally:
        await player.close()


@pytest.mark.asyncio
async def test_fake_player_uses_real_slimproto_button_events_for_play_and_pause(
    lyrion_test_endpoint: LyrionTestEndpoint,
) -> None:
    """Fake play and pause should be emitted as real SlimProto button events."""
    player = FakeSlimProtoPlayer(
        endpoint=lyrion_test_endpoint,
        player_id="fake-player-button",
        name="Fake Button Player",
        model="test",
    )
    await player.connect()
    try:
        if player.endpoint.fake_server is None:
            pytest.skip("Protocol-level button assertion only applies to fake backend")

        await player.play()
        assert player.endpoint.fake_server.slimproto_events[-1][0] == b"butn"
        assert player.endpoint.fake_server.players["fake-player-button"]["mode"] == "play"

        await player.pause()
        assert player.endpoint.fake_server.slimproto_events[-1][0] == b"butn"
        assert player.endpoint.fake_server.players["fake-player-button"]["mode"] == "pause"
    finally:
        await player.close()


@pytest.mark.asyncio
async def test_fake_player_reports_local_playback_state_across_multiple_sources(
    lyrion_test_endpoint: LyrionTestEndpoint,
) -> None:
    """A fake player should expose its own local state even when various sync sources update it in quick succession."""
    if lyrion_test_endpoint.fake_server is None:
        pytest.skip("local-state assertions are only meaningful for the fake LMS backend")

    player = FakeSlimProtoPlayer(
        endpoint=lyrion_test_endpoint,
        player_id="fake-player-local-state",
        name="Fake Local State",
        model="test",
    )
    provider = FakeMAProvider(lyrion_test_endpoint.fake_server, player.player_id)

    await player.connect()
    try:
        assert player.mode == "stop"
        assert player.is_paused() is False
        assert player.is_playing() is False

        await player.play()
        assert player.is_playing() is True
        assert player.is_paused() is False

        await provider.send_player_command(["pause"])
        assert player.is_paused() is True
        assert player.mode == "pause"

        await player.play()
        assert player.is_playing() is True

        await provider.send_player_command(["play"])
        assert player.is_playing() is True

        await player.pause()
        assert player.is_paused() is True
    finally:
        await player.close()


@pytest.mark.asyncio
async def test_fake_players_keep_state_in_sync_when_play_pause_sources_conflict(
    lyrion_test_endpoint: LyrionTestEndpoint,
) -> None:
    """A real-world nagging case: two players toggling state from different sources should leave the last writer in charge."""
    if lyrion_test_endpoint.fake_server is None:
        pytest.skip("the conflict case is validated against the fake LMS backend")

    player_a = FakeSlimProtoPlayer(
        endpoint=lyrion_test_endpoint,
        player_id="fake-player-a",
        name="Fake A",
        model="test",
    )
    player_b = FakeSlimProtoPlayer(
        endpoint=lyrion_test_endpoint,
        player_id="fake-player-b",
        name="Fake B",
        model="test",
    )
    provider_a = FakeMAProvider(lyrion_test_endpoint.fake_server, "fake-player-a")
    provider_b = FakeMAProvider(lyrion_test_endpoint.fake_server, "fake-player-b")

    await player_a.connect()
    await player_b.connect()
    try:
        await player_a.play()
        assert player_a.is_playing() is True
        assert player_b.is_playing() is False

        await provider_b.send_player_command(["pause"])
        assert player_b.is_paused() is True
        assert player_a.is_playing() is True

        await player_b.play()
        assert player_b.is_playing() is True
        assert player_a.is_playing() is True

        await provider_a.send_player_command(["pause"])
        assert player_a.is_paused() is True
        assert player_b.is_playing() is True

        await player_a.play()
        assert player_a.is_playing() is True
        assert player_b.is_playing() is True

        await player_b.pause()
        assert player_b.is_paused() is True
        assert player_a.is_playing() is True
    finally:
        await player_a.close()
        await player_b.close()


@pytest.mark.asyncio
@pytest.mark.live_lyrion_docker
async def test_queue_state_stays_consistent_when_three_sources_alternate_aggressively(
    lyrion_test_endpoint: LyrionTestEndpoint,
) -> None:
    """Queue transitions should remain coherent when MA/provider, direct JSON-RPC and SlimProto updates interleave."""
    player = FakeSlimProtoPlayer(
        endpoint=lyrion_test_endpoint,
        player_id="fake-player-queue-adversarial",
        name="Fake Queue Adversarial",
        model="test",
    )
    provider_client = ProviderStyleRpcClient(lyrion_test_endpoint, player.player_id)
    direct_rpc_client = EndpointRpcClient(lyrion_test_endpoint, player.player_id)

    await player.connect()
    try:
        await provider_client.send_player_command(["playlist", "clear"])
        await direct_rpc_client.send(["playlist", "add", "http://queue.local/track-a.mp3"])
        await provider_client.send_player_command(
            ["playlist", "add", "http://queue.local/track-b.mp3"]
        )
        await direct_rpc_client.send(["playlist", "add", "http://queue.local/track-c.mp3"])

        status = await player.request_status()
        assert _playlist_tracks(status) == 3
        assert _playlist_index(status) == 0

        # 1) MA/provider path changes active queue index.
        await provider_client.send_player_command(["playlist", "index", 1])
        status = await player.request_status()
        assert _playlist_index(status) == 1

        # 2) Direct LMS JSON-RPC mutates queue shape under the player.
        await direct_rpc_client.send(["playlist", "delete", 0])
        status = await player.request_status()
        assert _playlist_tracks(status) == 2
        assert _playlist_index(status) == 0

        # 3) SlimProto user button events step through the queue.
        await player.next_track()
        status = await player.request_status()
        assert _playlist_index(status) == 1

        # Alternate again across all three sources.
        await provider_client.send_player_command(
            ["playlist", "add", "http://queue.local/track-d.mp3"]
        )
        await direct_rpc_client.send(["playlist", "index", 2])
        await player.previous_track()
        status = await player.request_status()
        assert _playlist_tracks(status) == 3
        assert _playlist_index(status) == 1

        if player.endpoint.fake_server is not None:
            assert player.endpoint.fake_server.slimproto_events[-1][0] == b"butn"
    finally:
        await provider_client.close()
        await direct_rpc_client.close()
        await player.close()


@pytest.mark.asyncio
async def test_fake_player_harness_uses_same_endpoint_contract_for_real_lms() -> None:
    """The player only depends on the shared endpoint contract, not the backend implementation."""
    endpoint = LyrionTestEndpoint(
        host="127.0.0.1",
        port=9000,
        base_url="http://127.0.0.1:9000",
        source="docker",
        slimproto_port=3483,
    )

    player = FakeSlimProtoPlayer(
        endpoint=endpoint,
        player_id="docker-player-1",
        name="Docker Player",
        model="docker",
    )

    assert player.endpoint.host == "127.0.0.1"
    assert player.endpoint.slimproto_port == 3483
    assert player.player_id == "docker-player-1"


@pytest.mark.asyncio
async def test_fake_lms_server_builds_player_status_payload() -> None:
    """The fake LMS should keep its player roster and status payloads consistent for both discovery and runtime calls."""
    server = FakeLmsServer()
    await server.connect_player("test-1", "Kitchen", "test")

    result = server._build_serverstatus_result()
    assert result["player count"] == 1
    assert result["players_loop"][0]["playerid"] == "test-1"
    assert result["players_loop"][0]["connected"] == 1

    status = server._status_for_player("test-1")
    assert status["playerid"] == "test-1"
    assert status["mode"] == "stop"
    assert status["power"] == 1
