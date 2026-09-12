"""Test-only CometD stream helpers for pylyrion-facing coverage."""

from __future__ import annotations

from typing import Any

from aiohttp import ClientError, ClientTimeout
from music_assistant_models.errors import MusicAssistantError, ProviderUnavailableError

from pylyrion.cometd.helpers import LmsPlayerEventCallback
from pylyrion.cometd.player_status_events import NormalizedPlayerStatusEvent
from pylyrion.cometd.stream_core import CometDEventStreamCore
from pylyrion.errors import LyrionRequestError
from pylyrion.session import build_lms_url


class LyrionCometDEventStream(CometDEventStreamCore):
    """Test-only bridge from pylyrion events to MA-style callbacks."""

    provider: Any

    def __init__(
        self,
        provider: Any,
        event_callback: LmsPlayerEventCallback,
    ) -> None:
        """Initialize stream wrapper with provider and callback."""
        super().__init__(
            provider=provider,
            recoverable_errors=(ProviderUnavailableError, LyrionRequestError),
        )
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
        except MusicAssistantError as err:
            self.provider.logger.warning(
                "Status event handling failed for %s: %s",
                getattr(event, "player_id", "unknown"),
                err,
            )

    async def _post(
        self,
        messages: list[dict[str, object]],
        timeout: int,
    ) -> list[dict[str, object]]:
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
