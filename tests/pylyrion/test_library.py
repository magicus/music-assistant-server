"""Unit tests for pylyrion library browse helpers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from pylyrion.library import ARTIST_SPEC, LyrionLibraryClient, _normalize_lookup_ids
from pylyrion.models import LyrionEndpoint
from pylyrion.session import LyrionSession
from tests.pylyrion.rpc_test_doubles import FakeResponse


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


@pytest.mark.asyncio
async def test_library_client_id_search_and_entity_lookup() -> None:
    """Raw id discovery, search and entity lookup should round-trip LMS rows."""
    body = {
        "result": {
            "artists_loop": [{"id": "a1"}],
            "albums_loop": [{"id": "al1"}],
            "titles_loop": [{"id": "t1"}],
            "count": "1",
        }
    }
    client = _build_library_client(body)

    assert await client.get_artist_ids() == ["a1"]
    assert await client.get_album_ids() == ["al1"]
    assert await client.get_track_ids() == ["t1"]

    search_rows = await client.search_entities(ARTIST_SPEC, "ignored", 1)
    assert search_rows == [{"id": "a1"}]


@pytest.mark.asyncio
async def test_library_client_playlist_tracks_and_entity_data() -> None:
    """Raw playlist track pages and entity lookups should return LMS rows."""
    client = _build_library_client(
        {
            "result": {
                "playlisttracks_loop": [{"id": "p1"}],
                "playlists_loop": [{"id": "pl1", "playlist": "Mix"}],
                "genres_loop": [{"id": "g1", "genre": "Rock"}],
                "artists_loop": [{"id": "a1", "artist": "Artist 1"}],
                "count": "1",
            }
        }
    )

    playlists = await client.get_all_playlists()
    genres = await client.get_all_genres()
    playlist_page = await client.get_playlist_tracks_page("pl1")
    playlist_tracks = await client.get_playlist_tracks("pl1")
    artist_row = await client.get_entity_row(ARTIST_SPEC, "a1")

    assert playlists == [{"id": "pl1", "name": "Mix"}]
    assert genres == [{"id": "g1", "name": "Rock"}]
    assert playlist_page.items == [{"id": "p1"}]
    assert playlist_page.has_more is False
    assert playlist_tracks == [{"id": "p1"}]
    assert artist_row == {"id": "a1", "artist": "Artist 1"}


def test_normalize_lookup_ids_keeps_stable_order() -> None:
    """Lookup id normalization should deduplicate while preserving first-seen order."""
    assert _normalize_lookup_ids(["a", "a", "b", "a", "c"]) == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_library_client_iter_decoded_entities() -> None:
    """Decoded iteration should apply callback decoding on normalized row order."""
    client = _build_library_client(
        {
            "result": {
                "artists_loop": [{"id": "a1", "name": "Artist 1"}],
                "count": "1",
            }
        }
    )

    async def _decode(row: dict[str, str]) -> str:
        return row["id"]

    result = [
        item
        async for item in client.iter_decoded_entities(
            ARTIST_SPEC,
            ["a1", "a1"],
            _decode,
        )
    ]
    assert result == ["a1"]


@pytest.mark.asyncio
async def test_iter_entity_rows_incomplete_later_batch_no_replay() -> None:
    """Fallback should resume from failed batch, not replay already-yielded rows."""

    class _FakeSession:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []
            self.batch_calls = 0

        async def request(self, _player_id: str, command: list[object]) -> dict[str, object]:
            requested_ids = str(command[-1]).split(":", 1)[1].split(",")
            self.calls.append(requested_ids)
            if len(requested_ids) > 1:
                self.batch_calls += 1
                if self.batch_calls == 2:
                    return {
                        "albums_loop": [
                            {"id": requested_ids[0], "album": requested_ids[0]},
                        ]
                    }
            return {"albums_loop": [{"id": item_id, "album": item_id} for item_id in requested_ids]}

    session = _FakeSession()
    item_ids = [f"id{idx}" for idx in range(27)]

    rows = [
        row
        async for row in LyrionLibraryClient(session).iter_entity_rows(
            ARTIST_SPEC.__class__(
                key="album",
                command="albums",
                loop_key="albums_loop",
                tags="tags:abcdefghijklmnopqrstuvwxyz",
                id_filter_key="album_id",
                id_keys=("id", "album_id"),
                supports_batch_lookup=True,
            ),
            item_ids,
        )
    ]

    assert [row["id"] for row in rows] == item_ids
    assert session.calls[:2] == [item_ids[:25], item_ids[25:]]
    assert session.calls[2:] == [[item_id] for item_id in item_ids[25:]]
