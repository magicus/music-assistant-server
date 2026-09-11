"""MA adapter for the shared pylyrion CometD stream core."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiohttp import ClientError, ClientTimeout
from music_assistant_models.errors import MusicAssistantError, ProviderUnavailableError

from music_assistant.providers.lyrion.client import build_lms_url
from pylyrion.cometd.helpers import LmsPlayerEventCallback
from pylyrion.cometd.player_status_events import NormalizedPlayerStatusEvent
from pylyrion.cometd.stream_core import CometDEventStreamCore
from pylyrion.errors import LyrionRequestError

if TYPE_CHECKING:
    from music_assistant.providers.lyrion_player.provider import LyrionPlayerProvider


class LyrionCometDEventStream(CometDEventStreamCore):
    """Thin MA wrapper that maps pylyrion events to MA event models."""

    provider: LyrionPlayerProvider

    def __init__(
        self,
        provider: LyrionPlayerProvider,
        event_callback: LmsPlayerEventCallback,
    ) -> None:
        """
        Initialize CometD event stream.

        :param provider: Owning Lyrion player provider instance.
        :param event_callback: Callback invoked for each MA CometD event.
        """
        super().__init__(
            provider=provider,
            recoverable_errors=(ProviderUnavailableError, LyrionRequestError),
        )
        self._event_callback = event_callback

    async def _emit_normalized_player_events(
        self,
        events: list[NormalizedPlayerStatusEvent],
    ) -> None:
        """Forward normalized pylyrion events to callback consumers."""
        for event in events:
            await self._emit_event(event)

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
