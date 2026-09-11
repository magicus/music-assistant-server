"""Lyrion-specific CometD stream built on top of Bayeux primitives."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import TYPE_CHECKING, Any, cast

from aiohttp import ClientError, ClientTimeout
from music_assistant_models.errors import ProviderUnavailableError

from .bayeux_client import BayeuxClient
from .constants import (
    COMETD_CONNECT_TIMEOUT,
    COMETD_PLAYERSTATUS_SUBSCRIBE_INTERVAL,
    COMETD_PLAYERSTATUS_TAGS,
    COMETD_RETRY_DELAY,
    COMETD_SERVERSTATUS_BATCH_SIZE,
    COMETD_SERVERSTATUS_SUBSCRIBE_INTERVAL,
    COMETD_STATUS_STALENESS_FACTOR,
    COMETD_STATUS_WATCHDOG_INTERVAL,
    RPC_TIMEOUT,
)

if TYPE_CHECKING:
    from .provider import LyrionPlayerProvider

StatusPayload = dict[str, Any]
LmsPlayerEventCallback = Callable[[Any], Awaitable[None]]


class LyrionCometDEventStream:
    """Own one CometD session and emit normalized per-player events."""

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
        self._known_server_player_ids: set[str] | None = None
        self._known_server_player_count: int | None = None

    def start(self) -> None:
        """Start the background stream task when needed."""
        if self._task is not None and not self._task.done():
            if self._watchdog_task is None or self._watchdog_task.done():
                self._watchdog_task = self.provider.mass.create_task(self._watchdog_loop())
            return
        self._task = self.provider.mass.create_task(self._listener_loop())
        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = self.provider.mass.create_task(self._watchdog_loop())

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

        if self._client_id is not None:
            with suppress(ProviderUnavailableError):
                await self._bayeux.disconnect(self._client_id, RPC_TIMEOUT)

        self._reset_session_state()

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

    def get_last_player_status_seen_at(self, player_id: str) -> float | None:
        """Return the last time CometD updated one player's status."""
        return self._status_seen_at.get(player_id)

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

    async def _listener_loop(self) -> None:
        """Keep one CometD session alive and reconnect on failures."""
        while not self.provider.unloading:
            try:
                await self._run_session()
            except ProviderUnavailableError as err:
                self.provider.logger.debug(
                    "CometD listener cycle failed: %s",
                    err,
                )

            self._reset_session_state()
            if self.provider.unloading:
                return  # type: ignore[unreachable]
            await asyncio.sleep(COMETD_RETRY_DELAY)

    async def _watchdog_loop(self) -> None:
        """Periodically heal stale playerstatus subscriptions."""
        while not self.provider.unloading:
            await asyncio.sleep(COMETD_STATUS_WATCHDOG_INTERVAL)
            if self.provider.unloading:
                return
            await self._run_watchdog_tick()

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

        self.provider.logger.warning(
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

    async def _restart_stale_session(self, stale_player_ids: list[str]) -> None:
        """Restart the CometD session when the whole status stream looks dead."""
        client_id = self._client_id
        if client_id is None:
            return

        self.provider.logger.warning(
            "Restarting CometD session after stale playerstatus for %s",
            ", ".join(stale_player_ids),
        )
        self._reset_session_state()
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        self._task = self.provider.mass.create_task(self._listener_loop())
        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = self.provider.mass.create_task(self._watchdog_loop())

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
                "subscribe:60",
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

    async def _handle_message(self, message: dict[str, Any]) -> None:
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

    async def _refresh_stale_player_subscriptions(self) -> None:
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

        self.provider.logger.warning(
            "CometD playerstatus went stale for %s; resubscribing",
            ", ".join(stale_player_ids),
        )
        for player_id in stale_player_ids:
            self._subscribed_player_ids.discard(player_id)
            self._status_by_player.pop(player_id, None)
            self._pending_player_ids.add(player_id)
            self._touch_player_status_activity(player_id)

    def _reset_session_state(self) -> None:
        """Clear volatile CometD session state after reconnect or stop."""
        self._client_id = None
        self._subscribed_player_ids.clear()
        self._pending_player_ids.clear()
        self._status_by_player.clear()
        self._status_seen_at.clear()
        self._status_wait_events.clear()
        self._known_server_player_ids = None
        self._known_server_player_count = None

    def _touch_player_status_activity(self, player_id: str) -> None:
        """Record fresh activity for one player and wake status waiters."""
        self._status_seen_at[player_id] = time.monotonic()
        if event := self._status_wait_events.get(player_id):
            event.set()

    def _handle_server_status(self, payload: dict[str, Any]) -> None:
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

    def _apply_server_player_connection_state(self, payload: dict[str, Any]) -> None:
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

            if not hasattr(player, "_attr_available") or not hasattr(player, "update_state"):
                continue

            available = bool(connected)
            if getattr(player, "_attr_available", None) == available:
                continue

            player._attr_available = available
            player.update_state()

    async def _handle_player_status(
        self,
        player_id: str,
        partial: StatusPayload,
    ) -> None:
        """Merge one playerstatus payload, compute diffs, and emit events."""
        from music_assistant.providers.lyrion_player.cometd_events import (  # noqa: PLC0415
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
            await self._event_callback(
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

        is_initial = previous is None
        await self._event_callback(
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
            await self._event_callback(
                LmsPlayerPlaybackChangedEvent(
                    player_id=player_id,
                    old_mode=old_mode,
                    new_mode=new_mode,
                )
            )

        old_power = _get_power(previous)
        new_power = _get_power(merged)
        if old_power is not None and new_power is not None and old_power != new_power:
            await self._event_callback(
                LmsPlayerPowerChangedEvent(
                    player_id=player_id,
                    old_powered=old_power,
                    new_powered=new_power,
                )
            )

        old_volume = _get_int(previous, "mixer volume")
        new_volume = _get_int(merged, "mixer volume")
        if old_volume is not None and new_volume is not None and old_volume != new_volume:
            await self._event_callback(
                LmsPlayerVolumeChangedEvent(
                    player_id=player_id,
                    old_volume=old_volume,
                    new_volume=new_volume,
                )
            )

        old_repeat = _get_int(previous, "playlist repeat")
        new_repeat = _get_int(merged, "playlist repeat")
        if old_repeat is not None and new_repeat is not None and old_repeat != new_repeat:
            await self._event_callback(
                LmsPlayerRepeatChangedEvent(
                    player_id=player_id,
                    old_repeat=old_repeat,
                    new_repeat=new_repeat,
                )
            )

        old_shuffle = _get_int(previous, "playlist shuffle")
        new_shuffle = _get_int(merged, "playlist shuffle")
        if old_shuffle is not None and new_shuffle is not None and old_shuffle != new_shuffle:
            await self._event_callback(
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
                await self._event_callback(
                    LmsPlayerSeekedEvent(
                        player_id=player_id,
                        old_time=old_time,
                        new_time=new_time,
                    )
                )

        old_timestamp = _get_float(previous, "playlist_timestamp")
        new_timestamp = _get_float(merged, "playlist_timestamp")
        old_tracks = _get_int(previous, "playlist_tracks")
        new_tracks = _get_int(merged, "playlist_tracks")
        if old_timestamp != new_timestamp or old_tracks != new_tracks:
            await self._event_callback(
                LmsPlayerPlaylistChangedEvent(
                    player_id=player_id,
                    old_playlist_timestamp=old_timestamp,
                    new_playlist_timestamp=new_timestamp,
                    old_playlist_tracks=old_tracks,
                    new_playlist_tracks=new_tracks,
                )
            )

    async def _post(
        self,
        messages: list[dict[str, Any]],
        timeout: int,
    ) -> list[dict[str, Any]]:
        """POST Bayeux messages and return normalized dict payloads."""
        host = self.provider.get_configured_host()
        if not host:
            raise ProviderUnavailableError("Lyrion host is not configured")
        port = self.provider.get_configured_port()
        if port is None:
            raise ProviderUnavailableError("Lyrion port is not configured")

        url = f"http://{host}:{port}/cometd"
        try:
            async with self.provider.mass.http_session.post(
                url,
                json=messages,
                timeout=ClientTimeout(total=timeout),
            ) as response:
                response.raise_for_status()
                payload = cast(
                    "dict[str, Any] | list[dict[str, Any]]",
                    await response.json(),
                )
        except (ClientError, TimeoutError, ValueError) as err:
            raise ProviderUnavailableError(
                f"CometD request to {host}:{port} failed: {err}"
            ) from err

        if isinstance(payload, dict):
            return [payload]
        return [message for message in payload if isinstance(message, dict)]


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


def _same_active_track(
    old_status: StatusPayload,
    new_status: StatusPayload,
) -> bool:
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
    first_item = playlist_loop[0]
    if not isinstance(first_item, dict):
        return None

    for key in ("id", "track_id"):
        value = first_item.get(key)
        if value is not None:
            return str(value)
    return None


def _extract_server_player_ids(payload: dict[str, Any]) -> set[str]:
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


def _is_invalid_player_payload(payload: dict[str, Any]) -> bool:
    """Return True when LMS status subscription reports an invalid player."""
    error = payload.get("error")
    return isinstance(error, str) and error == "invalid player"
