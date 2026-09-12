"""Shared pytest fixtures for Lyrion player endpoint-agnostic tests."""

from __future__ import annotations

from collections.abc import Generator

import pytest

pytest_plugins = ("tests.providers.lyrion.fixtures",)

_WHITEBOX_FILE = "tests/providers/lyrion_player/test_harness_player_whitebox.py"


def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    """Route player tests to Docker in live mode, excluding strict whitebox."""
    if not config.getoption("--live-lyrion-docker"):
        return

    for item in items:
        if str(item.path).endswith(_WHITEBOX_FILE):
            item.add_marker(
                pytest.mark.skip(
                    reason=(
                        "Whitebox tests require fake LMS internals; "
                        "skip in --live-lyrion-docker mode."
                    )
                )
            )
            continue
        item.add_marker(pytest.mark.live_lyrion_docker)


@pytest.fixture(scope="session", autouse=True)
def ensure_live_lms_endpoint(
    pytestconfig: pytest.Config,
    request: pytest.FixtureRequest,
) -> Generator[None]:
    """Eagerly initialize live Docker LMS once for the player test package."""
    if pytestconfig.getoption("--live-lyrion-docker"):
        request.getfixturevalue("lyrion_live_lms_endpoint")
    return
