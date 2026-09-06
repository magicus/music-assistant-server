"""Shared test doubles for Lyrion JSON-RPC based tests."""

from __future__ import annotations

from typing import Any


class FakeResponse:
    """Minimal async response context manager for HTTP/JSON-RPC tests."""

    def __init__(self, body: Any, status: int = 200) -> None:
        self._body = body
        self.status = status

    async def json(self) -> Any:
        return self._body

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise RuntimeError("status error")

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class FakeRpcTransport:
    """Route JSON-RPC payloads to a local handler and capture sent commands."""

    def __init__(self, handler: Any) -> None:
        self._handler = handler
        self.commands: list[list[Any]] = []

    def post(self, _url: str, json: dict[str, Any], timeout: Any) -> FakeResponse:
        del timeout
        player_id = str(json["params"][0])
        command = list(json["params"][1])
        self.commands.append(command)
        result = self._handler(player_id, command)
        if isinstance(result, Exception):
            raise result
        return FakeResponse({"result": result})
