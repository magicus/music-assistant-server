# mypy: disable-error-code="union-attr,attr-defined"
"""Tests for shared and provider-specific Lyrion player setup flow helpers."""

from __future__ import annotations

import asyncio
import socket
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from music_assistant_models.errors import SetupFailedError

from music_assistant.providers.lyrion import setup_flow as shared_setup_flow
from music_assistant.providers.lyrion_player import setup_flow
from tests.providers.lyrion.rpc_test_doubles import FakeResponse


class _FakeWriter:
    async def wait_closed(self) -> None:
        return None

    def close(self) -> None:
        return None


def _session(*, setup_data: dict[str, Any] | None = None) -> Any:
    """Create a minimal setup session mock."""
    session = Mock()
    session.context = SimpleNamespace(
        setup_data=setup_data or {},
        instance_id=None,
        kind="setup",
        values={},
    )
    session.form = AsyncMock()
    session.finish = AsyncMock()
    session.mass = Mock()
    session.mass.config.get_provider_configs = AsyncMock(return_value=[])
    session.mass.config.get_provider_setup_value = Mock(return_value=None)
    return session


def _provider_cfg(instance_id: str) -> Any:
    return SimpleNamespace(instance_id=instance_id)


async def test_run_setup_delegates_to_shared_flow() -> None:
    """Provider setup entrypoint should call shared LMS setup helper."""
    session = _session()
    with patch.object(setup_flow, "run_lms_setup_flow", new=AsyncMock()) as mocked:
        await setup_flow.run_setup(session)

    mocked.assert_awaited_once()
    call = mocked.await_args.kwargs
    assert call["current_domain"] == "lyrion_player"


async def test_run_lms_setup_flow_retries_on_finish_error() -> None:
    """Shared setup flow should re-render form with mapped base error."""
    session = _session()
    session.form.side_effect = [
        {"lms_host": " 127.0.0.1 ", "port": 9000},
        {"lms_host": "127.0.0.1", "port": 9001},
    ]
    session.finish.side_effect = [
        shared_setup_flow.SetupFlowError("invalid", translation_key="bad_host"),
        None,
    ]

    logger = Mock()
    await shared_setup_flow.run_lms_setup_flow(
        session,
        current_domain="lyrion_player",
        host_key="lms_host",
        port_key="port",
        default_port=9000,
        logger=logger,
        log_prefix="Lyrion player",
    )

    assert session.form.await_count == 2
    assert session.form.await_args_list[1].kwargs["errors"] == {"base": "bad_host"}
    assert session.finish.await_args_list[-1].args[0] == {
        "lms_host": "127.0.0.1",
        "port": 9001,
    }


async def test_validate_lms_endpoint_happy_path() -> None:
    """Endpoint validator should succeed for reachable endpoint with LMS result."""
    http_session = Mock()
    http_session.post = Mock(return_value=FakeResponse({"result": {"count": 1}}))

    with (
        patch.object(
            asyncio.get_running_loop(),
            "getaddrinfo",
            new=AsyncMock(return_value=[object()]),
        ),
        patch(
            "music_assistant.providers.lyrion.setup_flow.asyncio.open_connection",
            new=AsyncMock(return_value=(object(), _FakeWriter())),
        ),
    ):
        await shared_setup_flow.validate_lms_endpoint(
            host="localhost",
            port=9000,
            http_session=http_session,
        )


async def test_validate_lms_endpoint_accepts_bracketed_ipv6() -> None:
    """Endpoint validator should normalize URL-style IPv6 host brackets."""
    http_session = Mock()
    http_session.post = Mock(return_value=FakeResponse({"result": {"count": 1}}))

    with (
        patch.object(
            asyncio.get_running_loop(),
            "getaddrinfo",
            new=AsyncMock(return_value=[object()]),
        ),
        patch(
            "music_assistant.providers.lyrion.setup_flow.asyncio.open_connection",
            new=AsyncMock(return_value=(object(), _FakeWriter())),
        ) as open_connection_mock,
    ):
        await shared_setup_flow.validate_lms_endpoint(
            host="[::1]",
            port=9000,
            http_session=http_session,
        )
    open_connection_mock.assert_awaited_once_with("::1", 9000)


async def test_validate_lms_endpoint_errors() -> None:
    """Endpoint validator should raise typed setup errors for bad input/state."""
    http_session = Mock()
    http_session.post = Mock(return_value=FakeResponse({"result": {"count": 1}}))

    with pytest.raises(SetupFailedError, match="host_required"):
        await shared_setup_flow.validate_lms_endpoint(
            host=" ",
            port=9000,
            http_session=http_session,
        )

    with pytest.raises(SetupFailedError, match="invalid_port"):
        await shared_setup_flow.validate_lms_endpoint(
            host="127.0.0.1",
            port="not-a-port",
            http_session=http_session,
        )

    with (
        patch.object(
            asyncio.get_running_loop(),
            "getaddrinfo",
            new=AsyncMock(side_effect=socket.gaierror("dns-fail")),
        ),
        pytest.raises(SetupFailedError, match="host_unresolvable"),
    ):
        await shared_setup_flow.validate_lms_endpoint(
            host="totally-invalid-hostname",
            port=9000,
            http_session=http_session,
        )


async def test_validate_lms_endpoint_unreachable_and_not_lyrion() -> None:
    """Validator should map network and payload problems to setup errors."""
    http_session = Mock()
    http_session.post = Mock(return_value=FakeResponse({"result": None}))

    with (
        patch.object(
            asyncio.get_running_loop(),
            "getaddrinfo",
            new=AsyncMock(return_value=[object()]),
        ),
        patch(
            "music_assistant.providers.lyrion.setup_flow.asyncio.open_connection",
            new=AsyncMock(side_effect=OSError("no route")),
        ),
        pytest.raises(SetupFailedError, match="endpoint_unreachable"),
    ):
        await shared_setup_flow.validate_lms_endpoint(
            host="localhost",
            port=9000,
            http_session=http_session,
        )

    with (
        patch.object(
            asyncio.get_running_loop(),
            "getaddrinfo",
            new=AsyncMock(return_value=[object()]),
        ),
        patch(
            "music_assistant.providers.lyrion.setup_flow.asyncio.open_connection",
            new=AsyncMock(return_value=(object(), _FakeWriter())),
        ),
        pytest.raises(SetupFailedError, match="serverstatus_invalid"),
    ):
        await shared_setup_flow.validate_lms_endpoint(
            host="localhost",
            port=9000,
            http_session=http_session,
        )

    bad_json_session = Mock()
    bad_json_session.post = Mock(return_value=FakeResponse(ValueError("bad json")))
    bad_json_session.post.return_value.json = AsyncMock(side_effect=ValueError("bad json"))
    with (
        patch.object(
            asyncio.get_running_loop(),
            "getaddrinfo",
            new=AsyncMock(return_value=[object()]),
        ),
        patch(
            "music_assistant.providers.lyrion.setup_flow.asyncio.open_connection",
            new=AsyncMock(return_value=(object(), _FakeWriter())),
        ),
        pytest.raises(SetupFailedError, match="endpoint_not_lyrion"),
    ):
        await shared_setup_flow.validate_lms_endpoint(
            host="localhost",
            port=9000,
            http_session=bad_json_session,
        )


def test_normalize_and_port_helpers() -> None:
    """Helper functions should sanitize host and parse/coerce ports safely."""
    normalized = shared_setup_flow._normalize_submitted_values(
        {"h": "  host.local  ", "p": "9010"},
        host_key="h",
        port_key="p",
        default_port=9000,
    )
    assert normalized == {"h": "host.local", "p": 9010}

    assert shared_setup_flow._coerce_port(None) is None
    assert shared_setup_flow._coerce_port("abc") is None
    assert shared_setup_flow._coerce_port(0) is None
    assert shared_setup_flow._coerce_port(65536) is None
    assert shared_setup_flow._coerce_port("9000") == 9000

    assert shared_setup_flow._is_ip_address("127.0.0.1") is True
    assert shared_setup_flow._is_ip_address("localhost") is False


async def test_prefill_uses_instance_values_when_available() -> None:
    """Prefill should prefer existing instance setup values and normalize host."""
    session = _session(setup_data={"lms_host": " ", "port": "9000"})
    session.context.instance_id = "lyrion_player--x"
    session.context.kind = "reconfigure"
    session.context.values = {"lms_host": "ctx.local", "port": 9002}
    session.mass.config.get_provider_setup_value = Mock(side_effect=[" host.local ", "9001"])

    data: dict[str, object] = {"lms_host": " ", "port": "not-int"}
    await shared_setup_flow._prefill_lms_endpoint(
        session,
        data,
        current_domain="lyrion_player",
        host_key="lms_host",
        port_key="port",
    )

    assert data["lms_host"] == "host.local"
    assert data["port"] == 9001


async def test_prefill_uses_sibling_provider_when_setting_up() -> None:
    """Setup flow should borrow host/port from sibling Lyrion provider instances."""
    session = _session(setup_data={})
    session.context.kind = "setup"
    session.context.instance_id = "lyrion_player--new"
    session.mass.config.get_provider_configs = AsyncMock(
        side_effect=[[_provider_cfg("lyrion_player--old")], []]
    )

    def _setup_value(instance_id: str, key: str) -> Any:
        if instance_id == "lyrion_player--old" and key == "lms_host":
            return "old.local"
        if instance_id == "lyrion_player--old" and key == "port":
            return "9003"
        return None

    session.mass.config.get_provider_setup_value = Mock(side_effect=_setup_value)

    data: dict[str, object] = {}
    await shared_setup_flow._prefill_lms_endpoint(
        session,
        data,
        current_domain="lyrion_player",
        host_key="lms_host",
        port_key="port",
    )

    assert data == {"lms_host": "old.local", "port": 9003}
