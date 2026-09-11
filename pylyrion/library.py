"""Raw library browsing helpers for pylyrion."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

from pylyrion.session import LyrionSession, normalize_lms_text_value

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


__all__ = [
    "ALBUM_SPEC",
    "ARTIST_SPEC",
    "DEFAULT_BROWSE_PAGE_SIZE",
    "TRACK_SPEC",
    "LyrionEntitySpec",
    "LyrionLibraryClient",
    "LyrionPage",
    "get_entity_page",
    "get_simple_browse_page",
    "normalize_row",
]

# Public module exports end here.
