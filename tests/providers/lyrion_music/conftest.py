"""Fixtures for Lyrion music provider contract tests."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import ClientSession

from music_assistant.providers.lyrion_music.constants import SUPPORTED_FEATURES
from music_assistant.providers.lyrion_music.provider import LyrionMusicProvider
from tests.common import use_real_create_task
from tests.providers.lyrion.lms_server_harness import LyrionTestEndpoint

pytest_plugins = ("tests.providers.lyrion.fixtures",)


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
