"""Bridge normalized CometD player events into runtime player updates."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from pylyrion.cometd.player_status_events import (
    NormalizedPlayerStatusEvent,
    PlayerPlaylistChanged,
    PlayerRepeatChanged,
    PlayerShuffleChanged,
    PlayerStatusUpdated,
)

if TYPE_CHECKING:
    from pylyrion.cometd.helpers import StatusPayload


DEFAULT_MODE_MAP: Mapping[str, object] = {
    "play": "play",
    "pause": "pause",
    "stop": "stop",
}


class LyrionCometDEventAdapter:
    """Translate normalized LMS events to runtime player updates."""

    def __init__(
        self,
        provider: Any,
        *,
        mode_map: Mapping[str, object] = DEFAULT_MODE_MAP,
        idle_state: object = "stop",
        is_supported_player: Callable[[object], bool] | None = None,
    ) -> None:
        """
        Initialize event adapter.

        :param provider: Owning provider instance.
        :param mode_map: Mapping from LMS mode values to runtime playback states.
        :param idle_state: Fallback playback state for unknown LMS mode values.
        :param is_supported_player: Optional predicate to filter player objects.
        """
        self.provider = provider
        self._mode_map = mode_map
        self._idle_state = idle_state
        self._is_supported_player = is_supported_player

    async def handle_event(self, event: NormalizedPlayerStatusEvent) -> None:
        """
        Apply one normalized event to runtime player state.

        :param event: Normalized internal LMS event.
        """
        player = self.provider.mass.players.get_player(event.player_id)
        if player is None:
            return
        if self._is_supported_player is not None and not self._is_supported_player(player):
            return

        if isinstance(event, PlayerStatusUpdated):
            self.apply_status(player, event.status)
            if event.is_initial:
                await player.sync_queue_from_lms()

        if isinstance(event, PlayerPlaylistChanged):
            await player.sync_queue_from_lms()

        if isinstance(
            event,
            (PlayerRepeatChanged, PlayerShuffleChanged),
        ):
            await player.sync_queue_from_lms()

    def apply_status(
        self,
        player: Any,
        status: StatusPayload,
    ) -> None:
        """
        Apply one normalized status payload to runtime player state.

        :param player: Target player.
        :param status: Merged playerstatus payload from LMS CometD stream.
        """
        if _is_invalid_player_status(status):
            player._attr_available = False
            player.update_state()
            return

        if (connected := _get_status_int(status, "player_connected")) is not None:
            player._attr_available = bool(connected)
        else:
            player._attr_available = True

        mode = str(status.get("mode") or "stop")
        player._attr_playback_state = self._mode_map.get(mode, self._idle_state)

        if "power" in status:
            with suppress(TypeError, ValueError):
                player._attr_powered = bool(int(status["power"]))

        if "mixer volume" in status:
            with suppress(TypeError, ValueError):
                player._attr_volume_level = max(
                    0,
                    min(100, int(status["mixer volume"])),
                )

        if "time" in status:
            with suppress(TypeError, ValueError):
                player._attr_elapsed_time = float(status["time"])
                player._attr_elapsed_time_last_updated = time.time()

        previous_group_members = tuple(player.group_members)
        player._attr_group_members = _extract_group_members(player.player_id, status)

        player.update_state()

        if previous_group_members != tuple(player.group_members):
            _refresh_related_group_players(
                self.provider,
                player,
                set(previous_group_members),
                set(player.group_members),
                status,
            )


def _get_status_int(status: StatusPayload, key: str) -> int | None:
    """Parse one integer field from status payload."""
    value = status.get(key)
    if value is None:
        return None
    try:
        return int(value)
    except TypeError, ValueError:
        return None


def _is_invalid_player_status(status: StatusPayload) -> bool:
    """Return True when LMS reports that the status player is invalid."""
    error = status.get("error")
    return isinstance(error, str) and error == "invalid player"


def _extract_group_members(player_id: str, status: StatusPayload) -> list[str]:
    """Extract runtime group members from LMS sync fields in a status payload."""
    sync_slaves = _extract_sync_slaves(status)
    if sync_slaves:
        members = [member_id for member_id in sync_slaves if member_id != player_id]
        return [player_id, *members] if members else []

    sync_master = _extract_sync_master(status)
    if sync_master and sync_master != player_id:
        return []
    return []


def _extract_sync_master(status: StatusPayload) -> str | None:
    """Extract sync-master player id from LMS status payload."""
    for key in ("sync_master", "sync_master_id", "sync_master_playerid"):
        raw_value = status.get(key)
        if raw_value in (None, "", "-"):
            continue
        if isinstance(raw_value, dict):
            if player_id := raw_value.get("playerid"):
                return str(player_id)
            continue
        return str(raw_value)
    return None


def _extract_sync_slaves(status: StatusPayload) -> list[str]:
    """Extract sync-slave player ids from LMS status payload."""
    for key in ("sync_slaves", "sync_slaves_loop"):
        raw_value = status.get(key)
        if not raw_value:
            continue

        result: list[str] = []
        if isinstance(raw_value, str):
            for part in raw_value.split(","):
                value = part.strip()
                if value:
                    result.append(value)
        elif isinstance(raw_value, list):
            for item in raw_value:
                if isinstance(item, dict):
                    if player_id := item.get("playerid"):
                        result.append(str(player_id))
                elif item:
                    result.append(str(item))

        deduped = list(dict.fromkeys(result))
        if deduped:
            return deduped
    return []


def _refresh_related_group_players(
    provider: Any,
    player: Any,
    previous_members: set[str],
    current_members: set[str],
    status: StatusPayload,
) -> None:
    """Refresh players affected by a group topology change."""
    related_ids = (previous_members | current_members) - {player.player_id}

    if sync_master := _extract_sync_master(status):
        if sync_master != player.player_id:
            related_ids.add(sync_master)

    for provider_player in provider.players:
        if provider_player.player_id == player.player_id:
            continue
        if player.player_id in provider_player.group_members:
            related_ids.add(provider_player.player_id)

    for related_id in related_ids:
        if related_player := provider.mass.players.get_player(related_id):
            related_player.update_state()
