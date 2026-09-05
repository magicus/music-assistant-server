"""Minimal fake LMS player used by Lyrion tests."""

from __future__ import annotations

from typing import Any

import aiohttp

from .lms_server_harness import LyrionTestEndpoint


class FakeSlimProtoPlayer:
    """Small fake player that talks to the same LMS JSON-RPC contract as a real LMS client."""

    def __init__(
        self,
        *,
        endpoint: LyrionTestEndpoint,
        player_id: str,
        name: str,
        model: str,
    ) -> None:
        """Initialize the fake player state."""
        self.endpoint = endpoint
        self.player_id = player_id
        self.name = name
        self.model = model
        self._session = aiohttp.ClientSession()

    async def connect(self) -> dict[str, Any]:
        """Register the fake player with the LMS and return its generated status payload."""
        payload = {
            "id": 1,
            "method": "slim.request",
            "params": [self.player_id, ["player", "register", self.name, self.model]],
        }
        async with self._session.post(
            f"{self.endpoint.base_url}/jsonrpc.js",
            json=payload,
        ) as response:
            response.raise_for_status()
            data = await response.json()

        result = data.get("result")
        if isinstance(result, dict):
            return result
        return {"playerid": self.player_id, "connected": 1, "power": 1}

    async def disconnect(self) -> dict[str, Any]:
        """Unregister the fake player from the LMS."""
        payload = {
            "id": 1,
            "method": "slim.request",
            "params": [self.player_id, ["player", "disconnect"]],
        }
        async with self._session.post(
            f"{self.endpoint.base_url}/jsonrpc.js",
            json=payload,
        ) as response:
            response.raise_for_status()
            data = await response.json()

        result = data.get("result")
        if isinstance(result, dict):
            return result
        return {"playerid": self.player_id, "connected": 0}

    async def request_status(self) -> dict[str, Any]:
        """Request runtime status for the player through the standard LMS JSON-RPC API."""
        payload = {
            "id": 1,
            "method": "slim.request",
            "params": [self.player_id, ["status", "-", 1]],
        }
        async with self._session.post(
            f"{self.endpoint.base_url}/jsonrpc.js",
            json=payload,
        ) as response:
            response.raise_for_status()
            data = await response.json()

        result = data.get("result")
        if isinstance(result, dict):
            return result
        raise RuntimeError("Fake player status response was missing a JSON-RPC result")

    async def close(self) -> None:
        """Close the HTTP session used by the fake player."""
        await self._session.close()
