"""Tests for fake Lyrion players being backend-agnostic across fake and Docker LMS."""

from __future__ import annotations

import asyncio
from collections import Counter
from typing import Any

import aiohttp
import pytest

from tests.providers.lyrion.fake_lms_server import FakeLmsServer
from tests.providers.lyrion.lms_server_harness import LyrionTestEndpoint
from tests.providers.lyrion.scriptable_slimproto_player import ScriptableSlimProtoPlayer


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
        data: dict[str, Any] | None = None
        for attempt in range(3):
            try:
                async with self._session.post(
                    f"{self._endpoint.base_url}/jsonrpc.js",
                    json=payload,
                ) as response:
                    response.raise_for_status()
                    data = await response.json()
                break
            except aiohttp.ServerDisconnectedError:
                if attempt == 2:
                    raise
                await asyncio.sleep(0.1 * (attempt + 1))

        if data is None:
            raise RuntimeError("JSON-RPC response payload was unexpectedly empty")
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


def _playlist_values(status: dict[str, Any]) -> list[str]:
    """Return queue identity values in order from playlist_loop payload."""
    playlist_loop = status.get("playlist_loop")
    if not isinstance(playlist_loop, list):
        return []
    values: list[str] = []
    for row in playlist_loop:
        if not isinstance(row, dict):
            continue
        for key in ("url", "track_id", "title", "id"):
            raw = row.get(key)
            if raw is not None:
                values.append(str(raw))
                break
    return values


def _player_connected(status: dict[str, Any]) -> int:
    """Read player connection state across LMS key-name variants."""
    if "connected" in status:
        return int(status["connected"])
    return int(status.get("player_connected", 0))


def _playlist_repeat(status: dict[str, Any]) -> int:
    """Read repeat mode from LMS status across key-name variants."""
    if "playlist repeat" in status:
        return int(status["playlist repeat"])
    return int(status.get("playlist_repeat", 0))


def _repeat_mode_name(repeat_value: int) -> str:
    """Map LMS repeat integer to named loop mode expected by MA."""
    return {0: "none", 1: "track", 2: "playlist"}.get(repeat_value, "none")


async def _wait_for_playlist_index(
    client: EndpointRpcClient,
    expected_index: int,
    timeout: float = 2.0,
) -> dict[str, Any]:
    """Poll status until LMS reports the expected queue index."""
    deadline = asyncio.get_running_loop().time() + timeout
    latest = await client.send(["status", 0, 100])
    while asyncio.get_running_loop().time() < deadline:
        if _playlist_index(latest) == expected_index:
            return latest
        await asyncio.sleep(0.1)
        latest = await client.send(["status", 0, 100])
    return latest


async def _wait_for_playlist_repeat(
    client: EndpointRpcClient,
    expected_repeat: int,
    timeout: float = 2.0,
) -> dict[str, Any]:
    """Poll status until LMS reports the expected repeat value."""
    deadline = asyncio.get_running_loop().time() + timeout
    latest = await client.send(["status", 0, 100])
    while asyncio.get_running_loop().time() < deadline:
        if _playlist_repeat(latest) == expected_repeat:
            return latest
        await asyncio.sleep(0.1)
        latest = await client.send(["status", 0, 100])
    return latest


@pytest.mark.asyncio
async def test_fake_player_registers_on_fake_lms(
    lyrion_test_endpoint: LyrionTestEndpoint,
) -> None:
    """A fake slimproto player should show up as a connected LMS player on the active backend."""
    player = ScriptableSlimProtoPlayer(
        endpoint=lyrion_test_endpoint,
        player_id="fake-player-1",
        name="Fake Bedroom",
        model="test",
    )

    rpc_client: EndpointRpcClient | None = None
    try:
        await player.connect()
        rpc_client = EndpointRpcClient(lyrion_test_endpoint, player.rpc_player_id)
        state = (
            player.endpoint.fake_server.players["fake-player-1"]
            if player.endpoint.fake_server
            else None
        )
        if state is not None:
            assert state["connected"] == 1
            assert state["name"] == "Fake Bedroom"

        player_status = await rpc_client.send(["status", 0, 100])
        assert player_status.get("playerid", player.rpc_player_id) == player.rpc_player_id
        assert _player_connected(player_status) == 1
        assert player_status["mode"] == "stop"

        await player.disconnect()
    finally:
        if rpc_client is not None:
            await rpc_client.close()
        await player.close()


@pytest.mark.asyncio
async def test_fake_player_detects_connect_and_disconnect_events(
    lyrion_test_endpoint: LyrionTestEndpoint,
) -> None:
    """The active LMS backend should report when a slimproto player connects and disconnects."""
    player = ScriptableSlimProtoPlayer(
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
    player = ScriptableSlimProtoPlayer(
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
    player = ScriptableSlimProtoPlayer(
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
        assert player.endpoint.fake_server.slimproto_events[-1][0] == b"BUTN"
        assert player.endpoint.fake_server.players["fake-player-button"]["mode"] == "play"

        await player.pause()
        assert player.endpoint.fake_server.slimproto_events[-1][0] == b"BUTN"
        assert player.endpoint.fake_server.players["fake-player-button"]["mode"] == "pause"
    finally:
        await player.close()


@pytest.mark.asyncio
async def test_fake_player_ir_button_interface_emits_repeat_and_updates_state(
    lyrion_test_endpoint: LyrionTestEndpoint,
) -> None:
    """Named and raw IR interfaces should emit IR frames and drive repeat state updates."""
    if lyrion_test_endpoint.fake_server is None:
        pytest.skip("IR interface assertions are specific to the fake LMS backend")

    player = ScriptableSlimProtoPlayer(
        endpoint=lyrion_test_endpoint,
        player_id="fake-player-ir-interface",
        name="Fake IR Interface",
        model="test",
    )
    rpc_client: EndpointRpcClient | None = None
    try:
        await player.connect()
        rpc_client = EndpointRpcClient(lyrion_test_endpoint, player.rpc_player_id)

        await rpc_client.send(["playlist", "repeat", 0])
        status = await rpc_client.send(["status", 0, 100])
        assert _playlist_repeat(status) == 0

        await player.press_ir_button("repeat")
        status = await _wait_for_playlist_repeat(rpc_client, 1)
        assert _playlist_repeat(status) == 1
        assert lyrion_test_endpoint.fake_server.slimproto_events[-1][0] == b"IR  "

        await player.press_ir_code(0x768938C7)
        status = await _wait_for_playlist_repeat(rpc_client, 2)
        assert _playlist_repeat(status) == 2

        player.register_ir_button("repeat_alias", 0x768938C7)
        await player.press_ir_button("repeat_alias")
        status = await _wait_for_playlist_repeat(rpc_client, 0)
        assert _playlist_repeat(status) == 0
    finally:
        if rpc_client is not None:
            await rpc_client.close()
        await player.close()


@pytest.mark.asyncio
async def test_fake_player_reports_local_playback_state_across_multiple_sources(
    lyrion_test_endpoint: LyrionTestEndpoint,
) -> None:
    """A fake player should expose its own local state even when various sync sources update it in quick succession."""
    if lyrion_test_endpoint.fake_server is None:
        pytest.skip("local-state assertions are only meaningful for the fake LMS backend")

    player = ScriptableSlimProtoPlayer(
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
        assert _playlist_tracks(status) == 3
        assert _playlist_index(status) == 0

        # 1) MA/provider path changes active queue index.
        await provider_client.send_player_command(["playlist", "index", 1])
        status = await direct_rpc_client.send(["status", 0, 100])
        assert _playlist_index(status) == 1

        # 2) Direct LMS JSON-RPC mutates queue shape under the player.
        await direct_rpc_client.send(["playlist", "delete", 0])
        status = await direct_rpc_client.send(["status", 0, 100])
        assert _playlist_tracks(status) == 2
        assert _playlist_index(status) == 0

        # 3) SlimProto user button events step through the queue.
        await player.next_track()
        status = await _wait_for_playlist_index(direct_rpc_client, 1)
        assert _playlist_tracks(status) == 2
        assert 0 <= _playlist_index(status) <= 1

        # Alternate again across all three sources.
        await provider_client.send_player_command(
            ["playlist", "add", "http://queue.local/track-d.mp3"]
        )
        await direct_rpc_client.send(["playlist", "index", 2])
        await player.previous_track()
        status = await _wait_for_playlist_index(direct_rpc_client, 1)
        assert _playlist_tracks(status) == 3
        assert 0 <= _playlist_index(status) <= 2

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
async def test_queue_duplicate_track_reordering_survives_cross_source_churn(
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

        # Build: A, A, A, B, A, A (alternating MA/provider and direct JSON-RPC).
        await provider_client.send_player_command(["playlist", "add", track_a])
        await direct_rpc_client.send(["playlist", "add", track_a])
        await provider_client.send_player_command(["playlist", "add", track_a])
        await direct_rpc_client.send(["playlist", "add", track_b])
        await provider_client.send_player_command(["playlist", "add", track_a])
        await direct_rpc_client.send(["playlist", "add", track_a])

        status = await direct_rpc_client.send(["status", 0, 100])
        values = _playlist_values(status)
        assert len(values) == 6
        assert values[0] == values[1] == values[2] == values[4] == values[5]
        assert values[3] != values[0]
        assert _playlist_tracks(status) == 6
        assert _playlist_index(status) == 0

        # SlimProto rewires active index while queue reshapes around duplicates.
        await player.next_track()
        await direct_rpc_client.send(["playlist", "move", 5, 2])
        await provider_client.send_player_command(["playlist", "move", 0, 4])
        await player.next_track()
        await direct_rpc_client.send(["playlist", "delete", 3])

        status = await direct_rpc_client.send(["status", 0, 100])
        values = _playlist_values(status)
        assert len(values) == 5
        assert len(set(values)) == 1
        assert _playlist_tracks(status) == 5
        assert 0 <= _playlist_index(status) <= 4

        # Cross the former separator boundary and swap through equal neighbors.
        await provider_client.send_player_command(["playlist", "move", 4, 1])
        await player.previous_track()
        await provider_client.send_player_command(["playlist", "move", 2, 3])

        status = await direct_rpc_client.send(["status", 0, 100])
        values = _playlist_values(status)
        assert len(values) == 5
        assert len(set(values)) == 1
        assert _playlist_tracks(status) == 5
        assert 0 <= _playlist_index(status) <= 4
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

        # Build: A, A, B, A, A then churn by alternating all three sources.
        await provider_client.send_player_command(["playlist", "add", track_a])
        await direct_rpc_client.send(["playlist", "add", track_a])
        await provider_client.send_player_command(["playlist", "add", track_b])
        await direct_rpc_client.send(["playlist", "add", track_a])
        await provider_client.send_player_command(["playlist", "add", track_a])

        status = await direct_rpc_client.send(["status", 0, 100])
        values = _playlist_values(status)
        assert len(values) == 5
        assert values[0] == values[1] == values[3] == values[4]
        assert values[2] != values[0]
        assert _playlist_tracks(status) == 5

        await provider_client.send_player_command(["playlist", "index", 2])
        await player.next_track()
        await direct_rpc_client.send(["playlist", "move", 0, 4])
        await provider_client.send_player_command(["playlist", "move", 3, 1])
        await player.previous_track()
        await direct_rpc_client.send(["playlist", "move", 2, 0])

        status = await direct_rpc_client.send(["status", 0, 100])
        values = _playlist_values(status)
        counts = Counter(values)
        assert len(values) == 5
        assert sorted(counts.values()) == [1, 4]
        assert 0 <= _playlist_index(status) <= 4
    finally:
        if provider_client is not None:
            await provider_client.close()
        if direct_rpc_client is not None:
            await direct_rpc_client.close()
        await player.close()


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
        assert _playlist_repeat(status) == 0
        assert _repeat_mode_name(_playlist_repeat(status)) == "none"

        # MA/provider path: set track repeat.
        await provider_client.send_player_command(["playlist", "repeat", 1])
        status = await direct_rpc_client.send(["status", 0, 100])
        assert _playlist_repeat(status) == 1
        assert _repeat_mode_name(_playlist_repeat(status)) == "track"

        # SlimProto path: one repeat button press should advance to playlist repeat.
        await player.toggle_repeat()
        status = await _wait_for_playlist_repeat(direct_rpc_client, 2)
        assert _playlist_repeat(status) == 2
        assert _repeat_mode_name(_playlist_repeat(status)) == "playlist"

        # MA/provider path: explicitly force none.
        await provider_client.send_player_command(["playlist", "repeat", 0])
        status = await direct_rpc_client.send(["status", 0, 100])
        assert _playlist_repeat(status) == 0
        assert _repeat_mode_name(_playlist_repeat(status)) == "none"

        # Multi-toggle sequence with last-writer-wins checks after each step.
        await player.toggle_repeat()  # none -> track
        status = await _wait_for_playlist_repeat(direct_rpc_client, 1)
        assert _playlist_repeat(status) == 1

        await provider_client.send_player_command(["playlist", "repeat", 2])
        status = await direct_rpc_client.send(["status", 0, 100])
        assert _playlist_repeat(status) == 2

        await player.toggle_repeat()  # playlist -> none
        status = await _wait_for_playlist_repeat(direct_rpc_client, 0)
        assert _playlist_repeat(status) == 0

        await provider_client.send_player_command(["playlist", "repeat", 1])
        status = await direct_rpc_client.send(["status", 0, 100])
        assert _playlist_repeat(status) == 1

        await player.toggle_repeat()  # track -> playlist
        status = await _wait_for_playlist_repeat(direct_rpc_client, 2)
        assert _playlist_repeat(status) == 2
        assert _repeat_mode_name(_playlist_repeat(status)) == "playlist"
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
        assert _playlist_repeat(status) == 0

        await provider_client.send_player_command(["playlist", "repeat", 1])
        status = await direct_rpc_client.send(["status", 0, 100])
        assert _playlist_repeat(status) == 1

        # SlimProto write should override the MA/provider-set value.
        await player.toggle_repeat()
        status = await _wait_for_playlist_repeat(direct_rpc_client, 2)
        assert _playlist_repeat(status) == 2
        assert _repeat_mode_name(_playlist_repeat(status)) == "playlist"

        await provider_client.send_player_command(["playlist", "repeat", 0])
        status = await direct_rpc_client.send(["status", 0, 100])
        assert _playlist_repeat(status) == 0

        await player.toggle_repeat()
        status = await _wait_for_playlist_repeat(direct_rpc_client, 1)
        assert _playlist_repeat(status) == 1

        await player.toggle_repeat()
        status = await _wait_for_playlist_repeat(direct_rpc_client, 2)
        assert _playlist_repeat(status) == 2
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

            status = await _wait_for_playlist_repeat(direct_rpc_client, expected)
            assert _playlist_repeat(status) == expected

        final_status = await direct_rpc_client.send(["status", 0, 100])
        assert _playlist_repeat(final_status) == 2
        assert _repeat_mode_name(_playlist_repeat(final_status)) == "playlist"
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
        status = await _wait_for_playlist_repeat(direct_rpc_client, 0)
        assert _playlist_repeat(status) == 0

        await player.toggle_repeat()
        status = await _wait_for_playlist_repeat(direct_rpc_client, 1)
        assert _playlist_repeat(status) == 1

        await player.toggle_repeat()
        status = await _wait_for_playlist_repeat(direct_rpc_client, 2)
        assert _playlist_repeat(status) == 2

        await player.toggle_repeat()
        status = await _wait_for_playlist_repeat(direct_rpc_client, 0)
        assert _playlist_repeat(status) == 0
    finally:
        if provider_client is not None:
            await provider_client.close()
        if direct_rpc_client is not None:
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

    player = ScriptableSlimProtoPlayer(
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
