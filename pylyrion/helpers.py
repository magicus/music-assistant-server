"""Facade helpers that group parser and artwork utilities."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any, Literal, TypeVar

from pylyrion import artwork, media_parsers
from pylyrion.media_items import (
    LyrionAlbum,
    LyrionArtist,
    LyrionArtistRef,
    LyrionPlaylist,
    LyrionTrack,
)

ArtworkItem = TypeVar("ArtworkItem")

if TYPE_CHECKING:
    from pylyrion.server_control import LyrionServerControl


class LyrionPlayerControl:
    """Thin per-player control facade bound to one LMS player id."""

    def __init__(self, server_control: LyrionServerControl, player_id: str) -> None:
        """Store the parent server control and the target player id."""
        self._server_control = server_control
        self.player_id = player_id

    async def get_status(self) -> dict[str, Any]:
        """Return the current status payload for this player."""
        return await self._server_control.get_player_status(self.player_id)

    def get_last_status_seen_at(self) -> float | None:
        """Return the last status timestamp for this player."""
        return self._server_control._get_last_status_seen_at(self.player_id)

    def get_cached_status(self) -> dict[str, Any] | None:
        """Return the cached status snapshot for this player."""
        return self._server_control._get_cached_status(self.player_id)

    async def verify_status_expectation(
        self,
        baseline: float | None,
        expectation: Callable[[dict[str, Any]], bool] | None = None,
        expected_state: str = "status update",
    ) -> bool:
        """Verify expected status for this player via the status stream."""
        return await self._server_control._verify_status_expectation(
            self.player_id,
            baseline,
            expectation,
            expected_state,
        )

    async def play(self) -> dict[str, Any]:
        """Resume playback for this player."""
        return await self._server_control.play_player(self.player_id)

    async def pause(self) -> dict[str, Any]:
        """Pause playback for this player."""
        return await self._server_control.pause_player(self.player_id)

    async def stop(self) -> dict[str, Any]:
        """Stop playback for this player."""
        return await self._server_control.stop_player(self.player_id)

    async def set_power(self, powered: bool) -> None:
        """Set power state for this player."""
        await self._server_control.player_set_power(self.player_id, powered)

    async def set_volume(self, volume_level: int) -> dict[str, Any]:
        """Set volume for this player."""
        return await self._server_control.set_player_volume(self.player_id, volume_level)

    async def set_muted(self, muted: bool) -> dict[str, Any]:
        """Set mute state for this player."""
        return await self._server_control.set_player_muted(self.player_id, muted)

    async def next_track(self) -> dict[str, Any]:
        """Skip to the next queue entry."""
        return await self._server_control.next_player_track(self.player_id)

    async def previous_track(self) -> dict[str, Any]:
        """Skip to the previous queue entry."""
        return await self._server_control.previous_player_track(self.player_id)

    async def seek(self, position: int) -> dict[str, Any]:
        """Seek this player to the target playback position."""
        return await self._server_control.seek_player(self.player_id, position)

    async def sync_to(self, leader_player_id: str) -> dict[str, Any]:
        """Sync this player to a leader player."""
        return await self._server_control.sync_player_to(self.player_id, leader_player_id)

    async def unsync(self) -> dict[str, Any]:
        """Remove this player from its sync group."""
        return await self._server_control.unsync_player(self.player_id)

    async def get_queue_status(self, *, offset: int = 0, limit: int) -> dict[str, Any]:
        """Return queue info for this player."""
        return await self._server_control.get_player_queue_status(
            self.player_id,
            offset=offset,
            limit=limit,
        )

    async def set_queue_index(self, index: int | str) -> dict[str, Any]:
        """Set the active queue index for this player."""
        return await self._server_control.set_player_queue_index(self.player_id, index)

    async def set_sync_volume(self, enabled: bool) -> dict[str, Any]:
        """Enable or disable sync volume for this player."""
        return await self._server_control.set_player_sync_volume(self.player_id, enabled)

    async def set_repeat_mode(self, repeat_mode: int) -> dict[str, Any]:
        """Set repeat mode for this player."""
        return await self._server_control.set_player_repeat_mode(self.player_id, repeat_mode)

    async def set_shuffle_mode(self, shuffle_mode: int) -> dict[str, Any]:
        """Set shuffle mode for this player."""
        return await self._server_control.set_player_shuffle_mode(self.player_id, shuffle_mode)

    async def clear_queue(self) -> dict[str, Any]:
        """Clear the queue for this player."""
        return await self._server_control.clear_player_queue(self.player_id)

    async def add_track_id_to_queue(
        self,
        track_id: str,
        *,
        command: str = "add",
    ) -> dict[str, Any]:
        """Add or load one track id to this player's queue."""
        return await self._server_control.add_player_track_id_to_queue(
            self.player_id,
            track_id,
            command=command,
        )

    async def add_url_to_queue(
        self,
        url: str,
        title: str | None = None,
        artist: str | None = None,
        album: str | None = None,
    ) -> dict[str, Any]:
        """Queue one URL for this player."""
        return await self._server_control.add_player_url_to_queue(
            self.player_id,
            url,
            title=title,
            artist=artist,
            album=album,
        )

    async def play_url(self, url: str) -> dict[str, Any]:
        """Play one URL immediately on this player."""
        return await self._server_control.play_player_url(self.player_id, url)

    async def append_url(self, url: str) -> dict[str, Any]:
        """Append one URL to this player's queue."""
        return await self._server_control.append_player_url(self.player_id, url)

    async def move_queue_item(self, from_index: int, to_index: int) -> dict[str, Any]:
        """Move one queue item in this player's queue."""
        return await self._server_control.move_player_queue_item(
            self.player_id,
            from_index,
            to_index,
        )

    async def delete_queue_item(self, index: int) -> dict[str, Any]:
        """Delete one queue item from this player's queue."""
        return await self._server_control.delete_player_queue_item(self.player_id, index)


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
    "LyrionPlayerControl",
]
