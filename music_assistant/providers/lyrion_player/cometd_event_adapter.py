"""Bridge internal Lyrion CometD events into MA player updates."""

from __future__ import annotations

import time
from contextlib import suppress
from typing import TYPE_CHECKING

from music_assistant_models.enums import PlaybackState

from .cometd_events import (
    LmsPlayerEvent,
    LmsPlayerPlaylistChangedEvent,
    LmsPlayerRepeatChangedEvent,
    LmsPlayerShuffleChangedEvent,
    LmsPlayerStatusUpdatedEvent,
)
from .player import LyrionPlayer

if TYPE_CHECKING:
    from .cometd_events import StatusPayload
    from .provider import LyrionPlayerProvider


MODE_MAP = {
    "play": PlaybackState.PLAYING,
    "pause": PlaybackState.PAUSED,
    "stop": PlaybackState.IDLE,
}


class LyrionCometDEventAdapter:
    """Translate normalized LMS events to concrete MA updates."""

    def __init__(self, provider: LyrionPlayerProvider) -> None:
        """
        Initialize event adapter.

        :param provider: Owning Lyrion provider instance.
        """
        self.provider = provider

    async def handle_event(self, event: LmsPlayerEvent) -> None:
        """
        Apply one internal event to MA state.

        :param event: Normalized internal LMS event.
        """
        player = self.provider.mass.players.get_player(event.player_id)
        if not isinstance(player, LyrionPlayer):
            return

        if isinstance(event, LmsPlayerStatusUpdatedEvent):
            self.apply_status(player, event.status)
            if event.is_initial:
                await player.sync_queue_from_lms()

        if isinstance(event, LmsPlayerPlaylistChangedEvent):
            await player.sync_queue_from_lms()

        if isinstance(
            event,
            (LmsPlayerRepeatChangedEvent, LmsPlayerShuffleChangedEvent),
        ):
            await player.sync_queue_from_lms()

    def apply_status(
        self,
        player: LyrionPlayer,
        status: StatusPayload,
    ) -> None:
        """
        Apply one normalized status payload to MA runtime state.

        :param player: Target MA player.
        :param status: Merged playerstatus payload from LMS CometD stream.
        """
        player._attr_available = True

        mode = str(status.get("mode") or "stop")
        player._attr_playback_state = MODE_MAP.get(mode, PlaybackState.IDLE)

        if "power" in status:
            with suppress(TypeError, ValueError):
                player._attr_powered = bool(int(status["power"]))

        if "mixer volume" in status:
            with suppress(TypeError, ValueError):
                player._attr_volume_level = max(
                    0,
                    min(100, int(status["mixer volume"])),
                )

        if "time" in status:
            with suppress(TypeError, ValueError):
                player._attr_elapsed_time = float(status["time"])
                player._attr_elapsed_time_last_updated = time.time()

        player.update_state()
