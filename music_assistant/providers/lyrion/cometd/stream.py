"""Lyrion-specific CometD stream built on top of Bayeux primitives."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from typing import TYPE_CHECKING

from aiohttp import ClientError, ClientTimeout
from music_assistant_models.errors import MusicAssistantError, ProviderUnavailableError

from ..bayeux_client import BayeuxClient
from ..client import build_lms_url
from ..constants import (
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
from .helpers import (
    LmsPlayerEventCallback,
    StatusPayload,
    _get_float,
    _get_int,
    _get_mode,
    _get_power,
    _is_invalid_player_payload,
    _same_active_track,
)
from .recovery import _CometDRecoveryMixin
from .status import _CometDStatusMixin

if TYPE_CHECKING:
    from music_assistant.providers.lyrion_player.provider import LyrionPlayerProvider


class LyrionCometDEventStream(_CometDStatusMixin, _CometDRecoveryMixin):
    """Own one CometD session and emit normalized per-player events."""

    provider: LyrionPlayerProvider

    def __init__(
        self,
        provider: LyrionPlayerProvider,
        event_callback: LmsPlayerEventCallback,
    ) -> None:
        """
        Initialize CometD event stream.

        :param provider: Owning Lyrion player provider instance.
        :param event_callback: Callback invoked for each normalized event.
        """
        self.provider = provider
        self._event_callback = event_callback
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
        """Start the background stream task when needed."""
        if self._task is not None and not self._task.done():
            if self._watchdog_task is None or self._watchdog_task.done():
                self._watchdog_task = self.provider.mass.create_task(self._watchdog_loop())
            if self._expectation_task is None or self._expectation_task.done():
                self._expectation_task = self.provider.mass.create_task(self._expectation_loop())
            return
        self._task = self.provider.mass.create_task(self._listener_loop())
        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = self.provider.mass.create_task(self._watchdog_loop())
        if self._expectation_task is None or self._expectation_task.done():
            self._expectation_task = self.provider.mass.create_task(self._expectation_loop())

    async def stop(self) -> None:
        """Stop background task and clear session state."""
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
            with suppress(ProviderUnavailableError):
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

        self.provider.logger.warning(
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
                status = await self.provider.get_player_status(player_id)
            except ProviderUnavailableError as err:
                self.provider.logger.warning(
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

        self.provider.logger.warning(
            "No CometD status confirmation for %s (%s) after %s fallback polls",
            player_id,
            expected_state,
            len(COMETD_COMMAND_STATUS_BACKOFF),
        )
        return False

    async def _listener_loop(self) -> None:
        """Keep one CometD session alive and reconnect on failures."""
        while True:
            if self.provider.unloading:
                return
            try:
                await self._run_session()
            except ProviderUnavailableError as err:
                self.provider.logger.debug(
                    "CometD listener cycle failed: %s",
                    err,
                )

            self._reset_session_state()
            if self.provider.unloading:
                return
            await asyncio.sleep(COMETD_RETRY_DELAY)

    async def _watchdog_loop(self) -> None:
        """Periodically heal stale playerstatus subscriptions."""
        while True:
            if self.provider.unloading:
                return
            await asyncio.sleep(COMETD_STATUS_WATCHDOG_INTERVAL)
            if self.provider.unloading:
                return
            await self._run_watchdog_tick()

    async def _expectation_loop(self) -> None:
        """Monitor implicit state expectations that should resolve without commands."""
        while True:
            if self.provider.unloading:
                return
            await asyncio.sleep(self._next_expectation_delay())
            if self.provider.unloading:
                return
            await self._run_expectation_tick()

    async def _run_session(self) -> None:
        """Run one handshake/connect loop until reconnect is needed."""
        client_id = await self._bayeux.open_session(RPC_TIMEOUT)
        self._client_id = client_id
        self._subscribed_player_ids.clear()
        self._known_server_player_ids = None
        self._known_server_player_count = None

        for player in self.provider.players:
            self._pending_player_ids.add(player.player_id)

        await self._subscribe_server_status()

        await self._bayeux.run_connect_loop(
            client_id=client_id,
            connect_timeout=COMETD_CONNECT_TIMEOUT,
            should_stop=lambda: self.provider.unloading,
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
            except ProviderUnavailableError:
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
        """Merge one playerstatus payload, compute diffs, and emit events."""
        from music_assistant.providers.lyrion_player.cometd_events import (
            LmsPlayerPlaybackChangedEvent,
            LmsPlayerPlaylistChangedEvent,
            LmsPlayerPowerChangedEvent,
            LmsPlayerRepeatChangedEvent,
            LmsPlayerSeekedEvent,
            LmsPlayerShuffleChangedEvent,
            LmsPlayerStatusUpdatedEvent,
            LmsPlayerVolumeChangedEvent,
        )

        if _is_invalid_player_payload(partial):
            self._status_by_player.pop(player_id, None)
            self.mark_player_removed(player_id)
            await self._emit_event(
                LmsPlayerStatusUpdatedEvent(
                    player_id=player_id,
                    status=dict(partial),
                    is_initial=False,
                )
            )
            self.provider.schedule_players_discovery()
            return

        previous = self._status_by_player.get(player_id)
        merged = dict(previous or {})
        merged.update(partial)
        self._status_by_player[player_id] = merged
        self._touch_player_status_activity(player_id)
        self._update_track_end_expectation(player_id, merged)

        is_initial = previous is None
        await self._emit_event(
            LmsPlayerStatusUpdatedEvent(
                player_id=player_id,
                status=dict(merged),
                is_initial=is_initial,
            )
        )
        if is_initial:
            return

        assert previous is not None

        if (old_mode := _get_mode(previous)) != (new_mode := _get_mode(merged)):
            await self._emit_event(
                LmsPlayerPlaybackChangedEvent(
                    player_id=player_id,
                    old_mode=old_mode,
                    new_mode=new_mode,
                )
            )

        old_power = _get_power(previous)
        new_power = _get_power(merged)
        if old_power is not None and new_power is not None and old_power != new_power:
            await self._emit_event(
                LmsPlayerPowerChangedEvent(
                    player_id=player_id,
                    old_powered=old_power,
                    new_powered=new_power,
                )
            )

        old_volume = _get_int(previous, "mixer volume")
        new_volume = _get_int(merged, "mixer volume")
        if old_volume is not None and new_volume is not None and old_volume != new_volume:
            await self._emit_event(
                LmsPlayerVolumeChangedEvent(
                    player_id=player_id,
                    old_volume=old_volume,
                    new_volume=new_volume,
                )
            )

        old_repeat = _get_int(previous, "playlist repeat")
        new_repeat = _get_int(merged, "playlist repeat")
        if old_repeat is not None and new_repeat is not None and old_repeat != new_repeat:
            await self._emit_event(
                LmsPlayerRepeatChangedEvent(
                    player_id=player_id,
                    old_repeat=old_repeat,
                    new_repeat=new_repeat,
                )
            )

        old_shuffle = _get_int(previous, "playlist shuffle")
        new_shuffle = _get_int(merged, "playlist shuffle")
        if old_shuffle is not None and new_shuffle is not None and old_shuffle != new_shuffle:
            await self._emit_event(
                LmsPlayerShuffleChangedEvent(
                    player_id=player_id,
                    old_shuffle=old_shuffle,
                    new_shuffle=new_shuffle,
                )
            )

        old_time = _get_float(previous, "time")
        new_time = _get_float(merged, "time")
        if old_time is not None and new_time is not None and old_time != new_time:
            if _same_active_track(previous, merged):
                await self._emit_event(
                    LmsPlayerSeekedEvent(
                        player_id=player_id,
                        old_time=old_time,
                        new_time=new_time,
                    )
                )

        old_index = _get_int(previous, "playlist_cur_index")
        new_index = _get_int(merged, "playlist_cur_index")
        old_timestamp = _get_float(previous, "playlist_timestamp")
        new_timestamp = _get_float(merged, "playlist_timestamp")
        old_tracks = _get_int(previous, "playlist_tracks")
        new_tracks = _get_int(merged, "playlist_tracks")
        if old_index != new_index or old_timestamp != new_timestamp or old_tracks != new_tracks:
            await self._emit_event(
                LmsPlayerPlaylistChangedEvent(
                    player_id=player_id,
                    old_playlist_timestamp=old_timestamp,
                    new_playlist_timestamp=new_timestamp,
                    old_playlist_tracks=old_tracks,
                    new_playlist_tracks=new_tracks,
                )
            )

    async def _emit_event(self, event: object) -> None:
        """Emit one event without terminating connect loop on MA errors."""
        try:
            await self._event_callback(event)
        except MusicAssistantError as err:
            self.provider.logger.warning(
                "CometD event handling failed for %s: %s",
                getattr(event, "player_id", "unknown"),
                err,
            )

    async def _post(
        self,
        messages: list[dict[str, object]],
        timeout: int,
    ) -> list[dict[str, object]]:
        """POST Bayeux messages and return normalized dict payloads."""
        host = self.provider.get_configured_host()
        if not host:
            raise ProviderUnavailableError("Lyrion host is not configured")
        port = self.provider.get_configured_port()
        if port is None:
            raise ProviderUnavailableError("Lyrion port is not configured")

        url = build_lms_url(host, port, "/cometd")
        try:
            async with self.provider.mass.http_session.post(
                url,
                json=messages,
                timeout=ClientTimeout(total=timeout),
            ) as response:
                response.raise_for_status()
                payload = await response.json()
        except (ClientError, TimeoutError, ValueError) as err:
            raise ProviderUnavailableError(
                f"CometD request to {host}:{port} failed: {err}"
            ) from err

        if isinstance(payload, dict):
            return [payload]
        if not isinstance(payload, list):
            raise ProviderUnavailableError("CometD response must be a JSON object or list")
        return [message for message in payload if isinstance(message, dict)]
