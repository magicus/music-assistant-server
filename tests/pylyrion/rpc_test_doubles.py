"""Shared test doubles for pylyrion JSON-RPC tests."""

from __future__ import annotations

from typing import Any, Self


class FakeResponse:
    """Minimal async response context manager for HTTP/JSON-RPC tests."""

    def __init__(self, body: Any, status: int = 200) -> None:
        """Store a fake payload and status code returned by the mock HTTP client."""
        self._body = body
        self.status = status

    async def json(self) -> Any:
        """Return the mocked JSON payload."""
        return self._body

    def raise_for_status(self) -> None:
        """Raise when the fake status looks like an HTTP error."""
        if self.status >= 400:
            raise RuntimeError("status error")

    async def __aenter__(self) -> Self:
        """Support async context-manager usage in tests."""
        return self

    async def __aexit__(self, *args: object) -> None:
        """Ignore async context-manager cleanup."""
        return
