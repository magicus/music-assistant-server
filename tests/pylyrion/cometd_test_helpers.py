"""Test-only CometD stream helpers for pylyrion-facing coverage."""

from __future__ import annotations

from typing import Any

from aiohttp import ClientError, ClientTimeout

from pylyrion.cometd.helpers import LmsPlayerEventCallback
from pylyrion.cometd.player_status_events import NormalizedPlayerStatusEvent
from pylyrion.cometd.stream_core import CometDEventStreamCore
from pylyrion.errors import LyrionError, LyrionRequestError
from pylyrion.session import build_lms_url


class LyrionCometDTestStream(CometDEventStreamCore):
    """Test-only bridge from pylyrion events to runtime callbacks."""

    _runtime_context: Any

    def __init__(
        self,
        runtime_context: Any,
        event_callback: LmsPlayerEventCallback,
    ) -> None:
        """Initialize stream wrapper with runtime context and callback."""
        schedule_players_discovery = getattr(
            runtime_context,
            "schedule_players_discovery",
            lambda: None,
        )

        def _get_runtime_player_ids() -> set[str]:
            players = getattr(runtime_context, "players", ())
            return {player.player_id for player in players if getattr(player, "player_id", None)}

        super().__init__(
            recoverable_errors=(LyrionRequestError,),
            should_stop=lambda: runtime_context.unloading,
            logger=runtime_context.logger,
            get_player_status=runtime_context.get_player_status,
            get_initial_player_ids=_get_runtime_player_ids,
            get_current_player_ids=_get_runtime_player_ids,
            schedule_players_discovery=schedule_players_discovery,
            apply_server_player_connection_state=lambda _payload: None,
        )
        self._runtime_context = runtime_context
        self._event_callback = event_callback

    async def _emit_normalized_player_events(
        self,
        events: list[NormalizedPlayerStatusEvent],
    ) -> None:
        for event in events:
            await self._emit_event(event)

    async def _emit_event(self, event: object) -> None:
        try:
            await self._event_callback(event)
        except LyrionError as err:
            self._runtime_context.logger.warning(
                "Status event handling failed for %s: %s",
                getattr(event, "player_id", "unknown"),
                err,
            )

    async def _post(
        self,
        messages: list[dict[str, object]],
        timeout: int,
    ) -> list[dict[str, object]]:
        host = self._runtime_context.get_configured_host()
        if not host:
            raise LyrionRequestError("Lyrion host is not configured")
        port = self._runtime_context.get_configured_port()
        if port is None:
            raise LyrionRequestError("Lyrion port is not configured")

        url = build_lms_url(host, port, "/cometd")
        try:
            async with self._runtime_context.http_session.post(
                url,
                json=messages,
                timeout=ClientTimeout(total=timeout),
            ) as response:
                response.raise_for_status()
                payload = await response.json()
        except (ClientError, TimeoutError, ValueError) as err:
            raise LyrionRequestError(f"CometD request to {host}:{port} failed: {err}") from err

        if isinstance(payload, dict):
            return [payload]
        if not isinstance(payload, list):
            raise LyrionRequestError("CometD response must be a JSON object or list")
        return [message for message in payload if isinstance(message, dict)]
