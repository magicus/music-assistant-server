"""Unit tests for LMS sync-topology mapping in the CometD event adapter."""

from __future__ import annotations

from unittest.mock import MagicMock

from music_assistant.providers.lyrion_player.cometd_event_adapter import LyrionCometDEventAdapter
from music_assistant.providers.lyrion_player.player import LyrionPlayer


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

    return provider, leader, child


def test_apply_status_maps_sync_slaves_to_group_members() -> None:
    """Leader status with sync_slaves should surface as MA group_members."""
    provider, leader, _child = _build_provider_and_players()
    adapter = LyrionCometDEventAdapter(provider)

    adapter.apply_status(
        leader,
        {
            "mode": "play",
            "sync_slaves": "child",
        },
    )

    assert leader.group_members == ["leader", "child"]


def test_apply_status_refreshes_related_players_on_topology_change() -> None:
    """Topology changes should trigger related-player state refresh."""
    provider, leader, child = _build_provider_and_players()
    adapter = LyrionCometDEventAdapter(provider)

    original_child_update_state = child.update_state
    child.update_state = MagicMock(side_effect=original_child_update_state)

    adapter.apply_status(
        leader,
        {
            "mode": "play",
            "sync_slaves": [{"playerid": "child"}],
        },
    )

    adapter.apply_status(
        leader,
        {
            "mode": "stop",
        },
    )

    assert leader.group_members == []
    assert child.update_state.call_count >= 1
