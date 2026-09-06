"""Shared pytest fixtures for Lyrion endpoint-agnostic tests."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable

import pytest

from tests.providers.lyrion.fake_lms_server import FakeLmsServer
from tests.providers.lyrion.lms_server_harness import (
    DockerLmsServerHarness,
    FakeLmsServerHarness,
    LyrionTestEndpoint,
    LyrionTestLmsServer,
)

pytest_plugins = ("tests.providers.lyrion.live_docker",)


@pytest.fixture
async def lyrion_test_lms_server(
    unused_tcp_port_factory: Callable[[], int],
    pytestconfig: pytest.Config,
    request: pytest.FixtureRequest,
) -> AsyncGenerator[LyrionTestLmsServer]:
    """Return the active LMS harness for fake or Docker-backed Lyrion tests."""
    if pytestconfig.getoption("--live-lyrion-docker"):
        lms_endpoint = request.getfixturevalue("lyrion_live_lms_endpoint")
        endpoint = LyrionTestEndpoint(
            host=lms_endpoint.host,
            port=lms_endpoint.port,
            base_url=lms_endpoint.base_url,
            source="docker",
            fake_server=None,
            slimproto_port=3483,
        )
        yield DockerLmsServerHarness(endpoint)
        return

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
def lyrion_fake_server(
    lyrion_test_lms_server: LyrionTestLmsServer,
) -> FakeLmsServer:
    """Return fake LMS state when the current backend is the in-memory fake server."""
    if lyrion_test_lms_server.fake_server is None:
        pytest.skip("Test requires fake LMS server; real LMS endpoint configured")
    return lyrion_test_lms_server.fake_server
