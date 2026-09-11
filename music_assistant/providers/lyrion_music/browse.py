"""Browse helpers for the Lyrion music provider."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, unquote

from music_assistant_models.media_items import BrowseFolder, ItemMapping, MediaItemType

from . import client

if TYPE_CHECKING:
    from .provider import LyrionMusicProvider

_BROWSE_PAGE_TOKEN_PREFIX = "__page__"
type BrowseItems = list[MediaItemType | ItemMapping | BrowseFolder]
BrowseHandler = Callable[[Any, str | None, str | None], Awaitable[BrowseItems]]


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
    page_size = _get_browse_page_size()
    if offset >= page_size:
        previous_offset = max(0, offset - page_size)
        previous_token = _encode_browse_page_offset(previous_offset)
        items.append(
            BrowseFolder(
                item_id=previous_token,
                provider=provider_instance,
                path=_build_browse_path(
                    provider_instance,
                    *base_parts,
                    previous_token,
                ),
                name="Previous Page",
                translation_key="previous_page",
                is_playable=False,
            )
        )
    if has_more:
        next_offset = offset + page_size
        next_token = _encode_browse_page_offset(next_offset)
        items.append(
            BrowseFolder(
                item_id=next_token,
                provider=provider_instance,
                path=_build_browse_path(
                    provider_instance,
                    *base_parts,
                    next_token,
                ),
                name="Next Page",
                translation_key="next_page",
                is_playable=False,
            )
        )


def _get_browse_page_size() -> int:
    """Return browse page size from provider module for test/runtime parity."""
    from . import provider as provider_mod

    return provider_mod.BROWSE_PAGE_SIZE


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
        artists, has_more = await client.get_artists_page(
            provider,
            offset=offset,
        )
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
        albums, has_more = await client.get_albums_page(
            provider,
            offset=offset,
        )
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
        playlists, has_more = await client.get_playlists_page(
            provider,
            offset=offset,
        )
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
    playlist_tracks, has_more = await client.get_playlist_tracks_page(
        provider,
        playlist_id,
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
        genres, has_more = await client.get_genres_page(
            provider,
            offset=offset,
        )
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


async def browse_path(
    provider: LyrionMusicProvider,
    path: str,
) -> BrowseItems:
    """Browse artists, albums, tracks, playlists and genres from LMS."""
    path_parts = path.split("://", 1)[1].split("/") if "://" in path else []
    section = path_parts[0] if path_parts else None
    item_id = unquote(path_parts[1]) if len(path_parts) > 1 else None
    sub_item_id = unquote(path_parts[2]) if len(path_parts) > 2 else None

    if not section:
        return _build_root_browse(path, provider.instance_id)

    browse_handlers: dict[str, BrowseHandler] = {
        "artists": _browse_artists,
        "albums": _browse_albums,
        "tracks": _browse_tracks,
        "playlists": _browse_playlists,
        "genres": _browse_genres,
    }
    if handler := browse_handlers.get(section):
        return await handler(provider, item_id, sub_item_id)
    return []
