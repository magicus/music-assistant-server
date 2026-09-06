"""In-memory fake LMS server for provider contract tests."""

from __future__ import annotations

import asyncio
import struct
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

from aiohttp import web

from .catalog_seed import (
    fake_albums,
    fake_artists,
    fake_genres,
    fake_playlist_tracks,
    fake_playlists,
    fake_tracks,
)

PNG_1X1_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
    b"\x00\x00\x00\x0cIDAT\x08\x1dc``\x00\x00\x00\x02"
    b"\x00\x01\xe2!\xbc3\x00\x00\x00\x00IEND\xaeB`\x82"
)

SLIMPROTO_BUTTON_PLAY = 131090
SLIMPROTO_BUTTON_PAUSE = 131095
SLIMPROTO_BUTTON_STOP = 131082
SLIMPROTO_BUTTON_JUMP_REW = 131088
SLIMPROTO_BUTTON_JUMP_FWD = 131089
SLIMPROTO_IR_JUMP_REW = 0x7689C03F
SLIMPROTO_IR_JUMP_FWD = 0x7689A05F


@dataclass(slots=True, frozen=True)
class FakeRpcCall:
    """One captured JSON-RPC call sent by the provider."""

    command: str
    player_id: str
    args: tuple[Any, ...]


class FakeLmsServer:
    """Minimal LMS JSON-RPC surface with deterministic fake library data."""

    def __init__(self) -> None:
        """Initialize fake data and captured request state."""
        self.rpc_calls: list[FakeRpcCall] = []
        self.stream_requests: list[str] = []
        self.image_requests: list[str] = []
        self.command_errors: dict[str, tuple[int, str]] = {}
        self.incomplete_batch_for_entities: set[str] = set()
        self.missing_large_artist_images: set[str] = {"a3"}
        self.missing_large_album_images: set[str] = {"alb6"}
        self.players: dict[str, dict[str, Any]] = {}
        self.slimproto_players: dict[str, dict[str, Any]] = {}
        self.slimproto_events: list[tuple[bytes, bytes]] = []
        self._player_state_listeners: list[Callable[[str, dict[str, Any]], None]] = []
        self._slimproto_server: asyncio.Server | None = None
        self._slimproto_connections: dict[str, asyncio.StreamWriter] = {}

        self.artists: list[dict[str, Any]] = fake_artists()
        self.albums: list[dict[str, Any]] = fake_albums()
        self.tracks: list[dict[str, Any]] = fake_tracks()
        self.playlists: list[dict[str, Any]] = fake_playlists()
        self.genres: list[dict[str, Any]] = fake_genres()
        self.playlist_tracks: dict[str, list[str]] = fake_playlist_tracks()

    def add_player_state_listener(
        self,
        callback: Callable[[str, dict[str, Any]], None],
    ) -> None:
        """Register a callback for player connect/disconnect/state changes."""
        self._player_state_listeners.append(callback)

    def _notify_player_state(self, player_id: str) -> None:
        """Emit the latest state update to registered listeners."""
        if player_id not in self.players:
            player = self.slimproto_players.get(player_id)
            if player is None:
                return
            state = dict(player)
        else:
            state = dict(self.players[player_id])
        for listener in self._player_state_listeners:
            listener(player_id, state)

    async def connect_player(self, player_id: str, name: str, model: str) -> dict[str, Any]:
        """Register a connected fake player and return its player metadata."""
        player = self._ensure_player(player_id)
        player.update(
            {
                "name": name,
                "model": model,
                "connected": 1,
                "power": 1,
                "player_name": name,
            }
        )
        self._normalize_playlist_fields(player)
        self.slimproto_players[player_id] = {
            "playerid": player_id,
            "name": name,
            "model": model,
            "connected": True,
            "power": True,
            "mode": "stop",
            "volume": 50,
        }
        self._notify_player_state(player_id)
        return player

    async def disconnect_player(self, player_id: str) -> dict[str, Any]:
        """Disconnect a registered fake player."""
        player = self.players.pop(player_id, {})
        if player:
            player["connected"] = 0
            player["power"] = 0
            player["mode"] = "stop"
        slimproto = self.slimproto_players.get(player_id)
        if slimproto is not None:
            slimproto["connected"] = False
            slimproto["power"] = False
            slimproto["mode"] = "stop"
        self._notify_player_state(player_id)
        if not player:
            return {"playerid": player_id, "connected": 0, "power": 0}
        return player

    def set_player_mode(self, player_id: str, mode: str) -> dict[str, Any]:
        """Update the fake player's runtime play state and notify listeners."""
        player = self._ensure_player(player_id)
        player["mode"] = mode
        player["isplaying"] = 1 if mode == "play" else 0
        self._normalize_playlist_fields(player)
        slimproto = self.slimproto_players.setdefault(
            player_id,
            {
                "playerid": player_id,
                "name": player_id,
                "model": "test",
                "connected": True,
                "power": True,
                "mode": "stop",
                "volume": 50,
            },
        )
        slimproto["mode"] = mode
        slimproto["connected"] = True
        slimproto["power"] = True
        self._notify_player_state(player_id)
        return self._status_for_player(player_id)

    async def handle_jsonrpc_command(self, player_id: str, command: list[Any]) -> dict[str, Any]:
        """Apply a command list to a fake player, matching LMS JSON-RPC behavior."""
        if not command:
            raise ValueError("command cannot be empty")

        action = str(command[0])
        if action == "status":
            return self._status_for_player(player_id)
        if action == "play":
            return self.set_player_mode(player_id, "play")
        if action == "pause":
            return self.set_player_mode(player_id, "pause")
        if action == "stop":
            return self.set_player_mode(player_id, "stop")
        if action == "playlist":
            return self._handle_playlist_command(player_id, command)
        if action == "playlistcontrol":
            return self._handle_playlistcontrol_command(player_id, command)
        if action == "button":
            return self._handle_button_command(player_id, command)
        if action == "player":
            sub_action = command[1] if len(command) > 1 else ""
            if sub_action == "register":
                name = str(command[2]) if len(command) > 2 else player_id
                model = str(command[3]) if len(command) > 3 else "test"
                await self.connect_player(player_id, name, model)
                return self._status_for_player(player_id)
            if sub_action == "disconnect":
                await self.disconnect_player(player_id)
                return {"playerid": player_id, "connected": 0}
        if action == "players":
            return self._build_serverstatus_result()
        if action == "serverstatus":
            return self._build_serverstatus_result()
        return self._status_for_player(player_id)

    async def start_slimproto_server(self, host: str, port: int) -> None:
        """Start a tiny slimproto TCP server that tracks connect/disconnect and play/pause state."""
        self._slimproto_server = await asyncio.start_server(
            self._handle_slimproto_client,
            host,
            port,
        )

    async def stop_slimproto_server(self) -> None:
        """Stop the fake slimproto TCP server."""
        if self._slimproto_server is not None:
            self._slimproto_server.close()
            await self._slimproto_server.wait_closed()
            self._slimproto_server = None
        for writer in self._slimproto_connections.values():
            if not writer.is_closing():
                writer.close()
        self._slimproto_connections.clear()

    async def _handle_slimproto_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle a SlimProto connection using the real binary HELO/strm flow."""
        player_id: str | None = None
        name = "unknown"
        model = "test"

        async def _read_frame() -> tuple[bytes, bytes] | None:
            header = await reader.readexactly(8)
            if len(header) < 8:
                return None
            opcode = header[:4]
            length = struct.unpack("!I", header[4:])[0]
            payload = await reader.readexactly(length)
            return opcode, payload

        try:
            while True:
                frame = await _read_frame()
                if frame is None:
                    break
                opcode, payload = frame
                self.slimproto_events.append((opcode, payload))
                if opcode == b"HELO":
                    if len(payload) >= 8:
                        mac = ":".join(f"{byte:02x}" for byte in payload[2:8])
                        player_id = mac
                    text = payload.decode("utf-8", errors="ignore")
                    for marker in ("PlayerID=", "Name=", "ModelName="):
                        idx = text.find(marker)
                        if idx == -1:
                            continue
                        if marker == "PlayerID=":
                            player_id = text[idx + len(marker) :].split(",", 1)[0]
                        elif marker == "Name=":
                            name = text[idx + len(marker) :].split(",", 1)[0]
                        elif marker == "ModelName=":
                            model = text[idx + len(marker) :].split(",", 1)[0]
                    if player_id is None:
                        player_id = "00:00:00:00:00:00"
                    break
                if opcode == b"DSCO":
                    return
        except asyncio.IncompleteReadError:
            writer.close()
            return

        if player_id is None:
            writer.close()
            return

        self.players.setdefault(
            player_id,
            self._default_player_state(player_id),
        )
        self.players[player_id]["name"] = name
        self.players[player_id]["model"] = model
        self.players[player_id]["connected"] = 1
        self.players[player_id]["power"] = 1
        self.players[player_id]["player_name"] = name

        self.slimproto_players[player_id] = {
            "playerid": player_id,
            "name": name,
            "model": model,
            "connected": True,
            "power": True,
            "mode": self.players.get(player_id, {}).get("mode", "stop"),
            "volume": self.players.get(player_id, {}).get("volume", 50),
        }
        self._slimproto_connections[player_id] = writer
        self._notify_player_state(player_id)
        try:
            while True:
                frame = await _read_frame()
                if frame is None:
                    break
                opcode, payload = frame
                self.slimproto_events.append((opcode, payload))
                if opcode == b"strm":
                    action = payload[:1]
                    if action == b"p":
                        self.set_player_mode(player_id, "pause")
                    elif action == b"u":
                        self.set_player_mode(player_id, "play")
                    elif action == b"q":
                        self.set_player_mode(player_id, "stop")
                elif opcode in {b"BUTN", b"butn"}:
                    if len(payload) >= 8:
                        _, button = struct.unpack("!LL", payload[:8])
                        if button == SLIMPROTO_BUTTON_PLAY:
                            self.set_player_mode(player_id, "play")
                        elif button == SLIMPROTO_BUTTON_PAUSE:
                            self.set_player_mode(player_id, "pause")
                        elif button == SLIMPROTO_BUTTON_STOP:
                            self.set_player_mode(player_id, "stop")
                        elif button == SLIMPROTO_BUTTON_JUMP_FWD:
                            self._advance_playlist_index(player_id, +1)
                        elif button == SLIMPROTO_BUTTON_JUMP_REW:
                            self._advance_playlist_index(player_id, -1)
                elif opcode == b"IR  ":
                    if len(payload) >= 10:
                        _, _, _, ir_code = struct.unpack("!LBBL", payload[:10])
                        if ir_code == SLIMPROTO_IR_JUMP_FWD:
                            self._advance_playlist_index(player_id, +1)
                        elif ir_code == SLIMPROTO_IR_JUMP_REW:
                            self._advance_playlist_index(player_id, -1)
                elif opcode == b"DSCO":
                    await self.disconnect_player(player_id)
                    break
        except asyncio.IncompleteReadError, ConnectionResetError:
            pass
        finally:
            self._slimproto_connections.pop(player_id, None)
            if player_id in self.players:
                self.players[player_id]["connected"] = 0
                self.players[player_id]["power"] = 0
            if player_id in self.slimproto_players:
                self.slimproto_players[player_id]["connected"] = False
                self.slimproto_players[player_id]["power"] = False
                self.slimproto_players[player_id]["mode"] = self.players.get(player_id, {}).get(
                    "mode", "stop"
                )
            self._notify_player_state(player_id)
            if not writer.is_closing():
                writer.close()

    def _status_for_player(self, player_id: str) -> dict[str, Any]:
        """Return a status payload for a known fake player."""
        player = self.players.get(player_id)
        if player is None:
            return {
                "playerid": player_id,
                "connected": 0,
                "power": 0,
                "mode": "stop",
            }
        self._normalize_playlist_fields(player)
        playlist_loop = list(cast("list[dict[str, Any]]", player.get("playlist_loop", [])))
        return {
            "playerid": player_id,
            "name": player.get("name", player_id),
            "model": player.get("model", "test"),
            "connected": int(player.get("connected", 1)),
            "power": int(player.get("power", 1)),
            "mode": player.get("mode", "stop"),
            "playlist index": int(player.get("playlist index", 0)),
            "playlist tracks": int(player.get("playlist tracks", 0)),
            "playlist_cur_index": int(player.get("playlist_cur_index", 0)),
            "playlist_tracks": int(player.get("playlist_tracks", 0)),
            "playlist_loop": playlist_loop,
            "playlist shuffle": int(player.get("playlist shuffle", 0)),
            "playlist repeat": int(player.get("playlist repeat", 0)),
            "playlist_timestamp": float(player.get("playlist_timestamp", 0.0)),
            "volume": int(player.get("volume", 50)),
            "player_name": player.get("player_name", player_id),
            "isplaying": int(player.get("isplaying", 0)),
        }

    def _default_player_state(self, player_id: str) -> dict[str, Any]:
        """Return default fake player state used by queue/playback commands."""
        return {
            "playerid": player_id,
            "name": player_id,
            "model": "test",
            "connected": 1,
            "power": 1,
            "mode": "stop",
            "volume": 50,
            "playlist index": 0,
            "playlist tracks": 0,
            "playlist_cur_index": 0,
            "playlist_tracks": 0,
            "playlist_loop": [],
            "playlist shuffle": 0,
            "playlist repeat": 0,
            "playlist_timestamp": 0.0,
            "player_name": player_id,
            "isplaying": 0,
            "sync_master": "",
        }

    def _ensure_player(self, player_id: str) -> dict[str, Any]:
        """Ensure player state exists and has queue bookkeeping fields."""
        player = self.players.setdefault(player_id, self._default_player_state(player_id))
        self._normalize_playlist_fields(player)
        return player

    def _normalize_playlist_fields(self, player: dict[str, Any]) -> None:
        """Keep playlist field aliases in sync to match LMS/client naming variants."""
        playlist_loop = player.get("playlist_loop")
        if not isinstance(playlist_loop, list):
            playlist_loop = []
            player["playlist_loop"] = playlist_loop

        tracks = len(playlist_loop)

        current_index = _coerce_int(
            player.get("playlist_cur_index", player.get("playlist index", 0)),
            0,
        )
        if tracks <= 0:
            current_index = 0
        else:
            current_index = max(0, min(tracks - 1, current_index))

        player["playlist_tracks"] = tracks
        player["playlist tracks"] = tracks
        player["playlist_cur_index"] = current_index
        player["playlist index"] = current_index
        player.setdefault("playlist shuffle", 0)
        player.setdefault("playlist repeat", 0)
        player.setdefault("playlist_timestamp", 0.0)

    def _touch_playlist_timestamp(self, player: dict[str, Any]) -> None:
        """Bump playlist timestamp to emulate LMS queue mutation notifications."""
        current = player.get("playlist_timestamp", 0.0)
        try:
            current_float = float(current)
        except TypeError, ValueError:
            current_float = 0.0
        player["playlist_timestamp"] = current_float + 1.0

    def _append_playlist_item(
        self,
        player: dict[str, Any],
        *,
        track_id: str | None = None,
        url: str | None = None,
        title: str | None = None,
        artist: str | None = None,
        album: str | None = None,
    ) -> None:
        """Append one queue item and normalize playlist counters."""
        playlist_loop = cast("list[dict[str, Any]]", player.setdefault("playlist_loop", []))
        item: dict[str, Any] = {}
        if track_id is not None:
            item["track_id"] = track_id
            item["id"] = track_id
        if url is not None:
            item["url"] = url
        if title:
            item["title"] = title
        if artist:
            item["artist"] = artist
        if album:
            item["album"] = album
        playlist_loop.append(item)
        self._normalize_playlist_fields(player)

    def _set_playlist_index(self, player_id: str, index_value: Any) -> dict[str, Any]:
        """Set player queue index from absolute or relative LMS index input."""
        player = self._ensure_player(player_id)
        self._normalize_playlist_fields(player)
        tracks = _coerce_int(player.get("playlist_tracks"), 0)
        if tracks <= 0:
            player["playlist_cur_index"] = 0
            player["playlist index"] = 0
            return self._status_for_player(player_id)

        current = _coerce_int(player.get("playlist_cur_index"), 0)
        target = current
        if isinstance(index_value, str) and index_value.startswith(("+", "-")):
            target = current + _coerce_int(index_value, 0)
        else:
            target = _coerce_int(index_value, current)
        target = max(0, min(tracks - 1, target))
        player["playlist_cur_index"] = target
        player["playlist index"] = target
        player["mode"] = "play"
        player["isplaying"] = 1
        self._notify_player_state(player_id)
        return self._status_for_player(player_id)

    def _advance_playlist_index(self, player_id: str, delta: int) -> dict[str, Any]:
        """Move queue index forward/backward and mark player as playing."""
        current = _coerce_int(self._ensure_player(player_id).get("playlist_cur_index"), 0)
        return self._set_playlist_index(player_id, current + delta)

    def _handle_playlist_command(self, player_id: str, command: list[Any]) -> dict[str, Any]:
        """Apply LMS playlist commands to fake queue state."""
        player = self._ensure_player(player_id)
        sub_action = str(command[1]) if len(command) > 1 else ""

        if sub_action == "clear":
            player["playlist_loop"] = []
            player["playlist_cur_index"] = 0
            player["playlist index"] = 0
            player["playlist_tracks"] = 0
            player["playlist tracks"] = 0
            player["mode"] = "stop"
            player["isplaying"] = 0
            self._touch_playlist_timestamp(player)
            self._notify_player_state(player_id)
            return self._status_for_player(player_id)

        if sub_action == "play":
            if len(command) > 2 and isinstance(command[2], str):
                player["playlist_loop"] = []
                self._append_playlist_item(player, url=command[2])
                player["playlist_cur_index"] = 0
                player["playlist index"] = 0
                self._touch_playlist_timestamp(player)
            player["mode"] = "play"
            player["isplaying"] = 1
            self._notify_player_state(player_id)
            return self._status_for_player(player_id)

        if sub_action == "add":
            if len(command) > 2 and isinstance(command[2], str):
                self._append_playlist_item(player, url=command[2])
                self._touch_playlist_timestamp(player)
            self._notify_player_state(player_id)
            return self._status_for_player(player_id)

        if sub_action == "delete":
            if len(command) > 2:
                delete_index = _coerce_int(command[2], -1)
                playlist_loop = cast(
                    "list[dict[str, Any]]",
                    player.setdefault("playlist_loop", []),
                )
                if 0 <= delete_index < len(playlist_loop):
                    del playlist_loop[delete_index]
                    current = _coerce_int(player.get("playlist_cur_index"), 0)
                    if delete_index < current:
                        current -= 1
                    current = max(0, min(len(playlist_loop) - 1, current)) if playlist_loop else 0
                    player["playlist_cur_index"] = current
                    player["playlist index"] = current
                    self._touch_playlist_timestamp(player)
            self._normalize_playlist_fields(player)
            self._notify_player_state(player_id)
            return self._status_for_player(player_id)

        if sub_action == "move" and len(command) > 3:
            from_index = _coerce_int(command[2], -1)
            to_index = _coerce_int(command[3], -1)
            playlist_loop = cast("list[dict[str, Any]]", player.setdefault("playlist_loop", []))
            if 0 <= from_index < len(playlist_loop) and 0 <= to_index < len(playlist_loop):
                moved = playlist_loop.pop(from_index)
                playlist_loop.insert(to_index, moved)
                current = _coerce_int(player.get("playlist_cur_index"), 0)
                if current == from_index:
                    current = to_index
                elif from_index < current <= to_index:
                    current -= 1
                elif to_index <= current < from_index:
                    current += 1
                player["playlist_cur_index"] = current
                player["playlist index"] = current
                self._touch_playlist_timestamp(player)
            self._normalize_playlist_fields(player)
            self._notify_player_state(player_id)
            return self._status_for_player(player_id)

        if sub_action == "index" and len(command) > 2:
            return self._set_playlist_index(player_id, command[2])

        if sub_action == "repeat" and len(command) > 2:
            player["playlist repeat"] = _coerce_int(command[2], 0)
            self._notify_player_state(player_id)
            return self._status_for_player(player_id)

        if sub_action == "shuffle" and len(command) > 2:
            player["playlist shuffle"] = _coerce_int(command[2], 0)
            self._notify_player_state(player_id)
            return self._status_for_player(player_id)

        if sub_action == "zap" and len(command) > 2:
            return self._handle_playlist_command(player_id, ["playlist", "delete", command[2]])

        return self._status_for_player(player_id)

    def _handle_playlistcontrol_command(self, player_id: str, command: list[Any]) -> dict[str, Any]:
        """Apply LMS playlistcontrol commands used by Lyrion queue sync."""
        player = self._ensure_player(player_id)
        cmd_arg = next(
            (
                str(arg).split(":", 1)[1]
                for arg in command[1:]
                if isinstance(arg, str) and str(arg).startswith("cmd:")
            ),
            "",
        )
        track_id = next(
            (
                str(arg).split(":", 1)[1]
                for arg in command[1:]
                if isinstance(arg, str) and str(arg).startswith("track_id:")
            ),
            None,
        )
        url = next(
            (
                str(arg).split(":", 1)[1]
                for arg in command[1:]
                if isinstance(arg, str) and str(arg).startswith("url:")
            ),
            None,
        )
        title = next(
            (
                str(arg).split(":", 1)[1]
                for arg in command[1:]
                if isinstance(arg, str) and str(arg).startswith("title:")
            ),
            None,
        )
        artist = next(
            (
                str(arg).split(":", 1)[1]
                for arg in command[1:]
                if isinstance(arg, str) and str(arg).startswith("artist:")
            ),
            None,
        )
        album = next(
            (
                str(arg).split(":", 1)[1]
                for arg in command[1:]
                if isinstance(arg, str) and str(arg).startswith("album:")
            ),
            None,
        )

        if cmd_arg == "load":
            player["playlist_loop"] = []
            self._append_playlist_item(
                player,
                track_id=track_id,
                url=url,
                title=title,
                artist=artist,
                album=album,
            )
            player["playlist_cur_index"] = 0
            player["playlist index"] = 0
            player["mode"] = "play"
            player["isplaying"] = 1
            self._touch_playlist_timestamp(player)
            self._notify_player_state(player_id)
            return self._status_for_player(player_id)

        if cmd_arg == "add":
            self._append_playlist_item(
                player,
                track_id=track_id,
                url=url,
                title=title,
                artist=artist,
                album=album,
            )
            self._touch_playlist_timestamp(player)
            self._notify_player_state(player_id)
            return self._status_for_player(player_id)

        return self._status_for_player(player_id)

    def _handle_button_command(self, player_id: str, command: list[Any]) -> dict[str, Any]:
        """Apply LMS button command names commonly sent by clients."""
        if len(command) < 2:
            return self._status_for_player(player_id)

        button_name = str(command[1])
        if button_name == "jump_fwd":
            return self._advance_playlist_index(player_id, +1)
        if button_name == "jump_rew":
            return self._advance_playlist_index(player_id, -1)
        if button_name == "play":
            return self.set_player_mode(player_id, "play")
        if button_name == "pause":
            return self.set_player_mode(player_id, "pause")
        if button_name == "stop":
            return self.set_player_mode(player_id, "stop")
        return self._status_for_player(player_id)

    def _build_serverstatus_result(self) -> dict[str, Any]:
        """Build the serverstatus payload expected by CometD / roster discovery."""
        players_loop = [
            {
                "playerid": player_id,
                "name": player.get("name", player_id),
                "model": player.get("model", "test"),
                "connected": int(player.get("connected", 1)),
                "power": int(player.get("power", 1)),
            }
            for player_id, player in sorted(self.players.items())
        ]
        return {
            "player count": len(players_loop),
            "players_loop": players_loop,
        }

    @property
    def app(self) -> web.Application:
        """Build and return an aiohttp app exposing fake LMS endpoints."""
        app = web.Application()
        app.router.add_post("/jsonrpc.js", self.handle_jsonrpc)
        app.router.add_get(
            "/music/{track_id}/download",
            self.handle_track_stream,
        )
        for route in (
            "/music/{cover_id}/cover_600x600_f",
            "/music/{cover_id}/cover_300x300_f",
            "/contributor/{artist_id}/image_600x600_f",
            "/contributor/{artist_id}/image_300x300_f",
            "/imageproxy/mai/_artist/{artist_id}/image_600x600_f",
            "/imageproxy/mai/_artist/{artist_id}/image_300x300_f",
        ):
            app.router.add_get(route, self.handle_artwork)
        return app

    async def handle_jsonrpc(self, request: web.Request) -> web.Response:
        """Handle LMS JSON-RPC method slim.request."""
        payload = await request.json()
        params = payload.get("params") if isinstance(payload, dict) else None
        if not isinstance(params, list) or len(params) < 2:
            return self._rpc_error(-32602, "invalid params", status=400)

        player_id = str(params[0])
        command = params[1]
        if not isinstance(command, list) or not command:
            return self._rpc_error(-32602, "invalid command", status=400)

        command_name = str(command[0])
        self.rpc_calls.append(
            FakeRpcCall(
                command=command_name,
                player_id=player_id,
                args=tuple(command),
            )
        )

        if command_name in self.command_errors:
            code, message = self.command_errors[command_name]
            return self._rpc_error(code, message, status=200)

        if command_name == "serverstatus":
            return web.json_response({"id": 1, "result": self._build_serverstatus_result()})

        if command_name == "players":
            return web.json_response({"id": 1, "result": self._build_serverstatus_result()})

        if command_name == "status":
            status = self._status_for_player(player_id)
            return web.json_response({"id": 1, "result": status})

        if command_name in {
            "play",
            "pause",
            "stop",
            "playlist",
            "playlistcontrol",
            "button",
        }:
            result = await self.handle_jsonrpc_command(player_id, command)
            return web.json_response({"id": 1, "result": result})

        if command_name == "player":
            action = command[1] if len(command) > 1 else ""
            if action == "register":
                name = str(command[2]) if len(command) > 2 else player_id
                model = str(command[3]) if len(command) > 3 else "test"
                await self.connect_player(player_id, name, model)
                return web.json_response({"id": 1, "result": self._status_for_player(player_id)})
            if action == "disconnect":
                await self.disconnect_player(player_id)
                return web.json_response(
                    {"id": 1, "result": {"playerid": player_id, "connected": 0}}
                )

        if command_name == "artists":
            return self._rpc_result(command, "artists")
        if command_name == "albums":
            return self._rpc_result(command, "albums")
        if command_name == "titles":
            return self._rpc_result(command, "titles")
        if command_name == "playlists":
            return self._rpc_result(command, "playlists")
        if command_name == "genres":
            return self._rpc_result(command, "genres")

        return self._rpc_error(
            -32601,
            f"Unknown command: {command_name}",
            status=200,
        )

    async def handle_track_stream(self, request: web.Request) -> web.Response:
        """Return fake audio bytes for stream URL tests."""
        track_id = request.match_info["track_id"]
        self.stream_requests.append(track_id)
        body = f"FAKEAUDIO:{track_id}".encode()
        return web.Response(body=body, content_type="audio/mpeg")

    async def handle_artwork(self, request: web.Request) -> web.Response:
        """Return fake image bytes and optional 600px misses for fallback tests."""
        self.image_requests.append(request.path)
        artist_id = request.match_info.get("artist_id")
        cover_id = request.match_info.get("cover_id")
        if "600x600" in request.path:
            if artist_id and artist_id in self.missing_large_artist_images:
                return web.Response(status=404)
            if cover_id and cover_id in self.missing_large_album_images:
                return web.Response(status=404)
        return web.Response(body=PNG_1X1_BYTES, content_type="image/png")

    def clear_history(self) -> None:
        """Clear captured request state between assertions."""
        self.rpc_calls.clear()
        self.stream_requests.clear()
        self.image_requests.clear()

    def _rpc_result(self, command: list[Any], entity: str) -> web.Response:
        return web.json_response({"id": 1, "result": self._build_result(command, entity)})

    def _rpc_error(self, code: int, message: str, status: int) -> web.Response:
        return web.json_response(
            {"id": 1, "error": {"code": code, "message": message}},
            status=status,
        )

    def _build_result(self, command: list[Any], entity: str) -> dict[str, Any]:
        if entity == "playlists" and len(command) > 1 and command[1] == "tracks":
            return self._build_playlist_tracks_result(command)

        offset = _coerce_int(command[1] if len(command) > 1 else 0, 0)
        limit = _coerce_int(command[2] if len(command) > 2 else 250, 250)
        filters = [
            str(arg)
            for arg in command[3:]
            if isinstance(arg, (str, int, float)) and ":" in str(arg)
        ]

        if entity == "artists":
            rows = self._filter_artists(self.artists, filters)
            rows = self._maybe_make_incomplete_batch(entity, filters, rows)
            return _page_result("artists_loop", rows, offset, limit)
        if entity == "albums":
            rows = self._filter_albums(self.albums, filters)
            rows = self._maybe_make_incomplete_batch(entity, filters, rows)
            return _page_result("albums_loop", rows, offset, limit)
        if entity == "titles":
            rows = self._filter_tracks(self.tracks, filters)
            rows = self._maybe_make_incomplete_batch(entity, filters, rows)
            return _page_result("titles_loop", rows, offset, limit)
        if entity == "playlists":
            rows = self._filter_playlists(self.playlists, filters)
            return _page_result("playlists_loop", rows, offset, limit)

        rows = self._filter_genres(self.genres, filters)
        rows = sorted(
            rows,
            key=lambda row: str(row.get("genre") or row.get("name") or "").casefold(),
        )
        return _page_result("genres_loop", rows, offset, limit)

    def _build_playlist_tracks_result(self, command: list[Any]) -> dict[str, Any]:
        """Return a paged playlisttracks_loop response for playlists tracks."""
        offset = _coerce_int(command[2] if len(command) > 2 else 0, 0)
        limit = _coerce_int(command[3] if len(command) > 3 else 250, 250)
        filters = [
            str(arg)
            for arg in command[4:]
            if isinstance(arg, (str, int, float)) and ":" in str(arg)
        ]

        playlist_id: str | None = None
        for key, value in _iter_filters(filters):
            if key in {"playlist_id", "id"}:
                playlist_id = value
                break

        if playlist_id is None:
            return _page_result("playlisttracks_loop", [], offset, limit)

        wanted_ids = self.playlist_tracks.get(playlist_id, [])
        track_lookup = {str(row["id"]): row for row in self.tracks}
        rows = [track_lookup[track_id] for track_id in wanted_ids if track_id in track_lookup]
        return _page_result("playlisttracks_loop", rows, offset, limit)

    def _maybe_make_incomplete_batch(
        self,
        entity: str,
        filters: list[str],
        rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Optionally drop one row from multi-id lookups to simulate LMS bugs."""
        if entity not in self.incomplete_batch_for_entities:
            return rows

        id_filter_key = {
            "artists": "artist_id",
            "albums": "album_id",
            "titles": "track_id",
        }.get(entity)
        if id_filter_key is None:
            return rows

        for key, value in _iter_filters(filters):
            if key == id_filter_key and "," in value and len(rows) > 1:
                return rows[:-1]
        return rows

    def _filter_artists(
        self,
        rows: list[dict[str, Any]],
        filters: list[str],
    ) -> list[dict[str, Any]]:
        result = rows
        for key, value in _iter_filters(filters):
            if key == "search":
                result = [
                    row for row in result if _search_match(value, row, ("artist", "name", "id"))
                ]
            elif key in {"artist_id", "id"}:
                wanted = set(_split_csv(value))
                result = [row for row in result if str(row.get("id")) in wanted]
        return result

    def _filter_albums(
        self,
        rows: list[dict[str, Any]],
        filters: list[str],
    ) -> list[dict[str, Any]]:
        result = rows
        for key, value in _iter_filters(filters):
            if key == "search":
                result = [
                    row
                    for row in result
                    if _search_match(
                        value,
                        row,
                        ("album", "title", "artist", "id"),
                    )
                ]
            elif key in {"album_id", "id"}:
                wanted = set(_split_csv(value))
                result = [row for row in result if str(row.get("id")) in wanted]
            elif key == "artist_id":
                wanted = set(_split_csv(value))
                result = [row for row in result if str(row.get("artist_id")) in wanted]
            elif key == "genre_id":
                wanted = set(_split_csv(value))
                result = [row for row in result if str(row.get("genre_id")) in wanted]
        return result

    def _filter_tracks(
        self,
        rows: list[dict[str, Any]],
        filters: list[str],
    ) -> list[dict[str, Any]]:
        result = rows
        for key, value in _iter_filters(filters):
            if key == "search":
                result = [
                    row
                    for row in result
                    if _search_match(
                        value,
                        row,
                        ("title", "track", "artist", "album", "id"),
                    )
                ]
            elif key in {"track_id", "id"}:
                wanted = set(_split_csv(value))
                result = [row for row in result if str(row.get("id")) in wanted]
            elif key == "album_id":
                wanted = set(_split_csv(value))
                result = [row for row in result if str(row.get("album_id")) in wanted]
            elif key == "artist_id":
                wanted = set(_split_csv(value))
                result = [row for row in result if str(row.get("artist_id")) in wanted]
            elif key == "playlist_id":
                playlist_track_ids = set(self.playlist_tracks.get(value, []))
                result = [row for row in result if str(row.get("id")) in playlist_track_ids]
        return result

    def _filter_playlists(
        self,
        rows: list[dict[str, Any]],
        filters: list[str],
    ) -> list[dict[str, Any]]:
        result = rows
        for key, value in _iter_filters(filters):
            if key == "search":
                result = [
                    row for row in result if _search_match(value, row, ("playlist", "name", "id"))
                ]
            elif key in {"playlist_id", "id"}:
                wanted = set(_split_csv(value))
                result = [row for row in result if str(row.get("id")) in wanted]
        return result

    def _filter_genres(
        self,
        rows: list[dict[str, Any]],
        filters: list[str],
    ) -> list[dict[str, Any]]:
        result = rows
        for key, value in _iter_filters(filters):
            if key == "search":
                result = [
                    row for row in result if _search_match(value, row, ("genre", "name", "id"))
                ]
            elif key in {"genre_id", "id"}:
                wanted = set(_split_csv(value))
                result = [row for row in result if str(row.get("id")) in wanted]
        return result


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except TypeError, ValueError:
        return default


def _split_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _iter_filters(filters: list[str]) -> list[tuple[str, str]]:
    parsed: list[tuple[str, str]] = []
    for raw in filters:
        key, _, value = raw.partition(":")
        key = key.strip()
        value = value.strip()
        if not key or key == "tags":
            continue
        parsed.append((key, value))
    return parsed


def _search_match(
    query: str,
    row: dict[str, Any],
    keys: tuple[str, ...],
) -> bool:
    needle = query.casefold()
    for key in keys:
        value = row.get(key)
        if value is None:
            continue
        if needle in str(value).casefold():
            return True
    return False


def _page_result(
    loop_key: str,
    rows: list[dict[str, Any]],
    offset: int,
    limit: int,
) -> dict[str, Any]:
    start = max(0, offset)
    stop = start + max(0, limit)
    page = rows[start:stop]
    return {
        loop_key: [dict(item) for item in page],
        "count": len(rows),
    }
