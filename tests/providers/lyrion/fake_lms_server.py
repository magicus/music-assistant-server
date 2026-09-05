"""In-memory fake LMS server for provider contract tests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aiohttp import web

from .catalog_seed import (
    fake_albums,
    fake_artists,
    fake_genres,
    fake_playlist_tracks,
    fake_playlists,
    fake_tracks,
)

PNG_1X1_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
    b"\x00\x00\x00\x0cIDAT\x08\x1dc``\x00\x00\x00\x02"
    b"\x00\x01\xe2!\xbc3\x00\x00\x00\x00IEND\xaeB`\x82"
)


@dataclass(slots=True, frozen=True)
class FakeRpcCall:
    """One captured JSON-RPC call sent by the provider."""

    command: str
    player_id: str
    args: tuple[Any, ...]


class FakeLmsServer:
    """Minimal LMS JSON-RPC surface with deterministic fake library data."""

    def __init__(self) -> None:
        """Initialize fake data and captured request state."""
        self.rpc_calls: list[FakeRpcCall] = []
        self.stream_requests: list[str] = []
        self.image_requests: list[str] = []
        self.command_errors: dict[str, tuple[int, str]] = {}
        self.incomplete_batch_for_entities: set[str] = set()
        self.missing_large_artist_images: set[str] = {"a3"}
        self.missing_large_album_images: set[str] = {"alb6"}

        self.artists: list[dict[str, Any]] = fake_artists()
        self.albums: list[dict[str, Any]] = fake_albums()
        self.tracks: list[dict[str, Any]] = fake_tracks()
        self.playlists: list[dict[str, Any]] = fake_playlists()
        self.genres: list[dict[str, Any]] = fake_genres()
        self.playlist_tracks: dict[str, list[str]] = fake_playlist_tracks()

    @property
    def app(self) -> web.Application:
        """Build and return an aiohttp app exposing fake LMS endpoints."""
        app = web.Application()
        app.router.add_post("/jsonrpc.js", self.handle_jsonrpc)
        app.router.add_get(
            "/music/{track_id}/download",
            self.handle_track_stream,
        )
        for route in (
            "/music/{cover_id}/cover_600x600_f",
            "/music/{cover_id}/cover_300x300_f",
            "/contributor/{artist_id}/image_600x600_f",
            "/contributor/{artist_id}/image_300x300_f",
            "/imageproxy/mai/_artist/{artist_id}/image_600x600_f",
            "/imageproxy/mai/_artist/{artist_id}/image_300x300_f",
        ):
            app.router.add_get(route, self.handle_artwork)
        return app

    async def handle_jsonrpc(self, request: web.Request) -> web.Response:
        """Handle LMS JSON-RPC method slim.request."""
        payload = await request.json()
        params = payload.get("params") if isinstance(payload, dict) else None
        if not isinstance(params, list) or len(params) < 2:
            return self._rpc_error(-32602, "invalid params", status=400)

        player_id = str(params[0])
        command = params[1]
        if not isinstance(command, list) or not command:
            return self._rpc_error(-32602, "invalid command", status=400)

        command_name = str(command[0])
        self.rpc_calls.append(
            FakeRpcCall(
                command=command_name,
                player_id=player_id,
                args=tuple(command),
            )
        )

        if command_name in self.command_errors:
            code, message = self.command_errors[command_name]
            return self._rpc_error(code, message, status=200)

        if command_name == "serverstatus":
            return web.json_response({"id": 1, "result": {"count": 1}})

        if command_name == "artists":
            return self._rpc_result(command, "artists")
        if command_name == "albums":
            return self._rpc_result(command, "albums")
        if command_name == "titles":
            return self._rpc_result(command, "titles")
        if command_name == "playlists":
            return self._rpc_result(command, "playlists")
        if command_name == "genres":
            return self._rpc_result(command, "genres")

        return self._rpc_error(
            -32601,
            f"Unknown command: {command_name}",
            status=200,
        )

    async def handle_track_stream(self, request: web.Request) -> web.Response:
        """Return fake audio bytes for stream URL tests."""
        track_id = request.match_info["track_id"]
        self.stream_requests.append(track_id)
        body = f"FAKEAUDIO:{track_id}".encode()
        return web.Response(body=body, content_type="audio/mpeg")

    async def handle_artwork(self, request: web.Request) -> web.Response:
        """Return fake image bytes and optional 600px misses for fallback tests."""
        self.image_requests.append(request.path)
        artist_id = request.match_info.get("artist_id")
        cover_id = request.match_info.get("cover_id")
        if "600x600" in request.path:
            if artist_id and artist_id in self.missing_large_artist_images:
                return web.Response(status=404)
            if cover_id and cover_id in self.missing_large_album_images:
                return web.Response(status=404)
        return web.Response(body=PNG_1X1_BYTES, content_type="image/png")

    def clear_history(self) -> None:
        """Clear captured request state between assertions."""
        self.rpc_calls.clear()
        self.stream_requests.clear()
        self.image_requests.clear()

    def _rpc_result(self, command: list[Any], entity: str) -> web.Response:
        return web.json_response({"id": 1, "result": self._build_result(command, entity)})

    def _rpc_error(self, code: int, message: str, status: int) -> web.Response:
        return web.json_response(
            {"id": 1, "error": {"code": code, "message": message}},
            status=status,
        )

    def _build_result(self, command: list[Any], entity: str) -> dict[str, Any]:
        if entity == "playlists" and len(command) > 1 and command[1] == "tracks":
            return self._build_playlist_tracks_result(command)

        offset = _coerce_int(command[1] if len(command) > 1 else 0, 0)
        limit = _coerce_int(command[2] if len(command) > 2 else 250, 250)
        filters = [
            str(arg)
            for arg in command[3:]
            if isinstance(arg, (str, int, float)) and ":" in str(arg)
        ]

        if entity == "artists":
            rows = self._filter_artists(self.artists, filters)
            rows = self._maybe_make_incomplete_batch(entity, filters, rows)
            return _page_result("artists_loop", rows, offset, limit)
        if entity == "albums":
            rows = self._filter_albums(self.albums, filters)
            rows = self._maybe_make_incomplete_batch(entity, filters, rows)
            return _page_result("albums_loop", rows, offset, limit)
        if entity == "titles":
            rows = self._filter_tracks(self.tracks, filters)
            rows = self._maybe_make_incomplete_batch(entity, filters, rows)
            return _page_result("titles_loop", rows, offset, limit)
        if entity == "playlists":
            rows = self._filter_playlists(self.playlists, filters)
            return _page_result("playlists_loop", rows, offset, limit)

        rows = self._filter_genres(self.genres, filters)
        rows = sorted(
            rows,
            key=lambda row: str(row.get("genre") or row.get("name") or "").casefold(),
        )
        return _page_result("genres_loop", rows, offset, limit)

    def _build_playlist_tracks_result(self, command: list[Any]) -> dict[str, Any]:
        """Return a paged playlisttracks_loop response for playlists tracks."""
        offset = _coerce_int(command[2] if len(command) > 2 else 0, 0)
        limit = _coerce_int(command[3] if len(command) > 3 else 250, 250)
        filters = [
            str(arg)
            for arg in command[4:]
            if isinstance(arg, (str, int, float)) and ":" in str(arg)
        ]

        playlist_id: str | None = None
        for key, value in _iter_filters(filters):
            if key in {"playlist_id", "id"}:
                playlist_id = value
                break

        if playlist_id is None:
            return _page_result("playlisttracks_loop", [], offset, limit)

        wanted_ids = self.playlist_tracks.get(playlist_id, [])
        track_lookup = {str(row["id"]): row for row in self.tracks}
        rows = [track_lookup[track_id] for track_id in wanted_ids if track_id in track_lookup]
        return _page_result("playlisttracks_loop", rows, offset, limit)

    def _maybe_make_incomplete_batch(
        self,
        entity: str,
        filters: list[str],
        rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Optionally drop one row from multi-id lookups to simulate LMS bugs."""
        if entity not in self.incomplete_batch_for_entities:
            return rows

        id_filter_key = {
            "artists": "artist_id",
            "albums": "album_id",
            "titles": "track_id",
        }.get(entity)
        if id_filter_key is None:
            return rows

        for key, value in _iter_filters(filters):
            if key == id_filter_key and "," in value and len(rows) > 1:
                return rows[:-1]
        return rows

    def _filter_artists(
        self,
        rows: list[dict[str, Any]],
        filters: list[str],
    ) -> list[dict[str, Any]]:
        result = rows
        for key, value in _iter_filters(filters):
            if key == "search":
                result = [
                    row for row in result if _search_match(value, row, ("artist", "name", "id"))
                ]
            elif key in {"artist_id", "id"}:
                wanted = set(_split_csv(value))
                result = [row for row in result if str(row.get("id")) in wanted]
        return result

    def _filter_albums(
        self,
        rows: list[dict[str, Any]],
        filters: list[str],
    ) -> list[dict[str, Any]]:
        result = rows
        for key, value in _iter_filters(filters):
            if key == "search":
                result = [
                    row
                    for row in result
                    if _search_match(
                        value,
                        row,
                        ("album", "title", "artist", "id"),
                    )
                ]
            elif key in {"album_id", "id"}:
                wanted = set(_split_csv(value))
                result = [row for row in result if str(row.get("id")) in wanted]
            elif key == "artist_id":
                wanted = set(_split_csv(value))
                result = [row for row in result if str(row.get("artist_id")) in wanted]
            elif key == "genre_id":
                wanted = set(_split_csv(value))
                result = [row for row in result if str(row.get("genre_id")) in wanted]
        return result

    def _filter_tracks(
        self,
        rows: list[dict[str, Any]],
        filters: list[str],
    ) -> list[dict[str, Any]]:
        result = rows
        for key, value in _iter_filters(filters):
            if key == "search":
                result = [
                    row
                    for row in result
                    if _search_match(
                        value,
                        row,
                        ("title", "track", "artist", "album", "id"),
                    )
                ]
            elif key in {"track_id", "id"}:
                wanted = set(_split_csv(value))
                result = [row for row in result if str(row.get("id")) in wanted]
            elif key == "album_id":
                wanted = set(_split_csv(value))
                result = [row for row in result if str(row.get("album_id")) in wanted]
            elif key == "artist_id":
                wanted = set(_split_csv(value))
                result = [row for row in result if str(row.get("artist_id")) in wanted]
            elif key == "playlist_id":
                playlist_track_ids = set(self.playlist_tracks.get(value, []))
                result = [row for row in result if str(row.get("id")) in playlist_track_ids]
        return result

    def _filter_playlists(
        self,
        rows: list[dict[str, Any]],
        filters: list[str],
    ) -> list[dict[str, Any]]:
        result = rows
        for key, value in _iter_filters(filters):
            if key == "search":
                result = [
                    row for row in result if _search_match(value, row, ("playlist", "name", "id"))
                ]
            elif key in {"playlist_id", "id"}:
                wanted = set(_split_csv(value))
                result = [row for row in result if str(row.get("id")) in wanted]
        return result

    def _filter_genres(
        self,
        rows: list[dict[str, Any]],
        filters: list[str],
    ) -> list[dict[str, Any]]:
        result = rows
        for key, value in _iter_filters(filters):
            if key == "search":
                result = [
                    row for row in result if _search_match(value, row, ("genre", "name", "id"))
                ]
            elif key in {"genre_id", "id"}:
                wanted = set(_split_csv(value))
                result = [row for row in result if str(row.get("id")) in wanted]
        return result


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except TypeError, ValueError:
        return default


def _split_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _iter_filters(filters: list[str]) -> list[tuple[str, str]]:
    parsed: list[tuple[str, str]] = []
    for raw in filters:
        key, _, value = raw.partition(":")
        key = key.strip()
        value = value.strip()
        if not key or key == "tags":
            continue
        parsed.append((key, value))
    return parsed


def _search_match(
    query: str,
    row: dict[str, Any],
    keys: tuple[str, ...],
) -> bool:
    needle = query.casefold()
    for key in keys:
        value = row.get(key)
        if value is None:
            continue
        if needle in str(value).casefold():
            return True
    return False


def _page_result(
    loop_key: str,
    rows: list[dict[str, Any]],
    offset: int,
    limit: int,
) -> dict[str, Any]:
    start = max(0, offset)
    stop = start + max(0, limit)
    page = rows[start:stop]
    return {
        loop_key: [dict(item) for item in page],
        "count": len(rows),
    }
