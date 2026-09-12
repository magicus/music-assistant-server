"""Unit tests for non-live queue sync helper branches."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from music_assistant_models.enums import PlaybackState, RepeatMode
from music_assistant_models.errors import MusicAssistantError, ProviderUnavailableError

from music_assistant.providers.lyrion_player.constants import MAX_SYNC_QUEUE_ITEMS
from music_assistant.providers.lyrion_player.media_mapper import LmsQueueEntry
from music_assistant.providers.lyrion_player.queue.queue_sync import (
    LyrionQueueSync,
    _LmsMirrorEntry,
    _LmsQueueSnapshot,
    _MaQueueSnapshot,
)


def _build_queue_sync() -> tuple[LyrionQueueSync, Any]:
    """Create queue-sync instance with lightweight stubs."""
    provider = SimpleNamespace(
        send_player_command=AsyncMock(),
        get_player_queue_status=AsyncMock(),
        set_player_queue_index=AsyncMock(),
        set_player_repeat_mode=AsyncMock(),
        set_player_shuffle_mode=AsyncMock(),
        clear_player_queue=AsyncMock(),
        add_player_track_id_to_queue=AsyncMock(),
        add_player_url_to_queue=AsyncMock(),
        move_player_queue_item=AsyncMock(),
        delete_player_queue_item=AsyncMock(),
        play_player=AsyncMock(),
        build_stream_redirect_url=MagicMock(return_value="http://redirect/item"),
    )
    player = SimpleNamespace(
        player_id="player-1",
        provider=provider,
        lyrion_server=provider,
        logger=MagicMock(),
        mass=SimpleNamespace(
            player_queues=SimpleNamespace(
                get=Mock(return_value=None),
                items=Mock(return_value=[]),
                clear=Mock(),
                play_media=AsyncMock(),
                play_index=AsyncMock(),
                pause=AsyncMock(),
                set_repeat=AsyncMock(),
                set_shuffle=AsyncMock(),
            )
        ),
    )

    queue_sync = object.__new__(LyrionQueueSync)
    queue_sync.player = cast("Any", player)
    queue_sync._syncing_from_lms_queue = False
    queue_sync._syncing_to_lms_queue = False
    queue_sync._ma_queue_sync_pending = False
    queue_sync._ma_queue_sync_pending_sync_items = False
    queue_sync._lms_queue_sync_pending = False
    queue_sync._ma_model = None
    queue_sync._lms_model = None
    queue_sync._lms_shuffle_mode_raw = 0
    queue_sync._identity_map = {}
    queue_sync._media_mapper = MagicMock()
    queue_sync._sync_engine = SimpleNamespace(apply=AsyncMock())
    return queue_sync, player


@pytest.mark.asyncio
async def test_collect_ma_snapshot_defaults_when_queue_is_missing() -> None:
    """Missing MA queue should yield an empty, safe snapshot."""
    queue_sync, _player = _build_queue_sync()

    snapshot = await queue_sync._collect_ma_snapshot()

    assert snapshot.entries == ()
    assert snapshot.current_index == 0
    assert snapshot.protected_prefix_len == 0


@pytest.mark.asyncio
async def test_collect_ma_snapshot_uses_queue_state_and_entries() -> None:
    """Queue snapshot should reflect MA entries and protected prefix during playback."""
    queue_sync, player = _build_queue_sync()
    player.mass.player_queues.get.return_value = SimpleNamespace(
        current_index=2,
        shuffle_enabled=True,
        repeat_mode=RepeatMode.ALL,
        state=PlaybackState.PLAYING,
        index_in_buffer=1,
    )
    queue_sync._collect_ma_lms_queue_entries = AsyncMock(
        return_value=(_LmsMirrorEntry(kind="url", value="u"),)
    )

    snapshot = await queue_sync._collect_ma_snapshot()

    assert snapshot.current_index == 2
    assert snapshot.shuffle_enabled is True
    assert snapshot.repeat_mode == RepeatMode.ALL
    assert snapshot.protected_prefix_len == 2


@pytest.mark.asyncio
async def test_collect_lms_snapshot_returns_none_on_provider_unavailable() -> None:
    """LMS snapshot collection should gracefully handle temporary provider outages."""
    queue_sync, player = _build_queue_sync()
    player.provider.get_player_queue_status = AsyncMock(
        side_effect=ProviderUnavailableError("down")
    )

    assert await queue_sync._collect_lms_snapshot() is None


@pytest.mark.asyncio
async def test_public_mutation_wrappers_delegate_to_internal_helpers() -> None:
    """Public queue mutation wrapper methods should call their private implementations."""
    queue_sync, _player = _build_queue_sync()
    queue_sync._rebuild_lms_tail = AsyncMock()
    queue_sync._delete_lms_index = AsyncMock()
    queue_sync._move_lms_index = AsyncMock()
    queue_sync._insert_lms_entry_at = AsyncMock()

    entry = _LmsMirrorEntry(kind="url", value="u")
    await queue_sync.rebuild_from((entry,), 2)
    await queue_sync.delete_index(3)
    await queue_sync.move_index(4, 1)
    await queue_sync.insert_entry_at(entry, 0)

    queue_sync._rebuild_lms_tail.assert_awaited_once_with((entry,), 2)
    queue_sync._delete_lms_index.assert_awaited_once_with(3)
    queue_sync._move_lms_index.assert_awaited_once_with(4, 1)
    queue_sync._insert_lms_entry_at.assert_awaited_once_with(entry, 0)


@pytest.mark.asyncio
async def test_sync_ma_position_and_modes_to_lms() -> None:
    """Position/repeat/shuffle sync should dispatch only when target differs."""
    queue_sync, player = _build_queue_sync()
    ma_snapshot = _MaQueueSnapshot(
        entries=(_LmsMirrorEntry(kind="url", value="u"),),
        current_index=2,
        shuffle_enabled=True,
        repeat_mode=RepeatMode.ONE,
        protected_prefix_len=0,
    )
    lms_snapshot = _LmsQueueSnapshot(
        entries=(_LmsMirrorEntry(kind="url", value="u"),),
        current_index=0,
        shuffle_mode=0,
        repeat_mode=0,
        playback_mode="stop",
    )

    await queue_sync._sync_ma_position_to_lms(ma_snapshot, lms_snapshot)
    await queue_sync._sync_ma_modes_to_lms(ma_snapshot, lms_snapshot)

    player.provider.set_player_queue_index.assert_awaited_once_with("player-1", 2)
    player.provider.set_player_repeat_mode.assert_awaited_once_with("player-1", 1)
    player.provider.set_player_shuffle_mode.assert_awaited_once_with("player-1", 1)


@pytest.mark.asyncio
async def test_sync_entrypoints_set_pending_flags_when_busy() -> None:
    """Top-level sync methods should queue a pending rerun while another sync is active."""
    queue_sync, _player = _build_queue_sync()
    queue_sync._syncing_to_lms_queue = True

    await queue_sync.sync_ma_queue_to_lms(sync_items=False)
    assert queue_sync._ma_queue_sync_pending is True
    assert queue_sync._ma_queue_sync_pending_sync_items is False

    queue_sync._syncing_to_lms_queue = False
    queue_sync._syncing_from_lms_queue = True
    await queue_sync.sync_lms_queue_to_ma()
    assert queue_sync._lms_queue_sync_pending is True


@pytest.mark.asyncio
async def test_apply_lms_modes_to_ma_updates_repeat_and_shuffle() -> None:
    """LMS repeat/shuffle modes should map back to MA queue options."""
    queue_sync, player = _build_queue_sync()
    queue = SimpleNamespace(repeat_mode=RepeatMode.OFF, shuffle_enabled=False)
    player.mass.player_queues.get.return_value = queue

    await queue_sync._apply_lms_modes_to_ma(
        _LmsQueueSnapshot(
            entries=(),
            current_index=0,
            shuffle_mode=1,
            repeat_mode=2,
            playback_mode="stop",
        )
    )

    player.mass.player_queues.set_repeat.assert_awaited_once_with("player-1", RepeatMode.ALL)
    player.mass.player_queues.set_shuffle.assert_awaited_once_with("player-1", True)


@pytest.mark.asyncio
async def test_sync_lms_queue_to_ma_happy_path_updates_models_and_identity_map() -> None:
    """LMS->MA sync should apply entries/modes and cache normalized snapshots."""
    queue_sync, _player = _build_queue_sync()

    lms_snapshot = _LmsQueueSnapshot(
        entries=(_LmsMirrorEntry(kind="track_id", value="42"),),
        current_index=0,
        shuffle_mode=1,
        repeat_mode=2,
        playback_mode="play",
    )
    ma_snapshot = _MaQueueSnapshot(
        entries=(_LmsMirrorEntry(kind="track_id", value="42"),),
        current_index=0,
        shuffle_enabled=True,
        repeat_mode=RepeatMode.ALL,
        protected_prefix_len=0,
    )
    queue_sync._collect_lms_snapshot = AsyncMock(side_effect=[lms_snapshot, lms_snapshot])
    queue_sync._collect_ma_snapshot = AsyncMock(return_value=ma_snapshot)
    queue_sync._apply_lms_queue_entries_to_ma = AsyncMock()
    queue_sync._apply_lms_modes_to_ma = AsyncMock()
    queue_sync._media_mapper.resolve_matching_music_provider_instance = MagicMock(
        return_value="lyrion_music.instance"
    )

    await queue_sync.sync_lms_queue_to_ma()

    queue_sync._apply_lms_queue_entries_to_ma.assert_awaited_once_with(
        lms_snapshot.entries,
        "lyrion_music.instance",
        0,
        "play",
    )
    queue_sync._apply_lms_modes_to_ma.assert_awaited_once_with(lms_snapshot)
    assert queue_sync._lms_model == lms_snapshot
    assert queue_sync._ma_model == ma_snapshot
    assert queue_sync._identity_map == {0: ("track_id", "42")}


@pytest.mark.asyncio
async def test_rebuild_lms_tail_clears_for_full_rebuild_and_appends_entries() -> None:
    """Full rebuild should clear queue then append all desired entries."""
    queue_sync, player = _build_queue_sync()
    queue_sync._append_lms_entry = AsyncMock()

    source_entries = (
        _LmsMirrorEntry(kind="track_id", value="1"),
        _LmsMirrorEntry(kind="url", value="u2"),
    )
    await queue_sync._rebuild_lms_tail(source_entries, 0)

    player.provider.clear_player_queue.assert_awaited_once_with("player-1")
    assert queue_sync._append_lms_entry.await_count == 2


@pytest.mark.asyncio
async def test_rebuild_lms_tail_deletes_tail_from_index_before_append() -> None:
    """Tail rebuild should delete stale LMS rows from rebuild index then append source tail."""
    queue_sync, _player = _build_queue_sync()
    queue_sync._delete_lms_index = AsyncMock()
    queue_sync._append_lms_entry = AsyncMock()
    queue_sync._collect_lms_snapshot = AsyncMock(
        side_effect=[
            _LmsQueueSnapshot(
                entries=(
                    _LmsMirrorEntry(kind="url", value="a"),
                    _LmsMirrorEntry(kind="url", value="b"),
                    _LmsMirrorEntry(kind="url", value="c"),
                    _LmsMirrorEntry(kind="url", value="d"),
                ),
                current_index=0,
                shuffle_mode=0,
                repeat_mode=0,
                playback_mode="stop",
            ),
            _LmsQueueSnapshot(
                entries=(
                    _LmsMirrorEntry(kind="url", value="a"),
                    _LmsMirrorEntry(kind="url", value="b"),
                    _LmsMirrorEntry(kind="url", value="c"),
                ),
                current_index=0,
                shuffle_mode=0,
                repeat_mode=0,
                playback_mode="stop",
            ),
            _LmsQueueSnapshot(
                entries=(
                    _LmsMirrorEntry(kind="url", value="a"),
                    _LmsMirrorEntry(kind="url", value="b"),
                ),
                current_index=0,
                shuffle_mode=0,
                repeat_mode=0,
                playback_mode="stop",
            ),
        ]
    )

    source_entries = (
        _LmsMirrorEntry(kind="url", value="a"),
        _LmsMirrorEntry(kind="url", value="b"),
        _LmsMirrorEntry(kind="url", value="x"),
    )
    await queue_sync._rebuild_lms_tail(source_entries, 2)

    assert queue_sync._delete_lms_index.await_count == 2
    queue_sync._delete_lms_index.assert_any_await(2)
    queue_sync._append_lms_entry.assert_awaited_once_with(_LmsMirrorEntry(kind="url", value="x"))


@pytest.mark.asyncio
async def test_insert_lms_entry_at_uses_append_then_move_when_needed() -> None:
    """Insert-at-index should append first and move only when insertion point is before tail."""
    queue_sync, _player = _build_queue_sync()
    queue_sync._append_lms_entry = AsyncMock()
    queue_sync._move_lms_index = AsyncMock()
    queue_sync._collect_lms_snapshot = AsyncMock(
        return_value=_LmsQueueSnapshot(
            entries=(
                _LmsMirrorEntry(kind="url", value="a"),
                _LmsMirrorEntry(kind="url", value="b"),
                _LmsMirrorEntry(kind="url", value="c"),
            ),
            current_index=0,
            shuffle_mode=0,
            repeat_mode=0,
            playback_mode="stop",
        )
    )

    entry = _LmsMirrorEntry(kind="url", value="x")
    await queue_sync._insert_lms_entry_at(entry, 1)
    queue_sync._move_lms_index.assert_awaited_once_with(3, 1)


@pytest.mark.asyncio
async def test_insert_lms_entry_at_returns_early_without_snapshot() -> None:
    """Insert should no-op when current LMS snapshot cannot be read."""
    queue_sync, _player = _build_queue_sync()
    queue_sync._append_lms_entry = AsyncMock()
    queue_sync._move_lms_index = AsyncMock()
    queue_sync._collect_lms_snapshot = AsyncMock(return_value=None)

    await queue_sync._insert_lms_entry_at(_LmsMirrorEntry(kind="url", value="x"), 0)

    queue_sync._append_lms_entry.assert_not_awaited()
    queue_sync._move_lms_index.assert_not_awaited()


@pytest.mark.asyncio
async def test_append_lms_entry_uses_track_id_or_url_path() -> None:
    """Appending entries should dispatch track_id natively and URL via URL helper."""
    queue_sync, player = _build_queue_sync()
    queue_sync._add_url_entry_to_lms = AsyncMock()

    await queue_sync._append_lms_entry(_LmsMirrorEntry(kind="track_id", value="42"))
    await queue_sync._append_lms_entry(_LmsMirrorEntry(kind="url", value="http://x"))

    player.provider.add_player_track_id_to_queue.assert_awaited_once_with("player-1", "42")
    queue_sync._add_url_entry_to_lms.assert_awaited_once()


@pytest.mark.asyncio
async def test_add_url_entry_to_lms_falls_back_when_metadata_command_fails() -> None:
    """URL adds should delegate metadata payload to provider adapter."""
    queue_sync, player = _build_queue_sync()

    await queue_sync._add_url_entry_to_lms(
        _LmsMirrorEntry(
            kind="url",
            value="http://x",
            title="title",
            artist="artist",
            album="album",
        )
    )

    player.provider.add_player_url_to_queue.assert_awaited_once_with(
        "player-1",
        "http://x",
        title="title",
        artist="artist",
        album="album",
    )


@pytest.mark.asyncio
async def test_collect_ma_lms_queue_entries_maps_track_and_url_and_metadata() -> None:
    """MA queue entry collection should map track ids and URL fallbacks with metadata."""
    queue_sync, player = _build_queue_sync()
    q1 = SimpleNamespace(
        uri="track://1",
        queue_id="q",
        queue_item_id="i1",
        name="Row One",
        media_item=SimpleNamespace(
            name="Song 1", artist_str="Artist", album=SimpleNamespace(name="Album")
        ),
    )
    q2 = SimpleNamespace(
        uri=None,
        queue_id="q",
        queue_item_id="i2",
        name="Row Two",
        media_item=None,
    )
    player.mass.player_queues.items.return_value = [q1, q2]
    queue_sync._resolve_ma_uri_to_lms_entry = AsyncMock(
        return_value=LmsQueueEntry("track_id", "42")
    )

    entries = await queue_sync._collect_ma_lms_queue_entries()

    assert entries[0] == _LmsMirrorEntry(
        kind="track_id",
        value="42",
        title="Song 1",
        artist="Artist",
        album="Album",
    )
    assert entries[1].kind == "url"
    assert entries[1].title == "Row Two"
    player.provider.build_stream_redirect_url.assert_called_once()


def test_extract_queue_item_metadata_uses_media_item_values_when_present() -> None:
    """Metadata extraction should prefer media-item values and gracefully handle missing data."""
    item = SimpleNamespace(
        name="Queue Name",
        media_item=SimpleNamespace(
            name="Media Name", artist_str="A", album=SimpleNamespace(name="B")
        ),
    )
    assert LyrionQueueSync._extract_queue_item_metadata(item) == ("Media Name", "A", "B")

    plain_item = SimpleNamespace(name="Queue Name", media_item=None)
    assert LyrionQueueSync._extract_queue_item_metadata(plain_item) == ("Queue Name", None, None)


@pytest.mark.asyncio
async def test_apply_lms_queue_entries_to_ma_handles_empty_and_pause_mode() -> None:
    """LMS->MA queue replacement should clear, load and optionally pause."""
    queue_sync, player = _build_queue_sync()
    queue_sync._media_mapper.lms_queue_entry_to_ma_uri = MagicMock(side_effect=["u1", "u2"])

    await queue_sync._apply_lms_queue_entries_to_ma((), "music.inst", 0, "stop")
    player.mass.player_queues.clear.assert_called_once_with("player-1", skip_stop=True)

    player.mass.player_queues.clear.reset_mock()
    await queue_sync._apply_lms_queue_entries_to_ma(
        (
            _LmsMirrorEntry(kind="url", value="http://a"),
            _LmsMirrorEntry(kind="url", value="http://b"),
        ),
        "music.inst",
        1,
        "pause",
    )

    player.mass.player_queues.play_media.assert_awaited_once()
    player.mass.player_queues.play_index.assert_awaited_once_with("player-1", 1)
    player.mass.player_queues.pause.assert_awaited_once_with("player-1")


@pytest.mark.asyncio
async def test_resolve_ma_uri_to_lms_entry_delegates_to_mapper() -> None:
    """MA URI -> LMS entry resolution should delegate directly to media mapper."""
    queue_sync, _player = _build_queue_sync()
    expected = LmsQueueEntry(kind="track_id", value="42")
    queue_sync._media_mapper.resolve_ma_uri_to_lms_queue_entry = AsyncMock(return_value=expected)

    assert await queue_sync.resolve_ma_uri_to_lms_entry("lyrion://track/42") == expected
    queue_sync._media_mapper.resolve_ma_uri_to_lms_queue_entry.assert_awaited_once_with(
        "lyrion://track/42"
    )


def test_queue_sync_small_helpers_cover_mapping_and_limits() -> None:
    """Small helper methods should preserve identity/signature/repeat semantics."""
    queue_sync, player = _build_queue_sync()
    entries = (
        _LmsMirrorEntry(kind="url", value="a"),
        _LmsMirrorEntry(kind="track_id", value="1"),
    )

    assert queue_sync._entries_signature(entries) == (("url", "a"), ("track_id", "1"))
    assert queue_sync._within_sync_limits(MAX_SYNC_QUEUE_ITEMS, "MA") is True
    assert queue_sync._within_sync_limits(MAX_SYNC_QUEUE_ITEMS + 1, "MA") is False
    player.logger.warning.assert_called_once()

    assert queue_sync._parse_int("3", 0) == 3
    assert queue_sync._parse_int("bad", 7) == 7
    assert queue_sync._ma_repeat_to_lms(RepeatMode.OFF) == 0
    assert queue_sync._ma_repeat_to_lms(RepeatMode.ONE) == 1
    assert queue_sync._ma_repeat_to_lms(RepeatMode.ALL) == 2
    assert queue_sync._lms_repeat_to_ma(0) == RepeatMode.OFF
    assert queue_sync._lms_repeat_to_ma(1) == RepeatMode.ONE
    assert queue_sync._lms_repeat_to_ma(2) == RepeatMode.ALL

    queue = SimpleNamespace(
        state=PlaybackState.PLAYING,
        index_in_buffer=2,
        current_index=0,
    )
    assert queue_sync._determine_protected_prefix(queue) == 3

    queue.state = PlaybackState.IDLE
    assert queue_sync._determine_protected_prefix(queue) == 0


def test_queue_sync_init_sets_expected_defaults() -> None:
    """Regular construction should initialize internal sync state safely."""
    provider = SimpleNamespace(
        send_player_command=AsyncMock(),
        build_stream_redirect_url=MagicMock(return_value="http://redirect/item"),
    )
    player = SimpleNamespace(
        player_id="player-1",
        provider=provider,
        logger=MagicMock(),
        mass=SimpleNamespace(providers=[]),
    )

    queue_sync = LyrionQueueSync(cast("Any", player))

    assert queue_sync.player is player
    assert queue_sync.syncing_from_lms_queue is False
    assert queue_sync._syncing_to_lms_queue is False
    assert queue_sync._ma_queue_sync_pending is False
    assert queue_sync._lms_queue_sync_pending is False
    assert queue_sync._identity_map == {}


@pytest.mark.asyncio
async def test_sync_ma_queue_to_lms_breaks_when_ma_queue_exceeds_limit() -> None:
    """MA->LMS sync should stop early when MA snapshot exceeds sync limit."""
    queue_sync, _player = _build_queue_sync()
    queue_sync._collect_ma_snapshot = AsyncMock(
        return_value=_MaQueueSnapshot(
            entries=(_LmsMirrorEntry(kind="url", value="a"),),
            current_index=0,
            shuffle_enabled=False,
            repeat_mode=RepeatMode.OFF,
            protected_prefix_len=0,
        )
    )
    queue_sync._within_sync_limits = Mock(return_value=False)
    queue_sync._collect_lms_snapshot = AsyncMock()

    await queue_sync.sync_ma_queue_to_lms(sync_items=True)

    queue_sync._collect_lms_snapshot.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_ma_queue_to_lms_breaks_when_lms_snapshot_is_unavailable() -> None:
    """MA->LMS sync should stop when LMS status fetch returns no snapshot."""
    queue_sync, _player = _build_queue_sync()
    queue_sync._collect_ma_snapshot = AsyncMock(
        return_value=_MaQueueSnapshot(
            entries=(_LmsMirrorEntry(kind="url", value="a"),),
            current_index=0,
            shuffle_enabled=False,
            repeat_mode=RepeatMode.OFF,
            protected_prefix_len=0,
        )
    )
    queue_sync._within_sync_limits = Mock(return_value=True)
    queue_sync._collect_lms_snapshot = AsyncMock(return_value=None)

    await queue_sync.sync_ma_queue_to_lms(sync_items=True)

    queue_sync._collect_lms_snapshot.assert_awaited_once()


@pytest.mark.asyncio
async def test_sync_ma_queue_to_lms_skips_item_sync_when_lms_limit_guard_hits() -> None:
    """MA->LMS sync should stop before item changes when LMS queue exceeds guard."""
    queue_sync, _player = _build_queue_sync()
    queue_sync._collect_ma_snapshot = AsyncMock(
        return_value=_MaQueueSnapshot(
            entries=(_LmsMirrorEntry(kind="url", value="a"),),
            current_index=0,
            shuffle_enabled=False,
            repeat_mode=RepeatMode.OFF,
            protected_prefix_len=0,
        )
    )
    queue_sync._collect_lms_snapshot = AsyncMock(
        return_value=_LmsQueueSnapshot(
            entries=(_LmsMirrorEntry(kind="url", value="a"),),
            current_index=0,
            shuffle_mode=0,
            repeat_mode=0,
            playback_mode="stop",
        )
    )
    queue_sync._within_sync_limits = Mock(side_effect=[True, False])
    queue_sync._sync_ma_items_to_lms = AsyncMock()

    await queue_sync.sync_ma_queue_to_lms(sync_items=True)

    queue_sync._sync_ma_items_to_lms.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_ma_queue_to_lms_forces_item_sync_when_signatures_differ() -> None:
    """Position-only sync should escalate to full item sync when queue identities differ."""
    queue_sync, _player = _build_queue_sync()
    ma_snapshot = _MaQueueSnapshot(
        entries=(_LmsMirrorEntry(kind="url", value="ma-a"),),
        current_index=0,
        shuffle_enabled=False,
        repeat_mode=RepeatMode.OFF,
        protected_prefix_len=0,
    )
    lms_snapshot = _LmsQueueSnapshot(
        entries=(_LmsMirrorEntry(kind="url", value="lms-a"),),
        current_index=0,
        shuffle_mode=0,
        repeat_mode=0,
        playback_mode="stop",
    )
    queue_sync._collect_ma_snapshot = AsyncMock(return_value=ma_snapshot)
    queue_sync._collect_lms_snapshot = AsyncMock(side_effect=[lms_snapshot, lms_snapshot])
    queue_sync._within_sync_limits = Mock(return_value=True)
    queue_sync._sync_ma_items_to_lms = AsyncMock()
    queue_sync._sync_ma_position_to_lms = AsyncMock()
    queue_sync._sync_ma_modes_to_lms = AsyncMock()

    await queue_sync.sync_ma_queue_to_lms(sync_items=False)

    queue_sync._sync_ma_items_to_lms.assert_awaited_once_with(ma_snapshot, lms_snapshot)


@pytest.mark.asyncio
async def test_sync_ma_queue_to_lms_logs_provider_unavailable_and_triggers_pending_lms_sync() -> (
    None
):
    """MA->LMS sync should log provider outages and then run pending LMS->MA pass."""
    queue_sync, player = _build_queue_sync()
    ma_snapshot = _MaQueueSnapshot(
        entries=(_LmsMirrorEntry(kind="url", value="a"),),
        current_index=0,
        shuffle_enabled=False,
        repeat_mode=RepeatMode.OFF,
        protected_prefix_len=0,
    )
    lms_snapshot = _LmsQueueSnapshot(
        entries=(_LmsMirrorEntry(kind="url", value="a"),),
        current_index=0,
        shuffle_mode=0,
        repeat_mode=0,
        playback_mode="stop",
    )
    queue_sync._collect_ma_snapshot = AsyncMock(return_value=ma_snapshot)
    queue_sync._collect_lms_snapshot = AsyncMock(return_value=lms_snapshot)
    queue_sync._within_sync_limits = Mock(return_value=True)
    queue_sync._sync_ma_position_to_lms = AsyncMock(side_effect=ProviderUnavailableError("offline"))
    queue_sync._sync_ma_modes_to_lms = AsyncMock()
    queue_sync.sync_lms_queue_to_ma = AsyncMock()
    queue_sync._lms_queue_sync_pending = True

    await queue_sync.sync_ma_queue_to_lms(sync_items=True)

    player.logger.warning.assert_called_once()
    assert queue_sync._syncing_to_lms_queue is False
    queue_sync.sync_lms_queue_to_ma.assert_awaited_once()


@pytest.mark.asyncio
async def test_sync_lms_queue_to_ma_breaks_on_guard_conditions() -> None:
    """LMS->MA sync should stop on unavailable snapshot, oversize queue, or unchanged model."""
    queue_sync, _player = _build_queue_sync()
    snapshot = _LmsQueueSnapshot(
        entries=(_LmsMirrorEntry(kind="url", value="a"),),
        current_index=0,
        shuffle_mode=0,
        repeat_mode=0,
        playback_mode="stop",
    )

    queue_sync._collect_lms_snapshot = AsyncMock(return_value=None)
    await queue_sync.sync_lms_queue_to_ma()

    queue_sync._collect_lms_snapshot = AsyncMock(return_value=snapshot)
    queue_sync._within_sync_limits = Mock(return_value=False)
    await queue_sync.sync_lms_queue_to_ma()

    queue_sync._within_sync_limits = Mock(return_value=True)
    queue_sync._lms_model = snapshot
    queue_sync._collect_lms_snapshot = AsyncMock(return_value=snapshot)
    await queue_sync.sync_lms_queue_to_ma()


@pytest.mark.asyncio
async def test_sync_lms_queue_to_ma_breaks_when_no_matching_music_provider() -> None:
    """LMS->MA sync should skip apply when no same-endpoint Lyrion music provider exists."""
    queue_sync, _player = _build_queue_sync()
    queue_sync._lms_model = None
    queue_sync._collect_lms_snapshot = AsyncMock(
        return_value=_LmsQueueSnapshot(
            entries=(_LmsMirrorEntry(kind="url", value="a"),),
            current_index=0,
            shuffle_mode=0,
            repeat_mode=0,
            playback_mode="stop",
        )
    )
    queue_sync._within_sync_limits = Mock(return_value=True)
    queue_sync._media_mapper.resolve_matching_music_provider_instance = MagicMock(return_value=None)
    queue_sync._apply_lms_queue_entries_to_ma = AsyncMock()

    await queue_sync.sync_lms_queue_to_ma()

    queue_sync._apply_lms_queue_entries_to_ma.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_lms_queue_to_ma_logs_music_assistant_errors() -> None:
    """LMS->MA sync should log and exit when MA queue apply raises an MA error."""
    queue_sync, player = _build_queue_sync()
    snapshot = _LmsQueueSnapshot(
        entries=(_LmsMirrorEntry(kind="url", value="a"),),
        current_index=0,
        shuffle_mode=0,
        repeat_mode=0,
        playback_mode="stop",
    )
    queue_sync._collect_lms_snapshot = AsyncMock(return_value=snapshot)
    queue_sync._within_sync_limits = Mock(return_value=True)
    queue_sync._media_mapper.resolve_matching_music_provider_instance = MagicMock(
        return_value="lyrion_music.instance"
    )
    queue_sync._apply_lms_queue_entries_to_ma = AsyncMock(side_effect=MusicAssistantError("broken"))

    await queue_sync.sync_lms_queue_to_ma()

    player.logger.debug.assert_called_once()


@pytest.mark.asyncio
async def test_sync_lms_queue_to_ma_processes_pending_ma_sync_request() -> None:
    """Pending MA->LMS request should run after LMS->MA sync loop exits."""
    queue_sync, _player = _build_queue_sync()
    queue_sync._collect_lms_snapshot = AsyncMock(return_value=None)
    queue_sync._ma_queue_sync_pending = True
    queue_sync._ma_queue_sync_pending_sync_items = True
    queue_sync.sync_ma_queue_to_lms = AsyncMock()

    await LyrionQueueSync.sync_lms_queue_to_ma(queue_sync)

    queue_sync.sync_ma_queue_to_lms.assert_awaited_once_with(sync_items=True)
    assert queue_sync._ma_queue_sync_pending is False
    assert queue_sync._ma_queue_sync_pending_sync_items is False


@pytest.mark.asyncio
async def test_sync_ma_items_to_lms_delegates_to_sync_engine() -> None:
    """Item-sync helper should delegate planner input to the sync engine."""
    queue_sync, _player = _build_queue_sync()
    ma_snapshot = _MaQueueSnapshot(
        entries=(
            _LmsMirrorEntry(kind="url", value="a"),
            _LmsMirrorEntry(kind="track_id", value="1"),
        ),
        current_index=0,
        shuffle_enabled=False,
        repeat_mode=RepeatMode.OFF,
        protected_prefix_len=1,
    )
    lms_snapshot = _LmsQueueSnapshot(
        entries=(_LmsMirrorEntry(kind="url", value="a"),),
        current_index=0,
        shuffle_mode=0,
        repeat_mode=0,
        playback_mode="stop",
    )

    await queue_sync._sync_ma_items_to_lms(ma_snapshot, lms_snapshot)

    queue_sync._sync_engine.apply.assert_awaited_once()


def test_parse_lms_queue_state_rejects_invalid_rows_and_missing_fallback_model() -> None:
    """Parser should reject malformed rows and unknown partial rows without fallback model."""
    queue_sync, _player = _build_queue_sync()

    assert queue_sync._parse_lms_queue_state({"playlist_loop": [123]}) is None

    queue_sync._media_mapper.extract_lms_entry_from_playlist_item = MagicMock(return_value=None)
    queue_sync._lms_model = None
    parsed = queue_sync._parse_lms_queue_state({"playlist_loop": [{"title": "x"}]})
    assert parsed is None


@pytest.mark.asyncio
async def test_collect_ma_lms_queue_entries_covers_empty_and_url_mapping_paths() -> None:
    """Entry collection should support empty queue and URL-native rows with redirect wrapping."""
    queue_sync, player = _build_queue_sync()
    player.mass.player_queues.items.return_value = []
    assert await queue_sync._collect_ma_lms_queue_entries() == ()

    item = SimpleNamespace(
        uri="http://direct/stream",
        queue_id="q",
        queue_item_id="i1",
        name="Queue Row",
        media_item=None,
    )
    player.mass.player_queues.items.return_value = [item]
    queue_sync._resolve_ma_uri_to_lms_entry = AsyncMock(
        return_value=LmsQueueEntry(kind="url", value="http://direct/stream")
    )

    entries = await queue_sync._collect_ma_lms_queue_entries()

    assert entries[0].kind == "url"
    assert entries[0].value == "http://redirect/item"


@pytest.mark.asyncio
async def test_collect_lms_snapshot_parses_status_on_success() -> None:
    """LMS snapshot collection should parse status payload on successful command."""
    queue_sync, player = _build_queue_sync()
    status = {"playlist_loop": []}
    player.provider.get_player_queue_status = AsyncMock(return_value=status)
    expected = _LmsQueueSnapshot((), 0, 0, 0, "stop")
    queue_sync._parse_lms_queue_state = Mock(return_value=expected)

    parsed = await queue_sync._collect_lms_snapshot()

    assert parsed == expected


@pytest.mark.asyncio
async def test_sync_ma_position_noops_for_empty_entries_and_same_index() -> None:
    """Cursor sync should no-op for empty queues and already aligned indexes."""
    queue_sync, player = _build_queue_sync()
    lms_snapshot = _LmsQueueSnapshot((), 0, 0, 0, "stop")

    await queue_sync._sync_ma_position_to_lms(
        _MaQueueSnapshot((), 1, False, RepeatMode.OFF, 0),
        lms_snapshot,
    )
    await queue_sync._sync_ma_position_to_lms(
        _MaQueueSnapshot(
            entries=(_LmsMirrorEntry(kind="url", value="a"),),
            current_index=0,
            shuffle_enabled=False,
            repeat_mode=RepeatMode.OFF,
            protected_prefix_len=0,
        ),
        _LmsQueueSnapshot(
            entries=(_LmsMirrorEntry(kind="url", value="a"),),
            current_index=0,
            shuffle_mode=0,
            repeat_mode=0,
            playback_mode="stop",
        ),
    )

    player.provider.send_player_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_lms_modes_to_ma_returns_when_queue_missing() -> None:
    """Mode application should no-op when MA queue object is unavailable."""
    queue_sync, player = _build_queue_sync()
    player.mass.player_queues.get.return_value = None

    await queue_sync._apply_lms_modes_to_ma(
        _LmsQueueSnapshot(
            entries=(),
            current_index=0,
            shuffle_mode=0,
            repeat_mode=0,
            playback_mode="stop",
        )
    )

    player.mass.player_queues.set_repeat.assert_not_awaited()
    player.mass.player_queues.set_shuffle.assert_not_awaited()


@pytest.mark.asyncio
async def test_rebuild_lms_tail_returns_when_snapshot_unavailable_before_or_during_delete_loop() -> (
    None
):
    """Tail rebuild should abort safely if LMS snapshot cannot be refreshed."""
    queue_sync, _player = _build_queue_sync()
    queue_sync._collect_lms_snapshot = AsyncMock(return_value=None)
    queue_sync._delete_lms_index = AsyncMock()
    queue_sync._append_lms_entry = AsyncMock()

    await queue_sync._rebuild_lms_tail((_LmsMirrorEntry(kind="url", value="x"),), 1)
    queue_sync._delete_lms_index.assert_not_awaited()

    queue_sync._collect_lms_snapshot = AsyncMock(
        side_effect=[
            _LmsQueueSnapshot(
                entries=(
                    _LmsMirrorEntry(kind="url", value="a"),
                    _LmsMirrorEntry(kind="url", value="b"),
                ),
                current_index=0,
                shuffle_mode=0,
                repeat_mode=0,
                playback_mode="stop",
            ),
            None,
        ]
    )
    await queue_sync._rebuild_lms_tail(
        (
            _LmsMirrorEntry(kind="url", value="a"),
            _LmsMirrorEntry(kind="url", value="x"),
        ),
        1,
    )
    queue_sync._delete_lms_index.assert_awaited_once_with(1)


@pytest.mark.asyncio
async def test_move_and_delete_lms_index_send_expected_commands() -> None:
    """Index mutation helpers should emit direct LMS move/delete commands."""
    queue_sync, player = _build_queue_sync()

    await queue_sync._move_lms_index(5, 2)
    await queue_sync._delete_lms_index(3)

    player.provider.move_player_queue_item.assert_awaited_once_with("player-1", 5, 2)
    player.provider.delete_player_queue_item.assert_awaited_once_with("player-1", 3)


def test_determine_protected_prefix_falls_back_to_current_index_when_buffer_index_missing() -> None:
    """Protected prefix should use current_index when index_in_buffer is unavailable."""
    queue = SimpleNamespace(
        state=PlaybackState.PAUSED,
        index_in_buffer=None,
        current_index=4,
    )

    assert LyrionQueueSync._determine_protected_prefix(queue) == 5


@pytest.mark.asyncio
async def test_resolve_ma_uri_to_lms_entry_delegates_to_media_mapper() -> None:
    """URI resolver wrapper should delegate to media mapper implementation."""
    queue_sync, _player = _build_queue_sync()
    queue_sync._media_mapper.resolve_ma_uri_to_lms_queue_entry = AsyncMock(
        return_value=LmsQueueEntry(kind="track_id", value="42")
    )

    resolved = await queue_sync._resolve_ma_uri_to_lms_entry("dummy://uri")

    assert resolved == LmsQueueEntry(kind="track_id", value="42")


@pytest.mark.asyncio
async def test_add_url_entry_to_lms_reraises_provider_unavailable() -> None:
    """URL add should propagate provider-unavailable errors without fallback."""
    queue_sync, player = _build_queue_sync()
    player.provider.add_player_url_to_queue = AsyncMock(
        side_effect=ProviderUnavailableError("offline")
    )

    with pytest.raises(ProviderUnavailableError):
        await queue_sync._add_url_entry_to_lms(_LmsMirrorEntry(kind="url", value="http://x"))
