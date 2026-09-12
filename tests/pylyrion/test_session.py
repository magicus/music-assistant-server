"""Unit tests for pylyrion transport helpers."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import ClientError

from pylyrion.errors import LyrionProtocolError, LyrionRequestError, LyrionTimeoutError
from pylyrion.models import LyrionEndpoint
from pylyrion.session import LyrionSession, build_lms_url, normalize_lms_text_value
from tests.pylyrion.rpc_test_doubles import FakeResponse


def _build_session(host: Any = "127.0.0.1", port: Any = 9000) -> LyrionSession:
    """Create a standalone pylyrion session for transport tests."""
    http_session = SimpleNamespace(post=MagicMock())
    return LyrionSession(
        http_session=http_session,
        endpoint=LyrionEndpoint(host=str(host), port=port),
    )


def test_simple_url_and_text_helpers() -> None:
    """Helper functions should normalize common LMS inputs."""
    assert build_lms_url("2001:db8::1", 9000, "jsonrpc.js") == (
        "http://[2001:db8::1]:9000/jsonrpc.js"
    )
    assert build_lms_url("example.local", None, "/cometd") == ("http://example.local/cometd")
    assert normalize_lms_text_value(None) is None
    assert normalize_lms_text_value("  abc  ") == "abc"
    assert normalize_lms_text_value(123) == "123"
    assert normalize_lms_text_value("   ") is None


@pytest.mark.asyncio
async def test_session_request_happy_path() -> None:
    """Session request should POST JSON-RPC and return the result payload."""
    session = _build_session()
    session.http_session.post = MagicMock(return_value=FakeResponse({"result": {"ok": True}}))

    result = await session.request("player1", ["status", "-", 1])

    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_session_request_wraps_transport_and_protocol_errors() -> None:
    """Session request should map low-level failures to pylyrion errors."""
    session = _build_session()

    class _TimeoutResponse(FakeResponse):
        async def json(self) -> object:
            raise TimeoutError

    session.http_session.post = MagicMock(return_value=_TimeoutResponse({}))
    with pytest.raises(LyrionTimeoutError):
        await session.request("player1", ["status"])

    class _ValueResponse(FakeResponse):
        async def json(self) -> object:
            raise ValueError("bad-json")

    session.http_session.post = MagicMock(return_value=_ValueResponse({}))
    with pytest.raises(LyrionProtocolError):
        await session.request("player1", ["status"])

    session.http_session.post = MagicMock(side_effect=ClientError("down"))
    with pytest.raises(LyrionRequestError):
        await session.request("player1", ["status"])
