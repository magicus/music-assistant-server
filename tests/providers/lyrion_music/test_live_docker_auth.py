"""Docker-only Lyrion auth integration tests."""

from __future__ import annotations

from typing import Any

import pytest

pytestmark = [
    pytest.mark.live_lyrion_docker,
]


@pytest.mark.live_lyrion_docker
@pytest.mark.parametrize("auth_enabled", [False, True])
def test_live_docker_serverstatus_auth_modes(
    lyrion_live_lms_endpoint: Any,
    auth_enabled: bool,
) -> None:
    """Live Docker LMS should accept serverstatus with and without auth."""
    live_docker = __import__(
        "tests.providers.lyrion.live_docker",
        fromlist=[
            "_bring_down_lms",
            "_write_server_prefs",
            "_bring_up_lms",
            "_json_rpc",
            "LMS_CONFIG_DIR",
            "LMS_MUSIC_DIR",
        ],
    )
    default_auth_enabled = bool(
        lyrion_live_lms_endpoint.username and lyrion_live_lms_endpoint.password
    )
    username, password = ("lyrion-user", "lyrion-password") if auth_enabled else (None, None)

    if auth_enabled == default_auth_enabled:
        result = live_docker._json_rpc(
            lyrion_live_lms_endpoint.base_url,
            ["serverstatus", 0, 1],
            username=username,
            password=password,
        )
        assert isinstance(result.get("result"), dict)
        return

    try:
        live_docker._bring_down_lms(ignore_errors=True)
        live_docker._write_server_prefs(
            live_docker.LMS_CONFIG_DIR,
            auth_enabled=auth_enabled,
            username="lyrion-user",
            password_hash="68warDCjSmInKKk078r/Ii4ehe8",
        )
        live_docker._bring_up_lms(
            lyrion_live_lms_endpoint.base_url,
            auth_enabled=auth_enabled,
            username="lyrion-user",
            password="lyrion-password",
        )
        result = live_docker._json_rpc(
            lyrion_live_lms_endpoint.base_url,
            ["serverstatus", 0, 1],
            username=username,
            password=password,
        )
        assert isinstance(result.get("result"), dict)
    finally:
        live_docker._bring_down_lms(ignore_errors=True)
        live_docker._write_server_prefs(
            live_docker.LMS_CONFIG_DIR,
            auth_enabled=default_auth_enabled,
            username="lyrion-user",
            password_hash="68warDCjSmInKKk078r/Ii4ehe8",
        )
        live_docker._bring_up_lms(
            lyrion_live_lms_endpoint.base_url,
            auth_enabled=default_auth_enabled,
            username="lyrion-user",
            password="lyrion-password",
        )
