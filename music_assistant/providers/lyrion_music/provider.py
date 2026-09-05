"""Lyrion (LMS) music provider implementation."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from time import time_ns
from typing import Any, cast
from urllib.parse import quote, unquote

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
from music_assistant.providers.lyrion_music.shared.setup_flow import validate_lms_endpoint

from . import artwork, client, parsers, sync
from .constants import (
    ACTION_RESCAN_ARTWORK,
    BROWSE_PAGE_SIZE,
    CONF_ARTWORK_CACHE_BUSTER,
    DEFAULT_LMS_PORT,
    ITEM_CACHE_TTL,
    SEARCH_CACHE_TTL,
)

_BROWSE_PAGE_TOKEN_PREFIX = "__page__"
type BrowseItems = list[MediaItemType | ItemMapping | BrowseFolder]


def _encode_browse_page_offset(offset: int) -> str:
    """Encode a browse page offset into a path-safe token."""
    return f"{_BROWSE_PAGE_TOKEN_PREFIX}{max(0, offset)}"


def _parse_browse_page_offset(raw: str | None) -> int | None:
    """Parse a browse page offset token, returning None for non-page tokens."""
    if raw is None or not raw.startswith(_BROWSE_PAGE_TOKEN_PREFIX):
        return None
    raw_offset = raw.removeprefix(_BROWSE_PAGE_TOKEN_PREFIX)
    if not raw_offset.isdigit():
        return None
    return int(raw_offset)


def _build_browse_path(provider_instance: str, *parts: str) -> str:
    """Build a browse path for this provider from path segments."""
    return f"{provider_instance}://{'/'.join(parts)}"


def _append_browse_page_nav(
    items: BrowseItems,
    provider_instance: str,
    base_parts: tuple[str, ...],
    offset: int,
    has_more: bool,
) -> None:
    """Append previous/next browse page folders when applicable."""
    if offset >= BROWSE_PAGE_SIZE:
        previous_offset = max(0, offset - BROWSE_PAGE_SIZE)
        previous_token = _encode_browse_page_offset(previous_offset)
        items.append(
            BrowseFolder(
                item_id=previous_token,
                provider=provider_instance,
                path=_build_browse_path(provider_instance, *base_parts, previous_token),
                name="Previous Page",
                translation_key="previous_page",
                is_playable=False,
            )
        )
    if has_more:
        next_offset = offset + BROWSE_PAGE_SIZE
        next_token = _encode_browse_page_offset(next_offset)
        items.append(
            BrowseFolder(
                item_id=next_token,
                provider=provider_instance,
                path=_build_browse_path(provider_instance, *base_parts, next_token),
                name="Next Page",
                translation_key="next_page",
                is_playable=False,
            )
        )


def _build_root_browse(path: str, provider_instance: str) -> BrowseItems:
    """Build top-level browse folders."""
    return [
        BrowseFolder(
            item_id="artists",
            provider=provider_instance,
            path=f"{path}artists",
            name="Artist",
            translation_key="artists",
            is_playable=False,
        ),
        BrowseFolder(
            item_id="albums",
            provider=provider_instance,
            path=f"{path}albums",
            name="Album",
            translation_key="albums",
            is_playable=False,
        ),
        BrowseFolder(
            item_id="tracks",
            provider=provider_instance,
            path=f"{path}tracks",
            name="Track",
            translation_key="tracks",
            is_playable=False,
        ),
        BrowseFolder(
            item_id="playlists",
            provider=provider_instance,
            path=f"{path}playlists",
            name="Playlist",
            translation_key="playlists",
            is_playable=False,
        ),
        BrowseFolder(
            item_id="genres",
            provider=provider_instance,
            path=f"{path}genres",
            name="Genre",
            translation_key="genres",
            is_playable=False,
        ),
    ]


async def _browse_artists(
    provider: LyrionMusicProvider,
    item_id: str | None,
    sub_item_id: str | None,
) -> BrowseItems:
    """Browse artists root and per-artist albums with pagination."""
    artists_offset = _parse_browse_page_offset(item_id)
    if item_id is None or artists_offset is not None:
        offset = artists_offset or 0
        artists, has_more = await client.get_artists_page(provider, offset=offset)
        artist_items: BrowseItems = [
            BrowseFolder(
                item_id=artist.item_id,
                provider=provider.instance_id,
                path=_build_browse_path(
                    provider.instance_id,
                    "artists",
                    quote(artist.item_id, safe=""),
                ),
                name=artist.name,
                is_playable=False,
            )
            for artist in artists
        ]
        _append_browse_page_nav(
            artist_items,
            provider.instance_id,
            ("artists",),
            offset,
            has_more,
        )
        return artist_items

    artist_id = item_id
    album_offset = _parse_browse_page_offset(sub_item_id)
    if sub_item_id is not None and album_offset is None:
        return []
    offset = album_offset or 0
    artist_albums, has_more = await client.get_albums_page(
        provider,
        filter_value=f"artist_id:{artist_id}",
        offset=offset,
    )
    artist_album_items: BrowseItems = [*artist_albums]
    _append_browse_page_nav(
        artist_album_items,
        provider.instance_id,
        ("artists", quote(artist_id, safe="")),
        offset,
        has_more,
    )
    return artist_album_items


async def _browse_albums(
    provider: LyrionMusicProvider,
    item_id: str | None,
    sub_item_id: str | None,
) -> BrowseItems:
    """Browse albums root and per-album tracks with pagination."""
    albums_offset = _parse_browse_page_offset(item_id)
    if item_id is None or albums_offset is not None:
        offset = albums_offset or 0
        albums, has_more = await client.get_albums_page(provider, offset=offset)
        album_items: BrowseItems = [
            BrowseFolder(
                item_id=album.item_id,
                provider=provider.instance_id,
                path=_build_browse_path(
                    provider.instance_id,
                    "albums",
                    quote(album.item_id, safe=""),
                ),
                name=album.name,
                is_playable=False,
            )
            for album in albums
        ]
        _append_browse_page_nav(
            album_items,
            provider.instance_id,
            ("albums",),
            offset,
            has_more,
        )
        return album_items

    album_id = item_id
    track_offset = _parse_browse_page_offset(sub_item_id)
    if sub_item_id is not None and track_offset is None:
        return []
    offset = track_offset or 0
    album_tracks, has_more = await client.get_tracks_page(
        provider,
        filter_value=f"album_id:{album_id}",
        offset=offset,
    )
    album_track_items: BrowseItems = [*album_tracks]
    _append_browse_page_nav(
        album_track_items,
        provider.instance_id,
        ("albums", quote(album_id, safe="")),
        offset,
        has_more,
    )
    return album_track_items


async def _browse_tracks(
    provider: LyrionMusicProvider,
    item_id: str | None,
    _sub_item_id: str | None,
) -> BrowseItems:
    """Browse tracks root with pagination."""
    track_offset = _parse_browse_page_offset(item_id)
    if item_id is not None and track_offset is None:
        return []
    offset = track_offset or 0
    tracks, has_more = await client.get_tracks_page(provider, offset=offset)
    track_items: BrowseItems = [*tracks]
    _append_browse_page_nav(
        track_items,
        provider.instance_id,
        ("tracks",),
        offset,
        has_more,
    )
    return track_items


async def _browse_playlists(
    provider: LyrionMusicProvider,
    item_id: str | None,
    sub_item_id: str | None,
) -> BrowseItems:
    """Browse playlists root and per-playlist tracks with pagination."""
    playlists_offset = _parse_browse_page_offset(item_id)
    if item_id is None or playlists_offset is not None:
        offset = playlists_offset or 0
        playlists, has_more = await client.get_playlists_page(provider, offset=offset)
        playlist_items: BrowseItems = [
            BrowseFolder(
                item_id=playlist["id"],
                provider=provider.instance_id,
                path=_build_browse_path(
                    provider.instance_id,
                    "playlists",
                    quote(playlist["id"], safe=""),
                ),
                name=playlist["name"],
                is_playable=False,
            )
            for playlist in playlists
        ]
        _append_browse_page_nav(
            playlist_items,
            provider.instance_id,
            ("playlists",),
            offset,
            has_more,
        )
        return playlist_items

    playlist_id = item_id
    track_offset = _parse_browse_page_offset(sub_item_id)
    if sub_item_id is not None and track_offset is None:
        return []
    offset = track_offset or 0
    playlist_tracks, has_more = await client.get_tracks_page(
        provider,
        filter_value=f"playlist_id:{playlist_id}",
        offset=offset,
    )
    playlist_track_items: BrowseItems = [*playlist_tracks]
    _append_browse_page_nav(
        playlist_track_items,
        provider.instance_id,
        ("playlists", quote(playlist_id, safe="")),
        offset,
        has_more,
    )
    return playlist_track_items


async def _browse_genres(
    provider: LyrionMusicProvider,
    item_id: str | None,
    sub_item_id: str | None,
) -> BrowseItems:
    """Browse genres root and per-genre albums with pagination."""
    genres_offset = _parse_browse_page_offset(item_id)
    if item_id is None or genres_offset is not None:
        offset = genres_offset or 0
        genres, has_more = await client.get_genres_page(provider, offset=offset)
        genre_items: BrowseItems = [
            BrowseFolder(
                item_id=genre["id"],
                provider=provider.instance_id,
                path=_build_browse_path(
                    provider.instance_id,
                    "genres",
                    quote(genre["id"], safe=""),
                ),
                name=genre["name"],
                is_playable=False,
            )
            for genre in genres
        ]
        _append_browse_page_nav(
            genre_items,
            provider.instance_id,
            ("genres",),
            offset,
            has_more,
        )
        return genre_items

    genre_id = item_id
    album_offset = _parse_browse_page_offset(sub_item_id)
    if sub_item_id is not None and album_offset is None:
        return []
    offset = album_offset or 0
    genre_albums, has_more = await client.get_albums_page(
        provider,
        filter_value=f"genre_id:{genre_id}",
        offset=offset,
    )
    genre_album_items: BrowseItems = [*genre_albums]
    _append_browse_page_nav(
        genre_album_items,
        provider.instance_id,
        ("genres", quote(genre_id, safe="")),
        offset,
        has_more,
    )
    return genre_album_items


BrowseHandler = Callable[[Any, str | None, str | None], Awaitable[BrowseItems]]


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
        host = self._get_configured_host()
        port = self._get_configured_port()
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
        del page
        return await client.get_playlist_tracks(self, prov_playlist_id)

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """
        Get all tracks for the given album id.

        :param prov_album_id: Provider album id.
        """
        return await client.get_album_tracks(self, prov_album_id)

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
        raw_url = cast("str | None", track_data.get("url"))
        stream_url = parsers.to_lms_stream_url(self, item_id, raw_url)
        content_type = ContentType.try_parse(raw_url or stream_url)

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
        path_parts = path.split("://", 1)[1].split("/") if "://" in path else []
        section = path_parts[0] if path_parts else None
        item_id = unquote(path_parts[1]) if len(path_parts) > 1 else None
        sub_item_id = unquote(path_parts[2]) if len(path_parts) > 2 else None

        if not section:
            return _build_root_browse(path, self.instance_id)

        browse_handlers: dict[str, BrowseHandler] = {
            "artists": _browse_artists,
            "albums": _browse_albums,
            "tracks": _browse_tracks,
            "playlists": _browse_playlists,
            "genres": _browse_genres,
        }
        if handler := browse_handlers.get(section):
            return await handler(self, item_id, sub_item_id)
        return []

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

    def _get_configured_host(self) -> str | None:
        """Return configured host from setup data with config fallback."""
        return client.get_configured_host(self)

    def _get_configured_port(
        self,
        default: int | None = DEFAULT_LMS_PORT,
    ) -> int | None:
        """Return configured port from setup data with config fallback."""
        return client.get_configured_port(self, default)

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
