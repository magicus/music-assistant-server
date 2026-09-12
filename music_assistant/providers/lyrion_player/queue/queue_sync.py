"""Queue synchronization helpers for the Lyrion player."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.errors import MusicAssistantError, ProviderUnavailableError

from music_assistant.providers.lyrion_player.constants import (
    SYNC_REBUILD_COST_THRESHOLD,
    SYNC_REBUILD_RATIO_THRESHOLD,
)
from music_assistant.providers.lyrion_player.helpers.queue import QueueDiffPlanner, QueueSyncEngine
from music_assistant.providers.lyrion_player.media_mapper import LyrionMediaMapper

from .queue_models import _LmsMirrorEntry, _LmsQueueSnapshot, _MaQueueSnapshot
from .queue_sync_bridge import LyrionQueueSyncBridge

if TYPE_CHECKING:
    from music_assistant.providers.lyrion_player.player import LyrionPlayer


class LyrionQueueSync(LyrionQueueSyncBridge):
    """Own MA<->LMS queue mirroring and track mapping logic."""

    def __init__(self, player: LyrionPlayer) -> None:
        """
        Initialize queue sync helper.

        :param player: Owning Lyrion player instance.
        """
        self.player = player
        self._syncing_from_lms_queue = False
        self._syncing_to_lms_queue = False
        self._ma_queue_sync_pending = False
        self._ma_queue_sync_pending_sync_items = False
        self._lms_queue_sync_pending = False
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
    ) -> None:
        """
        Mirror the current MA queue state to LMS for this player.

        :param sync_items: If False, only sync current queue index.
        """
        if self._syncing_to_lms_queue or self._syncing_from_lms_queue:
            self._ma_queue_sync_pending = True
            self._ma_queue_sync_pending_sync_items = (
                self._ma_queue_sync_pending_sync_items or sync_items
            )
            return

        self._syncing_to_lms_queue = True
        try:
            current_sync_items = sync_items
            while True:
                ma_snapshot = await self._collect_ma_snapshot()
                if not self._within_sync_limits(len(ma_snapshot.entries), "MA"):
                    break

                lms_snapshot = await self._collect_lms_snapshot()
                if lms_snapshot is None:
                    break
                if not self._within_sync_limits(len(lms_snapshot.entries), "LMS"):
                    break

                if not current_sync_items and self._entries_signature(
                    ma_snapshot.entries
                ) != self._entries_signature(lms_snapshot.entries):
                    current_sync_items = True

                if current_sync_items:
                    await self._sync_ma_items_to_lms(ma_snapshot, lms_snapshot)

                await self._sync_ma_position_to_lms(ma_snapshot, lms_snapshot)
                await self._sync_ma_modes_to_lms(ma_snapshot, lms_snapshot)

                verified_lms = await self._collect_lms_snapshot()
                if verified_lms is None:
                    break

                self._ma_model = ma_snapshot
                self._lms_model = verified_lms
                self._lms_shuffle_mode_raw = verified_lms.shuffle_mode
                self._identity_map = {
                    index: entry.identity for index, entry in enumerate(verified_lms.entries)
                }

                if not self._ma_queue_sync_pending:
                    break
                current_sync_items = current_sync_items or self._ma_queue_sync_pending_sync_items
                self._ma_queue_sync_pending = False
                self._ma_queue_sync_pending_sync_items = False
        except ProviderUnavailableError as err:
            self.player.logger.warning(
                "Unable to sync MA queue to LMS for player %s: %s",
                self.player.player_id,
                err,
            )
        finally:
            self._syncing_to_lms_queue = False

        if self._lms_queue_sync_pending:
            self._lms_queue_sync_pending = False
            await self.sync_lms_queue_to_ma()

    async def sync_lms_queue_to_ma(self) -> None:
        """Mirror LMS playlist changes back into MA when tracks map cleanly."""
        if self._syncing_to_lms_queue or self._syncing_from_lms_queue:
            self._lms_queue_sync_pending = True
            return

        self._syncing_from_lms_queue = True
        try:
            while True:
                lms_snapshot = await self._collect_lms_snapshot()
                if lms_snapshot is None:
                    break
                if not self._within_sync_limits(len(lms_snapshot.entries), "LMS"):
                    break
                if self._lms_model == lms_snapshot:
                    break

                music_provider_instance = (
                    self._media_mapper.resolve_matching_music_provider_instance()
                )
                if music_provider_instance is None:
                    break

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

                if not self._lms_queue_sync_pending:
                    break
                self._lms_queue_sync_pending = False
        except MusicAssistantError as err:
            self.player.logger.debug(
                "Unable to mirror LMS queue to MA for %s: %s",
                self.player.player_id,
                err,
            )
        finally:
            self._syncing_from_lms_queue = False

        if self._ma_queue_sync_pending:
            pending_sync_items = self._ma_queue_sync_pending_sync_items
            self._ma_queue_sync_pending = False
            self._ma_queue_sync_pending_sync_items = False
            await self.sync_ma_queue_to_lms(sync_items=pending_sync_items)
