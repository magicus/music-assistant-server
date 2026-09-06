"""Low-level LMS JSON-RPC helpers shared by all Lyrion providers."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol, cast

from aiohttp import ClientError, ClientTimeout
from music_assistant_models.errors import ProviderUnavailableError

from music_assistant.providers.lyrion.constants import (
    CONF_LMS_HOST,
    CONF_LMS_PORT,
    DEFAULT_LMS_PORT,
    RPC_TIMEOUT,
)


class _ConfigProvider(Protocol):
    """Minimal provider API required for shared LMS request helpers."""

    mass: Any

    def get_setup_value(self, key: str, default: Any = None) -> Any: ...


def get_configured_host(provider: _ConfigProvider) -> str | None:
    """Return configured LMS host for a Lyrion provider."""
    raw_host = provider.get_setup_value(CONF_LMS_HOST)
    if not isinstance(raw_host, str):
        return None
    host = raw_host.strip()
    return host or None


def get_configured_port(
    provider: _ConfigProvider,
    default: int | None = DEFAULT_LMS_PORT,
) -> int | None:
    """Return configured LMS port for a Lyrion provider."""
    raw_port = provider.get_setup_value(CONF_LMS_PORT, default)
    if raw_port is None:
        return None
    try:
        return int(cast("int | str", raw_port))
    except TypeError, ValueError:
        return default


async def rpc_request(
    provider: _ConfigProvider,
    player_id: str,
    command: Sequence[Any],
    *,
    timeout: int = RPC_TIMEOUT,
) -> dict[str, Any]:
    """Execute one LMS JSON-RPC request using the shared Lyrion transport."""
    host = get_configured_host(provider)
    if not host:
        raise ProviderUnavailableError("Lyrion host is not configured")

    port = get_configured_port(provider)
    payload = {
        "id": 1,
        "method": "slim.request",
        "params": [player_id, list(command)],
    }
    url = f"http://{host}:{port}/jsonrpc.js"

    try:
        async with provider.mass.http_session.post(
            url,
            json=payload,
            timeout=ClientTimeout(total=timeout),
        ) as response:
            response.raise_for_status()
            data = cast("dict[str, Any]", await response.json())
    except TimeoutError as err:
        raise ProviderUnavailableError(
            f"Lyrion server at {host}:{port} did not respond in time ({timeout}s). "
            "Verify that Lyrion is running and reachable."
        ) from err
    except (ClientError, ValueError) as err:
        raise ProviderUnavailableError(
            f"Lyrion JSON-RPC request to {host}:{port} failed: {err}"
        ) from err

    if error_payload := cast("dict[str, Any] | None", data.get("error")):
        error_code = error_payload.get("code", "unknown")
        error_message = error_payload.get("message", "unknown JSON-RPC error")
        raise ProviderUnavailableError(
            f"Lyrion JSON-RPC command {command[0] if command else '<unknown>'} failed with "
            f"code {error_code}: {error_message}"
        )

    result = cast("dict[str, Any] | None", data.get("result"))
    if result is None:
        raise ProviderUnavailableError(
            f"Lyrion JSON-RPC response for command {command[0] if command else '<unknown>'} "
            "is missing result payload"
        )
    return result
