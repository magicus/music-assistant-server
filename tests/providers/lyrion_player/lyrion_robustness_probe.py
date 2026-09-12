"""Small CLI probe for manual Lyrion robustness checks."""

from __future__ import annotations

import argparse
import json
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen


def _json_rpc(base_url: str, player_id: str, command: list[Any]) -> dict[str, Any]:
    """Send one JSON-RPC request to LMS and return the decoded payload."""
    payload = {
        "id": 1,
        "method": "slim.request",
        "params": [player_id, command],
    }
    request = Request(
        f"{base_url}/jsonrpc.js",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request, timeout=10) as response:
        data = json.loads(response.read().decode("utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError(f"Unexpected JSON-RPC response payload: {data!r}")
    result = data.get("result")
    if not isinstance(result, dict):
        raise RuntimeError(f"Missing JSON-RPC result payload: {data!r}")
    return result


def _print_snapshot(base_url: str, player_id: str) -> None:
    """Print serverstatus and player status for one player."""
    snapshot = {
        "serverstatus": _json_rpc(base_url, "", ["serverstatus", 0, 500]),
        "playerstatus": _json_rpc(base_url, player_id, ["status", "-", 1]),
    }
    print(json.dumps(snapshot, indent=2, sort_keys=True))


def _send_command(base_url: str, player_id: str, action: str, track_id: str | None) -> None:
    """Send one reproducible LMS command from a second controller."""
    if action == "load-play":
        if not track_id:
            raise SystemExit("--track-id is required for load-play")
        _json_rpc(base_url, player_id, ["playlistcontrol", "cmd:load", f"track_id:{track_id}"])
        _json_rpc(base_url, player_id, ["play"])
        return

    command_map = {
        "play": ["play"],
        "pause": ["pause", 1],
        "stop": ["stop"],
        "next": ["playlist", "index", "+1"],
        "previous": ["playlist", "index", "-1"],
    }
    command = command_map.get(action)
    if command is None:
        raise SystemExit(f"Unsupported action: {action}")
    _json_rpc(base_url, player_id, command)


def main() -> None:
    """Run the probe CLI."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url", required=True, help="LMS base URL, e.g. http://127.0.0.1:9000"
    )
    parser.add_argument("--player-id", required=True, help="LMS player id to inspect or command")

    subparsers = parser.add_subparsers(dest="mode", required=True)

    subparsers.add_parser("snapshot", help="Print current serverstatus and playerstatus")

    command_parser = subparsers.add_parser("command", help="Send one control command")
    command_parser.add_argument(
        "--action",
        required=True,
        choices=["load-play", "play", "pause", "stop", "next", "previous"],
    )
    command_parser.add_argument("--track-id", help="Track id to load before playing")

    args = parser.parse_args()

    try:
        if args.mode == "snapshot":
            _print_snapshot(args.base_url, args.player_id)
        else:
            _send_command(args.base_url, args.player_id, args.action, args.track_id)
    except URLError as err:
        raise SystemExit(f"LMS request failed: {err}") from err


if __name__ == "__main__":
    main()
