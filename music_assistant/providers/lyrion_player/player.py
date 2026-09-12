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
        self.lyrion_server = provider.lyrion_server
        self.player_control = self.lyrion_server.get_player_control(player_id)
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
            status = await self.provider._get_player_status(self.player_id)
        except ProviderUnavailableError as err:
            raise PlayerCommandFailed(f"Unable to poll player {self.player_id}: {err}") from err
        self.provider.apply_status_update(self, status)

    async def play_media(self, media: PlayerMedia) -> None:
        """
        Play media on LMS.

        :param media: The media item to play.
        """
        try:
            await self._play_or_enqueue_media(media, command="load")
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
        try:
            await self._play_or_enqueue_media(media, command="add")
        except ProviderUnavailableError as err:
            raise PlayerCommandFailed(f"enqueue_next_media failed: {err}") from err

    async def play(self) -> None:
        """Resume playback."""
        try:
            await self.player_control.play()
        except ProviderUnavailableError as err:
            raise PlayerCommandFailed(f"play failed: {err}") from err
        self._attr_playback_state = PlaybackState.PLAYING
        self.update_state()

    async def pause(self) -> None:
        """Pause playback."""
        try:
            await self.player_control.pause()
        except ProviderUnavailableError as err:
            raise PlayerCommandFailed(f"pause failed: {err}") from err
        self._attr_playback_state = PlaybackState.PAUSED
        self.update_state()

    async def stop(self) -> None:
        """Stop playback."""
        try:
            await self.player_control.stop()
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
            await self.player_control.set_power(powered)
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
            await self.player_control.set_volume(volume_level)
        except ProviderUnavailableError as err:
            raise PlayerCommandFailed(f"volume_set failed: {err}") from err
        self._attr_volume_level = max(0, min(100, volume_level))
        self.update_state()

    async def volume_mute(self, muted: bool) -> None:
        """Mute or unmute playback volume."""
        try:
            await self._ensure_sync_volume_disabled()
            await self.player_control.set_muted(muted)
        except ProviderUnavailableError as err:
            raise PlayerCommandFailed(f"volume_mute failed: {err}") from err
        self._attr_volume_muted = muted
        self.update_state()

    async def next_track(self) -> None:
        """Skip to the next track on the active LMS queue/source."""
        try:
            await self.player_control.next_track()
        except ProviderUnavailableError as err:
            raise PlayerCommandFailed(f"next_track failed: {err}") from err

    async def previous_track(self) -> None:
        """Skip to the previous track on the active LMS queue/source."""
        try:
            await self.player_control.previous_track()
        except ProviderUnavailableError as err:
            raise PlayerCommandFailed(f"previous_track failed: {err}") from err

    async def seek(self, position: int) -> None:
        """Seek playback position in seconds on the active source."""
        target = max(0, int(position))
        try:
            await self.player_control.seek(target)
        except ProviderUnavailableError as err:
            raise PlayerCommandFailed(f"seek failed: {err}") from err
        self._attr_elapsed_time = float(target)
        self._attr_elapsed_time_last_updated = time.time()
        self.update_state()

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
                await self.lyrion_server.get_player_control(member_id).unsync()
                touched_members.add(member_id)
                current_members.pop(member_id, None)

            for member_id in dict.fromkeys(player_ids_to_add or []):
                if member_id == self.player_id or member_id in current_members:
                    continue
                await self.lyrion_server.get_player_control(member_id).sync_to(self.player_id)
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

    async def _play_or_enqueue_media(
        self,
        media: PlayerMedia,
        command: str,
    ) -> None:
        """Route media to LMS-native track_id flow first, else MA stream URL."""
        if await self._try_play_lyrion_track_id(media, command=command):
            if command == "add":
                await self._queue_sync.sync_ma_queue_to_lms()
            return

        stream_url = await self.mass.streams.resolve_stream_url(
            self.player_id,
            media,
        )
        if command == "load":
            await self.player_control.play_url(stream_url)
            return
        await self.player_control.append_url(stream_url)

    async def _try_play_lyrion_track_id(
        self,
        media: PlayerMedia,
        command: str = "load",
    ) -> bool:
        """Try LMS-native track_id queue load/add and return whether it succeeded."""
        if command not in {"load", "add"}:
            return False
        if not media.uri:
            return False

        lms_entry = await self._queue_sync.resolve_ma_uri_to_lms_entry(media.uri)
        if lms_entry is None or lms_entry.kind != "track_id":
            return False

        try:
            await self.player_control.add_track_id_to_queue(
                lms_entry.value,
                command=command,
            )
            if command == "load":
                # Ensure play_media always starts transport immediately.
                await self.player_control.play()
        except ProviderUnavailableError as err:
            self.logger.warning(
                "Native LMS queue %s failed for track_id %s, fall back to URL: %s",
                command,
                lms_entry.value,
                err,
            )
            return False
        return True

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
        await self.lyrion_server.set_player_sync_volume(self.player_id, False)

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


def _get_status_str(status: dict[str, Any], key: str) -> str | None:
    """Read a status field as a non-empty string when available."""
    value = status.get(key)
    if isinstance(value, str):
        return value
    if value is None:
        return None
    return str(value)


def _get_status_int(status: dict[str, Any], key: str) -> int | None:
    """Read a status field as integer when available."""
    value = status.get(key)
    if value is None:
        return None
    try:
        return int(cast("int | str", value))
    except TypeError, ValueError:
        return None


def _time_matches_target(status: dict[str, Any], target: int) -> bool:
    """Return True when reported playback time is close to target seconds."""
    value = status.get("time")
    if value is None:
        return False
    try:
        return abs(float(cast("int | float | str", value)) - float(target)) <= 1.0
    except TypeError, ValueError:
        return False


def _group_members_match_leader(status: dict[str, Any], player_id: str) -> bool:
    """Return True when status reflects the player as group leader/member."""
    sync_master = _get_status_str(status, "sync_master")
    if sync_master and sync_master not in ("-", player_id):
        return True

    sync_slaves = _get_status_str(status, "sync_slaves")
    if sync_slaves:
        members = [part.strip() for part in sync_slaves.split(",") if part.strip()]
        return player_id in members or bool(members)

    return sync_master == player_id
