"""Queue synchronization helpers for the Lyrion player."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from music_assistant_models.enums import PlaybackState, QueueOption, RepeatMode
from music_assistant_models.errors import MusicAssistantError, ProviderUnavailableError

from music_assistant.providers.lyrion_player.constants import (
    MAX_SYNC_QUEUE_ITEMS,
    SYNC_REBUILD_COST_THRESHOLD,
    SYNC_REBUILD_RATIO_THRESHOLD,
)
from music_assistant.providers.lyrion_player.media_mapper import LmsQueueEntry, LyrionMediaMapper

from .queue_planner import QueueDiffPlanner
from .queue_sync_engine import QueueSyncEngine, QueueSyncInput

if TYPE_CHECKING:
    from music_assistant_models.player import PlayerMedia
    from music_assistant_models.queue_item import QueueItem

    from music_assistant.providers.lyrion_player.player import LyrionPlayer


@dataclass(frozen=True)
class _LmsMirrorEntry:
    """One MA queue entry transformed for LMS queue mirroring."""

    kind: str
    value: str
    title: str | None = None
    artist: str | None = None
    album: str | None = None

    @property
    def identity(self) -> tuple[str, str]:
        """Return stable identity tuple used by diff/planning logic."""
        return (self.kind, self.value)


@dataclass(frozen=True)
class _MaQueueSnapshot:
    """MA queue state used as source/target in sync operations."""

    entries: tuple[_LmsMirrorEntry, ...]
    current_index: int
    shuffle_enabled: bool
    repeat_mode: RepeatMode
    protected_prefix_len: int


@dataclass(frozen=True)
class _LmsQueueSnapshot:
    """LMS queue state used as source/target in sync operations."""

    entries: tuple[_LmsMirrorEntry, ...]
    current_index: int
    shuffle_mode: int
    repeat_mode: int
    playback_mode: str


class LyrionQueueSync:
    """Own MA<->LMS queue mirroring and track mapping logic."""

    def __init__(self, player: LyrionPlayer) -> None:
        """
        Initialize queue sync helper.

        :param player: Owning Lyrion player instance.
        """
        self.player = player
        self._syncing_from_lms_queue = False
        self._syncing_to_lms_queue = False
        self._ma_model: _MaQueueSnapshot | None = None
        self._lms_model: _LmsQueueSnapshot | None = None
        self._lms_shuffle_mode_raw: int = 0
        self._identity_map: dict[int, tuple[str, str]] = {}
        self._media_mapper = LyrionMediaMapper(player)
        self._sync_engine = QueueSyncEngine[_LmsMirrorEntry](
            QueueDiffPlanner(
                rebuild_cost_threshold=SYNC_REBUILD_COST_THRESHOLD,
                rebuild_ratio_threshold=SYNC_REBUILD_RATIO_THRESHOLD,
            )
        )

    @property
    def syncing_from_lms_queue(self) -> bool:
        """Return whether LMS->MA queue sync is currently active."""
        return self._syncing_from_lms_queue

    async def sync_ma_queue_to_lms(
        self,
        sync_items: bool = True,
        allow_uninitialized: bool = False,
    ) -> None:
        """
        Mirror the current MA queue state to LMS for this player.

        :param sync_items: If False, only sync current queue index.
        :param allow_uninitialized: Deprecated compatibility flag.
        """
        if self._syncing_to_lms_queue or self._syncing_from_lms_queue:
            return

        _ = allow_uninitialized
        ma_snapshot = await self._collect_ma_snapshot()
        if not self._within_sync_limits(len(ma_snapshot.entries), "MA"):
            return

        self._syncing_to_lms_queue = True
        try:
            lms_snapshot = await self._collect_lms_snapshot()
            if lms_snapshot is None:
                return
            if not self._within_sync_limits(len(lms_snapshot.entries), "LMS"):
                return

            if not sync_items and self._entries_signature(
                ma_snapshot.entries
            ) != self._entries_signature(lms_snapshot.entries):
                sync_items = True

            if sync_items:
                await self._sync_ma_items_to_lms(ma_snapshot, lms_snapshot)

            await self._sync_ma_position_to_lms(ma_snapshot, lms_snapshot)
            await self._sync_ma_modes_to_lms(ma_snapshot, lms_snapshot)

            verified_lms = await self._collect_lms_snapshot()
            if verified_lms is None:
                return

            self._ma_model = ma_snapshot
            self._lms_model = verified_lms
            self._lms_shuffle_mode_raw = verified_lms.shuffle_mode
            self._identity_map = {
                index: entry.identity for index, entry in enumerate(verified_lms.entries)
            }
        except ProviderUnavailableError as err:
            self.player.logger.warning(
                "Unable to sync MA queue to LMS for player %s: %s",
                self.player.player_id,
                err,
            )
        finally:
            self._syncing_to_lms_queue = False

    async def sync_lms_queue_to_ma(self) -> None:
        """Mirror LMS playlist changes back into MA when tracks map cleanly."""
        if self._syncing_to_lms_queue:
            return

        lms_snapshot = await self._collect_lms_snapshot()
        if lms_snapshot is None:
            return
        if not self._within_sync_limits(len(lms_snapshot.entries), "LMS"):
            return
        if self._lms_model == lms_snapshot:
            return

        music_provider_instance = self._media_mapper.resolve_matching_music_provider_instance()
        if music_provider_instance is None:
            return

        self._syncing_from_lms_queue = True
        try:
            await self._apply_lms_queue_entries_to_ma(
                lms_snapshot.entries,
                music_provider_instance,
                lms_snapshot.current_index,
                lms_snapshot.playback_mode,
            )
            await self._apply_lms_modes_to_ma(lms_snapshot)
            ma_snapshot = await self._collect_ma_snapshot()
            self._ma_model = ma_snapshot
            self._lms_model = lms_snapshot
            self._lms_shuffle_mode_raw = lms_snapshot.shuffle_mode
            self._identity_map = {
                index: entry.identity for index, entry in enumerate(lms_snapshot.entries)
            }
        except MusicAssistantError as err:
            self.player.logger.debug(
                "Unable to mirror LMS queue to MA for %s: %s",
                self.player.player_id,
                err,
            )
        finally:
            self._syncing_from_lms_queue = False

    async def try_play_lyrion_track_id(
        self,
        media: PlayerMedia,
        command: str = "load",
    ) -> bool:
        """
        Try native LMS queue load for tracks from a matching Lyrion provider.

        :param media: Media payload received from MA queue controller.
        :param command: playlistcontrol command to execute, load or add.
        """
        if not media.uri:
            return False

        lms_entry = await self._media_mapper.resolve_ma_uri_to_lms_queue_entry(media.uri)
        if lms_entry is None or lms_entry.kind != "track_id":
            return False
        item_id = lms_entry.value

        if command not in {"load", "add"}:
            return False

        try:
            await self.player.provider.send_player_command(
                self.player.player_id,
                ["playlistcontrol", f"cmd:{command}", f"track_id:{item_id}"],
            )
            if command == "load":
                # Ensure play_media always starts transport immediately.
                await self.player.provider.send_player_command(
                    self.player.player_id,
                    ["play"],
                )
        except ProviderUnavailableError as err:
            self.player.logger.warning(
                ("Native LMS queue %s failed for track_id %s, fall back to URL: %s"),
                command,
                item_id,
                err,
            )
            return False
        return True

    async def rebuild_from(
        self,
        source_entries: tuple[_LmsMirrorEntry, ...],
        index: int,
    ) -> None:
        """Rebuild target LMS queue from index to end."""
        await self._rebuild_lms_tail(source_entries, index)

    async def delete_index(self, index: int) -> None:
        """Delete one LMS queue entry by index."""
        await self._delete_lms_index(index)

    async def move_index(self, from_index: int, to_index: int) -> None:
        """Move one LMS queue entry by index."""
        await self._move_lms_index(from_index, to_index)

    async def insert_entry_at(
        self,
        entry: _LmsMirrorEntry,
        index: int,
    ) -> None:
        """Insert one LMS entry at index using append+move."""
        await self._insert_lms_entry_at(entry, index)

    async def _sync_ma_items_to_lms(
        self,
        ma_snapshot: _MaQueueSnapshot,
        lms_snapshot: _LmsQueueSnapshot,
    ) -> None:
        """Apply queue item sync from MA source to LMS target."""
        await self._sync_engine.apply(
            QueueSyncInput(
                source_entries=ma_snapshot.entries,
                source_identities=tuple(entry.identity for entry in ma_snapshot.entries),
                target_identities=tuple(entry.identity for entry in lms_snapshot.entries),
                protected_prefix_len=ma_snapshot.protected_prefix_len,
            ),
            self,
        )

    def _parse_lms_queue_state(
        self,
        status: dict[str, Any],
    ) -> _LmsQueueSnapshot | None:
        """Parse LMS status into normalized snapshot."""
        playlist_items = cast(
            "list[Any]",
            status.get("playlist_loop", []),
        )
        lms_queue_entries: list[_LmsMirrorEntry] = []
        for item in playlist_items:
            if not isinstance(item, dict):
                return None
            entry = self._media_mapper.extract_lms_entry_from_playlist_item(item)
            if entry is not None:
                lms_queue_entries.append(
                    _LmsMirrorEntry(
                        kind=entry.kind,
                        value=entry.value,
                    )
                )
                continue
            return None

        current_index = self._parse_status_int(status, ("playlist_cur_index", "playlist index"), 0)
        shuffle_mode = self._parse_status_int(status, ("playlist shuffle", "playlist_shuffle"), 0)
        repeat_mode = self._parse_status_int(status, ("playlist repeat", "playlist_repeat"), 0)
        playback_mode = cast("str", status.get("mode") or "stop")
        return _LmsQueueSnapshot(
            entries=tuple(lms_queue_entries),
            current_index=current_index,
            shuffle_mode=shuffle_mode,
            repeat_mode=repeat_mode,
            playback_mode=playback_mode,
        )

    async def _apply_lms_queue_entries_to_ma(
        self,
        lms_queue_entries: tuple[_LmsMirrorEntry, ...],
        music_provider_instance: str,
        current_index: int,
        mode: str,
    ) -> None:
        """Replace MA queue with LMS entries and restore active index."""
        if not lms_queue_entries:
            self.player.mass.player_queues.clear(
                self.player.player_id,
                skip_stop=True,
            )
            return

        uris = [
            self._media_mapper.lms_queue_entry_to_ma_uri(
                LmsQueueEntry(kind=entry.kind, value=entry.value),
                music_provider_instance,
            )
            for entry in lms_queue_entries
        ]
        self.player.mass.player_queues.clear(
            self.player.player_id,
            skip_stop=True,
        )
        await self.player.mass.player_queues.play_media(
            queue_id=self.player.player_id,
            media=cast("list[Any]", uris),
            option=QueueOption.ADD,
        )
        if 0 <= current_index < len(uris) and mode in {"play", "pause"}:
            await self.player.mass.player_queues.play_index(
                self.player.player_id,
                current_index,
            )
            if mode == "pause":
                await self.player.mass.player_queues.pause(self.player.player_id)

    async def _collect_ma_lms_queue_entries(
        self,
    ) -> tuple[_LmsMirrorEntry, ...]:
        """
        Collect queue entries that LMS can represent as track_id or URL.

        Non-native items are mirrored as a local MA redirect URL that resolves
        to a fresh stream target only when LMS starts playback.
        """
        queue_items = self.player.mass.player_queues.items(self.player.player_id)
        if not queue_items:
            return ()

        queue_entries: list[_LmsMirrorEntry] = []
        for item in queue_items:
            item_uri = item.uri
            title, artist, album = self._extract_queue_item_metadata(item)
            lms_entry = None
            if item_uri:
                lms_entry = await self._resolve_ma_uri_to_lms_entry(item_uri)
            if lms_entry is not None:
                entry_value = lms_entry.value
                if lms_entry.kind == "url":
                    entry_value = self.player.provider.build_stream_redirect_url(
                        player_id=self.player.player_id,
                        queue_id=item.queue_id,
                        queue_item_id=item.queue_item_id,
                        ma_uri=item_uri,
                    )
                queue_entries.append(
                    _LmsMirrorEntry(
                        kind=lms_entry.kind,
                        value=entry_value,
                        title=title,
                        artist=artist,
                        album=album,
                    )
                )
                continue

            queue_entries.append(
                _LmsMirrorEntry(
                    kind="url",
                    value=self.player.provider.build_stream_redirect_url(
                        player_id=self.player.player_id,
                        queue_id=item.queue_id,
                        queue_item_id=item.queue_item_id,
                        ma_uri=item_uri,
                    ),
                    title=title,
                    artist=artist,
                    album=album,
                )
            )
        return tuple(queue_entries)

    async def _collect_ma_snapshot(self) -> _MaQueueSnapshot:
        """Return one normalized MA queue snapshot for this player."""
        queue = self.player.mass.player_queues.get(self.player.player_id)
        if queue is None:
            return _MaQueueSnapshot(
                entries=(),
                current_index=0,
                shuffle_enabled=False,
                repeat_mode=RepeatMode.OFF,
                protected_prefix_len=0,
            )

        entries = await self._collect_ma_lms_queue_entries()
        current_index = int(queue.current_index or 0)
        protected_prefix_len = self._determine_protected_prefix(queue)
        return _MaQueueSnapshot(
            entries=entries,
            current_index=current_index,
            shuffle_enabled=bool(queue.shuffle_enabled),
            repeat_mode=queue.repeat_mode,
            protected_prefix_len=protected_prefix_len,
        )

    async def _collect_lms_snapshot(self) -> _LmsQueueSnapshot | None:
        """Return one normalized LMS queue snapshot for this player."""
        try:
            status = await self.player.provider.send_player_command(
                self.player.player_id,
                ["status", 0, MAX_SYNC_QUEUE_ITEMS],
            )
        except ProviderUnavailableError:
            return None
        return self._parse_lms_queue_state(status)

    async def _sync_ma_position_to_lms(
        self,
        ma_snapshot: _MaQueueSnapshot,
        lms_snapshot: _LmsQueueSnapshot,
    ) -> None:
        """Apply queue cursor alignment from MA to LMS."""
        if not ma_snapshot.entries:
            return
        if ma_snapshot.current_index == lms_snapshot.current_index:
            return
        await self.player.provider.send_player_command(
            self.player.player_id,
            ["playlist", "index", ma_snapshot.current_index],
        )

    async def _sync_ma_modes_to_lms(
        self,
        ma_snapshot: _MaQueueSnapshot,
        lms_snapshot: _LmsQueueSnapshot,
    ) -> None:
        """Apply repeat/shuffle alignment from MA to LMS."""
        repeat_target = self._ma_repeat_to_lms(ma_snapshot.repeat_mode)
        if repeat_target != lms_snapshot.repeat_mode:
            await self.player.provider.send_player_command(
                self.player.player_id,
                ["playlist", "repeat", repeat_target],
            )

        shuffle_target = 1 if ma_snapshot.shuffle_enabled else 0
        if shuffle_target != lms_snapshot.shuffle_mode:
            await self.player.provider.send_player_command(
                self.player.player_id,
                ["playlist", "shuffle", shuffle_target],
            )

    async def _apply_lms_modes_to_ma(
        self,
        lms_snapshot: _LmsQueueSnapshot,
    ) -> None:
        """Apply repeat/shuffle alignment from LMS to MA queue options."""
        queue = self.player.mass.player_queues.get(self.player.player_id)
        if queue is None:
            return

        repeat_target = self._lms_repeat_to_ma(lms_snapshot.repeat_mode)
        if queue.repeat_mode != repeat_target:
            await self.player.mass.player_queues.set_repeat(
                self.player.player_id,
                repeat_target,
            )

        shuffle_target = lms_snapshot.shuffle_mode in (1, 2)
        if queue.shuffle_enabled != shuffle_target:
            await self.player.mass.player_queues.set_shuffle(
                self.player.player_id,
                shuffle_target,
            )

    async def _rebuild_lms_tail(
        self,
        source_entries: tuple[_LmsMirrorEntry, ...],
        rebuild_from_index: int,
    ) -> None:
        """Rebuild LMS queue from the given index onward."""
        if rebuild_from_index <= 0:
            await self.player.provider.send_player_command(
                self.player.player_id,
                ["playlist", "clear"],
            )
            for entry in source_entries:
                await self._append_lms_entry(entry)
            return

        # Remove tail by repeatedly deleting at the start index so index shifts
        # work in our favor and we do not need range-delete support.
        lms_snapshot = await self._collect_lms_snapshot()
        if lms_snapshot is None:
            return
        while len(lms_snapshot.entries) > rebuild_from_index:
            await self._delete_lms_index(rebuild_from_index)
            lms_snapshot = await self._collect_lms_snapshot()
            if lms_snapshot is None:
                return

        for entry in source_entries[rebuild_from_index:]:
            await self._append_lms_entry(entry)

    async def _insert_lms_entry_at(
        self,
        entry: _LmsMirrorEntry,
        index: int,
    ) -> None:
        """Insert one entry at index by append+move."""
        snapshot_before = await self._collect_lms_snapshot()
        if snapshot_before is None:
            return
        await self._append_lms_entry(entry)
        appended_index = len(snapshot_before.entries)
        if index < appended_index:
            await self._move_lms_index(appended_index, index)

    async def _append_lms_entry(self, entry: _LmsMirrorEntry) -> None:
        """Append one queue entry on LMS."""
        if entry.kind == "track_id":
            await self.player.provider.send_player_command(
                self.player.player_id,
                ["playlistcontrol", "cmd:add", f"track_id:{entry.value}"],
            )
            return
        await self._add_url_entry_to_lms(entry)

    async def _move_lms_index(self, from_index: int, to_index: int) -> None:
        """Move one LMS queue entry by index."""
        await self.player.provider.send_player_command(
            self.player.player_id,
            ["playlist", "move", from_index, to_index],
        )

    async def _delete_lms_index(self, index: int) -> None:
        """Delete one LMS queue entry by index."""
        await self.player.provider.send_player_command(
            self.player.player_id,
            ["playlist", "delete", index],
        )

    @staticmethod
    def _entries_signature(
        entries: tuple[_LmsMirrorEntry, ...],
    ) -> tuple[tuple[str, str], ...]:
        """Return immutable signature for queue content comparisons."""
        return tuple(entry.identity for entry in entries)

    def _within_sync_limits(self, queue_length: int, source_name: str) -> bool:
        """Return whether queue length is within configured sync guard."""
        if queue_length <= MAX_SYNC_QUEUE_ITEMS:
            return True
        self.player.logger.warning(
            "Skipping queue sync for %s: queue length %s exceeds limit %s",
            source_name,
            queue_length,
            MAX_SYNC_QUEUE_ITEMS,
        )
        return False

    @staticmethod
    def _parse_int(raw_value: Any, default: int) -> int:
        """Parse an int-like value with fallback default."""
        try:
            return int(raw_value)
        except TypeError, ValueError:
            return default

    @classmethod
    def _parse_status_int(
        cls,
        status: dict[str, Any],
        keys: tuple[str, ...],
        default: int,
    ) -> int:
        """Parse integer status field from the first matching key alias."""
        for key in keys:
            if key in status:
                return cls._parse_int(status.get(key), default)
        return default

    @staticmethod
    def _ma_repeat_to_lms(repeat_mode: RepeatMode) -> int:
        """Map MA repeat mode to LMS repeat integer value."""
        if repeat_mode == RepeatMode.ONE:
            return 1
        if repeat_mode == RepeatMode.ALL:
            return 2
        return 0

    @staticmethod
    def _lms_repeat_to_ma(repeat_mode: int) -> RepeatMode:
        """Map LMS repeat integer value to MA repeat mode."""
        if repeat_mode == 1:
            return RepeatMode.ONE
        if repeat_mode == 2:
            return RepeatMode.ALL
        return RepeatMode.OFF

    @staticmethod
    def _determine_protected_prefix(queue: Any) -> int:
        """Return immutable prefix length during active playback."""
        if queue.state in (PlaybackState.PLAYING, PlaybackState.PAUSED):
            if queue.index_in_buffer is not None:
                return max(0, int(queue.index_in_buffer) + 1)
            if queue.current_index is not None:
                return max(0, int(queue.current_index) + 1)
        return 0

    async def _resolve_ma_uri_to_lms_entry(
        self,
        uri: str,
    ) -> LmsQueueEntry | None:
        """Resolve one MA URI to an LMS queue entry."""
        return await self._media_mapper.resolve_ma_uri_to_lms_queue_entry(uri)

    async def _add_url_entry_to_lms(self, entry: _LmsMirrorEntry) -> None:
        """Add one URL entry to LMS with optional display metadata."""
        command = [
            "playlistcontrol",
            "cmd:add",
            f"url:{entry.value}",
        ]
        if entry.title:
            command.append(f"title:{entry.title}")
        if entry.artist:
            command.append(f"artist:{entry.artist}")
        if entry.album:
            command.append(f"album:{entry.album}")
        try:
            await self.player.provider.send_player_command(
                self.player.player_id,
                command,
            )
        except ProviderUnavailableError:
            raise
        except MusicAssistantError:
            await self.player.provider.send_player_command(
                self.player.player_id,
                ["playlist", "add", entry.value],
            )

    @staticmethod
    def _extract_queue_item_metadata(
        item: QueueItem,
    ) -> tuple[str | None, str | None, str | None]:
        """Extract title/artist/album strings for LMS display metadata."""
        title = item.name or None
        artist: str | None = None
        album: str | None = None
        media_item = item.media_item
        if media_item is not None:
            title = media_item.name or title
            artist = cast("str", getattr(media_item, "artist_str", "") or "") or None
            album_obj = getattr(media_item, "album", None)
            if album_obj is not None:
                album = cast("str", getattr(album_obj, "name", "") or "") or None
        return title, artist, album
