"""Unit tests for robust LMS queue parsing in the Lyrion player provider."""

from __future__ import annotations

from typing import Any

from music_assistant.providers.lyrion_player.media_mapper import LmsQueueEntry, LyrionMediaMapper
from music_assistant.providers.lyrion_player.queue.queue_sync import (
    LyrionQueueSync,
    _LmsMirrorEntry,
    _LmsQueueSnapshot,
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
    queue_sync._media_mapper = _MapperStub()
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
    queue_sync._media_mapper = _MapperStub()
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
