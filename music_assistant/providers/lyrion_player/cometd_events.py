"""Pure internal event models for Lyrion CometD integration."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

StatusPayload = dict[str, Any]


@dataclass(frozen=True)
class LmsPlayerEvent:
    """Base class for normalized internal LMS player events."""

    player_id: str


@dataclass(frozen=True)
class LmsPlayerStatusUpdatedEvent(LmsPlayerEvent):
    """Event emitted whenever a player status snapshot was updated."""

    status: StatusPayload
    is_initial: bool


@dataclass(frozen=True)
class LmsPlayerPlaybackChangedEvent(LmsPlayerEvent):
    """Event emitted when playback mode changes."""

    old_mode: str
    new_mode: str


@dataclass(frozen=True)
class LmsPlayerPowerChangedEvent(LmsPlayerEvent):
    """Event emitted when power state changes."""

    old_powered: bool
    new_powered: bool


@dataclass(frozen=True)
class LmsPlayerVolumeChangedEvent(LmsPlayerEvent):
    """Event emitted when volume changes."""

    old_volume: int
    new_volume: int


@dataclass(frozen=True)
class LmsPlayerRepeatChangedEvent(LmsPlayerEvent):
    """Event emitted when repeat mode changes."""

    old_repeat: int
    new_repeat: int


@dataclass(frozen=True)
class LmsPlayerShuffleChangedEvent(LmsPlayerEvent):
    """Event emitted when shuffle mode changes."""

    old_shuffle: int
    new_shuffle: int


@dataclass(frozen=True)
class LmsPlayerSeekedEvent(LmsPlayerEvent):
    """Event emitted when playback position changes within the same track."""

    old_time: float
    new_time: float


@dataclass(frozen=True)
class LmsPlayerPlaylistChangedEvent(LmsPlayerEvent):
    """Event emitted when LMS signals that queue composition changed."""

    old_playlist_timestamp: float | None
    new_playlist_timestamp: float | None
    old_playlist_tracks: int | None
    new_playlist_tracks: int | None


LmsPlayerEventCallback = Callable[[LmsPlayerEvent], Awaitable[None]]


__all__ = [
    "LmsPlayerEvent",
    "LmsPlayerPlaybackChangedEvent",
    "LmsPlayerPlaylistChangedEvent",
    "LmsPlayerPowerChangedEvent",
    "LmsPlayerRepeatChangedEvent",
    "LmsPlayerSeekedEvent",
    "LmsPlayerShuffleChangedEvent",
    "LmsPlayerStatusUpdatedEvent",
    "LmsPlayerVolumeChangedEvent",
]
