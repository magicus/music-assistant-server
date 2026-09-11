"""Shared setup flow helpers for Lyrion providers."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from aiohttp import ClientError, ClientSession, ClientTimeout
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType
from music_assistant_models.errors import SetupFailedError

from music_assistant.models.setup_flow import SetupFlowError
from music_assistant.providers.lyrion.client import build_lms_url

if TYPE_CHECKING:
    import logging

    from music_assistant.models.setup_flow import SetupSession


_DISCOVERY_MESSAGE = b"eIPAD\x00NAME\x00JSON\x00UUID\x00VERS"
_DISCOVERY_TARGET = ("255.255.255.255", 3483)


@dataclass(slots=True)
class _DiscoveredLMSEndpoint:
    """One LMS endpoint returned by UDP discovery."""

    host: str
    port: int
    name: str | None = None
    uuid: str | None = None


@dataclass(slots=True)
class _ConfiguredLMSEndpoint:
    """One configured LMS endpoint from an existing provider instance."""

    host: str
    port: int | None


async def run_lms_setup_flow(
    session: SetupSession,
    current_domain: str,
    host_key: str,
    port_key: str,
    default_port: int,
    logger: logging.Logger,
    log_prefix: str,
) -> None:
    """Run shared setup flow for providers that connect to an LMS endpoint."""
    errors: dict[str, str] | None = None
    setup_data = dict(session.context.setup_data)
    await _prefill_lms_endpoint(
        session,
        setup_data,
        current_domain=current_domain,
        host_key=host_key,
        port_key=port_key,
    )
    entries = _entries(host_key, port_key, default_port)
    while True:
        form_entries = [
            replace(
                entry,
                value=setup_data.get(entry.key, entry.value),
            )
            for entry in entries
        ]
        submitted = await session.form(
            form_entries,
            step_id="user",
            errors=errors,
            last_step=True,
        )
        normalized = _normalize_submitted_values(
            submitted,
            host_key=host_key,
            port_key=port_key,
            default_port=default_port,
        )
        setup_data[host_key] = normalized[host_key]
        setup_data[port_key] = normalized[port_key]

        try:
            await session.finish(setup_data)
            return
        except SetupFlowError as err:
            detail = str(err) or err.__class__.__name__
            has_generic_key = err.translation_key is None or err.translation_key == "setup_failed"
            error_key = detail if has_generic_key else (err.translation_key or detail)
            logger.warning(
                "%s setup failed for %s:%s: %s",
                log_prefix,
                setup_data.get(host_key),
                setup_data.get(port_key),
                detail,
            )
            errors = {"base": error_key}


async def validate_lms_endpoint(
    host: object,
    port: object,
    *,
    http_session: ClientSession,
    translation_owner: str | None = None,
) -> None:
    """Validate a configured LMS endpoint."""
    host_str = str(host or "").strip()
    if host_str.startswith("[") and host_str.endswith("]"):
        host_str = host_str[1:-1]
    if not host_str:
        raise SetupFailedError(
            "host_required",
            translation_key="host_required",
            translation_owner=translation_owner,
        )

    resolved_port = _coerce_port(port)
    if resolved_port is None:
        raise SetupFailedError(
            "invalid_port",
            translation_key="invalid_port",
            translation_owner=translation_owner,
        )

    if not _is_ip_address(host_str):
        try:
            await asyncio.get_running_loop().getaddrinfo(
                host_str,
                None,
                type=socket.SOCK_STREAM,
            )
        except socket.gaierror as err:
            msg = f"host_unresolvable: {host_str}"
            raise SetupFailedError(
                msg,
                translation_key="host_unresolvable",
                translation_owner=translation_owner,
                translation_args=[host_str],
            ) from err

    try:
        conn = await asyncio.wait_for(
            asyncio.open_connection(host_str, resolved_port),
            timeout=5,
        )
        _, writer = conn
        writer.close()
        await writer.wait_closed()
    except (TimeoutError, OSError) as err:
        msg = f"endpoint_unreachable: {host_str}:{resolved_port}"
        raise SetupFailedError(
            msg,
            translation_key="endpoint_unreachable",
            translation_owner=translation_owner,
        ) from err

    payload = {
        "id": 1,
        "method": "slim.request",
        "params": ["", ["serverstatus", 0, 1]],
    }
    url = build_lms_url(host_str, resolved_port, "/jsonrpc.js")
    try:
        async with http_session.post(
            url,
            json=payload,
            timeout=ClientTimeout(total=5),
        ) as response:
            response.raise_for_status()
            body = await response.json()
    except (ClientError, TimeoutError, ValueError) as err:
        msg = f"endpoint_not_lyrion: {host_str}:{resolved_port}"
        raise SetupFailedError(
            msg,
            translation_key="endpoint_not_lyrion",
            translation_owner=translation_owner,
        ) from err

    if not isinstance(body, dict) or not isinstance(body.get("result"), dict):
        msg = f"serverstatus_invalid: {host_str}:{resolved_port}"
        raise SetupFailedError(
            msg,
            translation_key="serverstatus_invalid",
            translation_owner=translation_owner,
        )


def _entries(
    host_key: str,
    port_key: str,
    default_port: int,
) -> tuple[ConfigEntry, ...]:
    """Return the shared LMS endpoint form entries."""
    return (
        ConfigEntry(
            key=host_key,
            type=ConfigEntryType.STRING,
            required=True,
        ),
        ConfigEntry(
            key=port_key,
            type=ConfigEntryType.INTEGER,
            required=True,
            default_value=default_port,
        ),
    )


async def _prefill_lms_endpoint(
    session: SetupSession,
    setup_data: dict[str, object],
    current_domain: str,
    host_key: str,
    port_key: str,
) -> None:
    """Prefill host/port from current or sibling Lyrion provider configs."""
    existing_host = setup_data.get(host_key)
    if isinstance(existing_host, str):
        stripped_host = existing_host.strip()
        if stripped_host:
            setup_data[host_key] = stripped_host
        else:
            setup_data.pop(host_key, None)

    existing_port = _coerce_port(setup_data.get(port_key))
    if existing_port is None:
        setup_data.pop(port_key, None)
    else:
        setup_data[port_key] = existing_port

    instance_id = session.context.instance_id
    if instance_id:
        if not setup_data.get(host_key):
            host = session.mass.config.get_provider_setup_value(
                instance_id,
                host_key,
            )
            if not host:
                host = session.context.values.get(host_key)
            if isinstance(host, str):
                host = host.strip()
            if host:
                setup_data[host_key] = str(host)

        if _coerce_port(setup_data.get(port_key)) is None:
            port = _coerce_port(
                session.mass.config.get_provider_setup_value(
                    instance_id,
                    port_key,
                )
                or session.context.values.get(port_key)
            )
            if port is not None:
                setup_data[port_key] = port

        if session.context.kind != "setup":
            return
        if setup_data.get(host_key):
            return

    if session.context.kind != "setup":
        return

    sibling_domain = "lyrion_player" if current_domain == "lyrion_music" else "lyrion_music"

    current_endpoints = await _configured_lms_endpoints(
        session,
        current_domain,
        host_key,
        port_key,
    )
    sibling_endpoints = await _configured_lms_endpoints(
        session,
        sibling_domain,
        host_key,
        port_key,
    )

    # Refill mode: if the sibling provider has any host this provider lacks,
    # suggest that first. This must be symmetric regardless of start domain.
    current_hosts = {endpoint.host.lower() for endpoint in current_endpoints}
    for endpoint in sibling_endpoints:
        if endpoint.host.lower() in current_hosts:
            continue
        setup_data[host_key] = endpoint.host
        if setup_data.get(port_key) is None and endpoint.port is not None:
            setup_data[port_key] = endpoint.port
        return

    domain_order = [current_domain, sibling_domain]
    configured_hosts = await _configured_lms_hosts(
        session,
        domain_order,
        host_key,
    )
    discovered_endpoints = await _discover_lms_endpoints(timeout=3.0)
    if discovered := _select_discovered_lms_endpoint(
        discovered_endpoints,
        configured_hosts,
    ):
        setup_data[host_key] = discovered.host
        if setup_data.get(port_key) is None:
            setup_data[port_key] = discovered.port
        return

    if configured_hosts:
        return

    for domain in domain_order:
        for config in await session.mass.config.get_provider_configs(
            provider_domain=domain,
        ):
            if config.instance_id == session.context.instance_id:
                continue
            host = session.mass.config.get_provider_setup_value(
                config.instance_id,
                host_key,
            )
            if not host:
                continue
            setup_data[host_key] = str(host)
            if setup_data.get(port_key) is None:
                port = _coerce_port(
                    session.mass.config.get_provider_setup_value(
                        config.instance_id,
                        port_key,
                    )
                )
                if port is not None:
                    setup_data[port_key] = port
            return


def _normalize_submitted_values(
    values: Mapping[str, object],
    host_key: str,
    port_key: str,
    default_port: int,
) -> dict[str, object]:
    """Normalize submitted endpoint values before persisting setup_data."""
    host = values.get(host_key)
    if isinstance(host, str):
        normalized_host = host.strip()
    elif host is None:
        normalized_host = ""
    else:
        normalized_host = str(host).strip()

    port = _coerce_port(values.get(port_key))
    if port is None:
        port = default_port

    return {
        host_key: normalized_host,
        port_key: port,
    }


def _is_ip_address(value: str) -> bool:
    """Return True when the given host string is an IP literal."""
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def _coerce_port(value: object) -> int | None:
    """Convert a raw stored port value to int when possible."""
    if value is None:
        return None
    if not isinstance(value, (int, float, str, bytes, bytearray)):
        return None
    try:
        int_port = int(value)
    except TypeError, ValueError:
        return None
    if int_port < 1 or int_port > 65535:
        return None
    return int_port


def _unpack_discovery_response(
    data: bytes,
    addr: tuple[str, int],
) -> _DiscoveredLMSEndpoint | None:
    """Parse one LMS UDP discovery response into a connectable endpoint."""
    if data[0:1] != b"E":
        return None

    payload = data[1:]
    fields: dict[str, str] = {"host": addr[0]}
    while len(payload) >= 5:
        if len(payload) < 5 + payload[4]:
            return None
        try:
            tag = payload[0:4].decode().lower()
            value = payload[5 : 5 + payload[4]].decode()
        except UnicodeDecodeError:
            return None
        fields[tag] = value
        payload = payload[5 + payload[4] :]

    if fields.get("uuid") == "slimproto":
        return None

    port = _coerce_port(fields.get("json"))
    host = fields.get("host")
    if not host or port is None:
        return None

    return _DiscoveredLMSEndpoint(
        host=host,
        port=port,
        name=fields.get("name"),
        uuid=fields.get("uuid"),
    )


class _LMSDiscoveryProtocol(asyncio.DatagramProtocol):
    """Collect UDP TLV discovery responses from LMS servers."""

    def __init__(self) -> None:
        """Initialize response collection."""
        self.transport: asyncio.DatagramTransport | None = None
        self.discovered: dict[tuple[str, int], _DiscoveredLMSEndpoint] = {}

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        """Store the datagram transport."""
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        """Collect one valid LMS discovery response."""
        if endpoint := _unpack_discovery_response(data, addr):
            self.discovered[(endpoint.host, endpoint.port)] = endpoint


async def _configured_lms_hosts(
    session: SetupSession,
    domains: list[str],
    host_key: str,
) -> set[str]:
    """Return configured LMS hosts plus resolved IPv4 addresses."""
    hosts: set[str] = set()
    for domain in domains:
        for config in await session.mass.config.get_provider_configs(
            provider_domain=domain,
        ):
            if config.instance_id == session.context.instance_id:
                continue
            host = session.mass.config.get_provider_setup_value(
                config.instance_id,
                host_key,
            )
            if not isinstance(host, str):
                continue
            normalized_host = host.strip().lower()
            if not normalized_host:
                continue
            hosts.add(normalized_host)
            resolved_host = await _resolve_discovery_host(normalized_host)
            if resolved_host is not None:
                hosts.add(resolved_host)
    return hosts


async def _resolve_discovery_host(host: str) -> str | None:
    """Resolve one configured host to its primary IPv4 address."""
    if _is_ip_address(host):
        return host
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host,
            None,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror:
        return None

    for family, _, _, _, sockaddr in infos:
        if family == socket.AF_INET:
            return str(sockaddr[0])
    return None


def _select_discovered_lms_endpoint(
    endpoints: list[_DiscoveredLMSEndpoint],
    configured_hosts: set[str],
) -> _DiscoveredLMSEndpoint | None:
    """Return the first discovered LMS endpoint that is not configured."""
    for endpoint in endpoints:
        if endpoint.host.lower() in configured_hosts:
            continue
        return endpoint
    return None


async def _configured_lms_endpoints(
    session: SetupSession,
    domain: str,
    host_key: str,
    port_key: str,
) -> list[_ConfiguredLMSEndpoint]:
    """Return configured LMS endpoints for one provider domain, preserving order."""
    endpoints: list[_ConfiguredLMSEndpoint] = []
    for config in await session.mass.config.get_provider_configs(provider_domain=domain):
        if config.instance_id == session.context.instance_id:
            continue
        host = session.mass.config.get_provider_setup_value(config.instance_id, host_key)
        if not isinstance(host, str):
            continue
        normalized_host = host.strip()
        if not normalized_host:
            continue
        endpoints.append(
            _ConfiguredLMSEndpoint(
                host=normalized_host,
                port=_coerce_port(
                    session.mass.config.get_provider_setup_value(
                        config.instance_id,
                        port_key,
                    )
                ),
            )
        )
    return endpoints


async def _discover_lms_endpoints(
    timeout: float,
) -> list[_DiscoveredLMSEndpoint]:
    """Send one LMS UDP discovery probe and return all usable responses."""
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    protocol = _LMSDiscoveryProtocol()
    transport: asyncio.DatagramTransport | None = None
    try:
        transport, _ = await loop.create_datagram_endpoint(
            lambda: protocol,
            sock=sock,
        )
        transport.sendto(_DISCOVERY_MESSAGE, _DISCOVERY_TARGET)
        await asyncio.sleep(timeout)
    except OSError:
        return []
    finally:
        if transport is not None:
            transport.close()
        else:
            sock.close()

    return list(protocol.discovered.values())
