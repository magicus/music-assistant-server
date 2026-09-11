"""Unit tests for pylyrion player helpers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from pylyrion.client import LyrionClient
from pylyrion.models import LyrionEndpoint
from pylyrion.player import LyrionPlayerClient
from pylyrion.session import LyrionSession
from tests.providers.lyrion.rpc_test_doubles import FakeResponse


def _build_player_client() -> LyrionPlayerClient:
    """Create a pylyrion player client backed by a fake HTTP transport."""
    http_session = SimpleNamespace(post=MagicMock(return_value=FakeResponse({"result": {}})))
    session = LyrionSession(
        http_session=http_session,
        endpoint=LyrionEndpoint(host="127.0.0.1", port=9000),
    )
    return LyrionPlayerClient(session)


@pytest.mark.asyncio
async def test_player_client_get_status() -> None:
    """Player status should be requested through the raw pylyrion client."""
    transport = SimpleNamespace(
        post=MagicMock(return_value=FakeResponse({"result": {"mode": "play"}}))
    )
    session = LyrionSession(
        http_session=transport,
        endpoint=LyrionEndpoint(host="127.0.0.1", port=9000),
    )
    client = LyrionPlayerClient(session)

    assert await client.get_status("player1") == {"mode": "play"}


@pytest.mark.asyncio
async def test_high_level_client_exposes_player_control() -> None:
    """
    The high-level client should expose player helpers through one facade.

    """
    transport = SimpleNamespace(
        post=MagicMock(return_value=FakeResponse({"result": {"mode": "stop"}}))
    )
    session = LyrionSession(
        http_session=transport,
        endpoint=LyrionEndpoint(host="127.0.0.1", port=9000),
    )
    client = LyrionClient(session)

    assert await client.players.get_status("player1") == {"mode": "stop"}
