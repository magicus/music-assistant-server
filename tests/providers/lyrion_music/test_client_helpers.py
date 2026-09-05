"""Unit tests for Lyrion client helper functions."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import ClientError
from music_assistant_models.errors import MediaNotFoundError, ProviderUnavailableError

from music_assistant.providers.lyrion_music import client


class _Response:
    """Minimal async response context manager for rpc tests."""

    def __init__(self, *, payload: dict[str, Any], status_error: Exception | None = None) -> None:
        self._payload = payload
        self._status_error = status_error

    async def json(self) -> dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        if self._status_error is not None:
            raise self._status_error

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


def _provider(*, host: Any = "127.0.0.1", port: Any = 9000) -> Any:
    provider = Mock()
    provider.logger = Mock()
    provider.mass.http_session.post = Mock()

    def _get_setup_value(key: str, default: Any = None) -> Any:
        if key == client.CONF_LMS_HOST:
            return host
        if key == client.CONF_LMS_PORT:
            return port if port is not None else default
        return default

    provider.get_setup_value = Mock(side_effect=_get_setup_value)
    provider._disabled_batch_lookup_keys = set()
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


async def test_get_entity_pages_skip_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """Page decoders should skip rows that raise MediaNotFoundError."""

    async def _page(*args: Any, **kwargs: Any) -> tuple[list[dict[str, Any]], bool]:
        del args, kwargs
        return ([{"id": "1"}, {"id": "2"}], False)

    monkeypatch.setattr(client, "_get_entity_page", _page)

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

    async def _page(*args: Any, **kwargs: Any) -> tuple[list[dict[str, Any]], bool]:
        del args, kwargs
        return (
            [
                {"id": "x1", "playlist": "P1"},
                {"id": "x2", "name": "P2"},
                {"id": "x3"},
                {"playlist": "missing-id"},
            ],
            True,
        )

    monkeypatch.setattr(client, "_get_simple_browse_page", _page)

    provider = _provider()
    playlists, has_more = await client.get_playlists_page(provider)
    assert has_more is True
    assert playlists == [
        {"id": "x1", "name": "P1"},
        {"id": "x2", "name": "P2"},
        {"id": "x3", "name": "x3"},
    ]

    async def _genres_page(*args: Any, **kwargs: Any) -> tuple[list[dict[str, Any]], bool]:
        del args, kwargs
        return ([{"id": "g1", "genre": "Rock"}, {"id": "g2"}], False)

    monkeypatch.setattr(client, "_get_simple_browse_page", _genres_page)
    genres, has_more = await client.get_genres_page(provider)
    assert has_more is False
    assert genres == [{"id": "g1", "name": "Rock"}, {"id": "g2", "name": "g2"}]


async def test_get_all_playlists_and_genres_paging(monkeypatch: pytest.MonkeyPatch) -> None:
    """Paged loops should stop on empty page or has_more False."""
    playlist_pages = [([{"id": "p1", "name": "P1"}], True), ([{"id": "p2", "name": "P2"}], False)]
    genre_pages = [([{"id": "g1", "name": "G1"}], True), ([], True)]

    async def _playlists(_provider: Any, offset: int = 0, limit: int = 0):
        del _provider, limit
        return playlist_pages[0] if offset == 0 else playlist_pages[1]

    async def _genres(_provider: Any, offset: int = 0, limit: int = 0):
        del _provider, limit
        return genre_pages[0] if offset == 0 else genre_pages[1]

    monkeypatch.setattr(client, "get_playlists_page", _playlists)
    monkeypatch.setattr(client, "get_genres_page", _genres)

    provider = _provider()
    assert await client.get_all_playlists(provider) == [
        {"id": "p1", "name": "P1"},
        {"id": "p2", "name": "P2"},
    ]
    assert await client.get_all_genres(provider) == [{"id": "g1", "name": "G1"}]


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
    provider = _provider()

    tracks = await client.get_album_tracks(provider, "alb1")
    assert [(t.disc_number, t.track_number) for t in tracks] == [(1, 1), (1, 2), (2, 3)]

    playlist_tracks = await client.get_playlist_tracks(provider, "pl1")
    assert len(playlist_tracks) == 1


def test_count_and_lookup_helpers() -> None:
    """Small helper functions should cover edge branches."""
    assert client._extract_browse_total_count({"count": None}) is None
    assert client._extract_browse_total_count({"count": "0"}) is None
    assert client._extract_browse_total_count({"count": "2"}) == 2

    assert client._format_lookup_progress(1, 0) == "progress: unknown"
    assert client._format_lookup_progress(1, 1) == "single-item lookup"
    assert "item 2/4" in client._format_lookup_progress(2, 4)

    command = client._create_lookup_command(client.ALBUM_SPEC, ["1", "2"])
    assert command[-1] == "album_id:1,2"

    with pytest.raises(ValueError):
        client._create_lookup_command(client.ALBUM_SPEC, [])


def test_split_lookup_reply_and_normalize() -> None:
    """Lookup response splitter should preserve request order and fail on misses."""
    result = {
        "albums_loop": [
            {"id": "a2", "album": "two"},
            {"id": "a1", "album": "one"},
            {"id": "a1", "album": "dup"},
        ]
    }
    split = client._split_lookup_reply(client.ALBUM_SPEC, result, ["a1", "a2"])
    assert [item["id"] for item in split] == ["a1", "a2"]

    with pytest.raises(ValueError):
        client._split_lookup_reply(client.ALBUM_SPEC, result, ["a1", "missing"])

    provider = _provider()
    normalized = client._normalize_lookup_ids(provider, client.ALBUM_SPEC, ["a", "a", "b"])
    assert normalized == ["a", "b"]


def test_batch_flags_and_chunking() -> None:
    """Batch enable/disable state and chunking should behave predictably."""
    provider = _provider()

    assert client._is_batch_lookup_enabled(provider, client.ALBUM_SPEC) is True
    assert client._is_batch_lookup_enabled(provider, client.ARTIST_SPEC) is False

    err = ValueError("x")
    client._disable_batch_lookup(provider, client.ALBUM_SPEC, err)
    assert client.ALBUM_SPEC.key in provider._disabled_batch_lookup_keys
    client._disable_batch_lookup(provider, client.ALBUM_SPEC, err)

    chunks = list(client._chunked(["1", "2", "3", "4", "5"], 2))
    assert chunks == [["1", "2"], ["3", "4"], ["5"]]


async def test_get_entity_data_and_iter_entities_fast_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Single-item and empty-id paths should work without pipeline workers."""

    async def _raw(_provider: Any, _spec: Any, _ids: list[str]):
        yield {"id": "42"}

    async def _decode(_provider: Any, _spec: Any, _raw_item: dict[str, Any]):
        return "decoded"

    monkeypatch.setattr(client, "_iter_raw_entities", _raw)
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

    async def _empty(_provider: Any, _spec: Any, _ids: list[str]):
        if False:
            yield {}

    monkeypatch.setattr(client, "_iter_raw_entities", _empty)

    with pytest.raises(MediaNotFoundError):
        await client._get_entity_data(_provider(), client.TRACK_SPEC, "missing")


async def test_get_entity_pages_has_more_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    """has_more should use count when present and fallback to page size when absent."""

    async def _rpc_count(*args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        return {"albums_loop": [{"id": "1"}], "count": 2}

    monkeypatch.setattr(client, "rpc_request", _rpc_count)
    page, has_more = await client._get_entity_page(_provider(), client.ALBUM_SPEC, 0, 1)
    assert len(page) == 1
    assert has_more is True

    async def _rpc_entity_no_count(*args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        return {"albums_loop": []}

    monkeypatch.setattr(client, "rpc_request", _rpc_entity_no_count)
    page, has_more = await client._get_entity_page(_provider(), client.ALBUM_SPEC, 0, 1)
    assert page == []
    assert has_more is False

    async def _rpc_no_count(*args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        return {"playlists_loop": [{"id": "1"}]}

    monkeypatch.setattr(client, "rpc_request", _rpc_no_count)
    page, has_more = await client._get_simple_browse_page(
        _provider(), "playlists", "playlists_loop", 0, 1
    )
    assert len(page) == 1
    assert has_more is True


async def test_get_browse_ids_without_count_uses_found_so_far_and_offset_paging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_get_browse_ids should use the no-count progress text and advance offset by page size."""
    provider = _provider()
    observed_offsets: list[int] = []

    first_page = [{"id": f"id{i}"} for i in range(client.BROWSE_PAGE_SIZE)]
    second_page = [{"id": "id-last"}]

    async def _rpc(_provider: Any, player_id: str, command: list[Any]) -> dict[str, Any]:
        del _provider, player_id
        observed_offsets.append(int(command[1]))
        if int(command[1]) == 0:
            return {client.ARTIST_SPEC.loop_key: first_page}
        if int(command[1]) == client.BROWSE_PAGE_SIZE:
            return {client.ARTIST_SPEC.loop_key: second_page}
        return {client.ARTIST_SPEC.loop_key: []}

    progress_texts: list[str] = []

    def _capture_progress(text: str) -> None:
        progress_texts.append(text)

    monkeypatch.setattr(client, "rpc_request", _rpc)
    monkeypatch.setattr(client, "update_current_task_progress_text", _capture_progress)
    monkeypatch.setattr(client, "_update_weighted_sync_progress", lambda **_: None)

    ids = await client._get_browse_ids(provider, client.ARTIST_SPEC)

    assert len(ids) == client.BROWSE_PAGE_SIZE + 1
    assert observed_offsets == [0, client.BROWSE_PAGE_SIZE]
    assert any("found so far" in text for text in progress_texts)


async def test_get_entity_page_filter_and_has_more_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """_get_entity_page should append filter value and compute has_more=False at total boundary."""
    captured_commands: list[list[Any]] = []

    async def _rpc(_provider: Any, player_id: str, command: list[Any]) -> dict[str, Any]:
        del _provider, player_id
        captured_commands.append(command)
        return {"albums_loop": [{"id": "1"}], "count": 3}

    monkeypatch.setattr(client, "rpc_request", _rpc)

    page, has_more = await client._get_entity_page(
        _provider(),
        client.ALBUM_SPEC,
        offset=2,
        limit=5,
        filter_value="genre_id:g1",
    )

    assert len(page) == 1
    assert has_more is False
    assert captured_commands
    assert captured_commands[0][-1] == "genre_id:g1"


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

    provider.mass.http_session.post.return_value = _Response(payload={"result": {"ok": True}})
    assert await client.rpc_request(provider, "", ["serverstatus"]) == {"ok": True}

    with pytest.raises(ProviderUnavailableError, match="not configured"):
        await client.rpc_request(_provider(host=" "), "", ["serverstatus"])

    provider.mass.http_session.post = Mock(side_effect=TimeoutError)
    with pytest.raises(ProviderUnavailableError, match="did not respond in time"):
        await client.rpc_request(provider, "", ["serverstatus"])

    provider.mass.http_session.post = Mock(side_effect=ClientError("boom"))
    with pytest.raises(ProviderUnavailableError, match="connection"):
        await client.rpc_request(provider, "", ["serverstatus"])

    bad_json_response = _Response(payload={"result": {"x": 1}})
    bad_json_response.json = AsyncMock(side_effect=ValueError("bad json"))
    provider.mass.http_session.post = Mock(return_value=bad_json_response)
    with pytest.raises(ProviderUnavailableError, match="invalid JSON"):
        await client.rpc_request(provider, "", ["serverstatus"])

    provider.mass.http_session.post = Mock(
        return_value=_Response(payload={"error": {"code": -1, "message": "nope"}})
    )
    with pytest.raises(ProviderUnavailableError, match="failed with code"):
        await client.rpc_request(provider, "", ["albums"])

    provider.mass.http_session.post = Mock(return_value=_Response(payload={"result": None}))
    with pytest.raises(ProviderUnavailableError, match="missing result"):
        await client.rpc_request(provider, "", ["albums"])


def test_get_configured_host_and_port() -> None:
    """Host and port accessors should normalize and coerce setup values."""
    assert client.get_configured_host(_provider(host=" host.local ")) == "host.local"
    assert client.get_configured_host(_provider(host=None)) is None
    assert client.get_configured_host(_provider(host=42)) is None

    assert client.get_configured_port(_provider(port="9000")) == 9000
    assert client.get_configured_port(_provider(port="bad"), default=1234) == 1234
    assert client.get_configured_port(_provider(port=None), default=None) is None
