# mypy: disable-error-code="union-attr,attr-defined"
"""Tests for shared and provider-specific Lyrion setup flow helpers."""

from __future__ import annotations

import asyncio
import socket
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from music_assistant_models.errors import SetupFailedError

from music_assistant.providers.lyrion import setup_flow as shared_setup_flow
from music_assistant.providers.lyrion_music import setup_flow
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
    assert call["current_domain"] == "lyrion_music"


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
        current_domain="lyrion_music",
        host_key="lms_host",
        port_key="port",
        default_port=9000,
        logger=logger,
        log_prefix="Lyrion music",
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
    session.context.instance_id = "lyrion_music--x"
    session.context.kind = "reconfigure"
    session.context.values = {"lms_host": "ctx.local", "port": 9002}
    session.mass.config.get_provider_setup_value = Mock(side_effect=[" host.local ", "9001"])

    data: dict[str, object] = {"lms_host": " ", "port": "not-int"}
    await shared_setup_flow._prefill_lms_endpoint(
        session,
        data,
        current_domain="lyrion_music",
        host_key="lms_host",
        port_key="port",
    )

    assert data["lms_host"] == "host.local"
    assert data["port"] == 9001


async def test_prefill_ignores_existing_sibling_when_setting_up() -> None:
    """Setup flow should not suggest an already configured sibling LMS host."""
    session = _session(setup_data={})
    session.context.kind = "setup"
    session.context.instance_id = "lyrion_music--new"

    async def _provider_configs(*, provider_domain: str | None = None) -> list[Any]:
        if provider_domain == "lyrion_music":
            return [_provider_cfg("lyrion_music--old")]
        return []

    session.mass.config.get_provider_configs = AsyncMock(side_effect=_provider_configs)

    def _setup_value(instance_id: str, key: str) -> Any:
        if instance_id == "lyrion_music--old" and key == "lms_host":
            return "old.local"
        if instance_id == "lyrion_music--old" and key == "port":
            return "9003"
        return None

    session.mass.config.get_provider_setup_value = Mock(side_effect=_setup_value)

    with patch.object(
        shared_setup_flow,
        "_discover_lms_endpoints",
        new=AsyncMock(return_value=[]),
    ):
        data: dict[str, object] = {}
        await shared_setup_flow._prefill_lms_endpoint(
            session,
            data,
            current_domain="lyrion_music",
            host_key="lms_host",
            port_key="port",
        )

    assert data == {}


def test_unpack_discovery_response_happy_path() -> None:
    """UDP discovery parser should extract host, JSON port and metadata."""
    data = b"ENAME\x06LyrionJSON\x049000UUID\x06abc123VERS\x039.2"

    result = shared_setup_flow._unpack_discovery_response(
        data,
        ("192.168.1.20", 3483),
    )

    assert result is not None
    assert result.host == "192.168.1.20"
    assert result.port == 9000
    assert result.name == "Lyrion"
    assert result.uuid == "abc123"


def test_unpack_discovery_response_invalid_payloads() -> None:
    """UDP discovery parser should reject invalid and slimproto responses."""
    assert (
        shared_setup_flow._unpack_discovery_response(
            b"Xbogus",
            ("1.2.3.4", 3483),
        )
        is None
    )

    slimproto = b"EJSON\x049000UUID\x09slimproto"
    assert (
        shared_setup_flow._unpack_discovery_response(
            slimproto,
            ("1.2.3.4", 3483),
        )
        is None
    )

    missing_json = b"ENAME\x06LyrionUUID\x06abc123"
    assert (
        shared_setup_flow._unpack_discovery_response(
            missing_json,
            ("1.2.3.4", 3483),
        )
        is None
    )


async def test_prefill_uses_network_discovery() -> None:
    """Setup flow should use the first discovered LMS when none are configured."""
    session = _session(setup_data={})
    session.context.kind = "setup"

    with patch.object(
        shared_setup_flow,
        "_discover_lms_endpoints",
        new=AsyncMock(
            return_value=[
                shared_setup_flow._DiscoveredLMSEndpoint(
                    host="192.168.1.30",
                    port=9000,
                    name="Lyrion",
                    uuid="abc123",
                )
            ]
        ),
    ):
        data: dict[str, object] = {}
        await shared_setup_flow._prefill_lms_endpoint(
            session,
            data,
            current_domain="lyrion_music",
            host_key="lms_host",
            port_key="port",
        )

    assert data == {"lms_host": "192.168.1.30", "port": 9000}


async def test_prefill_skips_already_configured_discovered_hosts() -> None:
    """Setup flow should pick the first discovered LMS that is not configured."""
    session = _session(setup_data={})
    session.context.kind = "setup"

    async def _provider_configs(*, provider_domain: str | None = None) -> list[Any]:
        if provider_domain == "lyrion_music":
            return [_provider_cfg("lyrion_music--old")]
        if provider_domain == "lyrion_player":
            return []
        return []

    session.mass.config.get_provider_configs = AsyncMock(side_effect=_provider_configs)

    def _setup_value(instance_id: str, key: str) -> Any:
        if instance_id == "lyrion_music--old" and key == "lms_host":
            return "192.168.1.20"
        if instance_id == "lyrion_music--old" and key == "port":
            return "9000"
        return None

    session.mass.config.get_provider_setup_value = Mock(side_effect=_setup_value)

    with patch.object(
        shared_setup_flow,
        "_discover_lms_endpoints",
        new=AsyncMock(
            return_value=[
                shared_setup_flow._DiscoveredLMSEndpoint(
                    host="192.168.1.20",
                    port=9000,
                    name="foo",
                    uuid="foo",
                ),
                shared_setup_flow._DiscoveredLMSEndpoint(
                    host="192.168.1.30",
                    port=9000,
                    name="bar",
                    uuid="bar",
                ),
            ]
        ),
    ):
        data: dict[str, object] = {}
        await shared_setup_flow._prefill_lms_endpoint(
            session,
            data,
            current_domain="lyrion_music",
            host_key="lms_host",
            port_key="port",
        )

    assert data == {"lms_host": "192.168.1.30", "port": 9000}


async def test_prefill_ignores_only_already_configured_discovery() -> None:
    """Setup flow should leave fields empty when discovery finds only known hosts."""
    session = _session(setup_data={})
    session.context.kind = "setup"

    async def _provider_configs(*, provider_domain: str | None = None) -> list[Any]:
        if provider_domain == "lyrion_music":
            return [_provider_cfg("lyrion_music--old")]
        if provider_domain == "lyrion_player":
            return []
        return []

    session.mass.config.get_provider_configs = AsyncMock(side_effect=_provider_configs)

    def _setup_value(instance_id: str, key: str) -> Any:
        if instance_id == "lyrion_music--old" and key == "lms_host":
            return "192.168.1.20"
        if instance_id == "lyrion_music--old" and key == "port":
            return "9000"
        return None

    session.mass.config.get_provider_setup_value = Mock(side_effect=_setup_value)

    with patch.object(
        shared_setup_flow,
        "_discover_lms_endpoints",
        new=AsyncMock(
            return_value=[
                shared_setup_flow._DiscoveredLMSEndpoint(
                    host="192.168.1.20",
                    port=9000,
                    name="foo",
                    uuid="foo",
                )
            ]
        ),
    ):
        data: dict[str, object] = {}
        await shared_setup_flow._prefill_lms_endpoint(
            session,
            data,
            current_domain="lyrion_music",
            host_key="lms_host",
            port_key="port",
        )

    assert data == {}


async def test_prefill_syncs_missing_player_with_existing_music_host() -> None:
    """Player setup should prefill from existing music host before new discovery."""
    session = _session(setup_data={})
    session.context.kind = "setup"
    session.mass.config.get_provider_configs = AsyncMock(
        side_effect=[
            [],
            [_provider_cfg("lyrion_music--old")],
            [],
            [_provider_cfg("lyrion_music--old")],
        ]
    )

    def _setup_value(instance_id: str, key: str) -> Any:
        if instance_id == "lyrion_music--old" and key == "lms_host":
            return "old.local"
        if instance_id == "lyrion_music--old" and key == "port":
            return "9003"
        return None

    session.mass.config.get_provider_setup_value = Mock(side_effect=_setup_value)

    with patch.object(
        shared_setup_flow,
        "_discover_lms_endpoints",
        new=AsyncMock(
            return_value=[
                shared_setup_flow._DiscoveredLMSEndpoint(
                    host="192.168.1.40",
                    port=9000,
                    name="new",
                    uuid="new",
                )
            ]
        ),
    ):
        data: dict[str, object] = {}
        await shared_setup_flow._prefill_lms_endpoint(
            session,
            data,
            current_domain="lyrion_player",
            host_key="lms_host",
            port_key="port",
        )

    assert data == {"lms_host": "old.local", "port": 9003}


async def test_prefill_syncs_next_unmatched_sibling_host() -> None:
    """When one side has fewer instances, prefill should pick first unmatched sibling host."""
    session = _session(setup_data={})
    session.context.kind = "setup"

    async def _provider_configs(*, provider_domain: str | None = None) -> list[Any]:
        if provider_domain == "lyrion_player":
            return [_provider_cfg("lyrion_player--1")]
        if provider_domain == "lyrion_music":
            return [
                _provider_cfg("lyrion_music--1"),
                _provider_cfg("lyrion_music--2"),
            ]
        return []

    session.mass.config.get_provider_configs = AsyncMock(side_effect=_provider_configs)

    def _setup_value(instance_id: str, key: str) -> Any:
        if instance_id == "lyrion_player--1" and key == "lms_host":
            return "host-a.local"
        if instance_id == "lyrion_music--1" and key == "lms_host":
            return "host-a.local"
        if instance_id == "lyrion_music--2" and key == "lms_host":
            return "host-b.local"
        if instance_id == "lyrion_music--2" and key == "port":
            return "9005"
        return None

    session.mass.config.get_provider_setup_value = Mock(side_effect=_setup_value)

    with patch.object(
        shared_setup_flow,
        "_discover_lms_endpoints",
        new=AsyncMock(return_value=[]),
    ):
        data: dict[str, object] = {}
        await shared_setup_flow._prefill_lms_endpoint(
            session,
            data,
            current_domain="lyrion_player",
            host_key="lms_host",
            port_key="port",
        )

    assert data == {"lms_host": "host-b.local", "port": 9005}


async def test_prefill_balanced_prefers_new_discovery_host() -> None:
    """When both provider types are balanced, prefill should use new discovery."""
    session = _session(setup_data={})
    session.context.kind = "setup"

    async def _provider_configs(*, provider_domain: str | None = None) -> list[Any]:
        if provider_domain == "lyrion_player":
            return [_provider_cfg("lyrion_player--1")]
        if provider_domain == "lyrion_music":
            return [_provider_cfg("lyrion_music--1")]
        return []

    session.mass.config.get_provider_configs = AsyncMock(side_effect=_provider_configs)

    def _setup_value(instance_id: str, key: str) -> Any:
        if instance_id in {"lyrion_player--1", "lyrion_music--1"} and key == "lms_host":
            return "192.168.1.20"
        return None

    session.mass.config.get_provider_setup_value = Mock(side_effect=_setup_value)

    with patch.object(
        shared_setup_flow,
        "_discover_lms_endpoints",
        new=AsyncMock(
            return_value=[
                shared_setup_flow._DiscoveredLMSEndpoint(
                    host="192.168.1.50",
                    port=9000,
                    name="new",
                    uuid="new",
                )
            ]
        ),
    ):
        data: dict[str, object] = {}
        await shared_setup_flow._prefill_lms_endpoint(
            session,
            data,
            current_domain="lyrion_player",
            host_key="lms_host",
            port_key="port",
        )

    assert data == {"lms_host": "192.168.1.50", "port": 9000}


async def test_prefill_refill_mode_is_symmetric_on_set_difference() -> None:
    """Refill mode should trigger on sibling-only hosts even when counts match."""
    session = _session(setup_data={})
    session.context.kind = "setup"

    async def _provider_configs(*, provider_domain: str | None = None) -> list[Any]:
        if provider_domain == "lyrion_player":
            return [_provider_cfg("lyrion_player--1")]
        if provider_domain == "lyrion_music":
            return [_provider_cfg("lyrion_music--1")]
        return []

    session.mass.config.get_provider_configs = AsyncMock(side_effect=_provider_configs)

    def _setup_value(instance_id: str, key: str) -> Any:
        if instance_id == "lyrion_player--1" and key == "lms_host":
            return "192.168.1.10"
        if instance_id == "lyrion_music--1" and key == "lms_host":
            return "192.168.1.20"
        if instance_id == "lyrion_music--1" and key == "port":
            return "9004"
        return None

    session.mass.config.get_provider_setup_value = Mock(side_effect=_setup_value)

    with patch.object(
        shared_setup_flow,
        "_discover_lms_endpoints",
        new=AsyncMock(
            return_value=[
                shared_setup_flow._DiscoveredLMSEndpoint(
                    host="192.168.1.99",
                    port=9000,
                    name="new",
                    uuid="new",
                )
            ]
        ),
    ):
        data: dict[str, object] = {}
        await shared_setup_flow._prefill_lms_endpoint(
            session,
            data,
            current_domain="lyrion_player",
            host_key="lms_host",
            port_key="port",
        )

    assert data == {"lms_host": "192.168.1.20", "port": 9004}
