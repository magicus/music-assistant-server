"""Status cache and server-roster handling for Lyrion CometD."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from .helpers import StatusPayload, _extract_server_player_ids, _get_int

if TYPE_CHECKING:
    from music_assistant.providers.lyrion_player.provider import LyrionPlayerProvider


class _CometDStatusMixin:
    """Mixin with status cache lifecycle and wait primitives."""

    provider: LyrionPlayerProvider
    _pending_player_ids: set[str]
    _subscribed_player_ids: set[str]
    _status_by_player: dict[str, StatusPayload]
    _status_seen_at: dict[str, float]
    _status_wait_events: dict[str, asyncio.Event]
    _track_end_expectations: dict[str, object]
    _expectation_recovery_inflight: set[str]
    _known_server_player_ids: set[str] | None
    _known_server_player_count: int | None
    _client_id: str | None

    def mark_player_seen(self, player_id: str) -> None:
        """
        Mark player as present so status subscription can be scheduled.

        :param player_id: LMS player id.
        """
        self._pending_player_ids.add(player_id)
        self._touch_player_status_activity(player_id)

    def mark_player_removed(self, player_id: str) -> None:
        """
        Remove cached state for a removed player.

        :param player_id: LMS player id.
        """
        self._subscribed_player_ids.discard(player_id)
        self._pending_player_ids.discard(player_id)
        self._status_by_player.pop(player_id, None)
        self._status_seen_at.pop(player_id, None)
        self._status_wait_events.pop(player_id, None)
        self._track_end_expectations.pop(player_id, None)
        self._expectation_recovery_inflight.discard(player_id)

    def get_last_player_status_seen_at(
        self,
        player_id: str,
    ) -> float | None:
        """Return the last time CometD updated one player's status."""
        return self._status_seen_at.get(player_id)

    def get_player_status_snapshot(
        self,
        player_id: str,
    ) -> StatusPayload | None:
        """Return a shallow copy of the latest merged player status."""
        status = self._status_by_player.get(player_id)
        if status is None:
            return None
        return dict(status)

    async def wait_for_player_status_update(
        self,
        player_id: str,
        since: float | None,
        timeout: float,
    ) -> bool:
        """
        Wait for a newer CometD status update for one player.

        :param player_id: LMS player id.
        :param since: Baseline timestamp to compare against.
        :param timeout: Maximum wait time in seconds.
        :return: True when a newer status arrived in time.
        """
        baseline = since or 0.0
        if self._status_seen_at.get(player_id, 0.0) > baseline:
            return True

        event = self._status_wait_events.setdefault(player_id, asyncio.Event())
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self._status_seen_at.get(player_id, 0.0) > baseline
            try:
                await asyncio.wait_for(event.wait(), timeout=remaining)
            except TimeoutError:
                return self._status_seen_at.get(player_id, 0.0) > baseline
            event.clear()
            if self._status_seen_at.get(player_id, 0.0) > baseline:
                return True

    def _reset_session_state(self) -> None:
        """Clear volatile CometD session state after reconnect or stop."""
        self._client_id = None
        self._subscribed_player_ids.clear()
        self._pending_player_ids.clear()
        self._status_by_player.clear()
        self._status_seen_at.clear()
        self._status_wait_events.clear()
        self._track_end_expectations.clear()
        self._expectation_recovery_inflight.clear()
        self._known_server_player_ids = None
        self._known_server_player_count = None

    def _touch_player_status_activity(
        self,
        player_id: str,
    ) -> None:
        """Record fresh activity for one player and wake status waiters."""
        self._status_seen_at[player_id] = time.monotonic()
        if event := self._status_wait_events.get(player_id):
            event.set()

    def _handle_server_status(
        self,
        payload: dict[str, object],
    ) -> None:
        """Detect server roster changes and trigger provider rediscovery."""
        self._apply_server_player_connection_state(payload)

        player_ids = _extract_server_player_ids(payload)
        if player_ids:
            current_player_ids = {player.player_id for player in self.provider.players}
            if self._known_server_player_ids is None:
                self._known_server_player_ids = player_ids
                self._known_server_player_count = len(player_ids)
                if player_ids != current_player_ids:
                    self.provider.schedule_players_discovery()
                return

            if player_ids != self._known_server_player_ids:
                self._known_server_player_ids = player_ids
                self._known_server_player_count = len(player_ids)
                self.provider.schedule_players_discovery()
            return

        player_count = _get_int(payload, "player count")
        if player_count is None:
            return

        if self._known_server_player_count is None:
            self._known_server_player_count = player_count
            if player_count != len(self.provider.players):
                self.provider.schedule_players_discovery()
            return

        if player_count != self._known_server_player_count:
            self._known_server_player_count = player_count
            self.provider.schedule_players_discovery()

    def _apply_server_player_connection_state(
        self,
        payload: dict[str, object],
    ) -> None:
        """Apply connected flags from serverstatus to MA player availability."""
        players_loop = payload.get("players_loop")
        if not isinstance(players_loop, list):
            return

        for player_data in players_loop:
            if not isinstance(player_data, dict):
                continue

            raw_player_id = player_data.get("playerid")
            if not raw_player_id:
                continue
            player_id = str(raw_player_id)

            connected = _get_int(player_data, "connected")
            if connected is None:
                continue

            player = self.provider.mass.players.get_player(player_id)
            if player is None:
                continue

            player_provider = getattr(player, "provider", None)
            if (
                player_provider is not None
                and getattr(player_provider, "instance_id", None) != self.provider.instance_id
            ):
                continue

            if not hasattr(player, "_attr_available") or not hasattr(
                player,
                "update_state",
            ):
                continue

            available = bool(connected)
            if getattr(player, "_attr_available", None) == available:
                continue

            player._attr_available = available
            player.update_state()
