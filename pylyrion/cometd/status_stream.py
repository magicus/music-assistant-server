"""High-level server status stream API with normalized player events."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from pylyrion.cometd.helpers import LmsPlayerEventCallback
from pylyrion.cometd.player_status_events import NormalizedPlayerStatusEvent
from pylyrion.cometd.stream_core import CometDEventStreamCore

PostMessagesCallback = Callable[[list[dict[str, object]], int], Awaitable[list[dict[str, object]]]]


class _CometDStatusRuntime(CometDEventStreamCore):
    """Private transport/runtime implementation for status streaming."""

    def __init__(
        self,
        provider: Any,
        *,
        post_messages: PostMessagesCallback,
        recoverable_errors: tuple[type[Exception], ...],
        events_callback: Callable[[list[NormalizedPlayerStatusEvent]], Awaitable[None]],
    ) -> None:
        """Initialize private runtime wiring."""
        super().__init__(
            provider=provider,
            recoverable_errors=recoverable_errors,
        )
        self._post_messages = post_messages
        self._events_callback = events_callback

    async def _emit_normalized_player_events(
        self,
        events: list[NormalizedPlayerStatusEvent],
    ) -> None:
        """Forward normalized events to the facade callback."""
        await self._events_callback(events)

    async def _post(
        self,
        messages: list[dict[str, object]],
        timeout: int,
    ) -> list[dict[str, object]]:
        """Delegate Bayeux batch POST transport to configured callback."""
        return await self._post_messages(messages, timeout)


class PlayerStatusStream:
    """Public high-level status stream facade with subscribe-based events."""

    def __init__(
        self,
        provider: Any,
        *,
        post_messages: PostMessagesCallback,
        recoverable_errors: tuple[type[Exception], ...],
    ) -> None:
        """
        Initialize high-level status stream.

        :param provider: Provider-like owner object used for status/recovery helpers.
        :param post_messages: Callback used to POST Bayeux messages.
        :param recoverable_errors: Errors that should trigger reconnect/retry behavior.
        """
        self.provider = provider
        self._subscribers: list[LmsPlayerEventCallback] = []
        self._runtime = _CometDStatusRuntime(
            provider=provider,
            post_messages=post_messages,
            recoverable_errors=recoverable_errors,
            events_callback=self._dispatch_normalized_events,
        )

    def start(self) -> None:
        """Start background status stream tasks."""
        self._runtime.start()

    async def stop(self) -> None:
        """Stop background status stream tasks and clean state."""
        await self._runtime.stop()

    def mark_player_seen(self, player_id: str) -> None:
        """Mark a player as present and ensure status subscription."""
        self._runtime.mark_player_seen(player_id)

    def mark_player_removed(self, player_id: str) -> None:
        """Mark a player as removed and clear tracked state."""
        self._runtime.mark_player_removed(player_id)

    def get_last_player_status_seen_at(self, player_id: str) -> float | None:
        """Return timestamp of the latest status update for a player."""
        return self._runtime.get_last_player_status_seen_at(player_id)

    def get_player_status_snapshot(self, player_id: str) -> dict[str, object] | None:
        """Return cached merged status snapshot for a player."""
        return self._runtime.get_player_status_snapshot(player_id)

    async def wait_for_player_status_update(
        self,
        player_id: str,
        since: float | None,
        timeout: float,
    ) -> bool:
        """Wait until player status changes after a baseline timestamp."""
        return await self._runtime.wait_for_player_status_update(
            player_id,
            since,
            timeout,
        )

    async def verify_player_status_expectation(
        self,
        player_id: str,
        baseline: float | None,
        expectation: Callable[[dict[str, object]], bool] | None = None,
        expected_state: str = "status update",
    ) -> bool:
        """Verify expected status with stream-first and polling fallback strategy."""
        return await self._runtime.verify_player_status_expectation(
            player_id,
            baseline,
            expectation,
            expected_state,
        )

    def subscribe(self, callback: LmsPlayerEventCallback) -> Callable[[], None]:
        """Subscribe to normalized player events and return an unsubscribe callback."""
        self._subscribers.append(callback)

        def _unsubscribe() -> None:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

        return _unsubscribe

    async def _dispatch_normalized_events(
        self,
        events: list[NormalizedPlayerStatusEvent],
    ) -> None:
        """Emit normalized player-status events to all subscribers."""
        if not events:
            return
        for event in events:
            for callback in tuple(self._subscribers):
                try:
                    await callback(event)
                except Exception as err:
                    self.provider.logger.warning(
                        "Status event handling failed for %s: %s",
                        getattr(event, "player_id", "unknown"),
                        err,
                    )
