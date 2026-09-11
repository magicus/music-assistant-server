"""Whitebox-only player harness tests that require fake LMS internals."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from tests.providers.lyrion.fake_lms_server import FakeLmsServer
from tests.providers.lyrion.lms_server_harness import LyrionTestEndpoint
from tests.providers.lyrion.scriptable_slimproto_player import ScriptableSlimProtoPlayer
from tests.providers.lyrion_player.harness_test_support import EndpointRpcClient, FakeMAProvider


class _FakeWriter:
    def __init__(self) -> None:
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        return None

    def is_closing(self) -> bool:
        return False

    def close(self) -> None:
        return None

    async def wait_closed(self) -> None:
        return None


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.usefixtures("fake_lms_server"),
]


async def test_player_seek_updates_scriptable_elapsed_time(
    lyrion_test_endpoint: LyrionTestEndpoint,
) -> None:
    """Fake harness seek should mirror LMS time in scriptable player local state."""
    player = ScriptableSlimProtoPlayer(
        endpoint=lyrion_test_endpoint,
        player_id="fake-player-time-seek",
        name="Fake Seek Player",
        model="test",
    )
    rpc_client: EndpointRpcClient | None = None
    try:
        await player.connect()
        rpc_client = EndpointRpcClient(lyrion_test_endpoint, player.rpc_player_id)

        await rpc_client.send(["time", 37])
        status = await rpc_client.send(["status", 0, 100])
        assert float(status.get("time", 0.0)) == 37.0
        assert player.elapsed_time == 37.0

        await rpc_client.send(["time", 5])
        status = await rpc_client.send(["status", 0, 100])
        assert float(status.get("time", 0.0)) == 5.0
        assert player.elapsed_time == 5.0
    finally:
        if rpc_client is not None:
            await rpc_client.close()
        await player.close()


async def test_player_connect_uses_rpc_player_id_for_live_endpoint() -> None:
    """Live-style connect should advertise the RPC player id in the HELO payload."""
    endpoint = LyrionTestEndpoint(
        host="127.0.0.1",
        port=9000,
        base_url="http://127.0.0.1:9000",
        source="docker",
    )
    player = ScriptableSlimProtoPlayer(
        endpoint=endpoint,
        player_id="test-alias",
        name="Live Player",
        model="test",
    )
    fake_writer = _FakeWriter()

    with patch(
        "tests.providers.lyrion.scriptable_slimproto_player.asyncio.open_connection",
        new=AsyncMock(return_value=(object(), fake_writer)),
    ):
        result = await player.connect()

    payload = fake_writer.writes[0]
    assert f"PlayerID={player.rpc_player_id}".encode() in payload
    assert f"PlayerID={player.player_id}".encode() not in payload
    assert result["playerid"] == player.rpc_player_id

    await player.close()


async def test_player_local_playback_state_across_multiple_sources(
    lyrion_test_endpoint: LyrionTestEndpoint,
    fake_lms_server: FakeLmsServer,
) -> None:
    """Fake harness should keep scriptable local mode/is_playing state coherent."""
    player = ScriptableSlimProtoPlayer(
        endpoint=lyrion_test_endpoint,
        player_id="fake-player-local-state",
        name="Fake Local State",
        model="test",
    )
    provider = FakeMAProvider(fake_lms_server, player.player_id)
    rpc_client: EndpointRpcClient | None = None

    await player.connect()
    try:
        rpc_client = EndpointRpcClient(lyrion_test_endpoint, player.rpc_player_id)
        assert player.mode == "stop"
        assert player.is_paused() is False
        assert player.is_playing() is False

        await provider.send_player_command(["playlistcontrol", "cmd:load", "track_id:t1"])
        await provider.send_player_command(["play"])
        assert player.is_playing() is True
        assert player.is_paused() is False

        await rpc_client.send(["pause"])
        assert player.is_playing() is True
        assert player.mode == "play"

        await provider.send_player_command(["play"])
        assert player.is_playing() is True

        await player.pause()
        assert player.is_playing() is True
    finally:
        if rpc_client is not None:
            await rpc_client.close()
        await player.close()


async def test_two_players_local_state_remains_coherent_under_mixed_pause_sources(
    lyrion_test_endpoint: LyrionTestEndpoint,
    fake_lms_server: FakeLmsServer,
) -> None:
    """Fake harness local player states should stay coherent with mixed pause inputs."""
    player_a = ScriptableSlimProtoPlayer(
        endpoint=lyrion_test_endpoint,
        player_id="fake-player-a",
        name="Fake A",
        model="test",
    )
    player_b = ScriptableSlimProtoPlayer(
        endpoint=lyrion_test_endpoint,
        player_id="fake-player-b",
        name="Fake B",
        model="test",
    )
    provider_a = FakeMAProvider(fake_lms_server, "fake-player-a")
    provider_b = FakeMAProvider(fake_lms_server, "fake-player-b")
    rpc_a: EndpointRpcClient | None = None

    await player_a.connect()
    await player_b.connect()
    try:
        rpc_a = EndpointRpcClient(lyrion_test_endpoint, player_a.rpc_player_id)

        await provider_a.send_player_command(["playlistcontrol", "cmd:load", "track_id:t1"])
        await provider_a.send_player_command(["play"])
        assert player_a.is_playing() is True
        assert player_b.is_playing() is False

        await provider_b.send_player_command(["pause"])
        assert player_b.is_playing() is False
        assert player_a.is_playing() is True

        await provider_b.send_player_command(["playlistcontrol", "cmd:load", "track_id:t1"])
        await provider_b.send_player_command(["play"])
        assert player_b.is_playing() is True
        assert player_a.is_playing() is True

        await rpc_a.send(["pause"])
        assert player_a.is_playing() is True
        assert player_b.is_playing() is True

        await player_b.pause()
        assert player_b.is_playing() is True
        assert player_a.is_playing() is True
    finally:
        if rpc_a is not None:
            await rpc_a.close()
        await player_a.close()
        await player_b.close()


async def test_fake_lms_server_builds_player_status_payload() -> None:
    """Fake LMS internals should report coherent player roster/status payloads."""
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
