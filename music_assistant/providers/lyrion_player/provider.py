"""Lyrion (LMS) player provider implementation."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast
from urllib.parse import urlencode

from aiohttp import ClientError, ClientTimeout, ServerDisconnectedError, web
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType
from music_assistant_models.errors import (
    InvalidDataError,
    MusicAssistantError,
    ProviderUnavailableError,
)

from music_assistant.models.player_provider import PlayerProvider
from music_assistant.providers.lyrion_music.shared.setup_flow import validate_lms_endpoint

from .cometd_event_adapter import LyrionCometDEventAdapter
from .constants import (
    CONF_LMS_HOST,
    CONF_LMS_PORT,
    DEFAULT_LMS_PORT,
    PLAYERS_BATCH_SIZE,
    RPC_TIMEOUT,
)
from .lyrion_cometd import LyrionCometDEventStream
from .player import LyrionPlayer


class LyrionPlayerProvider(PlayerProvider):
    """Player provider for Lyrion/Logitech Media Server managed players."""

    _unregister_stream_redirect_route: Callable[[], None] | None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize provider internals."""
        super().__init__(*args, **kwargs)
        self._unregister_stream_redirect_route = None
        self._cometd_adapter = LyrionCometDEventAdapter(self)
        self._cometd_stream = LyrionCometDEventStream(
            self,
            self._cometd_adapter.handle_event,
        )

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to configure this provider."""
        configured_host = self._get_configured_host() or ""
        configured_port = self._get_configured_port(default=None)
        return (
            ConfigEntry(
                key=CONF_LMS_HOST,
                type=ConfigEntryType.STRING,
                required=False,
                read_only=True,
                value=configured_host,
            ),
            ConfigEntry(
                key=CONF_LMS_PORT,
                type=ConfigEntryType.INTEGER,
                required=False,
                read_only=True,
                value=configured_port,
            ),
        )

    async def handle_async_init(self) -> None:
        """Validate the configured Lyrion endpoint."""
        host = self._get_configured_host()
        port = self._get_configured_port()
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
        self._cometd_stream.start()

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        await self._cometd_stream.stop()
        if unregister := self._unregister_stream_redirect_route:
            self._unregister_stream_redirect_route = None
            unregister()
        await super().unload(is_removed)

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
                    self._cometd_stream.mark_player_seen(player_id)
                    continue
                lyrion_player = LyrionPlayer(
                    provider=self,
                    player_id=player_id,
                    initial_data=player_data,
                )
                await self.mass.players.register(lyrion_player)
                self._cometd_stream.mark_player_seen(player_id)

            if len(players) < PLAYERS_BATCH_SIZE:
                break
            offset += PLAYERS_BATCH_SIZE

        for player_id in seen_player_ids:
            self._cometd_stream.mark_player_seen(player_id)

        for known_player in self.players:
            if known_player.player_id not in seen_player_ids:
                self._cometd_stream.mark_player_removed(known_player.player_id)
                await self.mass.players.unregister(known_player.player_id)

    async def remove_player(self, player_id: str) -> None:
        """Remove a player from MA."""
        await self.mass.players.unregister(player_id, True)

    async def get_player_status(self, player_id: str) -> dict[str, Any]:
        """
        Return runtime status for a player.

        :param player_id: LMS player id.
        """
        return await self._rpc_request(
            player_id=player_id,
            command=["status", "-", 1],
        )

    def apply_status_update(
        self,
        player: LyrionPlayer,
        status: dict[str, Any],
    ) -> None:
        """
        Apply a normalized player status payload through the CometD adapter.

        :param player: Target Lyrion player.
        :param status: LMS player status payload.
        """
        self._cometd_adapter.apply_status(player, status)

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
        return await self._rpc_request(player_id=player_id, command=command)

    def get_configured_host(self) -> str | None:
        """Return configured host from setup data with config fallback."""
        raw_host = self.get_setup_value(CONF_LMS_HOST)
        if not isinstance(raw_host, str):
            return None
        host = raw_host.strip()
        return host or None

    def get_configured_port(
        self,
        default: int | None = DEFAULT_LMS_PORT,
    ) -> int | None:
        """Return configured port from setup data with config fallback."""
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
        """
        Execute one LMS JSON-RPC request.

        :param player_id: LMS player id.
            Empty string for server-level commands.
        :param command: LMS command list.
        """
        host = self._get_configured_host()
        if not host:
            raise ProviderUnavailableError("Lyrion host is not configured")
        port = self._get_configured_port()
        payload = {
            "id": 1,
            "method": "slim.request",
            "params": [player_id, command],
        }
        url = f"http://{host}:{port}/jsonrpc.js"
        self.logger.debug(
            "Lyrion RPC request to %s:%s with command %s",
            host,
            port,
            command,
        )

        last_error: Exception | None = None
        for attempt in range(2):
            try:
                async with self.mass.http_session.post(
                    url,
                    json=payload,
                    timeout=ClientTimeout(total=RPC_TIMEOUT),
                ) as response:
                    response.raise_for_status()
                    data = cast("dict[str, Any]", await response.json())
                break
            except (
                ServerDisconnectedError,
                ClientError,
                TimeoutError,
                ValueError,
            ) as err:
                last_error = err
                if attempt == 0 and isinstance(err, ServerDisconnectedError):
                    self.logger.debug(
                        ("Retrying Lyrion RPC request to %s:%s after disconnect"),
                        host,
                        port,
                    )
                    continue
                raise ProviderUnavailableError(
                    f"Lyrion JSON-RPC request to {host}:{port} failed: {err}"
                ) from err

        assert last_error is None or isinstance(data, dict)

        result = cast("dict[str, Any] | None", data.get("result"))
        if result is None:
            raise ProviderUnavailableError("Lyrion JSON-RPC response is missing result payload")
        return result

    def _get_configured_host(self) -> str | None:
        """Backward-compatible wrapper for internal host access."""
        return self.get_configured_host()

    def _get_configured_port(
        self,
        default: int | None = DEFAULT_LMS_PORT,
    ) -> int | None:
        """Backward-compatible wrapper for internal port access."""
        return self.get_configured_port(default)
