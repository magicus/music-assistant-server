"""Unit tests for Bayeux meta-flow helper used by the Lyrion CometD stream."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from pylyrion.cometd.bayeux_client import BayeuxClient
from pylyrion.errors import LyrionRequestError


@pytest.mark.asyncio
async def test_open_session_performs_handshake_and_meta_subscribe() -> None:
    """Session open should handshake, subscribe, and return the negotiated client id."""
    sender = AsyncMock(
        side_effect=[
            [{"successful": True, "clientId": "cid-1"}],
            [{"successful": True}],
        ]
    )
    client = BayeuxClient(sender)

    client_id = await client.open_session(timeout=10)

    assert client_id == "cid-1"
    assert sender.await_count == 2
    first_payload = sender.await_args_list[0].args[0][0]
    second_payload = sender.await_args_list[1].args[0][0]
    assert first_payload["channel"] == "/meta/handshake"
    assert second_payload["channel"] == "/meta/subscribe"
    assert second_payload["subscription"] == "/cid-1/**"


@pytest.mark.asyncio
async def test_handshake_rejects_empty_unsuccessful_or_missing_client_id() -> None:
    """Handshake should fail for empty payloads, failed responses, and missing client ids."""
    for response in (
        [],
        [{"successful": False}],
        [{"successful": True}],
        [{"successful": True, "clientId": ""}],
    ):
        client = BayeuxClient(AsyncMock(return_value=response))
        with pytest.raises(LyrionRequestError):
            await client.handshake(timeout=5)


@pytest.mark.asyncio
async def test_subscribe_meta_and_publish_validate_successful_responses() -> None:
    """Meta subscribe and publish should reject unsuccessful/empty responses."""
    client = BayeuxClient(AsyncMock(return_value=[]))
    with pytest.raises(LyrionRequestError):
        await client.subscribe_meta("cid", "/cid/**", timeout=5)

    client = BayeuxClient(AsyncMock(return_value=[{"successful": False}]))
    with pytest.raises(LyrionRequestError):
        await client.publish("/slim/subscribe", "cid", {"k": "v"}, timeout=5)


@pytest.mark.asyncio
async def test_run_connect_loop_stops_on_failed_connect_and_calls_hooks() -> None:
    """Connect pump should process non-failed messages and stop at reconnect requests."""
    responses = [
        [
            {"channel": "/foo", "successful": True},
            {"channel": "/meta/connect", "successful": True},
        ],
        [{"channel": "/meta/connect", "successful": False}],
    ]
    sender = AsyncMock(side_effect=responses)
    client = BayeuxClient(sender)

    seen: list[dict[str, object]] = []
    pre_connect_hook = AsyncMock()

    await client.run_connect_loop(
        client_id="cid",
        connect_timeout=10,
        should_stop=lambda: False,
        message_handler=lambda message: _collect_message(seen, message),
        pre_connect_hook=pre_connect_hook,
    )

    assert pre_connect_hook.await_count == 2
    assert len(seen) == 2
    assert seen[0]["channel"] == "/foo"
    assert seen[1]["channel"] == "/meta/connect"


@pytest.mark.asyncio
async def test_disconnect_posts_meta_disconnect() -> None:
    """Disconnect should emit a best-effort /meta/disconnect message."""
    sender = AsyncMock(return_value=[{"successful": True}])
    client = BayeuxClient(sender)

    await client.disconnect("cid", timeout=4)

    payload = sender.await_args.args[0][0]
    assert payload["channel"] == "/meta/disconnect"
    assert payload["clientId"] == "cid"


def test_is_failed_connect_only_matches_unsuccessful_meta_connect() -> None:
    """Failed-connect detection should only trigger for /meta/connect unsuccessful replies."""
    assert BayeuxClient.is_failed_connect({"channel": "/meta/connect", "successful": False})
    assert not BayeuxClient.is_failed_connect({"channel": "/meta/connect", "successful": True})
    assert not BayeuxClient.is_failed_connect({"channel": "/foo", "successful": False})


async def _collect_message(seen: list[dict[str, object]], message: dict[str, object]) -> None:
    """Collect one connect-loop message."""
    seen.append(message)
