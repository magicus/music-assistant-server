"""Core CometD stream runtime independent from MA event classes."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from contextlib import suppress
from logging import Logger
from typing import Any

from pylyrion.cometd.bayeux_client import BayeuxClient
from pylyrion.cometd.constants import (
    COMETD_COMMAND_STATUS_BACKOFF,
    COMETD_CONNECT_TIMEOUT,
    COMETD_PLAYERSTATUS_SUBSCRIBE_INTERVAL,
    COMETD_PLAYERSTATUS_TAGS,
    COMETD_RETRY_DELAY,
    COMETD_SERVERSTATUS_BATCH_SIZE,
    COMETD_SERVERSTATUS_SUBSCRIBE_INTERVAL,
    COMETD_STATUS_WATCHDOG_INTERVAL,
    RPC_TIMEOUT,
)
from pylyrion.cometd.helpers import StatusPayload
from pylyrion.cometd.player_status_events import NormalizedPlayerStatusEvent, merge_player_status
from pylyrion.cometd.recovery import _CometDRecoveryMixin
from pylyrion.cometd.status import _CometDStatusMixin


class CometDEventStreamCore(_CometDStatusMixin, _CometDRecoveryMixin):
    """Run one CometD session and emit normalized player-status transitions."""

    def __init__(
        self,
        recoverable_errors: tuple[type[Exception], ...],
        should_stop: Callable[[], bool],
        logger: Logger,
        get_player_status: Callable[[str], Awaitable[StatusPayload]],
        get_initial_player_ids: Callable[[], set[str]],
        get_current_player_ids: Callable[[], set[str]],
        schedule_players_discovery: Callable[[], None],
        apply_server_player_connection_state: Callable[[dict[str, object]], None],
    ) -> None:
        """
        Initialize CometD stream core.

        :param recoverable_errors: Errors that should trigger reconnect/retry behavior.
        :param should_stop: Callback returning True when runtime should stop.
        :param logger: Logger used by CometD runtime internals.
        :param get_player_status: Fetch one player's JSON-RPC status snapshot.
        :param get_initial_player_ids: Return player ids to subscribe at session start.
        :param get_current_player_ids: Return currently tracked runtime player ids.
        :param schedule_players_discovery: Schedule runtime player rediscovery.
        :param apply_server_player_connection_state: Apply transport-level connection hints.
        """
        self._recoverable_errors = recoverable_errors
        self._should_stop = should_stop
        self._logger = logger
        self._get_player_status = get_player_status
        self._get_initial_player_ids = get_initial_player_ids
        self._get_current_player_ids = get_current_player_ids
        self._schedule_players_discovery = schedule_players_discovery
        self._apply_server_player_connection_state = apply_server_player_connection_state
        self._bayeux = BayeuxClient(self._post)
        self._task: asyncio.Task[None] | None = None
        self._watchdog_task: asyncio.Task[None] | None = None
        self._client_id: str | None = None
        self._subscribed_player_ids: set[str] = set()
        self._pending_player_ids: set[str] = set()
        self._status_by_player: dict[str, StatusPayload] = {}
        self._status_seen_at: dict[str, float] = {}
        self._status_wait_events: dict[str, asyncio.Event] = {}
        self._expectation_task: asyncio.Task[None] | None = None
        self._track_end_expectations: dict[str, object] = {}
        self._expectation_recovery_inflight: set[str] = set()
        self._known_server_player_ids: set[str] | None = None
        self._known_server_player_count: int | None = None

    def start(self) -> None:
        """Start background stream/watchdog/expectation tasks."""
        if self._task is not None and not self._task.done():
            if self._watchdog_task is None or self._watchdog_task.done():
                self._watchdog_task = self._create_background_task(
                    self._watchdog_loop(),
                    task_name="pylyrion-cometd-watchdog",
                )
            if self._expectation_task is None or self._expectation_task.done():
                self._expectation_task = self._create_background_task(
                    self._expectation_loop(),
                    task_name="pylyrion-cometd-expectation",
                )
            return
        self._task = self._create_background_task(
            self._listener_loop(),
            task_name="pylyrion-cometd-listener",
        )
        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = self._create_background_task(
                self._watchdog_loop(),
                task_name="pylyrion-cometd-watchdog",
            )
        if self._expectation_task is None or self._expectation_task.done():
            self._expectation_task = self._create_background_task(
                self._expectation_loop(),
                task_name="pylyrion-cometd-expectation",
            )

    def _create_background_task(
        self,
        target: Coroutine[Any, Any, None],
        task_name: str,
    ) -> asyncio.Task[None]:
        """Create one internal background task and surface failures in logs."""
        task = asyncio.create_task(target, name=task_name)
        task.add_done_callback(self._handle_background_task_done)
        return task

    def _handle_background_task_done(self, task: asyncio.Task[None]) -> None:
        """Log unexpected background task failures."""
        if task.cancelled():
            return
        if err := task.exception():
            self._logger.warning(
                "Exception in task %s: %s",
                task.get_name(),
                err,
            )

    async def stop(self) -> None:
        """Stop background tasks, disconnect best-effort and clear session state."""
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._watchdog_task
            self._watchdog_task = None

        if self._expectation_task is not None:
            self._expectation_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._expectation_task
            self._expectation_task = None

        if self._client_id is not None:
            with suppress(*self._recoverable_errors):
                await self._bayeux.disconnect(self._client_id, RPC_TIMEOUT)

        self._reset_session_state()

    async def verify_player_status_expectation(
        self,
        player_id: str,
        baseline: float | None,
        expectation: Callable[[StatusPayload], bool] | None = None,
        expected_state: str = "status update",
    ) -> bool:
        """
        Verify expected status via CometD first, then fallback polling with backoff.

        :param player_id: LMS player id.
        :param baseline: Baseline status timestamp before expectation started.
        :param expectation: Optional predicate for expected status shape.
        :param expected_state: Human-readable expected-state description for logging.
        :returns: True when expectation was satisfied.
        """

        def _is_satisfied(status: StatusPayload | None) -> bool:
            if status is None:
                return False
            if expectation is None:
                return True
            return expectation(status)

        if await self.wait_for_player_status_update(
            player_id,
            baseline,
            COMETD_COMMAND_STATUS_BACKOFF[0],
        ):
            if _is_satisfied(self.get_player_status_snapshot(player_id)):
                return True

        self._logger.warning(
            "No CometD confirmation for %s (%s) within %ss; polling LMS up to %s times",
            player_id,
            expected_state,
            COMETD_COMMAND_STATUS_BACKOFF[0],
            len(COMETD_COMMAND_STATUS_BACKOFF),
        )

        rolling_baseline = baseline
        for attempt, wait_seconds in enumerate(COMETD_COMMAND_STATUS_BACKOFF):
            if await self.wait_for_player_status_update(
                player_id,
                rolling_baseline,
                wait_seconds,
            ):
                if _is_satisfied(self.get_player_status_snapshot(player_id)):
                    return True
                rolling_baseline = self.get_last_player_status_seen_at(player_id)

            try:
                status = await self._get_player_status(player_id)
            except self._recoverable_errors as err:
                self._logger.warning(
                    "Fallback status poll %s/%s failed for %s (%s): %s",
                    attempt + 1,
                    len(COMETD_COMMAND_STATUS_BACKOFF),
                    player_id,
                    expected_state,
                    err,
                )
                continue

            await self._handle_player_status(player_id, status)
            rolling_baseline = self.get_last_player_status_seen_at(player_id)
            if _is_satisfied(self.get_player_status_snapshot(player_id)):
                return True

        self._logger.warning(
            "No CometD status confirmation for %s (%s) after %s fallback polls",
            player_id,
            expected_state,
            len(COMETD_COMMAND_STATUS_BACKOFF),
        )
        return False

    async def _listener_loop(self) -> None:
        """Keep one CometD session alive and reconnect on failures."""
        while True:
            if self._should_stop():
                return
            try:
                await self._run_session()
            except self._recoverable_errors as err:
                self._logger.debug("CometD listener cycle failed: %s", err)

            self._reset_session_state()
            if self._should_stop():
                return
            await asyncio.sleep(COMETD_RETRY_DELAY)

    async def _watchdog_loop(self) -> None:
        """Periodically heal stale playerstatus subscriptions."""
        while True:
            if self._should_stop():
                return
            await asyncio.sleep(COMETD_STATUS_WATCHDOG_INTERVAL)
            if self._should_stop():
                return
            await self._run_watchdog_tick()

    async def _expectation_loop(self) -> None:
        """Monitor implicit state expectations that should resolve without commands."""
        while True:
            if self._should_stop():
                return
            await asyncio.sleep(self._next_expectation_delay())
            if self._should_stop():
                return
            await self._run_expectation_tick()

    async def _run_session(self) -> None:
        """Run one handshake/connect loop until reconnect is needed."""
        client_id = await self._bayeux.open_session(RPC_TIMEOUT)
        self._client_id = client_id
        self._subscribed_player_ids.clear()
        self._known_server_player_ids = None
        self._known_server_player_count = None

        self._pending_player_ids.update(self._get_initial_player_ids())

        await self._subscribe_server_status()

        await self._bayeux.run_connect_loop(
            client_id=client_id,
            connect_timeout=COMETD_CONNECT_TIMEOUT,
            should_stop=self._should_stop,
            message_handler=self._handle_message,
            pre_connect_hook=self._flush_pending_player_subscriptions,
        )

    async def _flush_pending_player_subscriptions(self) -> None:
        """Subscribe playerstatus streams for pending players."""
        if self._client_id is None or not self._pending_player_ids:
            return

        await self._refresh_stale_player_subscriptions()

        pending = sorted(self._pending_player_ids)
        self._pending_player_ids.clear()
        failed: list[str] = []
        for player_id in pending:
            if player_id in self._subscribed_player_ids:
                continue
            try:
                await self._subscribe_player_status(player_id)
            except self._recoverable_errors:
                failed.append(player_id)

        for player_id in failed:
            self._pending_player_ids.add(player_id)

    async def _subscribe_player_status(self, player_id: str) -> None:
        """Subscribe to status updates for one LMS player."""
        if self._client_id is None:
            return

        response_channel = f"/{self._client_id}/slim/playerstatus/{player_id}"
        request = [
            player_id,
            [
                "status",
                "-",
                1,
                COMETD_PLAYERSTATUS_TAGS,
                f"subscribe:{COMETD_PLAYERSTATUS_SUBSCRIBE_INTERVAL}",
                "alarmData:1",
            ],
        ]
        response = await self._bayeux.publish(
            channel="/slim/subscribe",
            client_id=self._client_id,
            data={
                "response": response_channel,
                "request": request,
            },
            timeout=RPC_TIMEOUT,
        )

        self._subscribed_player_ids.add(player_id)
        self._touch_player_status_activity(player_id)
        for message in response[1:]:
            await self._handle_message(message)

    async def _subscribe_server_status(self) -> None:
        """Subscribe to serverstatus updates to detect player roster changes."""
        if self._client_id is None:
            return

        response_channel = f"/{self._client_id}/slim/serverstatus"
        request = [
            "",
            [
                "serverstatus",
                0,
                COMETD_SERVERSTATUS_BATCH_SIZE,
                f"subscribe:{COMETD_SERVERSTATUS_SUBSCRIBE_INTERVAL}",
            ],
        ]
        response = await self._bayeux.publish(
            channel="/slim/subscribe",
            client_id=self._client_id,
            data={
                "response": response_channel,
                "request": request,
            },
            timeout=RPC_TIMEOUT,
        )

        for message in response[1:]:
            await self._handle_message(message)

    async def _handle_message(self, message: dict[str, object]) -> None:
        """Handle one normalized CometD message."""
        channel = message.get("channel")
        if not isinstance(channel, str):
            return

        if "/slim/playerstatus/" in channel:
            player_id = channel.rsplit("/", 1)[-1]
            data = message.get("data")
            if isinstance(data, dict):
                await self._handle_player_status(player_id, data)
            return

        if channel.endswith("/slim/serverstatus"):
            data = message.get("data")
            if isinstance(data, dict):
                self._handle_server_status(data)
            return

    async def _handle_player_status(
        self,
        player_id: str,
        partial: StatusPayload,
    ) -> None:
        """Merge one playerstatus payload and emit normalized transitions."""
        previous = self._status_by_player.get(player_id)
        merged, events, invalid_player = merge_player_status(player_id, previous, partial)
        if invalid_player:
            self._status_by_player.pop(player_id, None)
            self.mark_player_removed(player_id)
            await self._emit_normalized_player_events(events)
            self._schedule_players_discovery()
            return

        assert merged is not None
        self._status_by_player[player_id] = merged
        self._touch_player_status_activity(player_id)
        self._update_track_end_expectation(player_id, merged)
        await self._emit_normalized_player_events(events)

    async def _emit_normalized_player_events(
        self,
        events: list[NormalizedPlayerStatusEvent],
    ) -> None:
        """Emit normalized player-status events through implementation callback."""
        raise NotImplementedError

    async def _post(
        self,
        messages: list[dict[str, object]],
        timeout: int,
    ) -> list[dict[str, object]]:
        """Post one Bayeux batch request and return normalized response payload."""
        raise NotImplementedError
