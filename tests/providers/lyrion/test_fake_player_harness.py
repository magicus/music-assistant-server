"""Tests for fake Lyrion players being backend-agnostic across fake and Docker LMS."""

from __future__ import annotations

from typing import Any

import pytest

from tests.providers.lyrion.fake_lms_player import FakeSlimProtoPlayer
from tests.providers.lyrion.fake_lms_server import FakeLmsServer
from tests.providers.lyrion.lms_server_harness import FakeLmsServerHarness, LyrionTestEndpoint


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


@pytest.mark.asyncio
async def test_fake_player_registers_on_fake_lms(unused_tcp_port_factory) -> None:
    """A fake slimproto player should show up as a connected LMS player on the fake server."""
    harness = FakeLmsServerHarness(unused_tcp_port_factory)
    await harness.start()
    try:
        player = FakeSlimProtoPlayer(
            endpoint=harness.endpoint,
            player_id="fake-player-1",
            name="Fake Bedroom",
            model="test",
        )

        await player.connect()
        state = harness.fake_server.players["fake-player-1"]
        assert state["connected"] == 1
        assert state["name"] == "Fake Bedroom"

        player_status = await player.request_status()
        assert player_status["playerid"] == "fake-player-1"
        assert player_status["connected"] == 1
        assert player_status["mode"] == "stop"

        await player.disconnect()
        await player.close()
    finally:
        await harness.stop()


@pytest.mark.asyncio
async def test_fake_player_detects_connect_and_disconnect_events(unused_tcp_port_factory) -> None:
    """The fake LMS should report when a slimproto player connects and disconnects."""
    harness = FakeLmsServerHarness(unused_tcp_port_factory)
    await harness.start()
    try:
        player = FakeSlimProtoPlayer(
            endpoint=harness.endpoint,
            player_id="fake-player-2",
            name="Fake Office",
            model="test",
        )

        await player.connect()
        assert harness.fake_server.slimproto_players["fake-player-2"]["connected"] is True

        await player.disconnect()
        assert harness.fake_server.slimproto_players["fake-player-2"]["connected"] is False

        await player.close()
    finally:
        await harness.stop()


@pytest.mark.asyncio
async def test_fake_player_and_ma_provider_sync_play_and_pause_states(
    unused_tcp_port_factory,
) -> None:
    """Both MA commands and slimproto player commands should update the same underlying LMS state."""
    harness = FakeLmsServerHarness(unused_tcp_port_factory)
    await harness.start()
    try:
        player = FakeSlimProtoPlayer(
            endpoint=harness.endpoint,
            player_id="fake-player-3",
            name="Fake Kitchen",
            model="test",
        )
        await player.connect()
        provider = FakeMAProvider(harness.fake_server, "fake-player-3")

        await provider.send_player_command(["play"])
        assert harness.fake_server.players["fake-player-3"]["mode"] == "play"
        assert player.mode == "play"
        assert provider.latest_status["mode"] == "play"

        await player.pause()
        assert harness.fake_server.players["fake-player-3"]["mode"] == "pause"
        assert provider.latest_status["mode"] == "pause"
        assert player.mode == "pause"

        await player.disconnect()
        await player.close()
    finally:
        await harness.stop()


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
