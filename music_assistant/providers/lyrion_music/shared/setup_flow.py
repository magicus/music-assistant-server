"""Shared setup flow helpers for Lyrion providers."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Mapping
from dataclasses import replace
from typing import TYPE_CHECKING

from aiohttp import ClientError, ClientTimeout
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType
from music_assistant_models.errors import SetupFailedError

from music_assistant.models.setup_flow import SetupFlowError

if TYPE_CHECKING:
    import logging

    from music_assistant.models.setup_flow import SetupSession


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
    http_session: object,
    translation_owner: str | None = None,
) -> None:
    """Validate a configured LMS endpoint."""
    host_str = str(host or "").strip()
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
    url = f"http://{host_str}:{resolved_port}/jsonrpc.js"
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

    if not isinstance(body, dict) or body.get("result") is None:
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

    domain_order = [
        current_domain,
        *(x for x in ("lyrion_music", "lyrion_player") if x != current_domain),
    ]
    for domain in domain_order:
        for config in await session.mass.config.get_provider_configs(provider_domain=domain):
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
