"""Shared fixtures for tests that need an LMS endpoint."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable

import pytest

from .fake_lms_server import FakeLmsServer
from .lms_server_harness import FakeLmsServerHarness, LyrionTestEndpoint, LyrionTestLmsServer


@pytest.fixture
async def lyrion_test_lms_server(
    unused_tcp_port_factory: Callable[[], int],
) -> AsyncGenerator[LyrionTestLmsServer]:
    """Return default fake LMS harness implementation."""
    harness = FakeLmsServerHarness(unused_tcp_port_factory)
    await harness.start()
    try:
        yield harness
    finally:
        await harness.stop()


@pytest.fixture
async def lyrion_test_endpoint(
    lyrion_test_lms_server: LyrionTestLmsServer,
) -> LyrionTestEndpoint:
    """Return endpoint details from the active LMS test harness."""
    return lyrion_test_lms_server.endpoint


@pytest.fixture
def fake_lms_server(
    lyrion_test_lms_server: LyrionTestLmsServer,
) -> FakeLmsServer:
    """Return fake LMS state."""
    if lyrion_test_lms_server.fake_server is None:
        pytest.skip("Test requires fake LMS server")
    return lyrion_test_lms_server.fake_server
