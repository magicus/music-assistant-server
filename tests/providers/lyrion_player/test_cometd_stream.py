"""Tests for Lyrion CometD serverstatus-driven rediscovery."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.errors import MusicAssistantError, ProviderUnavailableError

from music_assistant.providers.lyrion.cometd.stream import LyrionCometDEventStream
from music_assistant.providers.lyrion_player.cometd_events import LmsPlayerPlaylistChangedEvent
from music_assistant.providers.lyrion_player.provider import LyrionPlayerProvider
from tests.providers.lyrion.fake_lms_server import FakeLmsServer
from tests.providers.lyrion.rpc_test_doubles import FakeResponse


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
        self.get_player_status = AsyncMock(return_value={})
        self.discovery_calls = 0

    def schedule_players_discovery(self) -> None:
        """Record rediscovery scheduling calls."""
        self.discovery_calls += 1


class _StubCometDProvider(_StubProvider):
    """Provider stub with JSON-RPC endpoints for CometD unit tests."""

    def __init__(self, player_ids: list[str]) -> None:
        super().__init__(player_ids)
        self.mass.http_session = SimpleNamespace(post=MagicMock())

    def get_configured_host(self) -> str | None:
        return "127.0.0.1"

    def get_configured_port(self) -> int | None:
        return 9000


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


async def test_playerstatus_playlist_change_detects_index_only_navigation() -> None:
    """Index-only navigation should still emit a playlist-change event."""
    provider = _StubProvider(["player_a"])
    emitted_events: list[object] = []

    async def _capture_event(event: object) -> None:
        emitted_events.append(event)

    stream = LyrionCometDEventStream(cast("Any", provider), _capture_event)

    await stream._handle_player_status(
        "player_a",
        {
            "mode": "play",
            "playlist_tracks": 3,
            "playlist_cur_index": 0,
            "playlist_timestamp": 10.0,
        },
    )
    await stream._handle_player_status(
        "player_a",
        {
            "mode": "play",
            "playlist_tracks": 3,
            "playlist_cur_index": 1,
            "playlist_timestamp": 10.0,
        },
    )

    assert any(isinstance(event, LmsPlayerPlaylistChangedEvent) for event in emitted_events)


async def test_wait_for_player_status_update_observes_new_status() -> None:
    """Status waiters should resolve once a fresher CometD status arrives."""
    provider = _StubProvider(["player_a"])
    stream = LyrionCometDEventStream(cast("Any", provider), _noop_event_callback)

    baseline = stream.get_last_player_status_seen_at("player_a")
    wait_task = asyncio.create_task(
        stream.wait_for_player_status_update("player_a", baseline, timeout=1)
    )
    await asyncio.sleep(0)

    await stream._handle_message(
        {
            "channel": "/abc/slim/playerstatus/player_a",
            "data": {"mode": "play"},
        }
    )

    assert await wait_task


async def test_stale_playerstatus_is_resubscribed_and_cache_cleared() -> None:
    """Stale CometD playerstatus should be dropped and marked for refresh."""
    provider = _StubProvider(["player_a"])
    stream = LyrionCometDEventStream(cast("Any", provider), _noop_event_callback)

    await stream._handle_message(
        {
            "channel": "/abc/slim/playerstatus/player_a",
            "data": {"mode": "play", "playlist_tracks": 1},
        }
    )
    stream._status_seen_at["player_a"] = 0.0
    stream._subscribed_player_ids.add("player_a")

    await stream._refresh_stale_player_subscriptions()

    assert "player_a" not in stream._subscribed_player_ids
    assert "player_a" in stream._pending_player_ids
    assert "player_a" not in stream._status_by_player


async def test_session_reset_clears_status_cache() -> None:
    """Reconnect resets should clear volatile playerstatus cache state."""
    provider = _StubProvider(["player_a"])
    stream = LyrionCometDEventStream(cast("Any", provider), _noop_event_callback)

    await stream._handle_message(
        {
            "channel": "/abc/slim/playerstatus/player_a",
            "data": {"mode": "play"},
        }
    )
    stream._pending_player_ids.add("player_a")
    stream._subscribed_player_ids.add("player_a")

    stream._reset_session_state()

    assert stream._client_id is None
    assert not stream._pending_player_ids
    assert not stream._subscribed_player_ids
    assert not stream._status_by_player
    assert not stream._status_seen_at


async def test_watchdog_restarts_when_all_subscriptions_go_stale() -> None:
    """The watchdog should restart the session if the whole stream goes stale."""
    provider = _StubProvider(["player_a"])
    stream = LyrionCometDEventStream(cast("Any", provider), _noop_event_callback)

    stream._client_id = "client-1"
    stream._subscribed_player_ids.add("player_a")
    stream._status_seen_at["player_a"] = 0.0

    restart_calls: list[list[str]] = []

    async def _restart_stale_session(stale_player_ids: list[str]) -> None:
        restart_calls.append(stale_player_ids)

    cast("Any", stream)._restart_stale_session = _restart_stale_session

    await stream._run_watchdog_tick()

    assert restart_calls == [["player_a"]]


async def test_playback_status_arms_track_end_expectation() -> None:
    """Play status with time+duration should arm a track-end expectation."""
    provider = _StubProvider(["player_a"])
    stream = LyrionCometDEventStream(cast("Any", provider), _noop_event_callback)

    await stream._handle_message(
        {
            "channel": "/abc/slim/playerstatus/player_a",
            "data": {
                "mode": "play",
                "time": 95,
                "duration": 100,
                "playlist_cur_index": 3,
            },
        }
    )

    assert "player_a" in stream._track_end_expectations


async def test_track_end_due_triggers_implicit_recovery() -> None:
    """A due track-end expectation should trigger implicit status recovery."""
    provider = _StubProvider(["player_a"])
    stream = LyrionCometDEventStream(cast("Any", provider), _noop_event_callback)

    await stream._handle_message(
        {
            "channel": "/abc/slim/playerstatus/player_a",
            "data": {
                "mode": "play",
                "time": 1,
                "duration": 1,
                "playlist_cur_index": 0,
            },
        }
    )
    expectation = stream._track_end_expectations["player_a"]
    expectation.expected_transition_at = 0.0

    calls: list[tuple[str, str]] = []

    async def _recover_expected_status(player_id: str, reason: str) -> None:
        calls.append((player_id, reason))

    cast("Any", stream)._recover_expected_status = _recover_expected_status

    await stream._run_expectation_tick()

    assert calls == [("player_a", "track-end transition")]


async def test_active_state_timeout_triggers_implicit_recovery() -> None:
    """Active runtime state without fresh updates should trigger implicit recovery."""
    provider = _StubProvider(["player_a"])
    stream = LyrionCometDEventStream(cast("Any", provider), _noop_event_callback)

    await stream._handle_message(
        {
            "channel": "/abc/slim/playerstatus/player_a",
            "data": {
                "mode": "pause",
                "playlist_tracks": 4,
            },
        }
    )
    stream._status_seen_at["player_a"] = 0.0

    calls: list[tuple[str, str]] = []

    async def _recover_expected_status(player_id: str, reason: str) -> None:
        calls.append((player_id, reason))

    cast("Any", stream)._recover_expected_status = _recover_expected_status

    await stream._run_expectation_tick()

    assert calls == [("player_a", "active-state timeout")]


async def test_verify_player_status_expectation_falls_back_to_polling_with_fake_lms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fallback polling should recover state when CometD stays silent."""
    provider = _StubProvider(["player_a"])
    stream = LyrionCometDEventStream(cast("Any", provider), _noop_event_callback)
    fake_server = FakeLmsServer()
    await fake_server.connect_player("player_a", "Kitchen", "test")

    polls = 0

    async def _get_player_status(player_id: str) -> dict[str, Any]:
        nonlocal polls
        polls += 1
        if polls == 2:
            fake_server.set_player_mode(player_id, "play")
        return await fake_server.handle_jsonrpc_command(player_id, ["status", "-", 1])

    provider.get_player_status = AsyncMock(side_effect=_get_player_status)
    monkeypatch.setattr(
        stream,
        "wait_for_player_status_update",
        AsyncMock(return_value=False),
    )

    assert await stream.verify_player_status_expectation(
        "player_a",
        baseline=None,
        expectation=lambda status: status.get("mode") == "play",
        expected_state="play",
    )
    assert polls == 2
    assert stream.get_player_status_snapshot("player_a") is not None
    assert stream.get_player_status_snapshot("player_a")["mode"] == "play"


@pytest.mark.parametrize("body", [None, 1, "oops"])
async def test_post_rejects_non_object_and_non_list_payloads(body: Any) -> None:
    """CometD POST helper should reject scalar JSON payloads cleanly."""
    provider = _StubCometDProvider(["player_a"])
    provider.mass.http_session.post = MagicMock(return_value=FakeResponse(body))
    stream = LyrionCometDEventStream(cast("Any", provider), _noop_event_callback)

    with pytest.raises(ProviderUnavailableError, match="JSON object or list"):
        await stream._post([], timeout=1)


async def test_start_creates_listener_and_support_tasks() -> None:
    """Starting stream should create listener/watchdog/expectation tasks."""
    provider = _StubProvider(["player_a"])
    provider.unloading = True
    stream = LyrionCometDEventStream(cast("Any", provider), _noop_event_callback)

    stream.start()

    assert stream._task is not None
    assert stream._watchdog_task is not None
    assert stream._expectation_task is not None
    await stream._task
    await stream._watchdog_task
    await stream._expectation_task


async def test_start_reuses_running_listener_and_restarts_side_tasks() -> None:
    """Starting again with active listener should only recreate side tasks when needed."""
    provider = _StubProvider(["player_a"])
    stream = LyrionCometDEventStream(cast("Any", provider), _noop_event_callback)
    stream._task = asyncio.create_task(asyncio.sleep(5))
    stream._watchdog_task = asyncio.create_task(asyncio.sleep(0))
    await stream._watchdog_task
    stream._expectation_task = asyncio.create_task(asyncio.sleep(0))
    await stream._expectation_task
    provider.unloading = True

    stream.start()

    assert stream._task is not None and not stream._task.done()
    assert stream._watchdog_task is not None
    assert stream._expectation_task is not None
    stream._task.cancel()
    with suppress(asyncio.CancelledError):
        await stream._task


async def test_stop_cancels_tasks_disconnects_and_resets_state() -> None:
    """Stopping should cancel active tasks, disconnect session and clear state."""
    provider = _StubProvider(["player_a"])
    stream = LyrionCometDEventStream(cast("Any", provider), _noop_event_callback)
    stream._task = asyncio.create_task(asyncio.sleep(5))
    stream._watchdog_task = asyncio.create_task(asyncio.sleep(5))
    stream._expectation_task = asyncio.create_task(asyncio.sleep(5))
    stream._client_id = "cid"
    stream._status_by_player["player_a"] = {"mode": "play"}
    stream._bayeux.disconnect = AsyncMock(return_value=None)

    await stream.stop()

    stream._bayeux.disconnect.assert_awaited_once()
    assert stream._task is None
    assert stream._watchdog_task is None
    assert stream._expectation_task is None
    assert stream._client_id is None
    assert not stream._status_by_player


async def test_run_session_initializes_client_and_subscriptions() -> None:
    """One session run should handshake, subscribe and start connect loop."""
    provider = _StubProvider(["player_a", "player_b"])
    stream = LyrionCometDEventStream(cast("Any", provider), _noop_event_callback)
    stream._bayeux.open_session = AsyncMock(return_value="cid")
    stream._bayeux.run_connect_loop = AsyncMock(return_value=None)
    stream._subscribe_server_status = AsyncMock(return_value=None)

    await stream._run_session()

    assert stream._client_id == "cid"
    assert stream._pending_player_ids == {"player_a", "player_b"}
    stream._subscribe_server_status.assert_awaited_once()
    stream._bayeux.run_connect_loop.assert_awaited_once()


async def test_flush_pending_player_subscriptions_requeues_failures() -> None:
    """Failed player subscription attempts should be put back into pending set."""
    provider = _StubProvider(["player_a"])
    stream = LyrionCometDEventStream(cast("Any", provider), _noop_event_callback)
    stream._client_id = "cid"
    stream._pending_player_ids = {"ok", "fail"}
    stream._refresh_stale_player_subscriptions = AsyncMock(return_value=None)

    async def _subscribe(player_id: str) -> None:
        if player_id == "fail":
            raise ProviderUnavailableError("down")

    stream._subscribe_player_status = AsyncMock(side_effect=_subscribe)

    await stream._flush_pending_player_subscriptions()

    assert stream._pending_player_ids == {"fail"}


async def test_next_expectation_delay_covers_idle_near_end_and_backoff() -> None:
    """Expectation delay should adapt between idle and near-track-end timings."""
    provider = _StubProvider(["player_a"])
    stream = LyrionCometDEventStream(cast("Any", provider), _noop_event_callback)

    idle = stream._next_expectation_delay()
    assert idle > 0

    now = asyncio.get_running_loop().time()
    stream._track_end_expectations["player_a"] = SimpleNamespace(expected_transition_at=now + 0.1)
    near_end = stream._next_expectation_delay()
    assert near_end <= idle


async def test_emit_event_logs_music_assistant_errors() -> None:
    """Event emit should swallow MA errors and keep loop alive."""
    provider = _StubProvider(["player_a"])

    async def _raising_callback(_event: object) -> None:
        raise MusicAssistantError("boom")

    stream = LyrionCometDEventStream(cast("Any", provider), _raising_callback)

    await stream._emit_event(SimpleNamespace(player_id="player_a"))

    provider.logger.warning.assert_called_once()
