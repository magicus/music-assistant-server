"""Lyrion (LMS) music provider implementation."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable, Sequence
from time import time_ns
from typing import Any

from music_assistant_models.background_task import TaskSchedule
from music_assistant_models.config_entries import ConfigActionResult, ConfigEntry
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    EventType,
    MediaType,
    StreamType,
)
from music_assistant_models.errors import InvalidDataError, MediaNotFoundError
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    BrowseFolder,
    ItemMapping,
    MediaItemType,
    Playlist,
    SearchResults,
    Track,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.cache import use_cache
from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.lyrion.client import (
    get_configured_host as get_shared_configured_host,
)
from music_assistant.providers.lyrion.client import (
    get_configured_port as get_shared_configured_port,
)
from music_assistant.providers.lyrion.setup_flow import validate_lms_endpoint
from pylyrion.lyrion_constants import CONF_ARTWORK_CACHE_BUSTER, ITEM_CACHE_TTL, SEARCH_CACHE_TTL

from . import artwork, browse, client, parsers, sync
from .constants import ACTION_RESCAN_ARTWORK, BROWSE_PAGE_SIZE


class LyrionMusicProvider(MusicProvider):
    """Music provider that reads catalog metadata from a Lyrion/LMS server."""

    _disabled_batch_lookup_keys: set[str]
    _unsubscribe_music_sync_completed: Callable[[], None] | None

    @property
    def is_streaming_provider(self) -> bool:
        """Return False for this local, server-hosted library catalog."""
        return False

    @property
    def supported_media_types(self) -> set[MediaType]:
        """Return media types this provider can currently serve."""
        return {
            MediaType.ARTIST,
            MediaType.ALBUM,
            MediaType.TRACK,
            MediaType.PLAYLIST,
        }

    async def get_library_artists(self) -> AsyncGenerator[Artist]:
        """Retrieve library artists from LMS."""
        async for artist in client.iter_library_artists(self):
            yield artist

    async def get_library_albums(self) -> AsyncGenerator[Album]:
        """Retrieve library albums from LMS."""
        async for album in client.iter_library_albums(self):
            yield album

    async def get_library_tracks(self) -> AsyncGenerator[Track]:
        """Retrieve library tracks from LMS."""
        async for track in client.iter_library_tracks(self):
            yield track

    async def get_library_playlists(self) -> AsyncGenerator[Playlist]:
        """Retrieve library playlists from LMS."""
        for playlist in await client.get_all_playlists(self):
            yield parsers.parse_playlist(self, playlist)

    async def get_library_genres(self) -> AsyncGenerator[str]:
        """Retrieve library genres from LMS."""
        for genre in await client.get_all_genres(self):
            yield genre["name"]

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to configure this provider."""
        return (
            ConfigEntry(
                key=ACTION_RESCAN_ARTWORK,
                type=ConfigEntryType.ACTION,
                action=ACTION_RESCAN_ARTWORK,
            ),
        )

    async def handle_config_action(
        self,
        action: str,
    ) -> tuple[ConfigEntry, ...] | ConfigActionResult | None:
        """Handle one-shot options actions."""
        if action != ACTION_RESCAN_ARTWORK:
            return await super().handle_config_action(action)

        self._rotate_artwork_cache_token()
        self._trigger_artwork_backfill_tasks()
        return ConfigActionResult(
            translation_key=ACTION_RESCAN_ARTWORK,
        )

    async def handle_async_init(self) -> None:
        """Validate the configured Lyrion endpoint."""
        self._disabled_batch_lookup_keys = set()
        self._unsubscribe_music_sync_completed = None
        host = self.get_configured_host()
        port = self.get_configured_port()
        self.logger.debug(
            "Validating Lyrion JSON-RPC endpoint %s:%s",
            host,
            port,
        )
        await validate_lms_endpoint(
            host=host,
            port=port,
            http_session=self.mass.http_session,
            translation_owner=self.translation_owner,
        )

    def get_configured_host(self) -> str | None:
        """Return configured LMS host for this provider."""
        return get_shared_configured_host(self)

    def get_configured_port(self, default: int | None = None) -> int | None:
        """Return configured LMS port for this provider."""
        return get_shared_configured_port(self, default)

    async def loaded_in_mass(self) -> None:
        """Subscribe to sync-completed events once provider is active."""
        await super().loaded_in_mass()
        self._unsubscribe_music_sync_completed = self.mass.subscribe(
            self._on_music_sync_completed,
            EventType.MUSIC_SYNC_COMPLETED,
        )
        self._register_artwork_backfill_tasks()

    async def unload(self, is_removed: bool = False) -> None:
        """Cleanup event subscriptions on provider unload."""
        if self._unsubscribe_music_sync_completed is not None:
            self._unsubscribe_music_sync_completed()
            self._unsubscribe_music_sync_completed = None
        self.mass.tasks.unregister_scheduled_task(
            self._album_artwork_task_id,
            clear_persisted_state=is_removed,
        )
        self.mass.tasks.unregister_scheduled_task(
            self._artist_artwork_task_id,
            clear_persisted_state=is_removed,
        )
        await super().unload(is_removed)

    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 5,
    ) -> SearchResults:
        """
        Search artists, albums and tracks in LMS.

        :param search_query: Search query.
        :param media_types: A list of media_types to include.
        :param limit: Number of items to return per media type.
        """
        result = SearchResults()

        if MediaType.ARTIST in media_types:
            result.artists = await self._search_artists(search_query, limit)
        if MediaType.ALBUM in media_types:
            result.albums = await self._search_albums(search_query, limit)
        if MediaType.TRACK in media_types:
            result.tracks = await self._search_tracks(search_query, limit)

        return result

    async def resolve_image(self, path: str) -> str | bytes:
        """Resolve artist artwork URLs with LMS size fallback when needed."""
        return await artwork.resolve_image(self, path)

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """
        Get full artist details by id.

        :param prov_artist_id: Provider artist id.
        """
        return await artwork.build_artist(
            self,
            await self._get_artist_data(prov_artist_id),
        )

    async def get_album(self, prov_album_id: str) -> Album:
        """
        Get full album details by id.

        :param prov_album_id: Provider album id.
        """
        return await artwork.build_album(
            self,
            await self._get_album_data(prov_album_id),
        )

    async def get_track(self, prov_track_id: str) -> Track:
        """
        Get full track details by id.

        :param prov_track_id: Provider track id.
        """
        return parsers.parse_track(
            self,
            await self._get_track_data(prov_track_id),
        )

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get full playlist details by id."""
        playlists = await client.get_all_playlists(self)
        for playlist in playlists:
            if playlist["id"] == prov_playlist_id:
                return parsers.parse_playlist(self, playlist)
        raise MediaNotFoundError(f"Playlist not found: {prov_playlist_id}")

    async def get_playlist_tracks(
        self,
        prov_playlist_id: str,
        page: int = 0,
    ) -> list[Track]:
        """Get tracks for a playlist id."""
        tracks, _ = await client.get_playlist_tracks_page(
            self,
            prov_playlist_id,
            offset=page * BROWSE_PAGE_SIZE,
            limit=BROWSE_PAGE_SIZE,
        )
        return tracks

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """
        Get all tracks for the given album id.

        :param prov_album_id: Provider album id.
        """
        return await client.get_album_tracks(self, prov_album_id)

    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Get all albums for the given artist id."""
        albums: list[Album] = []
        offset = 0
        while True:
            page, has_more = await client.get_albums_page(
                self,
                filter_value=f"artist_id:{prov_artist_id}",
                offset=offset,
            )
            albums.extend(page)
            if not has_more:
                break
            offset += BROWSE_PAGE_SIZE
        return albums

    async def get_stream_details(
        self,
        item_id: str,
        media_type: MediaType,
    ) -> StreamDetails:
        """
        Return stream details for an LMS track.

        :param item_id: Provider track id.
        :param media_type: Requested media type.
        """
        if media_type != MediaType.TRACK:
            raise MediaNotFoundError(f"Unsupported media type: {media_type}")

        track_data = await self._get_track_data(item_id)
        track_url = track_data.get("url")
        stream_url = parsers.to_lms_stream_url(self, item_id, track_url)
        content_type = ContentType.try_parse(track_url or stream_url)

        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(content_type=content_type),
            media_type=MediaType.TRACK,
            stream_type=StreamType.HTTP,
            path=stream_url,
            can_seek=True,
            allow_seek=True,
        )

    async def browse(
        self,
        path: str,
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Browse artists, albums, tracks, playlists and genres from LMS."""
        return await browse.browse_path(self, path)

    @use_cache(SEARCH_CACHE_TTL)
    async def _search_artists(self, query: str, limit: int) -> list[Artist]:
        """Search artists in LMS."""
        return await client.search_artists(self, query, limit)

    @use_cache(SEARCH_CACHE_TTL)
    async def _search_albums(self, query: str, limit: int) -> list[Album]:
        """Search albums in LMS."""
        return await client.search_albums(self, query, limit)

    @use_cache(SEARCH_CACHE_TTL)
    async def _search_tracks(self, query: str, limit: int) -> list[Track]:
        """Search tracks in LMS."""
        return await client.search_tracks(self, query, limit)

    @use_cache(ITEM_CACHE_TTL, allow_expired_cache=True)
    async def _get_artist_data(self, artist_id: str) -> dict[str, Any]:
        """Get artist payload from LMS."""
        return await client.get_artist_data(self, artist_id)

    @use_cache(ITEM_CACHE_TTL, allow_expired_cache=True)
    async def _get_album_data(self, album_id: str) -> dict[str, Any]:
        """Get album payload from LMS."""
        return await client.get_album_data(self, album_id)

    async def _sync_library_artists(self) -> set[int]:
        """Sync library artists and refresh rows when artwork differs."""
        return await sync.sync_library_artists(self)

    async def _sync_library_albums(self) -> set[int]:
        """Sync library albums and refresh rows when artist/art differs."""
        return await sync.sync_library_albums(self)

    async def _sync_library_tracks(self) -> set[int]:
        """Sync library tracks using shared Lyrion sync engine."""
        return await sync.sync_library_tracks(self)

    async def _sync_artist_artwork_from_library(self) -> None:
        """Backfill artist artwork for Lyrion-mapped library items."""
        await sync.sync_artist_artwork_from_library(self)

    async def _sync_album_artwork_from_library(self) -> None:
        """Backfill album artwork for Lyrion-mapped library items."""
        await sync.sync_album_artwork_from_library(self)

    @use_cache(ITEM_CACHE_TTL, allow_expired_cache=True)
    async def _get_track_data(self, track_id: str) -> dict[str, Any]:
        """Get track payload from LMS."""
        return await client.get_track_data(self, track_id)

    def _on_music_sync_completed(self, _event: Any) -> None:
        """Queue artwork backfill tasks when global music sync completes."""
        if self.unloading:
            return
        self.logger.info("Regular music sync completed; starting Lyrion artwork backfill tasks")
        self.logger.debug("MUSIC_SYNC_COMPLETED received; queueing Lyrion artwork backfill tasks")
        self._trigger_artwork_backfill_tasks()

    def _trigger_artwork_backfill_tasks(self) -> None:
        """Queue album first, then artist artwork backfill tasks."""
        for task_id in (self._album_artwork_task_id, self._artist_artwork_task_id):
            try:
                self.mass.tasks.run_task(task_id)
            except InvalidDataError as err:
                self.logger.debug(
                    "Artwork backfill task scheduling skipped for %s: %s",
                    task_id,
                    err,
                )

    def _rotate_artwork_cache_token(self) -> None:
        """Rotate cache token appended to Lyrion artwork URLs."""
        self._update_setup_data(
            CONF_ARTWORK_CACHE_BUSTER,
            str(time_ns()),
            immediate=True,
        )
        self.logger.info(
            "Rotated Lyrion artwork cache token for %s",
            self.instance_id,
        )

    def _register_artwork_backfill_tasks(self) -> None:
        """Register visible artwork tasks and keep them disabled by default."""
        task_domain = "lyrion_artwork_sync"
        self.mass.tasks.register_scheduled_task(
            task_id=self._album_artwork_task_id,
            name="Sync Album Artwork for Lyrion Music Library",
            handler=self._sync_album_artwork_from_library,
            schedule=TaskSchedule.hourly(every=12),
            translation_owner=self.translation_owner,
            metadata={
                "task_domain": task_domain,
                "provider_domain": self.domain,
                "provider_instance": self.instance_id,
                "provider_name": self.name,
                "media_type": MediaType.ALBUM.value,
            },
            allow_retry=True,
        )
        self.mass.tasks.register_scheduled_task(
            task_id=self._artist_artwork_task_id,
            name="Sync Artist Artwork for Lyrion Music Library",
            handler=self._sync_artist_artwork_from_library,
            schedule=TaskSchedule.hourly(every=12),
            translation_owner=self.translation_owner,
            metadata={
                "task_domain": task_domain,
                "provider_domain": self.domain,
                "provider_instance": self.instance_id,
                "provider_name": self.name,
                "media_type": MediaType.ARTIST.value,
            },
            allow_retry=True,
        )
        self.mass.tasks.set_task_enabled(self._album_artwork_task_id, False)
        self.mass.tasks.set_task_enabled(self._artist_artwork_task_id, False)

    @property
    def _album_artwork_task_id(self) -> str:
        """Return deterministic task id for album artwork backfill."""
        return f"{self.instance_id}_album_artwork_sync"

    @property
    def _artist_artwork_task_id(self) -> str:
        """Return deterministic task id for artist artwork backfill."""
        return f"{self.instance_id}_artist_artwork_sync"
