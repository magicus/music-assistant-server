"""Unit tests for Lyrion live-docker auth helper behavior."""

from __future__ import annotations

from typing import Any, Self

import pytest

from tests.providers.lyrion.live_docker import _write_server_prefs


@pytest.mark.parametrize(
    ("auth_enabled", "expected_header"),
    [
        (False, ""),
        (True, "Basic bHlyaW9uLXVzZXI6bHlyaW9uLXBhc3N3b3Jk"),
    ],
)
def test_live_docker_json_rpc_auth_headers(
    auth_enabled: bool,
    expected_header: str,
) -> None:
    """Docker helper should include Basic Auth only when auth is enabled."""
    captured: dict[str, str] = {}

    class _FakeResponse:
        def __init__(self, payload: dict[str, object]) -> None:
            self._payload = payload

        def read(self) -> bytes:
            return b'{"result": {"ok": true}}'

        def __enter__(self) -> Self:
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

    def _fake_urlopen(req: object, timeout: float) -> _FakeResponse:
        assert timeout == 5.0
        headers = getattr(req, "headers", {})
        captured["Authorization"] = headers.get("Authorization", "")
        return _FakeResponse({"result": {"ok": True}})

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            "tests.providers.lyrion.live_docker.urlopen",
            _fake_urlopen,
        )
        result = __import__(
            "tests.providers.lyrion.live_docker",
            fromlist=["_json_rpc"],
        )._json_rpc(
            "http://127.0.0.1:9000",
            ["serverstatus", 0, 1],
            timeout=5.0,
            username=("lyrion-user" if auth_enabled else None),
            password=("lyrion-password" if auth_enabled else None),
        )

    assert result == {"result": {"ok": True}}
    assert captured["Authorization"] == expected_header


def test_write_server_prefs_supports_auth(tmp_path: Any) -> None:
    """Docker LMS prefs helper should emit auth fields when auth is enabled."""
    _write_server_prefs(
        tmp_path,
        auth_enabled=True,
        username="lyrion-user",
        password_hash="68warDCjSmInKKk078r/Ii4ehe8",
    )

    prefs = (tmp_path / "prefs" / "server.prefs").read_text(encoding="utf-8")
    assert "authorize: '1'" in prefs
    assert "username: lyrion-user" in prefs
    assert "password: 68warDCjSmInKKk078r/Ii4ehe8" in prefs
