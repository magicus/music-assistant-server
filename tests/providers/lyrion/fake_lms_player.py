"""Minimal fake LMS player used by Lyrion tests."""

from __future__ import annotations

import asyncio
from typing import Any

import aiohttp

from .lms_server_harness import LyrionTestEndpoint


class FakeSlimProtoPlayer:
    """Small fake player that talks to LMS over both JSON-RPC and slimproto."""

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
        self.mode = "stop"
        self._session = aiohttp.ClientSession()
        self._slimproto_writer: asyncio.StreamWriter | None = None

    async def connect(self) -> dict[str, Any]:
        """Register the fake player with the LMS and open a slimproto socket."""
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

        self._slimproto_writer = await asyncio.open_connection(
            self.endpoint.host,
            self.endpoint.slimproto_port,
        )
        writer = self._slimproto_writer[1]
        writer.write(f"HELLO {self.player_id}\n".encode())
        await writer.drain()

        result = data.get("result")
        if isinstance(result, dict):
            self.mode = str(result.get("mode", self.mode))
            return result
        return {"playerid": self.player_id, "connected": 1, "power": 1}

    async def disconnect(self) -> dict[str, Any]:
        """Unregister the fake player from the LMS and close the slimproto connection."""
        if self._slimproto_writer is not None:
            writer = self._slimproto_writer[1]
            if not writer.is_closing():
                writer.write(b"DISCONNECT\n")
                await writer.drain()
                writer.close()
                await writer.wait_closed()
            self._slimproto_writer = None

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
            self.mode = "stop"
            return result
        return {"playerid": self.player_id, "connected": 0}

    async def play(self) -> dict[str, Any]:
        """Send a play command over slimproto and reflect the state in LMS."""
        await self._send_command("play")
        return {"playerid": self.player_id, "mode": "play"}

    async def pause(self) -> dict[str, Any]:
        """Send a pause command over slimproto and reflect the state in LMS."""
        await self._send_command("pause")
        return {"playerid": self.player_id, "mode": "pause"}

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
            self.mode = str(result.get("mode", self.mode))
            return result
        raise RuntimeError("Fake player status response was missing a JSON-RPC result")

    async def _send_command(self, command: str) -> None:
        """Send a state-changing slimproto command to the fake LMS."""
        if self._slimproto_writer is None:
            msg = "Fake slimproto player is not connected"
            raise RuntimeError(msg)
        writer = self._slimproto_writer[1]
        writer.write(f"{command}\n".encode())
        await writer.drain()
        self.mode = command

    async def close(self) -> None:
        """Close the HTTP session used by the fake player."""
        if self._slimproto_writer is not None:
            writer = self._slimproto_writer[1]
            if not writer.is_closing():
                writer.close()
                await writer.wait_closed()
            self._slimproto_writer = None
        await self._session.close()
