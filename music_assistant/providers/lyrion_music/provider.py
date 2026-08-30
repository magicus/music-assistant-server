"""Lyrion (LMS) music provider implementation."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence
from time import time_ns
from typing import Any, cast
from urllib.parse import quote, unquote

from music_assistant_models.config_entries import ConfigActionResult, ConfigEntry
from music_assistant_models.enums import ConfigEntryType, ContentType, MediaType, StreamType
from music_assistant_models.errors import (
    MediaNotFoundError,
    ProviderUnavailableError,
    SetupFailedError,
)
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

from . import artwork, client, parsers, sync
from .constants import (
    ACTION_ROTATE_ARTWORK_CACHE_TOKEN,
    CONF_ARTWORK_CACHE_BUSTER,
    CONF_LMS_HOST,
    CONF_LMS_PORT,
    DEFAULT_LMS_PORT,
    ITEM_CACHE_TTL,
    SEARCH_CACHE_TTL,
)


class LyrionMusicProvider(MusicProvider):
    """Music provider that reads catalog metadata from a Lyrion/LMS server."""

    _disabled_batch_lookup_keys: set[str]

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
        configured_host = self._get_configured_host() or ""
        configured_port = self._get_configured_port(default=None)
        return (
            ConfigEntry(
                key=CONF_LMS_HOST,
                type=ConfigEntryType.STRING,
                required=False,
                read_only=True,
                value=configured_host,
            ),
            ConfigEntry(
                key=CONF_LMS_PORT,
                type=ConfigEntryType.INTEGER,
                required=False,
                read_only=True,
                value=configured_port,
            ),
            ConfigEntry(
                key=ACTION_ROTATE_ARTWORK_CACHE_TOKEN,
                type=ConfigEntryType.ACTION,
                action=ACTION_ROTATE_ARTWORK_CACHE_TOKEN,
            ),
        )

    async def handle_config_action(
        self,
        action: str,
    ) -> tuple[ConfigEntry, ...] | ConfigActionResult | None:
        """Handle one-shot options actions."""
        if action != ACTION_ROTATE_ARTWORK_CACHE_TOKEN:
            return await super().handle_config_action(action)

        # Cache token is appended to image URLs, forcing a fresh download.
        self._update_setup_data(
            CONF_ARTWORK_CACHE_BUSTER,
            str(time_ns()),
            immediate=True,
        )
        self.logger.info(
            "Rotated Lyrion artwork cache token for %s",
            self.instance_id,
        )
        return ConfigActionResult(translation_key=ACTION_ROTATE_ARTWORK_CACHE_TOKEN)

    async def handle_async_init(self) -> None:
        """Validate the configured Lyrion endpoint."""
        self._disabled_batch_lookup_keys = set()
        host = self._get_configured_host()
        port = self._get_configured_port()
        if not host:
            raise SetupFailedError("Please configure the Lyrion host before connecting.")
        self.logger.debug(
            "Validating Lyrion JSON-RPC endpoint %s:%s",
            host,
            port,
        )
        try:
            await client.rpc_request(
                self,
                player_id="",
                command=["serverstatus", 0, 1],
            )
        except ProviderUnavailableError as err:
            self.logger.warning(
                "Lyrion endpoint validation failed for %s:%s: %s",
                host,
                port,
                err,
            )
            raise SetupFailedError(str(err)) from err

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
        item_id = path_parts[1] if len(path_parts) > 1 else None

        if not section:
            return [
                BrowseFolder(
                    item_id="artists",
                    provider=self.instance_id,
                    path=f"{path}artists",
                    name="Artist",
                    translation_key="artists",
                    is_playable=False,
                ),
                BrowseFolder(
                    item_id="albums",
                    provider=self.instance_id,
                    path=f"{path}albums",
                    name="Album",
                    translation_key="albums",
                    is_playable=False,
                ),
                BrowseFolder(
                    item_id="tracks",
                    provider=self.instance_id,
                    path=f"{path}tracks",
                    name="Track",
                    translation_key="tracks",
                    is_playable=False,
                ),
                BrowseFolder(
                    item_id="playlists",
                    provider=self.instance_id,
                    path=f"{path}playlists",
                    name="Playlist",
                    translation_key="playlists",
                    is_playable=False,
                ),
                BrowseFolder(
                    item_id="genres",
                    provider=self.instance_id,
                    path=f"{path}genres",
                    name="Genre",
                    translation_key="genres",
                    is_playable=False,
                ),
            ]

        if section == "artists" and item_id is None:
            artists = await client.get_all_artists(self)
            return [
                BrowseFolder(
                    item_id=artist.item_id,
                    provider=self.instance_id,
                    path=f"{path}/{quote(artist.item_id, safe='')}",
                    name=artist.name,
                    is_playable=False,
                )
                for artist in artists
            ]

        if section == "artists" and item_id is not None:
            artist_id = unquote(item_id)
            return await client.get_all_albums(
                self,
                filter_value=f"artist_id:{artist_id}",
            )

        if section == "albums" and item_id is None:
            albums = await client.get_all_albums(self)
            return [
                BrowseFolder(
                    item_id=album.item_id,
                    provider=self.instance_id,
                    path=f"{path}/{quote(album.item_id, safe='')}",
                    name=album.name,
                    is_playable=False,
                )
                for album in albums
            ]

        if section == "albums" and item_id is not None:
            album_id = unquote(item_id)
            return await self.get_album_tracks(album_id)

        if section == "tracks" and item_id is None:
            return await client.get_all_tracks(self)

        if section == "playlists" and item_id is None:
            playlists = await client.get_all_playlists(self)
            return [
                BrowseFolder(
                    item_id=playlist["id"],
                    provider=self.instance_id,
                    path=f"{path}/{quote(playlist['id'], safe='')}",
                    name=playlist["name"],
                    is_playable=False,
                )
                for playlist in playlists
            ]

        if section == "playlists" and item_id is not None:
            playlist_id = unquote(item_id)
            return await client.get_playlist_tracks(self, playlist_id)

        if section == "genres" and item_id is None:
            genres = await client.get_all_genres(self)
            return [
                BrowseFolder(
                    item_id=genre["id"],
                    provider=self.instance_id,
                    path=f"{path}/{quote(genre['id'], safe='')}",
                    name=genre["name"],
                    is_playable=False,
                )
                for genre in genres
            ]

        if section == "genres" and item_id is not None:
            genre_id = unquote(item_id)
            return await client.get_all_albums(
                self,
                filter_value=f"genre_id:{genre_id}",
            )

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
