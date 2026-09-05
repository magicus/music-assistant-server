"""Fixtures for Lyrion music provider contract tests."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import ClientSession, web

from music_assistant.providers.lyrion_music.constants import SUPPORTED_FEATURES
from music_assistant.providers.lyrion_music.provider import LyrionMusicProvider
from tests.common import use_real_create_task
from tests.providers.lyrion.fake_lms_server import FakeLmsServer
from tests.providers.lyrion.fixtures import LyrionTestEndpoint

if TYPE_CHECKING:
    from tests.providers.lyrion.live_docker import LiveLmsEndpoint

pytest_plugins = ("tests.providers.lyrion.live_docker",)


@pytest.fixture
async def lyrion_test_endpoint(
    unused_tcp_port_factory: Callable[[], int],
    pytestconfig: pytest.Config,
    request: pytest.FixtureRequest,
) -> AsyncGenerator[LyrionTestEndpoint]:
    """Return LMS endpoint for Docker mode or fake mode."""
    if pytestconfig.getoption("--live-lyrion-docker"):
        lms_endpoint: LiveLmsEndpoint = request.getfixturevalue("lyrion_live_lms_endpoint")
        yield LyrionTestEndpoint(
            host=lms_endpoint.host,
            port=lms_endpoint.port,
            base_url=lms_endpoint.base_url,
            source="docker",
            fake_server=None,
        )
        return

    fake_server = FakeLmsServer()
    runner = web.AppRunner(fake_server.app)
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
    lyrion_test_endpoint: LyrionTestEndpoint,
) -> FakeLmsServer:
    """Return fake LMS server state when tests run in fake mode."""
    if lyrion_test_endpoint.fake_server is None:
        pytest.skip("Test requires fake LMS server; real LMS endpoint configured")
    return lyrion_test_endpoint.fake_server
