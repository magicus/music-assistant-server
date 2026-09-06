"""Minimal fake LMS player used by Lyrion tests."""

from __future__ import annotations

import asyncio
import struct
from collections.abc import Callable
from typing import Any

import aiohttp

from .lms_server_harness import LyrionTestEndpoint


class FakeSlimProtoPlayer:
    """Small fake player that speaks the real SlimProto wire format."""

    _BUTTON_CODES = {
        "play": 131090,
        "pause": 131095,
        "stop": 131082,
        "jump_rew": 131083,
        "jump_fwd": 131086,
    }

    @staticmethod
    def _make_frame(command: bytes, payload: bytes = b"") -> bytes:
        """Build a real SlimProto frame: 2-byte length + 4-byte opcode + payload."""
        return struct.pack("!H", len(payload) + 4) + command + payload

    @staticmethod
    def _make_helo_payload(player_id: str, name: str, model: str) -> bytes:
        """Encode minimal HELO payload with the player identity and capabilities."""
        return (
            b"\x0c\x00\x00\x00\x00\x00\x00\x00"  # deviceid=12, revision=0
            b"\x00\x00\x00\x00\x00\x00"  # 6-byte MAC placeholder
            + b"\x00" * 16
            + struct.pack("!H", 0)
            + struct.pack("!II", 0, 0)
            + b"en"
            + (
                b"PlayerID="
                + player_id.encode()
                + b",Name="
                + name.encode()
                + b",ModelName="
                + model.encode()
                + b",MaxSampleRate=44100"
            )
        )

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
        """Connect to the LMS using the real SlimProto HELO handshake."""
        self._slimproto_writer = await asyncio.open_connection(
            self.endpoint.host,
            self.endpoint.slimproto_port,
        )
        writer = self._slimproto_writer[1]
        helo_payload = self._make_helo_payload(self.player_id, self.name, self.model)
        writer.write(self._make_frame(b"HELO", helo_payload))
        await writer.drain()

        if self.endpoint.fake_server is not None:
            await self._wait_for_server_registration()
            self.mode = self.endpoint.fake_server.players.get(self.player_id, {}).get(
                "mode", self.mode
            )
            return self.endpoint.fake_server._status_for_player(self.player_id)
        return {"playerid": self.player_id, "connected": 1, "power": 1}

    async def disconnect(self) -> dict[str, Any]:
        """Disconnect the player using the SlimProto DSCO frame and close the socket."""
        if self._slimproto_writer is not None:
            writer = self._slimproto_writer[1]
            if not writer.is_closing():
                writer.write(self._make_frame(b"DSCO", b"\x00"))
                await writer.drain()
                writer.close()
                try:
                    await writer.wait_closed()
                except ConnectionResetError:
                    pass
            self._slimproto_writer = None

        if self.endpoint.fake_server is not None:
            await self.endpoint.fake_server.disconnect_player(self.player_id)
            self.mode = "stop"
            return self.endpoint.fake_server._status_for_player(self.player_id)
        return {"playerid": self.player_id, "connected": 0}

    async def play(self) -> dict[str, Any]:
        """Send a play command over SlimProto and reflect the state in LMS."""
        await self._send_command("play")
        return {"playerid": self.player_id, "mode": "play"}

    async def pause(self) -> dict[str, Any]:
        """Send a pause command over SlimProto and reflect the state in LMS."""
        await self._send_command("pause")
        return {"playerid": self.player_id, "mode": "pause"}

    async def next_track(self) -> dict[str, Any]:
        """Send a next-track button command over SlimProto."""
        await self._send_command("jump_fwd")
        return {"playerid": self.player_id}

    async def previous_track(self) -> dict[str, Any]:
        """Send a previous-track button command over SlimProto."""
        await self._send_command("jump_rew")
        return {"playerid": self.player_id}

    def is_playing(self) -> bool:
        """Return whether this fake player currently interprets itself as playing."""
        return self.mode == "play"

    def is_paused(self) -> bool:
        """Return whether this fake player currently interprets itself as paused."""
        return self.mode == "pause"

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

    async def _wait_for_server_registration(self) -> None:
        """Wait until the fake LMS fully registers the connected player."""
        if self.endpoint.fake_server is None:
            return

        deadline = asyncio.get_running_loop().time() + 1.0
        while asyncio.get_running_loop().time() < deadline:
            if self.player_id in self.endpoint.fake_server.players:
                return
            await asyncio.sleep(0.01)

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
        """Send a user-button SlimProto event that matches real LMS behavior."""
        if self._slimproto_writer is None:
            msg = "Fake slimproto player is not connected"
            raise RuntimeError(msg)
        writer = self._slimproto_writer[1]
        button = self._BUTTON_CODES.get(command)
        if button is None:
            msg = f"Unsupported slimproto command: {command}"
            raise ValueError(msg)
        timestamp = 0
        writer.write(self._make_frame(b"butn", struct.pack("!LL", timestamp, button)))
        await writer.drain()
        if command in {"play", "pause", "stop"}:
            await self._wait_for_server_mode(command)
            return

        if self.endpoint.fake_server is not None:
            await self.request_status()

    async def close(self) -> None:
        """Close the HTTP session used by the fake player."""
        if self._slimproto_writer is not None:
            writer = self._slimproto_writer[1]
            if not writer.is_closing():
                writer.close()
                await writer.wait_closed()
            self._slimproto_writer = None
        await self._session.close()
