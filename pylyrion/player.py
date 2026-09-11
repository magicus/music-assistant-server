"""Raw player control helpers for pylyrion."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pylyrion.errors import LyrionRequestError
from pylyrion.session import LyrionSession


class LyrionPlayerClient:
    """Expose player control and status operations."""

    def __init__(self, session: LyrionSession) -> None:
        self._session = session

    async def get_status(self, player_id: str) -> dict[str, object]:
        """Return runtime status for one player."""
        result = await self._session.request(player_id, ["status", "-", 1])
        return dict(result)

    async def get_players_page(self, offset: int, limit: int) -> list[dict[str, object]]:
        """Return one paged LMS player listing."""
        result = await self._session.request("", ["players", offset, limit])
        players_loop = result.get("players_loop")
        if not isinstance(players_loop, list):
            return []
        return [player for player in players_loop if isinstance(player, dict)]

    async def send_command(
        self,
        player_id: str,
        command: Sequence[Any],
    ) -> dict[str, object]:
        """Send one raw LMS command for a specific player."""
        result = await self._session.request(player_id, list(command))
        return dict(result)

    async def play(self, player_id: str) -> dict[str, object]:
        """Resume playback for one player."""
        return await self.send_command(player_id, ["play"])

    async def pause(self, player_id: str) -> dict[str, object]:
        """Pause playback for one player."""
        return await self.send_command(player_id, ["pause", 1])

    async def stop(self, player_id: str) -> dict[str, object]:
        """Stop playback for one player."""
        return await self.send_command(player_id, ["stop"])

    async def set_power(self, player_id: str, powered: bool) -> dict[str, object]:
        """Set power state for one player."""
        return await self.send_command(player_id, ["power", 1 if powered else 0])

    async def unsync(self, player_id: str) -> dict[str, object]:
        """Remove one player from its current LMS sync group."""
        return await self.send_command(player_id, ["sync", "-"])

    async def sync_to(self, player_id: str, leader_player_id: str) -> dict[str, object]:
        """Join one player to another LMS sync leader."""
        return await self.send_command(player_id, ["sync", leader_player_id])

    async def get_queue_status(
        self,
        player_id: str,
        offset: int = 0,
        limit: int = 500,
    ) -> dict[str, object]:
        """Return LMS status payload with queue rows for one player."""
        return await self.send_command(player_id, ["status", offset, limit])

    async def set_queue_index(self, player_id: str, index: int | str) -> dict[str, object]:
        """Set active queue index for one player."""
        return await self.send_command(player_id, ["playlist", "index", index])

    async def next_track(self, player_id: str) -> dict[str, object]:
        """Skip to the next queue entry for one player."""
        return await self.set_queue_index(player_id, "+1")

    async def previous_track(self, player_id: str) -> dict[str, object]:
        """Skip to the previous queue entry for one player."""
        return await self.set_queue_index(player_id, "-1")

    async def set_repeat_mode(self, player_id: str, repeat_mode: int) -> dict[str, object]:
        """Set LMS queue repeat mode for one player."""
        return await self.send_command(player_id, ["playlist", "repeat", repeat_mode])

    async def set_shuffle_mode(self, player_id: str, shuffle_mode: int) -> dict[str, object]:
        """Set LMS queue shuffle mode for one player."""
        return await self.send_command(player_id, ["playlist", "shuffle", shuffle_mode])

    async def clear_queue(self, player_id: str) -> dict[str, object]:
        """Clear LMS queue for one player."""
        return await self.send_command(player_id, ["playlist", "clear"])

    async def set_volume(self, player_id: str, volume_level: int) -> dict[str, object]:
        """Set playback volume for one player."""
        return await self.send_command(
            player_id,
            ["mixer", "volume", max(0, min(100, int(volume_level)))],
        )

    async def set_muted(self, player_id: str, muted: bool) -> dict[str, object]:
        """Set playback mute state for one player."""
        return await self.send_command(player_id, ["mixer", "muting", 1 if muted else 0])

    async def seek(self, player_id: str, position: int) -> dict[str, object]:
        """Seek playback to one position in whole seconds."""
        return await self.send_command(player_id, ["time", max(0, int(position))])

    async def set_sync_volume(self, player_id: str, enabled: bool) -> dict[str, object]:
        """Set LMS syncVolume preference for one player."""
        return await self.send_command(player_id, ["playerpref", "syncVolume", 1 if enabled else 0])

    async def add_track_id(
        self,
        player_id: str,
        track_id: str,
        command: str = "add",
    ) -> dict[str, object]:
        """Add or load one track_id via LMS playlistcontrol command."""
        return await self.send_command(
            player_id,
            ["playlistcontrol", f"cmd:{command}", f"track_id:{track_id}"],
        )

    async def move_queue_item(
        self,
        player_id: str,
        from_index: int,
        to_index: int,
    ) -> dict[str, object]:
        """Move one LMS queue item by index."""
        return await self.send_command(
            player_id,
            ["playlist", "move", from_index, to_index],
        )

    async def delete_queue_item(self, player_id: str, index: int) -> dict[str, object]:
        """Delete one LMS queue item by index."""
        return await self.send_command(player_id, ["playlist", "delete", index])

    async def play_url(self, player_id: str, url: str) -> dict[str, object]:
        """Start playback of one URL on the player."""
        return await self.send_command(player_id, ["playlist", "play", url])

    async def add_url(self, player_id: str, url: str) -> dict[str, object]:
        """Append one URL to the player queue."""
        return await self.send_command(player_id, ["playlist", "add", url])

    async def add_url_to_queue(
        self,
        player_id: str,
        url: str,
        title: str | None = None,
        artist: str | None = None,
        album: str | None = None,
    ) -> dict[str, object]:
        """Add one URL to the queue, with fallback for older LMS payload support."""
        command: list[Any] = [
            "playlistcontrol",
            "cmd:add",
            f"url:{url}",
        ]
        if title:
            command.append(f"title:{title}")
        if artist:
            command.append(f"artist:{artist}")
        if album:
            command.append(f"album:{album}")
        try:
            return await self.send_command(player_id, command)
        except LyrionRequestError:
            return await self.send_command(player_id, ["playlist", "add", url])
