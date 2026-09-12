"""Focused tests for helper branches in the Lyrion CometD stream."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Self, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import ClientError
from music_assistant_models.errors import ProviderUnavailableError

from pylyrion.cometd.helpers import (
    _extract_current_track_id,
    _extract_server_player_ids,
    _get_float,
    _get_int,
    _get_mode,
    _get_power,
    _is_invalid_player_payload,
    _same_active_track,
)
from tests.pylyrion.cometd_test_helpers import LyrionCometDEventStream


async def _noop_event_callback(_event: object) -> None:
    """Ignore emitted CometD events."""


class _FakeResponse:
    """Tiny aiohttp response stand-in with async context-manager support."""

    def __init__(self, payload: object, raise_error: Exception | None = None) -> None:
        self._payload = payload
        self._raise_error = raise_error

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> None:
        del exc_type, exc, tb

    def raise_for_status(self) -> None:
        if self._raise_error is not None:
            raise self._raise_error

    async def json(self) -> object:
        return self._payload


def _build_provider(host: str | None = "127.0.0.1", port: int | None = 9000) -> Any:
    """Create provider stub with configurable endpoint and HTTP transport."""
    provider = SimpleNamespace()
    provider.instance_id = "lyrion_player.test"
    provider.unloading = False
    provider.logger = MagicMock()
    provider.players = []
    provider.mass = SimpleNamespace(
        create_task=AsyncMock(),
        http_session=SimpleNamespace(post=MagicMock()),
        players=SimpleNamespace(get_player=lambda _player_id: None),
    )
    provider.get_configured_host = MagicMock(return_value=host)
    provider.get_configured_port = MagicMock(return_value=port)
    provider.get_player_status = AsyncMock(return_value={})
    return provider


def test_status_helpers_parse_payloads_safely() -> None:
    """Low-level status helper functions should normalize mixed payload types."""
    assert _get_mode({"mode": "play"}) == "play"
    assert _get_mode({"mode": 1}) == "stop"

    assert _get_power({"power": "1"}) is True
    assert _get_power({"power": "0"}) is False
    assert _get_power({}) is None

    assert _get_int({"k": "12"}, "k") == 12
    assert _get_int({"k": "x"}, "k") is None
    assert _get_float({"k": "12.5"}, "k") == 12.5
    assert _get_float({"k": "x"}, "k") is None


def test_extract_current_track_id_and_track_comparison() -> None:
    """Track extraction and active-track equality should handle valid and invalid cases."""
    status = {
        "playlist_cur_index": 1,
        "playlist_loop": [{"id": "a"}, {"track_id": "b"}],
    }
    assert _extract_current_track_id(status) == "b"
    assert _extract_current_track_id({"playlist_cur_index": 10, "playlist_loop": []}) is None

    old_status = {
        "playlist_cur_index": 0,
        "playlist_loop": [{"track_id": "same"}],
    }
    new_status = {
        "playlist_cur_index": 0,
        "playlist_loop": [{"id": "same"}],
    }
    assert _same_active_track(old_status, new_status)


def test_extract_server_player_ids_and_invalid_payload_detection() -> None:
    """Serverstatus extraction and invalid-player detection should be robust."""
    payload = {
        "players_loop": [
            {"playerid": "one"},
            {"playerid": "two"},
            {"not": "a-player"},
            "junk",
        ]
    }
    assert _extract_server_player_ids(payload) == {"one", "two"}
    assert _extract_server_player_ids({"players_loop": "bad"}) == set()

    assert _is_invalid_player_payload({"error": "invalid player"})
    assert not _is_invalid_player_payload({"error": "other"})


@pytest.mark.asyncio
async def test_post_rejects_missing_endpoint_configuration() -> None:
    """CometD POST should fail fast when host/port is not configured."""
    provider_no_host = _build_provider(host=None, port=9000)
    stream = LyrionCometDEventStream(cast("Any", provider_no_host), _noop_event_callback)
    with pytest.raises(ProviderUnavailableError, match="host"):
        await stream._post([], timeout=1)

    provider_no_port = _build_provider(host="127.0.0.1", port=None)
    stream = LyrionCometDEventStream(cast("Any", provider_no_port), _noop_event_callback)
    with pytest.raises(ProviderUnavailableError, match="port"):
        await stream._post([], timeout=1)


@pytest.mark.asyncio
async def test_post_normalizes_dict_and_list_payloads() -> None:
    """CometD POST should normalize dict payloads and filter list entries to dicts."""
    provider = _build_provider()
    provider.mass.http_session.post.return_value = _FakeResponse({"channel": "/ok"})
    stream = LyrionCometDEventStream(cast("Any", provider), _noop_event_callback)

    result = await stream._post([{"id": "1"}], timeout=1)
    assert result == [{"channel": "/ok"}]

    provider.mass.http_session.post.return_value = _FakeResponse([{"a": 1}, "skip", {"b": 2}])
    result = await stream._post([{"id": "2"}], timeout=1)
    assert result == [{"a": 1}, {"b": 2}]


@pytest.mark.asyncio
async def test_post_wraps_transport_and_payload_errors() -> None:
    """CometD POST should wrap HTTP and invalid payload failures as provider-unavailable."""
    provider = _build_provider()
    provider.mass.http_session.post.return_value = _FakeResponse(
        {}, raise_error=ClientError("http-fail")
    )
    stream = LyrionCometDEventStream(cast("Any", provider), _noop_event_callback)
    with pytest.raises(ProviderUnavailableError):
        await stream._post([{"id": "1"}], timeout=1)

    provider.mass.http_session.post.return_value = _FakeResponse("invalid")
    with pytest.raises(ProviderUnavailableError, match="JSON object or list"):
        await stream._post([{"id": "2"}], timeout=1)
