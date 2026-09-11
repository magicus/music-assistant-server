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

from music_assistant.providers.lyrion_player.player import (
    LyrionPlayer,
    _get_status_int,
    _get_status_str,
    _group_members_match_leader,
    _time_matches_target,
)
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
    provider.play_player = AsyncMock()
    provider.pause_player = AsyncMock()
    provider.stop_player = AsyncMock()
    provider.set_player_power = AsyncMock()
    provider.sync_player_to = AsyncMock()
    provider.unsync_player = AsyncMock()
    provider.next_player_track = AsyncMock()
    provider.previous_player_track = AsyncMock()
    provider.seek_player = AsyncMock()
    provider.set_player_volume = AsyncMock()
    provider.set_player_muted = AsyncMock()
    provider.set_player_sync_volume = AsyncMock()
    provider.play_player_url = AsyncMock()
    provider.append_player_url = AsyncMock()
    provider.get_player_queue_status = AsyncMock(return_value={"mode": "play"})
    provider.get_last_cometd_status_seen_at = MagicMock(return_value=0.0)
    provider.get_cached_cometd_status = MagicMock(return_value={"playlist_cur_index": 0})
    provider.wait_for_cometd_status_update = AsyncMock(return_value=True)
    provider.verify_cometd_status_expectation = AsyncMock(return_value=True)
    provider.get_player_status = AsyncMock(return_value={"mode": "play"})
    provider.apply_status_update = MagicMock()
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

    mock_provider.set_player_muted.assert_awaited_once_with("test_player", True)
    assert player.volume_muted is True


@pytest.mark.asyncio
async def test_grouped_volume_mute_disables_sync_volume_first(
    player: LyrionPlayer, mock_provider: MagicMock
) -> None:
    """MA mute on a synced/grouped player should first disable LMS syncVolume."""
    player._attr_group_members = ["test_player", "member_a"]

    await player.volume_mute(True)

    mock_provider.set_player_sync_volume.assert_awaited_once_with("test_player", False)
    mock_provider.set_player_muted.assert_awaited_once_with("test_player", True)


@pytest.mark.asyncio
async def test_volume_mute_wraps_provider_unavailable(
    player: LyrionPlayer, mock_provider: MagicMock
) -> None:
    """Provider availability failures surface as PlayerCommandFailed."""
    mock_provider.set_player_muted.side_effect = ProviderUnavailableError("offline")

    with pytest.raises(PlayerCommandFailed, match="volume_mute failed"):
        await player.volume_mute(False)


@pytest.mark.asyncio
async def test_next_track_dispatches_playlist_index_increment(
    player: LyrionPlayer, mock_provider: MagicMock
) -> None:
    """Next track maps to LMS playlist index +1."""
    await player.next_track()

    mock_provider.next_player_track.assert_awaited_once_with("test_player")


@pytest.mark.asyncio
async def test_previous_track_dispatches_playlist_index_decrement(
    player: LyrionPlayer, mock_provider: MagicMock
) -> None:
    """Previous track maps to LMS playlist index -1."""
    await player.previous_track()

    mock_provider.previous_player_track.assert_awaited_once_with("test_player")


@pytest.mark.asyncio
async def test_seek_dispatches_time_and_updates_elapsed(
    player: LyrionPlayer, mock_provider: MagicMock
) -> None:
    """Seek maps to LMS time command and updates tracked elapsed state."""
    before = time.time()

    await player.seek(42)

    mock_provider.seek_player.assert_awaited_once_with("test_player", 42)
    assert player.elapsed_time == 42.0
    assert player.elapsed_time_last_updated is not None
    assert player.elapsed_time_last_updated >= before


@pytest.mark.asyncio
async def test_seek_clamps_negative_positions(
    player: LyrionPlayer, mock_provider: MagicMock
) -> None:
    """Negative seek positions clamp to zero seconds."""
    await player.seek(-11)

    mock_provider.seek_player.assert_awaited_once_with("test_player", 0)
    assert player.elapsed_time == 0.0


@pytest.mark.asyncio
async def test_grouped_volume_set_disables_sync_volume_first(
    player: LyrionPlayer, mock_provider: MagicMock
) -> None:
    """MA volume changes on a synced/grouped player should first disable LMS syncVolume."""
    player._attr_group_members = ["test_player", "member_a"]

    await player.volume_set(42)

    mock_provider.set_player_sync_volume.assert_awaited_once_with("test_player", False)
    mock_provider.set_player_volume.assert_awaited_once_with("test_player", 42)


@pytest.mark.asyncio
async def test_ungrouped_volume_set_skips_sync_volume_disable(
    player: LyrionPlayer, mock_provider: MagicMock
) -> None:
    """Ungrouped player volume changes should not send LMS syncVolume commands."""
    await player.volume_set(42)

    mock_provider.set_player_volume.assert_awaited_once_with("test_player", 42)


@pytest.mark.asyncio
async def test_transport_and_seek_wrap_provider_unavailable(
    player: LyrionPlayer, mock_provider: MagicMock
) -> None:
    """Transport and seek failures are wrapped consistently."""
    mock_provider.next_player_track.side_effect = ProviderUnavailableError("down")

    with pytest.raises(PlayerCommandFailed, match="next_track failed"):
        await player.next_track()

    mock_provider.next_player_track.reset_mock(side_effect=True)
    mock_provider.previous_player_track.side_effect = ProviderUnavailableError("down")
    with pytest.raises(PlayerCommandFailed, match="previous_track failed"):
        await player.previous_track()

    mock_provider.previous_player_track.reset_mock(side_effect=True)
    mock_provider.seek_player.side_effect = ProviderUnavailableError("down")
    with pytest.raises(PlayerCommandFailed, match="seek failed"):
        await player.seek(12)


@pytest.mark.asyncio
async def test_poll_applies_status_and_wraps_provider_unavailable(
    player: LyrionPlayer, mock_provider: MagicMock
) -> None:
    """Polling should apply status updates and wrap provider outages."""
    await player.poll()
    mock_provider.apply_status_update.assert_called_once_with(player, {"mode": "play"})

    mock_provider.get_player_status = AsyncMock(side_effect=ProviderUnavailableError("down"))
    with pytest.raises(PlayerCommandFailed, match="Unable to poll player"):
        await player.poll()


@pytest.mark.asyncio
async def test_play_media_native_queue_path_skips_stream_url_resolution(
    player: LyrionPlayer, mock_provider: MagicMock
) -> None:
    """Native track-id playback should avoid URL fallback path."""
    media = MagicMock()
    media.uri = "lyrion://track/1"
    player._queue_sync.try_play_lyrion_track_id = AsyncMock(return_value=True)

    await player.play_media(media)

    player._queue_sync.try_play_lyrion_track_id.assert_awaited_once_with(media)
    mock_provider.play_player_url.assert_not_awaited()
    assert player.current_media is media


@pytest.mark.asyncio
async def test_play_media_stream_url_path_dispatches_playlist_play(
    player: LyrionPlayer, mock_provider: MagicMock
) -> None:
    """Non-native media should resolve a stream URL and use playlist play."""
    media = MagicMock()
    media.uri = "spotify://track/123"
    player._queue_sync.try_play_lyrion_track_id = AsyncMock(return_value=False)
    mock_provider.mass.streams.resolve_stream_url = AsyncMock(return_value="http://stream/abc")

    await player.play_media(media)

    mock_provider.play_player_url.assert_awaited_once_with("test_player", "http://stream/abc")


@pytest.mark.asyncio
async def test_play_media_wraps_provider_unavailable_in_url_path(
    player: LyrionPlayer, mock_provider: MagicMock
) -> None:
    """URL fallback playback errors should map to PlayerCommandFailed."""
    media = MagicMock()
    media.uri = "spotify://track/123"
    player._queue_sync.try_play_lyrion_track_id = AsyncMock(return_value=False)
    mock_provider.mass.streams.resolve_stream_url = AsyncMock(return_value="http://stream/abc")
    mock_provider.play_player_url.side_effect = ProviderUnavailableError("down")

    with pytest.raises(PlayerCommandFailed, match="play_media failed"):
        await player.play_media(media)


@pytest.mark.asyncio
async def test_enqueue_next_media_covers_native_and_url_paths(
    player: LyrionPlayer, mock_provider: MagicMock
) -> None:
    """enqueue_next_media should handle both native and URL-based queue insertion."""
    media = MagicMock()
    media.uri = "spotify://track/123"

    player._queue_sync.try_play_lyrion_track_id = AsyncMock(return_value=True)
    player._queue_sync.sync_ma_queue_to_lms = AsyncMock()
    await player.enqueue_next_media(media)
    player._queue_sync.sync_ma_queue_to_lms.assert_awaited_once()

    player._queue_sync.try_play_lyrion_track_id = AsyncMock(return_value=False)
    mock_provider.mass.streams.resolve_stream_url = AsyncMock(return_value="http://stream/next")
    mock_provider.append_player_url.reset_mock()
    await player.enqueue_next_media(media)
    mock_provider.append_player_url.assert_awaited_once_with(
        "test_player", "http://stream/next"
    )


@pytest.mark.asyncio
async def test_play_pause_stop_and_power_dispatch_expected_commands(
    player: LyrionPlayer, mock_provider: MagicMock
) -> None:
    """Transport/power commands should delegate through thin provider adapters."""
    await player.play()
    await player.pause()
    await player.stop()
    await player.power(True)
    await player.power(False)

    mock_provider.play_player.assert_awaited_once_with("test_player")
    mock_provider.pause_player.assert_awaited_once_with("test_player")
    mock_provider.stop_player.assert_awaited_once_with("test_player")
    assert mock_provider.set_player_power.await_args_list[0].args == ("test_player", True)
    assert mock_provider.set_player_power.await_args_list[1].args == ("test_player", False)


@pytest.mark.asyncio
async def test_queue_event_callbacks_filter_by_player_id(player: LyrionPlayer) -> None:
    """Queue event callbacks should ignore foreign queue ids and mirror own queue updates."""
    player._queue_sync.sync_ma_queue_to_lms = AsyncMock()

    await player._on_ma_queue_items_updated(MagicMock(object_id="other"))
    await player._on_ma_queue_updated(MagicMock(object_id="other"))
    assert player._queue_sync.sync_ma_queue_to_lms.await_count == 0

    await player._on_ma_queue_items_updated(MagicMock(object_id="test_player"))
    await player._on_ma_queue_updated(MagicMock(object_id="test_player"))
    assert player._queue_sync.sync_ma_queue_to_lms.await_count == 2


def test_player_status_helper_functions_cover_fallback_branches() -> None:
    """Small status helper functions should handle invalid or missing values safely."""
    assert _get_status_str({"mode": "play"}, "mode") == "play"
    assert _get_status_str({"mode": 7}, "mode") == "7"
    assert _get_status_str({}, "mode") is None

    assert _get_status_int({"x": "12"}, "x") == 12
    assert _get_status_int({"x": "bad"}, "x") is None
    assert _get_status_int({}, "x") is None

    assert _time_matches_target({"time": 10}, 10)
    assert _time_matches_target({"time": "10.8"}, 10)
    assert not _time_matches_target({"time": "bad"}, 10)
    assert not _time_matches_target({}, 10)

    assert _group_members_match_leader({"sync_master": "other"}, "leader")
    assert _group_members_match_leader({"sync_slaves": "x,y"}, "leader")
    assert not _group_members_match_leader({"sync_slaves": ""}, "leader")


@pytest.mark.asyncio
async def test_next_track_retries_polling_when_cometd_confirmation_is_missing(
    player: LyrionPlayer, mock_provider: MagicMock
) -> None:
    """Command verification delegates to shared CometD expectation helper."""
    await player.next_track()

    mock_provider.verify_cometd_status_expectation.assert_awaited_once()


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

    mock_provider.unsync_player.assert_awaited_once_with("member_a")
    mock_provider.sync_player_to.assert_awaited_once_with("member_b", "test_player")
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
    mock_provider.sync_player_to.side_effect = ProviderUnavailableError("offline")

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


@pytest.mark.asyncio
async def test_enqueue_next_media_wraps_provider_unavailable(
    player: LyrionPlayer, mock_provider: MagicMock
) -> None:
    """Enqueue URL path provider failures should map to PlayerCommandFailed."""
    media = MagicMock()
    media.uri = "spotify://track/123"
    player._queue_sync.try_play_lyrion_track_id = AsyncMock(return_value=False)
    mock_provider.mass.streams.resolve_stream_url = AsyncMock(return_value="http://stream/next")
    mock_provider.append_player_url.side_effect = ProviderUnavailableError("down")

    with pytest.raises(PlayerCommandFailed, match="enqueue_next_media failed"):
        await player.enqueue_next_media(media)


@pytest.mark.asyncio
async def test_play_pause_stop_power_and_volume_set_wrap_provider_unavailable(
    player: LyrionPlayer, mock_provider: MagicMock
) -> None:
    """Command helpers should wrap ProviderUnavailableError consistently."""
    mock_provider.set_player_volume.side_effect = ProviderUnavailableError("down")
    mock_provider.play_player.side_effect = ProviderUnavailableError("down")
    mock_provider.pause_player.side_effect = ProviderUnavailableError("down")
    mock_provider.stop_player.side_effect = ProviderUnavailableError("down")
    mock_provider.set_player_power.side_effect = ProviderUnavailableError("down")
    with pytest.raises(PlayerCommandFailed, match="play failed"):
        await player.play()

    with pytest.raises(PlayerCommandFailed, match="pause failed"):
        await player.pause()

    with pytest.raises(PlayerCommandFailed, match="stop failed"):
        await player.stop()

    with pytest.raises(PlayerCommandFailed, match="power failed"):
        await player.power(True)

    with pytest.raises(PlayerCommandFailed, match="volume_set failed"):
        await player.volume_set(10)


@pytest.mark.asyncio
async def test_next_previous_with_unknown_cached_index_use_baseline_only(
    player: LyrionPlayer, mock_provider: MagicMock
) -> None:
    """When no cached playlist index exists, next/previous should use basic status verification."""
    mock_provider.get_cached_cometd_status.return_value = {"playlist_cur_index": None}

    await player.next_track()
    await player.previous_track()

    calls = mock_provider.verify_cometd_status_expectation.await_args_list
    assert calls[0].args[0] == "test_player"
    assert calls[0].kwargs["expectation"] is None
    assert calls[1].args[0] == "test_player"
    assert calls[1].kwargs["expectation"] is None


@pytest.mark.asyncio
async def test_set_members_noop_and_skip_self_or_unknown_members(
    player: LyrionPlayer, mock_provider: MagicMock
) -> None:
    """set_members should noop on empty changes and skip self/non-members safely."""
    await player.set_members(player_ids_to_add=None, player_ids_to_remove=None)
    mock_provider.unsync_player.assert_not_awaited()
    mock_provider.sync_player_to.assert_not_awaited()

    player._attr_group_members = ["test_player", "member_a"]
    await player.set_members(
        player_ids_to_remove=["test_player", "missing"],
        player_ids_to_add=["test_player", "member_a"],
    )
    mock_provider.unsync_player.assert_not_awaited()
    mock_provider.sync_player_to.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_queue_and_metadata_wrappers(
    player: LyrionPlayer, mock_provider: MagicMock
) -> None:
    """Sync wrappers should delegate to queue-sync and metadata helpers."""
    player._queue_sync.sync_lms_queue_to_ma = AsyncMock(return_value=None)
    await player.sync_queue_from_lms()
    player._queue_sync.sync_lms_queue_to_ma.assert_awaited_once()

    await player.sync_from_lms({"name": "Kitchen", "model": "Squeeze"})
    assert player._attr_name == "Kitchen"


@pytest.mark.asyncio
async def test_sync_from_lms_adds_mac_identifier_when_player_id_is_mac(
    mock_provider: MagicMock,
) -> None:
    """MAC-like player ids should be added to device identifiers during metadata mapping."""
    mac_player = LyrionPlayer(mock_provider, "aa:bb:cc:dd:ee:ff", {})
    await mac_player.sync_from_lms({"name": "Kitchen", "model": "Squeeze"})

    assert mac_player.device_info is not None
    assert mac_player.device_info.identifiers
