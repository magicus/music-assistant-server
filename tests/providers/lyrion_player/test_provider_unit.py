"""Unit tests for non-live helper behavior in the Lyrion player provider."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from music_assistant_models.errors import MusicAssistantError

from music_assistant.providers.lyrion_player.player import LyrionPlayer
from music_assistant.providers.lyrion_player.provider import LyrionPlayerProvider


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
    provider._cometd_adapter = MagicMock()
    provider._cometd_stream = SimpleNamespace(
        start=MagicMock(),
        stop=AsyncMock(),
        mark_player_seen=MagicMock(),
        mark_player_removed=MagicMock(),
        get_last_player_status_seen_at=MagicMock(return_value=1.0),
        get_player_status_snapshot=MagicMock(return_value={"mode": "play"}),
        wait_for_player_status_update=AsyncMock(return_value=True),
        verify_player_status_expectation=AsyncMock(return_value=True),
    )
    provider.get_setup_value = MagicMock(side_effect=lambda key, default=None: default)
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
async def test_loaded_and_unload_manage_routes_discovery_and_cometd() -> None:
    """Provider load/unload should wire route registration, discovery and CometD lifecycle."""
    provider = _build_provider_stub()
    provider.discover_players = AsyncMock()

    await provider.loaded_in_mass()

    provider.mass.streams.register_dynamic_route.assert_called_once()
    provider.discover_players.assert_awaited_once()
    provider._cometd_stream.start.assert_called_once()

    with patch("music_assistant.models.provider.Provider.unload", new=AsyncMock()) as unload_base:
        await provider.unload(False)
    provider._cometd_stream.stop.assert_awaited_once()
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

    known_gone_player = SimpleNamespace(player_id="gone")
    provider.mass.players.iter_players = MagicMock(return_value=[known_gone_player])

    provider._rpc_request = AsyncMock(
        side_effect=[
            {
                "players_loop": [
                    {"playerid": "new-1", "name": "Kitchen", "model": "Squeeze"},
                    {"playerid": "new-2", "name": "Office", "model": "Squeeze"},
                ]
            },
            {"players_loop": []},
        ]
    )

    await provider.discover_players()

    assert provider.mass.players.register.await_count == 2
    provider._cometd_stream.mark_player_seen.assert_any_call("new-1")
    provider._cometd_stream.mark_player_seen.assert_any_call("new-2")
    provider._cometd_stream.mark_player_removed.assert_called_once_with("gone")
    provider.mass.players.unregister.assert_awaited_once_with("gone")


@pytest.mark.asyncio
async def test_handle_get_stream_url_happy_path_redirects() -> None:
    """Route handler should resolve queue item to fresh stream URL and redirect."""
    provider = _build_provider_stub()

    request = MagicMock()
    request.query = {"player_id": "p1", "queue_id": "p1", "queue_item_id": "qi1"}

    player = MagicMock(spec=LyrionPlayer)
    player.provider = provider
    provider.mass.players.get_player.return_value = player
    queue_item = SimpleNamespace(queue_id="p1", queue_item_id="qi1", uri="x")
    provider.mass.player_queues.get_item.return_value = queue_item
    provider.mass.player_queues.player_media_from_queue_item = AsyncMock(
        return_value=SimpleNamespace()
    )
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

    bad_request = MagicMock()
    bad_request.query = {"player_id": "", "queue_id": "", "queue_item_id": ""}
    with pytest.raises(web.HTTPBadRequest):
        await provider._handle_get_stream_url(bad_request)

    request = MagicMock()
    request.query = {"player_id": "p1", "queue_id": "p1", "queue_item_id": "qi1"}

    provider.mass.players.get_player.return_value = None
    with pytest.raises(web.HTTPNotFound, match="Unknown Lyrion player"):
        await provider._handle_get_stream_url(request)

    request_player = MagicMock(spec=LyrionPlayer)
    request_player.provider = provider
    provider.mass.players.get_player.return_value = request_player
    provider.mass.player_queues.get_item.return_value = None
    with pytest.raises(web.HTTPNotFound, match="Unknown queue item"):
        await provider._handle_get_stream_url(request)

    provider.mass.player_queues.get_item.return_value = SimpleNamespace(
        queue_id="p1", queue_item_id="qi1", uri="x"
    )
    provider.mass.player_queues.player_media_from_queue_item = AsyncMock(
        side_effect=MusicAssistantError("boom")
    )
    with pytest.raises(web.HTTPNotFound, match="Unable to resolve stream URL"):
        await provider._handle_get_stream_url(request)
