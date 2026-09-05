"""Shared fixtures for tests that need an LMS endpoint."""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from urllib.parse import urlparse

import pytest
from aiohttp import web

from .fake_lms_server import FakeLmsServer


@dataclass(slots=True, frozen=True)
class LyrionTestEndpoint:
    """Resolved endpoint details for either fake or real LMS."""

    host: str
    port: int
    base_url: str
    source: str
    fake_server: FakeLmsServer | None = None


def using_real_lms_from_env() -> bool:
    """Return True when tests use a real LMS endpoint via environment."""
    return bool(os.getenv("LYRION_TEST_LMS_URL") or os.getenv("LYRION_TEST_LMS_HOST"))


@pytest.fixture
async def lyrion_test_endpoint(
    unused_tcp_port_factory: Callable[[], int],
) -> AsyncGenerator[LyrionTestEndpoint]:
    """Return an LMS endpoint, fake by default and real when env is set."""
    if raw_url := os.getenv("LYRION_TEST_LMS_URL"):
        parsed = urlparse(raw_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or not parsed.port:
            msg = "LYRION_TEST_LMS_URL must include scheme, host and port"
            raise ValueError(msg)
        yield LyrionTestEndpoint(
            host=parsed.hostname,
            port=parsed.port,
            base_url=f"{parsed.scheme}://{parsed.hostname}:{parsed.port}",
            source="real_url",
            fake_server=None,
        )
        return

    if raw_host := os.getenv("LYRION_TEST_LMS_HOST"):
        port = int(os.getenv("LYRION_TEST_LMS_PORT", "9000"))
        yield LyrionTestEndpoint(
            host=raw_host,
            port=port,
            base_url=f"http://{raw_host}:{port}",
            source="real_host",
            fake_server=None,
        )
        return

    fake_server = FakeLmsServer()
    app = fake_server.app
    runner = web.AppRunner(app)
    await runner.setup()
    port = unused_tcp_port_factory()
    site = web.TCPSite(runner, host="127.0.0.1", port=port)
    await site.start()

    endpoint = LyrionTestEndpoint(
        host="127.0.0.1",
        port=port,
        base_url=f"http://127.0.0.1:{port}",
        source="fake",
        fake_server=fake_server,
    )
    try:
        yield endpoint
    finally:
        await runner.cleanup()


@pytest.fixture
def fake_lms_server(
    lyrion_test_endpoint: LyrionTestEndpoint,
) -> FakeLmsServer:
    """Return fake LMS state when tests are not using a real LMS."""
    if lyrion_test_endpoint.fake_server is None:
        pytest.skip("Test requires fake LMS server; real LMS endpoint configured")
    return lyrion_test_endpoint.fake_server
