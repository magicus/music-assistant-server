"""Focused tests for helper branches in the pylyrion CometD stream helper."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Self, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import ClientError

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
from pylyrion.errors import LyrionRequestError
from tests.pylyrion.cometd_test_helpers import LyrionCometDTestStream


async def _noop_event_callback(_event: object) -> None:
    """Ignore emitted CometD events."""


class _FakeResponse:
    """Tiny aiohttp response stand-in with async context-manager support."""

    def __init__(
        self,
        payload: object,
        raise_error: Exception | None = None,
    ) -> None:
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


def _build_runtime_context(
    host: str | None = "127.0.0.1",
    port: int | None = 9000,
) -> Any:
    """Create runtime context stub with configurable endpoint and transport."""
    runtime_context = SimpleNamespace()
    runtime_context.instance_id = "lyrion_player.test"
    runtime_context.unloading = False
    runtime_context.logger = MagicMock()
    runtime_context.players = []
    runtime_context.http_session = SimpleNamespace(post=MagicMock())
    runtime_context.get_configured_host = MagicMock(return_value=host)
    runtime_context.get_configured_port = MagicMock(return_value=port)
    runtime_context.get_player_status = AsyncMock(return_value={})
    runtime_context.schedule_players_discovery = MagicMock()
    return runtime_context


def test_status_helpers_parse_payloads_safely() -> None:
    """Low-level status helpers should normalize mixed payload types."""
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
    """Track extraction and active-track equality should handle cases."""
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
    """Serverstatus extraction and invalid-player detection should work."""
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
    context_no_host = _build_runtime_context(host=None, port=9000)
    stream = LyrionCometDTestStream(
        cast("Any", context_no_host),
        _noop_event_callback,
    )
    with pytest.raises(LyrionRequestError, match="host"):
        await stream._post([], timeout=1)

    context_no_port = _build_runtime_context(host="127.0.0.1", port=None)
    stream = LyrionCometDTestStream(
        cast("Any", context_no_port),
        _noop_event_callback,
    )
    with pytest.raises(LyrionRequestError, match="port"):
        await stream._post([], timeout=1)


@pytest.mark.asyncio
async def test_post_normalizes_dict_and_list_payloads() -> None:
    """CometD POST should normalize payloads and filter dict entries."""
    runtime_context = _build_runtime_context()
    runtime_context.http_session.post.return_value = _FakeResponse({"channel": "/ok"})
    stream = LyrionCometDTestStream(
        cast("Any", runtime_context),
        _noop_event_callback,
    )

    result = await stream._post([{"id": "1"}], timeout=1)
    assert result == [{"channel": "/ok"}]

    runtime_context.http_session.post.return_value = _FakeResponse([{"a": 1}, "skip", {"b": 2}])
    result = await stream._post([{"id": "2"}], timeout=1)
    assert result == [{"a": 1}, {"b": 2}]


@pytest.mark.asyncio
async def test_post_wraps_transport_and_payload_errors() -> None:
    """CometD POST should wrap transport and payload failures."""
    runtime_context = _build_runtime_context()
    runtime_context.http_session.post.return_value = _FakeResponse(
        {}, raise_error=ClientError("http-fail")
    )
    stream = LyrionCometDTestStream(
        cast("Any", runtime_context),
        _noop_event_callback,
    )
    with pytest.raises(LyrionRequestError):
        await stream._post([{"id": "1"}], timeout=1)

    runtime_context.http_session.post.return_value = _FakeResponse("invalid")
    with pytest.raises(LyrionRequestError, match="JSON object or list"):
        await stream._post([{"id": "2"}], timeout=1)
