"""Facade helpers that group parser and artwork utilities."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Literal, TypeVar

from pylyrion import artwork, media_parsers
from pylyrion.media_items import (
    LyrionAlbum,
    LyrionArtist,
    LyrionArtistRef,
    LyrionPlaylist,
    LyrionTrack,
)

ArtworkItem = TypeVar("ArtworkItem")


class LyrionMediaParserFacade:
    """Expose grouped media-parser helpers for provider adapters."""

    def parse_artist(
        self,
        row: Mapping[str, str],
        artwork_url: str | None = None,
    ) -> LyrionArtist:
        """Parse one normalized LMS artist row."""
        return media_parsers.parse_artist(row, artwork_url=artwork_url)

    def parse_album(
        self,
        row: Mapping[str, str],
        artwork_url: str | None = None,
    ) -> LyrionAlbum:
        """Parse one normalized LMS album row."""
        return media_parsers.parse_album(row, artwork_url=artwork_url)

    def parse_track(
        self,
        row: Mapping[str, str],
        artwork_url: str | None = None,
    ) -> LyrionTrack:
        """Parse one normalized LMS track row."""
        return media_parsers.parse_track(row, artwork_url=artwork_url)

    def parse_playlist(self, row: Mapping[str, str]) -> LyrionPlaylist:
        """Parse one normalized LMS playlist row."""
        return media_parsers.parse_playlist(row)

    def extract_item_id(
        self,
        row: Mapping[str, str],
        id_keys: tuple[str, ...] = ("id", "track_id", "album_id", "artist_id"),
    ) -> str | None:
        """Extract first available id from one normalized payload."""
        return media_parsers.extract_item_id(row, id_keys=id_keys)

    def to_lms_stream_url(
        self,
        *,
        track_id: str,
        host: str,
        port: int,
        stream_path_template: str = media_parsers.STREAM_PATH_TEMPLATE,
        raw_url: str | None = None,
    ) -> str:
        """Resolve stream URL from raw URL or LMS stream path template."""
        return media_parsers.to_lms_stream_url(
            track_id=track_id,
            host=host,
            port=port,
            stream_path_template=stream_path_template,
            raw_url=raw_url,
        )

    def extract_artist_ref(
        self,
        row: Mapping[str, str],
        preferred_name_keys: tuple[str, ...],
        preferred_id_keys: tuple[str, ...],
    ) -> tuple[str | None, str | None]:
        """Extract artist name and provider id from one payload."""
        return media_parsers.extract_artist_ref(
            row,
            preferred_name_keys=preferred_name_keys,
            preferred_id_keys=preferred_id_keys,
        )

    def extract_track_artists(
        self,
        row: Mapping[str, str],
    ) -> list[LyrionArtistRef]:
        """Extract track artists with LMS role precedence."""
        return media_parsers.extract_track_artists(row)

    def extract_first_list_value(self, value: str) -> str | None:
        """Extract first value from scalar or comma-separated field."""
        return media_parsers.extract_first_list_value(value)

    def extract_values_for_keys(
        self,
        row: Mapping[str, str],
        keys: tuple[str, ...],
        split_mode: Literal["id", "name"],
    ) -> list[str]:
        """Extract values for first populated key."""
        return media_parsers.extract_values_for_keys(row, keys, split_mode)

    def parse_int(self, value: str | None, default: int = 0) -> int:
        """Parse integer-like payload value with fallback."""
        return media_parsers.parse_int(value, default=default)


class LyrionArtworkFacade:
    """Expose grouped artwork URL and probing helpers for adapters."""

    async def ensure_preferred_artwork_size(
        self,
        item: ArtworkItem,
        probe_image: Callable[[str], Awaitable[bool]],
        get_thumb_path: Callable[[ArtworkItem], str | None],
        set_thumb_path: Callable[[ArtworkItem, str | None], None],
    ) -> tuple[str, ...]:
        """Keep 600px artwork when reachable, otherwise fall back to 300px."""
        return await artwork.ensure_preferred_artwork_size(
            item,
            probe_image,
            get_thumb_path,
            set_thumb_path,
        )

    async def fetch_remote_image_if_ok(
        self,
        session: Any,
        url: str,
    ) -> bytes | None:
        """Fetch image bytes when endpoint returns a valid image payload."""
        return await artwork.fetch_remote_image_if_ok(session, url)

    async def probe_remote_image(
        self,
        session: Any,
        cache: Any,
        provider_id: str,
        url: str,
        cache_category: str,
    ) -> bool:
        """Probe remote image endpoint and cache outcome."""
        return await artwork.probe_remote_image(
            session,
            cache,
            provider_id,
            url,
            cache_category,
        )

    def append_artwork_cache_buster(self, url: str, token: str | None) -> str:
        """Append cache-buster query token when provided."""
        return artwork.append_artwork_cache_buster(url, token)

    def normalize_artist_artwork_path(self, path: str) -> str:
        """Normalize artist artwork path to a canonical LMS variant."""
        return artwork.normalize_artist_artwork_path(path)

    def artwork_fallback_path(self, path: str) -> str | None:
        """Return preferred fallback artwork path for 600px URLs."""
        return artwork.artwork_fallback_path(path)

    def extract_artwork_url(
        self,
        row: Mapping[str, str],
        *,
        host: str,
        port: int,
        fallback_id: str | None = None,
        cache_buster_token: str | None = None,
    ) -> str | None:
        """Extract artwork URL from album/track payload fields."""
        return artwork.extract_artwork_url(
            row,
            host=host,
            port=port,
            fallback_id=fallback_id,
            cache_buster_token=cache_buster_token,
        )

    def extract_artist_artwork_url(
        self,
        row: Mapping[str, str],
        *,
        host: str,
        port: int,
        fallback_artist_id: str | None = None,
        cache_buster_token: str | None = None,
    ) -> str | None:
        """Extract artwork URL from artist payload fields."""
        return artwork.extract_artist_artwork_url(
            row,
            host=host,
            port=port,
            fallback_artist_id=fallback_artist_id,
            cache_buster_token=cache_buster_token,
        )

    def to_lms_absolute_url(self, host: str, port: int, path: str) -> str:
        """Build absolute LMS URL from relative path."""
        return artwork.to_lms_absolute_url(host, port, path)


__all__ = [
    "LyrionArtworkFacade",
    "LyrionMediaParserFacade",
]
