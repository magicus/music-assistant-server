"""Unit tests for shared Lyrion JSON-RPC client helpers."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from music_assistant.providers.lyrion.client import (
    build_lms_url,
    get_configured_host,
    get_configured_port,
    normalize_lms_text_value,
)


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
