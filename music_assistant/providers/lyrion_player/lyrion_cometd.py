"""Lyrion-specific CometD stream built on top of Bayeux primitives."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import TYPE_CHECKING, Any, cast

from aiohttp import ClientError, ClientTimeout
from music_assistant_models.errors import ProviderUnavailableError

from .bayeux_client import BayeuxClient
from .cometd_events import (
    LmsPlayerEventCallback,
    LmsPlayerPlaybackChangedEvent,
    LmsPlayerPlaylistChangedEvent,
    LmsPlayerPowerChangedEvent,
    LmsPlayerRepeatChangedEvent,
    LmsPlayerSeekedEvent,
    LmsPlayerShuffleChangedEvent,
    LmsPlayerStatusUpdatedEvent,
    LmsPlayerVolumeChangedEvent,
    StatusPayload,
)
from .constants import (
    COMETD_CONNECT_TIMEOUT,
    COMETD_PLAYERSTATUS_TAGS,
    COMETD_RETRY_DELAY,
    RPC_TIMEOUT,
)

if TYPE_CHECKING:
    from .provider import LyrionPlayerProvider


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
        self._client_id: str | None = None
        self._subscribed_player_ids: set[str] = set()
        self._pending_player_ids: set[str] = set()
        self._status_by_player: dict[str, StatusPayload] = {}

    def start(self) -> None:
        """Start the background stream task when needed."""
        if self._task is not None and not self._task.done():
            return
        self._task = self.provider.mass.create_task(self._listener_loop())

    async def stop(self) -> None:
        """Stop background task and clear session state."""
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

        if self._client_id is not None:
            with suppress(ProviderUnavailableError):
                await self._bayeux.disconnect(self._client_id, RPC_TIMEOUT)

        self._client_id = None
        self._subscribed_player_ids.clear()
        self._pending_player_ids.clear()
        self._status_by_player.clear()

    def mark_player_seen(self, player_id: str) -> None:
        """
        Mark player as present so status subscription can be scheduled.

        :param player_id: LMS player id.
        """
        self._pending_player_ids.add(player_id)

    def mark_player_removed(self, player_id: str) -> None:
        """
        Remove cached state for a removed player.

        :param player_id: LMS player id.
        """
        self._subscribed_player_ids.discard(player_id)
        self._pending_player_ids.discard(player_id)
        self._status_by_player.pop(player_id, None)

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

            self._client_id = None
            self._subscribed_player_ids.clear()
            if self.provider.unloading:
                return  # type: ignore[unreachable]
            await asyncio.sleep(COMETD_RETRY_DELAY)

    async def _run_session(self) -> None:
        """Run one handshake/connect loop until reconnect is needed."""
        client_id = await self._bayeux.open_session(RPC_TIMEOUT)
        self._client_id = client_id
        self._subscribed_player_ids.clear()

        for player in self.provider.players:
            self._pending_player_ids.add(player.player_id)

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

    async def _handle_player_status(
        self,
        player_id: str,
        partial: StatusPayload,
    ) -> None:
        """Merge one playerstatus payload, compute diffs, and emit events."""
        previous = self._status_by_player.get(player_id)
        merged = dict(previous or {})
        merged.update(partial)
        self._status_by_player[player_id] = merged

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
