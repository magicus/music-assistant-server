"""Low-level LMS JSON-RPC helpers shared by all Lyrion providers."""

from __future__ import annotations

from base64 import b64encode
from collections.abc import Mapping, Sequence
from typing import Any, Protocol, cast

from aiohttp import ClientError, ClientTimeout
from music_assistant_models.errors import ProviderUnavailableError

from music_assistant.helpers.throttle_retry import ThrottlerManager
from music_assistant.providers.lyrion.constants import (
    CONF_LMS_HOST,
    CONF_LMS_PASSWORD,
    CONF_LMS_PORT,
    CONF_LMS_USERNAME,
    DEFAULT_LMS_PORT,
    RPC_TIMEOUT,
)

_RPC_THROTTLER = ThrottlerManager(rate_limit=10, period=1)


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


def build_lms_url(host: str, port: int | None, path: str) -> str:
    """Build an HTTP URL for an LMS endpoint with IPv6-safe host formatting."""
    normalized_host = host
    if ":" in host and not host.startswith("[") and not host.endswith("]"):
        normalized_host = f"[{host}]"
    normalized_path = path if path.startswith("/") else f"/{path}"
    if port is None:
        return f"http://{normalized_host}{normalized_path}"
    return f"http://{normalized_host}:{port}{normalized_path}"


def normalize_lms_text_value(value: object) -> str | None:
    """Normalize a raw LMS scalar into a stripped text value."""
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def get_configured_basic_auth(provider: _ConfigProvider) -> dict[str, str] | None:
    """Return optional HTTP Basic Auth headers for LMS HTTP requests."""
    username = normalize_lms_text_value(provider.get_setup_value(CONF_LMS_USERNAME))
    password = normalize_lms_text_value(provider.get_setup_value(CONF_LMS_PASSWORD))
    if username is None and password is None:
        return None
    token = b64encode(f"{username or ''}:{password or ''}".encode()).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def acquire_lms_request_slot() -> object:
    """Return shared request limiter context for LMS HTTP calls."""
    return _RPC_THROTTLER.acquire()


async def rpc_request(
    provider: _ConfigProvider,
    player_id: str,
    command: Sequence[Any],
    *,
    timeout: int = RPC_TIMEOUT,
) -> Mapping[str, object]:
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
    url = build_lms_url(host, port, "/jsonrpc.js")

    headers = get_configured_basic_auth(provider)
    try:
        async with (
            _RPC_THROTTLER.acquire(),
            provider.mass.http_session.post(
                url,
                json=payload,
                headers=headers,
                timeout=ClientTimeout(total=timeout),
            ) as response,
        ):
            response.raise_for_status()
            data = await response.json()
            if not isinstance(data, Mapping):
                raise ProviderUnavailableError(
                    f"Lyrion JSON-RPC connection request to {host}:{port} "
                    "returned a non-object JSON payload"
                )
    except TimeoutError as err:
        raise ProviderUnavailableError(
            f"Lyrion server at {host}:{port} did not respond in time "
            f"({timeout}s). Verify that Lyrion is running and reachable."
        ) from err
    except ValueError as err:
        raise ProviderUnavailableError(
            f"Lyrion JSON-RPC connection request to {host}:{port} returned invalid JSON: {err}"
        ) from err
    except ClientError as err:
        raise ProviderUnavailableError(
            f"Lyrion JSON-RPC connection request to {host}:{port} failed: {err}"
        ) from err

    command_name = command[0] if command else "<unknown>"
    error_payload = data.get("error")
    if error_payload is not None:
        if not isinstance(error_payload, Mapping):
            raise ProviderUnavailableError(
                f"Lyrion JSON-RPC command {command_name} returned an invalid error object"
            )
        error_code = error_payload.get("code", "unknown")
        error_message = error_payload.get("message", "unknown JSON-RPC error")
        raise ProviderUnavailableError(
            f"Lyrion JSON-RPC command {command_name} failed with code {error_code}: {error_message}"
        )

    result = data.get("result")
    if not isinstance(result, dict):
        raise ProviderUnavailableError(
            f"Lyrion JSON-RPC response for command {command_name} must contain a result object"
        )
    return cast("dict[str, object]", result)
