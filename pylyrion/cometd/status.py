"""Status cache and server-roster handling for Lyrion CometD."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from .helpers import StatusPayload, _extract_server_player_ids, _get_int


class _CometDStatusMixin:
    """Mixin with status cache lifecycle and wait primitives."""

    _get_current_player_ids: Callable[[], set[str]]
    _schedule_players_discovery: Callable[[], None]
    _apply_server_player_connection_state: Callable[[dict[str, object]], None]
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
        """Detect server roster changes and trigger rediscovery callback."""
        self._apply_server_player_connection_state(payload)
        current_player_ids = self._get_current_player_ids()

        player_ids = _extract_server_player_ids(payload)
        if player_ids:
            if self._known_server_player_ids is None:
                self._known_server_player_ids = player_ids
                self._known_server_player_count = len(player_ids)
                if player_ids != current_player_ids:
                    self._schedule_players_discovery()
                return

            if player_ids != self._known_server_player_ids:
                self._known_server_player_ids = player_ids
                self._known_server_player_count = len(player_ids)
                self._schedule_players_discovery()
            return

        player_count = _get_int(payload, "player count")
        if player_count is None:
            return

        if self._known_server_player_count is None:
            self._known_server_player_count = player_count
            if player_count != len(current_player_ids):
                self._schedule_players_discovery()
            return

        if player_count != self._known_server_player_count:
            self._known_server_player_count = player_count
            self._schedule_players_discovery()
