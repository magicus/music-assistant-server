"""Lyrion library sync helpers."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar

from music_assistant_models.enums import MediaType
from music_assistant_models.errors import MediaNotFoundError, ProviderUnavailableError
from music_assistant_models.media_items import Album, Artist, Track

from music_assistant.controllers.tasks import (
    report_current_task_failure,
    update_current_task_progress_from_index,
    update_current_task_progress_text,
)

from . import parsers

if TYPE_CHECKING:
    from music_assistant.controllers.music.media.base import MediaControllerBase
    from music_assistant.providers.lyrion_music.provider import LyrionMusicProvider


ArtworkItem = TypeVar("ArtworkItem", Artist, Album)


ExtraNeedsUpdateFn = Callable[["LyrionMusicProvider", Any, Any], Awaitable[bool]]
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
) -> bool:
    """Return True when linked artist artwork metadata has changed."""
    try:
        library_item = await provider.mass.music.artists.get_library_item(sync_details.item_id)
    except MediaNotFoundError:
        return True
    return parsers.artist_metadata_needs_update(library_item, prov_item)


async def _album_needs_update(
    provider: LyrionMusicProvider,
    sync_details: Any,
    prov_item: Album,
) -> bool:
    """Return True when linked album artist/artwork metadata has changed."""
    try:
        library_item = await provider.mass.music.albums.get_library_item(sync_details.item_id)
    except MediaNotFoundError:
        return True
    return parsers.album_metadata_needs_update(library_item, prov_item)


async def _track_needs_update(
    provider: LyrionMusicProvider,
    sync_details: Any,
    prov_item: Track,
) -> bool:
    """Return True when known track relation backfills are still missing."""
    del provider
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
        fetch_item=provider.get_artist,
        needs_update=parsers.artist_metadata_needs_update,
    )


async def sync_album_artwork_from_library(provider: LyrionMusicProvider) -> None:
    """Refresh album artwork for this provider based on MA library rows."""
    await _sync_library_artwork(
        provider,
        media_type=MediaType.ALBUM,
        controller=provider.mass.music.albums,
        fetch_item=provider.get_album,
        needs_update=parsers.album_metadata_needs_update,
    )


async def _sync_library_artwork(
    provider: LyrionMusicProvider,
    media_type: MediaType,
    controller: MediaControllerBase[ArtworkItem],
    fetch_item: Callable[[str], Awaitable[ArtworkItem]],
    needs_update: Callable[[ArtworkItem, ArtworkItem], bool],
) -> None:
    """Backfill artwork by reloading provider items for mapped MA library entries."""
    library_items = [
        item async for item in controller.iter_library_items(provider=provider.instance_id)
    ]
    total_items = len(library_items)
    if total_items == 0:
        update_current_task_progress_text(f"No {media_type.value}s to backfill artwork for")
        return

    updated_items = 0
    skipped_items = 0
    for item_index, library_item in enumerate(library_items, 1):
        update_current_task_progress_from_index(
            item_index,
            total_items,
            f"Refreshing {media_type.value} artwork {item_index}/{total_items}: {library_item.name}",
        )
        provider_item_id = _resolve_provider_item_id(provider, library_item)
        if provider_item_id is None:
            skipped_items += 1
            continue
        try:
            provider_item = await fetch_item(provider_item_id)
            if not needs_update(library_item, provider_item):
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
        f"Artwork refresh done: updated {updated_items}/{total_items}, skipped {skipped_items}"
    )


def _resolve_provider_item_id(
    provider: LyrionMusicProvider,
    library_item: ArtworkItem,
) -> str | None:
    """Resolve provider item id for this provider from a library item mapping."""
    for mapping in library_item.provider_mappings:
        if mapping.provider_instance == provider.instance_id and mapping.in_library:
            return mapping.item_id
    for mapping in library_item.provider_mappings:
        if mapping.provider_domain == provider.domain and mapping.in_library:
            return mapping.item_id
    return None


async def _sync_library_entities(provider: LyrionMusicProvider, spec: SyncSpec) -> set[int]:
    """Sync one media type using shared logic plus entity-specific hooks."""
    provider.logger.debug("Start sync of %s to Music Assistant library.", spec.media_type.value)
    cur_db_ids: set[int] = set()
    item_count = 0
    controller = spec.controller_getter(provider)

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

            async with provider.mass.music.database.deferred_commit():
                if not sync_details:
                    for prov_map in prov_item.provider_mappings:
                        prov_map.in_library = True
                    library_item = await controller.add_item_to_library(prov_item)
                    db_id = int(library_item.item_id)
                    favorite = library_item.favorite
                else:
                    needs_update = provider._library_item_needs_update(sync_details, prov_item)
                    if not needs_update and spec.extra_needs_update is not None:
                        needs_update = await spec.extra_needs_update(
                            provider, sync_details, prov_item
                        )

                    if needs_update:
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
        except Exception as err:
            provider._handle_sync_item_failure(spec.media_type, prov_item.uri, err)
            provider._protect_failed_sync_item(
                spec.media_type, prov_item.item_id, db_id, cur_db_ids
            )
            continue

        if spec.post_item_sync is not None:
            try:
                await spec.post_item_sync(provider, prov_item)
            except Exception as err:
                provider._handle_sync_item_failure(spec.media_type, prov_item.uri, err)

    return cur_db_ids
