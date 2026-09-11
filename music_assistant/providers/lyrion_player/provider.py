"""Lyrion (LMS) player provider implementation."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any, cast
from urllib.parse import urlencode

from aiohttp import ClientError, ClientTimeout, web
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType
from music_assistant_models.errors import (
    InvalidDataError,
    MusicAssistantError,
    ProviderUnavailableError,
)

from music_assistant.models.player_provider import PlayerProvider
from music_assistant.providers.lyrion.client import build_lms_url, rpc_request
from music_assistant.providers.lyrion.constants import STATUS_COMMAND_VERIFY_TIMEOUT
from music_assistant.providers.lyrion.setup_flow import validate_lms_endpoint
from pylyrion.client import LyrionClient
from pylyrion.cometd import PlayerStatusStream
from pylyrion.errors import LyrionProtocolError, LyrionRequestError, LyrionTimeoutError
from pylyrion.models import LyrionEndpoint
from pylyrion.session import LyrionSession

from .constants import (
    CONF_FALLBACK_POLLING,
    CONF_FALLBACK_POLLING_INTERVAL,
    CONF_LMS_HOST,
    CONF_LMS_PORT,
    DEFAULT_FALLBACK_POLLING_INTERVAL,
    DEFAULT_LMS_PORT,
    PLAYERS_BATCH_SIZE,
)
from .player import LyrionPlayer
from .status_event_adapter import LyrionStatusEventAdapter


class LyrionPlayerProvider(PlayerProvider):
    """Player provider for Lyrion/Logitech Media Server managed players."""

    _unregister_stream_redirect_route: Callable[[], None] | None
    _unsubscribe_status_events: Callable[[], None] | None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize provider internals."""
        super().__init__(*args, **kwargs)
        self._unregister_stream_redirect_route = None
        self._unsubscribe_status_events = None
        self._discover_players_task: asyncio.Task[None] | None = None
        self._discover_players_again = False
        self._status_event_adapter = LyrionStatusEventAdapter(self)
        self._status_stream = PlayerStatusStream(
            self,
            post_messages=self._post_status_stream_messages,
            recoverable_errors=(ProviderUnavailableError, LyrionRequestError),
        )
        self._unsubscribe_status_events = self._status_stream.subscribe(
            self._status_event_adapter.handle_event
        )

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to configure this provider."""
        return (
            ConfigEntry(
                key=CONF_FALLBACK_POLLING,
                type=ConfigEntryType.BOOLEAN,
                required=False,
                default_value=False,
                advanced=True,
            ),
            ConfigEntry(
                key=CONF_FALLBACK_POLLING_INTERVAL,
                type=ConfigEntryType.INTEGER,
                required=False,
                default_value=DEFAULT_FALLBACK_POLLING_INTERVAL,
                range=(5, 300),
                advanced=True,
            ),
        )

    async def handle_async_init(self) -> None:
        """Validate the configured Lyrion endpoint."""
        host = self.get_configured_host()
        port = self.get_configured_port()
        self.logger.debug(
            "Validating Lyrion JSON-RPC endpoint %s:%s",
            host,
            port,
        )
        await validate_lms_endpoint(
            host=host,
            port=port,
            http_session=self.mass.http_session,
            translation_owner=self.translation_owner,
        )

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        self._unregister_stream_redirect_route = self.mass.streams.register_dynamic_route(
            f"/{self.instance_id}/get_stream_url",
            self._handle_get_stream_url,
        )
        await self.discover_players()
        self._status_stream.start()

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        if self._discover_players_task is not None:
            self._discover_players_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._discover_players_task
            self._discover_players_task = None

        await self._status_stream.stop()
        if unsubscribe := self._unsubscribe_status_events:
            self._unsubscribe_status_events = None
            unsubscribe()
        if unregister := self._unregister_stream_redirect_route:
            self._unregister_stream_redirect_route = None
            unregister()
        await super().unload(is_removed)

    def schedule_players_discovery(self) -> None:
        """Schedule players rediscovery and coalesce bursts into one task."""
        if self.unloading:
            return

        if self._discover_players_task and not self._discover_players_task.done():
            self._discover_players_again = True
            return

        self._discover_players_again = False
        self._discover_players_task = self.mass.create_task(self._run_discover_players_loop())
        self._discover_players_task.add_done_callback(self._handle_discover_players_done)

    def build_stream_redirect_url(
        self,
        player_id: str,
        queue_id: str,
        queue_item_id: str,
        ma_uri: str | None,
    ) -> str:
        """
        Build URL for on-demand stream resolution.

        LMS requests this URL when it needs a fresh playable stream.

        :param player_id: Target player id.
        :param queue_id: MA queue id.
        :param queue_item_id: MA queue item id.
        :param ma_uri: Optional source URI used for diagnostics.
        """
        params = {
            "player_id": player_id,
            "queue_id": queue_id,
            "queue_item_id": queue_item_id,
        }
        if ma_uri:
            params["ma_uri"] = ma_uri
        query = urlencode(params)
        return f"{self.mass.streams.base_url}/{self.instance_id}/get_stream_url?{query}"

    async def discover_players(self) -> None:
        """Discover players known by LMS and register them in MA."""
        seen_player_ids: set[str] = set()

        offset = 0
        while True:
            result = await self._rpc_request(
                player_id="",
                command=["players", offset, PLAYERS_BATCH_SIZE],
            )
            players = cast(
                "list[dict[str, Any]]",
                result.get("players_loop", []),
            )
            if not players:
                break

            for player_data in players:
                if not (
                    player_id := cast(
                        "str | None",
                        player_data.get("playerid"),
                    )
                ):
                    continue
                seen_player_ids.add(player_id)
                existing_player = self.mass.players.get_player(player_id)
                if existing_player and isinstance(existing_player, LyrionPlayer):
                    await existing_player.sync_from_lms(player_data)
                    self._status_stream.mark_player_seen(player_id)
                    continue
                lyrion_player = LyrionPlayer(
                    provider=self,
                    player_id=player_id,
                    initial_data=player_data,
                )
                await self.mass.players.register(lyrion_player)
                self._status_stream.mark_player_seen(player_id)

            if len(players) < PLAYERS_BATCH_SIZE:
                break
            offset += PLAYERS_BATCH_SIZE

        for player_id in seen_player_ids:
            self._status_stream.mark_player_seen(player_id)

        for known_player in self.players:
            if known_player.player_id not in seen_player_ids:
                self._status_stream.mark_player_removed(known_player.player_id)
                await self.mass.players.unregister(known_player.player_id)

    async def remove_player(self, player_id: str) -> None:
        """Remove a player from MA."""
        await self.mass.players.unregister(player_id, True)

    async def get_player_status(self, player_id: str) -> dict[str, Any]:
        """
        Return runtime status for a player.

        :param player_id: LMS player id.
        """
        try:
            return await self._build_pylyrion_client().players.get_status(player_id)
        except (LyrionProtocolError, LyrionRequestError, LyrionTimeoutError) as err:
            raise ProviderUnavailableError(str(err)) from err

    async def play_player(self, player_id: str) -> dict[str, Any]:
        """Resume playback for one LMS player via pylyrion."""
        try:
            return await self._build_pylyrion_client().players.play(player_id)
        except (LyrionProtocolError, LyrionRequestError, LyrionTimeoutError) as err:
            raise ProviderUnavailableError(str(err)) from err

    async def pause_player(self, player_id: str) -> dict[str, Any]:
        """Pause playback for one LMS player via pylyrion."""
        try:
            return await self._build_pylyrion_client().players.pause(player_id)
        except (LyrionProtocolError, LyrionRequestError, LyrionTimeoutError) as err:
            raise ProviderUnavailableError(str(err)) from err

    async def stop_player(self, player_id: str) -> dict[str, Any]:
        """Stop playback for one LMS player via pylyrion."""
        try:
            return await self._build_pylyrion_client().players.stop(player_id)
        except (LyrionProtocolError, LyrionRequestError, LyrionTimeoutError) as err:
            raise ProviderUnavailableError(str(err)) from err

    async def set_player_power(self, player_id: str, powered: bool) -> dict[str, Any]:
        """Set player power state via pylyrion."""
        try:
            return await self._build_pylyrion_client().players.set_power(player_id, powered)
        except (LyrionProtocolError, LyrionRequestError, LyrionTimeoutError) as err:
            raise ProviderUnavailableError(str(err)) from err

    async def sync_player_to(
        self,
        player_id: str,
        leader_player_id: str,
    ) -> dict[str, Any]:
        """Join one player to an LMS sync leader via pylyrion."""
        try:
            return await self._build_pylyrion_client().players.sync_to(
                player_id,
                leader_player_id,
            )
        except (LyrionProtocolError, LyrionRequestError, LyrionTimeoutError) as err:
            raise ProviderUnavailableError(str(err)) from err

    async def unsync_player(self, player_id: str) -> dict[str, Any]:
        """Remove one player from LMS sync grouping via pylyrion."""
        try:
            return await self._build_pylyrion_client().players.unsync(player_id)
        except (LyrionProtocolError, LyrionRequestError, LyrionTimeoutError) as err:
            raise ProviderUnavailableError(str(err)) from err

    async def get_player_queue_status(
        self,
        player_id: str,
        *,
        offset: int = 0,
        limit: int,
    ) -> dict[str, Any]:
        """Return queue-inclusive LMS status via pylyrion."""
        try:
            return await self._build_pylyrion_client().players.get_queue_status(
                player_id,
                offset=offset,
                limit=limit,
            )
        except (LyrionProtocolError, LyrionRequestError, LyrionTimeoutError) as err:
            raise ProviderUnavailableError(str(err)) from err

    async def set_player_queue_index(self, player_id: str, index: int | str) -> dict[str, Any]:
        """Set active LMS queue index via pylyrion."""
        try:
            return await self._build_pylyrion_client().players.set_queue_index(player_id, index)
        except (LyrionProtocolError, LyrionRequestError, LyrionTimeoutError) as err:
            raise ProviderUnavailableError(str(err)) from err

    async def next_player_track(self, player_id: str) -> dict[str, Any]:
        """Skip to next LMS queue entry via pylyrion."""
        try:
            return await self._build_pylyrion_client().players.next_track(player_id)
        except (LyrionProtocolError, LyrionRequestError, LyrionTimeoutError) as err:
            raise ProviderUnavailableError(str(err)) from err

    async def previous_player_track(self, player_id: str) -> dict[str, Any]:
        """Skip to previous LMS queue entry via pylyrion."""
        try:
            return await self._build_pylyrion_client().players.previous_track(player_id)
        except (LyrionProtocolError, LyrionRequestError, LyrionTimeoutError) as err:
            raise ProviderUnavailableError(str(err)) from err

    async def set_player_volume(self, player_id: str, volume_level: int) -> dict[str, Any]:
        """Set LMS mixer volume via pylyrion."""
        try:
            return await self._build_pylyrion_client().players.set_volume(player_id, volume_level)
        except (LyrionProtocolError, LyrionRequestError, LyrionTimeoutError) as err:
            raise ProviderUnavailableError(str(err)) from err

    async def set_player_muted(self, player_id: str, muted: bool) -> dict[str, Any]:
        """Set LMS mixer mute state via pylyrion."""
        try:
            return await self._build_pylyrion_client().players.set_muted(player_id, muted)
        except (LyrionProtocolError, LyrionRequestError, LyrionTimeoutError) as err:
            raise ProviderUnavailableError(str(err)) from err

    async def seek_player(self, player_id: str, position: int) -> dict[str, Any]:
        """Seek LMS playback position via pylyrion."""
        try:
            return await self._build_pylyrion_client().players.seek(player_id, position)
        except (LyrionProtocolError, LyrionRequestError, LyrionTimeoutError) as err:
            raise ProviderUnavailableError(str(err)) from err

    async def set_player_sync_volume(self, player_id: str, enabled: bool) -> dict[str, Any]:
        """Set LMS syncVolume preference via pylyrion."""
        try:
            return await self._build_pylyrion_client().players.set_sync_volume(player_id, enabled)
        except (LyrionProtocolError, LyrionRequestError, LyrionTimeoutError) as err:
            raise ProviderUnavailableError(str(err)) from err

    async def set_player_repeat_mode(self, player_id: str, repeat_mode: int) -> dict[str, Any]:
        """Set LMS repeat mode via pylyrion."""
        try:
            return await self._build_pylyrion_client().players.set_repeat_mode(
                player_id,
                repeat_mode,
            )
        except (LyrionProtocolError, LyrionRequestError, LyrionTimeoutError) as err:
            raise ProviderUnavailableError(str(err)) from err

    async def set_player_shuffle_mode(
        self,
        player_id: str,
        shuffle_mode: int,
    ) -> dict[str, Any]:
        """Set LMS shuffle mode via pylyrion."""
        try:
            return await self._build_pylyrion_client().players.set_shuffle_mode(
                player_id,
                shuffle_mode,
            )
        except (LyrionProtocolError, LyrionRequestError, LyrionTimeoutError) as err:
            raise ProviderUnavailableError(str(err)) from err

    async def clear_player_queue(self, player_id: str) -> dict[str, Any]:
        """Clear LMS queue via pylyrion."""
        try:
            return await self._build_pylyrion_client().players.clear_queue(player_id)
        except (LyrionProtocolError, LyrionRequestError, LyrionTimeoutError) as err:
            raise ProviderUnavailableError(str(err)) from err

    async def add_player_track_id_to_queue(
        self,
        player_id: str,
        track_id: str,
        *,
        command: str = "add",
    ) -> dict[str, Any]:
        """Add or load one LMS track_id via pylyrion."""
        try:
            return await self._build_pylyrion_client().players.add_track_id(
                player_id,
                track_id,
                command=command,
            )
        except (LyrionProtocolError, LyrionRequestError, LyrionTimeoutError) as err:
            raise ProviderUnavailableError(str(err)) from err

    async def add_player_url_to_queue(
        self,
        player_id: str,
        url: str,
        title: str | None = None,
        artist: str | None = None,
        album: str | None = None,
    ) -> dict[str, Any]:
        """Add one URL to LMS queue via pylyrion."""
        try:
            return await self._build_pylyrion_client().players.add_url_to_queue(
                player_id,
                url,
                title=title,
                artist=artist,
                album=album,
            )
        except (LyrionProtocolError, LyrionRequestError, LyrionTimeoutError) as err:
            raise ProviderUnavailableError(str(err)) from err

    async def play_player_url(self, player_id: str, url: str) -> dict[str, Any]:
        """Start playback of one URL via pylyrion."""
        try:
            return await self._build_pylyrion_client().players.play_url(player_id, url)
        except (LyrionProtocolError, LyrionRequestError, LyrionTimeoutError) as err:
            raise ProviderUnavailableError(str(err)) from err

    async def append_player_url(self, player_id: str, url: str) -> dict[str, Any]:
        """Append one URL to LMS queue via pylyrion."""
        try:
            return await self._build_pylyrion_client().players.add_url(player_id, url)
        except (LyrionProtocolError, LyrionRequestError, LyrionTimeoutError) as err:
            raise ProviderUnavailableError(str(err)) from err

    async def move_player_queue_item(
        self,
        player_id: str,
        from_index: int,
        to_index: int,
    ) -> dict[str, Any]:
        """Move one LMS queue item via pylyrion."""
        try:
            return await self._build_pylyrion_client().players.move_queue_item(
                player_id,
                from_index,
                to_index,
            )
        except (LyrionProtocolError, LyrionRequestError, LyrionTimeoutError) as err:
            raise ProviderUnavailableError(str(err)) from err

    async def delete_player_queue_item(self, player_id: str, index: int) -> dict[str, Any]:
        """Delete one LMS queue item via pylyrion."""
        try:
            return await self._build_pylyrion_client().players.delete_queue_item(
                player_id,
                index,
            )
        except (LyrionProtocolError, LyrionRequestError, LyrionTimeoutError) as err:
            raise ProviderUnavailableError(str(err)) from err

    def apply_status_update(
        self,
        player: LyrionPlayer,
        status: dict[str, Any],
    ) -> None:
        """
        Apply a normalized player status payload through the status adapter.

        :param player: Target Lyrion player.
        :param status: LMS player status payload.
        """
        self._status_event_adapter.apply_status(player, status)

    def get_last_status_seen_at(self, player_id: str) -> float | None:
        """Return the last status-stream timestamp for one player."""
        return self._status_stream.get_last_player_status_seen_at(player_id)

    def get_cached_status(self, player_id: str) -> dict[str, Any] | None:
        """Return cached status snapshot for one player."""
        return self._status_stream.get_player_status_snapshot(player_id)

    async def wait_for_status_update(
        self,
        player_id: str,
        since: float | None,
        timeout: float = STATUS_COMMAND_VERIFY_TIMEOUT,
    ) -> bool:
        """Wait for a newer status update for one player."""
        return await self._status_stream.wait_for_player_status_update(
            player_id,
            since,
            timeout,
        )

    async def verify_status_expectation(
        self,
        player_id: str,
        baseline: float | None,
        expectation: Callable[[dict[str, Any]], bool] | None = None,
        expected_state: str = "status update",
    ) -> bool:
        """Verify expected status via stream events and fallback polling."""
        return await self._status_stream.verify_player_status_expectation(
            player_id,
            baseline,
            expectation,
            expected_state,
        )

    async def play_player_with_verify(self, player_id: str) -> None:
        """Resume playback and verify mode transition to play."""
        await self._run_command_with_status_verify(
            player_id,
            command=lambda: self.play_player(player_id),
            expectation=lambda status: _get_status_str(status, "mode") == "play",
            expected_state="mode=play",
        )

    async def pause_player_with_verify(self, player_id: str) -> None:
        """Pause playback and verify mode transition to pause."""
        await self._run_command_with_status_verify(
            player_id,
            command=lambda: self.pause_player(player_id),
            expectation=lambda status: _get_status_str(status, "mode") == "pause",
            expected_state="mode=pause",
        )

    async def stop_player_with_verify(self, player_id: str) -> None:
        """Stop playback and verify mode transition to stop."""
        await self._run_command_with_status_verify(
            player_id,
            command=lambda: self.stop_player(player_id),
            expectation=lambda status: _get_status_str(status, "mode") == "stop",
            expected_state="mode=stop",
        )

    async def set_player_power_with_verify(self, player_id: str, powered: bool) -> None:
        """Set power and verify power status."""
        target = 1 if powered else 0
        await self._run_command_with_status_verify(
            player_id,
            command=lambda: self.set_player_power(player_id, powered),
            expectation=lambda status: _get_status_int(status, "power") == target,
            expected_state=f"power={target}",
        )

    async def set_player_volume_with_verify(self, player_id: str, volume_level: int) -> None:
        """Set volume and verify mixer volume."""
        target = max(0, min(100, int(volume_level)))
        await self._run_command_with_status_verify(
            player_id,
            command=lambda: self.set_player_volume(player_id, target),
            expectation=lambda status: _get_status_int(status, "mixer volume") == target,
            expected_state=f"mixer volume={target}",
        )

    async def set_player_muted_with_verify(self, player_id: str, muted: bool) -> None:
        """Set mute and verify mixer muting status."""
        target = 1 if muted else 0
        await self._run_command_with_status_verify(
            player_id,
            command=lambda: self.set_player_muted(player_id, muted),
            expectation=lambda status: _get_status_int(status, "mixer muting") == target,
            expected_state=f"mixer muting={target}",
        )

    async def next_player_track_with_verify(self, player_id: str) -> None:
        """Skip to next track and verify queue index transition."""
        previous = self.get_cached_status(player_id)
        previous_index = _get_status_int(previous or {}, "playlist_cur_index")
        if previous_index is None:
            await self._run_command_with_status_verify(
                player_id,
                command=lambda: self.next_player_track(player_id),
            )
            return

        await self._run_command_with_status_verify(
            player_id,
            command=lambda: self.next_player_track(player_id),
            expectation=lambda status: (
                _get_status_int(status, "playlist_cur_index") not in (None, previous_index)
            ),
            expected_state="playlist_cur_index changed",
        )

    async def previous_player_track_with_verify(self, player_id: str) -> None:
        """Skip to previous track and verify queue index transition."""
        previous = self.get_cached_status(player_id)
        previous_index = _get_status_int(previous or {}, "playlist_cur_index")
        if previous_index is None:
            await self._run_command_with_status_verify(
                player_id,
                command=lambda: self.previous_player_track(player_id),
            )
            return

        await self._run_command_with_status_verify(
            player_id,
            command=lambda: self.previous_player_track(player_id),
            expectation=lambda status: (
                _get_status_int(status, "playlist_cur_index") not in (None, previous_index)
            ),
            expected_state="playlist_cur_index changed",
        )

    async def seek_player_with_verify(self, player_id: str, position: int) -> None:
        """Seek playback and verify time progression near target."""
        target = max(0, int(position))
        await self._run_command_with_status_verify(
            player_id,
            command=lambda: self.seek_player(player_id, target),
            expectation=lambda status: _time_matches_target(status, target),
            expected_state=f"time~={target}",
        )

    async def send_player_command(
        self,
        player_id: str,
        command: list[Any],
    ) -> dict[str, Any]:
        """
        Send a command to a specific player.

        :param player_id: LMS player id.
        :param command: LMS command list.
        """
        try:
            return await self._build_pylyrion_client().players.send_command(player_id, command)
        except (LyrionProtocolError, LyrionRequestError, LyrionTimeoutError) as err:
            raise ProviderUnavailableError(str(err)) from err

    async def _run_command_with_status_verify(
        self,
        player_id: str,
        command: Callable[[], Awaitable[dict[str, Any]]],
        expectation: Callable[[dict[str, Any]], bool] | None = None,
        expected_state: str = "status update",
    ) -> None:
        """Run one player command and verify status expectation."""
        baseline = self.get_last_status_seen_at(player_id)
        await command()
        await self.verify_status_expectation(
            player_id,
            baseline,
            expectation=expectation,
            expected_state=expected_state,
        )

    def get_configured_host(self) -> str | None:
        """Return configured host from setup data."""
        raw_host = self.get_setup_value(CONF_LMS_HOST)
        if not isinstance(raw_host, str):
            return None
        host = raw_host.strip()
        return host or None

    def get_configured_port(
        self,
        default: int | None = DEFAULT_LMS_PORT,
    ) -> int | None:
        """Return configured port from setup data."""
        raw_port = self.get_setup_value(CONF_LMS_PORT, default)
        if raw_port is None:
            return None
        try:
            return int(cast("int | str", raw_port))
        except TypeError, ValueError:
            return default

    async def _post_status_stream_messages(
        self,
        messages: list[dict[str, object]],
        timeout: int,
    ) -> list[dict[str, object]]:
        """POST stream messages and normalize response payload for pylyrion runtime."""
        host = self.get_configured_host()
        if not host:
            raise ProviderUnavailableError("Lyrion host is not configured")
        port = self.get_configured_port()
        if port is None:
            raise ProviderUnavailableError("Lyrion port is not configured")

        url = build_lms_url(host, port, "/cometd")
        try:
            async with self.mass.http_session.post(
                url,
                json=messages,
                timeout=ClientTimeout(total=timeout),
            ) as response:
                response.raise_for_status()
                payload = await response.json()
        except (ClientError, TimeoutError, ValueError) as err:
            raise ProviderUnavailableError(
                f"Status stream request to {host}:{port} failed: {err}"
            ) from err

        if isinstance(payload, dict):
            return [payload]
        if not isinstance(payload, list):
            raise ProviderUnavailableError("Status stream response must be a JSON object or list")
        return [message for message in payload if isinstance(message, dict)]

    async def _handle_get_stream_url(
        self,
        request: web.Request,
    ) -> web.Response:
        """
        Resolve one queue item to a fresh playable URL and redirect LMS to it.

        Query params:
            player_id: id of the player requesting the stream.
            queue_id: queue id to resolve from.
            queue_item_id: queue item id in that queue.
            ma_uri: optional source uri (debug only).
        """
        player_id = request.query.get("player_id", "")
        queue_id = request.query.get("queue_id", "")
        queue_item_id = request.query.get("queue_item_id", "")
        ma_uri = request.query.get("ma_uri", "")

        if not player_id or not queue_id or not queue_item_id:
            raise web.HTTPBadRequest(reason="Missing required query params")

        player = self.mass.players.get_player(player_id)
        if not isinstance(player, LyrionPlayer) or (
            player.provider.instance_id != self.instance_id
        ):
            raise web.HTTPNotFound(reason=f"Unknown Lyrion player: {player_id}")

        if queue_id != player_id:
            raise web.HTTPBadRequest(reason="queue_id must match player_id")

        queue_item = self.mass.player_queues.get_item(queue_id, queue_item_id)
        if queue_item is None:
            raise web.HTTPNotFound(reason=f"Unknown queue item: {queue_item_id}")

        try:
            player_media = await self.mass.player_queues.player_media_from_queue_item(queue_item)
            stream_url = await self.mass.streams.resolve_stream_url(
                player_id,
                player_media,
            )
        except (
            MusicAssistantError,
            ProviderUnavailableError,
            InvalidDataError,
        ) as err:
            self.logger.debug(
                "Failed to resolve fresh stream URL for %s (%s): %s",
                queue_item_id,
                ma_uri or queue_item.uri,
                err,
            )
            raise web.HTTPNotFound(reason="Unable to resolve stream URL") from err

        raise web.HTTPFound(location=stream_url)

    async def _rpc_request(
        self,
        player_id: str,
        command: list[Any],
    ) -> dict[str, Any]:
        """Execute one LMS JSON-RPC request via the shared Lyrion transport."""
        return await rpc_request(self, player_id=player_id, command=command)

    def _build_pylyrion_client(self) -> LyrionClient:
        """Build a pylyrion client from current MA provider config."""
        host = self.get_configured_host()
        if not host:
            raise ProviderUnavailableError("Lyrion host is not configured")
        session = LyrionSession(
            http_session=self.mass.http_session,
            endpoint=LyrionEndpoint(
                host=host,
                port=self.get_configured_port(),
            ),
        )
        return LyrionClient(session)

    async def _run_discover_players_loop(self) -> None:
        """Run player discovery once or repeatedly while new triggers arrive."""
        while not self.unloading:
            self._discover_players_again = False
            await self.discover_players()
            if not self._discover_players_again:
                break

    def _handle_discover_players_done(self, task: asyncio.Task[None]) -> None:
        """Log any discovery task failure and clear task bookkeeping."""
        if self._discover_players_task is task:
            self._discover_players_task = None

        if task.cancelled():
            return
        if exception := task.exception():
            self.logger.warning(
                "Lyrion dynamic player rediscovery failed: %s",
                exception,
            )


def _get_status_str(status: dict[str, Any], key: str) -> str | None:
    """Read a status field as a non-empty string when available."""
    value = status.get(key)
    if isinstance(value, str):
        return value
    if value is None:
        return None
    return str(value)


def _get_status_int(status: dict[str, Any], key: str) -> int | None:
    """Read a status field as integer when available."""
    value = status.get(key)
    if value is None:
        return None
    try:
        return int(cast("int | str", value))
    except TypeError, ValueError:
        return None


def _time_matches_target(status: dict[str, Any], target: int) -> bool:
    """Return True when reported playback time is close to target seconds."""
    value = status.get("time")
    if value is None:
        return False
    try:
        return abs(float(cast("int | float | str", value)) - float(target)) <= 1.0
    except TypeError, ValueError:
        return False
