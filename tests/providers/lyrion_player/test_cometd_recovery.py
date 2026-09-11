"""Endpoint-level CometD recovery tests for the Lyrion player provider."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from music_assistant.providers.lyrion.lyrion_cometd import LyrionCometDEventStream
from tests.providers.lyrion.scriptable_slimproto_player import ScriptableSlimProtoPlayer
from tests.providers.lyrion_player.harness_test_support import (
    EndpointRpcClient,
    ProviderStyleRpcClient,
    wait_for_mode,
)


async def _noop_event_callback(_event: object) -> None:
    """Ignore emitted CometD events."""


async def _resolve_first_track_id(client: EndpointRpcClient) -> str:
    """Return the first available LMS track id from the seeded catalog."""
    titles_result = await client.send(["titles", 0, 1])
    titles_loop = titles_result.get("titles_loop")
    if not isinstance(titles_loop, list) or not titles_loop:
        pytest.fail("Expected at least one playable LMS title in seeded catalog")
    first_title = titles_loop[0]
    if not isinstance(first_title, dict) or "id" not in first_title:
        pytest.fail(f"Unable to resolve playable LMS track id from titles payload: {titles_result}")
    return str(first_title["id"])


class _HttpStatusProvider:
    """Tiny provider stub that reads player status through JSON-RPC."""

    def __init__(self, base_url: str) -> None:
        self.logger = MagicMock()
        self._base_url = base_url
        self._session = aiohttp.ClientSession()
        self.polls = 0
        self.first_poll_seen = asyncio.Event()

    async def close(self) -> None:
        """Close the HTTP session used for status polling."""
        await self._session.close()

    async def get_player_status(self, player_id: str) -> dict[str, Any]:
        """Fetch one player status snapshot through LMS JSON-RPC."""
        self.polls += 1
        payload = {
            "id": 1,
            "method": "slim.request",
            "params": [player_id, ["status", "-", 1]],
        }
        async with self._session.post(f"{self._base_url}/jsonrpc.js", json=payload) as response:
            response.raise_for_status()
            data = await response.json()

        result = data.get("result")
        if not isinstance(result, dict):
            raise RuntimeError(f"Missing status result payload: {data}")

        if self.polls == 1:
            self.first_poll_seen.set()
        return cast("dict[str, Any]", result)


@pytest.mark.asyncio
async def test_verify_player_status_expectation_recovers_after_external_change(
    lyrion_test_endpoint,
) -> None:
    """A stale CometD stream should recover via JSON-RPC polling when another controller changes state."""
    player = ScriptableSlimProtoPlayer(
        endpoint=lyrion_test_endpoint,
        player_id="robustness-player",
        name="Robustness Player",
        model="test",
    )
    provider_client: ProviderStyleRpcClient | None = None
    direct_rpc_client: EndpointRpcClient | None = None
    status_provider: _HttpStatusProvider | None = None

    try:
        await player.connect()
        provider_client = ProviderStyleRpcClient(lyrion_test_endpoint, player.rpc_player_id)
        direct_rpc_client = EndpointRpcClient(lyrion_test_endpoint, player.rpc_player_id)
        first_track_id = await _resolve_first_track_id(direct_rpc_client)

        await provider_client.send_player_command(
            ["playlistcontrol", "cmd:load", f"track_id:{first_track_id}"]
        )
        await provider_client.send_player_command(["stop"])
        await wait_for_mode(direct_rpc_client, "stop")

        status_provider = _HttpStatusProvider(lyrion_test_endpoint.base_url)
        stream = LyrionCometDEventStream(cast("Any", status_provider), _noop_event_callback)
        stream.wait_for_player_status_update = AsyncMock(return_value=False)

        async def _external_state_change() -> None:
            await status_provider.first_poll_seen.wait()
            await provider_client.send_player_command(["play"])

        state_change_task = asyncio.create_task(_external_state_change())
        try:
            assert await stream.verify_player_status_expectation(
                player.rpc_player_id,
                baseline=None,
                expectation=lambda status: status.get("mode") == "play",
                expected_state="play",
            )
        finally:
            state_change_task.cancel()
            with suppress(asyncio.CancelledError):
                await state_change_task

        assert await wait_for_mode(direct_rpc_client, "play")
        assert status_provider.polls >= 2
        snapshot = stream.get_player_status_snapshot(player.rpc_player_id)
        assert snapshot is not None
        assert snapshot["mode"] == "play"
    finally:
        if status_provider is not None:
            await status_provider.close()
        if provider_client is not None:
            await provider_client.close()
        if direct_rpc_client is not None:
            await direct_rpc_client.close()
        await player.close()
