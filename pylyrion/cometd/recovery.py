"""Expectation and staleness recovery logic for Lyrion CometD."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Coroutine
from contextlib import suppress
from logging import Logger

from .constants import (
    COMETD_ACTIVE_STATE_TIMEOUT,
    COMETD_EXPECTATION_LOOP_IDLE_INTERVAL,
    COMETD_IMPLICIT_STATUS_BACKOFF,
    COMETD_PLAYERSTATUS_SUBSCRIBE_INTERVAL,
    COMETD_STATUS_STALENESS_FACTOR,
    COMETD_TRACK_END_GRACE,
)
from .helpers import (
    StatusPayload,
    _extract_current_track_id,
    _get_float,
    _get_int,
    _get_mode,
    _TrackEndExpectation,
)


class _CometDRecoveryMixin:
    """Mixin with implicit expectation and stale-session recovery."""

    _task: asyncio.Task[None] | None
    _watchdog_task: asyncio.Task[None] | None
    _client_id: str | None
    _subscribed_player_ids: set[str]
    _pending_player_ids: set[str]
    _status_by_player: dict[str, StatusPayload]
    _status_seen_at: dict[str, float]
    _track_end_expectations: dict[str, object]
    _expectation_recovery_inflight: set[str]
    _logger: Logger

    def get_last_player_status_seen_at(self, player_id: str) -> float | None: ...

    async def verify_player_status_expectation(
        self,
        player_id: str,
        baseline: float | None,
        expectation: object = None,
        expected_state: str = "status update",
    ) -> bool: ...

    def _touch_player_status_activity(self, player_id: str) -> None: ...

    def _reset_session_state(self) -> None: ...

    async def _listener_loop(self) -> None: ...

    async def _watchdog_loop(self) -> None: ...

    def _create_background_task(
        self,
        target: Coroutine[Any, Any, None],
        task_name: str,
    ) -> asyncio.Task[None]: ...

    def _next_expectation_delay(self) -> float:
        """Compute next loop delay using inverse backoff near track end."""
        if not self._track_end_expectations:
            return COMETD_EXPECTATION_LOOP_IDLE_INTERVAL

        now = time.monotonic()
        nearest_delay = COMETD_EXPECTATION_LOOP_IDLE_INTERVAL
        for expectation in self._track_end_expectations.values():
            remaining = expectation.expected_transition_at - now
            if remaining <= COMETD_TRACK_END_GRACE:
                return COMETD_IMPLICIT_STATUS_BACKOFF[-1]

            next_delay = COMETD_EXPECTATION_LOOP_IDLE_INTERVAL
            for backoff in COMETD_IMPLICIT_STATUS_BACKOFF:
                if remaining > backoff:
                    next_delay = min(next_delay, remaining - backoff)
                    break
            nearest_delay = min(nearest_delay, next_delay)
        return max(COMETD_IMPLICIT_STATUS_BACKOFF[-1], nearest_delay)

    async def _run_expectation_tick(self) -> None:
        """Evaluate non-command expectations and recover when they are missed."""
        now = time.monotonic()

        for player_id, status in list(self._status_by_player.items()):
            if player_id in self._expectation_recovery_inflight:
                continue

            if self._requires_activity_expectation(status):
                status_age = now - self._status_seen_at.get(player_id, now)
                if status_age > COMETD_ACTIVE_STATE_TIMEOUT:
                    await self._recover_expected_status(
                        player_id,
                        reason="active-state timeout",
                    )
                    continue

            expectation_obj = self._track_end_expectations.get(player_id)
            if expectation_obj is None:
                continue
            expectation = expectation_obj
            if not isinstance(expectation, _TrackEndExpectation):
                continue
            if self._track_transition_satisfied(player_id, expectation):
                self._track_end_expectations.pop(player_id, None)
                continue
            if now + COMETD_TRACK_END_GRACE < expectation.expected_transition_at:
                continue

            await self._recover_expected_status(
                player_id,
                reason="track-end transition",
            )

    async def _recover_expected_status(
        self,
        player_id: str,
        reason: str,
    ) -> None:
        """Poll JSON-RPC with backoff until the expected state recovers."""
        if player_id in self._expectation_recovery_inflight:
            return
        self._expectation_recovery_inflight.add(player_id)
        try:
            baseline = self.get_last_player_status_seen_at(player_id)

            def _expectation(_status: StatusPayload) -> bool:
                expectation_obj = self._track_end_expectations.get(player_id)
                if not isinstance(expectation_obj, _TrackEndExpectation):
                    return True
                return self._track_transition_satisfied(player_id, expectation_obj)

            await self.verify_player_status_expectation(
                player_id,
                baseline,
                expectation=_expectation,
                expected_state=reason,
            )
        finally:
            self._expectation_recovery_inflight.discard(player_id)

    async def _run_watchdog_tick(self) -> None:
        """Check whether CometD status has gone stale and recover it."""
        if self._client_id is None:
            return

        stale_after = COMETD_PLAYERSTATUS_SUBSCRIBE_INTERVAL * COMETD_STATUS_STALENESS_FACTOR
        now = time.monotonic()
        stale_player_ids = [
            player_id
            for player_id in sorted(self._subscribed_player_ids)
            if now - self._status_seen_at.get(player_id, 0.0) > stale_after
        ]
        if not stale_player_ids:
            return

        self._logger.warning(
            "CometD playerstatus stale for %s",
            ", ".join(stale_player_ids),
        )
        if len(stale_player_ids) == len(self._subscribed_player_ids):
            await self._restart_stale_session(stale_player_ids)
            return

        for player_id in stale_player_ids:
            self._subscribed_player_ids.discard(player_id)
            self._status_by_player.pop(player_id, None)
            self._pending_player_ids.add(player_id)
            self._touch_player_status_activity(player_id)

    async def _restart_stale_session(
        self,
        stale_player_ids: list[str],
    ) -> None:
        """Restart the CometD session when the whole status stream looks dead."""
        client_id = self._client_id
        if client_id is None:
            return

        self._logger.warning(
            "Restarting CometD session after stale playerstatus for %s",
            ", ".join(stale_player_ids),
        )
        self._reset_session_state()
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        self._task = self._create_background_task(
            self._listener_loop(),
            task_name="pylyrion-cometd-listener",
        )
        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = self._create_background_task(
                self._watchdog_loop(),
                task_name="pylyrion-cometd-watchdog",
            )

    async def _refresh_stale_player_subscriptions(
        self,
    ) -> None:
        """Resubscribe players whose status has gone stale."""
        stale_after = COMETD_PLAYERSTATUS_SUBSCRIBE_INTERVAL * COMETD_STATUS_STALENESS_FACTOR
        now = time.monotonic()
        stale_player_ids = [
            player_id
            for player_id in sorted(self._subscribed_player_ids)
            if now - self._status_seen_at.get(player_id, 0.0) > stale_after
        ]
        if not stale_player_ids:
            return

        self._logger.warning(
            "CometD playerstatus went stale for %s; resubscribing",
            ", ".join(stale_player_ids),
        )
        for player_id in stale_player_ids:
            self._subscribed_player_ids.discard(player_id)
            self._status_by_player.pop(player_id, None)
            self._pending_player_ids.add(player_id)
            self._touch_player_status_activity(player_id)

    def _update_track_end_expectation(
        self,
        player_id: str,
        status: StatusPayload,
    ) -> None:
        """Arm or clear track-end expectation from runtime playback status."""
        if _get_mode(status) != "play":
            self._track_end_expectations.pop(player_id, None)
            return

        elapsed = _get_float(status, "time")
        duration = _get_float(status, "duration")
        if elapsed is None or duration is None or duration <= 0:
            self._track_end_expectations.pop(player_id, None)
            return

        remaining = max(0.0, duration - elapsed)
        self._track_end_expectations[player_id] = _TrackEndExpectation(
            expected_transition_at=time.monotonic() + remaining,
            baseline_index=_get_int(status, "playlist_cur_index"),
            baseline_track_id=_extract_current_track_id(status),
            baseline_playlist_timestamp=_get_float(status, "playlist_timestamp"),
        )

    def _track_transition_satisfied(
        self,
        player_id: str,
        expectation: _TrackEndExpectation,
    ) -> bool:
        """Return True if playback moved off expected track state."""
        status = self._status_by_player.get(player_id)
        if status is None:
            return True
        if _get_mode(status) != "play":
            return True

        current_index = _get_int(status, "playlist_cur_index")
        if expectation.baseline_index is not None and current_index != expectation.baseline_index:
            return True

        current_track_id = _extract_current_track_id(status)
        if expectation.baseline_track_id and current_track_id != expectation.baseline_track_id:
            return True

        current_timestamp = _get_float(status, "playlist_timestamp")
        return (
            expectation.baseline_playlist_timestamp is not None
            and current_timestamp is not None
            and current_timestamp != expectation.baseline_playlist_timestamp
        )

    @staticmethod
    def _requires_activity_expectation(status: StatusPayload) -> bool:
        """Return True if runtime state should keep producing fresh updates."""
        if _get_mode(status) in ("play", "pause"):
            return True
        sync_master = status.get("sync_master")
        if isinstance(sync_master, str) and sync_master.strip():
            return True
        sync_slaves = status.get("sync_slaves")
        if isinstance(sync_slaves, str) and sync_slaves.strip():
            return True
        if _get_int(status, "playlist_tracks") not in (None, 0):
            return True
        return bool(
            _get_int(status, "playlist_cur_index") not in (None, -1)
            and _extract_current_track_id(status)
        )
