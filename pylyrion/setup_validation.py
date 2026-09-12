"""Endpoint validation helpers for Lyrion servers."""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import socket
from typing import Any

from aiohttp import ClientError, ClientTimeout

from pylyrion.errors import LyrionError
from pylyrion.session import build_lms_url, normalize_lms_text_value


class LyrionEndpointValidationError(LyrionError):
    """Raised when a configured Lyrion endpoint fails validation."""

    def __init__(
        self,
        error_key: str,
        details: str | None = None,
        translation_args: list[str] | None = None,
    ) -> None:
        """Store structured validation failure details for setup flows."""
        self.error_key = error_key
        self.details = details
        self.translation_args = translation_args
        message = error_key if details is None else f"{error_key}: {details}"
        super().__init__(message)


async def validate_lms_endpoint(
    host: object,
    port: object,
    *,
    http_session: Any,
    username: object | None = None,
    password: object | None = None,
) -> None:
    """Validate that endpoint is reachable and serves Lyrion JSON-RPC."""
    host_str = str(host or "").strip()
    if host_str.startswith("[") and host_str.endswith("]"):
        host_str = host_str[1:-1]
    if not host_str:
        raise LyrionEndpointValidationError("host_required")

    resolved_port = _coerce_port(port)
    if resolved_port is None:
        raise LyrionEndpointValidationError("invalid_port")

    if not _is_ip_address(host_str):
        try:
            await asyncio.get_running_loop().getaddrinfo(
                host_str,
                None,
                type=socket.SOCK_STREAM,
            )
        except socket.gaierror as err:
            raise LyrionEndpointValidationError(
                "host_unresolvable",
                details=host_str,
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
        raise LyrionEndpointValidationError(
            "endpoint_unreachable",
            details=f"{host_str}:{resolved_port}",
        ) from err

    payload = {
        "id": 1,
        "method": "slim.request",
        "params": ["", ["serverstatus", 0, 1]],
    }
    url = build_lms_url(host_str, resolved_port, "/jsonrpc.js")
    headers: dict[str, str] | None = None
    normalized_username = normalize_lms_text_value(username)
    normalized_password = normalize_lms_text_value(password)
    if normalized_username is not None or normalized_password is not None:
        token = base64.b64encode(
            f"{normalized_username or ''}:{normalized_password or ''}".encode()
        ).decode("ascii")
        headers = {"Authorization": f"Basic {token}"}
    try:
        async with http_session.post(
            url,
            json=payload,
            headers=headers,
            timeout=ClientTimeout(total=5),
        ) as response:
            response.raise_for_status()
            body = await response.json()
    except (ClientError, TimeoutError, ValueError) as err:
        raise LyrionEndpointValidationError(
            "endpoint_not_lyrion",
            details=f"{host_str}:{resolved_port}",
        ) from err

    if not isinstance(body, dict) or not isinstance(body.get("result"), dict):
        raise LyrionEndpointValidationError(
            "serverstatus_invalid",
            details=f"{host_str}:{resolved_port}",
        )


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
