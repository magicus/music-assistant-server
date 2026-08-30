"""
Lyrion player implementation.

The player keeps a strict split between LMS-native and external MA content.
Native LMS tracks are mirrored by LMS track_id so they stay native in LMS.
External MA tracks are mirrored by a playable URL so LMS can still queue them.
Queue sync must therefore handle mixed queues explicitly in both directions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from music_assistant_models.enums import EventType, IdentifierType, PlaybackState, PlayerFeature
from music_assistant_models.errors import PlayerCommandFailed, ProviderUnavailableError
from music_assistant_models.player import DeviceInfo

from music_assistant.helpers.util import is_valid_mac_address
from music_assistant.models.player import Player, PlayerMedia

from .queue import LyrionQueueSync

if TYPE_CHECKING:
    from music_assistant_models.event import MassEvent

    from .provider import LyrionPlayerProvider


class LyrionPlayer(Player):
    """A single LMS-managed player exposed as an MA player."""

    provider: LyrionPlayerProvider  # type: ignore[misc]

    def __init__(
        self,
        provider: LyrionPlayerProvider,
        player_id: str,
        initial_data: dict[str, Any],
    ) -> None:
        """
        Initialize LyrionPlayer.

        :param provider: Lyrion player provider instance.
        :param player_id: LMS player id.
        :param initial_data: Player metadata returned by LMS players call.
        """
        super().__init__(provider, player_id)
        self._attr_supported_features = {
            PlayerFeature.PLAY_MEDIA,
            PlayerFeature.ENQUEUE,
            PlayerFeature.PAUSE,
            PlayerFeature.POWER,
            PlayerFeature.VOLUME_SET,
        }
        self._attr_available = True
        self._queue_sync = LyrionQueueSync(self)
        self._on_unload_callbacks.append(
            self.mass.subscribe(
                self._on_ma_queue_items_updated,
                EventType.QUEUE_ITEMS_UPDATED,
                id_filter=self.player_id,
            )
        )
        self._on_unload_callbacks.append(
            self.mass.subscribe(
                self._on_ma_queue_updated,
                EventType.QUEUE_UPDATED,
                id_filter=self.player_id,
            )
        )
        self._apply_player_metadata(initial_data)

    @property
    def needs_poll(self) -> bool:
        """Return False because runtime sync is primarily CometD-driven."""
        return False

    @property
    def poll_interval(self) -> int:
        """Return state poll interval in seconds."""
        return 5 if self._attr_playback_state == PlaybackState.PLAYING else 20

    async def poll(self) -> None:
        """Poll runtime status from LMS as fallback when explicitly invoked."""
        try:
            status = await self.provider.get_player_status(self.player_id)
        except ProviderUnavailableError as err:
            raise PlayerCommandFailed(f"Unable to poll player {self.player_id}: {err}") from err
        self.provider.apply_status_update(self, status)

    async def play_media(self, media: PlayerMedia) -> None:
        """
        Play media on LMS.

        :param media: The media item to play.
        """
        # If this media originates from a Lyrion music provider instance
        # on the same LMS, load by track_id in LMS so native clients show
        # regular queue metadata/items.
        if await self._queue_sync.try_play_lyrion_track_id(media):
            self._attr_current_media = media
            self._attr_playback_state = PlaybackState.PLAYING
            self.update_state()
            return

        stream_url = await self.mass.streams.resolve_stream_url(
            self.player_id,
            media,
        )
        try:
            await self.provider.send_player_command(
                self.player_id, ["playlist", "play", stream_url]
            )
        except ProviderUnavailableError as err:
            raise PlayerCommandFailed(f"play_media failed: {err}") from err

        self._attr_current_media = media
        self._attr_playback_state = PlaybackState.PLAYING
        self.update_state()

    async def enqueue_next_media(self, media: PlayerMedia) -> None:
        """
        Enqueue the next media item and refresh the LMS queue mirror from MA.

        :param media: The media item to enqueue.
        """
        if await self._queue_sync.try_play_lyrion_track_id(
            media,
            command="add",
        ):
            await self._queue_sync.sync_ma_queue_to_lms(
                allow_uninitialized=True,
            )
            return

        stream_url = await self.mass.streams.resolve_stream_url(
            self.player_id,
            media,
        )
        try:
            await self.provider.send_player_command(
                self.player_id,
                ["playlist", "add", stream_url],
            )
        except ProviderUnavailableError as err:
            raise PlayerCommandFailed(f"enqueue_next_media failed: {err}") from err

    async def play(self) -> None:
        """Resume playback."""
        try:
            await self.provider.send_player_command(self.player_id, ["play"])
        except ProviderUnavailableError as err:
            raise PlayerCommandFailed(f"play failed: {err}") from err
        self._attr_playback_state = PlaybackState.PLAYING
        self.update_state()

    async def pause(self) -> None:
        """Pause playback."""
        try:
            await self.provider.send_player_command(
                self.player_id,
                ["pause", 1],
            )
        except ProviderUnavailableError as err:
            raise PlayerCommandFailed(f"pause failed: {err}") from err
        self._attr_playback_state = PlaybackState.PAUSED
        self.update_state()

    async def stop(self) -> None:
        """Stop playback."""
        try:
            await self.provider.send_player_command(self.player_id, ["stop"])
        except ProviderUnavailableError as err:
            raise PlayerCommandFailed(f"stop failed: {err}") from err
        self._attr_playback_state = PlaybackState.IDLE
        self._attr_current_media = None
        self.update_state()

    async def power(self, powered: bool) -> None:
        """
        Set player power state.

        :param powered: True to power on, False to power off.
        """
        try:
            await self.provider.send_player_command(
                self.player_id,
                ["power", 1 if powered else 0],
            )
        except ProviderUnavailableError as err:
            raise PlayerCommandFailed(f"power failed: {err}") from err
        self._attr_powered = powered
        self.update_state()

    async def volume_set(self, volume_level: int) -> None:
        """
        Set player volume.

        :param volume_level: Volume level from 0 to 100.
        """
        try:
            await self.provider.send_player_command(
                self.player_id,
                ["mixer", "volume", max(0, min(100, volume_level))],
            )
        except ProviderUnavailableError as err:
            raise PlayerCommandFailed(f"volume_set failed: {err}") from err
        self._attr_volume_level = max(0, min(100, volume_level))
        self.update_state()

    async def sync_queue_from_lms(self) -> None:
        """Refresh MA queue mirror from LMS queue state via JSON-RPC."""
        await self._queue_sync.sync_lms_queue_to_ma()

    async def sync_from_lms(self, player_data: dict[str, Any]) -> None:
        """
        Update static metadata from LMS discovery payload.

        :param player_data: Player metadata from players_loop.
        """
        self._apply_player_metadata(player_data)
        self.update_state()

    async def _on_ma_queue_items_updated(self, event: MassEvent) -> None:
        """
        Mirror queue item changes from MA to LMS.

        :param event: Queue items updated event.
        """
        if event.object_id != self.player_id or self._queue_sync.syncing_from_lms_queue:
            return
        await self._queue_sync.sync_ma_queue_to_lms()

    async def _on_ma_queue_updated(self, event: MassEvent) -> None:
        """
        Mirror queue cursor/playhead changes from MA to LMS.

        :param event: Queue updated event.
        """
        if event.object_id != self.player_id or self._queue_sync.syncing_from_lms_queue:
            return
        await self._queue_sync.sync_ma_queue_to_lms(sync_items=False)

    def _apply_player_metadata(self, player_data: dict[str, Any]) -> None:
        """
        Map LMS player metadata to MA player attributes.

        :param player_data: Player metadata from players_loop.
        """
        self._attr_name = cast(
            "str",
            player_data.get("name") or self.player_id,
        )
        model = cast("str | None", player_data.get("model"))
        self._attr_device_info = DeviceInfo(
            manufacturer="Lyrion",
            model=model or "Unknown",
        )
        # Do not add IP as matching identifier for LMS players:
        # multiple LMS players can legitimately report the same host IP
        # (bridge/server address), which may cause protocol auto-linking
        # to route playback through the wrong output protocol.
        if is_valid_mac_address(self.player_id):
            self._attr_device_info.add_identifier(
                IdentifierType.MAC_ADDRESS,
                self.player_id,
            )
