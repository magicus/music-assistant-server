"""Low-level LMS JSON-RPC helpers shared by all Lyrion providers."""

from __future__ import annotations

from typing import Any, Protocol, cast

from music_assistant.constants import CONF_PORT
from pylyrion.session import build_lms_url as _build_lms_url
from pylyrion.session import normalize_lms_text_value as _normalize_lms_text_value

build_lms_url = _build_lms_url
normalize_lms_text_value = _normalize_lms_text_value

CONF_LMS_HOST = "lms_host"
CONF_LMS_PORT = CONF_PORT
DEFAULT_LMS_PORT = 9000


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
