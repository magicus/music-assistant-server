"""Neutral playerstatus merge and diff events for CometD consumers."""

from __future__ import annotations

from dataclasses import dataclass

from .helpers import (
    StatusPayload,
    _get_float,
    _get_int,
    _get_mode,
    _get_power,
    _is_invalid_player_payload,
    _same_active_track,
)


@dataclass(frozen=True)
class PlayerStatusUpdated:
    """Merged playerstatus snapshot emitted for every valid update."""

    player_id: str
    status: StatusPayload
    is_initial: bool


@dataclass(frozen=True)
class PlayerPlaybackChanged:
    """Playback mode transition."""

    player_id: str
    old_mode: str
    new_mode: str


@dataclass(frozen=True)
class PlayerPowerChanged:
    """Power transition."""

    player_id: str
    old_powered: bool
    new_powered: bool


@dataclass(frozen=True)
class PlayerVolumeChanged:
    """Volume transition."""

    player_id: str
    old_volume: int
    new_volume: int


@dataclass(frozen=True)
class PlayerRepeatChanged:
    """Repeat-mode transition."""

    player_id: str
    old_repeat: int
    new_repeat: int


@dataclass(frozen=True)
class PlayerShuffleChanged:
    """Shuffle-mode transition."""

    player_id: str
    old_shuffle: int
    new_shuffle: int


@dataclass(frozen=True)
class PlayerSeeked:
    """Seek transition within the same active track."""

    player_id: str
    old_time: float
    new_time: float


@dataclass(frozen=True)
class PlayerPlaylistChanged:
    """Playlist shape or cursor transition."""

    player_id: str
    old_playlist_timestamp: float | None
    new_playlist_timestamp: float | None
    old_playlist_tracks: int | None
    new_playlist_tracks: int | None


NormalizedPlayerStatusEvent = (
    PlayerStatusUpdated
    | PlayerPlaybackChanged
    | PlayerPowerChanged
    | PlayerVolumeChanged
    | PlayerRepeatChanged
    | PlayerShuffleChanged
    | PlayerSeeked
    | PlayerPlaylistChanged
)


def merge_player_status(
    player_id: str,
    previous: StatusPayload | None,
    partial: StatusPayload,
) -> tuple[StatusPayload | None, list[NormalizedPlayerStatusEvent], bool]:
    """
    Merge one status payload and emit normalized transitions.

    :param player_id: LMS player id.
    :param previous: Previous merged playerstatus if known.
    :param partial: New partial status payload from CometD.
    :returns: Tuple of merged status (or None for invalid-player payloads),
        emitted normalized events, and invalid-player flag.
    """
    if _is_invalid_player_payload(partial):
        return (
            None,
            [
                PlayerStatusUpdated(
                    player_id=player_id,
                    status=dict(partial),
                    is_initial=False,
                )
            ],
            True,
        )

    merged = dict(previous or {})
    merged.update(partial)
    is_initial = previous is None
    events: list[NormalizedPlayerStatusEvent] = [
        PlayerStatusUpdated(
            player_id=player_id,
            status=dict(merged),
            is_initial=is_initial,
        )
    ]
    if is_initial:
        return merged, events, False

    assert previous is not None

    if (old_mode := _get_mode(previous)) != (new_mode := _get_mode(merged)):
        events.append(
            PlayerPlaybackChanged(
                player_id=player_id,
                old_mode=old_mode,
                new_mode=new_mode,
            )
        )

    old_power = _get_power(previous)
    new_power = _get_power(merged)
    if old_power is not None and new_power is not None and old_power != new_power:
        events.append(
            PlayerPowerChanged(
                player_id=player_id,
                old_powered=old_power,
                new_powered=new_power,
            )
        )

    old_volume = _get_int(previous, "mixer volume")
    new_volume = _get_int(merged, "mixer volume")
    if old_volume is not None and new_volume is not None and old_volume != new_volume:
        events.append(
            PlayerVolumeChanged(
                player_id=player_id,
                old_volume=old_volume,
                new_volume=new_volume,
            )
        )

    old_repeat = _get_int(previous, "playlist repeat")
    new_repeat = _get_int(merged, "playlist repeat")
    if old_repeat is not None and new_repeat is not None and old_repeat != new_repeat:
        events.append(
            PlayerRepeatChanged(
                player_id=player_id,
                old_repeat=old_repeat,
                new_repeat=new_repeat,
            )
        )

    old_shuffle = _get_int(previous, "playlist shuffle")
    new_shuffle = _get_int(merged, "playlist shuffle")
    if old_shuffle is not None and new_shuffle is not None and old_shuffle != new_shuffle:
        events.append(
            PlayerShuffleChanged(
                player_id=player_id,
                old_shuffle=old_shuffle,
                new_shuffle=new_shuffle,
            )
        )

    old_time = _get_float(previous, "time")
    new_time = _get_float(merged, "time")
    if old_time is not None and new_time is not None and old_time != new_time:
        if _same_active_track(previous, merged):
            events.append(
                PlayerSeeked(
                    player_id=player_id,
                    old_time=old_time,
                    new_time=new_time,
                )
            )

    old_index = _get_int(previous, "playlist_cur_index")
    new_index = _get_int(merged, "playlist_cur_index")
    old_timestamp = _get_float(previous, "playlist_timestamp")
    new_timestamp = _get_float(merged, "playlist_timestamp")
    old_tracks = _get_int(previous, "playlist_tracks")
    new_tracks = _get_int(merged, "playlist_tracks")
    if old_index != new_index or old_timestamp != new_timestamp or old_tracks != new_tracks:
        events.append(
            PlayerPlaylistChanged(
                player_id=player_id,
                old_playlist_timestamp=old_timestamp,
                new_playlist_timestamp=new_timestamp,
                old_playlist_tracks=old_tracks,
                new_playlist_tracks=new_tracks,
            )
        )

    return merged, events, False
