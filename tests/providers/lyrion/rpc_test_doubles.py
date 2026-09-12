"""Shared test doubles for Lyrion JSON-RPC based tests."""

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


class FakeRpcTransport:
    """Route JSON-RPC payloads to a local handler and capture sent commands."""

    def __init__(self, handler: Any) -> None:
        """Initialize the transport with a local JSON-RPC handler."""
        self._handler = handler
        self.commands: list[list[Any]] = []

    def post(
        self,
        _url: str,
        json: dict[str, Any],
        timeout: Any,
        *,
        auth: Any = None,
        **kwargs: Any,
    ) -> FakeResponse:
        """Dispatch a fake JSON-RPC request to the local handler."""
        del timeout, auth, kwargs
        player_id = str(json["params"][0])
        command = list(json["params"][1])
        self.commands.append(command)
        result = self._handler(player_id, command)
        if isinstance(result, Exception):
            raise result
        return FakeResponse({"result": result})
