"""Lyrion client helpers for paging, sync progress and MA model mapping."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Mapping
from dataclasses import dataclass
from time import monotonic
from typing import TYPE_CHECKING, Any, Literal, cast

from music_assistant_models.errors import MediaNotFoundError, ProviderUnavailableError

from music_assistant.controllers.tasks import (
    get_current_task,
    update_current_task_progress,
    update_current_task_progress_text,
)
from music_assistant.providers.lyrion.client import (
    get_configured_basic_auth,
    normalize_lms_text_value,
    rpc_request,
)
from pylyrion.errors import LyrionProtocolError, LyrionRequestError, LyrionTimeoutError
from pylyrion.library import ALBUM_SPEC as PY_ALBUM_SPEC
from pylyrion.library import ARTIST_SPEC as PY_ARTIST_SPEC
from pylyrion.library import TRACK_SPEC as PY_TRACK_SPEC
from pylyrion.library import LyrionLibraryClient, normalize_lookup_ids
from pylyrion.lyrion_constants import BATCH_LOOKUP_SIZE
from pylyrion.models import LyrionEndpoint
from pylyrion.session import LyrionSession

from . import parsers
from .constants import ARTWORK_WORKER_COUNT, BROWSE_PAGE_SIZE

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
    supports_batch_lookup: bool = False


ARTIST_SPEC = LmsEntitySpec(
    key="artist",
    command="artists",
    loop_key="artists_loop",
    id_filter_key="artist_id",
    id_keys=("id", "artist_id", "contributor_id"),
    supports_batch_lookup=False,
)

ALBUM_SPEC = LmsEntitySpec(
    key="album",
    command="albums",
    loop_key="albums_loop",
    id_filter_key="album_id",
    id_keys=("id", "album_id"),
    supports_batch_lookup=True,
)

TRACK_SPEC = LmsEntitySpec(
    key="track",
    command="titles",
    loop_key="titles_loop",
    id_filter_key="track_id",
    id_keys=("id", "track_id"),
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
    for artist_row in page.items:
        try:
            artists.append(parsers.parse_artist(provider, artist_row))
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
    for album_row in page.items:
        try:
            albums.append(parsers.parse_album(provider, album_row))
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
    for track_row in page.items:
        try:
            tracks.append(parsers.parse_track(provider, track_row))
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
    for playlist_row in page.items:
        playlist_id = parsers.extract_item_id(
            playlist_row,
            id_keys=("id", "playlist_id"),
        )
        if playlist_id is None:
            continue
        playlist_name = playlist_row.get("playlist") or playlist_row.get("name") or playlist_id
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
    for genre_row in page.items:
        genre_id = parsers.extract_item_id(
            genre_row,
            id_keys=("id", "genre_id"),
        )
        if genre_id is None:
            continue
        genre_name = genre_row.get("genre") or genre_row.get("name") or genre_id
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
    for track_row in page.items:
        if parsers.extract_item_id(track_row, id_keys=("id", "track_id")) is None:
            continue
        tracks.append(parsers.parse_track(provider, track_row))
    return tracks, page.has_more


async def get_playlist_tracks(provider: LyrionMusicProvider, playlist_id: str) -> list[Track]:
    """Return all tracks for a playlist id."""
    track_rows = await _build_library_client(provider).get_playlist_tracks(playlist_id)
    tracks: list[Track] = []
    for track_row in track_rows:
        if parsers.extract_item_id(track_row, id_keys=("id", "track_id")) is None:
            continue
        tracks.append(parsers.parse_track(provider, track_row))
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
        basic_auth_headers=get_configured_basic_auth(provider),
    )


def _build_library_client(provider: LyrionMusicProvider) -> LyrionLibraryClient:
    """Create a pylyrion library client from the MA provider config."""
    return LyrionLibraryClient(_build_library_session(provider))


async def search_artists(provider: LyrionMusicProvider, query: str, limit: int) -> list[Artist]:
    """Search artists in LMS."""
    library = _build_library_client(provider)
    try:
        result = await library.search_entities(PY_ARTIST_SPEC, query, limit)
    except (LyrionProtocolError, LyrionRequestError, LyrionTimeoutError) as err:
        raise ProviderUnavailableError(str(err)) from err
    artists: list[Artist] = []
    for artist_row in result:
        if parsers.extract_item_id(artist_row) is None:
            continue
        artists.append(parsers.parse_artist(provider, artist_row))
    return artists


async def search_albums(provider: LyrionMusicProvider, query: str, limit: int) -> list[Album]:
    """Search albums in LMS."""
    library = _build_library_client(provider)
    try:
        result = await library.search_entities(PY_ALBUM_SPEC, query, limit)
    except (LyrionProtocolError, LyrionRequestError, LyrionTimeoutError) as err:
        raise ProviderUnavailableError(str(err)) from err
    albums: list[Album] = []
    for album_row in result:
        if parsers.extract_item_id(album_row) is None:
            continue
        albums.append(parsers.parse_album(provider, album_row))
    return albums


async def search_tracks(provider: LyrionMusicProvider, query: str, limit: int) -> list[Track]:
    """Search tracks in LMS."""
    library = _build_library_client(provider)
    try:
        result = await library.search_entities(PY_TRACK_SPEC, query, limit)
    except (LyrionProtocolError, LyrionRequestError, LyrionTimeoutError) as err:
        raise ProviderUnavailableError(str(err)) from err
    tracks: list[Track] = []
    for track_row in result:
        if parsers.extract_item_id(track_row) is None:
            continue
        tracks.append(parsers.parse_track(provider, track_row))
    return tracks


async def get_artist_data(provider: LyrionMusicProvider, artist_id: str) -> Mapping[str, object]:
    """Get artist payload from LMS."""
    return await _get_entity_data(provider, ARTIST_SPEC, artist_id)


async def get_album_data(provider: LyrionMusicProvider, album_id: str) -> Mapping[str, object]:
    """Get album payload from LMS."""
    return await _get_entity_data(provider, ALBUM_SPEC, album_id)


async def get_track_data(provider: LyrionMusicProvider, track_id: str) -> Mapping[str, object]:
    """Get track payload from LMS."""
    return await _get_entity_data(provider, TRACK_SPEC, track_id)


async def _get_entity_data(
    provider: LyrionMusicProvider,
    spec: LmsEntitySpec,
    item_id: str,
) -> Mapping[str, str]:
    """Fetch one normalized entity row by id using the shared lookup flow."""
    py_spec = _to_py_entity_spec(spec)
    try:
        row = await _build_library_client(provider).get_entity_row(py_spec, item_id)
    except LyrionRequestError as err:
        message = str(err)
        if "not found" in message.lower():
            raise MediaNotFoundError(f"{spec.key.title()} not found: {item_id}") from err
        raise ProviderUnavailableError(message) from err
    except (LyrionProtocolError, LyrionTimeoutError) as err:
        raise ProviderUnavailableError(str(err)) from err
    return dict(row)


def _to_py_entity_spec(spec: LmsEntitySpec):
    """Map MA-side entity spec to the corresponding pylyrion entity spec."""
    if spec.key == "artist":
        return PY_ARTIST_SPEC
    if spec.key == "album":
        return PY_ALBUM_SPEC
    return PY_TRACK_SPEC


async def _iter_entities(
    provider: LyrionMusicProvider,
    spec: LmsEntitySpec,
    item_ids: list[str],
) -> AsyncGenerator[Artist | Album | Track]:
    """Yield decoded entities via pylyrion lookup + MA decode callback."""
    ordered_ids = normalize_lookup_ids(item_ids)
    if not ordered_ids:
        return

    artwork_worker_count = max(1, ARTWORK_WORKER_COUNT)

    provider.logger.debug(
        "Lyrion %s decode pipeline concurrency -> %s workers",
        spec.key,
        artwork_worker_count,
    )
    library = _build_library_client(provider)
    async for entity in library.iter_decoded_entities(
        _to_py_entity_spec(spec),
        ordered_ids,
        lambda row: _decode_entity(provider, spec, row),
        worker_count=artwork_worker_count,
    ):
        yield entity


async def _decode_entity(
    provider: LyrionMusicProvider,
    spec: LmsEntitySpec,
    entity_row: Mapping[str, str],
) -> Artist | Album | Track:
    """Decode one normalized LMS row into its MA model."""
    if spec.key == "artist":
        return parsers.parse_artist(provider, entity_row)
    if spec.key == "album":
        return parsers.parse_album(provider, entity_row)
    return parsers.parse_track(provider, entity_row)


async def _iter_entity_rows(
    provider: LyrionMusicProvider,
    spec: LmsEntitySpec,
    item_ids: list[str],
) -> AsyncGenerator[Mapping[str, str]]:
    """Yield normalized LMS rows in request order via pylyrion lookup."""
    ordered_ids = normalize_lookup_ids(item_ids)
    total_items = len(ordered_ids)
    if total_items == 0:
        return

    if len(ordered_ids) != len(item_ids):
        provider.logger.debug(
            "Lyrion %s lookup deduplicated ids from %s to %s",
            spec.key,
            len(item_ids),
            len(ordered_ids),
        )

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
        async for row in _build_library_client(provider).iter_entity_rows(
            _to_py_entity_spec(spec),
            ordered_ids,
        ):
            index += 1
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
                row,
                requested_id=requested_id,
            )
            yield row
    except (LyrionProtocolError, LyrionRequestError, LyrionTimeoutError) as err:
        raise ProviderUnavailableError(str(err)) from err


async def _iter_raw_entities(
    provider: LyrionMusicProvider,
    spec: LmsEntitySpec,
    item_ids: list[str],
) -> AsyncGenerator[Mapping[str, str]]:
    """Yield raw LMS entities in request order, with batch-to-single fallback."""
    ordered_ids = normalize_lookup_ids(item_ids)
    total_items = len(ordered_ids)
    if total_items == 0:
        return

    if total_items == 1:
        item_id = ordered_ids[0]
        _log_lookup_request(provider, spec, item_id, item_index=1, total_items=total_items)
        request_started = monotonic()
        result = await rpc_request(
            provider,
            player_id="",
            command=_create_lookup_command(spec, [item_id]),
        )
        request_elapsed_ms = (monotonic() - request_started) * 1000
        for raw_item in _split_lookup_reply(spec, result, [item_id]):
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
    processed_items = 0
    try:
        if use_batch:
            for chunk in _chunked(ordered_ids, BATCH_LOOKUP_SIZE):
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
                for _index_offset, raw_item in enumerate(
                    _split_lookup_reply(spec, result, chunk),
                    start=chunk_start,
                ):
                    _log_lookup_response(
                        provider,
                        spec,
                        raw_item,
                        request_elapsed_ms=request_elapsed_ms,
                    )
                    yield raw_item
                processed_items += len(chunk)
            return
    except ValueError as err:
        _disable_batch_lookup(provider, spec, err)
        fallback_start = processed_items if use_batch else 0
    except ProviderUnavailableError:
        fallback_start = processed_items if use_batch else 0
    else:
        fallback_start = 0

    for item_index, item_id in enumerate(ordered_ids[fallback_start:], start=fallback_start + 1):
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


def _create_lookup_command(spec: LmsEntitySpec, item_ids: list[str]) -> list[Any]:
    """Create one LMS lookup command for one or many ids."""
    if not item_ids:
        raise ValueError(f"{spec.key} lookup requires at least one id")
    return [
        spec.command,
        0,
        len(item_ids),
        f"{spec.id_filter_key}:{','.join(item_ids)}",
    ]


def _split_lookup_reply(
    spec: LmsEntitySpec,
    result: Mapping[str, object],
    expected_ids: list[str],
) -> list[Mapping[str, str]]:
    """Map LMS lookup replies back to request order and validate misses."""
    raw_items = cast("list[Mapping[str, object]]", result.get(spec.loop_key, []))
    items_by_id: dict[str, Mapping[str, str]] = {}
    for raw_item in raw_items:
        normalized_item = _normalize_lms_row(raw_item)
        item_id = parsers.extract_item_id(normalized_item, id_keys=spec.id_keys)
        if item_id is None or item_id in items_by_id:
            continue
        items_by_id[item_id] = normalized_item

    missing_ids = [item_id for item_id in expected_ids if item_id not in items_by_id]
    if missing_ids:
        raise ValueError(
            f"Lyrion {spec.key} lookup returned incomplete data (missing {len(missing_ids)} ids)"
        )

    return [items_by_id[item_id] for item_id in expected_ids]


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


def _normalize_lms_row(raw_item: Mapping[str, object]) -> Mapping[str, str]:
    """Normalize one LMS payload row into a string-key/string-value mapping."""
    normalized: dict[str, str] = {}
    for key, value in raw_item.items():
        if key is None:
            continue
        text_value = normalize_lms_text_value(value)
        if text_value is None:
            continue
        normalized[str(key)] = text_value
    return normalized


def _chunked(item_ids: list[str], chunk_size: int) -> list[list[str]]:
    """Yield stable chunks from a list of ids."""
    return [
        item_ids[offset : offset + chunk_size] for offset in range(0, len(item_ids), chunk_size)
    ]
