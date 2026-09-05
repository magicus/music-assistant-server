"""Fixtures for Lyrion music provider contract tests."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import ClientSession

from music_assistant.providers.lyrion_music.constants import SUPPORTED_FEATURES
from music_assistant.providers.lyrion_music.provider import LyrionMusicProvider
from tests.common import use_real_create_task
from tests.providers.lyrion.fake_lms_server import FakeLmsServer
from tests.providers.lyrion.lms_server_harness import (
    DockerLmsServerHarness,
    FakeLmsServerHarness,
    LyrionTestEndpoint,
    LyrionTestLmsServer,
)

if TYPE_CHECKING:
    from tests.providers.lyrion.live_docker import LiveLmsEndpoint

pytest_plugins = ("tests.providers.lyrion.live_docker",)


@pytest.fixture
async def lyrion_test_lms_server(
    unused_tcp_port_factory: Callable[[], int],
    pytestconfig: pytest.Config,
    request: pytest.FixtureRequest,
) -> AsyncGenerator[LyrionTestLmsServer]:
    """Return Docker or fake LMS harness based on test mode flags."""
    if pytestconfig.getoption("--live-lyrion-docker"):
        lms_endpoint: LiveLmsEndpoint = request.getfixturevalue("lyrion_live_lms_endpoint")
        endpoint = LyrionTestEndpoint(
            host=lms_endpoint.host,
            port=lms_endpoint.port,
            base_url=lms_endpoint.base_url,
            source="docker",
            fake_server=None,
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
    """Return endpoint details for the active LMS harness."""
    return lyrion_test_lms_server.endpoint


@pytest.fixture
async def lyrion_provider(
    lyrion_test_endpoint: LyrionTestEndpoint,
) -> AsyncGenerator[LyrionMusicProvider]:
    """Return an initialized Lyrion music provider bound to test endpoint."""
    setup_data: dict[str, Any] = {
        "lms_host": lyrion_test_endpoint.host,
        "port": lyrion_test_endpoint.port,
    }

    session = ClientSession()
    mass = Mock()
    mass.http_session = session
    mass.cache.get_with_freshness = AsyncMock(return_value=(None, False, False))
    mass.cache.set = AsyncMock(return_value=None)
    mass.config.get = Mock(
        side_effect=lambda key, default=None: (
            setup_data if key == "providers/lyrion_music--test/setup_data" else default
        )
    )
    mass.config.get_raw_provider_config_value = Mock(return_value=None)
    mass.config.decrypt_string = Mock(side_effect=lambda value: value)
    use_real_create_task(mass)

    manifest = Mock()
    manifest.domain = "lyrion_music"
    manifest.name = "Lyrion Music"
    manifest.type = Mock()
    manifest.stage = Mock()

    config = Mock()
    config.name = "Lyrion Contract Test"
    config.instance_id = "lyrion_music--test"
    config.values = {}
    config.get_value = Mock(return_value=None)

    provider = LyrionMusicProvider(mass, manifest, config, SUPPORTED_FEATURES)

    await provider.handle_async_init()
    try:
        yield provider
    finally:
        await session.close()


@pytest.fixture
def lyrion_fake_server(
    lyrion_test_lms_server: LyrionTestLmsServer,
) -> FakeLmsServer:
    """Return fake LMS server state when tests run in fake mode."""
    if lyrion_test_lms_server.fake_server is None:
        pytest.skip("Test requires fake LMS server; real LMS endpoint configured")
    return lyrion_test_lms_server.fake_server
