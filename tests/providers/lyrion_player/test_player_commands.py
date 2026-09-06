"""Unit tests for command dispatch in the Lyrion player implementation."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import PlayerFeature
from music_assistant_models.errors import (
    InvalidCommand,
    PlayerCommandFailed,
    ProviderUnavailableError,
)

from music_assistant.controllers.players import PlayerController
from music_assistant.providers.lyrion_player.player import LyrionPlayer


@pytest.fixture
def mock_provider() -> MagicMock:
    """Return a minimal provider stub for LyrionPlayer command tests."""
    provider = MagicMock()
    provider.instance_id = "lyrion_player"
    provider.logger = MagicMock()
    provider.mass = MagicMock()
    provider.mass.subscribe = MagicMock(return_value=lambda: None)
    provider.mass.config = MagicMock()
    provider.mass.config.create_default_player_config = MagicMock()
    provider.mass.config.get_base_player_config = MagicMock(return_value=MagicMock())
    provider.send_player_command = AsyncMock()
    return provider


@pytest.fixture
def player(mock_provider: MagicMock) -> LyrionPlayer:
    """Return a LyrionPlayer instance backed by a mocked provider."""
    return LyrionPlayer(mock_provider, "test_player", {})


def _build_controller_with_lyrion_players() -> tuple[
    PlayerController,
    MagicMock,
    MagicMock,
    LyrionPlayer,
    LyrionPlayer,
]:
    """Create a PlayerController with two real LyrionPlayer instances."""
    mass = MagicMock()
    mass.closing = False
    mass.loop = None
    mass.config = MagicMock()
    mass.config.get = MagicMock(return_value=[])
    mass.config.get_raw_player_config_value = MagicMock(
        side_effect=lambda _player_id, _key, default=None: default
    )
    mass.config.get_raw_core_config_value = MagicMock(return_value="GLOBAL")
    mass.config.create_default_player_config = MagicMock()
    mass.config.get_base_player_config = MagicMock(return_value=MagicMock())
    mass.signal_event = MagicMock()
    mass.get_providers = MagicMock(return_value=[])
    mass.subscribe = MagicMock(return_value=lambda: None)

    controller = PlayerController(mass)
    mass.players = controller

    provider = MagicMock()
    provider.instance_id = "lyrion_player"
    provider.logger = MagicMock()
    provider.mass = mass
    provider.send_player_command = AsyncMock()

    leader = LyrionPlayer(provider, "leader", {"name": "Leader", "model": "test"})
    member = LyrionPlayer(provider, "member", {"name": "Member", "model": "test"})
    provider.players = [leader, member]

    leader._attr_powered = True
    member._attr_powered = True
    leader._attr_available = True
    member._attr_available = True
    leader._attr_can_group_with = {"member", provider.instance_id}
    member._attr_can_group_with = {"leader", provider.instance_id}
    leader._attr_volume_control = "native"
    member._attr_volume_control = "native"

    controller._players = {leader.player_id: leader, member.player_id: member}
    leader.update_state(signal_event=False)
    member.update_state(signal_event=False)
    return controller, mass, provider, leader, member


@pytest.mark.asyncio
async def test_supported_features_include_transport_mute_and_seek(player: LyrionPlayer) -> None:
    """Expected capabilities are exposed by the player feature set."""
    assert PlayerFeature.NEXT_PREVIOUS in player.supported_features
    assert PlayerFeature.VOLUME_MUTE in player.supported_features
    assert PlayerFeature.SEEK in player.supported_features
    assert PlayerFeature.SET_MEMBERS in player.supported_features


@pytest.mark.asyncio
async def test_volume_mute_dispatches_lms_command(
    player: LyrionPlayer, mock_provider: MagicMock
) -> None:
    """Muting maps to LMS mixer muting command and updates local state."""
    await player.volume_mute(True)

    mock_provider.send_player_command.assert_awaited_once_with(
        "test_player", ["mixer", "muting", 1]
    )
    assert player.volume_muted is True


@pytest.mark.asyncio
async def test_volume_mute_wraps_provider_unavailable(
    player: LyrionPlayer, mock_provider: MagicMock
) -> None:
    """Provider availability failures surface as PlayerCommandFailed."""
    mock_provider.send_player_command.side_effect = ProviderUnavailableError("offline")

    with pytest.raises(PlayerCommandFailed, match="volume_mute failed"):
        await player.volume_mute(False)


@pytest.mark.asyncio
async def test_next_track_dispatches_playlist_index_increment(
    player: LyrionPlayer, mock_provider: MagicMock
) -> None:
    """Next track maps to LMS playlist index +1."""
    await player.next_track()

    mock_provider.send_player_command.assert_awaited_once_with(
        "test_player", ["playlist", "index", "+1"]
    )


@pytest.mark.asyncio
async def test_previous_track_dispatches_playlist_index_decrement(
    player: LyrionPlayer, mock_provider: MagicMock
) -> None:
    """Previous track maps to LMS playlist index -1."""
    await player.previous_track()

    mock_provider.send_player_command.assert_awaited_once_with(
        "test_player", ["playlist", "index", "-1"]
    )


@pytest.mark.asyncio
async def test_seek_dispatches_time_and_updates_elapsed(
    player: LyrionPlayer, mock_provider: MagicMock
) -> None:
    """Seek maps to LMS time command and updates tracked elapsed state."""
    before = time.time()

    await player.seek(42)

    mock_provider.send_player_command.assert_awaited_once_with("test_player", ["time", 42])
    assert player.elapsed_time == 42.0
    assert player.elapsed_time_last_updated is not None
    assert player.elapsed_time_last_updated >= before


@pytest.mark.asyncio
async def test_seek_clamps_negative_positions(
    player: LyrionPlayer, mock_provider: MagicMock
) -> None:
    """Negative seek positions clamp to zero seconds."""
    await player.seek(-11)

    mock_provider.send_player_command.assert_awaited_once_with("test_player", ["time", 0])
    assert player.elapsed_time == 0.0


@pytest.mark.asyncio
async def test_transport_and_seek_wrap_provider_unavailable(
    player: LyrionPlayer, mock_provider: MagicMock
) -> None:
    """Transport and seek failures are wrapped consistently."""
    mock_provider.send_player_command.side_effect = ProviderUnavailableError("down")

    with pytest.raises(PlayerCommandFailed, match="next_track failed"):
        await player.next_track()

    mock_provider.send_player_command.reset_mock(side_effect=True)
    mock_provider.send_player_command.side_effect = ProviderUnavailableError("down")
    with pytest.raises(PlayerCommandFailed, match="previous_track failed"):
        await player.previous_track()

    mock_provider.send_player_command.reset_mock(side_effect=True)
    mock_provider.send_player_command.side_effect = ProviderUnavailableError("down")
    with pytest.raises(PlayerCommandFailed, match="seek failed"):
        await player.seek(12)


@pytest.mark.asyncio
async def test_set_members_dispatches_sync_commands(
    player: LyrionPlayer, mock_provider: MagicMock
) -> None:
    """Adding/removing members maps to LMS sync commands."""
    player._attr_group_members = ["test_player", "member_a"]

    await player.set_members(
        player_ids_to_add=["member_b"],
        player_ids_to_remove=["member_a"],
    )

    assert mock_provider.send_player_command.await_args_list[0].args == (
        "member_a",
        ["sync", "-"],
    )
    assert mock_provider.send_player_command.await_args_list[1].args == (
        "member_b",
        ["sync", "test_player"],
    )
    assert player.group_members == ["test_player", "member_b"]


@pytest.mark.asyncio
async def test_set_members_rejects_when_player_is_synced(
    player: LyrionPlayer, mock_provider: MagicMock
) -> None:
    """A sync child can not manage group members."""
    leader = MagicMock()
    leader.player_id = "leader"
    leader.provider = mock_provider
    leader.type = "player"
    leader.group_members = ["leader", "test_player"]
    leader.state.available = True

    mock_provider.mass.players.iter_players = MagicMock(return_value=[leader])

    with pytest.raises(InvalidCommand, match="cannot set members"):
        await player.set_members(player_ids_to_add=["member_b"])


@pytest.mark.asyncio
async def test_set_members_wraps_provider_unavailable(
    player: LyrionPlayer, mock_provider: MagicMock
) -> None:
    """Provider failures while changing sync members are wrapped."""
    mock_provider.send_player_command.side_effect = ProviderUnavailableError("offline")

    with pytest.raises(PlayerCommandFailed, match="set_members failed"):
        await player.set_members(player_ids_to_add=["member_a"])


@pytest.mark.asyncio
async def test_controller_cmd_set_members_uses_lyrion_native_sync_path() -> None:
    """The MA grouping API should reach Lyrion native sync commands."""
    controller, _mass, provider, leader, member = _build_controller_with_lyrion_players()

    await controller.cmd_set_members("leader", player_ids_to_add=["member"])

    provider.send_player_command.assert_awaited_once_with("member", ["sync", "leader"])
    assert leader.group_members == ["leader", "member"]
    assert member.synced_to == "leader"
    assert member.state.synced_to == "leader"

    provider.send_player_command.reset_mock()

    await controller.cmd_set_members("leader", player_ids_to_remove=["member"])

    provider.send_player_command.assert_awaited_once_with("member", ["sync", "-"])
    assert leader.group_members == []
    assert member.synced_to is None
    assert member.state.synced_to is None


@pytest.mark.asyncio
async def test_controller_cmd_group_volume_targets_all_lyrion_members() -> None:
    """MA group volume commands should fan out to all grouped Lyrion members."""
    controller, _mass, provider, leader, member = _build_controller_with_lyrion_players()
    leader._attr_group_members = ["leader", "member"]
    leader._attr_volume_level = 30
    member._attr_volume_level = 30
    leader.update_state(signal_event=False)
    member.update_state(signal_event=False)

    await controller.cmd_group_volume("leader", 60)

    assert provider.send_player_command.await_count == 2
    assert provider.send_player_command.await_args_list[0].args == (
        "leader",
        ["mixer", "volume", 60],
    )
    assert provider.send_player_command.await_args_list[1].args == (
        "member",
        ["mixer", "volume", 60],
    )
