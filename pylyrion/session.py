"""Transport primitives for Lyrion JSON-RPC requests."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Any, cast

from aiohttp import ClientError, ClientTimeout

from pylyrion.errors import (
    LyrionProtocolError,
    LyrionRequestError,
    LyrionTimeoutError,
)
from pylyrion.models import LyrionEndpoint


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


@asynccontextmanager
async def _null_request_guard() -> AbstractAsyncContextManager[None]:
    yield


@dataclass(slots=True)
class LyrionSession:
    """Hold transport configuration for a Lyrion endpoint."""

    http_session: Any
    endpoint: LyrionEndpoint
    timeout: int = 10
    request_guard: Callable[[], AsyncContextManager[None]] | None = None

    async def request(
        self,
        player_id: str,
        command: Sequence[Any],
    ) -> Mapping[str, object]:
        """Execute one LMS JSON-RPC request."""
        payload = {
            "id": 1,
            "method": "slim.request",
            "params": [player_id, list(command)],
        }
        url = build_lms_url(
            self.endpoint.host,
            self.endpoint.port,
            self.endpoint.path,
        )
        guard = self.request_guard or _null_request_guard

        try:
            async with (
                guard(),
                self.http_session.post(
                    url,
                    json=payload,
                    timeout=ClientTimeout(total=self.timeout),
                ) as response,
            ):
                response.raise_for_status()
                data = cast("Mapping[str, object]", await response.json())
        except TimeoutError as err:
            raise LyrionTimeoutError(
                "Lyrion server at "
                f"{self.endpoint.host}:{self.endpoint.port} did not respond "
                "in time "
                f"({self.timeout}s)."
                " Verify that Lyrion is running and reachable."
            ) from err
        except ValueError as err:
            raise LyrionProtocolError(
                "Lyrion JSON-RPC connection request to "
                f"{self.endpoint.host}:{self.endpoint.port} returned invalid "
                f"JSON: {err}"
            ) from err
        except ClientError as err:
            raise LyrionRequestError(
                "Lyrion JSON-RPC connection request to "
                f"{self.endpoint.host}:{self.endpoint.port} failed: {err}"
            ) from err

        command_name = command[0] if command else "<unknown>"
        error_payload = data.get("error")
        if error_payload is not None:
            if not isinstance(error_payload, Mapping):
                raise LyrionProtocolError(
                    f"Lyrion JSON-RPC command {command_name} returned an invalid error object"
                )
            error_code = error_payload.get("code", "unknown")
            error_message = error_payload.get(
                "message",
                "unknown JSON-RPC error",
            )
            raise LyrionRequestError(
                "Lyrion JSON-RPC command "
                f"{command_name} failed with code {error_code}: "
                f"{error_message}"
            )

        result = data.get("result")
        if not isinstance(result, dict):
            raise LyrionProtocolError(
                f"Lyrion JSON-RPC response for command {command_name} must contain a result object"
            )
        return cast("dict[str, object]", result)
