"""Shared helper classes/utilities for Lyrion player harness tests."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

import aiohttp

from tests.providers.lyrion.fake_lms_server import FakeLmsServer
from tests.providers.lyrion.lms_server_harness import LyrionTestEndpoint


class FakeMAProvider:
    """Minimal MA provider stub used to prove bidirectional play/pause sync."""

    def __init__(self, server: FakeLmsServer, player_id: str) -> None:
        """Initialize the provider stub state."""
        self.server = server
        self.player_id = player_id
        self.latest_status: dict[str, Any] = {"playerid": player_id, "mode": "stop"}
        self.server.add_player_state_listener(self._on_player_state_changed)

    def _on_player_state_changed(self, player_id: str, state: dict[str, Any]) -> None:
        """Track server-side updates as MA would do when LMS status changes."""
        if player_id != self.player_id:
            return
        self.latest_status = state

    async def send_player_command(self, command: Sequence[str | int]) -> dict[str, Any]:
        """Send a command to the server, mirroring the MA provider JSON-RPC call path."""
        return await self.server.handle_jsonrpc_command(self.player_id, list(command))


class EndpointRpcClient:
    """Small JSON-RPC client used for backend-agnostic command injection."""

    def __init__(self, endpoint: LyrionTestEndpoint, player_id: str) -> None:
        """Initialize endpoint and player identity."""
        self._endpoint = endpoint
        self._player_id = player_id
        self._session = aiohttp.ClientSession()

    async def send(self, command: list[Any]) -> dict[str, Any]:
        """Send a raw LMS JSON-RPC command for the configured player."""
        payload = {
            "id": 1,
            "method": "slim.request",
            "params": [self._player_id, command],
        }
        data: dict[str, Any] | None = None
        for attempt in range(3):
            try:
                async with self._session.post(
                    f"{self._endpoint.base_url}/jsonrpc.js",
                    json=payload,
                ) as response:
                    response.raise_for_status()
                    data = await response.json()
                break
            except aiohttp.ServerDisconnectedError:
                if attempt == 2:
                    raise
                await asyncio.sleep(0.1 * (attempt + 1))

        if data is None:
            raise RuntimeError("JSON-RPC response payload was unexpectedly empty")
        result = data.get("result")
        if not isinstance(result, dict):
            raise TypeError("JSON-RPC response is missing result payload")
        return result

    async def close(self) -> None:
        """Close the underlying HTTP session."""
        await self._session.close()


class ProviderStyleRpcClient:
    """Fake MA/provider command path through LMS JSON-RPC."""

    def __init__(self, endpoint: LyrionTestEndpoint, player_id: str) -> None:
        """Initialize provider-style client."""
        self._rpc = EndpointRpcClient(endpoint, player_id)

    async def send_player_command(self, command: list[Any]) -> dict[str, Any]:
        """Send a command as MA/provider would do."""
        return await self._rpc.send(command)

    async def close(self) -> None:
        """Close resources held by the provider-style client."""
        await self._rpc.close()


def playlist_index(status: dict[str, Any]) -> int:
    """Read current queue index from LMS status across key-name variants."""
    if "playlist_cur_index" in status:
        return int(status["playlist_cur_index"])
    return int(status.get("playlist index", 0))


def playlist_tracks(status: dict[str, Any]) -> int:
    """Read queue length from LMS status across key-name variants."""
    if "playlist_tracks" in status:
        return int(status["playlist_tracks"])
    return int(status.get("playlist tracks", 0))


def playlist_values(status: dict[str, Any]) -> list[str]:
    """Return queue identity values in order from playlist_loop payload."""
    playlist_loop = status.get("playlist_loop")
    if not isinstance(playlist_loop, list):
        return []
    values: list[str] = []
    for row in playlist_loop:
        if not isinstance(row, dict):
            continue
        for key in ("url", "track_id", "title", "id"):
            raw = row.get(key)
            if raw is not None:
                values.append(str(raw))
                break
    return values


def player_connected(status: dict[str, Any]) -> int:
    """Read player connection state across LMS key-name variants."""
    if "connected" in status:
        return int(status["connected"])
    return int(status.get("player_connected", 0))


def playlist_repeat(status: dict[str, Any]) -> int:
    """Read repeat mode from LMS status across key-name variants."""
    if "playlist repeat" in status:
        return int(status["playlist repeat"])
    return int(status.get("playlist_repeat", 0))


def playlist_shuffle(status: dict[str, Any]) -> int:
    """Read shuffle mode from LMS status across key-name variants."""
    if "playlist shuffle" in status:
        return int(status["playlist shuffle"])
    return int(status.get("playlist_shuffle", 0))


def playback_mode(status: dict[str, Any]) -> str:
    """Read playback mode from LMS status."""
    mode = status.get("mode")
    return str(mode) if isinstance(mode, str) else "stop"


def sync_master(status: dict[str, Any]) -> str | None:
    """Read sync master id from LMS status across key-name variants."""
    for key in ("sync_master", "sync_master_id", "sync_master_playerid"):
        value = status.get(key)
        if isinstance(value, str):
            cleaned = value.strip()
            if cleaned and cleaned != "-":
                return cleaned
        elif value is not None:
            cleaned = str(value).strip()
            if cleaned and cleaned != "-":
                return cleaned
    return None


def sync_slaves(status: dict[str, Any]) -> set[str]:
    """Read sync slave ids from LMS status across field formats."""
    if isinstance(status.get("sync_slaves"), str):
        raw_value = str(status["sync_slaves"])
        return {part.strip() for part in raw_value.split(",") if part.strip()}

    if isinstance(status.get("sync_slaves_loop"), list):
        members: set[str] = set()
        for item in status["sync_slaves_loop"]:
            if isinstance(item, dict):
                if player_id := item.get("playerid"):
                    members.add(str(player_id))
            elif item:
                members.add(str(item))
        return members
    return set()


def repeat_mode_name(repeat_value: int) -> str:
    """Map LMS repeat integer to named loop mode expected by MA."""
    return {0: "none", 1: "track", 2: "playlist"}.get(repeat_value, "none")


async def wait_for_playlist_index(
    client: EndpointRpcClient,
    expected_index: int,
    timeout: float = 2.0,
) -> dict[str, Any]:
    """Poll status until LMS reports the expected queue index."""
    deadline = asyncio.get_running_loop().time() + timeout
    latest = await client.send(["status", 0, 100])
    while asyncio.get_running_loop().time() < deadline:
        if playlist_index(latest) == expected_index:
            return latest
        await asyncio.sleep(0.1)
        latest = await client.send(["status", 0, 100])
    return latest


async def wait_for_playlist_repeat(
    client: EndpointRpcClient,
    expected_repeat: int,
    timeout: float = 2.0,
) -> dict[str, Any]:
    """Poll status until LMS reports the expected repeat value."""
    deadline = asyncio.get_running_loop().time() + timeout
    latest = await client.send(["status", 0, 100])
    while asyncio.get_running_loop().time() < deadline:
        if playlist_repeat(latest) == expected_repeat:
            return latest
        await asyncio.sleep(0.1)
        latest = await client.send(["status", 0, 100])
    return latest


async def wait_for_playlist_shuffle(
    client: EndpointRpcClient,
    expected_shuffle: int,
    timeout: float = 2.0,
) -> dict[str, Any]:
    """Poll status until LMS reports the expected shuffle value."""
    deadline = asyncio.get_running_loop().time() + timeout
    latest = await client.send(["status", 0, 100])
    while asyncio.get_running_loop().time() < deadline:
        if playlist_shuffle(latest) == expected_shuffle:
            return latest
        await asyncio.sleep(0.1)
        latest = await client.send(["status", 0, 100])
    return latest


async def wait_for_sync_master(
    client: EndpointRpcClient,
    expected_master: str | None,
    timeout: float = 2.0,
) -> dict[str, Any]:
    """Poll status until LMS reports the expected sync master."""
    deadline = asyncio.get_running_loop().time() + timeout
    latest = await client.send(["status", 0, 100])
    while asyncio.get_running_loop().time() < deadline:
        if sync_master(latest) == expected_master:
            return latest
        await asyncio.sleep(0.1)
        latest = await client.send(["status", 0, 100])
    return latest


async def wait_for_mode(
    client: EndpointRpcClient,
    expected_mode: str,
    timeout: float = 2.0,
) -> dict[str, Any]:
    """Poll status until LMS reports the expected playback mode."""
    deadline = asyncio.get_running_loop().time() + timeout
    latest = await client.send(["status", 0, 100])
    while asyncio.get_running_loop().time() < deadline:
        if playback_mode(latest) == expected_mode:
            return latest
        await asyncio.sleep(0.1)
        latest = await client.send(["status", 0, 100])
    return latest
