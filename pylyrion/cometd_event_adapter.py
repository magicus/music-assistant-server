"""Bridge normalized CometD player events into runtime player updates."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from contextlib import suppress
from typing import TYPE_CHECKING, Protocol

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

GetPlayerCallback = Callable[[str], object | None]
IterPlayersCallback = Callable[[], Iterable["LyrionRuntimePlayer"]]
SyncPlayerQueueCallback = Callable[[str], Awaitable[None]]
UpdatePlayerStateCallback = Callable[["LyrionRuntimePlayer"], None]


class LyrionRuntimePlayer(Protocol):
    """Neutral pylyrion runtime player contract for event adaptation."""

    player_id: str

    @property
    def group_members(self) -> list[str]:
        """Return current grouped player ids for this runtime player."""

    def set_available(self, available: bool) -> None:
        """Set runtime availability state."""

    def set_playback_state(self, playback_state: object) -> None:
        """Set runtime playback state."""

    def set_powered(self, powered: bool) -> None:
        """Set runtime powered state."""

    def set_volume_level(self, volume_level: int) -> None:
        """Set runtime volume level as percentage."""

    def set_elapsed_time(self, elapsed_time: float, updated_at: float) -> None:
        """Set runtime elapsed time and its update timestamp."""

    def set_group_members(self, group_members: list[str]) -> None:
        """Set grouped player ids for this runtime player."""


class LyrionCometDEventAdapter:
    """Translate normalized LMS events to runtime player updates."""

    def __init__(
        self,
        *,
        mode_map: Mapping[str, object] = DEFAULT_MODE_MAP,
        idle_state: object = "stop",
        is_supported_player: Callable[[LyrionRuntimePlayer], bool] | None = None,
        get_player: Callable[[str], LyrionRuntimePlayer | None],
        iter_players: IterPlayersCallback,
        sync_player_queue: SyncPlayerQueueCallback,
        update_player_state: UpdatePlayerStateCallback,
    ) -> None:
        """
        Initialize event adapter.

        :param mode_map: Mapping from LMS mode to runtime playback states.
        :param idle_state: Fallback playback state for unknown LMS mode values.
        :param is_supported_player: Optional player filter predicate.
        :param get_player: Callback that returns runtime player for player_id.
        :param iter_players: Callback returning players for group refresh.
        :param sync_player_queue: Callback to sync runtime queue for
            one player id.
        :param update_player_state: Callback to publish runtime player state.
        """
        self._mode_map = mode_map
        self._idle_state = idle_state
        self._is_supported_player = is_supported_player
        self._get_player = get_player
        self._iter_players = iter_players
        self._sync_player_queue = sync_player_queue
        self._update_player_state = update_player_state

    async def handle_event(self, event: NormalizedPlayerStatusEvent) -> None:
        """
        Apply one normalized event to runtime player state.

        :param event: Normalized internal LMS event.
        """
        player = self._get_player(event.player_id)
        if player is None:
            return
        if self._is_supported_player is not None and not self._is_supported_player(player):
            return

        if isinstance(event, PlayerStatusUpdated):
            self.apply_status(player, event.status)
            if event.is_initial:
                await self._sync_player_queue(event.player_id)

        if isinstance(event, PlayerPlaylistChanged):
            await self._sync_player_queue(event.player_id)

        if isinstance(
            event,
            (PlayerRepeatChanged, PlayerShuffleChanged),
        ):
            await self._sync_player_queue(event.player_id)

    def apply_status(
        self,
        player: LyrionRuntimePlayer,
        status: StatusPayload,
    ) -> None:
        """
        Apply one normalized status payload to runtime player state.

        :param player: Target player.
        :param status: Merged playerstatus payload from LMS CometD stream.
        """
        if _is_invalid_player_status(status):
            player.set_available(False)
            self._update_player_state(player)
            return

        if (connected := _get_status_int(status, "player_connected")) is not None:
            player.set_available(bool(connected))
        else:
            player.set_available(True)

        mode = str(status.get("mode") or "stop")
        player.set_playback_state(
            self._mode_map.get(
                mode,
                self._idle_state,
            )
        )

        if "power" in status:
            with suppress(TypeError, ValueError):
                player.set_powered(bool(int(status["power"])))

        if "mixer volume" in status:
            with suppress(TypeError, ValueError):
                player.set_volume_level(max(0, min(100, int(status["mixer volume"]))))

        if "time" in status:
            with suppress(TypeError, ValueError):
                player.set_elapsed_time(float(status["time"]), time.time())

        previous_group_members = tuple(player.group_members)
        player.set_group_members(
            _extract_group_members(
                player.player_id,
                status,
            )
        )

        self._update_player_state(player)

        if previous_group_members != tuple(player.group_members):
            _refresh_related_group_players(
                get_player=self._get_player,
                iter_players=self._iter_players,
                update_player_state=self._update_player_state,
                player=player,
                previous_members=set(previous_group_members),
                current_members=set(player.group_members),
                status=status,
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
    """Extract runtime group members from LMS sync fields."""
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
    *,
    get_player: Callable[[str], LyrionRuntimePlayer | None],
    iter_players: IterPlayersCallback,
    update_player_state: UpdatePlayerStateCallback,
    player: LyrionRuntimePlayer,
    previous_members: set[str],
    current_members: set[str],
    status: StatusPayload,
) -> None:
    """Refresh players affected by a group topology change."""
    related_ids = (previous_members | current_members) - {player.player_id}

    if sync_master := _extract_sync_master(status):
        if sync_master != player.player_id:
            related_ids.add(sync_master)

    for provider_player in iter_players():
        if provider_player.player_id == player.player_id:
            continue
        if player.player_id in provider_player.group_members:
            related_ids.add(provider_player.player_id)

    for related_id in related_ids:
        if related_player := get_player(related_id):
            update_player_state(related_player)
