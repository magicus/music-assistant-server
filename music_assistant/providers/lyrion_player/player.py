"""
Lyrion player implementation.

The player keeps a strict split between LMS-native and external MA content.
Native LMS tracks are mirrored by LMS track_id so they stay native in LMS.
External MA tracks are mirrored by a playable URL so LMS can still queue them.
Queue sync must therefore handle mixed queues explicitly in both directions.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, cast

from music_assistant_models.enums import EventType, IdentifierType, PlaybackState
from music_assistant_models.errors import (
    InvalidCommand,
    PlayerCommandFailed,
    ProviderUnavailableError,
)
from music_assistant_models.player import DeviceInfo

from music_assistant.helpers.util import is_valid_mac_address
from music_assistant.models.player import Player, PlayerMedia

from .constants import (
    COMETD_COMMAND_STATUS_VERIFY_TIMEOUT,
    CONF_FALLBACK_POLLING,
    CONF_FALLBACK_POLLING_INTERVAL,
    DEFAULT_FALLBACK_POLLING_INTERVAL,
    PLAYER_SUPPORTED_FEATURES,
)
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
        self._attr_supported_features = PLAYER_SUPPORTED_FEATURES
        self._attr_available = True
        self._attr_can_group_with = {provider.instance_id}
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
        """Return whether MA should periodically poll this player for state."""
        return bool(
            self.provider.get_config_value(
                CONF_FALLBACK_POLLING,
                False,
                return_type=bool,
            )
        )

    @property
    def poll_interval(self) -> int:
        """Return state poll interval in seconds."""
        return int(
            self.provider.get_config_value(
                CONF_FALLBACK_POLLING_INTERVAL,
                DEFAULT_FALLBACK_POLLING_INTERVAL,
                return_type=int,
            )
        )

    async def poll(self) -> None:
        """Poll runtime status from LMS when explicitly invoked."""
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
            await self._queue_sync.sync_ma_queue_to_lms()
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
        baseline = self.provider.get_last_cometd_status_seen_at(self.player_id)
        try:
            await self.provider.send_player_command(self.player_id, ["play"])
        except ProviderUnavailableError as err:
            raise PlayerCommandFailed(f"play failed: {err}") from err
        self._attr_playback_state = PlaybackState.PLAYING
        self.update_state()
        await self._verify_cometd_status_update(baseline)

    async def pause(self) -> None:
        """Pause playback."""
        baseline = self.provider.get_last_cometd_status_seen_at(self.player_id)
        try:
            await self.provider.send_player_command(
                self.player_id,
                ["pause", 1],
            )
        except ProviderUnavailableError as err:
            raise PlayerCommandFailed(f"pause failed: {err}") from err
        self._attr_playback_state = PlaybackState.PAUSED
        self.update_state()
        await self._verify_cometd_status_update(baseline)

    async def stop(self) -> None:
        """Stop playback."""
        baseline = self.provider.get_last_cometd_status_seen_at(self.player_id)
        try:
            await self.provider.send_player_command(self.player_id, ["stop"])
        except ProviderUnavailableError as err:
            raise PlayerCommandFailed(f"stop failed: {err}") from err
        self._attr_playback_state = PlaybackState.IDLE
        self._attr_current_media = None
        self.update_state()
        await self._verify_cometd_status_update(baseline)

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
            await self._ensure_sync_volume_disabled()
            await self.provider.send_player_command(
                self.player_id,
                ["mixer", "volume", max(0, min(100, volume_level))],
            )
        except ProviderUnavailableError as err:
            raise PlayerCommandFailed(f"volume_set failed: {err}") from err
        self._attr_volume_level = max(0, min(100, volume_level))
        self.update_state()

    async def volume_mute(self, muted: bool) -> None:
        """Mute or unmute playback volume."""
        try:
            await self._ensure_sync_volume_disabled()
            await self.provider.send_player_command(
                self.player_id,
                ["mixer", "muting", 1 if muted else 0],
            )
        except ProviderUnavailableError as err:
            raise PlayerCommandFailed(f"volume_mute failed: {err}") from err
        self._attr_volume_muted = muted
        self.update_state()

    async def next_track(self) -> None:
        """Skip to the next track on the active LMS queue/source."""
        baseline = self.provider.get_last_cometd_status_seen_at(self.player_id)
        try:
            await self.provider.send_player_command(
                self.player_id,
                ["playlist", "index", "+1"],
            )
        except ProviderUnavailableError as err:
            raise PlayerCommandFailed(f"next_track failed: {err}") from err
        await self._verify_cometd_status_update(baseline)

    async def previous_track(self) -> None:
        """Skip to the previous track on the active LMS queue/source."""
        baseline = self.provider.get_last_cometd_status_seen_at(self.player_id)
        try:
            await self.provider.send_player_command(
                self.player_id,
                ["playlist", "index", "-1"],
            )
        except ProviderUnavailableError as err:
            raise PlayerCommandFailed(f"previous_track failed: {err}") from err
        await self._verify_cometd_status_update(baseline)

    async def seek(self, position: int) -> None:
        """Seek playback position in seconds on the active source."""
        target = max(0, int(position))
        baseline = self.provider.get_last_cometd_status_seen_at(self.player_id)
        try:
            await self.provider.send_player_command(
                self.player_id,
                ["time", target],
            )
        except ProviderUnavailableError as err:
            raise PlayerCommandFailed(f"seek failed: {err}") from err
        self._attr_elapsed_time = float(target)
        self._attr_elapsed_time_last_updated = time.time()
        self.update_state()
        await self._verify_cometd_status_update(baseline)

    async def set_members(
        self,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """Apply member changes through LMS native sync commands."""
        if self.synced_to:
            raise InvalidCommand("Player is synced, cannot set members")
        if not player_ids_to_add and not player_ids_to_remove:
            return

        touched_members: set[str] = set()
        current_members = dict.fromkeys(self._attr_group_members)

        try:
            for member_id in dict.fromkeys(player_ids_to_remove or []):
                if member_id == self.player_id:
                    continue
                if member_id not in current_members:
                    continue
                await self.provider.send_player_command(member_id, ["sync", "-"])
                touched_members.add(member_id)
                current_members.pop(member_id, None)

            for member_id in dict.fromkeys(player_ids_to_add or []):
                if member_id == self.player_id or member_id in current_members:
                    continue
                await self.provider.send_player_command(member_id, ["sync", self.player_id])
                touched_members.add(member_id)
                current_members[member_id] = None
        except ProviderUnavailableError as err:
            raise PlayerCommandFailed(f"set_members failed: {err}") from err

        other_members = [member_id for member_id in current_members if member_id != self.player_id]
        self._attr_group_members = [self.player_id, *other_members] if other_members else []
        self.update_state()

        for member_id in touched_members:
            if member := self.mass.players.get_player(member_id):
                member.update_state()

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

    async def _verify_cometd_status_update(self, baseline: float | None) -> None:
        """Poll LMS when CometD does not confirm a recent command quickly."""
        if await self.provider.wait_for_cometd_status_update(
            self.player_id,
            baseline,
            COMETD_COMMAND_STATUS_VERIFY_TIMEOUT,
        ):
            return

        self.provider.logger.warning(
            "No CometD status update for %s within %ss; polling LMS",
            self.player_id,
            COMETD_COMMAND_STATUS_VERIFY_TIMEOUT,
        )
        try:
            status = await self.provider.get_player_status(self.player_id)
        except ProviderUnavailableError as err:
            self.provider.logger.warning(
                "Fallback status poll failed for %s: %s",
                self.player_id,
                err,
            )
            return

        self.provider.apply_status_update(self, status)

    async def _on_ma_queue_items_updated(self, event: MassEvent) -> None:
        """
        Mirror queue item changes from MA to LMS.

        :param event: Queue items updated event.
        """
        if event.object_id != self.player_id:
            return
        await self._queue_sync.sync_ma_queue_to_lms()

    async def _on_ma_queue_updated(self, event: MassEvent) -> None:
        """
        Mirror queue cursor/playhead changes from MA to LMS.

        :param event: Queue updated event.
        """
        if event.object_id != self.player_id:
            return
        await self._queue_sync.sync_ma_queue_to_lms(sync_items=False)

    async def _ensure_sync_volume_disabled(self) -> None:
        """Disable LMS syncVolume before MA volume changes in sync."""
        if not (self.synced_to or self.group_members):
            return
        # LMS' native syncVolume flattens the whole sync domain to one
        # absolute level. That is the opposite of MA's group-volume
        # behavior, which preserves each member's relative balance by
        # adjusting them individually. Material Skin ships its own custom
        # group-volume flow for the same reason: stock LMS syncVolume is
        # too blunt for the group-volume UX users typically expect.
        await self.provider.send_player_command(
            self.player_id,
            ["playerpref", "syncVolume", 0],
        )

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
