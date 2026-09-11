"""Unit tests for pylyrion library browse helpers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from pylyrion.library import LyrionLibraryClient
from pylyrion.models import LyrionEndpoint
from pylyrion.session import LyrionSession
from tests.providers.lyrion.rpc_test_doubles import FakeResponse


def _build_library_client(body: dict[str, object]) -> LyrionLibraryClient:
    """Create a pylyrion library client backed by a fake HTTP transport."""
    http_session = SimpleNamespace(post=MagicMock(return_value=FakeResponse(body)))
    session = LyrionSession(
        http_session=http_session,
        endpoint=LyrionEndpoint(host="127.0.0.1", port=9000),
    )
    return LyrionLibraryClient(session)


@pytest.mark.asyncio
async def test_library_client_pages_artists_and_playlists() -> None:
    """Library browsing should return raw LMS rows plus has-more metadata."""
    client = _build_library_client(
        {
            "result": {
                "artists_loop": [{"id": "a1", "name": "Artist 1"}],
                "playlists_loop": [{"id": "p1", "playlist": "Mix"}],
                "count": "1",
            }
        }
    )

    artists_page = await client.get_artists_page()
    playlists_page = await client.get_playlists_page()

    assert artists_page.items == [{"id": "a1", "name": "Artist 1"}]
    assert artists_page.has_more is False
    assert playlists_page.items == [{"id": "p1", "playlist": "Mix"}]
    assert playlists_page.has_more is False


@pytest.mark.asyncio
async def test_library_client_pages_tracks_with_filter() -> None:
    """Track browsing should pass filters through to the JSON-RPC request."""
    transport = SimpleNamespace(post=MagicMock(return_value=FakeResponse({"result": {}})))
    session = LyrionSession(
        http_session=transport,
        endpoint=LyrionEndpoint(host="127.0.0.1", port=9000),
    )
    client = LyrionLibraryClient(session)

    await client.get_tracks_page(filter_value="album_id:alb1")

    called_url = transport.post.call_args.args[0]
    called_payload = transport.post.call_args.kwargs["json"]
    assert called_url.endswith("/jsonrpc.js")
    assert called_payload["params"][1][-1] == "album_id:alb1"
