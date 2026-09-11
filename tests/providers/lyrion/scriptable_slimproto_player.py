"""
Scriptable SlimProto-only player used by Lyrion tests.

This helper intentionally emits only SlimProto frames for player behavior.
Do not add JSON-RPC command fallbacks here (including transport/navigation commands),
because these tests exist to validate pure wire-protocol behavior.
"""

from __future__ import annotations

import asyncio
import hashlib
import struct
from collections.abc import Callable
from contextlib import suppress
from typing import Any, ClassVar

from .lms_server_harness import LyrionTestEndpoint


class ScriptableSlimProtoPlayer:
    """Small scriptable player that speaks the real SlimProto wire format."""

    # Use a Squeezebox2-compatible device id so LMS enables IR processing.
    _HELO_DEVICE_ID = 4

    _BUTTON_CODES: ClassVar[dict[str, int]] = {
        "play": 131090,
        "pause": 131095,
        "stop": 131082,
    }

    _IR_BUTTON_CODES: ClassVar[dict[str, int]] = {
        "jump_rew": 0x7689C03F,
        "jump_fwd": 0x7689A05F,
        "repeat": 0x768938C7,
        "shuffle": 0x7689D827,
        "muting": 0x7689C43B,
    }

    @staticmethod
    def _make_frame(command: bytes, payload: bytes = b"") -> bytes:
        """Build a real SlimProto frame: 4-byte opcode + 4-byte payload length + payload."""
        return command + struct.pack("!I", len(payload)) + payload

    @staticmethod
    def _make_helo_payload(player_id: str, mac_address: bytes, name: str, model: str) -> bytes:
        """Encode minimal HELO payload with the player identity and capabilities."""
        return (
            bytes((ScriptableSlimProtoPlayer._HELO_DEVICE_ID, 0))
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
        self.volume_muted = False
        self.elapsed_time = 0.0
        live_mac = self._derive_live_player_id(player_id)
        self.rpc_player_id = player_id if self.endpoint.fake_server is not None else live_mac
        self._mac_address = bytes.fromhex(live_mac.replace(":", ""))
        self._slimproto_writer: tuple[asyncio.StreamReader, asyncio.StreamWriter] | None = None
        self._last_ir_send_time: float = 0.0
        self._ir_button_codes = dict(self._IR_BUTTON_CODES)
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
        muting = state.get("mixer muting")
        if muting is not None:
            self.volume_muted = bool(int(muting))
        elapsed = state.get("time")
        if elapsed is not None:
            self.elapsed_time = float(elapsed)

    async def connect(self) -> dict[str, Any]:
        """Connect to the LMS using the real SlimProto HELO handshake."""
        connect_error: OSError | None = None
        for _attempt in range(10):
            try:
                self._slimproto_writer = await asyncio.open_connection(
                    self.endpoint.host,
                    self.endpoint.slimproto_port,
                )
                break
            except OSError as err:
                connect_error = err
                await asyncio.sleep(0.2)
        else:
            if connect_error is not None:
                raise connect_error
            msg = "SlimProto connection failed without an OSError"
            raise RuntimeError(msg)

        assert self._slimproto_writer is not None
        writer = self._slimproto_writer[1]
        helo_player_id = (
            self.player_id if self.endpoint.fake_server is not None else self.rpc_player_id
        )
        helo_payload = self._make_helo_payload(
            helo_player_id,
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
                with suppress(ConnectionResetError):
                    await writer.wait_closed()
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
        await self.press_ir_button("jump_fwd")
        return {"playerid": self.player_id}

    async def previous_track(self) -> dict[str, Any]:
        """Send a previous-track button command over SlimProto."""
        await self.press_ir_button("jump_rew")
        return {"playerid": self.player_id}

    async def toggle_repeat(self) -> dict[str, Any]:
        """Send a repeat-toggle button command over SlimProto."""
        await self.press_ir_button("repeat")
        return {"playerid": self.player_id}

    async def toggle_shuffle(self) -> dict[str, Any]:
        """Send a shuffle-toggle button command over SlimProto."""
        await self.press_ir_button("shuffle")
        return {"playerid": self.player_id}

    async def toggle_mute(self) -> dict[str, Any]:
        """Send a mute-toggle IR button over SlimProto."""
        await self.press_ir_button("muting")
        return {"playerid": self.player_id}

    async def press_ir_button(self, button: str) -> None:
        """Send one named IR button using the built-in symbolic mapping."""
        ir_code = self._ir_button_codes.get(button)
        if ir_code is None:
            msg = f"Unsupported slimproto IR button: {button}"
            raise ValueError(msg)
        await self.press_ir_code(ir_code)

    async def press_ir_code(self, ir_code: int, *, code_format: int = 0, bits: int = 32) -> None:
        """Send one raw IR code frame for custom button scenarios."""
        await self._send_ir_frame(ir_code=ir_code, code_format=code_format, bits=bits)

    def register_ir_button(self, button: str, ir_code: int) -> None:
        """Register or override one named IR button mapping for a test scenario."""
        self._ir_button_codes[button] = ir_code

    def is_playing(self) -> bool:
        """Return whether this fake player currently interprets itself as playing."""
        return self.mode == "play"

    def is_paused(self) -> bool:
        """Return whether this fake player currently interprets itself as paused."""
        return self.mode == "pause"

    def is_muted(self) -> bool:
        """Return whether this fake player currently interprets itself as muted."""
        return self.volume_muted

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

        current_mode = self.endpoint.fake_server.players.get(self.player_id, {}).get("mode")
        if isinstance(current_mode, str):
            self.mode = current_mode

    async def _send_command(self, command: str) -> None:
        """Send a user-button SlimProto event that matches real LMS behavior."""
        if self._slimproto_writer is None:
            msg = "Scriptable SlimProto player is not connected"
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

    async def _send_ir_frame(self, *, ir_code: int, code_format: int = 0, bits: int = 32) -> None:
        """Send one SlimProto IR frame with caller-provided code payload."""
        if self._slimproto_writer is None:
            msg = "Scriptable SlimProto player is not connected"
            raise RuntimeError(msg)
        writer = self._slimproto_writer[1]

        # LMS treats same-button presses within IRMINTIME as hold/repeat; space frames as singles.
        now = asyncio.get_running_loop().time()
        min_interval = 0.16
        elapsed = now - self._last_ir_send_time
        if elapsed < min_interval:
            await asyncio.sleep(min_interval - elapsed)

        timestamp = int(asyncio.get_running_loop().time() * 1000) & 0xFFFFFFFF
        payload = struct.pack("!LBBL", timestamp, code_format, bits, ir_code)
        writer.write(self._make_frame(b"IR  ", payload))
        await writer.drain()
        self._last_ir_send_time = asyncio.get_running_loop().time()

    async def close(self) -> None:
        """Close open SlimProto resources held by the test player."""
        if self._slimproto_writer is not None:
            writer = self._slimproto_writer[1]
            if not writer.is_closing():
                writer.close()
                await writer.wait_closed()
            self._slimproto_writer = None

    @staticmethod
    def _derive_live_player_id(seed: str) -> str:
        """Create a deterministic locally administered MAC string from an arbitrary seed."""
        digest = hashlib.sha1(seed.encode()).digest()
        first_octet = (digest[0] | 0x02) & 0xFE
        mac = bytes([first_octet, digest[1], digest[2], digest[3], digest[4], digest[5]])
        return ":".join(f"{octet:02x}" for octet in mac)
