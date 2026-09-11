"""Unit tests for non-live helper behavior in the Lyrion player provider."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from music_assistant_models.errors import MusicAssistantError, ProviderUnavailableError

from music_assistant.providers.lyrion_player.player import LyrionPlayer
from music_assistant.providers.lyrion_player.provider import (
    PLAYERS_BATCH_SIZE,
    LyrionPlayerProvider,
)
from pylyrion.errors import LyrionTimeoutError
from pylyrion.server_control import LyrionServerControl


def _build_provider_stub() -> LyrionPlayerProvider:
    """Create a provider instance via __new__ with mocked dependencies."""
    provider = LyrionPlayerProvider.__new__(LyrionPlayerProvider)
    provider.config = SimpleNamespace(instance_id="lyrion_player.test", name=None)
    provider.manifest = SimpleNamespace(domain="lyrion_player", type="player", name="Lyrion")
    provider.unloading = False
    provider.logger = MagicMock()

    mass = SimpleNamespace()
    mass.create_task = asyncio.create_task
    mass.http_session = MagicMock()
    mass.streams = SimpleNamespace(
        base_url="http://127.0.0.1:8095",
        register_dynamic_route=MagicMock(return_value=lambda: None),
    )
    mass.subscribe = MagicMock(return_value=lambda: None)
    mass.config = SimpleNamespace(
        create_default_player_config=MagicMock(),
        get_base_player_config=MagicMock(return_value=MagicMock()),
    )
    mass.players = SimpleNamespace(
        register=AsyncMock(),
        unregister=AsyncMock(),
        get_player=MagicMock(return_value=None),
        iter_players=MagicMock(return_value=[]),
    )
    mass.player_queues = SimpleNamespace(
        get_item=MagicMock(return_value=None),
        player_media_from_queue_item=AsyncMock(),
    )
    provider.mass = cast("Any", mass)

    provider._discover_players_task = None
    provider._discover_players_again = False
    provider._unregister_stream_redirect_route = None
    provider._status_event_adapter = MagicMock()
    provider._unsubscribe_status_events = None
    provider._status_stream = SimpleNamespace(
        start=MagicMock(),
        stop=AsyncMock(),
        mark_player_seen=MagicMock(),
        mark_player_removed=MagicMock(),
        get_last_player_status_seen_at=MagicMock(return_value=1.0),
        get_player_status_snapshot=MagicMock(return_value={"mode": "play"}),
        wait_for_player_status_update=AsyncMock(return_value=True),
        verify_player_status_expectation=AsyncMock(return_value=True),
    )
    provider.lyrion_server = LyrionServerControl(
        get_players_client=provider._build_pylyrion_player_client,
        status_stream=provider._status_stream,
        unavailable_error_factory=lambda err: ProviderUnavailableError(str(err)),
    )
    provider.get_setup_value = MagicMock(
        side_effect=lambda key, default=None: {
            "lms_host": "127.0.0.1",
            "port": 9000,
        }.get(key, default)
    )
    return provider


def test_build_stream_redirect_url_includes_required_query_params() -> None:
    """Redirect URL should include required queue context and optional ma_uri."""
    provider = _build_provider_stub()

    url = provider.build_stream_redirect_url("p1", "q1", "qi1", "spotify://track/1")
    assert "player_id=p1" in url
    assert "queue_id=q1" in url
    assert "queue_item_id=qi1" in url
    assert "ma_uri=" in url

    url_without_uri = provider.build_stream_redirect_url("p1", "q1", "qi1", None)
    assert "ma_uri=" not in url_without_uri


def test_get_configured_host_and_port_parsing() -> None:
    """Configured host/port helpers should normalize valid input and fallback on invalid."""
    provider = _build_provider_stub()
    provider.get_setup_value = MagicMock(
        side_effect=lambda key, default=None: {
            "lms_host": " 127.0.0.1 ",
            "port": "9090",
        }.get(key, default)
    )
    assert provider.get_configured_host() == "127.0.0.1"
    assert provider.get_configured_port() == 9090

    provider.get_setup_value = MagicMock(
        side_effect=lambda key, default=None: {"lms_host": " ", "port": "bad"}.get(key, default)
    )
    assert provider.get_configured_host() is None
    assert provider.get_configured_port(default=1234) == 1234


@pytest.mark.asyncio
async def test_loaded_and_unload_manage_routes_discovery_and_status_stream() -> None:
    """Provider load/unload should wire route registration, discovery and status stream lifecycle."""
    provider = _build_provider_stub()
    provider.discover_players = AsyncMock()

    await provider.loaded_in_mass()

    provider.mass.streams.register_dynamic_route.assert_called_once()
    provider.discover_players.assert_awaited_once()
    provider._status_stream.start.assert_called_once()

    with patch("music_assistant.models.provider.Provider.unload", new=AsyncMock()) as unload_base:
        await provider.unload(False)
    provider._status_stream.stop.assert_awaited_once()
    unload_base.assert_awaited_once()


def test_schedule_players_discovery_noops_when_unloading() -> None:
    """Rediscovery scheduling should not queue work while unloading."""
    provider = _build_provider_stub()
    provider.unloading = True
    provider.schedule_players_discovery()
    assert provider._discover_players_task is None


@pytest.mark.asyncio
async def test_discover_players_registers_new_and_unloads_missing() -> None:
    """Discovery should register unknown players and unregister stale known players."""
    provider = _build_provider_stub()
    players = cast("Any", provider.mass.players)

    known_gone_player = SimpleNamespace(player_id="gone")
    players.iter_players = MagicMock(return_value=[known_gone_player])

    provider.lyrion_server.get_players_page = AsyncMock(
        side_effect=[
            [
                {"playerid": "new-1", "name": "Kitchen", "model": "Squeeze"},
                {"playerid": "new-2", "name": "Office", "model": "Squeeze"},
            ],
            [],
        ]
    )

    await provider.discover_players()

    assert players.register.await_count == 2
    provider._status_stream.mark_player_seen.assert_any_call("new-1")
    provider._status_stream.mark_player_seen.assert_any_call("new-2")
    provider._status_stream.mark_player_removed.assert_called_once_with("gone")
    cast("Any", players.unregister).assert_awaited_once_with("gone")


@pytest.mark.asyncio
async def test_handle_get_stream_url_happy_path_redirects() -> None:
    """Route handler should resolve queue item to fresh stream URL and redirect."""
    provider = _build_provider_stub()
    players = cast("Any", provider.mass.players)
    queues = cast("Any", provider.mass.player_queues)

    request = MagicMock()
    request.query = {"player_id": "p1", "queue_id": "p1", "queue_item_id": "qi1"}

    player = MagicMock(spec=LyrionPlayer)
    player.provider = provider
    players.get_player.return_value = player
    queue_item = SimpleNamespace(queue_id="p1", queue_item_id="qi1", uri="x")
    queues.get_item.return_value = queue_item
    queues.player_media_from_queue_item = AsyncMock(return_value=SimpleNamespace())
    provider.mass.streams.resolve_stream_url = AsyncMock(
        return_value="http://stream.local/fresh.mp3"
    )

    with pytest.raises(web.HTTPFound) as exc:
        await provider._handle_get_stream_url(request)

    assert exc.value.location == "http://stream.local/fresh.mp3"


@pytest.mark.asyncio
async def test_handle_get_stream_url_rejects_invalid_input_and_resolution_failures() -> None:
    """Route handler should reject bad query/player/item and map resolution errors to 404."""
    provider = _build_provider_stub()
    players = cast("Any", provider.mass.players)
    queues = cast("Any", provider.mass.player_queues)

    bad_request = MagicMock()
    bad_request.query = {"player_id": "", "queue_id": "", "queue_item_id": ""}
    with pytest.raises(web.HTTPBadRequest):
        await provider._handle_get_stream_url(bad_request)

    request = MagicMock()
    request.query = {"player_id": "p1", "queue_id": "p1", "queue_item_id": "qi1"}

    players.get_player.return_value = None
    with pytest.raises(web.HTTPNotFound, match="Unknown Lyrion player"):
        await provider._handle_get_stream_url(request)

    request_player = MagicMock(spec=LyrionPlayer)
    request_player.provider = provider
    players.get_player.return_value = request_player
    queues.get_item.return_value = None
    with pytest.raises(web.HTTPNotFound, match="Unknown queue item"):
        await provider._handle_get_stream_url(request)

    queues.get_item.return_value = SimpleNamespace(queue_id="p1", queue_item_id="qi1", uri="x")
    queues.player_media_from_queue_item = AsyncMock(side_effect=MusicAssistantError("boom"))
    with pytest.raises(web.HTTPNotFound, match="Unable to resolve stream URL"):
        await provider._handle_get_stream_url(request)


@pytest.mark.asyncio
async def test_provider_init_wires_status_adapter_and_stream() -> None:
    """Constructor should create status adapter and stream after base init."""
    with (
        patch(
            "music_assistant.models.player_provider.PlayerProvider.__init__",
            return_value=None,
        ),
        patch(
            "music_assistant.providers.lyrion_player.provider.LyrionStatusEventAdapter"
        ) as mock_adapter_cls,
        patch(
            "music_assistant.providers.lyrion_player.provider.PlayerStatusStream"
        ) as mock_stream_cls,
    ):
        adapter = MagicMock()
        adapter.handle_event = AsyncMock()
        stream = MagicMock()
        mock_adapter_cls.return_value = adapter
        mock_stream_cls.return_value = stream

        provider = LyrionPlayerProvider()

    assert provider._status_event_adapter is adapter
    assert provider._status_stream is stream
    mock_stream_cls.assert_called_once()
    assert (
        mock_stream_cls.call_args.kwargs["post_messages"] == provider._post_status_stream_messages
    )
    assert provider._unsubscribe_status_events is not None


@pytest.mark.asyncio
async def test_get_config_entries_and_handle_async_init() -> None:
    """Provider should expose expected config entries and validate endpoint on init."""
    provider = _build_provider_stub()
    provider.get_configured_host = MagicMock(return_value="127.0.0.1")
    provider.get_configured_port = MagicMock(return_value=9000)
    provider.manifest.name = "Lyrion"

    entries = await provider.get_config_entries()
    assert len(entries) == 2

    with patch(
        "music_assistant.providers.lyrion_player.provider.validate_lms_endpoint",
        new=AsyncMock(return_value=None),
    ) as validate:
        await provider.handle_async_init()

    validate.assert_awaited_once()
    provider.logger.debug.assert_called_once()


@pytest.mark.asyncio
async def test_provider_status_wrapper_methods_delegate_to_stream() -> None:
    """Provider convenience methods should delegate to status stream internals."""
    provider = _build_provider_stub()

    assert provider.get_last_status_seen_at("p1") == 1.0
    assert provider.get_cached_status("p1") == {"mode": "play"}

    assert await provider.wait_for_status_update("p1", since=0.5, timeout=2)
    provider._status_stream.wait_for_player_status_update.assert_awaited_once_with("p1", 0.5, 2)

    expectation = lambda status: status.get("mode") == "play"
    assert await provider.verify_status_expectation(
        "p1",
        baseline=1.0,
        expectation=expectation,
        expected_state="play",
    )
    provider._status_stream.verify_player_status_expectation.assert_awaited_once_with(
        "p1",
        1.0,
        expectation,
        "play",
    )


@pytest.mark.asyncio
async def test_provider_command_and_remove_delegate() -> None:
    """remove_player and send_player_command should delegate via pylyrion."""
    provider = _build_provider_stub()
    players = cast("Any", provider.mass.players)
    player_client = AsyncMock()
    player_client.players.send_command = AsyncMock(return_value={"ok": True})

    with patch(
        "music_assistant.providers.lyrion_player.provider.LyrionClient",
        return_value=player_client,
    ):
        result = await provider.send_player_command("p1", ["stop"])

    assert result == {"ok": True}
    player_client.players.send_command.assert_awaited_once_with("p1", ["stop"])

    await provider.remove_player("p1")
    players.unregister.assert_awaited_once_with("p1", True)


def test_apply_status_update_delegates_to_adapter() -> None:
    """apply_status_update should pass through untouched payload to adapter."""
    provider = _build_provider_stub()
    player = MagicMock(spec=LyrionPlayer)
    status = {"mode": "play"}

    provider.apply_status_update(player, status)

    provider._status_event_adapter.apply_status.assert_called_once_with(player, status)


@pytest.mark.asyncio
async def test_handle_get_stream_url_rejects_queue_id_mismatch() -> None:
    """Route handler should reject queue ids that do not match player id."""
    provider = _build_provider_stub()
    request = MagicMock()
    request.query = {"player_id": "p1", "queue_id": "other", "queue_item_id": "qi1"}

    player = MagicMock(spec=LyrionPlayer)
    player.provider = provider
    provider.mass.players.get_player.return_value = player

    with pytest.raises(web.HTTPBadRequest, match="queue_id must match player_id"):
        await provider._handle_get_stream_url(request)


@pytest.mark.asyncio
async def test_handle_get_stream_url_rejects_foreign_provider_player() -> None:
    """Route handler should reject Lyrion players not owned by this provider instance."""
    provider = _build_provider_stub()
    request = MagicMock()
    request.query = {"player_id": "p1", "queue_id": "p1", "queue_item_id": "qi1"}

    player = MagicMock(spec=LyrionPlayer)
    player.provider = SimpleNamespace(instance_id="foreign")
    provider.mass.players.get_player.return_value = player

    with pytest.raises(web.HTTPNotFound, match="Unknown Lyrion player"):
        await provider._handle_get_stream_url(request)


@pytest.mark.asyncio
async def test_run_discover_players_loop_handles_retrigger_and_unloading() -> None:
    """Discovery loop should repeat once on retrigger and skip when unloading."""
    provider = _build_provider_stub()
    calls = 0

    async def _discover() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            provider._discover_players_again = True

    provider.discover_players = AsyncMock(side_effect=_discover)

    await provider._run_discover_players_loop()
    assert calls == 2

    provider.unloading = True
    provider.discover_players = AsyncMock()
    await provider._run_discover_players_loop()
    provider.discover_players.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_discover_players_done_clears_task_and_logs_exception() -> None:
    """Completion callback should clear bookkeeping and report non-cancel failures."""
    provider = _build_provider_stub()

    ok_task = asyncio.create_task(asyncio.sleep(0))
    provider._discover_players_task = ok_task
    await ok_task
    provider._handle_discover_players_done(ok_task)
    assert provider._discover_players_task is None

    async def _fail() -> None:
        raise RuntimeError("boom")

    fail_task = asyncio.create_task(_fail())
    provider._discover_players_task = fail_task
    with suppress(RuntimeError):
        await fail_task
    provider._handle_discover_players_done(fail_task)
    provider.logger.warning.assert_called()


@pytest.mark.asyncio
async def test_unload_cancels_discovery_task_when_present() -> None:
    """Unload should cancel an active discovery task before shutting down stream."""
    provider = _build_provider_stub()
    provider._discover_players_task = asyncio.create_task(asyncio.sleep(5))

    with patch("music_assistant.models.provider.Provider.unload", new=AsyncMock()) as unload_base:
        await provider.unload(False)

    assert provider._discover_players_task is None
    provider._status_stream.stop.assert_awaited_once()
    unload_base.assert_awaited_once()


@pytest.mark.asyncio
async def test_discover_players_handles_missing_ids_existing_players_and_paging() -> None:
    """Discovery should skip missing ids, update existing players and continue paged fetches."""
    provider = _build_provider_stub()
    existing = MagicMock(spec=LyrionPlayer)
    existing.sync_from_lms = AsyncMock(return_value=None)

    provider.mass.players.get_player = MagicMock(
        side_effect=lambda player_id: existing if player_id == "existing" else None
    )
    provider.lyrion_server.get_players_page = AsyncMock(
        side_effect=[
            [
                {},
                {"playerid": "existing", "name": "Kitchen", "model": "x"},
            ],
            [],
        ]
    )

    await provider.discover_players()

    existing.sync_from_lms.assert_awaited_once()
    provider._status_stream.mark_player_seen.assert_any_call("existing")
    provider.mass.players.register.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_player_status_uses_pylyrion_client() -> None:
    """get_player_status should use the pylyrion player client facade."""
    provider = _build_provider_stub()
    player_client = AsyncMock()
    player_client.players.get_status = AsyncMock(return_value={"mode": "play"})

    with patch(
        "music_assistant.providers.lyrion_player.provider.LyrionClient",
        return_value=player_client,
    ) as client_cls:
        status = await provider.get_player_status("p1")

    assert status == {"mode": "play"}
    client_cls.assert_called_once()
    player_client.players.get_status.assert_awaited_once_with("p1")


@pytest.mark.asyncio
async def test_transport_adapter_methods_use_pylyrion_client() -> None:
    """Transport adapter helpers should delegate play/pause/stop/power through pylyrion."""
    provider = _build_provider_stub()
    player_client = AsyncMock()
    player_client.players.play = AsyncMock(return_value={})
    player_client.players.pause = AsyncMock(return_value={})
    player_client.players.stop = AsyncMock(return_value={})
    player_client.players.set_power = AsyncMock(return_value={})
    player_client.players.sync_to = AsyncMock(return_value={})
    player_client.players.unsync = AsyncMock(return_value={})
    player_client.players.get_queue_status = AsyncMock(return_value={})
    player_client.players.set_queue_index = AsyncMock(return_value={})
    player_client.players.next_track = AsyncMock(return_value={})
    player_client.players.previous_track = AsyncMock(return_value={})
    player_client.players.set_volume = AsyncMock(return_value={})
    player_client.players.set_muted = AsyncMock(return_value={})
    player_client.players.seek = AsyncMock(return_value={})
    player_client.players.set_sync_volume = AsyncMock(return_value={})
    player_client.players.set_repeat_mode = AsyncMock(return_value={})
    player_client.players.set_shuffle_mode = AsyncMock(return_value={})
    player_client.players.clear_queue = AsyncMock(return_value={})
    player_client.players.add_track_id = AsyncMock(return_value={})
    player_client.players.add_url_to_queue = AsyncMock(return_value={})
    player_client.players.play_url = AsyncMock(return_value={})
    player_client.players.add_url = AsyncMock(return_value={})
    player_client.players.move_queue_item = AsyncMock(return_value={})
    player_client.players.delete_queue_item = AsyncMock(return_value={})

    with patch(
        "music_assistant.providers.lyrion_player.provider.LyrionClient",
        return_value=player_client,
    ):
        await provider.play_player("p1")
        await provider.pause_player("p1")
        await provider.stop_player("p1")
        await provider.set_player_power("p1", True)
        await provider.sync_player_to("p2", "p1")
        await provider.unsync_player("p2")
        await provider.get_player_queue_status("p1", limit=123)
        await provider.set_player_queue_index("p1", 9)
        await provider.next_player_track("p1")
        await provider.previous_player_track("p1")
        await provider.set_player_volume("p1", 37)
        await provider.set_player_muted("p1", True)
        await provider.seek_player("p1", 12)
        await provider.set_player_sync_volume("p1", False)
        await provider.set_player_repeat_mode("p1", 2)
        await provider.set_player_shuffle_mode("p1", 1)
        await provider.clear_player_queue("p1")
        await provider.add_player_track_id_to_queue("p1", "42", command="load")
        await provider.add_player_url_to_queue(
            "p1",
            "http://example/stream",
            title="Title",
            artist="Artist",
            album="Album",
        )
        await provider.play_player_url("p1", "http://example/play")
        await provider.append_player_url("p1", "http://example/add")
        await provider.move_player_queue_item("p1", 5, 2)
        await provider.delete_player_queue_item("p1", 4)

    player_client.players.play.assert_awaited_once_with("p1")
    player_client.players.pause.assert_awaited_once_with("p1")
    player_client.players.stop.assert_awaited_once_with("p1")
    player_client.players.set_power.assert_awaited_once_with("p1", True)
    player_client.players.sync_to.assert_awaited_once_with("p2", "p1")
    player_client.players.unsync.assert_awaited_once_with("p2")
    player_client.players.get_queue_status.assert_awaited_once_with(
        "p1",
        offset=0,
        limit=123,
    )
    player_client.players.set_queue_index.assert_awaited_once_with("p1", 9)
    player_client.players.next_track.assert_awaited_once_with("p1")
    player_client.players.previous_track.assert_awaited_once_with("p1")
    player_client.players.set_volume.assert_awaited_once_with("p1", 37)
    player_client.players.set_muted.assert_awaited_once_with("p1", True)
    player_client.players.seek.assert_awaited_once_with("p1", 12)
    player_client.players.set_sync_volume.assert_awaited_once_with("p1", False)
    player_client.players.set_repeat_mode.assert_awaited_once_with("p1", 2)
    player_client.players.set_shuffle_mode.assert_awaited_once_with("p1", 1)
    player_client.players.clear_queue.assert_awaited_once_with("p1")
    player_client.players.add_track_id.assert_awaited_once_with(
        "p1",
        "42",
        command="load",
    )
    player_client.players.add_url_to_queue.assert_awaited_once_with(
        "p1",
        "http://example/stream",
        title="Title",
        artist="Artist",
        album="Album",
    )
    player_client.players.play_url.assert_awaited_once_with("p1", "http://example/play")
    player_client.players.add_url.assert_awaited_once_with("p1", "http://example/add")
    player_client.players.move_queue_item.assert_awaited_once_with("p1", 5, 2)
    player_client.players.delete_queue_item.assert_awaited_once_with("p1", 4)


@pytest.mark.asyncio
async def test_transport_adapter_methods_map_pylyrion_errors() -> None:
    """Pylyrion transport failures should map to ProviderUnavailableError."""
    provider = _build_provider_stub()
    player_client = AsyncMock()
    player_client.players.play = AsyncMock(side_effect=LyrionTimeoutError("timeout"))

    with (
        patch(
            "music_assistant.providers.lyrion_player.provider.LyrionClient",
            return_value=player_client,
        ),
        pytest.raises(ProviderUnavailableError, match="timeout"),
    ):
        await provider.play_player("p1")


def test_get_configured_helpers_handle_invalid_types_and_none_port() -> None:
    """Host/port helpers should reject non-string hosts and preserve None port."""
    provider = _build_provider_stub()
    provider.get_setup_value = MagicMock(
        side_effect=lambda key, default=None: {
            "lms_host": 123,
            "port": None,
        }.get(key, default)
    )

    assert provider.get_configured_host() is None
    assert provider.get_configured_port() is None


def test_handle_discover_players_done_ignores_cancelled_task() -> None:
    """Done callback should no-op when task was cancelled."""
    provider = _build_provider_stub()

    class _CancelledTask:
        def cancelled(self) -> bool:
            return True

        def exception(self) -> Exception | None:
            return None

    task = cast("Any", _CancelledTask())
    provider._discover_players_task = task

    provider._handle_discover_players_done(task)

    assert provider._discover_players_task is None


@pytest.mark.asyncio
async def test_discover_players_breaks_immediately_when_lms_returns_no_players() -> None:
    """Discovery loop should stop when first page has no players_loop entries."""
    provider = _build_provider_stub()
    provider.lyrion_server.get_players_page = AsyncMock(return_value=[])

    await provider.discover_players()

    provider.lyrion_server.get_players_page.assert_awaited_once_with(0, PLAYERS_BATCH_SIZE)


@pytest.mark.asyncio
async def test_discover_players_advances_offset_on_full_page() -> None:
    """Discovery should request next page when LMS returns a full batch."""
    provider = _build_provider_stub()
    first_page = [
        {"playerid": f"p{idx}", "name": f"P{idx}", "model": "Squeeze"}
        for idx in range(PLAYERS_BATCH_SIZE)
    ]
    provider.lyrion_server.get_players_page = AsyncMock(side_effect=[first_page, []])

    await provider.discover_players()

    assert provider.lyrion_server.get_players_page.await_count == 2
    assert provider.lyrion_server.get_players_page.await_args_list[1].args[0] == PLAYERS_BATCH_SIZE
