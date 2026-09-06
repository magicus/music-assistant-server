"""Minimal fake LMS player used by Lyrion tests."""

from __future__ import annotations

import asyncio
import hashlib
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
    }

    _IR_CODES = {
        "jump_rew": 0x7689C03F,
        "jump_fwd": 0x7689A05F,
    }

    @staticmethod
    def _make_frame(command: bytes, payload: bytes = b"") -> bytes:
        """Build a real SlimProto frame: 4-byte opcode + 4-byte payload length + payload."""
        return command + struct.pack("!I", len(payload)) + payload

    @staticmethod
    def _make_helo_payload(player_id: str, mac_address: bytes, name: str, model: str) -> bytes:
        """Encode minimal HELO payload with the player identity and capabilities."""
        return (
            b"\x0c\x00"  # deviceid=12 (squeezeplay), revision=0
            + mac_address
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
        live_mac = self._derive_live_player_id(player_id)
        self.rpc_player_id = player_id if self.endpoint.fake_server is not None else live_mac
        self._mac_address = bytes.fromhex(live_mac.replace(":", ""))
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
        helo_payload = self._make_helo_payload(
            self.player_id,
            self._mac_address,
            self.name,
            self.model,
        )
        writer.write(self._make_frame(b"HELO", helo_payload))
        await writer.drain()

        if self.endpoint.fake_server is not None:
            await self._wait_for_server_registration()
            self.mode = self.endpoint.fake_server.players.get(self.player_id, {}).get(
                "mode", self.mode
            )
            return self.endpoint.fake_server._status_for_player(self.player_id)

        return {"playerid": self.rpc_player_id, "connected": 1, "power": 1}

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
        await self._send_ir_command("jump_fwd")
        return {"playerid": self.player_id}

    async def previous_track(self) -> dict[str, Any]:
        """Send a previous-track button command over SlimProto."""
        await self._send_ir_command("jump_rew")
        return {"playerid": self.player_id}

    def is_playing(self) -> bool:
        """Return whether this fake player currently interprets itself as playing."""
        return self.mode == "play"

    def is_paused(self) -> bool:
        """Return whether this fake player currently interprets itself as paused."""
        return self.mode == "pause"

    async def request_status(self) -> dict[str, Any]:
        """Return runtime status for the player using the active backend's real contract."""
        payload = {
            "id": 1,
            "method": "slim.request",
            "params": [self.rpc_player_id, ["status", "-", 1]],
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
        timestamp = int(asyncio.get_running_loop().time() * 1000) & 0xFFFFFFFF

        writer.write(self._make_frame(b"BUTN", struct.pack("!LL", timestamp, button)))
        await writer.drain()
        if command in {"play", "pause", "stop"}:
            await self._wait_for_server_mode(command)
            return

        if self.endpoint.fake_server is not None:
            await self.request_status()

    async def _send_ir_command(self, command: str) -> None:
        """Send a SlimProto IR frame for transport navigation commands."""
        if self._slimproto_writer is None:
            msg = "Fake slimproto player is not connected"
            raise RuntimeError(msg)
        writer = self._slimproto_writer[1]
        ir_code = self._IR_CODES.get(command)
        if ir_code is None:
            msg = f"Unsupported slimproto IR command: {command}"
            raise ValueError(msg)

        timestamp = int(asyncio.get_running_loop().time() * 1000) & 0xFFFFFFFF
        payload = struct.pack("!LBBL", timestamp, 0, 32, ir_code)
        writer.write(self._make_frame(b"IR  ", payload))
        await writer.drain()

    async def close(self) -> None:
        """Close the HTTP session used by the fake player."""
        if self._slimproto_writer is not None:
            writer = self._slimproto_writer[1]
            if not writer.is_closing():
                writer.close()
                await writer.wait_closed()
            self._slimproto_writer = None
        await self._session.close()

    @staticmethod
    def _derive_live_player_id(seed: str) -> str:
        """Create a deterministic locally administered MAC string from an arbitrary seed."""
        digest = hashlib.sha1(seed.encode()).digest()
        first_octet = (digest[0] | 0x02) & 0xFE
        mac = bytes([first_octet, digest[1], digest[2], digest[3], digest[4], digest[5]])
        return ":".join(f"{octet:02x}" for octet in mac)
