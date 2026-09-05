"""LMS test-server harness abstractions for fake and Docker backends."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from aiohttp import web

from .fake_lms_server import FakeLmsServer


@dataclass(slots=True, frozen=True)
class LyrionTestEndpoint:
    """Resolved endpoint details for fake or Docker-managed LMS."""

    host: str
    port: int
    base_url: str
    source: str
    fake_server: FakeLmsServer | None = None


class LyrionTestLmsServer(Protocol):
    """Common harness contract for fake and Docker-backed LMS servers."""

    source: str
    fake_server: FakeLmsServer | None

    @property
    def endpoint(self) -> LyrionTestEndpoint:
        """Return resolved endpoint details for this harness."""

    async def start(self) -> None:
        """Prepare the harness before tests use its endpoint."""

    async def stop(self) -> None:
        """Tear down harness resources after test use."""


class FakeLmsServerHarness:
    """Fake LMS implementation of the shared Lyrion test harness contract."""

    source = "fake"

    def __init__(self, unused_tcp_port_factory: Callable[[], int]) -> None:
        """
        Initialize fake harness dependencies.

        :param unused_tcp_port_factory: Factory used to allocate a free port.
        """
        self._unused_tcp_port_factory = unused_tcp_port_factory
        self._runner: web.AppRunner | None = None
        self._endpoint: LyrionTestEndpoint | None = None
        self.fake_server: FakeLmsServer | None = None

    @property
    def endpoint(self) -> LyrionTestEndpoint:
        """Return fake LMS endpoint details."""
        if self._endpoint is None:
            msg = "FakeLmsServerHarness not started"
            raise RuntimeError(msg)
        return self._endpoint

    async def start(self) -> None:
        """Start the in-memory fake LMS HTTP server."""
        fake_server = FakeLmsServer()
        runner = web.AppRunner(fake_server.app)
        await runner.setup()
        port = self._unused_tcp_port_factory()
        site = web.TCPSite(runner, host="127.0.0.1", port=port)
        await site.start()

        self._runner = runner
        self.fake_server = fake_server
        self._endpoint = LyrionTestEndpoint(
            host="127.0.0.1",
            port=port,
            base_url=f"http://127.0.0.1:{port}",
            source=self.source,
            fake_server=fake_server,
        )

    async def stop(self) -> None:
        """Stop fake LMS server if it is running."""
        if self._runner is not None:
            await self._runner.cleanup()
        self._runner = None
        self._endpoint = None
        self.fake_server = None


class DockerLmsServerHarness:
    """Docker LMS adapter implementing the shared harness contract."""

    source = "docker"
    fake_server: FakeLmsServer | None = None

    def __init__(self, endpoint: LyrionTestEndpoint) -> None:
        """
        Store already-prepared Docker endpoint details.

        :param endpoint: Docker-backed LMS endpoint resolved by live_docker.
        """
        self._endpoint = endpoint

    @property
    def endpoint(self) -> LyrionTestEndpoint:
        """Return Docker LMS endpoint details."""
        return self._endpoint

    async def start(self) -> None:
        """No-op: Docker lifecycle is managed by live_docker fixture."""

    async def stop(self) -> None:
        """No-op: Docker lifecycle is managed by live_docker fixture."""
