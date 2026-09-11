"""Raw library browsing helpers for pylyrion."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from time import monotonic
from typing import Any, AsyncGenerator, Literal, cast

from pylyrion.session import LyrionSession, normalize_lms_text_value
from pylyrion.errors import LyrionRequestError

EntityKey = Literal["artist", "album", "track", "playlist", "genre"]


@dataclass(frozen=True, slots=True)
class LyrionEntitySpec:
    """Describe how one entity maps onto an LMS JSON-RPC command surface."""

    key: EntityKey
    command: str
    loop_key: str
    id_filter_key: str
    id_keys: tuple[str, ...]
    tags: str
    supports_batch_lookup: bool = False


@dataclass(frozen=True, slots=True)
class LyrionPage:
    """Return one raw LMS page plus pagination metadata."""

    items: list[Mapping[str, object]]
    has_more: bool


ARTIST_SPEC = LyrionEntitySpec(
    key="artist",
    command="artists",
    loop_key="artists_loop",
    id_filter_key="artist_id",
    id_keys=("id", "artist_id", "contributor_id"),
    tags="tags:4abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ",
)

ALBUM_SPEC = LyrionEntitySpec(
    key="album",
    command="albums",
    loop_key="albums_loop",
    id_filter_key="album_id",
    id_keys=("id", "album_id"),
    tags="tags:abcdefghijklmnopqrstuvwxyz",
    supports_batch_lookup=True,
)

TRACK_SPEC = LyrionEntitySpec(
    key="track",
    command="titles",
    loop_key="titles_loop",
    id_filter_key="track_id",
    id_keys=("id", "track_id"),
    tags="tags:abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ",
    supports_batch_lookup=True,
)

DEFAULT_BROWSE_PAGE_SIZE = 250
BATCH_LOOKUP_SIZE = 25
ARTWORK_WORKER_COUNT = 6


def normalize_row(raw_item: Mapping[str, object]) -> dict[str, str]:
    """Normalize one LMS payload row into a string-only mapping."""
    normalized_item: dict[str, str] = {}
    for key, value in raw_item.items():
        if normalized_value := normalize_lms_text_value(value):
            normalized_item[key] = normalized_value
    return normalized_item


async def get_entity_page(
    session: LyrionSession,
    spec: LyrionEntitySpec,
    offset: int,
    limit: int,
    filter_value: str | None = None,
) -> LyrionPage:
    """Return one paged entity response rowset with has-more metadata."""
    command: list[Any] = [spec.command, offset, limit, spec.tags]
    if filter_value:
        command.append(filter_value)
    result = await session.request("", command)
    raw_items = cast(
        "list[Mapping[str, object]]",
        result.get(spec.loop_key, []),
    )
    count = normalize_lms_text_value(result.get("count"))
    if count is not None and count.isdigit() and int(count) > 0:
        has_more = offset + len(raw_items) < int(count)
    else:
        has_more = len(raw_items) >= limit
    return LyrionPage(items=raw_items, has_more=has_more)


async def get_simple_browse_page(
    session: LyrionSession,
    command: str,
    loop_key: str,
    offset: int,
    limit: int,
) -> LyrionPage:
    """Return one paged simple browse response (playlists/genres)."""
    result = await session.request("", [command, offset, limit])
    raw_items = cast(
        "list[Mapping[str, object]]",
        result.get(loop_key, []),
    )
    count = normalize_lms_text_value(result.get("count"))
    if count is not None and count.isdigit() and int(count) > 0:
        has_more = offset + len(raw_items) < int(count)
    else:
        has_more = len(raw_items) >= limit
    return LyrionPage(items=raw_items, has_more=has_more)


async def get_entity_ids(
    session: LyrionSession,
    spec: LyrionEntitySpec,
    page_size: int,
    filter_value: str | None = None,
) -> list[str]:
    """Return ids for one LMS entity using paged browse requests."""
    ids: list[str] = []
    seen: set[str] = set()
    expected_total: int | None = None
    offset = 0
    page_index = 0
    while True:
        page_index += 1
        command: list[Any] = [spec.command, offset, page_size, spec.tags]
        if filter_value:
            command.append(filter_value)
        request_started = monotonic()
        result = await session.request("", command)
        request_elapsed_ms = (monotonic() - request_started) * 1000
        if expected_total is None:
            expected_total = _extract_browse_total_count(result)
        raw_items = cast(
            "list[Mapping[str, object]]",
            result.get(spec.loop_key, []),
        )
        if not raw_items:
            break
        ids_before_page = len(ids)
        for raw_item in raw_items:
            normalized_item = normalize_row(raw_item)
            if (item_id := _extract_item_id(normalized_item, spec.id_keys)) is None:
                continue
            if item_id in seen:
                continue
            seen.add(item_id)
            ids.append(item_id)
        if len(ids) == ids_before_page:
            break
        if expected_total:
            _ = request_elapsed_ms
        if len(raw_items) < page_size:
            break
        offset += page_size
    return ids


async def get_entity_data(
    session: LyrionSession,
    spec: LyrionEntitySpec,
    item_id: str,
) -> Mapping[str, object]:
    """Fetch one raw entity payload by id using the shared lookup flow."""
    async for raw_item in iter_raw_entities(session, spec, [item_id]):
        return raw_item
    raise LyrionRequestError(f"Lyrion {spec.key.title()} not found: {item_id}")


async def get_playlist_tracks_page(
    session: LyrionSession,
    playlist_id: str,
    offset: int = 0,
    limit: int = DEFAULT_BROWSE_PAGE_SIZE,
) -> LyrionPage:
    """Return one paged playlist track response using LMS playlists/tracks."""
    result = await session.request(
        "",
        [
            "playlists",
            "tracks",
            offset,
            limit,
            f"playlist_id:{playlist_id}",
            "tags:abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ",
        ],
    )
    raw_items = cast(
        "list[Mapping[str, object]]",
        result.get("playlisttracks_loop", []),
    )
    tracks_page = LyrionPage(items=raw_items, has_more=False)
    expected_total = _extract_browse_total_count(result)
    if expected_total is not None:
        tracks_page = LyrionPage(
            items=raw_items,
            has_more=offset + len(raw_items) < expected_total,
        )
    else:
        tracks_page = LyrionPage(items=raw_items, has_more=len(raw_items) >= limit)
    return tracks_page


async def iter_raw_entities(
    session: LyrionSession,
    spec: LyrionEntitySpec,
    item_ids: list[str],
) -> AsyncGenerator[Mapping[str, str]]:
    """Yield raw LMS entities in request order, with optional batch fallback."""
    total_items = len(item_ids)
    if len(item_ids) == 1:
        item_id = item_ids[0]
        result = await session.request("", _create_lookup_command(spec, item_ids))
        for raw_item in _split_lookup_reply(spec, result, item_ids):
            yield raw_item
        return

    use_batch = spec.supports_batch_lookup
    try:
        if use_batch:
            processed_items = 0
            for chunk in _chunked(item_ids, BATCH_LOOKUP_SIZE):
                chunk_start = processed_items + 1
                chunk_end = processed_items + len(chunk)
                _ = chunk_start, chunk_end, total_items
                result = await session.request("", _create_lookup_command(spec, chunk))
                for raw_item in _split_lookup_reply(spec, result, chunk):
                    yield raw_item
                processed_items += len(chunk)
            return
    except ValueError:
        fallback_start = 0
    except LyrionRequestError:
        fallback_start = 0 if not use_batch else processed_items
    else:
        fallback_start = 0

    for item_id in item_ids[fallback_start:]:
        result = await session.request("", _create_lookup_command(spec, [item_id]))
        for raw_item in _split_lookup_reply(spec, result, [item_id]):
            yield raw_item


async def search_entities(
    session: LyrionSession,
    spec: LyrionEntitySpec,
    query: str,
    limit: int,
) -> list[Mapping[str, object]]:
    """Search LMS for one entity type and return raw rows."""
    result = await session.request(
        "",
        [spec.command, 0, limit, spec.tags, f"search:{query}"],
    )
    return [
        normalize_row(row)
        for row in cast("list[Mapping[str, object]]", result.get(spec.loop_key, []))
    ]


def _extract_browse_total_count(result: Mapping[str, object]) -> int | None:
    """Extract total item count from an LMS browse response when available."""
    count = normalize_lms_text_value(result.get("count"))
    if count is None or not count.isdigit():
        return None
    parsed = int(count)
    return parsed if parsed > 0 else None


def _extract_item_id(raw_item: Mapping[str, str], id_keys: tuple[str, ...]) -> str | None:
    """Extract the first matching non-empty id from one normalized row."""
    for key in id_keys:
        if item_id := normalize_lms_text_value(raw_item.get(key)):
            return item_id
    return None


def _create_lookup_command(spec: LyrionEntitySpec, item_ids: list[str]) -> list[Any]:
    """Create one LMS command that looks up one or many ids."""
    if not item_ids:
        raise ValueError(f"{spec.key} lookup requires at least one id")
    return [
        spec.command,
        0,
        len(item_ids),
        spec.tags,
        f"{spec.id_filter_key}:{','.join(item_ids)}",
    ]


def _split_lookup_reply(
    spec: LyrionEntitySpec,
    result: Mapping[str, object],
    expected_ids: list[str],
) -> list[Mapping[str, str]]:
    """Map LMS lookup replies back to request order and validate misses."""
    raw_items = cast("list[Mapping[str, object]]", result.get(spec.loop_key, []))
    items_by_id: dict[str, Mapping[str, str]] = {}
    for raw_item in raw_items:
        normalized_item = normalize_row(raw_item)
        item_id = _extract_item_id(normalized_item, spec.id_keys)
        if item_id is None or item_id in items_by_id:
            continue
        items_by_id[item_id] = normalized_item

    missing_ids = [item_id for item_id in expected_ids if item_id not in items_by_id]
    if missing_ids:
        raise ValueError(
            f"Lyrion {spec.key} lookup returned incomplete data (missing {len(missing_ids)} ids)"
        )

    return [items_by_id[item_id] for item_id in expected_ids]


def _chunked(item_ids: list[str], chunk_size: int) -> list[list[str]]:
    """Yield stable chunks from a list of ids."""
    return [
        item_ids[offset : offset + chunk_size]
        for offset in range(0, len(item_ids), chunk_size)
    ]


class LyrionLibraryClient:
    """Expose raw Lyrion library browse operations."""

    def __init__(self, session: LyrionSession) -> None:
        self._session = session

    async def get_artists_page(
        self,
        offset: int = 0,
        limit: int = DEFAULT_BROWSE_PAGE_SIZE,
    ) -> LyrionPage:
        """Return one artist page from Lyrion."""
        return await get_entity_page(self._session, ARTIST_SPEC, offset, limit)

    async def get_albums_page(
        self,
        offset: int = 0,
        limit: int = DEFAULT_BROWSE_PAGE_SIZE,
        filter_value: str | None = None,
    ) -> LyrionPage:
        """Return one album page from Lyrion."""
        return await get_entity_page(
            self._session,
            ALBUM_SPEC,
            offset,
            limit,
            filter_value=filter_value,
        )

    async def get_tracks_page(
        self,
        offset: int = 0,
        limit: int = DEFAULT_BROWSE_PAGE_SIZE,
        filter_value: str | None = None,
    ) -> LyrionPage:
        """Return one track page from Lyrion."""
        return await get_entity_page(
            self._session,
            TRACK_SPEC,
            offset,
            limit,
            filter_value=filter_value,
        )

    async def get_playlists_page(
        self,
        offset: int = 0,
        limit: int = DEFAULT_BROWSE_PAGE_SIZE,
    ) -> LyrionPage:
        """Return one playlist page from Lyrion."""
        return await get_simple_browse_page(
            self._session,
            "playlists",
            "playlists_loop",
            offset,
            limit,
        )

    async def get_genres_page(
        self,
        offset: int = 0,
        limit: int = DEFAULT_BROWSE_PAGE_SIZE,
    ) -> LyrionPage:
        """Return one genre page from Lyrion."""
        return await get_simple_browse_page(
            self._session,
            "genres",
            "genres_loop",
            offset,
            limit,
        )

    async def get_artist_ids(self, filter_value: str | None = None) -> list[str]:
        """Return all artist ids from Lyrion."""
        return await get_entity_ids(self._session, ARTIST_SPEC, DEFAULT_BROWSE_PAGE_SIZE, filter_value)

    async def get_album_ids(self, filter_value: str | None = None) -> list[str]:
        """Return all album ids from Lyrion."""
        return await get_entity_ids(self._session, ALBUM_SPEC, DEFAULT_BROWSE_PAGE_SIZE, filter_value)

    async def get_track_ids(self, filter_value: str | None = None) -> list[str]:
        """Return all track ids from Lyrion."""
        return await get_entity_ids(self._session, TRACK_SPEC, DEFAULT_BROWSE_PAGE_SIZE, filter_value)

    async def get_playlist_tracks_page(
        self,
        playlist_id: str,
        offset: int = 0,
        limit: int = DEFAULT_BROWSE_PAGE_SIZE,
    ) -> LyrionPage:
        """Return one paged playlist track response using LMS playlists/tracks."""
        return await get_playlist_tracks_page(self._session, playlist_id, offset, limit)

    async def iter_raw_entities(self, spec: LyrionEntitySpec, item_ids: list[str]) -> AsyncGenerator[Mapping[str, str]]:
        """Yield raw LMS entities in request order."""
        async for raw_item in iter_raw_entities(self._session, spec, item_ids):
            yield raw_item

    async def get_entity_data(self, spec: LyrionEntitySpec, item_id: str) -> Mapping[str, object]:
        """Fetch one raw entity payload by id."""
        return await get_entity_data(self._session, spec, item_id)

    async def search_entities(self, spec: LyrionEntitySpec, query: str, limit: int) -> list[Mapping[str, object]]:
        """Search one LMS entity type and return raw rows."""
        return await search_entities(self._session, spec, query, limit)


__all__ = [
    "ALBUM_SPEC",
    "ARTWORK_WORKER_COUNT",
    "ARTIST_SPEC",
    "BATCH_LOOKUP_SIZE",
    "DEFAULT_BROWSE_PAGE_SIZE",
    "TRACK_SPEC",
    "LyrionEntitySpec",
    "LyrionLibraryClient",
    "LyrionPage",
    "get_entity_data",
    "get_entity_ids",
    "get_playlist_tracks_page",
    "iter_raw_entities",
    "search_entities",
    "get_entity_page",
    "get_simple_browse_page",
    "normalize_row",
]
