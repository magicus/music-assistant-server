"""Core data models for pylyrion."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LyrionEndpoint:
    """Describe one Lyrion JSON-RPC endpoint."""

    host: str
    port: int | None = 9000
    path: str = "/jsonrpc.js"
