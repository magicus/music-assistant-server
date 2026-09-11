"""Transport helpers for CometD status-stream requests."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from aiohttp import ClientError, ClientTimeout

from pylyrion.errors import LyrionRequestError
from pylyrion.session import LyrionSession, build_lms_url

PostMessagesCallback = Callable[[list[dict[str, object]], int], Awaitable[list[dict[str, object]]]]


def build_cometd_post_messages_callback(
    get_session: Callable[[], LyrionSession],
    unavailable_error_factory: Callable[[Exception], Exception] | None = None,
) -> PostMessagesCallback:
    """Build a CometD POST callback bound to a pylyrion session factory."""

    async def _post_messages(
        messages: list[dict[str, object]],
        timeout: int,
    ) -> list[dict[str, object]]:
        session = get_session()
        endpoint = session.endpoint
        url = build_lms_url(endpoint.host, endpoint.port, "/cometd")

        try:
            async with session.http_session.post(
                url,
                json=messages,
                timeout=ClientTimeout(total=timeout),
            ) as response:
                response.raise_for_status()
                payload = await response.json()
        except (ClientError, TimeoutError, ValueError) as err:
            if unavailable_error_factory is not None:
                raise unavailable_error_factory(err) from err
            raise LyrionRequestError(
                f"Lyrion CometD request to {endpoint.host}:{endpoint.port} failed: {err}"
            ) from err

        if isinstance(payload, dict):
            return [payload]
        if not isinstance(payload, list):
            if unavailable_error_factory is not None:
                raise unavailable_error_factory(
                    ValueError("Status stream response must be a JSON object or list")
                )
            raise LyrionRequestError("Status stream response must be a JSON object or list")

        return [message for message in payload if isinstance(message, dict)]

    return _post_messages
