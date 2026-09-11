"""Unit tests for pylyrion CometD playerstatus event normalization."""

from __future__ import annotations

from pylyrion.cometd.player_status_events import (
    PlayerPlaybackChanged,
    PlayerPlaylistChanged,
    PlayerPowerChanged,
    PlayerRepeatChanged,
    PlayerSeeked,
    PlayerShuffleChanged,
    PlayerStatusUpdated,
    PlayerVolumeChanged,
    merge_player_status,
)


def test_invalid_player_payload_returns_removal_signal() -> None:
    """Invalid-player payloads should return no merged status and one status-update event."""
    merged, events, invalid_player = merge_player_status(
        "player_a",
        previous={"mode": "play"},
        partial={"error": "invalid player"},
    )

    assert invalid_player is True
    assert merged is None
    assert len(events) == 1
    assert isinstance(events[0], PlayerStatusUpdated)
    assert events[0].is_initial is False


def test_initial_status_emits_only_status_updated() -> None:
    """First valid payload should only emit one initial status-updated event."""
    merged, events, invalid_player = merge_player_status(
        "player_a",
        previous=None,
        partial={"mode": "play", "playlist_cur_index": 0, "playlist_loop": [{"id": "a"}]},
    )

    assert invalid_player is False
    assert merged is not None
    assert len(events) == 1
    assert isinstance(events[0], PlayerStatusUpdated)
    assert events[0].is_initial is True


def test_runtime_delta_emits_all_transition_events() -> None:
    """Runtime deltas should emit all expected transition event types."""
    previous = {
        "mode": "play",
        "power": 0,
        "mixer volume": 10,
        "playlist repeat": 0,
        "playlist shuffle": 0,
        "time": 1,
        "playlist_cur_index": 0,
        "playlist_timestamp": 1.0,
        "playlist_tracks": 2,
        "playlist_loop": [{"id": "a"}, {"id": "b"}],
    }
    merged, events, invalid_player = merge_player_status(
        "player_a",
        previous=previous,
        partial={
            "mode": "pause",
            "power": 1,
            "mixer volume": 20,
            "playlist repeat": 1,
            "playlist shuffle": 1,
            "time": 2,
            "playlist_cur_index": 0,
            "playlist_timestamp": 2.0,
            "playlist_tracks": 3,
            "playlist_loop": [{"id": "a"}, {"id": "b"}],
        },
    )

    assert invalid_player is False
    assert merged is not None
    assert any(isinstance(event, PlayerStatusUpdated) for event in events)
    assert any(isinstance(event, PlayerPlaybackChanged) for event in events)
    assert any(isinstance(event, PlayerPowerChanged) for event in events)
    assert any(isinstance(event, PlayerVolumeChanged) for event in events)
    assert any(isinstance(event, PlayerRepeatChanged) for event in events)
    assert any(isinstance(event, PlayerShuffleChanged) for event in events)
    assert any(isinstance(event, PlayerSeeked) for event in events)
    assert any(isinstance(event, PlayerPlaylistChanged) for event in events)


def test_time_change_without_same_track_does_not_emit_seek_event() -> None:
    """Seek events require both statuses to point at the same active track."""
    previous = {
        "mode": "play",
        "time": 1,
        "playlist_cur_index": 0,
        "playlist_loop": [{"id": "a"}, {"id": "b"}],
    }
    _merged, events, _invalid_player = merge_player_status(
        "player_a",
        previous=previous,
        partial={
            "time": 2,
            "playlist_cur_index": 1,
            "playlist_loop": [{"id": "a"}, {"id": "b"}],
        },
    )

    assert not any(isinstance(event, PlayerSeeked) for event in events)
