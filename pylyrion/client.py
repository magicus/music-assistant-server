"""High-level pylyrion client facade."""

from __future__ import annotations

from pylyrion.library import LyrionLibraryClient
from pylyrion.models import LyrionEndpoint
from pylyrion.player import LyrionPlayerClient
from pylyrion.session import LyrionSession


class LyrionClient:
    """Bundle connection, library browsing, and player control."""

    def __init__(self, session: LyrionSession) -> None:
        """Initialize the facade with a shared transport session."""
        self.session = session
        self.library = LyrionLibraryClient(session)
        self.players = LyrionPlayerClient(session)


__all__ = [
    "LyrionClient",
    "LyrionEndpoint",
    "LyrionLibraryClient",
    "LyrionPlayerClient",
]
