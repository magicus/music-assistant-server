"""Lyrion (LMS) player provider implementation."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from typing import Any, cast
from urllib.parse import urlencode

from aiohttp import web
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType, PlaybackState
from music_assistant_models.errors import (
    InvalidDataError,
    MusicAssistantError,
    ProviderUnavailableError,
)

from music_assistant.models.player_provider import PlayerProvider
from music_assistant.providers.lyrion.constants import STATUS_COMMAND_VERIFY_TIMEOUT
from music_assistant.providers.lyrion.setup_flow import validate_lms_endpoint
from pylyrion.client import LyrionClient
from pylyrion.cometd.transport import build_cometd_post_messages_callback
from pylyrion.cometd_event_adapter import LyrionCometDEventAdapter
from pylyrion.errors import LyrionRequestError
from pylyrion.models import LyrionEndpoint
from pylyrion.player import LyrionPlayerClient
from pylyrion.server_control import LyrionServerControl
from pylyrion.session import LyrionSession
from pylyrion.status_stream import PlayerStatusStream

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

MODE_MAP = {
    "play": PlaybackState.PLAYING,
    "pause": PlaybackState.PAUSED,
    "stop": PlaybackState.IDLE,
}


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
        self._status_event_adapter = LyrionCometDEventAdapter(
            self,
            mode_map=MODE_MAP,
            idle_state=PlaybackState.IDLE,
            is_supported_player=lambda player: isinstance(player, LyrionPlayer),
        )
        self._status_stream = PlayerStatusStream(
            self,
            post_messages=build_cometd_post_messages_callback(
                get_session=self._build_pylyrion_session,
                unavailable_error_factory=lambda err: ProviderUnavailableError(str(err)),
            ),
            recoverable_errors=(ProviderUnavailableError, LyrionRequestError),
        )
        self.lyrion_server = LyrionServerControl(
            get_players_client=self._build_pylyrion_player_client,
            status_stream=self._status_stream,
            unavailable_error_factory=lambda err: ProviderUnavailableError(str(err)),
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
            players = await self.lyrion_server.get_players_page(offset, PLAYERS_BATCH_SIZE)
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
        return await self.lyrion_server.get_player_status(player_id)

    async def play_player(self, player_id: str) -> dict[str, Any]:
        """Resume playback for one LMS player via pylyrion."""
        return await self.lyrion_server.play_player(player_id)

    async def pause_player(self, player_id: str) -> dict[str, Any]:
        """Pause playback for one LMS player via pylyrion."""
        return await self.lyrion_server.pause_player(player_id)

    async def stop_player(self, player_id: str) -> dict[str, Any]:
        """Stop playback for one LMS player via pylyrion."""
        return await self.lyrion_server.stop_player(player_id)

    async def set_player_power(self, player_id: str, powered: bool) -> dict[str, Any]:
        """Set player power state via pylyrion."""
        return await self.lyrion_server.set_player_power(player_id, powered)

    async def sync_player_to(
        self,
        player_id: str,
        leader_player_id: str,
    ) -> dict[str, Any]:
        """Join one player to an LMS sync leader via pylyrion."""
        return await self.lyrion_server.sync_player_to(player_id, leader_player_id)

    async def unsync_player(self, player_id: str) -> dict[str, Any]:
        """Remove one player from LMS sync grouping via pylyrion."""
        return await self.lyrion_server.unsync_player(player_id)

    async def get_player_queue_status(
        self,
        player_id: str,
        *,
        offset: int = 0,
        limit: int,
    ) -> dict[str, Any]:
        """Return queue-inclusive LMS status via pylyrion."""
        return await self.lyrion_server.get_player_queue_status(
            player_id,
            offset=offset,
            limit=limit,
        )

    async def set_player_queue_index(self, player_id: str, index: int | str) -> dict[str, Any]:
        """Set active LMS queue index via pylyrion."""
        return await self.lyrion_server.set_player_queue_index(player_id, index)

    async def next_player_track(self, player_id: str) -> dict[str, Any]:
        """Skip to next LMS queue entry via pylyrion."""
        return await self.lyrion_server.next_player_track(player_id)

    async def previous_player_track(self, player_id: str) -> dict[str, Any]:
        """Skip to previous LMS queue entry via pylyrion."""
        return await self.lyrion_server.previous_player_track(player_id)

    async def set_player_volume(self, player_id: str, volume_level: int) -> dict[str, Any]:
        """Set LMS mixer volume via pylyrion."""
        return await self.lyrion_server.set_player_volume(player_id, volume_level)

    async def set_player_muted(self, player_id: str, muted: bool) -> dict[str, Any]:
        """Set LMS mixer mute state via pylyrion."""
        return await self.lyrion_server.set_player_muted(player_id, muted)

    async def seek_player(self, player_id: str, position: int) -> dict[str, Any]:
        """Seek LMS playback position via pylyrion."""
        return await self.lyrion_server.seek_player(player_id, position)

    async def set_player_sync_volume(self, player_id: str, enabled: bool) -> dict[str, Any]:
        """Set LMS syncVolume preference via pylyrion."""
        return await self.lyrion_server.set_player_sync_volume(player_id, enabled)

    async def set_player_repeat_mode(self, player_id: str, repeat_mode: int) -> dict[str, Any]:
        """Set LMS repeat mode via pylyrion."""
        return await self.lyrion_server.set_player_repeat_mode(player_id, repeat_mode)

    async def set_player_shuffle_mode(
        self,
        player_id: str,
        shuffle_mode: int,
    ) -> dict[str, Any]:
        """Set LMS shuffle mode via pylyrion."""
        return await self.lyrion_server.set_player_shuffle_mode(player_id, shuffle_mode)

    async def clear_player_queue(self, player_id: str) -> dict[str, Any]:
        """Clear LMS queue via pylyrion."""
        return await self.lyrion_server.clear_player_queue(player_id)

    async def add_player_track_id_to_queue(
        self,
        player_id: str,
        track_id: str,
        *,
        command: str = "add",
    ) -> dict[str, Any]:
        """Add or load one LMS track_id via pylyrion."""
        return await self.lyrion_server.add_player_track_id_to_queue(
            player_id,
            track_id,
            command=command,
        )

    async def add_player_url_to_queue(
        self,
        player_id: str,
        url: str,
        title: str | None = None,
        artist: str | None = None,
        album: str | None = None,
    ) -> dict[str, Any]:
        """Add one URL to LMS queue via pylyrion."""
        return await self.lyrion_server.add_player_url_to_queue(
            player_id,
            url,
            title=title,
            artist=artist,
            album=album,
        )

    async def play_player_url(self, player_id: str, url: str) -> dict[str, Any]:
        """Start playback of one URL via pylyrion."""
        return await self.lyrion_server.play_player_url(player_id, url)

    async def append_player_url(self, player_id: str, url: str) -> dict[str, Any]:
        """Append one URL to LMS queue via pylyrion."""
        return await self.lyrion_server.append_player_url(player_id, url)

    async def move_player_queue_item(
        self,
        player_id: str,
        from_index: int,
        to_index: int,
    ) -> dict[str, Any]:
        """Move one LMS queue item via pylyrion."""
        return await self.lyrion_server.move_player_queue_item(player_id, from_index, to_index)

    async def delete_player_queue_item(self, player_id: str, index: int) -> dict[str, Any]:
        """Delete one LMS queue item via pylyrion."""
        return await self.lyrion_server.delete_player_queue_item(player_id, index)

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
        return self.lyrion_server.get_last_status_seen_at(player_id)

    def get_cached_status(self, player_id: str) -> dict[str, Any] | None:
        """Return cached status snapshot for one player."""
        return self.lyrion_server.get_cached_status(player_id)

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
        return await self.lyrion_server.verify_status_expectation(
            player_id,
            baseline,
            expectation,
            expected_state,
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
        return await self.lyrion_server.send_player_command(player_id, command)

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

    def _build_pylyrion_session(self) -> LyrionSession:
        """Build a pylyrion session from current MA provider config."""
        host = self.get_configured_host()
        if not host:
            raise ProviderUnavailableError("Lyrion host is not configured")
        return LyrionSession(
            http_session=self.mass.http_session,
            endpoint=LyrionEndpoint(
                host=host,
                port=self.get_configured_port(),
            ),
        )

    def _build_pylyrion_client(self) -> LyrionClient:
        """Build a pylyrion client from current MA provider config."""
        return LyrionClient(self._build_pylyrion_session())

    def _build_pylyrion_player_client(self) -> LyrionPlayerClient:
        """Build pylyrion player client from current MA provider config."""
        return self._build_pylyrion_client().players

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
