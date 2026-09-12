"""Unit tests for robust LMS queue parsing in the Lyrion player provider."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.enums import RepeatMode

from music_assistant.providers.lyrion_player.media_mapper import LmsQueueEntry, LyrionMediaMapper
from music_assistant.providers.lyrion_player.queue.queue_sync import (
    LyrionQueueSync,
    _LmsMirrorEntry,
    _LmsQueueSnapshot,
    _MaQueueSnapshot,
)


class _MapperStub:
    """Minimal mapper stub used to drive queue parser behavior."""

    @staticmethod
    def extract_lms_entry_from_playlist_item(item: dict[str, object]) -> LmsQueueEntry | None:
        """Return URL entry when URL/title contains an http URL, else None."""
        for key in ("url", "title"):
            value = item.get(key)
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                return LmsQueueEntry(kind="url", value=value)
        return None


def test_media_mapper_extracts_url_from_title_when_url_keys_missing() -> None:
    """URL-like title should be accepted as queue URL when dedicated URL fields are absent."""
    item: dict[str, object] = {
        "id": "-123",
        "title": "http://queue.local/track-a.mp3",
    }

    entry = LyrionMediaMapper.extract_lms_entry_from_playlist_item(item)

    assert entry is not None
    assert entry.kind == "url"
    assert entry.value == "http://queue.local/track-a.mp3"


def test_parse_lms_queue_state_reuses_previous_entry_for_transient_partial_row() -> None:
    """Parser should reuse previous queue identity when one row is temporarily unparseable."""
    queue_sync = object.__new__(LyrionQueueSync)
    queue_sync._media_mapper = _MapperStub()  # type: ignore[assignment]
    queue_sync._lms_model = _LmsQueueSnapshot(
        entries=(
            _LmsMirrorEntry(kind="url", value="http://queue.local/track-a.mp3"),
            _LmsMirrorEntry(kind="url", value="http://queue.local/track-b.mp3"),
        ),
        current_index=0,
        shuffle_mode=0,
        repeat_mode=0,
        playback_mode="play",
    )

    status: dict[str, Any] = {
        "playlist_loop": [
            {"title": "http://queue.local/track-a.mp3", "id": "-1"},
            {"title": None, "id": "-2"},
        ],
        "playlist_cur_index": 0,
        "playlist shuffle": 0,
        "playlist repeat": 0,
        "mode": "play",
    }

    parsed = queue_sync._parse_lms_queue_state(status)

    assert parsed is not None
    assert parsed.entries[0].value == "http://queue.local/track-a.mp3"
    assert parsed.entries[1].value == "http://queue.local/track-b.mp3"


def test_parse_lms_queue_state_returns_none_for_partial_row_after_length_change() -> None:
    """Parser should refuse fallback when queue length changed and identity reuse is unsafe."""
    queue_sync = object.__new__(LyrionQueueSync)
    queue_sync._media_mapper = _MapperStub()  # type: ignore[assignment]
    queue_sync._lms_model = _LmsQueueSnapshot(
        entries=(
            _LmsMirrorEntry(kind="url", value="http://queue.local/track-a.mp3"),
            _LmsMirrorEntry(kind="url", value="http://queue.local/track-b.mp3"),
        ),
        current_index=0,
        shuffle_mode=0,
        repeat_mode=0,
        playback_mode="play",
    )

    status: dict[str, Any] = {
        "playlist_loop": [
            {"title": "http://queue.local/track-a.mp3", "id": "-1"},
            {"title": None, "id": "-2"},
            {"title": "http://queue.local/track-c.mp3", "id": "-3"},
        ],
        "playlist_cur_index": 0,
        "playlist shuffle": 0,
        "playlist repeat": 0,
        "mode": "play",
    }

    parsed = queue_sync._parse_lms_queue_state(status)

    assert parsed is None


@pytest.mark.asyncio
async def test_apply_lms_modes_maps_album_shuffle_to_ma_enabled() -> None:
    """LMS shuffle album mode (2) should map to MA shuffle_enabled=True."""
    player_queues = Mock()
    queue = SimpleNamespace(repeat_mode=RepeatMode.OFF, shuffle_enabled=False)
    player_queues.get = Mock(return_value=queue)
    player_queues.set_repeat = AsyncMock()
    player_queues.set_shuffle = AsyncMock()

    queue_sync = object.__new__(LyrionQueueSync)
    queue_sync.player = cast(
        "Any",
        SimpleNamespace(
            player_id="test-player",
            mass=SimpleNamespace(player_queues=player_queues),
        ),
    )

    await queue_sync._apply_lms_modes_to_ma(
        _LmsQueueSnapshot(
            entries=(),
            current_index=0,
            shuffle_mode=2,
            repeat_mode=0,
            playback_mode="stop",
        )
    )

    player_queues.set_repeat.assert_not_awaited()
    player_queues.set_shuffle.assert_awaited_once_with("test-player", True)


@pytest.mark.asyncio
async def test_sync_ma_queue_to_lms_reruns_once_when_pending_request_arrives() -> None:
    """A queue update that arrives during sync should trigger one follow-up MA->LMS pass."""
    entries = (_LmsMirrorEntry(kind="url", value="http://queue.local/track-a.mp3"),)
    ma_snapshot = _MaQueueSnapshot(
        entries=entries,
        current_index=0,
        shuffle_enabled=False,
        repeat_mode=RepeatMode.OFF,
        protected_prefix_len=0,
    )
    lms_snapshot = _LmsQueueSnapshot(
        entries=entries,
        current_index=0,
        shuffle_mode=0,
        repeat_mode=0,
        playback_mode="stop",
    )

    queue_sync = object.__new__(LyrionQueueSync)
    queue_sync.player = cast(
        "Any",
        SimpleNamespace(
            player_id="test-player",
            provider=SimpleNamespace(),
            logger=Mock(),
        ),
    )
    queue_sync._syncing_from_lms_queue = False
    queue_sync._syncing_to_lms_queue = False
    queue_sync._ma_queue_sync_pending = False
    queue_sync._ma_queue_sync_pending_sync_items = False
    queue_sync._lms_queue_sync_pending = False
    queue_sync._ma_model = None
    queue_sync._lms_model = lms_snapshot
    queue_sync._lms_shuffle_mode_raw = 0
    queue_sync._identity_map = {}
    queue_sync._media_mapper = Mock()
    queue_sync._collect_ma_snapshot = AsyncMock(return_value=ma_snapshot)
    queue_sync._collect_lms_snapshot = AsyncMock(return_value=lms_snapshot)
    queue_sync._within_sync_limits = Mock(return_value=True)
    queue_sync._sync_ma_items_to_lms = AsyncMock()
    queue_sync._sync_ma_modes_to_lms = AsyncMock()

    position_calls = 0

    async def _sync_position(
        first_snapshot: _MaQueueSnapshot, second_snapshot: _LmsQueueSnapshot
    ) -> None:
        nonlocal position_calls
        del first_snapshot, second_snapshot
        position_calls += 1
        if position_calls == 1:
            await queue_sync.sync_ma_queue_to_lms(sync_items=True)

    queue_sync._sync_ma_position_to_lms = AsyncMock(side_effect=_sync_position)

    await queue_sync.sync_ma_queue_to_lms(sync_items=False)

    assert queue_sync._collect_ma_snapshot.await_count == 2
    assert queue_sync._sync_ma_items_to_lms.await_count == 1
    assert queue_sync._sync_ma_position_to_lms.await_count == 2
    assert queue_sync._sync_ma_modes_to_lms.await_count == 2
