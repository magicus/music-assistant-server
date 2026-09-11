"""Raw player control helpers for pylyrion."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pylyrion.session import LyrionSession


class LyrionPlayerClient:
    """Expose player control and status operations."""

    def __init__(self, session: LyrionSession) -> None:
        self._session = session

    async def get_status(self, player_id: str) -> dict[str, object]:
        """Return runtime status for one player."""
        result = await self._session.request(player_id, ["status", "-", 1])
        return dict(result)

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
