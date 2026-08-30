"""Lyrion client helpers for RPC, paging and raw item retrieval."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Iterable
from dataclasses import dataclass
from time import monotonic
from typing import TYPE_CHECKING, Any, Literal, cast

from aiohttp import ClientError, ClientTimeout
from music_assistant_models.errors import MediaNotFoundError, ProviderUnavailableError

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.controllers.tasks import (
    get_current_task,
    update_current_task_progress,
    update_current_task_progress_text,
)

from . import parsers
from .constants import (
    ALBUM_TAGS,
    ARTIST_TAGS,
    ARTWORK_WORKER_COUNT,
    BATCH_LOOKUP_SIZE,
    BROWSE_PAGE_SIZE,
    CONF_LMS_HOST,
    CONF_LMS_PORT,
    DEFAULT_LMS_PORT,
    RPC_TIMEOUT,
    TRACK_TAGS,
)

if TYPE_CHECKING:
    from music_assistant_models.media_items import Album, Artist, Track

    from music_assistant.providers.lyrion_music.provider import LyrionMusicProvider


EntityKey = Literal["artist", "album", "track"]


@dataclass(frozen=True, slots=True)
class LmsEntitySpec:
    """Describe how one entity maps onto an LMS JSON-RPC command surface."""

    key: EntityKey
    command: str
    loop_key: str
    id_filter_key: str
    id_keys: tuple[str, ...]
    tags: str
    supports_batch_lookup: bool = False


ARTIST_SPEC = LmsEntitySpec(
    key="artist",
    command="artists",
    loop_key="artists_loop",
    id_filter_key="artist_id",
    id_keys=("id", "artist_id", "contributor_id"),
    tags=ARTIST_TAGS,
    supports_batch_lookup=False,
)

ALBUM_SPEC = LmsEntitySpec(
    key="album",
    command="albums",
    loop_key="albums_loop",
    id_filter_key="album_id",
    id_keys=("id", "album_id"),
    tags=ALBUM_TAGS,
    supports_batch_lookup=True,
)

TRACK_SPEC = LmsEntitySpec(
    key="track",
    command="titles",
    loop_key="titles_loop",
    id_filter_key="track_id",
    id_keys=("id", "track_id"),
    tags=TRACK_TAGS,
    supports_batch_lookup=True,
)


async def get_all_artists(provider: LyrionMusicProvider) -> list[Artist]:
    """Return all artists from LMS using paginated RPC calls."""
    return [artist async for artist in iter_library_artists(provider)]


async def get_all_albums(
    provider: LyrionMusicProvider, filter_value: str | None = None
) -> list[Album]:
    """Return all albums from LMS using paginated RPC calls."""
    return [album async for album in iter_library_albums(provider, filter_value)]


async def get_all_tracks(provider: LyrionMusicProvider) -> list[Track]:
    """Return all tracks from LMS using paginated RPC calls."""
    return [track async for track in iter_library_tracks(provider)]


async def get_artists_page(
    provider: LyrionMusicProvider,
    offset: int = 0,
    limit: int = BROWSE_PAGE_SIZE,
) -> tuple[list[Artist], bool]:
    """Return one paginated artist page from LMS plus a has-more flag."""
    raw_artists, has_more = await _get_entity_page(
        provider,
        spec=ARTIST_SPEC,
        offset=offset,
        limit=limit,
    )
    artists: list[Artist] = []
    for raw_artist in raw_artists:
        try:
            artists.append(parsers.parse_artist(provider, raw_artist))
        except MediaNotFoundError:
            continue
    return artists, has_more


async def get_albums_page(
    provider: LyrionMusicProvider,
    filter_value: str | None = None,
    offset: int = 0,
    limit: int = BROWSE_PAGE_SIZE,
) -> tuple[list[Album], bool]:
    """Return one paginated album page from LMS plus a has-more flag."""
    raw_albums, has_more = await _get_entity_page(
        provider,
        spec=ALBUM_SPEC,
        offset=offset,
        limit=limit,
        filter_value=filter_value,
    )
    albums: list[Album] = []
    for raw_album in raw_albums:
        try:
            albums.append(parsers.parse_album(provider, raw_album))
        except MediaNotFoundError:
            continue
    return albums, has_more


async def get_tracks_page(
    provider: LyrionMusicProvider,
    filter_value: str | None = None,
    offset: int = 0,
    limit: int = BROWSE_PAGE_SIZE,
) -> tuple[list[Track], bool]:
    """Return one paginated track page from LMS plus a has-more flag."""
    raw_tracks, has_more = await _get_entity_page(
        provider,
        spec=TRACK_SPEC,
        offset=offset,
        limit=limit,
        filter_value=filter_value,
    )
    tracks: list[Track] = []
    for raw_track in raw_tracks:
        try:
            tracks.append(parsers.parse_track(provider, raw_track))
        except MediaNotFoundError:
            continue
    return tracks, has_more


async def get_playlists_page(
    provider: LyrionMusicProvider,
    offset: int = 0,
    limit: int = BROWSE_PAGE_SIZE,
) -> tuple[list[dict[str, str]], bool]:
    """Return one paginated playlist page from LMS plus a has-more flag."""
    raw_playlists, has_more = await _get_simple_browse_page(
        provider,
        command="playlists",
        loop_key="playlists_loop",
        offset=offset,
        limit=limit,
    )
    playlists: list[dict[str, str]] = []
    for raw_playlist in raw_playlists:
        playlist_id = parsers.extract_item_id(raw_playlist, id_keys=("id", "playlist_id"))
        if playlist_id is None:
            continue
        playlist_name = str(raw_playlist.get("playlist") or raw_playlist.get("name") or playlist_id)
        playlists.append({"id": playlist_id, "name": playlist_name})
    return playlists, has_more


async def get_genres_page(
    provider: LyrionMusicProvider,
    offset: int = 0,
    limit: int = BROWSE_PAGE_SIZE,
) -> tuple[list[dict[str, str]], bool]:
    """Return one paginated genre page from LMS plus a has-more flag."""
    raw_genres, has_more = await _get_simple_browse_page(
        provider,
        command="genres",
        loop_key="genres_loop",
        offset=offset,
        limit=limit,
    )
    genres: list[dict[str, str]] = []
    for raw_genre in raw_genres:
        genre_id = parsers.extract_item_id(raw_genre, id_keys=("id", "genre_id"))
        if genre_id is None:
            continue
        genre_name = str(raw_genre.get("genre") or raw_genre.get("name") or genre_id)
        genres.append({"id": genre_id, "name": genre_name})
    return genres, has_more


async def iter_library_artists(provider: LyrionMusicProvider) -> AsyncGenerator[Artist]:
    """Yield artists from LMS incrementally for library sync."""
    async for artist in _iter_entities(
        provider,
        spec=ARTIST_SPEC,
        item_ids=await get_artist_ids(provider),
    ):
        yield cast("Artist", artist)


async def iter_library_albums(
    provider: LyrionMusicProvider,
    filter_value: str | None = None,
) -> AsyncGenerator[Album]:
    """Yield albums from LMS incrementally for library sync."""
    async for album in _iter_entities(
        provider,
        spec=ALBUM_SPEC,
        item_ids=await get_album_ids(provider, filter_value),
    ):
        yield cast("Album", album)


async def iter_library_tracks(
    provider: LyrionMusicProvider,
    filter_value: str | None = None,
) -> AsyncGenerator[Track]:
    """Yield tracks from LMS incrementally for library sync."""
    async for track in _iter_entities(
        provider,
        spec=TRACK_SPEC,
        item_ids=await get_track_ids(provider, filter_value=filter_value),
    ):
        yield cast("Track", track)


async def get_all_playlists(provider: LyrionMusicProvider) -> list[dict[str, str]]:
    """Return all playlists from LMS as id/name pairs."""
    playlists: list[dict[str, str]] = []
    offset = 0
    while True:
        page, has_more = await get_playlists_page(provider, offset=offset)
        if not page:
            break
        playlists.extend(page)
        if not has_more:
            break
        offset += BROWSE_PAGE_SIZE
    return playlists


async def get_playlist_tracks(provider: LyrionMusicProvider, playlist_id: str) -> list[Track]:
    """Return all tracks for a playlist id."""
    return [
        track
        async for track in iter_library_tracks(provider, filter_value=f"playlist_id:{playlist_id}")
    ]


async def get_album_tracks(provider: LyrionMusicProvider, album_id: str) -> list[Track]:
    """Return all tracks for the given album id."""
    tracks = [
        track async for track in iter_library_tracks(provider, filter_value=f"album_id:{album_id}")
    ]
    if any(track.track_number for track in tracks):
        tracks.sort(key=lambda item: (item.disc_number or 0, item.track_number or 0))
    return tracks


async def get_artist_ids(provider: LyrionMusicProvider) -> list[str]:
    """Return all artist ids from LMS."""
    return await _get_browse_ids(provider, ARTIST_SPEC)


async def get_album_ids(
    provider: LyrionMusicProvider, filter_value: str | None = None
) -> list[str]:
    """Return all album ids from LMS."""
    return await _get_browse_ids(provider, ALBUM_SPEC, filter_value)


async def get_track_ids(
    provider: LyrionMusicProvider, filter_value: str | None = None
) -> list[str]:
    """Return all track ids from LMS."""
    return await _get_browse_ids(provider, TRACK_SPEC, filter_value)


async def _get_browse_ids(
    provider: LyrionMusicProvider,
    spec: LmsEntitySpec,
    filter_value: str | None = None,
) -> list[str]:
    """Return ids for one LMS entity using paged browse requests."""
    provider.logger.debug(
        "Lyrion %s id discovery -> command %s (filter: %s)",
        spec.key,
        spec.command,
        filter_value or "none",
    )
    update_current_task_progress_text(f"Fetching number of {spec.key}s from Lyrion...")
    ids: list[str] = []
    seen: set[str] = set()
    expected_total: int | None = None
    offset = 0
    page_index = 0
    while True:
        page_index += 1
        command: list[Any] = [spec.command, offset, BROWSE_PAGE_SIZE, spec.tags]
        if filter_value:
            command.append(filter_value)
        provider.logger.debug(
            "Lyrion %s id discovery request -> page %s (offset: %s, limit: %s)",
            spec.key,
            page_index,
            offset,
            BROWSE_PAGE_SIZE,
        )
        request_started = monotonic()
        result = await rpc_request(provider, player_id="", command=command)
        request_elapsed_ms = (monotonic() - request_started) * 1000
        if expected_total is None:
            expected_total = _extract_browse_total_count(result)
        raw_items = cast("list[dict[str, Any]]", result.get(spec.loop_key, []))
        provider.logger.debug(
            "Lyrion %s id discovery response <- page %s (%s rows, rpc: %.1f ms)",
            spec.key,
            page_index,
            len(raw_items),
            request_elapsed_ms,
        )
        if not raw_items:
            break
        for raw_item in raw_items:
            if (item_id := parsers.extract_item_id(raw_item, id_keys=spec.id_keys)) is None:
                continue
            if item_id in seen:
                continue
            seen.add(item_id)
            ids.append(item_id)
        if expected_total:
            progress_text = f"Getting {spec.key} ids from Lyrion: {len(ids)}/{expected_total}"
            update_current_task_progress_text(progress_text)
            _update_weighted_sync_progress(
                phase="id_discovery",
                current=len(ids),
                total=expected_total,
                text=progress_text,
            )
        else:
            update_current_task_progress_text(
                f"Getting {spec.key} ids from Lyrion: {len(ids)} found so far"
            )
        if len(raw_items) < BROWSE_PAGE_SIZE:
            break
        offset += BROWSE_PAGE_SIZE
    provider.logger.debug(
        "Lyrion %s id discovery <- %s ids",
        spec.key,
        len(ids),
    )
    _update_weighted_sync_progress(
        phase="id_discovery",
        current=1,
        total=1,
        text=f"Getting {spec.key} ids from Lyrion: done ({len(ids)})",
    )
    return ids


def _extract_browse_total_count(result: dict[str, Any]) -> int | None:
    """Extract total item count from an LMS browse response when available."""
    count = result.get("count")
    if count is None:
        return None
    parsed = parsers.parse_int(count, default=0)
    return parsed if parsed > 0 else None


async def get_all_genres(provider: LyrionMusicProvider) -> list[dict[str, str]]:
    """Return all genres from LMS as id/name pairs."""
    genres: list[dict[str, str]] = []
    offset = 0
    while True:
        page, has_more = await get_genres_page(provider, offset=offset)
        if not page:
            break
        genres.extend(page)
        if not has_more:
            break
        offset += BROWSE_PAGE_SIZE
    return genres


async def _get_entity_page(
    provider: LyrionMusicProvider,
    spec: LmsEntitySpec,
    offset: int,
    limit: int,
    filter_value: str | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Return one paged entity response rowset with has-more metadata."""
    command: list[Any] = [spec.command, offset, limit, spec.tags]
    if filter_value:
        command.append(filter_value)
    result = await rpc_request(
        provider,
        player_id="",
        command=command,
    )
    raw_items = cast("list[dict[str, Any]]", result.get(spec.loop_key, []))
    expected_total = _extract_browse_total_count(result)
    if expected_total is not None:
        has_more = offset + len(raw_items) < expected_total
    else:
        has_more = len(raw_items) >= limit
    return raw_items, has_more


async def _get_simple_browse_page(
    provider: LyrionMusicProvider,
    command: str,
    loop_key: str,
    offset: int,
    limit: int,
) -> tuple[list[dict[str, Any]], bool]:
    """Return one paged simple browse response (playlists/genres)."""
    result = await rpc_request(
        provider,
        player_id="",
        command=[command, offset, limit],
    )
    raw_items = cast("list[dict[str, Any]]", result.get(loop_key, []))
    expected_total = _extract_browse_total_count(result)
    if expected_total is not None:
        has_more = offset + len(raw_items) < expected_total
    else:
        has_more = len(raw_items) >= limit
    return raw_items, has_more


async def search_artists(provider: LyrionMusicProvider, query: str, limit: int) -> list[Artist]:
    """Search artists in LMS."""
    result = await rpc_request(
        provider,
        player_id="",
        command=["artists", 0, limit, ARTIST_TAGS, f"search:{query}"],
    )
    artists: list[Artist] = []
    for raw_artist in cast("list[dict[str, Any]]", result.get("artists_loop", [])):
        if parsers.extract_item_id(raw_artist) is None:
            continue
        artists.append(parsers.parse_artist(provider, raw_artist))
    return artists


async def search_albums(provider: LyrionMusicProvider, query: str, limit: int) -> list[Album]:
    """Search albums in LMS."""
    result = await rpc_request(
        provider,
        player_id="",
        command=["albums", 0, limit, ALBUM_TAGS, f"search:{query}"],
    )
    albums: list[Album] = []
    for raw_album in cast("list[dict[str, Any]]", result.get("albums_loop", [])):
        if parsers.extract_item_id(raw_album) is None:
            continue
        albums.append(parsers.parse_album(provider, raw_album))
    return albums


async def search_tracks(provider: LyrionMusicProvider, query: str, limit: int) -> list[Track]:
    """Search tracks in LMS."""
    result = await rpc_request(
        provider,
        player_id="",
        command=["titles", 0, limit, TRACK_TAGS, f"search:{query}"],
    )
    tracks: list[Track] = []
    for raw_track in cast("list[dict[str, Any]]", result.get("titles_loop", [])):
        if parsers.extract_item_id(raw_track) is None:
            continue
        tracks.append(parsers.parse_track(provider, raw_track))
    return tracks


async def get_artist_data(provider: LyrionMusicProvider, artist_id: str) -> dict[str, Any]:
    """Get artist payload from LMS."""
    return await _get_entity_data(provider, ARTIST_SPEC, artist_id)


async def get_album_data(provider: LyrionMusicProvider, album_id: str) -> dict[str, Any]:
    """Get album payload from LMS."""
    return await _get_entity_data(provider, ALBUM_SPEC, album_id)


async def get_track_data(provider: LyrionMusicProvider, track_id: str) -> dict[str, Any]:
    """Get track payload from LMS."""
    return await _get_entity_data(provider, TRACK_SPEC, track_id)


async def _get_entity_data(
    provider: LyrionMusicProvider,
    spec: LmsEntitySpec,
    item_id: str,
) -> dict[str, Any]:
    """Fetch one raw entity payload by id using the shared lookup flow."""
    async for raw_item in _iter_raw_entities(provider, spec, [item_id]):
        return raw_item
    raise MediaNotFoundError(f"{spec.key.title()} not found: {item_id}")


async def _fetch_worker(
    provider: LyrionMusicProvider,
    spec: LmsEntitySpec,
    ordered_ids: list[str],
    raw_queue: asyncio.Queue[dict[str, Any] | object],
    stop_sentinel: object,
    error_box: list[Exception],
) -> None:
    try:
        async for raw_item in _iter_raw_entities(provider, spec, ordered_ids):
            await raw_queue.put(raw_item)
    except Exception as err:
        error_box.append(err)
    finally:
        await raw_queue.put(stop_sentinel)


async def _decode_worker(
    provider: LyrionMusicProvider,
    spec: LmsEntitySpec,
    raw_queue: asyncio.Queue[dict[str, Any] | object],
    decoded_queue: asyncio.Queue[Artist | Album | Track | object],
    stop_sentinel: object,
    artwork_worker_count: int,
    error_box: list[Exception],
) -> None:
    try:
        while True:
            payload = await raw_queue.get()
            if payload is stop_sentinel:
                break
            decoded = await _decode_entity(provider, spec, cast("dict[str, Any]", payload))
            await decoded_queue.put(decoded)
    except Exception as err:
        error_box.append(err)
    finally:
        for _ in range(artwork_worker_count):
            await decoded_queue.put(stop_sentinel)


async def _artwork_worker(
    provider: LyrionMusicProvider,
    spec: LmsEntitySpec,
    decoded_queue: asyncio.Queue[Artist | Album | Track | object],
    output_queue: asyncio.Queue[Artist | Album | Track | object],
    stop_sentinel: object,
    error_box: list[Exception],
) -> None:
    try:
        del provider, spec
        while True:
            decoded = await decoded_queue.get()
            if decoded is stop_sentinel:
                break
            await output_queue.put(cast("Artist | Album | Track", decoded))
    except Exception as err:
        error_box.append(err)
    finally:
        await output_queue.put(stop_sentinel)


async def _iter_entities(
    provider: LyrionMusicProvider,
    spec: LmsEntitySpec,
    item_ids: list[str],
) -> AsyncGenerator[Artist | Album | Track]:
    """Yield decoded entities via a bounded fetch -> decode -> pass-through pipeline."""
    ordered_ids = _normalize_lookup_ids(provider, spec, item_ids)
    if not ordered_ids:
        return

    # For single-item lookups we avoid pipeline overhead and return immediately.
    if len(ordered_ids) == 1:
        raw_item = await _get_entity_data(provider, spec, ordered_ids[0])
        entity = await _decode_entity(provider, spec, raw_item)
        yield entity
        return

    raw_queue: asyncio.Queue[dict[str, Any] | object] = asyncio.Queue(maxsize=2)
    decoded_queue: asyncio.Queue[Artist | Album | Track | object] = asyncio.Queue(maxsize=2)
    output_queue: asyncio.Queue[Artist | Album | Track | object] = asyncio.Queue(maxsize=2)
    stop_sentinel = object()
    error_box: list[Exception] = []
    artwork_worker_count = max(1, ARTWORK_WORKER_COUNT)

    provider.logger.debug(
        "Lyrion %s decode pipeline concurrency -> %s workers",
        spec.key,
        artwork_worker_count,
    )

    tasks = [
        asyncio.create_task(
            _fetch_worker(provider, spec, ordered_ids, raw_queue, stop_sentinel, error_box)
        ),
        asyncio.create_task(
            _decode_worker(
                provider,
                spec,
                raw_queue,
                decoded_queue,
                stop_sentinel,
                artwork_worker_count,
                error_box,
            )
        ),
    ]
    for _ in range(artwork_worker_count):
        tasks.append(
            asyncio.create_task(
                _artwork_worker(
                    provider,
                    spec,
                    decoded_queue,
                    output_queue,
                    stop_sentinel,
                    error_box,
                )
            )
        )
    try:
        completed_workers = 0
        while True:
            item = await output_queue.get()
            if item is stop_sentinel:
                completed_workers += 1
                if completed_workers >= artwork_worker_count:
                    break
                continue
            if error_box:
                raise error_box[0]
            yield cast("Artist | Album | Track", item)

        if error_box:
            raise error_box[0]
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _decode_entity(
    provider: LyrionMusicProvider,
    spec: LmsEntitySpec,
    raw_item: dict[str, Any],
) -> Artist | Album | Track:
    """Decode one raw LMS item into its MA model."""
    if spec.key == "artist":
        return parsers.parse_artist(provider, raw_item)
    if spec.key == "album":
        return parsers.parse_album(provider, raw_item)
    return parsers.parse_track(provider, raw_item)


async def _iter_raw_entities(
    provider: LyrionMusicProvider,
    spec: LmsEntitySpec,
    item_ids: list[str],
) -> AsyncGenerator[dict[str, Any]]:
    """Yield raw LMS entities in request order, with optional batch fallback."""
    total_items = len(item_ids)
    if len(item_ids) == 1:
        item_id = item_ids[0]
        _log_lookup_request(provider, spec, item_id, item_index=1, total_items=total_items)
        request_started = monotonic()
        result = await rpc_request(
            provider, player_id="", command=_create_lookup_command(spec, item_ids)
        )
        request_elapsed_ms = (monotonic() - request_started) * 1000
        for raw_item in _split_lookup_reply(spec, result, item_ids):
            if _should_report_lookup_progress():
                fetch_text = f"Fetching {spec.key}s from Lyrion: 1/{total_items}"
                _update_weighted_sync_progress(
                    phase="entity_fetch",
                    current=1,
                    total=total_items,
                    text=fetch_text,
                )
            _log_lookup_response(
                provider,
                spec,
                raw_item,
                requested_id=item_id,
                request_elapsed_ms=request_elapsed_ms,
            )
            yield raw_item
        return

    use_batch = _is_batch_lookup_enabled(provider, spec)
    if use_batch:
        provider.logger.debug(
            "Lyrion %s lookup: %s ids in batches of %s",
            spec.key,
            len(item_ids),
            BATCH_LOOKUP_SIZE,
        )
    else:
        provider.logger.debug(
            "Lyrion %s lookup: %s ids using single-item requests",
            spec.key,
            len(item_ids),
        )

    try:
        if use_batch:
            processed_items = 0
            for chunk in _chunked(item_ids, BATCH_LOOKUP_SIZE):
                chunk_start = processed_items + 1
                chunk_end = processed_items + len(chunk)
                provider.logger.debug(
                    "Lyrion %s lookup request -> batch %s-%s/%s (%s ids)",
                    spec.key,
                    chunk_start,
                    chunk_end,
                    total_items,
                    len(chunk),
                )
                for index_offset, item_id in enumerate(chunk, start=chunk_start):
                    _log_lookup_request(
                        provider,
                        spec,
                        item_id,
                        item_index=index_offset,
                        total_items=total_items,
                    )
                request_started = monotonic()
                result = await rpc_request(
                    provider,
                    player_id="",
                    command=_create_lookup_command(spec, chunk),
                )
                request_elapsed_ms = (monotonic() - request_started) * 1000
                for index_offset, raw_item in enumerate(
                    _split_lookup_reply(spec, result, chunk),
                    start=chunk_start,
                ):
                    if _should_report_lookup_progress():
                        fetch_text = (
                            f"Fetching {spec.key}s from Lyrion: {index_offset}/{total_items}"
                        )
                        _update_weighted_sync_progress(
                            phase="entity_fetch",
                            current=index_offset,
                            total=total_items,
                            text=fetch_text,
                        )
                    _log_lookup_response(
                        provider,
                        spec,
                        raw_item,
                        request_elapsed_ms=request_elapsed_ms,
                    )
                    yield raw_item
                processed_items += len(chunk)
            return
    except (ProviderUnavailableError, ValueError) as err:
        _disable_batch_lookup(provider, spec, err)

    for item_index, item_id in enumerate(item_ids, start=1):
        _log_lookup_request(
            provider,
            spec,
            item_id,
            item_index=item_index,
            total_items=total_items,
        )
        request_started = monotonic()
        result = await rpc_request(
            provider,
            player_id="",
            command=_create_lookup_command(spec, [item_id]),
        )
        request_elapsed_ms = (monotonic() - request_started) * 1000
        for raw_item in _split_lookup_reply(spec, result, [item_id]):
            if _should_report_lookup_progress():
                fetch_text = f"Fetching {spec.key}s from Lyrion: {item_index}/{total_items}"
                _update_weighted_sync_progress(
                    phase="entity_fetch",
                    current=item_index,
                    total=total_items,
                    text=fetch_text,
                )
            _log_lookup_response(
                provider,
                spec,
                raw_item,
                requested_id=item_id,
                request_elapsed_ms=request_elapsed_ms,
            )
            yield raw_item


def _should_report_lookup_progress() -> bool:
    """Return if generic lookup progress should be reported for current task."""
    if not (task := get_current_task()):
        return True
    return task.metadata.get("task_domain") != "lyrion_artwork_sync"


def _update_weighted_sync_progress(
    phase: Literal["id_discovery", "entity_fetch"],
    current: int,
    total: int,
    text: str | None = None,
) -> None:
    """Map sync progress across phases: ids=0..50%, fetch=50..100%."""
    if not _should_report_lookup_progress():
        return
    if total <= 0:
        return

    ratio = max(0.0, min(1.0, current / total))
    progress = int(ratio * 50) if phase == "id_discovery" else 50 + int(ratio * 50)
    update_current_task_progress(min(100, max(0, progress)), text)


def _log_lookup_request(
    provider: LyrionMusicProvider,
    spec: LmsEntitySpec,
    item_id: str,
    item_index: int,
    total_items: int,
) -> None:
    """Log one lookup request line with progress details."""
    provider.logger.debug(
        "Lyrion %s lookup request -> id %s (%s)",
        spec.key,
        item_id,
        _format_lookup_progress(item_index, total_items),
    )


def _log_lookup_response(
    provider: LyrionMusicProvider,
    spec: LmsEntitySpec,
    raw_item: dict[str, Any],
    requested_id: str | None = None,
    request_elapsed_ms: float | None = None,
) -> None:
    """Log one lookup response line with id and best-effort display name."""
    response_id = parsers.extract_item_id(raw_item, id_keys=spec.id_keys) or requested_id
    if response_id is None:
        response_id = "unknown"
    if spec.key == "artist":
        name = str(raw_item.get("artist") or raw_item.get("name") or response_id)
    elif spec.key == "album":
        name = str(raw_item.get("album") or raw_item.get("title") or response_id)
    else:
        name = str(raw_item.get("title") or raw_item.get("track") or response_id)
    timing_str = ""
    if request_elapsed_ms is not None:
        timing_str = f" (rpc: {request_elapsed_ms:.1f} ms)"

    provider.logger.debug(
        "Lyrion %s lookup response <- id %s (%s)%s",
        spec.key,
        response_id,
        name,
        timing_str,
    )


def _format_lookup_progress(item_index: int, total_items: int) -> str:
    """Format lookup progress as item counters plus percentage."""
    if total_items <= 0:
        return "progress: unknown"
    if total_items == 1:
        return "single-item lookup"
    progress_pct = (item_index / total_items) * 100
    return f"item {item_index}/{total_items}, progress: {progress_pct:.1f}%"


def _create_lookup_command(spec: LmsEntitySpec, item_ids: list[str]) -> list[Any]:
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
    spec: LmsEntitySpec,
    result: dict[str, Any],
    expected_ids: list[str],
) -> list[dict[str, Any]]:
    """Map LMS lookup replies back to request order and validate misses."""
    raw_items = cast("list[dict[str, Any]]", result.get(spec.loop_key, []))
    items_by_id: dict[str, dict[str, Any]] = {}
    for raw_item in raw_items:
        item_id = parsers.extract_item_id(raw_item, id_keys=spec.id_keys)
        if item_id is None or item_id in items_by_id:
            continue
        items_by_id[item_id] = raw_item

    missing_ids = [item_id for item_id in expected_ids if item_id not in items_by_id]
    if missing_ids:
        raise ValueError(
            f"Lyrion {spec.key} lookup returned incomplete data (missing {len(missing_ids)} ids)"
        )

    return [items_by_id[item_id] for item_id in expected_ids]


def _normalize_lookup_ids(
    provider: LyrionMusicProvider,
    spec: LmsEntitySpec,
    item_ids: list[str],
) -> list[str]:
    """Deduplicate ids while preserving order."""
    ordered_ids = list(dict.fromkeys(item_ids))
    if len(ordered_ids) != len(item_ids):
        provider.logger.debug(
            "Lyrion %s lookup deduplicated ids from %s to %s",
            spec.key,
            len(item_ids),
            len(ordered_ids),
        )
    return ordered_ids


def _get_disabled_batch_lookup_keys(provider: LyrionMusicProvider) -> set[EntityKey]:
    """Return entity keys whose batch lookup has been disabled at runtime."""
    return cast("set[EntityKey]", provider._disabled_batch_lookup_keys)


def _is_batch_lookup_enabled(provider: LyrionMusicProvider, spec: LmsEntitySpec) -> bool:
    """Return True when this entity type may use batch id lookup."""
    if not spec.supports_batch_lookup:
        return False
    return spec.key not in _get_disabled_batch_lookup_keys(provider)


def _disable_batch_lookup(
    provider: LyrionMusicProvider,
    spec: LmsEntitySpec,
    err: Exception,
) -> None:
    """Disable batch lookup for one entity type after an incompatible response."""
    disabled = _get_disabled_batch_lookup_keys(provider)
    if spec.key in disabled:
        return
    disabled.add(spec.key)
    provider.logger.warning(
        "Disabled Lyrion %s batch lookup after failure: %s. Falling back to single-item requests.",
        spec.key,
        err,
    )


def _chunked(item_ids: list[str], chunk_size: int) -> Iterable[list[str]]:
    """Yield stable chunks from a list of ids."""
    for offset in range(0, len(item_ids), chunk_size):
        yield item_ids[offset : offset + chunk_size]


async def rpc_request(
    provider: LyrionMusicProvider, player_id: str, command: list[Any]
) -> dict[str, Any]:
    """Execute one LMS JSON-RPC request."""
    host = get_configured_host(provider)
    if not host:
        raise ProviderUnavailableError("Lyrion host is not configured")
    port = get_configured_port(provider)
    payload = {
        "id": 1,
        "method": "slim.request",
        "params": [player_id, command],
    }
    url = f"http://{host}:{port}/jsonrpc.js"
    provider.logger.log(
        VERBOSE_LOG_LEVEL,
        "Lyrion RPC %s -> %s:%s",
        command[0],
        host,
        port,
    )

    try:
        async with provider.mass.http_session.post(
            url, json=payload, timeout=ClientTimeout(total=RPC_TIMEOUT)
        ) as response:
            response.raise_for_status()
            data = cast("dict[str, Any]", await response.json())
    except TimeoutError as err:
        raise ProviderUnavailableError(
            f"Lyrion server at {host}:{port} did not respond in time "
            f"({RPC_TIMEOUT}s). Verify that Lyrion is running and reachable."
        ) from err
    except ClientError as err:
        raise ProviderUnavailableError(
            f"Lyrion JSON-RPC connection to {host}:{port} failed: {err}. "
            "Verify host/port and local network connectivity."
        ) from err
    except ValueError as err:
        raise ProviderUnavailableError(
            f"Lyrion JSON-RPC returned invalid JSON for command {command[0]}"
        ) from err

    if error_payload := cast("dict[str, Any] | None", data.get("error")):
        error_code = error_payload.get("code", "unknown")
        error_message = error_payload.get("message", "unknown JSON-RPC error")
        raise ProviderUnavailableError(
            f"Lyrion JSON-RPC command {command[0]} failed with code {error_code}: {error_message}"
        )

    result = cast("dict[str, Any] | None", data.get("result"))
    if result is None:
        raise ProviderUnavailableError(
            f"Lyrion JSON-RPC response for command {command[0]} is missing result payload"
        )
    return result


def get_configured_host(provider: LyrionMusicProvider) -> str | None:
    """Return configured host from setup data with config fallback."""
    raw_host = provider.get_setup_value(CONF_LMS_HOST)
    if not isinstance(raw_host, str):
        return None
    host = raw_host.strip()
    return host or None


def get_configured_port(
    provider: LyrionMusicProvider, default: int | None = DEFAULT_LMS_PORT
) -> int | None:
    """Return configured port from setup data with config fallback."""
    raw_port = provider.get_setup_value(CONF_LMS_PORT, default)
    if raw_port is None:
        return None
    try:
        return int(cast("int | str", raw_port))
    except TypeError, ValueError:
        return default
