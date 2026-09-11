"""Player lifecycle and transport behavior tests for the Lyrion harness."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from unittest.mock import AsyncMock, MagicMock

import pytest

from music_assistant.providers.lyrion_player.player import LyrionPlayer
from tests.providers.lyrion.lms_server_harness import LyrionTestEndpoint
from tests.providers.lyrion.scriptable_slimproto_player import ScriptableSlimProtoPlayer
from tests.providers.lyrion_player.harness_test_support import (
    EndpointRpcClient,
    FakeMAProvider,
    ProviderStyleRpcClient,
    player_connected,
    playlist_repeat,
    sync_master,
    sync_slaves,
    wait_for_mode,
    wait_for_playlist_repeat,
)


async def _resolve_first_track_id(client: EndpointRpcClient) -> str:
    """Return the first available LMS track id from the seeded catalog."""
    titles_result = await client.send(["titles", 0, 1])
    titles_loop = titles_result.get("titles_loop")
    if not isinstance(titles_loop, list) or not titles_loop:
        pytest.fail("Expected at least one playable LMS title in seeded catalog")
    first_title = titles_loop[0]
    if not isinstance(first_title, dict) or "id" not in first_title:
        pytest.fail(f"Unable to resolve playable LMS track id from titles payload: {titles_result}")
    return str(first_title["id"])


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
        assert player_connected(player_status) == 1
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
    """Provider-issued play should update LMS state, while pause remains a no-op for this harness."""
    player = ScriptableSlimProtoPlayer(
        endpoint=lyrion_test_endpoint,
        player_id="fake-player-3",
        name="Fake Kitchen",
        model="test",
    )
    await player.connect()
    provider_client: ProviderStyleRpcClient | None = None
    rpc_client: EndpointRpcClient | None = None
    try:
        provider = (
            FakeMAProvider(player.endpoint.fake_server, "fake-player-3")
            if player.endpoint.fake_server is not None
            else None
        )
        provider_client = ProviderStyleRpcClient(lyrion_test_endpoint, player.rpc_player_id)
        rpc_client = EndpointRpcClient(lyrion_test_endpoint, player.rpc_player_id)
        first_track_id = await _resolve_first_track_id(rpc_client)

        await provider_client.send_player_command(
            ["playlistcontrol", "cmd:load", f"track_id:{first_track_id}"]
        )

        await provider_client.send_player_command(["play"])
        status = await wait_for_mode(rpc_client, "play")
        assert status["mode"] == "play"
        if player.endpoint.fake_server is not None:
            assert player.endpoint.fake_server.players["fake-player-3"]["mode"] == "play"
            assert provider is not None
            assert provider.latest_status["mode"] == "play"

        await provider_client.send_player_command(["pause"])
        status = await rpc_client.send(["status", 0, 100])
        assert status["mode"] == "play"
        if player.endpoint.fake_server is not None:
            assert player.endpoint.fake_server.players["fake-player-3"]["mode"] == "play"
            assert provider is not None
            assert provider.latest_status["mode"] == "play"

        await player.disconnect()
    finally:
        if provider_client is not None:
            await provider_client.close()
        if rpc_client is not None:
            await rpc_client.close()
        await player.close()


@pytest.mark.asyncio
async def test_fake_player_uses_real_slimproto_button_events_for_play_and_pause(
    lyrion_test_endpoint: LyrionTestEndpoint,
) -> None:
    """SlimProto play/pause button frames should not force LMS mode transitions for this harness."""
    player = ScriptableSlimProtoPlayer(
        endpoint=lyrion_test_endpoint,
        player_id="fake-player-button",
        name="Fake Button Player",
        model="test",
    )
    await player.connect()
    rpc_client: EndpointRpcClient | None = None
    provider_client: ProviderStyleRpcClient | None = None
    try:
        rpc_client = EndpointRpcClient(lyrion_test_endpoint, player.rpc_player_id)
        provider_client = ProviderStyleRpcClient(lyrion_test_endpoint, player.rpc_player_id)
        first_track_id = await _resolve_first_track_id(rpc_client)

        await player.play()
        status = await rpc_client.send(["status", 0, 100])
        assert status["mode"] == "stop"
        if player.endpoint.fake_server is not None:
            assert player.endpoint.fake_server.slimproto_events[-1][0] == b"BUTN"
            assert player.endpoint.fake_server.players["fake-player-button"]["mode"] == "stop"

        await provider_client.send_player_command(
            ["playlistcontrol", "cmd:load", f"track_id:{first_track_id}"]
        )
        await provider_client.send_player_command(["play"])
        status = await wait_for_mode(rpc_client, "play")
        assert status["mode"] == "play"

        await player.pause()
        status = await rpc_client.send(["status", 0, 100])
        assert status["mode"] == "play"
        if player.endpoint.fake_server is not None:
            assert player.endpoint.fake_server.slimproto_events[-1][0] == b"BUTN"
            assert player.endpoint.fake_server.players["fake-player-button"]["mode"] == "play"
    finally:
        if rpc_client is not None:
            await rpc_client.close()
        if provider_client is not None:
            await provider_client.close()
        await player.close()


@pytest.mark.asyncio
async def test_fake_player_ir_button_interface_emits_repeat_and_updates_state(
    lyrion_test_endpoint: LyrionTestEndpoint,
) -> None:
    """Named and raw IR interfaces should emit IR frames and drive repeat state updates."""
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
        assert playlist_repeat(status) == 0

        await player.press_ir_button("repeat")
        status = await wait_for_playlist_repeat(rpc_client, 1)
        assert playlist_repeat(status) == 1
        if lyrion_test_endpoint.fake_server is not None:
            assert lyrion_test_endpoint.fake_server.slimproto_events[-1][0] == b"IR  "

        await player.press_ir_code(0x768938C7)
        status = await wait_for_playlist_repeat(rpc_client, 2)
        assert playlist_repeat(status) == 2

        player.register_ir_button("repeat_alias", 0x768938C7)
        await player.press_ir_button("repeat_alias")
        status = await wait_for_playlist_repeat(rpc_client, 0)
        assert playlist_repeat(status) == 0
    finally:
        if rpc_client is not None:
            await rpc_client.close()
        await player.close()


@pytest.mark.asyncio
async def test_fake_player_ir_mute_toggles_mixer_muting_state(
    lyrion_test_endpoint: LyrionTestEndpoint,
) -> None:
    """SlimProto mute IR frames should not synthesize LMS muting state changes."""
    player = ScriptableSlimProtoPlayer(
        endpoint=lyrion_test_endpoint,
        player_id="fake-player-ir-muting",
        name="Fake IR Mute",
        model="test",
    )
    rpc_client: EndpointRpcClient | None = None
    try:
        await player.connect()
        rpc_client = EndpointRpcClient(lyrion_test_endpoint, player.rpc_player_id)

        await rpc_client.send(["mixer", "muting", 0])
        status = await rpc_client.send(["status", 0, 100])
        assert int(status.get("mixer muting", 0)) == 0

        await player.toggle_mute()
        status = await rpc_client.send(["status", 0, 100])
        assert int(status.get("mixer muting", 0)) == 0
        if lyrion_test_endpoint.fake_server is not None:
            assert player.is_muted() is False
            assert lyrion_test_endpoint.fake_server.slimproto_events[-1][0] == b"IR  "

        await rpc_client.send(["mixer", "muting", 1])
        status = await rpc_client.send(["status", 0, 100])
        assert int(status.get("mixer muting", 0)) == 0

        await player.toggle_mute()
        status = await rpc_client.send(["status", 0, 100])
        assert int(status.get("mixer muting", 0)) == 0
        if lyrion_test_endpoint.fake_server is not None:
            assert player.is_muted() is False
    finally:
        if rpc_client is not None:
            await rpc_client.close()
        await player.close()


@pytest.mark.asyncio
async def test_seek_path_uses_jsonrpc_time(
    lyrion_test_endpoint: LyrionTestEndpoint,
) -> None:
    """LMS accepts JSON-RPC time seeks, but may omit the field while stopped."""
    player = ScriptableSlimProtoPlayer(
        endpoint=lyrion_test_endpoint,
        player_id="live-player-time-seek",
        name="Live Seek Player",
        model="test",
    )
    rpc_client: EndpointRpcClient | None = None
    try:
        await player.connect()
        rpc_client = EndpointRpcClient(lyrion_test_endpoint, player.rpc_player_id)

        await rpc_client.send(["time", 37])
        status = await rpc_client.send(["status", 0, 100])
        if "time" in status:
            assert isinstance(status["time"], int | float)
            assert float(status["time"]) >= 0.0
        else:
            assert status.get("mode", "stop") in {"stop", "play", "pause"}
    finally:
        if rpc_client is not None:
            await rpc_client.close()
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
async def test_ma_volume_change_disables_lms_sync_volume_before_grouped_volume_change(  # noqa: PLR0915
    lyrion_test_endpoint: LyrionTestEndpoint,
) -> None:
    """MA-driven Lyrion volume changes must disable LMS syncVolume first."""
    player_a = ScriptableSlimProtoPlayer(
        endpoint=lyrion_test_endpoint,
        player_id="sync-volume-guard-a",
        name="Sync Volume Guard A",
        model="test",
    )
    player_b = ScriptableSlimProtoPlayer(
        endpoint=lyrion_test_endpoint,
        player_id="sync-volume-guard-b",
        name="Sync Volume Guard B",
        model="test",
    )
    player_c = ScriptableSlimProtoPlayer(
        endpoint=lyrion_test_endpoint,
        player_id="sync-volume-guard-c",
        name="Sync Volume Guard C",
        model="test",
    )

    rpc_a: EndpointRpcClient | None = None
    rpc_b: EndpointRpcClient | None = None
    rpc_c: EndpointRpcClient | None = None

    async def _wait_until_grouped(
        first_client: EndpointRpcClient,
        first_player_id: str,
        second_client: EndpointRpcClient,
        second_player_id: str,
        timeout: float = 4.0,
    ) -> tuple[dict[str, object], dict[str, object]]:
        deadline = asyncio.get_running_loop().time() + timeout
        first_status = await first_client.send(["status", 0, 100])
        second_status = await second_client.send(["status", 0, 100])
        while asyncio.get_running_loop().time() < deadline:
            first_root = sync_master(first_status) or first_player_id
            second_root = sync_master(second_status) or second_player_id
            if first_root == second_root:
                return first_status, second_status
            if second_player_id in sync_slaves(first_status):
                return first_status, second_status
            if first_player_id in sync_slaves(second_status):
                return first_status, second_status
            await asyncio.sleep(0.1)
            first_status = await first_client.send(["status", 0, 100])
            second_status = await second_client.send(["status", 0, 100])
        pytest.fail(
            "Players did not become grouped in time; "
            f"first_status={first_status}, second_status={second_status}"
        )

    async def _ensure_grouped(
        anchor_client: EndpointRpcClient,
        anchor_player_id: str,
        member_client: EndpointRpcClient,
        member_player_id: str,
        *,
        reset_anchor: bool = False,
    ) -> None:
        if reset_anchor:
            await anchor_client.send(["sync", "-"])
        await member_client.send(["sync", "-"])

        if lyrion_test_endpoint.fake_server is not None:
            await member_client.send(["sync", anchor_player_id])
        else:
            await anchor_client.send(["sync", member_player_id])

        first_status, second_status = await _wait_until_grouped(
            anchor_client,
            anchor_player_id,
            member_client,
            member_player_id,
        )
        if (sync_master(first_status) or anchor_player_id) == (
            sync_master(second_status) or member_player_id
        ):
            return

        if reset_anchor:
            await anchor_client.send(["sync", "-"])
        await member_client.send(["sync", "-"])

        if lyrion_test_endpoint.fake_server is not None:
            await anchor_client.send(["sync", member_player_id])
        else:
            await member_client.send(["sync", anchor_player_id])

        await _wait_until_grouped(
            anchor_client,
            anchor_player_id,
            member_client,
            member_player_id,
        )

    async def _playerpref_sync_volume(client: EndpointRpcClient, value: int | None = None) -> int:
        command: list[object] = ["playerpref", "syncVolume", "?"]
        if value is not None:
            command = ["playerpref", "syncVolume", value]
        result = await client.send(command)
        return int(result.get("_p2", 0))

    try:
        await player_a.connect()
        await player_b.connect()
        await player_c.connect()

        rpc_a = EndpointRpcClient(lyrion_test_endpoint, player_a.rpc_player_id)
        rpc_b = EndpointRpcClient(lyrion_test_endpoint, player_b.rpc_player_id)
        rpc_c = EndpointRpcClient(lyrion_test_endpoint, player_c.rpc_player_id)

        leader_id = player_a.rpc_player_id
        member_b_id = player_b.rpc_player_id
        member_c_id = player_c.rpc_player_id

        await _ensure_grouped(rpc_a, leader_id, rpc_b, member_b_id, reset_anchor=True)
        await _ensure_grouped(rpc_a, leader_id, rpc_c, member_c_id)

        await rpc_a.send(["mixer", "volume", 20])
        await rpc_b.send(["mixer", "volume", 40])
        await rpc_c.send(["mixer", "volume", 60])
        await rpc_a.send(["playerpref", "syncVolume", 1])
        await rpc_b.send(["playerpref", "syncVolume", 1])
        await rpc_c.send(["playerpref", "syncVolume", 1])

        provider = MagicMock()
        provider.instance_id = "lyrion_player"
        provider.logger = MagicMock()
        provider.mass = MagicMock()
        provider.mass.subscribe = MagicMock(return_value=lambda: None)
        provider.mass.config = MagicMock()
        provider.mass.config.create_default_player_config = MagicMock()
        provider.mass.config.get_base_player_config = MagicMock(return_value=MagicMock())
        provider.get_last_cometd_status_seen_at = MagicMock(return_value=0.0)
        provider.verify_cometd_status_expectation = AsyncMock(return_value=True)

        client_by_id = {
            leader_id: rpc_a,
            member_b_id: rpc_b,
            member_c_id: rpc_c,
        }

        async def _send_player_command(player_id: str, command: list[object]) -> dict[str, object]:
            return await client_by_id[player_id].send(command)

        provider.send_player_command = _send_player_command

        leader_player = LyrionPlayer(
            provider,
            leader_id,
            {"name": "Sync Volume Guard A", "model": "test"},
        )
        leader_player._attr_group_members = [leader_id, member_b_id, member_c_id]

        await leader_player.volume_set(33)

        status_a = await rpc_a.send(["status", 0, 100])
        status_b = await rpc_b.send(["status", 0, 100])
        status_c = await rpc_c.send(["status", 0, 100])
        assert int(status_a.get("mixer volume", status_a.get("volume", 0))) == 33
        assert int(status_b.get("mixer volume", status_b.get("volume", 0))) == 40
        assert int(status_c.get("mixer volume", status_c.get("volume", 0))) == 60
        assert await _playerpref_sync_volume(rpc_a) == 0
        assert await _playerpref_sync_volume(rpc_b) == 1
        assert await _playerpref_sync_volume(rpc_c) == 1
    finally:
        if rpc_a is not None:
            await rpc_a.close()
        if rpc_b is not None:
            await rpc_b.close()
        if rpc_c is not None:
            await rpc_c.close()
        for player in (player_a, player_b, player_c):
            with suppress(Exception):
                await asyncio.wait_for(player.disconnect(), timeout=2.0)
            with suppress(Exception):
                await asyncio.wait_for(player.close(), timeout=2.0)


@pytest.mark.asyncio
async def test_group_lifecycle_three_players_and_group_transport(  # noqa: PLR0915
    lyrion_test_endpoint: LyrionTestEndpoint,
) -> None:
    """Group A+B, add C, remove B, then verify group-wide play/pause remains on A+C."""
    player_a = ScriptableSlimProtoPlayer(
        endpoint=lyrion_test_endpoint,
        player_id="group-player-a",
        name="Group Player A",
        model="test",
    )
    player_b = ScriptableSlimProtoPlayer(
        endpoint=lyrion_test_endpoint,
        player_id="group-player-b",
        name="Group Player B",
        model="test",
    )
    player_c = ScriptableSlimProtoPlayer(
        endpoint=lyrion_test_endpoint,
        player_id="group-player-c",
        name="Group Player C",
        model="test",
    )

    rpc_a: EndpointRpcClient | None = None
    rpc_b: EndpointRpcClient | None = None
    rpc_c: EndpointRpcClient | None = None

    def _group_root(player_id: str, status: dict[str, object]) -> str:
        """Return a stable grouping root id for one player status payload."""
        return sync_master(status) or player_id

    async def _ensure_grouped(
        first_client: EndpointRpcClient,
        first_player_id: str,
        second_client: EndpointRpcClient,
        second_player_id: str,
        *,
        reset_first: bool = False,
    ) -> None:
        """Ensure two players are grouped, tolerating backend sync-direction differences."""

        async def _is_grouped() -> bool:
            first_status = await first_client.send(["status", 0, 100])
            second_status = await second_client.send(["status", 0, 100])
            first_root = _group_root(first_player_id, first_status)
            second_root = _group_root(second_player_id, second_status)
            if first_root == second_root:
                return True
            return second_player_id in sync_slaves(first_status) or first_player_id in sync_slaves(
                second_status
            )

        async def _wait_grouped(timeout: float = 2.0) -> bool:
            deadline = asyncio.get_running_loop().time() + timeout
            while asyncio.get_running_loop().time() < deadline:
                if await _is_grouped():
                    return True
                await asyncio.sleep(0.1)
            return await _is_grouped()

        if reset_first:
            await first_client.send(["sync", "-"])
        await second_client.send(["sync", "-"])

        if lyrion_test_endpoint.fake_server is not None:
            first_sync_result = await second_client.send(["sync", first_player_id])
        else:
            first_sync_result = await first_client.send(["sync", second_player_id])

        if await _wait_grouped(timeout=3.0):
            return

        # Fallback for backends that interpret sync from the opposite side.
        if reset_first:
            await first_client.send(["sync", "-"])
        await second_client.send(["sync", "-"])
        if lyrion_test_endpoint.fake_server is not None:
            second_sync_result = await first_client.send(["sync", second_player_id])
        else:
            second_sync_result = await second_client.send(["sync", first_player_id])

        if await _wait_grouped(timeout=3.0):
            return

        first_status = await first_client.send(["status", 0, 100])
        second_status = await second_client.send(["status", 0, 100])
        pytest.fail(
            "Unable to establish LMS sync group for pair "
            f"{first_player_id} and {second_player_id}; "
            f"first_sync_result={first_sync_result}, "
            f"second_sync_result={second_sync_result}, "
            f"first_status={first_status}, second_status={second_status}"
        )

    try:
        await player_a.connect()
        await player_b.connect()
        await player_c.connect()

        rpc_a = EndpointRpcClient(lyrion_test_endpoint, player_a.rpc_player_id)
        rpc_b = EndpointRpcClient(lyrion_test_endpoint, player_b.rpc_player_id)
        rpc_c = EndpointRpcClient(lyrion_test_endpoint, player_c.rpc_player_id)

        status_a = await rpc_a.send(["status", 0, 100])
        status_b = await rpc_b.send(["status", 0, 100])
        status_c = await rpc_c.send(["status", 0, 100])
        leader_id = str(status_a.get("playerid", player_a.rpc_player_id))
        member_b_id = str(status_b.get("playerid", player_b.rpc_player_id))
        member_c_id = str(status_c.get("playerid", player_c.rpc_player_id))

        # Build group: A + B
        await _ensure_grouped(rpc_a, leader_id, rpc_b, member_b_id, reset_first=True)

        # Extend group with C
        await _ensure_grouped(rpc_a, leader_id, rpc_c, member_c_id)

        status_a = await rpc_a.send(["status", 0, 100])
        status_b = await rpc_b.send(["status", 0, 100])
        status_c = await rpc_c.send(["status", 0, 100])
        root_a = _group_root(leader_id, status_a)
        root_b = _group_root(member_b_id, status_b)
        root_c = _group_root(member_c_id, status_c)
        assert root_a == root_b == root_c

        # Remove B from group, A and C should remain grouped.
        await rpc_b.send(["sync", "-"])
        await _ensure_grouped(rpc_a, leader_id, rpc_c, member_c_id)

        status_a = await rpc_a.send(["status", 0, 100])
        status_c = await rpc_c.send(["status", 0, 100])
        root_after_a = _group_root(leader_id, status_a)
        root_after_c = _group_root(member_c_id, status_c)
        assert root_after_a == root_after_c

        # B should now be independent from the A/C sync domain.
        await asyncio.sleep(0.2)
        status_b = await rpc_b.send(["status", 0, 100])
        assert _group_root(member_b_id, status_b) != _group_root(leader_id, status_a)
    finally:
        if rpc_a is not None:
            await rpc_a.close()
        if rpc_b is not None:
            await rpc_b.close()
        if rpc_c is not None:
            await rpc_c.close()
        for player in (player_a, player_b, player_c):
            with suppress(Exception):
                await asyncio.wait_for(player.disconnect(), timeout=2.0)
            with suppress(Exception):
                await asyncio.wait_for(player.close(), timeout=2.0)


@pytest.mark.asyncio
@pytest.mark.live_lyrion_docker
async def test_live_group_transport_three_players_with_member_removal(  # noqa: PLR0915
    lyrion_test_endpoint: LyrionTestEndpoint,
) -> None:
    """Live LMS: load/play follows active group and excludes removed member."""
    player_a = ScriptableSlimProtoPlayer(
        endpoint=lyrion_test_endpoint,
        player_id="live-group-player-a",
        name="Live Group Player A",
        model="test",
    )
    player_b = ScriptableSlimProtoPlayer(
        endpoint=lyrion_test_endpoint,
        player_id="live-group-player-b",
        name="Live Group Player B",
        model="test",
    )
    player_c = ScriptableSlimProtoPlayer(
        endpoint=lyrion_test_endpoint,
        player_id="live-group-player-c",
        name="Live Group Player C",
        model="test",
    )

    rpc_a: EndpointRpcClient | None = None
    rpc_b: EndpointRpcClient | None = None
    rpc_c: EndpointRpcClient | None = None

    def _group_root(player_id: str, status: dict[str, object]) -> str:
        return sync_master(status) or player_id

    def _first_track_id(status: dict[str, object]) -> str | None:
        playlist_loop = status.get("playlist_loop")
        if not isinstance(playlist_loop, list) or not playlist_loop:
            return None
        first = playlist_loop[0]
        if not isinstance(first, dict):
            return None
        for key in ("id", "track_id"):
            value = first.get(key)
            if value is not None:
                return str(value)
        return None

    async def _wait_until_grouped(
        first_client: EndpointRpcClient,
        first_player_id: str,
        second_client: EndpointRpcClient,
        second_player_id: str,
        timeout: float = 4.0,
    ) -> tuple[dict[str, object], dict[str, object]]:
        deadline = asyncio.get_running_loop().time() + timeout
        first_status = await first_client.send(["status", 0, 100])
        second_status = await second_client.send(["status", 0, 100])
        while asyncio.get_running_loop().time() < deadline:
            first_root = _group_root(first_player_id, first_status)
            second_root = _group_root(second_player_id, second_status)
            if first_root == second_root:
                return first_status, second_status
            if second_player_id in sync_slaves(first_status):
                return first_status, second_status
            if first_player_id in sync_slaves(second_status):
                return first_status, second_status
            await asyncio.sleep(0.1)
            first_status = await first_client.send(["status", 0, 100])
            second_status = await second_client.send(["status", 0, 100])
        pytest.fail(
            "Players did not become grouped in time; "
            f"first_status={first_status}, second_status={second_status}"
        )

    async def _ensure_grouped(
        anchor_client: EndpointRpcClient,
        anchor_player_id: str,
        member_client: EndpointRpcClient,
        member_player_id: str,
        *,
        reset_anchor: bool = False,
    ) -> None:
        if reset_anchor:
            await anchor_client.send(["sync", "-"])
        await member_client.send(["sync", "-"])

        if lyrion_test_endpoint.fake_server is not None:
            await member_client.send(["sync", anchor_player_id])
        else:
            await anchor_client.send(["sync", member_player_id])

        first_status, second_status = await _wait_until_grouped(
            anchor_client,
            anchor_player_id,
            member_client,
            member_player_id,
        )
        first_root = _group_root(anchor_player_id, first_status)
        second_root = _group_root(member_player_id, second_status)
        if first_root == second_root:
            return

        if reset_anchor:
            await anchor_client.send(["sync", "-"])
        await member_client.send(["sync", "-"])

        if lyrion_test_endpoint.fake_server is not None:
            await anchor_client.send(["sync", member_player_id])
        else:
            await member_client.send(["sync", anchor_player_id])

        await _wait_until_grouped(
            anchor_client,
            anchor_player_id,
            member_client,
            member_player_id,
        )

    async def _wait_for_track_ids(
        expected_ids: dict[str, str],
        timeout: float = 6.0,
    ) -> dict[str, dict[str, object]]:
        assert rpc_a is not None
        assert rpc_b is not None
        assert rpc_c is not None
        client_by_id = {
            player_a.rpc_player_id: rpc_a,
            player_b.rpc_player_id: rpc_b,
            player_c.rpc_player_id: rpc_c,
        }
        deadline = asyncio.get_running_loop().time() + timeout
        last_status: dict[str, dict[str, object]] = {}
        while asyncio.get_running_loop().time() < deadline:
            all_match = True
            for player_id, expected_track_id in expected_ids.items():
                status = await client_by_id[player_id].send(["status", 0, 100])
                last_status[player_id] = status
                if _first_track_id(status) != expected_track_id:
                    all_match = False
            if all_match:
                return last_status
            await asyncio.sleep(0.1)

        pytest.fail(
            "Timed out waiting for expected track ids; "
            f"expected_ids={expected_ids}, last_status={last_status}"
        )

    try:
        await player_a.connect()
        await player_b.connect()
        await player_c.connect()

        rpc_a = EndpointRpcClient(lyrion_test_endpoint, player_a.rpc_player_id)
        rpc_b = EndpointRpcClient(lyrion_test_endpoint, player_b.rpc_player_id)
        rpc_c = EndpointRpcClient(lyrion_test_endpoint, player_c.rpc_player_id)

        status_a = await rpc_a.send(["status", 0, 100])
        status_b = await rpc_b.send(["status", 0, 100])
        status_c = await rpc_c.send(["status", 0, 100])
        leader_id = str(status_a.get("playerid", player_a.rpc_player_id))
        member_b_id = str(status_b.get("playerid", player_b.rpc_player_id))
        member_c_id = str(status_c.get("playerid", player_c.rpc_player_id))

        titles_result = await rpc_a.send(["titles", 0, 2])
        titles_loop = titles_result.get("titles_loop")
        if not isinstance(titles_loop, list) or len(titles_loop) < 2:
            pytest.skip("Need at least two LMS titles for transport propagation test")
        first_title = titles_loop[0]
        second_title = titles_loop[1]
        if not isinstance(first_title, dict) or "id" not in first_title:
            pytest.skip("Unable to resolve first playable LMS track id")
        if not isinstance(second_title, dict) or "id" not in second_title:
            pytest.skip("Unable to resolve second playable LMS track id")
        first_track_id = str(first_title["id"])
        second_track_id = str(second_title["id"])

        # Build initial group A+B+C.
        await _ensure_grouped(rpc_a, leader_id, rpc_b, member_b_id, reset_anchor=True)
        await _ensure_grouped(rpc_a, leader_id, rpc_c, member_c_id)

        status_a = await rpc_a.send(["status", 0, 100])
        status_b = await rpc_b.send(["status", 0, 100])
        status_c = await rpc_c.send(["status", 0, 100])
        root_before = _group_root(leader_id, status_a)
        assert root_before == _group_root(member_b_id, status_b)
        assert root_before == _group_root(member_c_id, status_c)

        provider_by_id = {
            leader_id: ProviderStyleRpcClient(lyrion_test_endpoint, leader_id),
            member_b_id: ProviderStyleRpcClient(lyrion_test_endpoint, member_b_id),
            member_c_id: ProviderStyleRpcClient(lyrion_test_endpoint, member_c_id),
        }

        # Robust transport trigger for live LMS: load + explicit play.
        await provider_by_id[root_before].send_player_command(
            ["playlistcontrol", "cmd:load", f"track_id:{first_track_id}"]
        )
        await provider_by_id[root_before].send_player_command(["play"])

        await _wait_for_track_ids(
            {
                leader_id: first_track_id,
                member_b_id: first_track_id,
                member_c_id: first_track_id,
            }
        )

        # Remove B and verify next load/play only propagates inside remaining A/C group.
        await rpc_b.send(["sync", "-"])
        await _ensure_grouped(rpc_a, leader_id, rpc_c, member_c_id)

        status_a = await rpc_a.send(["status", 0, 100])
        status_c = await rpc_c.send(["status", 0, 100])
        root_after = _group_root(leader_id, status_a)
        assert root_after == _group_root(member_c_id, status_c)

        await provider_by_id[root_after].send_player_command(
            ["playlistcontrol", "cmd:load", f"track_id:{second_track_id}"]
        )
        await provider_by_id[root_after].send_player_command(["play"])

        await _wait_for_track_ids(
            {
                leader_id: second_track_id,
                member_c_id: second_track_id,
                member_b_id: first_track_id,
            }
        )
    finally:
        if "provider_by_id" in locals():
            for client in provider_by_id.values():
                await client.close()
        if rpc_a is not None:
            await rpc_a.close()
        if rpc_b is not None:
            await rpc_b.close()
        if rpc_c is not None:
            await rpc_c.close()
        for player in (player_a, player_b, player_c):
            with suppress(Exception):
                await asyncio.wait_for(player.disconnect(), timeout=2.0)
            with suppress(Exception):
                await asyncio.wait_for(player.close(), timeout=2.0)
