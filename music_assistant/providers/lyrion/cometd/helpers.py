"""Shared helper types and parsing utilities for Lyrion CometD."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, cast

StatusPayload = dict[str, object]
LmsPlayerEventCallback = Callable[[Any], Awaitable[None]]


@dataclass
class _TrackEndExpectation:
    """Expected playback transition when current track nears end."""

    expected_transition_at: float
    baseline_index: int | None
    baseline_track_id: str | None
    baseline_playlist_timestamp: float | None


def _get_mode(status: StatusPayload) -> str:
    """Return normalized LMS playback mode from a status payload."""
    mode = status.get("mode")
    if isinstance(mode, str):
        return mode
    return "stop"


def _get_power(status: StatusPayload) -> bool | None:
    """Return boolean power state from LMS status, if available."""
    if "power" not in status:
        return None
    try:
        return bool(int(cast("int | str", status["power"])))
    except TypeError, ValueError:
        return None


def _get_int(status: StatusPayload, key: str) -> int | None:
    """Parse integer field from LMS status payload."""
    value = status.get(key)
    if value is None:
        return None
    try:
        return int(cast("int | str", value))
    except TypeError, ValueError:
        return None


def _get_float(status: StatusPayload, key: str) -> float | None:
    """Parse float field from LMS status payload."""
    value = status.get(key)
    if value is None:
        return None
    try:
        return float(cast("int | float | str", value))
    except TypeError, ValueError:
        return None


def _same_active_track(old_status: StatusPayload, new_status: StatusPayload) -> bool:
    """Return True when both statuses point to the same active queue item."""
    if old_status.get("playlist_cur_index") != new_status.get("playlist_cur_index"):
        return False

    old_track_id = _extract_current_track_id(old_status)
    new_track_id = _extract_current_track_id(new_status)
    if old_track_id is None or new_track_id is None:
        return False
    return old_track_id == new_track_id


def _extract_current_track_id(status: StatusPayload) -> str | None:
    """Extract active track id from status.playlist_loop if present."""
    playlist_loop = status.get("playlist_loop")
    if not isinstance(playlist_loop, list) or not playlist_loop:
        return None

    current_index = _get_int(status, "playlist_cur_index")
    if current_index is None or not 0 <= current_index < len(playlist_loop):
        return None

    current_item = playlist_loop[current_index]
    if not isinstance(current_item, dict):
        return None

    for key in ("id", "track_id"):
        value = current_item.get(key)
        if value is not None:
            return str(value)
    return None


def _extract_server_player_ids(payload: StatusPayload) -> set[str]:
    """Extract normalized player ids from a serverstatus payload."""
    players_loop = payload.get("players_loop")
    if not isinstance(players_loop, list):
        return set()

    player_ids: set[str] = set()
    for player_data in players_loop:
        if not isinstance(player_data, dict):
            continue
        player_id = player_data.get("playerid")
        if player_id:
            player_ids.add(str(player_id))
    return player_ids


def _is_invalid_player_payload(payload: StatusPayload) -> bool:
    """Return True when LMS status subscription reports an invalid player."""
    error = payload.get("error")
    return isinstance(error, str) and error == "invalid player"
