"""pylyrion exception types."""

from __future__ import annotations


class LyrionError(RuntimeError):
    """Base error raised by pylyrion."""


class LyrionRequestError(LyrionError):
    """Raised when a request cannot be sent or completed."""


class LyrionTimeoutError(LyrionRequestError):
    """Raised when Lyrion does not respond in time."""


class LyrionProtocolError(LyrionError):
    """Raised when Lyrion returns an invalid JSON-RPC payload."""
