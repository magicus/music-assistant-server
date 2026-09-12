"""Lyrion library sync helpers."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass
from time import monotonic
from typing import TYPE_CHECKING, Any, TypeVar

from music_assistant_models.enums import MediaType
from music_assistant_models.errors import MediaNotFoundError, ProviderUnavailableError
from music_assistant_models.media_items import Album, Artist, ProviderMapping, Track

from music_assistant.controllers.tasks import (
    report_current_task_failure,
    update_current_task_progress_from_index,
    update_current_task_progress_text,
)
from pylyrion.library import normalize_lookup_ids

from . import artwork, parsers

if TYPE_CHECKING:
    from music_assistant.controllers.music.media.base import MediaControllerBase
    from music_assistant.providers.lyrion_music.provider import LyrionMusicProvider


ArtworkItem = TypeVar("ArtworkItem", Artist, Album)


ExtraNeedsUpdateFn = Callable[..., Awaitable[bool]]
PostItemSyncFn = Callable[["LyrionMusicProvider", Any], Awaitable[None]]


@dataclass(slots=True)
class SyncSpec:
    """Describe per-entity behavior for generic Lyrion library sync."""

    media_type: MediaType
    iter_items: Callable[[LyrionMusicProvider], AsyncGenerator[Any]]
    controller_getter: Callable[[LyrionMusicProvider], Any]
    extra_needs_update: ExtraNeedsUpdateFn | None = None
    skip_if_new_and_unavailable: bool = False
    post_item_sync: PostItemSyncFn | None = None


async def _artist_needs_update(
    provider: LyrionMusicProvider,
    sync_details: Any,
    prov_item: Artist,
    library_item: Any,
) -> bool:
    """Return True when linked artist artwork metadata has changed."""
    del provider
    del sync_details
    return parsers.artist_metadata_needs_update(library_item, prov_item)


async def _album_needs_update(
    provider: LyrionMusicProvider,
    sync_details: Any,
    prov_item: Album,
    library_item: Any,
) -> bool:
    """Return True when linked album artist/artwork metadata has changed."""
    del provider
    del sync_details
    return parsers.album_metadata_needs_update(library_item, prov_item)


async def _track_needs_update(
    provider: LyrionMusicProvider,
    sync_details: Any,
    prov_item: Track,
    library_item: Any | None = None,
) -> bool:
    """Return True when known track relation backfills are still missing."""
    del provider
    del library_item
    has_album = bool(getattr(sync_details, "has_album", True))
    has_artists = bool(getattr(sync_details, "has_artists", True))
    return bool((prov_item.album and not has_album) or (prov_item.artists and not has_artists))


async def _post_album_sync(provider: LyrionMusicProvider, prov_item: Album) -> None:
    """Optionally import album tracks after a successful album sync."""
    if not provider.library_sync_album_tracks_enabled():
        return
    await provider.import_album_tracks(prov_item.item_id, prov_item.name)


async def _iter_library_artists(provider: LyrionMusicProvider) -> AsyncGenerator[Artist]:
    """Yield artists from provider library for sync."""
    async for item in provider.get_library_artists():
        yield item


async def _iter_library_albums(provider: LyrionMusicProvider) -> AsyncGenerator[Album]:
    """Yield albums from provider library for sync."""
    async for item in provider.get_library_albums():
        yield item


async def _iter_library_tracks(provider: LyrionMusicProvider) -> AsyncGenerator[Track]:
    """Yield tracks from provider library for sync."""
    async for item in provider.get_library_tracks():
        yield item


ARTIST_SYNC_SPEC = SyncSpec(
    media_type=MediaType.ARTIST,
    iter_items=_iter_library_artists,
    controller_getter=lambda provider: provider.mass.music.artists,
    extra_needs_update=_artist_needs_update,
)

ALBUM_SYNC_SPEC = SyncSpec(
    media_type=MediaType.ALBUM,
    iter_items=_iter_library_albums,
    controller_getter=lambda provider: provider.mass.music.albums,
    extra_needs_update=_album_needs_update,
    post_item_sync=_post_album_sync,
)

TRACK_SYNC_SPEC = SyncSpec(
    media_type=MediaType.TRACK,
    iter_items=_iter_library_tracks,
    controller_getter=lambda provider: provider.mass.music.tracks,
    extra_needs_update=_track_needs_update,
    skip_if_new_and_unavailable=True,
)


async def sync_library_artists(provider: LyrionMusicProvider) -> set[int]:
    """Sync library artists and refresh rows when artwork differs."""
    return await _sync_library_entities(provider, ARTIST_SYNC_SPEC)


async def sync_library_albums(provider: LyrionMusicProvider) -> set[int]:
    """Sync library albums and refresh rows when artist or artwork differs."""
    return await _sync_library_entities(provider, ALBUM_SYNC_SPEC)


async def sync_library_tracks(provider: LyrionMusicProvider) -> set[int]:
    """Sync library tracks with the same generic sync engine."""
    return await _sync_library_entities(provider, TRACK_SYNC_SPEC)


async def sync_artist_artwork_from_library(provider: LyrionMusicProvider) -> None:
    """Refresh artist artwork for this provider based on MA library rows."""
    await _sync_library_artwork(
        provider,
        media_type=MediaType.ARTIST,
        controller=provider.mass.music.artists,
    )


async def sync_album_artwork_from_library(provider: LyrionMusicProvider) -> None:
    """Refresh album artwork for this provider based on MA library rows."""
    await _sync_library_artwork(
        provider,
        media_type=MediaType.ALBUM,
        controller=provider.mass.music.albums,
    )


async def _sync_library_artwork(
    provider: LyrionMusicProvider,
    media_type: MediaType,
    controller: MediaControllerBase[ArtworkItem],
) -> None:
    """Backfill artwork using provider-mapping details from previous LMS sync."""
    library_items = [
        item async for item in controller.iter_library_items(provider=provider.instance_id)
    ]
    total_items = len(library_items)
    if total_items == 0:
        update_current_task_progress_text(f"No {media_type.value}s to backfill artwork for")
        return

    updated_items = 0
    skipped_items = 0
    skipped_missing_mapping = 0
    skipped_missing_details = 0
    for item_index, library_item in enumerate(library_items, 1):
        provider.logger.debug(
            "Lyrion %s artwork backfill progress -> item %s/%s (%s)",
            media_type.value,
            item_index,
            total_items,
            library_item.name,
        )
        update_current_task_progress_from_index(
            item_index,
            total_items,
            f"Refreshing {media_type.value} artwork {item_index}/{total_items}: {library_item.name}",
        )
        mapping = _resolve_provider_mapping(provider, library_item)
        if mapping is None:
            skipped_items += 1
            skipped_missing_mapping += 1
            continue
        try:
            provider_item = deepcopy(library_item)
            artwork_url = _extract_artwork_url_from_mapping(provider, media_type, mapping)
            if artwork_url is None:
                skipped_items += 1
                skipped_missing_details += 1
                provider.logger.debug(
                    "Skipping %s artwork refresh for %s (%s): missing artwork details in provider mapping",
                    media_type.value,
                    library_item.item_id,
                    library_item.name,
                )
                continue
            old_thumb = artwork.get_thumb_path(provider_item)
            artwork.set_thumb_path(provider_item, artwork_url)
            validation_started = monotonic()
            attempted_artwork_urls = await artwork.ensure_preferred_artwork_size(
                provider,
                provider_item,
            )
            validation_elapsed_ms = (monotonic() - validation_started) * 1000
            provider.logger.debug(
                "Lyrion %s artwork backfill <- id %s (%s) (elapsed: %.1f ms)",
                media_type.value,
                provider_item.item_id,
                provider_item.name,
                validation_elapsed_ms,
            )
            if validation_elapsed_ms > 99 and attempted_artwork_urls:
                provider.logger.debug(
                    "Lyrion %s artwork backfill slow <- id %s (%s) (elapsed: %.1f ms) (urls: %s)",
                    media_type.value,
                    provider_item.item_id,
                    provider_item.name,
                    validation_elapsed_ms,
                    " | ".join(attempted_artwork_urls),
                )
            if artwork.get_thumb_path(provider_item) == old_thumb:
                continue
            await controller.update_item_in_library(int(library_item.item_id), provider_item)
            updated_items += 1
        except (MediaNotFoundError, ProviderUnavailableError, ValueError) as err:
            skipped_items += 1
            provider.logger.warning(
                "Skipping %s artwork refresh for %s (%s): %s",
                media_type.value,
                library_item.item_id,
                library_item.name,
                err,
            )
            report_current_task_failure(
                f"Failed {media_type.value} {library_item.item_id} ({library_item.name}): {err}"
            )

    update_current_task_progress_text(
        "Artwork refresh done: "
        f"updated {updated_items}/{total_items}, "
        f"skipped {skipped_items} "
        f"(missing mapping: {skipped_missing_mapping}, "
        f"missing details: {skipped_missing_details})"
    )


def _resolve_provider_mapping(
    provider: LyrionMusicProvider,
    library_item: ArtworkItem,
) -> ProviderMapping | None:
    """Resolve provider mapping for this provider from a library item."""
    for mapping in library_item.provider_mappings:
        if mapping.provider_instance == provider.instance_id and mapping.in_library:
            return mapping
    for mapping in library_item.provider_mappings:
        if mapping.provider_domain == provider.domain and mapping.in_library:
            return mapping
    return None


def _extract_artwork_url_from_mapping(
    provider: LyrionMusicProvider,
    media_type: MediaType,
    mapping: ProviderMapping,
) -> str | None:
    """Build artwork URL from stored provider mapping details without LMS lookup."""
    if not mapping.details:
        return None
    try:
        details = json.loads(mapping.details)
    except ValueError:
        return None
    if not isinstance(details, dict):
        return None
    raw = {k: str(v) for k, v in details.items() if v is not None}
    if media_type == MediaType.ARTIST:
        raw.setdefault("id", mapping.item_id)
        return artwork.extract_artist_artwork_url(provider, raw)
    return artwork.extract_artwork_url(provider, raw, fallback_id=mapping.item_id)


async def _lookup_library_items_for_sync(
    provider: LyrionMusicProvider,
    controller: MediaControllerBase[ArtworkItem],
    provider_item_ids: list[str],
) -> dict[str, Any]:
    """Batch-resolve library items for the given provider item ids."""
    if not provider_item_ids:
        return {}

    unique_item_ids = normalize_lookup_ids(provider_item_ids)
    result: dict[str, Any] = {}
    for library_item in await controller.get_library_items_by_prov_id(
        provider_instance=provider.instance_id,
        provider_item_ids=unique_item_ids,
        limit=len(unique_item_ids),
    ):
        for mapping in library_item.provider_mappings:
            if (
                mapping.provider_instance == provider.instance_id
                and mapping.item_id in unique_item_ids
            ):
                result[mapping.item_id] = library_item
                break
    return result


async def _sync_single_library_item(
    provider: LyrionMusicProvider,
    spec: SyncSpec,
    controller: MediaControllerBase[ArtworkItem],
    prov_item: Any,
    sync_details: Any | None,
    needs_update: bool,
    cur_db_ids: set[int],
) -> None:
    """Apply one provider item to the library using the precomputed update decision."""
    db_id: int | None = sync_details.item_id if sync_details else None

    async with provider.mass.music.database.deferred_commit():
        if not sync_details:
            for prov_map in prov_item.provider_mappings:
                prov_map.in_library = True
            library_item = await controller.add_item_to_library(prov_item)
            db_id = int(library_item.item_id)
            favorite = library_item.favorite
        elif needs_update:
            library_item = await controller.update_item_in_library(
                sync_details.item_id,
                prov_item,
            )
            db_id = int(library_item.item_id)
            favorite = library_item.favorite
        else:
            db_id = sync_details.item_id
            favorite = sync_details.favorite

        cur_db_ids.add(db_id)

        if not favorite and prov_item.favorite:
            await controller.set_favorite(db_id, True)

        fallback_genres = (
            set(prov_item.metadata.genres)
            if prov_item.metadata and prov_item.metadata.genres
            else None
        )
        await provider._sync_item_genres(
            spec.media_type,
            prov_item.item_id,
            db_id,
            fallback_genres,
        )

    await asyncio.sleep(0)


async def _sync_library_entities(provider: LyrionMusicProvider, spec: SyncSpec) -> set[int]:
    """Sync one media type using shared logic plus entity-specific hooks."""
    provider.logger.debug("Start sync of %s to Music Assistant library.", spec.media_type.value)
    cur_db_ids: set[int] = set()
    item_count = 0
    controller = spec.controller_getter(provider)
    pending_extra_checks: list[tuple[Any, Any]] = []

    async def flush_pending_extra_checks() -> None:
        if not pending_extra_checks:
            return

        library_items_by_provider_id = await _lookup_library_items_for_sync(
            provider,
            controller,
            [prov_item.item_id for _, prov_item in pending_extra_checks],
        )
        for sync_details, prov_item in pending_extra_checks:
            if not (library_item := library_items_by_provider_id.get(prov_item.item_id)):
                needs_update = True
            else:
                needs_update = await spec.extra_needs_update(
                    provider,
                    sync_details,
                    prov_item,
                    library_item,
                )
            await _sync_single_library_item(
                provider,
                spec,
                controller,
                prov_item,
                sync_details,
                needs_update,
                cur_db_ids,
            )
            if spec.post_item_sync is not None:
                try:
                    await spec.post_item_sync(provider, prov_item)
                except (MediaNotFoundError, ProviderUnavailableError, ValueError) as err:
                    provider._handle_sync_item_failure(spec.media_type, prov_item.uri, err)
        pending_extra_checks.clear()

    async for prov_item in spec.iter_items(provider):
        item_count += 1
        provider._update_sync_task_item_status(spec.media_type, item_count, prov_item.name)
        db_id: int | None = None
        try:
            sync_details = await controller.get_library_item_sync_details(
                prov_item.provider_mappings
            )
            db_id = sync_details.item_id if sync_details else None

            if spec.skip_if_new_and_unavailable and not sync_details and not prov_item.available:
                provider.logger.debug(
                    "Skipping sync of unavailable %s %s",
                    spec.media_type.value,
                    prov_item.uri,
                )
                continue

            needs_update = bool(
                sync_details and provider._library_item_needs_update(sync_details, prov_item)
            )
            if (
                not needs_update
                and spec.extra_needs_update is not None
                and sync_details is not None
            ):
                pending_extra_checks.append((sync_details, prov_item))
                if len(pending_extra_checks) >= 200:
                    await flush_pending_extra_checks()
                continue

            await _sync_single_library_item(
                provider,
                spec,
                controller,
                prov_item,
                sync_details,
                needs_update,
                cur_db_ids,
            )
        except (MediaNotFoundError, ProviderUnavailableError, ValueError) as err:
            provider._handle_sync_item_failure(spec.media_type, prov_item.uri, err)
            provider._protect_failed_sync_item(
                spec.media_type, prov_item.item_id, db_id, cur_db_ids
            )
            continue

        if spec.post_item_sync is not None:
            try:
                await spec.post_item_sync(provider, prov_item)
            except (MediaNotFoundError, ProviderUnavailableError, ValueError) as err:
                provider._handle_sync_item_failure(spec.media_type, prov_item.uri, err)

    await flush_pending_extra_checks()
    return cur_db_ids
