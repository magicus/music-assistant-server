"""Unit tests for shared Lyrion JSON-RPC client helpers."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import ClientError
from music_assistant_models.errors import ProviderUnavailableError

from music_assistant.providers.lyrion import client as client_module
from music_assistant.providers.lyrion.client import (
    build_lms_url,
    get_configured_host,
    get_configured_port,
    normalize_lms_text_value,
    rpc_request,
)
from tests.providers.lyrion.rpc_test_doubles import FakeResponse


class _NoopAcquire:
    """No-op async context manager used to bypass request throttling in tests."""

    async def __aenter__(self) -> _NoopAcquire:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


@pytest.fixture(autouse=True)
def _disable_rpc_throttler(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace global throttler acquire with a deterministic no-op context manager."""
    monkeypatch.setattr(client_module._RPC_THROTTLER, "acquire", lambda: _NoopAcquire())


def _build_provider(host: Any = "127.0.0.1", port: Any = 9000) -> Any:
    """Create minimal provider-like object expected by shared client helpers."""
    provider = SimpleNamespace()
    provider.mass = SimpleNamespace(http_session=SimpleNamespace(post=MagicMock()))
    provider.get_setup_value = MagicMock(
        side_effect=lambda key, default=None: {
            "lms_host": host,
            "port": port,
        }.get(key, default)
    )
    return provider


def test_simple_config_helpers_and_url_builder() -> None:
    """Host/port/url helper functions should normalize common LMS inputs."""
    provider = _build_provider(host=" 2001:db8::1 ", port="9090")

    assert get_configured_host(provider) == "2001:db8::1"
    assert get_configured_port(provider) == 9090
    assert (
        build_lms_url("2001:db8::1", 9000, "jsonrpc.js") == "http://[2001:db8::1]:9000/jsonrpc.js"
    )
    assert build_lms_url("example.local", None, "/cometd") == "http://example.local/cometd"


def test_normalize_lms_text_value() -> None:
    """Text helper should strip whitespace and collapse empty values."""
    assert normalize_lms_text_value(None) is None
    assert normalize_lms_text_value("  abc  ") == "abc"
    assert normalize_lms_text_value(123) == "123"
    assert normalize_lms_text_value("   ") is None


@pytest.mark.asyncio
async def test_rpc_request_happy_path() -> None:
    """rpc_request should POST JSON-RPC and return result payload on success."""
    provider = _build_provider()
    provider.mass.http_session.post = MagicMock(return_value=FakeResponse({"result": {"ok": True}}))

    result = await rpc_request(provider, "player1", ["status", "-", 1])

    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_rpc_request_missing_host_fails_fast() -> None:
    """rpc_request should fail with ProviderUnavailableError when host is not set."""
    provider = _build_provider(host="")

    with pytest.raises(ProviderUnavailableError, match="host is not configured"):
        await rpc_request(provider, "player1", ["status", "-", 1])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"error": "bad"}, "invalid error object"),
        ({"error": {"code": -32000, "message": "oops"}}, "failed with code"),
        ({"result": "bad"}, "must contain a result object"),
    ],
)
async def test_rpc_request_validates_response_shapes(body: dict[str, object], message: str) -> None:
    """rpc_request should reject malformed JSON-RPC response payloads."""
    provider = _build_provider()
    provider.mass.http_session.post = MagicMock(return_value=FakeResponse(body))

    with pytest.raises(ProviderUnavailableError, match=message):
        await rpc_request(provider, "player1", ["status", "-", 1])


@pytest.mark.asyncio
async def test_rpc_request_wraps_timeout_valueerror_and_client_error() -> None:
    """rpc_request should map transport/parsing errors to ProviderUnavailableError."""
    provider = _build_provider()

    class _TimeoutResponse(FakeResponse):
        async def json(self) -> object:
            raise TimeoutError

    provider.mass.http_session.post = MagicMock(return_value=_TimeoutResponse({}))
    with pytest.raises(ProviderUnavailableError, match="did not respond in time"):
        await rpc_request(provider, "player1", ["status"])

    class _ValueResponse(FakeResponse):
        async def json(self) -> object:
            raise ValueError("bad-json")

    provider.mass.http_session.post = MagicMock(return_value=_ValueResponse({}))
    with pytest.raises(ProviderUnavailableError, match="returned invalid JSON"):
        await rpc_request(provider, "player1", ["status"])

    provider.mass.http_session.post = MagicMock(side_effect=ClientError("down"))
    with pytest.raises(ProviderUnavailableError, match="connection request"):
        await rpc_request(provider, "player1", ["status"])
