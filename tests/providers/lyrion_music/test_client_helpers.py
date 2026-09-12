# mypy: disable-error-code="attr-defined,no-untyped-def,unreachable,method-assign"
"""Unit tests for Lyrion client helper functions."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import ClientError
from music_assistant_models.errors import MediaNotFoundError, ProviderUnavailableError

from music_assistant.providers.lyrion import client as shared_client
from music_assistant.providers.lyrion_music import client
from pylyrion.errors import LyrionRequestError
from tests.providers.lyrion.rpc_test_doubles import FakeResponse, FakeRpcTransport


def _provider(
    *,
    host: Any = "127.0.0.1",
    port: Any = 9000,
    rpc_handler: Callable[[str, list[Any]], dict[str, Any] | Exception] | None = None,
) -> Any:
    provider = Mock()
    provider.logger = Mock()
    provider.mass.http_session.post = Mock()

    def _get_setup_value(key: str, default: Any = None) -> Any:
        if key == shared_client.CONF_LMS_HOST:
            return host
        if key == shared_client.CONF_LMS_PORT:
            return port if port is not None else default
        return default

    provider.get_setup_value = Mock(side_effect=_get_setup_value)
    provider.get_configured_host = Mock(return_value=host)
    provider.get_configured_port = Mock(return_value=port)
    provider._disabled_batch_lookup_keys = set()
    if rpc_handler is not None:
        transport = FakeRpcTransport(rpc_handler)
        provider.mass.http_session.post = Mock(side_effect=transport.post)
        provider._fake_rpc_transport = transport
    return provider


async def _collect(gen: AsyncGenerator[Any]) -> list[Any]:
    return [item async for item in gen]


async def test_get_all_wrappers_delegate(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_all wrappers should materialize their iterators."""

    async def _artists(_provider: Any):
        yield "a"

    async def _albums(_provider: Any, _filter: str | None = None):
        del _filter
        yield "b"

    async def _tracks(_provider: Any, _filter: str | None = None):
        del _filter
        yield "c"

    monkeypatch.setattr(client, "iter_library_artists", _artists)
    monkeypatch.setattr(client, "iter_library_albums", _albums)
    monkeypatch.setattr(client, "iter_library_tracks", _tracks)

    provider = _provider()
    assert await client.get_all_artists(provider) == ["a"]
    assert await client.get_all_albums(provider) == ["b"]
    assert await client.get_all_tracks(provider) == ["c"]


async def test_rpc_request_uses_shared_throttler(monkeypatch: pytest.MonkeyPatch) -> None:
    """JSON-RPC calls should pass through a shared Lyrion throttler."""
    provider = _provider()
    provider.mass.http_session.post.return_value = FakeResponse({"result": {"ok": True}})

    acquired = False

    @asynccontextmanager
    async def fake_acquire():
        nonlocal acquired
        acquired = True
        yield

    monkeypatch.setattr(shared_client, "_RPC_THROTTLER", SimpleNamespace(acquire=fake_acquire))

    assert await shared_client.rpc_request(provider, "", ["serverstatus"]) == {"ok": True}
    assert acquired is True


async def test_get_entity_pages_skip_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """Page decoders should skip rows that raise MediaNotFoundError."""
    library = Mock()
    library.get_entity_page = AsyncMock(
        return_value=SimpleNamespace(items=[{"id": "1"}, {"id": "2"}], has_more=False)
    )
    monkeypatch.setattr(client, "_build_library_client", lambda _provider: library)

    def _parse_artist(_provider: Any, row: dict[str, Any]) -> str:
        if row["id"] == "2":
            raise MediaNotFoundError("missing")
        return "artist-ok"

    def _parse_album(_provider: Any, row: dict[str, Any]) -> str:
        if row["id"] == "2":
            raise MediaNotFoundError("missing")
        return "album-ok"

    def _parse_track(_provider: Any, row: dict[str, Any]) -> str:
        if row["id"] == "2":
            raise MediaNotFoundError("missing")
        return "track-ok"

    monkeypatch.setattr(client.parsers, "parse_artist", _parse_artist)
    monkeypatch.setattr(client.parsers, "parse_album", _parse_album)
    monkeypatch.setattr(client.parsers, "parse_track", _parse_track)

    provider = _provider()
    artists, artists_more = await client.get_artists_page(provider, offset=0, limit=2)
    albums, albums_more = await client.get_albums_page(provider, offset=0, limit=2)
    tracks, tracks_more = await client.get_tracks_page(provider, offset=0, limit=2)

    assert artists == ["artist-ok"]
    assert albums == ["album-ok"]
    assert tracks == ["track-ok"]
    assert artists_more is False
    assert albums_more is False
    assert tracks_more is False


async def test_get_simple_pages_name_fallbacks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Playlist/genre pages should support id/name fallback branches."""
    library = Mock()
    library.get_simple_browse_page = AsyncMock(
        return_value=SimpleNamespace(
            items=[
                {"id": "x1", "playlist": "P1"},
                {"id": "x2", "name": "P2"},
                {"id": "x3"},
                {"playlist": "missing-id"},
            ],
            has_more=True,
        )
    )
    monkeypatch.setattr(client, "_build_library_client", lambda _provider: library)

    provider = _provider()
    playlists, has_more = await client.get_playlists_page(provider)
    assert has_more is True
    assert playlists == [
        {"id": "x1", "name": "P1"},
        {"id": "x2", "name": "P2"},
        {"id": "x3", "name": "x3"},
    ]

    library.get_simple_browse_page = AsyncMock(
        return_value=SimpleNamespace(
            items=[{"id": "g1", "genre": "Rock"}, {"id": "g2"}],
            has_more=False,
        )
    )
    genres, has_more = await client.get_genres_page(provider)
    assert has_more is False
    assert genres == [{"id": "g1", "name": "Rock"}, {"id": "g2", "name": "g2"}]


async def test_get_all_playlists_and_genres_paging(monkeypatch: pytest.MonkeyPatch) -> None:
    """Paged loops should stop on empty page or has_more False."""
    library = Mock()
    library.get_all_playlists = AsyncMock(return_value=[{"id": "p1", "name": "P1"}])
    library.get_all_genres = AsyncMock(return_value=[{"id": "g1", "name": "G1"}])
    monkeypatch.setattr(client, "_build_library_client", lambda _provider: library)

    provider = _provider()
    assert await client.get_all_playlists(provider) == [
        {"id": "p1", "name": "P1"},
    ]
    assert await client.get_all_genres(provider) == [{"id": "g1", "name": "G1"}]


async def test_get_playlist_tracks_page_delegates_to_pylyrion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Playlist track paging should use the pylyrion page helper."""
    library = Mock()
    library.get_playlist_tracks_page = AsyncMock(
        return_value=SimpleNamespace(items=[{"id": "t1", "title": "Track 1"}], has_more=False)
    )
    monkeypatch.setattr(client, "_build_library_client", lambda _provider: library)
    monkeypatch.setattr(client.parsers, "parse_track", lambda _provider, row: row["id"])

    provider = _provider()
    tracks, has_more = await client.get_playlist_tracks_page(provider, "pl1")

    assert tracks == ["t1"]
    assert has_more is False


async def test_get_playlist_tracks_delegates_to_pylyrion(monkeypatch: pytest.MonkeyPatch) -> None:
    """Playlist track collection should use the pylyrion raw helper."""
    library = Mock()
    library.get_playlist_tracks = AsyncMock(return_value=[{"id": "t1", "title": "Track 1"}])
    monkeypatch.setattr(client, "_build_library_client", lambda _provider: library)
    monkeypatch.setattr(client.parsers, "parse_track", lambda _provider, row: row["id"])

    provider = _provider()
    assert await client.get_playlist_tracks(provider, "pl1") == ["t1"]


async def test_search_and_entity_data_delegate_to_pylyrion(monkeypatch: pytest.MonkeyPatch) -> None:
    """Search and raw entity data helpers should use the pylyrion client facade."""
    library = Mock()
    library.search_entities = AsyncMock(
        side_effect=[
            [{"id": "a1", "artist": "Artist 1"}],
            [{"id": "al1", "album": "Album 1"}],
            [{"id": "t1", "title": "Track 1"}],
        ]
    )
    library.get_entity_row = AsyncMock(
        side_effect=[
            {"id": "a1", "artist": "Artist 1"},
            {"id": "al1", "album": "Album 1"},
            {"id": "t1", "title": "Track 1"},
        ]
    )
    monkeypatch.setattr(client, "_build_library_client", lambda _provider: library)

    monkeypatch.setattr(client.parsers, "parse_artist", lambda _provider, row: row["id"])
    monkeypatch.setattr(client.parsers, "parse_album", lambda _provider, row: row["id"])
    monkeypatch.setattr(client.parsers, "parse_track", lambda _provider, row: row["id"])

    provider = _provider()

    assert await client.search_artists(provider, "artist", 5) == ["a1"]
    assert await client.search_albums(provider, "album", 5) == ["al1"]
    assert await client.search_tracks(provider, "track", 5) == ["t1"]

    assert await client.get_artist_data(provider, "a1") == {"id": "a1", "artist": "Artist 1"}
    assert await client.get_album_data(provider, "al1") == {"id": "al1", "album": "Album 1"}
    assert await client.get_track_data(provider, "t1") == {"id": "t1", "title": "Track 1"}


async def test_get_album_tracks_sorting_and_playlist_tracks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Album tracks should sort by disc/track and playlist filter should be delegated."""

    class _Track:
        def __init__(self, disc: int, num: int) -> None:
            self.disc_number = disc
            self.track_number = num

    async def _iter_tracks(_provider: Any, filter_value: str | None = None):
        if filter_value and filter_value.startswith("playlist_id"):
            yield _Track(0, 0)
            return
        yield _Track(2, 3)
        yield _Track(1, 2)
        yield _Track(1, 1)

    monkeypatch.setattr(client, "iter_library_tracks", _iter_tracks)

    def _rpc_request(player_id: str, command: list[Any]) -> dict[str, Any]:
        assert player_id == ""
        assert command[:2] == ["playlists", "tracks"]
        assert any(str(part).startswith("playlist_id:pl1") for part in command)
        return {"playlisttracks_loop": [{"id": "trk1", "track": "Track 1"}], "count": "1"}

    monkeypatch.setattr(
        client.parsers,
        "parse_track",
        lambda _provider, _raw: _Track(0, 0),
    )
    provider = _provider(rpc_handler=_rpc_request)

    tracks = await client.get_album_tracks(provider, "alb1")
    assert [(t.disc_number, t.track_number) for t in tracks] == [(1, 1), (1, 2), (2, 3)]

    playlist_tracks = await client.get_playlist_tracks(provider, "pl1")
    assert len(playlist_tracks) == 1


def test_count_and_lookup_helpers() -> None:
    """Small helper functions should cover edge branches."""
    assert client._format_lookup_progress(1, 0) == "progress: unknown"
    assert client._format_lookup_progress(1, 1) == "single-item lookup"
    assert "item 2/4" in client._format_lookup_progress(2, 4)


async def test_iter_entity_rows_delegates_to_pylyrion_and_reports_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Entity-row iteration should delegate to pylyrion and keep MA progress updates."""
    provider = _provider()
    captured_args: list[tuple[Any, list[str]]] = []

    async def _iter_rows(spec: Any, ids: list[str]):
        captured_args.append((spec, ids))
        for item_id in ids:
            yield {"id": item_id, "album": item_id}

    library = SimpleNamespace(iter_entity_rows=_iter_rows)
    monkeypatch.setattr(client, "_build_library_client", lambda _provider: library)

    progress_calls: list[tuple[int, int, str | None]] = []

    def _capture_progress(*, phase: str, current: int, total: int, text: str | None = None) -> None:
        del phase
        progress_calls.append((current, total, text))

    monkeypatch.setattr(client, "_update_weighted_sync_progress", _capture_progress)

    item_ids = ["id1", "id1", "id2"]

    yielded = [row async for row in client._iter_entity_rows(provider, client.ALBUM_SPEC, item_ids)]

    assert [item["id"] for item in yielded] == ["id1", "id2"]
    assert captured_args == [(client.PY_ALBUM_SPEC, ["id1", "id2"])]
    assert progress_calls[-1][0] == 2
    assert progress_calls[-1][1] == 2


async def test_batch_lookup_incomplete_batch_does_not_replay_prior_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Incomplete batch responses should only fall back from the first missing item onward."""
    provider = _provider()
    item_ids = [f"id{i}" for i in range(client.BATCH_LOOKUP_SIZE + 2)]
    call_log: list[list[str]] = []
    batch_calls = 0

    async def _rpc_request(
        _provider: Any,
        player_id: str,
        command: list[Any],
        *,
        timeout: int = 0,
    ) -> dict[str, Any]:
        del _provider, player_id, timeout
        nonlocal batch_calls
        requested_ids = str(command[-1]).split(":", 1)[1].split(",")
        call_log.append(requested_ids)
        if len(requested_ids) > 1:
            batch_calls += 1
            if batch_calls == 2:
                return {
                    client.ALBUM_SPEC.loop_key: [
                        {"id": requested_ids[0], "album": requested_ids[0]}
                    ]
                }
        return {
            client.ALBUM_SPEC.loop_key: [
                {"id": item_id, "album": item_id} for item_id in requested_ids
            ]
        }

    monkeypatch.setattr(client, "rpc_request", _rpc_request)

    yielded = [
        raw_item
        async for raw_item in client._iter_raw_entities(provider, client.ALBUM_SPEC, item_ids)
    ]

    assert [item["id"] for item in yielded] == item_ids
    assert call_log[:2] == [
        item_ids[: client.BATCH_LOOKUP_SIZE],
        item_ids[client.BATCH_LOOKUP_SIZE :],
    ]
    assert call_log[2:] == [[item_id] for item_id in item_ids[client.BATCH_LOOKUP_SIZE :]]


async def test_get_entity_data_and_iter_entities_fast_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Single-item and empty-id paths should work without pipeline workers."""

    async def _iter_decoded(
        _spec: Any,
        ids: list[str],
        decode_row: Any,
        worker_count: int = 1,
    ):
        del worker_count
        for item_id in ids:
            yield await decode_row({"id": item_id})

    async def _decode(_provider: Any, _spec: Any, _raw_item: dict[str, Any]):
        return "decoded"

    library = SimpleNamespace(
        get_entity_row=AsyncMock(return_value={"id": "42"}),
        iter_decoded_entities=_iter_decoded,
    )
    monkeypatch.setattr(client, "_build_library_client", lambda _provider: library)
    monkeypatch.setattr(client, "_decode_entity", _decode)

    provider = _provider()
    entity = await client._get_entity_data(provider, client.ALBUM_SPEC, "42")
    assert entity == {"id": "42"}

    items = await _collect(client._iter_entities(provider, client.ALBUM_SPEC, []))
    assert items == []

    items = await _collect(client._iter_entities(provider, client.ALBUM_SPEC, ["42"]))
    assert items == ["decoded"]


async def test_get_entity_data_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing entity should raise MediaNotFoundError."""
    library = Mock()
    library.get_entity_row = AsyncMock(side_effect=LyrionRequestError("Track not found: missing"))
    monkeypatch.setattr(client, "_build_library_client", lambda _provider: library)

    with pytest.raises(MediaNotFoundError):
        await client._get_entity_data(_provider(), client.TRACK_SPEC, "missing")


async def test_get_entity_pages_has_more_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Album page helper should surface has_more from pylyrion page responses."""
    library = Mock()
    library.get_entity_page = AsyncMock(
        return_value=SimpleNamespace(items=[{"id": "1"}], has_more=True)
    )
    monkeypatch.setattr(client, "_build_library_client", lambda _provider: library)
    monkeypatch.setattr(client.parsers, "parse_album", lambda _provider, row: row["id"])

    page, has_more = await client.get_albums_page(_provider(), offset=0, limit=1)
    assert page == ["1"]
    assert has_more is True


async def test_get_browse_ids_delegates_to_pylyrion_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_get_browse_ids should delegate id discovery to pylyrion and report completion."""
    provider = _provider()
    library = Mock()
    library.get_artist_ids = AsyncMock(return_value=["id1", "id2"])
    monkeypatch.setattr(client, "_build_library_client", lambda _provider: library)

    progress_texts: list[str] = []
    monkeypatch.setattr(client, "update_current_task_progress_text", progress_texts.append)
    monkeypatch.setattr(client, "_update_weighted_sync_progress", lambda **_: None)

    ids = await client._get_browse_ids(provider, client.ARTIST_SPEC)

    assert ids == ["id1", "id2"]
    library.get_artist_ids.assert_awaited_once_with(None)
    assert any("Fetching number of artists" in text for text in progress_texts)
    assert any("done (2)" in text for text in progress_texts)


async def test_get_entity_page_filter_and_has_more_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Album page helper should pass filter value into delegated pylyrion calls."""
    library = Mock()
    library.get_entity_page = AsyncMock(
        return_value=SimpleNamespace(items=[{"id": "1", "album": "A"}], has_more=False)
    )

    monkeypatch.setattr(client, "_build_library_client", lambda _provider: library)
    monkeypatch.setattr(client.parsers, "parse_album", lambda _provider, row: row["id"])

    page, has_more = await client.get_albums_page(
        _provider(),
        filter_value="genre_id:g1",
        offset=2,
        limit=5,
    )

    assert page == ["1"]
    assert has_more is False
    library.get_entity_page.assert_awaited_once_with(
        client.PY_ALBUM_SPEC,
        2,
        5,
        filter_value="genre_id:g1",
    )


def test_should_report_lookup_progress_task_domain_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lookup progress should be suppressed only for artwork-sync task domain."""
    monkeypatch.setattr(client, "get_current_task", lambda: None)
    assert client._should_report_lookup_progress() is True

    monkeypatch.setattr(
        client,
        "get_current_task",
        lambda: SimpleNamespace(metadata={"task_domain": "lyrion_artwork_sync"}),
    )
    assert client._should_report_lookup_progress() is False

    monkeypatch.setattr(
        client,
        "get_current_task",
        lambda: SimpleNamespace(metadata={"task_domain": "lyrion_sync"}),
    )
    assert client._should_report_lookup_progress() is True


async def test_rpc_request_success_and_error_paths() -> None:
    """rpc_request should map transport/protocol failures to ProviderUnavailableError."""
    provider = _provider()

    provider.mass.http_session.post.return_value = FakeResponse({"result": {"ok": True}})
    assert await shared_client.rpc_request(provider, "", ["serverstatus"]) == {"ok": True}

    with pytest.raises(ProviderUnavailableError, match="not configured"):
        await shared_client.rpc_request(_provider(host=" "), "", ["serverstatus"])

    provider.mass.http_session.post = Mock(side_effect=TimeoutError)
    with pytest.raises(ProviderUnavailableError, match="did not respond in time"):
        await shared_client.rpc_request(provider, "", ["serverstatus"])

    provider.mass.http_session.post = Mock(side_effect=ClientError("boom"))
    with pytest.raises(ProviderUnavailableError, match="connection"):
        await shared_client.rpc_request(provider, "", ["serverstatus"])

    bad_json_response = FakeResponse({"result": {"x": 1}})
    bad_json_response.json = AsyncMock(side_effect=ValueError("bad json"))
    provider.mass.http_session.post = Mock(return_value=bad_json_response)
    with pytest.raises(ProviderUnavailableError, match="invalid JSON"):
        await shared_client.rpc_request(provider, "", ["serverstatus"])

    provider.mass.http_session.post = Mock(
        return_value=FakeResponse({"error": {"code": -1, "message": "nope"}})
    )
    with pytest.raises(ProviderUnavailableError, match="failed with code"):
        await shared_client.rpc_request(provider, "", ["albums"])

    provider.mass.http_session.post = Mock(return_value=FakeResponse({"error": "oops"}))
    with pytest.raises(ProviderUnavailableError, match="invalid error object"):
        await shared_client.rpc_request(provider, "", ["albums"])

    provider.mass.http_session.post = Mock(return_value=FakeResponse({"result": None}))
    with pytest.raises(ProviderUnavailableError, match="must contain a result object"):
        await shared_client.rpc_request(provider, "", ["albums"])

    provider.mass.http_session.post = Mock(return_value=FakeResponse({"result": []}))
    with pytest.raises(ProviderUnavailableError, match="must contain a result object"):
        await shared_client.rpc_request(provider, "", ["albums"])


def test_get_configured_host_and_port() -> None:
    """Host and port accessors should normalize and coerce setup values."""
    assert shared_client.get_configured_host(_provider(host=" host.local ")) == "host.local"
    assert shared_client.get_configured_host(_provider(host=None)) is None
    assert shared_client.get_configured_host(_provider(host=42)) is None

    assert shared_client.get_configured_port(_provider(port="9000")) == 9000
    assert shared_client.get_configured_port(_provider(port="bad"), default=1234) == 1234
    assert shared_client.get_configured_port(_provider(port=None), default=None) is None


def test_normalize_lms_text_value() -> None:
    """Raw LMS scalar values should be coerced to stripped text near the transport boundary."""
    assert shared_client.normalize_lms_text_value("  abc  ") == "abc"
    assert shared_client.normalize_lms_text_value(42) == "42"
    assert shared_client.normalize_lms_text_value("") is None
    assert shared_client.normalize_lms_text_value(None) is None
