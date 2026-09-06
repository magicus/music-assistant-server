"""Tests for Lyrion CometD serverstatus-driven rediscovery."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

from music_assistant.providers.lyrion.lyrion_cometd import LyrionCometDEventStream
from music_assistant.providers.lyrion_player.cometd_events import LmsPlayerPlaylistChangedEvent
from music_assistant.providers.lyrion_player.provider import LyrionPlayerProvider


async def _noop_event_callback(_event: object) -> None:
    """Ignore emitted CometD events."""


class _StubProvider:
    """Tiny provider stub for CometD stream unit tests."""

    def __init__(self, player_ids: list[str]) -> None:
        """Initialize a provider stub with known players."""
        self.instance_id = "lyrion_player.test"
        self.players = [SimpleNamespace(player_id=player_id) for player_id in player_ids]
        self.unloading = False
        self.mass = SimpleNamespace(
            create_task=asyncio.create_task,
            players=SimpleNamespace(get_player=lambda _player_id: None),
        )
        self.logger = MagicMock()
        self.discovery_calls = 0

    def schedule_players_discovery(self) -> None:
        """Record rediscovery scheduling calls."""
        self.discovery_calls += 1


async def test_serverstatus_players_loop_triggers_on_roster_change() -> None:
    """Serverstatus player-id set changes should schedule rediscovery exactly once per change."""
    provider = _StubProvider(["player_a"])
    stream = LyrionCometDEventStream(provider, _noop_event_callback)  # type: ignore[arg-type]

    await stream._handle_message(
        {
            "channel": "/abc/slim/serverstatus",
            "data": {"players_loop": [{"playerid": "player_a"}]},
        }
    )
    assert provider.discovery_calls == 0

    await stream._handle_message(
        {
            "channel": "/abc/slim/serverstatus",
            "data": {
                "players_loop": [
                    {"playerid": "player_a"},
                    {"playerid": "player_b"},
                ]
            },
        }
    )
    assert provider.discovery_calls == 1

    await stream._handle_message(
        {
            "channel": "/abc/slim/serverstatus",
            "data": {
                "players_loop": [
                    {"playerid": "player_b"},
                    {"playerid": "player_a"},
                ]
            },
        }
    )
    assert provider.discovery_calls == 1


async def test_serverstatus_player_count_fallback_triggers_when_changed() -> None:
    """Fallback to player-count diffing when serverstatus omits players_loop."""
    provider = _StubProvider(["player_a"])
    stream = LyrionCometDEventStream(provider, _noop_event_callback)  # type: ignore[arg-type]

    await stream._handle_message(
        {
            "channel": "/abc/slim/serverstatus",
            "data": {"player count": 1},
        }
    )
    assert provider.discovery_calls == 0

    await stream._handle_message(
        {
            "channel": "/abc/slim/serverstatus",
            "data": {"player count": 2},
        }
    )
    assert provider.discovery_calls == 1


async def test_schedule_players_discovery_coalesces_overlapping_triggers() -> None:
    """Overlapping discovery triggers should coalesce into one task with one replay pass."""
    provider = LyrionPlayerProvider.__new__(LyrionPlayerProvider)
    provider.logger = MagicMock()
    provider.mass = cast("Any", SimpleNamespace(create_task=asyncio.create_task))
    provider.unloading = False
    provider._discover_players_task = None
    provider._discover_players_again = False

    gate = asyncio.Event()
    calls = 0

    async def _discover_players() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            await gate.wait()

    cast("Any", provider).discover_players = _discover_players

    provider.schedule_players_discovery()
    await asyncio.sleep(0)
    provider.schedule_players_discovery()
    gate.set()

    task = cast("Any", provider._discover_players_task)
    assert task is not None
    await task
    assert calls == 2


async def test_serverstatus_connected_updates_player_availability() -> None:
    """Serverstatus connected flag should update MA availability for known players."""
    provider = _StubProvider(["player_a"])
    stream = LyrionCometDEventStream(cast("Any", provider), _noop_event_callback)

    updates = 0

    def _update_state() -> None:
        nonlocal updates
        updates += 1

    ma_player = SimpleNamespace(
        provider=SimpleNamespace(instance_id=provider.instance_id),
        _attr_available=True,
        update_state=_update_state,
    )
    provider.mass.players = SimpleNamespace(
        get_player=lambda player_id: ma_player if player_id == "player_a" else None,
    )

    await stream._handle_message(
        {
            "channel": "/abc/slim/serverstatus",
            "data": {
                "players_loop": [{"playerid": "player_a", "connected": 0}],
            },
        }
    )
    assert ma_player._attr_available is False
    assert updates == 1


async def test_invalid_player_status_triggers_rediscovery() -> None:
    """An invalid-player status payload should schedule rediscovery."""
    provider = _StubProvider(["player_a"])
    stream = LyrionCometDEventStream(cast("Any", provider), _noop_event_callback)

    await stream._handle_message(
        {
            "channel": "/abc/slim/playerstatus/player_a",
            "data": {"error": "invalid player"},
        }
    )

    assert provider.discovery_calls == 1


async def test_playerstatus_playlist_change_detects_canonical_playlist_tracks_key() -> None:
    """Playlist-change detection should work for LMS payloads using `playlist_tracks`."""
    provider = _StubProvider(["player_a"])
    emitted_events: list[object] = []

    async def _capture_event(event: object) -> None:
        emitted_events.append(event)

    stream = LyrionCometDEventStream(cast("Any", provider), _capture_event)

    await stream._handle_player_status(
        "player_a",
        {
            "mode": "play",
            "playlist_tracks": 2,
            "playlist_cur_index": 0,
        },
    )
    await stream._handle_player_status(
        "player_a",
        {
            "mode": "play",
            "playlist_tracks": 3,
            "playlist_cur_index": 0,
        },
    )

    assert any(isinstance(event, LmsPlayerPlaylistChangedEvent) for event in emitted_events)
