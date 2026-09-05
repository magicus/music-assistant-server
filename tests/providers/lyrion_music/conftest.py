"""Fixtures for Lyrion music provider contract tests."""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator, Callable
from typing import Any
from unittest.mock import AsyncMock, Mock
from urllib.parse import urlparse

import pytest
from aiohttp import ClientSession, web

from music_assistant.providers.lyrion_music.constants import SUPPORTED_FEATURES
from music_assistant.providers.lyrion_music.provider import LyrionMusicProvider
from tests.common import use_real_create_task
from tests.providers.lyrion.fake_lms_server import FakeLmsServer
from tests.providers.lyrion.fixtures import LyrionTestEndpoint


@pytest.fixture
async def lyrion_test_endpoint(
    unused_tcp_port_factory: Callable[[], int],
) -> AsyncGenerator[LyrionTestEndpoint]:
    """Return fake LMS endpoint unless real LMS env vars are configured."""
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
