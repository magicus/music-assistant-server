"""Low-level LMS JSON-RPC helpers shared by all Lyrion providers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol, cast

from music_assistant_models.errors import ProviderUnavailableError

from music_assistant.helpers.throttle_retry import ThrottlerManager
from music_assistant.providers.lyrion.constants import (
    CONF_LMS_HOST,
    CONF_LMS_PORT,
    DEFAULT_LMS_PORT,
    RPC_TIMEOUT,
)
from pylyrion.errors import (
    LyrionProtocolError,
    LyrionRequestError,
    LyrionTimeoutError,
)
from pylyrion.models import LyrionEndpoint
from pylyrion.session import (
    LyrionSession,
)
from pylyrion.session import (
    build_lms_url as _build_lms_url,
)
from pylyrion.session import (
    normalize_lms_text_value as _normalize_lms_text_value,
)

build_lms_url = _build_lms_url
normalize_lms_text_value = _normalize_lms_text_value

_RPC_THROTTLER = ThrottlerManager(rate_limit=1, period=1)


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
) -> Mapping[str, object]:
    """Execute one LMS JSON-RPC request using the shared Lyrion transport."""
    host = get_configured_host(provider)
    if not host:
        raise ProviderUnavailableError("Lyrion host is not configured")

    port = get_configured_port(provider)
    session = LyrionSession(
        http_session=provider.mass.http_session,
        endpoint=LyrionEndpoint(host=host, port=port),
        timeout=timeout,
        request_guard=_RPC_THROTTLER.acquire,
    )
    try:
        return await session.request(player_id, command)
    except (
        LyrionProtocolError,
        LyrionRequestError,
        LyrionTimeoutError,
    ) as err:
        raise ProviderUnavailableError(str(err)) from err
