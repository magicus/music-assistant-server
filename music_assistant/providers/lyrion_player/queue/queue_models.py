"""Queue sync models used by the Lyrion queue adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from music_assistant_models.enums import RepeatMode


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
