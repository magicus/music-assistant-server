"""Shared fixtures for tests that need an LMS endpoint."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass

import pytest
from aiohttp import web

from .fake_lms_server import FakeLmsServer


@dataclass(slots=True, frozen=True)
class LyrionTestEndpoint:
    """Resolved endpoint details for fake or Docker-managed LMS."""

    host: str
    port: int
    base_url: str
    source: str
    fake_server: FakeLmsServer | None = None


@pytest.fixture
async def lyrion_test_endpoint(
    unused_tcp_port_factory: Callable[[], int],
) -> AsyncGenerator[LyrionTestEndpoint]:
    """Return a fake LMS endpoint."""
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
    """Return fake LMS state."""
    if lyrion_test_endpoint.fake_server is None:
        pytest.skip("Test requires fake LMS server")
    return lyrion_test_endpoint.fake_server
