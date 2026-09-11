"""High-level player command facade with status verification helpers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, cast

from pylyrion.errors import LyrionProtocolError, LyrionRequestError, LyrionTimeoutError
from pylyrion.player import LyrionPlayerClient
from pylyrion.status_stream import PlayerStatusStream


class LyrionServerControl:
    """Expose high-level player commands backed by pylyrion primitives."""

    def __init__(
        self,
        *,
        get_players_client: Callable[[], LyrionPlayerClient],
        status_stream: PlayerStatusStream,
        unavailable_error_factory: Callable[[Exception], Exception],
    ) -> None:
        """
        Initialize server control facade.

        :param get_players_client: Factory returning player client bound to current endpoint.
        :param status_stream: Status stream used for command verification and snapshots.
        :param unavailable_error_factory: Error mapper for transport/protocol failures.
        """
        self._get_players_client = get_players_client
        self._status_stream = status_stream
        self._unavailable_error_factory = unavailable_error_factory

    async def get_player_status(self, player_id: str) -> dict[str, Any]:
        """Return runtime status for one player."""
        return await self._run_player_call(lambda: self._players.get_status(player_id))

    async def play_player(self, player_id: str) -> dict[str, Any]:
        """Resume playback for one player."""
        return await self._run_player_call(lambda: self._players.play(player_id))

    async def pause_player(self, player_id: str) -> dict[str, Any]:
        """Pause playback for one player."""
        return await self._run_player_call(lambda: self._players.pause(player_id))

    async def stop_player(self, player_id: str) -> dict[str, Any]:
        """Stop playback for one player."""
        return await self._run_player_call(lambda: self._players.stop(player_id))

    async def set_player_power(self, player_id: str, powered: bool) -> dict[str, Any]:
        """Set player power state."""
        return await self._run_player_call(lambda: self._players.set_power(player_id, powered))

    async def sync_player_to(
        self,
        player_id: str,
        leader_player_id: str,
    ) -> dict[str, Any]:
        """Join one player to an LMS sync leader."""
        return await self._run_player_call(
            lambda: self._players.sync_to(
                player_id,
                leader_player_id,
            )
        )

    async def unsync_player(self, player_id: str) -> dict[str, Any]:
        """Remove one player from LMS sync grouping."""
        return await self._run_player_call(lambda: self._players.unsync(player_id))

    async def get_player_queue_status(
        self,
        player_id: str,
        *,
        offset: int = 0,
        limit: int,
    ) -> dict[str, Any]:
        """Return queue-inclusive status payload."""
        return await self._run_player_call(
            lambda: self._players.get_queue_status(
                player_id,
                offset=offset,
                limit=limit,
            )
        )

    async def set_player_queue_index(self, player_id: str, index: int | str) -> dict[str, Any]:
        """Set active queue index."""
        return await self._run_player_call(lambda: self._players.set_queue_index(player_id, index))

    async def next_player_track(self, player_id: str) -> dict[str, Any]:
        """Skip to next queue entry."""
        return await self._run_player_call(lambda: self._players.next_track(player_id))

    async def previous_player_track(self, player_id: str) -> dict[str, Any]:
        """Skip to previous queue entry."""
        return await self._run_player_call(lambda: self._players.previous_track(player_id))

    async def set_player_volume(self, player_id: str, volume_level: int) -> dict[str, Any]:
        """Set player volume."""
        return await self._run_player_call(
            lambda: self._players.set_volume(
                player_id,
                volume_level,
            )
        )

    async def set_player_muted(self, player_id: str, muted: bool) -> dict[str, Any]:
        """Set player mute state."""
        return await self._run_player_call(lambda: self._players.set_muted(player_id, muted))

    async def seek_player(self, player_id: str, position: int) -> dict[str, Any]:
        """Seek playback position."""
        return await self._run_player_call(lambda: self._players.seek(player_id, position))

    async def set_player_sync_volume(self, player_id: str, enabled: bool) -> dict[str, Any]:
        """Set LMS syncVolume preference."""
        return await self._run_player_call(
            lambda: self._players.set_sync_volume(player_id, enabled)
        )

    async def set_player_repeat_mode(self, player_id: str, repeat_mode: int) -> dict[str, Any]:
        """Set repeat mode."""
        return await self._run_player_call(
            lambda: self._players.set_repeat_mode(
                player_id,
                repeat_mode,
            )
        )

    async def set_player_shuffle_mode(
        self,
        player_id: str,
        shuffle_mode: int,
    ) -> dict[str, Any]:
        """Set shuffle mode."""
        return await self._run_player_call(
            lambda: self._players.set_shuffle_mode(
                player_id,
                shuffle_mode,
            )
        )

    async def clear_player_queue(self, player_id: str) -> dict[str, Any]:
        """Clear queue for one player."""
        return await self._run_player_call(lambda: self._players.clear_queue(player_id))

    async def add_player_track_id_to_queue(
        self,
        player_id: str,
        track_id: str,
        *,
        command: str = "add",
    ) -> dict[str, Any]:
        """Add or load one track id to queue."""
        return await self._run_player_call(
            lambda: self._players.add_track_id(
                player_id,
                track_id,
                command=command,
            )
        )

    async def add_player_url_to_queue(
        self,
        player_id: str,
        url: str,
        title: str | None = None,
        artist: str | None = None,
        album: str | None = None,
    ) -> dict[str, Any]:
        """Add one URL to queue."""
        return await self._run_player_call(
            lambda: self._players.add_url_to_queue(
                player_id,
                url,
                title=title,
                artist=artist,
                album=album,
            )
        )

    async def play_player_url(self, player_id: str, url: str) -> dict[str, Any]:
        """Play one URL immediately."""
        return await self._run_player_call(lambda: self._players.play_url(player_id, url))

    async def append_player_url(self, player_id: str, url: str) -> dict[str, Any]:
        """Append one URL to queue."""
        return await self._run_player_call(lambda: self._players.add_url(player_id, url))

    async def move_player_queue_item(
        self,
        player_id: str,
        from_index: int,
        to_index: int,
    ) -> dict[str, Any]:
        """Move one queue item by index."""
        return await self._run_player_call(
            lambda: self._players.move_queue_item(
                player_id,
                from_index,
                to_index,
            )
        )

    async def delete_player_queue_item(self, player_id: str, index: int) -> dict[str, Any]:
        """Delete one queue item by index."""
        return await self._run_player_call(
            lambda: self._players.delete_queue_item(
                player_id,
                index,
            )
        )

    async def send_player_command(
        self,
        player_id: str,
        command: list[Any],
    ) -> dict[str, Any]:
        """Send one raw command to a player."""
        return await self._run_player_call(lambda: self._players.send_command(player_id, command))

    def get_last_status_seen_at(self, player_id: str) -> float | None:
        """Return timestamp of last status update for one player."""
        return self._status_stream.get_last_player_status_seen_at(player_id)

    def get_cached_status(self, player_id: str) -> dict[str, Any] | None:
        """Return cached status snapshot for one player."""
        snapshot = self._status_stream.get_player_status_snapshot(player_id)
        if snapshot is None:
            return None
        return dict(snapshot)

    async def verify_status_expectation(
        self,
        player_id: str,
        baseline: float | None,
        expectation: Callable[[dict[str, Any]], bool] | None = None,
        expected_state: str = "status update",
    ) -> bool:
        """Verify expected status via stream events and polling fallback."""
        return await self._status_stream.verify_player_status_expectation(
            player_id,
            baseline,
            expectation,
            expected_state,
        )

    async def play_player_with_verify(self, player_id: str) -> None:
        """Resume playback and verify mode transition to play."""
        await self._run_command_with_status_verify(
            player_id,
            command=lambda: self.play_player(player_id),
            expectation=lambda status: _get_status_str(status, "mode") == "play",
            expected_state="mode=play",
        )

    async def pause_player_with_verify(self, player_id: str) -> None:
        """Pause playback and verify mode transition to pause."""
        await self._run_command_with_status_verify(
            player_id,
            command=lambda: self.pause_player(player_id),
            expectation=lambda status: _get_status_str(status, "mode") == "pause",
            expected_state="mode=pause",
        )

    async def stop_player_with_verify(self, player_id: str) -> None:
        """Stop playback and verify mode transition to stop."""
        await self._run_command_with_status_verify(
            player_id,
            command=lambda: self.stop_player(player_id),
            expectation=lambda status: _get_status_str(status, "mode") == "stop",
            expected_state="mode=stop",
        )

    async def set_player_power_with_verify(self, player_id: str, powered: bool) -> None:
        """Set power and verify power status."""
        target = 1 if powered else 0
        await self._run_command_with_status_verify(
            player_id,
            command=lambda: self.set_player_power(player_id, powered),
            expectation=lambda status: _get_status_int(status, "power") == target,
            expected_state=f"power={target}",
        )

    async def set_player_volume_with_verify(self, player_id: str, volume_level: int) -> None:
        """Set volume and verify mixer volume."""
        target = max(0, min(100, int(volume_level)))
        await self._run_command_with_status_verify(
            player_id,
            command=lambda: self.set_player_volume(player_id, target),
            expectation=lambda status: _get_status_int(status, "mixer volume") == target,
            expected_state=f"mixer volume={target}",
        )

    async def set_player_muted_with_verify(self, player_id: str, muted: bool) -> None:
        """Set mute and verify mixer muting status."""
        target = 1 if muted else 0
        await self._run_command_with_status_verify(
            player_id,
            command=lambda: self.set_player_muted(player_id, muted),
            expectation=lambda status: _get_status_int(status, "mixer muting") == target,
            expected_state=f"mixer muting={target}",
        )

    async def next_player_track_with_verify(self, player_id: str) -> None:
        """Skip to next track and verify queue index transition."""
        previous = self.get_cached_status(player_id)
        previous_index = _get_status_int(previous or {}, "playlist_cur_index")
        if previous_index is None:
            await self._run_command_with_status_verify(
                player_id,
                command=lambda: self.next_player_track(player_id),
            )
            return

        await self._run_command_with_status_verify(
            player_id,
            command=lambda: self.next_player_track(player_id),
            expectation=lambda status: (
                _get_status_int(status, "playlist_cur_index") not in (None, previous_index)
            ),
            expected_state="playlist_cur_index changed",
        )

    async def previous_player_track_with_verify(self, player_id: str) -> None:
        """Skip to previous track and verify queue index transition."""
        previous = self.get_cached_status(player_id)
        previous_index = _get_status_int(previous or {}, "playlist_cur_index")
        if previous_index is None:
            await self._run_command_with_status_verify(
                player_id,
                command=lambda: self.previous_player_track(player_id),
            )
            return

        await self._run_command_with_status_verify(
            player_id,
            command=lambda: self.previous_player_track(player_id),
            expectation=lambda status: (
                _get_status_int(status, "playlist_cur_index") not in (None, previous_index)
            ),
            expected_state="playlist_cur_index changed",
        )

    async def seek_player_with_verify(self, player_id: str, position: int) -> None:
        """Seek playback and verify time progression near target."""
        target = max(0, int(position))
        await self._run_command_with_status_verify(
            player_id,
            command=lambda: self.seek_player(player_id, target),
            expectation=lambda status: _time_matches_target(status, target),
            expected_state=f"time~={target}",
        )

    async def player_play(self, player_id: str) -> None:
        """Resume playback using verified command semantics."""
        await self.play_player_with_verify(player_id)

    async def player_pause(self, player_id: str) -> None:
        """Pause playback using verified command semantics."""
        await self.pause_player_with_verify(player_id)

    async def player_stop(self, player_id: str) -> None:
        """Stop playback using verified command semantics."""
        await self.stop_player_with_verify(player_id)

    async def player_set_power(self, player_id: str, powered: bool) -> None:
        """Set power using verified command semantics."""
        await self.set_player_power_with_verify(player_id, powered)

    async def player_set_volume(self, player_id: str, volume_level: int) -> None:
        """Set volume using verified command semantics."""
        await self.set_player_volume_with_verify(player_id, volume_level)

    async def player_set_muted(self, player_id: str, muted: bool) -> None:
        """Set mute using verified command semantics."""
        await self.set_player_muted_with_verify(player_id, muted)

    async def player_next_track(self, player_id: str) -> None:
        """Skip track using verified command semantics."""
        await self.next_player_track_with_verify(player_id)

    async def player_previous_track(self, player_id: str) -> None:
        """Back track using verified command semantics."""
        await self.previous_player_track_with_verify(player_id)

    async def player_seek(self, player_id: str, position: int) -> None:
        """Seek using verified command semantics."""
        await self.seek_player_with_verify(player_id, position)

    async def _run_command_with_status_verify(
        self,
        player_id: str,
        command: Callable[[], Awaitable[dict[str, Any]]],
        expectation: Callable[[dict[str, Any]], bool] | None = None,
        expected_state: str = "status update",
    ) -> None:
        """Run one player command and verify status expectation."""
        baseline = self.get_last_status_seen_at(player_id)
        await command()
        await self.verify_status_expectation(
            player_id,
            baseline,
            expectation=expectation,
            expected_state=expected_state,
        )

    @property
    def _players(self) -> LyrionPlayerClient:
        """Return players client for current endpoint."""
        return self._get_players_client()

    async def _run_player_call(
        self,
        callback: Callable[[], Awaitable[dict[str, object]]],
    ) -> dict[str, Any]:
        """Run one player-client call and normalize error mapping."""
        try:
            result = await callback()
        except (LyrionProtocolError, LyrionRequestError, LyrionTimeoutError) as err:
            raise self._unavailable_error_factory(err) from err
        return cast("dict[str, Any]", result)


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
