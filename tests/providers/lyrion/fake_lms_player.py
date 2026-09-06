"""Minimal fake LMS player used by Lyrion tests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
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
        self._server_state_listener: Callable[[str, dict[str, Any]], None] | None = None
        if self.endpoint.fake_server is not None:
            self._server_state_listener = self._on_server_state_changed
            self.endpoint.fake_server.add_player_state_listener(self._server_state_listener)

    def _on_server_state_changed(self, player_id: str, state: dict[str, Any]) -> None:
        """Keep the fake player state synchronized with updates from the LMS server."""
        if player_id != self.player_id:
            return
        mode = state.get("mode")
        if isinstance(mode, str):
            self.mode = mode

    async def connect(self) -> dict[str, Any]:
        """Register the fake player with the LMS and open a slimproto socket."""
        data: dict[str, Any] = {}
        if self.endpoint.fake_server is not None:
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

        if self.endpoint.fake_server is None:
            self.mode = "stop"
            return {"playerid": self.player_id, "connected": 0}

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
        """Return runtime status for the player using the active backend's real contract."""
        if self.endpoint.fake_server is None:
            connected = 1 if self._slimproto_writer is not None else 0
            return {
                "playerid": self.player_id,
                "name": self.name,
                "model": self.model,
                "connected": connected,
                "power": 1 if connected else 0,
                "mode": self.mode,
                "playlist index": 0,
                "playlist tracks": 0,
                "volume": 50,
                "player_name": self.name,
                "isplaying": 1 if self.mode == "play" else 0,
            }

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

    async def _wait_for_server_mode(self, expected_mode: str) -> None:
        """Wait briefly for the LMS fake server to apply a state change before returning."""
        if self.endpoint.fake_server is None:
            self.mode = expected_mode
            return

        deadline = asyncio.get_running_loop().time() + 1.0
        while asyncio.get_running_loop().time() < deadline:
            current_mode = self.endpoint.fake_server.players.get(self.player_id, {}).get("mode")
            if current_mode == expected_mode:
                self.mode = expected_mode
                return
            await asyncio.sleep(0.01)

        self.mode = expected_mode

    async def _send_command(self, command: str) -> None:
        """Send a state-changing slimproto command to the fake LMS."""
        if self._slimproto_writer is None:
            msg = "Fake slimproto player is not connected"
            raise RuntimeError(msg)
        writer = self._slimproto_writer[1]
        writer.write(f"{command}\n".encode())
        await writer.drain()
        await self._wait_for_server_mode(command)

    async def close(self) -> None:
        """Close the HTTP session used by the fake player."""
        if self._slimproto_writer is not None:
            writer = self._slimproto_writer[1]
            if not writer.is_closing():
                writer.close()
                await writer.wait_closed()
            self._slimproto_writer = None
        await self._session.close()
