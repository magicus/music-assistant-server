"""MA adapter for the shared pylyrion CometD stream core."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiohttp import ClientError, ClientTimeout
from music_assistant_models.errors import MusicAssistantError, ProviderUnavailableError

from music_assistant.providers.lyrion.client import build_lms_url
from pylyrion.cometd.helpers import LmsPlayerEventCallback
from pylyrion.cometd.player_status_events import (
    NormalizedPlayerStatusEvent,
    PlayerPlaybackChanged,
    PlayerPlaylistChanged,
    PlayerPowerChanged,
    PlayerRepeatChanged,
    PlayerSeeked,
    PlayerShuffleChanged,
    PlayerStatusUpdated,
    PlayerVolumeChanged,
)
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
        """Map normalized pylyrion events to MA event classes and emit them."""
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

        for event in events:
            if isinstance(event, PlayerStatusUpdated):
                await self._emit_event(
                    LmsPlayerStatusUpdatedEvent(
                        player_id=event.player_id,
                        status=dict(event.status),
                        is_initial=event.is_initial,
                    )
                )
            elif isinstance(event, PlayerPlaybackChanged):
                await self._emit_event(
                    LmsPlayerPlaybackChangedEvent(
                        player_id=event.player_id,
                        old_mode=event.old_mode,
                        new_mode=event.new_mode,
                    )
                )
            elif isinstance(event, PlayerPowerChanged):
                await self._emit_event(
                    LmsPlayerPowerChangedEvent(
                        player_id=event.player_id,
                        old_powered=event.old_powered,
                        new_powered=event.new_powered,
                    )
                )
            elif isinstance(event, PlayerVolumeChanged):
                await self._emit_event(
                    LmsPlayerVolumeChangedEvent(
                        player_id=event.player_id,
                        old_volume=event.old_volume,
                        new_volume=event.new_volume,
                    )
                )
            elif isinstance(event, PlayerRepeatChanged):
                await self._emit_event(
                    LmsPlayerRepeatChangedEvent(
                        player_id=event.player_id,
                        old_repeat=event.old_repeat,
                        new_repeat=event.new_repeat,
                    )
                )
            elif isinstance(event, PlayerShuffleChanged):
                await self._emit_event(
                    LmsPlayerShuffleChangedEvent(
                        player_id=event.player_id,
                        old_shuffle=event.old_shuffle,
                        new_shuffle=event.new_shuffle,
                    )
                )
            elif isinstance(event, PlayerSeeked):
                await self._emit_event(
                    LmsPlayerSeekedEvent(
                        player_id=event.player_id,
                        old_time=event.old_time,
                        new_time=event.new_time,
                    )
                )
            elif isinstance(event, PlayerPlaylistChanged):
                await self._emit_event(
                    LmsPlayerPlaylistChangedEvent(
                        player_id=event.player_id,
                        old_playlist_timestamp=event.old_playlist_timestamp,
                        new_playlist_timestamp=event.new_playlist_timestamp,
                        old_playlist_tracks=event.old_playlist_tracks,
                        new_playlist_tracks=event.new_playlist_tracks,
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
