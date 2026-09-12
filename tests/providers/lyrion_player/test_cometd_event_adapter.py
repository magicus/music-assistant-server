"""Unit tests for LMS sync-topology mapping in the CometD event adapter."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.enums import PlaybackState

from music_assistant.providers.lyrion_player.player import LyrionPlayer
from pylyrion.cometd.player_status_events import (
    PlayerPlaylistChanged,
    PlayerRepeatChanged,
    PlayerShuffleChanged,
    PlayerStatusUpdated,
)
from pylyrion.cometd_event_adapter import (
    LyrionCometDEventAdapter,
    _extract_group_members,
    _extract_sync_master,
    _extract_sync_slaves,
)

MODE_MAP = {
    "play": PlaybackState.PLAYING,
    "pause": PlaybackState.PAUSED,
    "stop": PlaybackState.IDLE,
}


def _build_adapter(provider: MagicMock) -> LyrionCometDEventAdapter:
    """Build pylyrion adapter configured for MA playback-state semantics."""
    return LyrionCometDEventAdapter(
        mode_map=MODE_MAP,
        idle_state=PlaybackState.IDLE,
        is_supported_player=lambda _player: True,
        get_player=provider.get_runtime_player,
        iter_players=provider.iter_runtime_players,
        sync_player_queue=provider.sync_lms_queue_to_ma,
        update_player_state=lambda runtime_player: runtime_player.update_state(),
    )


class _RuntimePlayer:
    """Test adapter from MA player to pylyrion runtime player protocol."""

    def __init__(self, player: LyrionPlayer) -> None:
        self.player = player

    @property
    def player_id(self) -> str:
        return self.player.player_id

    @property
    def group_members(self) -> list[str]:
        return list(self.player.group_members)

    def set_available(self, available: bool) -> None:
        self.player._attr_available = available

    def set_playback_state(self, playback_state: object) -> None:
        self.player._attr_playback_state = playback_state

    def set_powered(self, powered: bool) -> None:
        self.player._attr_powered = powered

    def set_volume_level(self, volume_level: int) -> None:
        self.player._attr_volume_level = volume_level

    def set_elapsed_time(self, elapsed_time: float, updated_at: float) -> None:
        self.player._attr_elapsed_time = elapsed_time
        self.player._attr_elapsed_time_last_updated = updated_at

    def set_group_members(self, group_members: list[str]) -> None:
        self.player._attr_group_members = group_members

    def update_state(self) -> None:
        self.player.update_state()


def _build_provider_and_players() -> tuple[MagicMock, LyrionPlayer, LyrionPlayer]:
    """Create two linked Lyrion players with a shared mocked provider."""
    provider = MagicMock()
    provider.instance_id = "lyrion_player"
    provider.logger = MagicMock()
    provider.mass = MagicMock()
    provider.mass.subscribe = MagicMock(return_value=lambda: None)
    provider.mass.config = MagicMock()
    provider.mass.config.create_default_player_config = MagicMock()
    provider.mass.config.get_base_player_config = MagicMock(return_value=MagicMock())
    provider.lyrion_server = provider

    player_by_id: dict[str, LyrionPlayer] = {}

    def _get_player(player_id: str) -> LyrionPlayer | None:
        return player_by_id.get(player_id)

    provider.mass.players.get_player = MagicMock(side_effect=_get_player)
    provider.mass.players.iter_players = MagicMock(
        side_effect=lambda **_kwargs: list(player_by_id.values())
    )

    leader = LyrionPlayer(provider, "leader", {})
    child = LyrionPlayer(provider, "child", {})
    player_by_id[leader.player_id] = leader
    player_by_id[child.player_id] = child
    provider.players = [leader, child]
    runtime_by_id: dict[str, _RuntimePlayer] = {
        leader.player_id: _RuntimePlayer(leader),
        child.player_id: _RuntimePlayer(child),
    }

    def _get_runtime_player(player_id: str) -> _RuntimePlayer | None:
        return runtime_by_id.get(player_id)

    provider.get_runtime_player = MagicMock(side_effect=_get_runtime_player)
    provider.iter_runtime_players = MagicMock(side_effect=lambda: tuple(runtime_by_id.values()))

    async def _sync_lms_queue_to_ma(player_id: str) -> None:
        runtime_player = runtime_by_id.get(player_id)
        if runtime_player is None:
            return
        await runtime_player.player.sync_queue_from_lms()

    provider.sync_lms_queue_to_ma = AsyncMock(side_effect=_sync_lms_queue_to_ma)

    return provider, leader, child


def test_apply_status_maps_sync_slaves_to_group_members() -> None:
    """Leader status with sync_slaves should surface as MA group_members."""
    provider, leader, _child = _build_provider_and_players()
    adapter = _build_adapter(provider)
    runtime_leader = provider.get_runtime_player("leader")
    assert runtime_leader is not None

    adapter.apply_status(
        runtime_leader,
        {
            "mode": "play",
            "sync_slaves": "child",
        },
    )

    assert leader.group_members == ["leader", "child"]


def test_apply_status_refreshes_related_players_on_topology_change() -> None:
    """Topology changes should trigger related-player state refresh."""
    provider, leader, child = _build_provider_and_players()
    adapter = _build_adapter(provider)
    runtime_leader = provider.get_runtime_player("leader")
    assert runtime_leader is not None

    original_child_update_state = child.update_state
    child.update_state = MagicMock(side_effect=original_child_update_state)

    adapter.apply_status(
        runtime_leader,
        {
            "mode": "play",
            "sync_slaves": [{"playerid": "child"}],
        },
    )

    adapter.apply_status(
        runtime_leader,
        {
            "mode": "stop",
        },
    )

    assert leader.group_members == []
    assert child.update_state.call_count >= 1


def test_apply_status_invalid_player_marks_unavailable() -> None:
    """Invalid-player payload should mark player unavailable and return early."""
    provider, leader, _child = _build_provider_and_players()
    adapter = _build_adapter(provider)
    runtime_leader = provider.get_runtime_player("leader")
    assert runtime_leader is not None

    leader._attr_available = True
    adapter.apply_status(runtime_leader, {"error": "invalid player"})

    assert leader.available is False


def test_apply_status_maps_runtime_fields_and_clamps_volume() -> None:
    """Normal status payload should map playback, power, volume and elapsed time."""
    provider, leader, _child = _build_provider_and_players()
    adapter = _build_adapter(provider)
    runtime_leader = provider.get_runtime_player("leader")
    assert runtime_leader is not None

    adapter.apply_status(
        runtime_leader,
        {
            "mode": "play",
            "player_connected": 1,
            "power": "1",
            "mixer volume": "150",
            "time": "8.5",
        },
    )

    assert leader._attr_available is True
    assert leader._attr_playback_state == PlaybackState.PLAYING
    assert leader._attr_powered is True
    assert leader._attr_volume_level == 100
    assert leader._attr_elapsed_time == 8.5


def test_extract_sync_helpers_cover_dict_str_and_empty_cases() -> None:
    """Sync parsing helpers should support dict/list/string forms and ignore empties."""
    assert _extract_sync_master({"sync_master": {"playerid": "leader"}}) == "leader"
    assert _extract_sync_master({"sync_master_id": "leader"}) == "leader"
    assert _extract_sync_master({"sync_master": "-"}) is None

    assert _extract_sync_slaves({"sync_slaves": "a,b, a"}) == ["a", "b"]
    assert _extract_sync_slaves({"sync_slaves_loop": [{"playerid": "x"}, "y", "x"]}) == [
        "x",
        "y",
    ]
    assert _extract_sync_slaves({"sync_slaves": ""}) == []


def test_extract_group_members_for_slave_and_leader_cases() -> None:
    """Group member extraction should expose leader+children for sync leaders only."""
    assert _extract_group_members("leader", {"sync_slaves": "child"}) == [
        "leader",
        "child",
    ]
    assert _extract_group_members("child", {"sync_master": "leader"}) == []
    assert _extract_group_members("solo", {"mode": "stop"}) == []


def test_apply_status_unknown_mode_defaults_to_idle() -> None:
    """Unknown LMS modes should map to idle playback state."""
    provider, leader, _child = _build_provider_and_players()
    adapter = _build_adapter(provider)
    runtime_leader = provider.get_runtime_player("leader")
    assert runtime_leader is not None
    adapter.apply_status(runtime_leader, {"mode": "something-odd"})
    assert leader._attr_playback_state == PlaybackState.IDLE


async def test_handle_event_sync_triggers_for_status_playlist_repeat_shuffle() -> None:
    """Adapter should sync queue for initial status, playlist, repeat and shuffle events."""
    provider, leader, _child = _build_provider_and_players()
    adapter = _build_adapter(provider)
    leader.sync_queue_from_lms = AsyncMock(return_value=None)

    await adapter.handle_event(
        PlayerStatusUpdated(
            player_id="leader",
            status={"mode": "play"},
            is_initial=True,
        )
    )
    await adapter.handle_event(
        PlayerPlaylistChanged(
            player_id="leader",
            old_playlist_timestamp=1.0,
            new_playlist_timestamp=2.0,
            old_playlist_tracks=1,
            new_playlist_tracks=2,
        )
    )
    await adapter.handle_event(
        PlayerRepeatChanged(
            player_id="leader",
            old_repeat=0,
            new_repeat=1,
        )
    )
    await adapter.handle_event(
        PlayerShuffleChanged(
            player_id="leader",
            old_shuffle=0,
            new_shuffle=1,
        )
    )

    assert leader.sync_queue_from_lms.call_count == 4


async def test_handle_event_ignores_non_lyrion_player() -> None:
    """Adapter should ignore events when target player is absent or wrong type."""
    provider, _leader, _child = _build_provider_and_players()
    adapter = _build_adapter(provider)
    provider.get_runtime_player = MagicMock(return_value=None)

    await adapter.handle_event(
        PlayerStatusUpdated(
            player_id="ghost",
            status={"mode": "play"},
            is_initial=True,
        )
    )
