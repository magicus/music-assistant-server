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
from music_assistant.providers.lyrion.client import get_configured_basic_auth
from music_assistant.providers.lyrion.setup_flow import validate_lms_endpoint
from pylyrion.client import LyrionClient
from pylyrion.cometd.constants import COMETD_COMMAND_STATUS_VERIFY_TIMEOUT
from pylyrion.cometd.player_status_events import NormalizedPlayerStatusEvent
from pylyrion.cometd.transport import build_cometd_post_messages_callback
from pylyrion.errors import LyrionRequestError
from pylyrion.lyrion_events import LyrionEvents
from pylyrion.models import LyrionEndpoint
from pylyrion.player import LyrionPlayerClient
from pylyrion.server_control import LyrionServerControl
from pylyrion.session import LyrionSession
from pylyrion.status_stream import PlayerStatusStream

from .constants import (
    CONF_FALLBACK_POLLING,
    CONF_FALLBACK_POLLING_INTERVAL,
    CONF_LMS_HOST,
    CONF_LMS_PASSWORD,
    CONF_LMS_PORT,
    CONF_LMS_USERNAME,
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
        self._runtime_players: dict[str, _ProviderRuntimePlayer] = {}
        self._event_adapter = LyrionEvents(
            mode_map=MODE_MAP,
            idle_state=PlaybackState.IDLE,
            is_supported_player=lambda _player: True,
            get_player=self._get_runtime_player,
            iter_players=self._iter_runtime_players,
            sync_player_queue=self._sync_lms_queue_to_ma,
            update_player_state=self._update_runtime_player_state,
        )
        self._status_stream = PlayerStatusStream(
            post_messages=build_cometd_post_messages_callback(
                get_session=self._build_pylyrion_session,
                unavailable_error_factory=lambda err: ProviderUnavailableError(str(err)),
            ),
            recoverable_errors=(ProviderUnavailableError, LyrionRequestError),
            should_stop=lambda: self.unloading,
            logger=cast("Any", getattr(self, "logger", None)),
            get_player_status=self._get_player_status,
            get_initial_player_ids=self._get_registered_player_ids,
            get_current_player_ids=self._get_registered_player_ids,
            schedule_players_discovery=self.schedule_players_discovery,
            apply_server_player_connection_state=self._apply_server_player_connection_state,
        )
        self.lyrion_server = LyrionServerControl(
            get_players_client=self._build_pylyrion_player_client,
            status_stream=self._status_stream,
            unavailable_error_factory=lambda err: ProviderUnavailableError(str(err)),
        )
        self._unsubscribe_status_events = self._status_stream.subscribe(self._handle_status_event)

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
            username=self.get_setup_value(CONF_LMS_USERNAME),
            password=self.get_setup_value(CONF_LMS_PASSWORD),
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
                runtime_players = cast(
                    "dict[str, _ProviderRuntimePlayer] | None",
                    getattr(self, "_runtime_players", None),
                )
                if runtime_players is not None:
                    runtime_players.pop(known_player.player_id, None)
                await self.mass.players.unregister(known_player.player_id)

    async def remove_player(self, player_id: str) -> None:
        """Remove a player from MA."""
        runtime_players = cast(
            "dict[str, _ProviderRuntimePlayer] | None",
            getattr(self, "_runtime_players", None),
        )
        if runtime_players is not None:
            runtime_players.pop(player_id, None)
        await self.mass.players.unregister(player_id, True)

    async def _get_player_status(self, player_id: str) -> dict[str, Any]:
        """Fetch one player state through the pylyrion facade for internal status use."""
        return await self.lyrion_server.get_player_status(player_id)

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
        self._apply_status_update(player, status)

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
        timeout: float = COMETD_COMMAND_STATUS_VERIFY_TIMEOUT,
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

    async def _send_player_command(
        self,
        player_id: str,
        command: list[Any],
    ) -> dict[str, Any]:
        """Send one raw LMS command for internal provider transport use."""
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

    async def _handle_status_event(self, event: NormalizedPlayerStatusEvent) -> None:
        """Apply one normalized player-status event from pylyrion."""
        await self._event_adapter.handle_event(event)

    def _apply_status_update(self, player: LyrionPlayer, status: dict[str, Any]) -> None:
        """Apply one normalized player status payload to a MA player."""
        self._event_adapter.apply_status(
            self._runtime_player_from_ma(player),
            status,
        )

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
            basic_auth_headers=get_configured_basic_auth(self),
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

    def _get_registered_player_ids(self) -> set[str]:
        """Return player ids currently registered by this provider."""
        return {player.player_id for player in self.players}

    def _runtime_player_from_ma(
        self,
        player: LyrionPlayer,
    ) -> _ProviderRuntimePlayer:
        """Return a pylyrion-neutral runtime player wrapper for one MA player."""
        wrapper = self._runtime_players.get(player.player_id)
        if wrapper is None:
            wrapper = _ProviderRuntimePlayer(player)
            self._runtime_players[player.player_id] = wrapper
        else:
            wrapper.player = player
        return wrapper

    def _get_runtime_player(
        self,
        player_id: str,
    ) -> _ProviderRuntimePlayer | None:
        """Resolve one runtime player wrapper by MA player id."""
        player = self.mass.players.get_player(player_id)
        if not isinstance(player, LyrionPlayer):
            return None
        if player.provider.instance_id != self.instance_id:
            return None
        return self._runtime_player_from_ma(player)

    def _iter_runtime_players(self) -> tuple[_ProviderRuntimePlayer, ...]:
        """Iterate pylyrion-neutral wrappers for provider-owned MA players."""
        return tuple(self._runtime_player_from_ma(player) for player in self.players)

    async def _sync_lms_queue_to_ma(self, player_id: str) -> None:
        """Run MA queue-mirror sync callback for one player id."""
        player = self.mass.players.get_player(player_id)
        if not isinstance(player, LyrionPlayer):
            return
        if player.provider.instance_id != self.instance_id:
            return
        await player._queue_sync.sync_lms_queue_to_ma()

    @staticmethod
    def _update_runtime_player_state(player: _ProviderRuntimePlayer) -> None:
        """Publish state from runtime wrapper back into MA."""
        player.update_state()

    def _apply_server_player_connection_state(
        self,
        payload: dict[str, object],
    ) -> None:
        """Apply serverstatus connected flags to MA player availability."""
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

            connected = self._parse_int(player_data.get("connected"))
            if connected is None:
                continue

            player = self.mass.players.get_player(player_id)
            if not isinstance(player, LyrionPlayer):
                continue

            if player.provider.instance_id != self.instance_id:
                continue

            available = bool(connected)
            if player.available == available:
                continue

            player._attr_available = available
            player.update_state()

    @staticmethod
    def _parse_int(value: object) -> int | None:
        """Parse one integer value from serverstatus fields."""
        if value is None:
            return None
        try:
            return int(cast("int | str", value))
        except TypeError, ValueError:
            return None

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


__all__ = [
    "PLAYERS_BATCH_SIZE",
    "LyrionPlayerProvider",
]


class _ProviderRuntimePlayer:
    """Provider-local adapter from MA player to pylyrion runtime player API."""

    def __init__(self, player: LyrionPlayer) -> None:
        """Store MA player reference for runtime adaptation."""
        self.player = player

    @property
    def player_id(self) -> str:
        """Return stable runtime player id."""
        return self.player.player_id

    @property
    def group_members(self) -> list[str]:
        """Return current runtime group member ids."""
        return list(self.player.group_members)

    def set_available(self, available: bool) -> None:
        """Apply runtime availability state."""
        self.player._attr_available = available

    def set_playback_state(self, playback_state: object) -> None:
        """Apply runtime playback state."""
        self.player._attr_playback_state = cast("PlaybackState", playback_state)

    def set_powered(self, powered: bool) -> None:
        """Apply runtime powered state."""
        self.player._attr_powered = powered

    def set_volume_level(self, volume_level: int) -> None:
        """Apply runtime volume level state."""
        self.player._attr_volume_level = volume_level

    def set_elapsed_time(self, elapsed_time: float, updated_at: float) -> None:
        """Apply runtime elapsed-time state."""
        self.player._attr_elapsed_time = elapsed_time
        self.player._attr_elapsed_time_last_updated = updated_at

    def set_group_members(self, group_members: list[str]) -> None:
        """Apply runtime group membership state."""
        self.player._attr_group_members = group_members

    def update_state(self) -> None:
        """Publish updated runtime state to MA."""
        self.player.update_state()
