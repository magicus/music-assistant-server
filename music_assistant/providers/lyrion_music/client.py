"""Lyrion client helpers for RPC, paging and raw item retrieval."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

from music_assistant_models.errors import MediaNotFoundError, ProviderUnavailableError

from music_assistant.controllers.tasks import (
    get_current_task,
    update_current_task_progress,
    update_current_task_progress_text,
)
from music_assistant.providers.lyrion.client import normalize_lms_text_value
from pylyrion.errors import LyrionProtocolError, LyrionRequestError, LyrionTimeoutError
from pylyrion.library import ALBUM_SPEC as PY_ALBUM_SPEC
from pylyrion.library import ARTIST_SPEC as PY_ARTIST_SPEC
from pylyrion.library import TRACK_SPEC as PY_TRACK_SPEC
from pylyrion.library import LyrionLibraryClient
from pylyrion.library import normalize_row as _normalize_lms_row
from pylyrion.models import LyrionEndpoint
from pylyrion.session import LyrionSession

from . import parsers
from .constants import ALBUM_TAGS, ARTIST_TAGS, ARTWORK_WORKER_COUNT, BROWSE_PAGE_SIZE, TRACK_TAGS

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
    page = await _build_library_client(provider).get_entity_page(
        PY_ARTIST_SPEC,
        offset,
        limit,
    )
    artists: list[Artist] = []
    for raw_artist in page.items:
        try:
            artists.append(parsers.parse_artist(provider, _normalize_lms_row(raw_artist)))
        except MediaNotFoundError:
            continue
    return artists, page.has_more


async def get_albums_page(
    provider: LyrionMusicProvider,
    filter_value: str | None = None,
    offset: int = 0,
    limit: int = BROWSE_PAGE_SIZE,
) -> tuple[list[Album], bool]:
    """Return one paginated album page from LMS plus a has-more flag."""
    page = await _build_library_client(provider).get_entity_page(
        PY_ALBUM_SPEC,
        offset,
        limit,
        filter_value=filter_value,
    )
    albums: list[Album] = []
    for raw_album in page.items:
        try:
            albums.append(parsers.parse_album(provider, _normalize_lms_row(raw_album)))
        except MediaNotFoundError:
            continue
    return albums, page.has_more


async def get_tracks_page(
    provider: LyrionMusicProvider,
    filter_value: str | None = None,
    offset: int = 0,
    limit: int = BROWSE_PAGE_SIZE,
) -> tuple[list[Track], bool]:
    """Return one paginated track page from LMS plus a has-more flag."""
    page = await _build_library_client(provider).get_entity_page(
        PY_TRACK_SPEC,
        offset,
        limit,
        filter_value=filter_value,
    )
    tracks: list[Track] = []
    for raw_track in page.items:
        try:
            tracks.append(parsers.parse_track(provider, _normalize_lms_row(raw_track)))
        except MediaNotFoundError:
            continue
    return tracks, page.has_more


async def get_playlists_page(
    provider: LyrionMusicProvider,
    offset: int = 0,
    limit: int = BROWSE_PAGE_SIZE,
) -> tuple[list[dict[str, str]], bool]:
    """Return one paginated playlist page from LMS plus a has-more flag."""
    page = await _build_library_client(provider).get_simple_browse_page(
        "playlists",
        "playlists_loop",
        offset,
        limit,
    )
    playlists: list[dict[str, str]] = []
    for raw_playlist in page.items:
        normalized_playlist = _normalize_lms_row(raw_playlist)
        playlist_id = parsers.extract_item_id(
            normalized_playlist,
            id_keys=("id", "playlist_id"),
        )
        if playlist_id is None:
            continue
        playlist_name = (
            normalized_playlist.get("playlist") or normalized_playlist.get("name") or playlist_id
        )
        playlists.append({"id": playlist_id, "name": playlist_name})
    return playlists, page.has_more


async def get_genres_page(
    provider: LyrionMusicProvider,
    offset: int = 0,
    limit: int = BROWSE_PAGE_SIZE,
) -> tuple[list[dict[str, str]], bool]:
    """Return one paginated genre page from LMS plus a has-more flag."""
    page = await _build_library_client(provider).get_simple_browse_page(
        "genres",
        "genres_loop",
        offset,
        limit,
    )
    genres: list[dict[str, str]] = []
    for raw_genre in page.items:
        normalized_genre = _normalize_lms_row(raw_genre)
        genre_id = parsers.extract_item_id(
            normalized_genre,
            id_keys=("id", "genre_id"),
        )
        if genre_id is None:
            continue
        genre_name = normalized_genre.get("genre") or normalized_genre.get("name") or genre_id
        genres.append({"id": genre_id, "name": genre_name})
    return genres, page.has_more


async def iter_library_artists(
    provider: LyrionMusicProvider,
) -> AsyncGenerator[Artist]:
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


async def get_all_playlists(
    provider: LyrionMusicProvider,
) -> list[dict[str, str]]:
    """Return all playlists from LMS as id/name pairs."""
    return await _build_library_client(provider).get_all_playlists()


async def get_playlist_tracks_page(
    provider: LyrionMusicProvider,
    playlist_id: str,
    offset: int = 0,
    limit: int = BROWSE_PAGE_SIZE,
) -> tuple[list[Track], bool]:
    """Return one paged playlist track response using LMS playlists/tracks."""
    page = await _build_library_client(provider).get_playlist_tracks_page(
        playlist_id,
        offset=offset,
        limit=limit,
    )
    tracks: list[Track] = []
    for raw_track in page.items:
        normalized_track = _normalize_lms_row(raw_track)
        if parsers.extract_item_id(normalized_track, id_keys=("id", "track_id")) is None:
            continue
        tracks.append(parsers.parse_track(provider, normalized_track))
    return tracks, page.has_more


async def get_playlist_tracks(provider: LyrionMusicProvider, playlist_id: str) -> list[Track]:
    """Return all tracks for a playlist id."""
    raw_tracks = await _build_library_client(provider).get_playlist_tracks(playlist_id)
    tracks: list[Track] = []
    for raw_track in raw_tracks:
        normalized_track = _normalize_lms_row(raw_track)
        if parsers.extract_item_id(normalized_track, id_keys=("id", "track_id")) is None:
            continue
        tracks.append(parsers.parse_track(provider, normalized_track))
    return tracks


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
    """Return ids for one LMS entity via the shared pylyrion browse client."""
    provider.logger.debug(
        "Lyrion %s id discovery -> command %s (filter: %s)",
        spec.key,
        spec.command,
        filter_value or "none",
    )
    update_current_task_progress_text(f"Fetching number of {spec.key}s from Lyrion...")
    library = _build_library_client(provider)
    try:
        if spec.key == "artist":
            ids = await library.get_artist_ids(filter_value)
        elif spec.key == "album":
            ids = await library.get_album_ids(filter_value)
        else:
            ids = await library.get_track_ids(filter_value)
    except (LyrionProtocolError, LyrionRequestError, LyrionTimeoutError) as err:
        raise ProviderUnavailableError(str(err)) from err

    update_current_task_progress_text(f"Getting {spec.key} ids from Lyrion: done ({len(ids)})")
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


def _extract_browse_total_count(result: Mapping[str, object]) -> int | None:
    """Extract total item count from an LMS browse response when available."""
    count = normalize_lms_text_value(result.get("count"))
    if count is None:
        return None
    parsed = parsers.parse_int(count, default=0)
    return parsed if parsed > 0 else None


async def get_all_genres(
    provider: LyrionMusicProvider,
) -> list[dict[str, str]]:
    """Return all genres from LMS as id/name pairs."""
    return await _build_library_client(provider).get_all_genres()


def _build_library_session(provider: LyrionMusicProvider) -> LyrionSession:
    """Create a neutral pylyrion session from MA provider config."""
    return LyrionSession(
        http_session=provider.mass.http_session,
        endpoint=LyrionEndpoint(
            host=provider.get_configured_host() or "",
            port=provider.get_configured_port(),
        ),
    )


def _build_library_client(provider: LyrionMusicProvider) -> LyrionLibraryClient:
    """Create a pylyrion library client from the MA provider config."""
    return LyrionLibraryClient(_build_library_session(provider))


async def search_artists(provider: LyrionMusicProvider, query: str, limit: int) -> list[Artist]:
    """Search artists in LMS."""
    library = _build_library_client(provider)
    result = await library.search_entities(PY_ARTIST_SPEC, query, limit)
    artists: list[Artist] = []
    for raw_artist in result:
        normalized_artist = _normalize_lms_row(raw_artist)
        if parsers.extract_item_id(normalized_artist) is None:
            continue
        artists.append(parsers.parse_artist(provider, normalized_artist))
    return artists


async def search_albums(provider: LyrionMusicProvider, query: str, limit: int) -> list[Album]:
    """Search albums in LMS."""
    library = _build_library_client(provider)
    result = await library.search_entities(PY_ALBUM_SPEC, query, limit)
    albums: list[Album] = []
    for raw_album in result:
        normalized_album = _normalize_lms_row(raw_album)
        if parsers.extract_item_id(normalized_album) is None:
            continue
        albums.append(parsers.parse_album(provider, normalized_album))
    return albums


async def search_tracks(provider: LyrionMusicProvider, query: str, limit: int) -> list[Track]:
    """Search tracks in LMS."""
    library = _build_library_client(provider)
    result = await library.search_entities(PY_TRACK_SPEC, query, limit)
    tracks: list[Track] = []
    for raw_track in result:
        normalized_track = _normalize_lms_row(raw_track)
        if parsers.extract_item_id(normalized_track) is None:
            continue
        tracks.append(parsers.parse_track(provider, normalized_track))
    return tracks


async def get_artist_data(provider: LyrionMusicProvider, artist_id: str) -> Mapping[str, object]:
    """Get artist payload from LMS."""
    return await _build_library_client(provider).get_entity_data(PY_ARTIST_SPEC, artist_id)


async def get_album_data(provider: LyrionMusicProvider, album_id: str) -> Mapping[str, object]:
    """Get album payload from LMS."""
    return await _build_library_client(provider).get_entity_data(PY_ALBUM_SPEC, album_id)


async def get_track_data(provider: LyrionMusicProvider, track_id: str) -> Mapping[str, object]:
    """Get track payload from LMS."""
    return await _build_library_client(provider).get_entity_data(PY_TRACK_SPEC, track_id)


async def _get_entity_data(
    provider: LyrionMusicProvider,
    spec: LmsEntitySpec,
    item_id: str,
) -> Mapping[str, str]:
    """Fetch one raw entity payload by id using the shared lookup flow."""
    py_spec = _to_py_entity_spec(spec)
    try:
        raw_item = await _build_library_client(provider).get_entity_data(py_spec, item_id)
    except LyrionRequestError as err:
        message = str(err)
        if "not found" in message.lower():
            raise MediaNotFoundError(f"{spec.key.title()} not found: {item_id}") from err
        raise ProviderUnavailableError(message) from err
    except (LyrionProtocolError, LyrionTimeoutError) as err:
        raise ProviderUnavailableError(str(err)) from err
    return _normalize_lms_row(raw_item)


def _to_py_entity_spec(spec: LmsEntitySpec):
    """Map MA-side entity spec to the corresponding pylyrion entity spec."""
    if spec.key == "artist":
        return PY_ARTIST_SPEC
    if spec.key == "album":
        return PY_ALBUM_SPEC
    return PY_TRACK_SPEC


async def _fetch_worker(
    provider: LyrionMusicProvider,
    spec: LmsEntitySpec,
    ordered_ids: list[str],
    raw_queue: asyncio.Queue[Mapping[str, str] | object],
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
    raw_queue: asyncio.Queue[Mapping[str, str] | object],
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
            decoded = await _decode_entity(provider, spec, cast("Mapping[str, str]", payload))
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

    raw_queue: asyncio.Queue[Mapping[str, str] | object] = asyncio.Queue(maxsize=2)
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
            _fetch_worker(
                provider,
                spec,
                ordered_ids,
                raw_queue,
                stop_sentinel,
                error_box,
            )
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
    raw_item: Mapping[str, str],
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
) -> AsyncGenerator[Mapping[str, str]]:
    """Yield raw LMS entities in request order via the pylyrion lookup flow."""
    ordered_ids = _normalize_lookup_ids(provider, spec, item_ids)
    total_items = len(ordered_ids)
    if total_items == 0:
        return

    for item_index, item_id in enumerate(ordered_ids, start=1):
        _log_lookup_request(
            provider,
            spec,
            item_id,
            item_index=item_index,
            total_items=total_items,
        )

    try:
        index = 0
        async for raw_item in _build_library_client(provider).iter_raw_entities(
            _to_py_entity_spec(spec),
            ordered_ids,
        ):
            index += 1
            normalized_item = _normalize_lms_row(raw_item)
            if _should_report_lookup_progress():
                fetch_text = f"Fetching {spec.key}s from Lyrion: {index}/{total_items}"
                _update_weighted_sync_progress(
                    phase="entity_fetch",
                    current=index,
                    total=total_items,
                    text=fetch_text,
                )
            requested_id = ordered_ids[index - 1] if index <= total_items else None
            _log_lookup_response(
                provider,
                spec,
                normalized_item,
                requested_id=requested_id,
            )
            yield normalized_item
    except (LyrionProtocolError, LyrionRequestError, LyrionTimeoutError) as err:
        raise ProviderUnavailableError(str(err)) from err


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
    raw_item: Mapping[str, str],
    requested_id: str | None = None,
    request_elapsed_ms: float | None = None,
) -> None:
    """Log one lookup response line with id and best-effort display name."""
    response_id = parsers.extract_item_id(raw_item, id_keys=spec.id_keys) or requested_id
    if response_id is None:
        response_id = "unknown"
    if spec.key == "artist":
        name = raw_item.get("artist") or raw_item.get("name") or response_id
    elif spec.key == "album":
        name = raw_item.get("album") or raw_item.get("title") or response_id
    else:
        name = raw_item.get("title") or raw_item.get("track") or response_id
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
