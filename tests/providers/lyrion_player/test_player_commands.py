"""Unit tests for command dispatch in the Lyrion player implementation."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from music_assistant_models.enums import PlayerFeature
from music_assistant_models.errors import (
    InvalidCommand,
    PlayerCommandFailed,
    ProviderUnavailableError,
)

from music_assistant.providers.lyrion_player.player import LyrionPlayer
from music_assistant.providers.lyrion_player.provider import LyrionPlayerProvider


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


def test_fallback_polling_setting_controls_needs_poll_and_interval(
    player: LyrionPlayer, mock_provider: MagicMock
) -> None:
    """Provider fallback polling should be a shared toggle for all Lyrion players."""
    mock_provider.get_config_value.side_effect = lambda key, default=None, **_kwargs: {
        "fallback_polling": True,
        "fallback_polling_interval": 17,
    }.get(key, default)

    assert player.needs_poll is True
    assert player.poll_interval == 17

    mock_provider.get_config_value.side_effect = lambda key, default=None, **_kwargs: {
        "fallback_polling": False,
        "fallback_polling_interval": 23,
    }.get(key, default)

    assert player.needs_poll is False


@pytest.fixture
def player_provider() -> MagicMock:
    """Return a provider stub for route-level tests."""
    provider = MagicMock()
    provider.instance_id = "lyrion_player"
    provider.logger = MagicMock()
    provider.mass = MagicMock()
    provider.mass.players = MagicMock()
    provider.mass.player_queues = MagicMock()
    provider.mass.streams = MagicMock()
    provider.mass.streams.resolve_stream_url = AsyncMock()
    provider.mass.player_queues.player_media_from_queue_item = AsyncMock()
    provider.mass.players.get_player = MagicMock()
    return provider


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
async def test_grouped_volume_mute_disables_sync_volume_first(
    player: LyrionPlayer, mock_provider: MagicMock
) -> None:
    """MA mute on a synced/grouped player should first disable LMS syncVolume."""
    player._attr_group_members = ["test_player", "member_a"]

    await player.volume_mute(True)

    assert mock_provider.send_player_command.await_args_list[0].args == (
        "test_player",
        ["playerpref", "syncVolume", 0],
    )
    assert mock_provider.send_player_command.await_args_list[1].args == (
        "test_player",
        ["mixer", "muting", 1],
    )


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
async def test_grouped_volume_set_disables_sync_volume_first(
    player: LyrionPlayer, mock_provider: MagicMock
) -> None:
    """MA volume changes on a synced/grouped player should first disable LMS syncVolume."""
    player._attr_group_members = ["test_player", "member_a"]

    await player.volume_set(42)

    assert mock_provider.send_player_command.await_args_list[0].args == (
        "test_player",
        ["playerpref", "syncVolume", 0],
    )
    assert mock_provider.send_player_command.await_args_list[1].args == (
        "test_player",
        ["mixer", "volume", 42],
    )


@pytest.mark.asyncio
async def test_ungrouped_volume_set_skips_sync_volume_disable(
    player: LyrionPlayer, mock_provider: MagicMock
) -> None:
    """Ungrouped player volume changes should not send LMS syncVolume commands."""
    await player.volume_set(42)

    mock_provider.send_player_command.assert_awaited_once_with(
        "test_player",
        ["mixer", "volume", 42],
    )


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
async def test_get_stream_url_rejects_queue_id_mismatch(player_provider: MagicMock) -> None:
    """The redirect endpoint only accepts the player queue for the player."""
    request = MagicMock()
    request.query = {
        "player_id": "player_1",
        "queue_id": "queue_2",
        "queue_item_id": "item_1",
    }

    request_player = MagicMock(spec=LyrionPlayer)
    request_player.provider = player_provider
    player_provider.mass.players.get_player.return_value = request_player

    with pytest.raises(web.HTTPBadRequest, match="queue_id must match player_id"):
        await LyrionPlayerProvider._handle_get_stream_url(player_provider, request)

    player_provider.mass.player_queues.get_item.assert_not_called()
