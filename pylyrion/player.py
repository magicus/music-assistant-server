"""Raw player control helpers for pylyrion."""

from __future__ import annotations

from pylyrion.session import LyrionSession


class LyrionPlayerClient:
    """Expose player control and status operations."""

    def __init__(self, session: LyrionSession) -> None:
        self._session = session

    async def get_status(self, player_id: str) -> dict[str, object]:
        """Return runtime status for one player."""
        result = await self._session.request(player_id, ["status", "-", 1])
        return dict(result)
