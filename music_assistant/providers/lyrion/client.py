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
)
from pylyrion.cometd.constants import RPC_TIMEOUT
from pylyrion.session import build_lms_url, normalize_lms_text_value

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
) -> dict[str, Any]:
    """Send one LMS JSON-RPC request via the shared MA provider transport."""
    host = get_configured_host(provider)
    port = get_configured_port(provider)
    if not host or port is None:
        raise ProviderUnavailableError("Lyrion server is not configured")

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
                timeout=ClientTimeout(total=RPC_TIMEOUT),
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
            "Lyrion server did not respond in time while processing the request"
        ) from err
    except ClientError as err:
        raise ProviderUnavailableError(f"Lyrion connection failed: {err}") from err
    except ValueError as err:
        raise ProviderUnavailableError("Lyrion JSON-RPC connection returned invalid JSON") from err

    error_payload = data.get("error")
    if error_payload is not None:
        if not isinstance(error_payload, dict):
            raise ProviderUnavailableError(
                f"Lyrion JSON-RPC command {command[0] if command else '<unknown>'} returned an invalid error object"
            )
        error_code = error_payload.get("code", "unknown")
        error_message = error_payload.get("message", "unknown JSON-RPC error")
        raise ProviderUnavailableError(
            "Lyrion JSON-RPC command "
            f"{command[0] if command else '<unknown>'} failed with code {error_code}: {error_message}"
        )

    result = data.get("result")
    if not isinstance(result, dict):
        raise ProviderUnavailableError(
            "Lyrion JSON-RPC response for command "
            f"{command[0] if command else '<unknown>'} must contain a result object"
        )
    return cast("dict[str, Any]", result)
