"""Minimal Bayeux protocol helper used by the Lyrion CometD stream."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from music_assistant_models.errors import ProviderUnavailableError

BayeuxMessage = dict[str, Any]
BayeuxSender = Callable[
    [list[BayeuxMessage], int],
    Awaitable[list[BayeuxMessage]],
]
BayeuxMessageHandler = Callable[[BayeuxMessage], Awaitable[None]]
BayeuxStopCheck = Callable[[], bool]
BayeuxPreConnectHook = Callable[[], Awaitable[None]]


class BayeuxClient:
    """Implement Bayeux meta flow on top of an injected transport."""

    def __init__(self, sender: BayeuxSender) -> None:
        """
        Initialize Bayeux client.

        :param sender: Async transport callback that posts Bayeux messages.
        """
        self._sender = sender
        self._message_id = 1

    def reset_message_id(self) -> None:
        """Reset outgoing message id counter for a new session."""
        self._message_id = 1

    async def open_session(self, timeout: int) -> str:
        """
        Open one Bayeux session and return client id.

        Performs message-id reset, handshake and self-channel meta subscribe.

        :param timeout: Request timeout in seconds.
        """
        self.reset_message_id()
        client_id = await self.handshake(timeout)
        await self.subscribe_meta(
            client_id,
            f"/{client_id}/**",
            timeout,
        )
        return client_id

    async def run_connect_loop(
        self,
        client_id: str,
        connect_timeout: int,
        should_stop: BayeuxStopCheck,
        message_handler: BayeuxMessageHandler,
        pre_connect_hook: BayeuxPreConnectHook | None = None,
    ) -> None:
        """
        Run /meta/connect long-polls until stop or reconnect request.

        :param client_id: Active Bayeux client id.
        :param connect_timeout: Long-poll timeout in seconds.
        :param should_stop: Callback to stop connect pumping.
        :param message_handler: Callback for each non-failed connect message.
        :param pre_connect_hook: Optional callback before each connect call.
        """
        while not should_stop():
            if pre_connect_hook is not None:
                await pre_connect_hook()

            response = await self.connect(client_id, connect_timeout)
            for message in response:
                if self.is_failed_connect(message):
                    return
                await message_handler(message)

    async def handshake(self, timeout: int) -> str:
        """Execute /meta/handshake and return Bayeux client id."""
        response = await self._sender(
            [
                {
                    "id": self._next_message_id(),
                    "channel": "/meta/handshake",
                    "version": "1.0",
                    "minimumVersion": "1.0",
                    "supportedConnectionTypes": ["long-polling"],
                    "advice": {
                        "timeout": 60000,
                        "interval": 0,
                    },
                }
            ],
            timeout,
        )
        if not response:
            raise ProviderUnavailableError("CometD handshake returned no payload")

        handshake = response[0]
        if not handshake.get("successful"):
            raise ProviderUnavailableError(f"CometD handshake failed: {handshake}")

        client_id = handshake.get("clientId")
        if not isinstance(client_id, str) or not client_id:
            raise ProviderUnavailableError("CometD handshake did not return a client id")
        return client_id

    async def subscribe_meta(
        self,
        client_id: str,
        subscription: str,
        timeout: int,
    ) -> list[BayeuxMessage]:
        """Execute /meta/subscribe for one channel pattern."""
        response = await self._sender(
            [
                {
                    "id": self._next_message_id(),
                    "channel": "/meta/subscribe",
                    "clientId": client_id,
                    "subscription": subscription,
                }
            ],
            timeout,
        )
        if not response or not response[0].get("successful", True):
            raise ProviderUnavailableError(f"CometD channel subscribe failed: {response}")
        return response

    async def publish(
        self,
        channel: str,
        client_id: str,
        data: dict[str, Any],
        timeout: int,
    ) -> list[BayeuxMessage]:
        """Post one non-meta Bayeux message."""
        response = await self._sender(
            [
                {
                    "id": self._next_message_id(),
                    "channel": channel,
                    "clientId": client_id,
                    "data": data,
                }
            ],
            timeout,
        )
        if not response or not response[0].get("successful", True):
            raise ProviderUnavailableError(f"CometD publish failed on {channel}: {response}")
        return response

    async def connect(
        self,
        client_id: str,
        timeout: int,
    ) -> list[BayeuxMessage]:
        """Execute one /meta/connect long-poll request."""
        return await self._sender(
            [
                {
                    "id": self._next_message_id(),
                    "channel": "/meta/connect",
                    "clientId": client_id,
                    "connectionType": "long-polling",
                }
            ],
            timeout,
        )

    async def disconnect(self, client_id: str, timeout: int) -> None:
        """Best-effort /meta/disconnect."""
        await self._sender(
            [
                {
                    "id": self._next_message_id(),
                    "channel": "/meta/disconnect",
                    "clientId": client_id,
                }
            ],
            timeout,
        )

    @staticmethod
    def is_failed_connect(message: BayeuxMessage) -> bool:
        """Return True when a connect response asks for reconnect."""
        if message.get("channel") != "/meta/connect":
            return False
        return not bool(message.get("successful", False))

    def _next_message_id(self) -> str:
        """Return next message id as Bayeux string id."""
        current = self._message_id
        self._message_id += 1
        return str(current)
