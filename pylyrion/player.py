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
