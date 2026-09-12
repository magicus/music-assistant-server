"""Whitebox-only player harness tests that require fake LMS internals."""

from __future__ import annotations

import pytest

from tests.providers.lyrion.fake_lms_server import FakeLmsServer

pytestmark = [
    pytest.mark.asyncio,
]


async def test_fake_lms_server_builds_player_status_payload() -> None:
    """Fake LMS internals should return coherent status payloads."""
    server = FakeLmsServer()
    await server.connect_player("test-1", "Kitchen", "test")

    result = server._build_serverstatus_result()
    assert result["player count"] == 1
    assert result["players_loop"][0]["playerid"] == "test-1"
    assert result["players_loop"][0]["connected"] == 1

    status = server._status_for_player("test-1")
    assert status["playerid"] == "test-1"
    assert status["mode"] == "stop"
    assert status["power"] == 1
