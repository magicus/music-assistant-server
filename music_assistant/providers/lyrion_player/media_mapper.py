"""Mapping helpers between MA queue items and LMS queue representations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, cast
from urllib.parse import parse_qs, urlparse

from music_assistant_models.enums import MediaType
from music_assistant_models.errors import MusicAssistantError
from music_assistant_models.media_items import Track

from music_assistant.helpers.uri import create_uri, parse_uri

from .constants import CONF_LMS_HOST, CONF_LMS_PORT

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType

    from .player import LyrionPlayer


class _ConfigProvider(Protocol):
    """Protocol for providers exposing setup value access."""

    def get_setup_value(
        self,
        key: str,
        default: ConfigValueType = None,
    ) -> ConfigValueType:
        """Return configured setup value for key."""


@dataclass(frozen=True)
class LmsQueueEntry:
    """One queue entry as represented in LMS."""

    kind: str
    value: str


class LyrionMediaMapper:
    """Translate MA queue URIs to LMS track_id/url and back."""

    LYRION_PROVIDER_DOMAIN = "lyrion_music"

    def __init__(self, player: LyrionPlayer) -> None:
        """
        Initialize media mapper.

        :param player: Owning Lyrion player instance.
        """
        self.player = player

    async def resolve_ma_uri_to_lms_queue_entry(
        self,
        uri: str,
    ) -> LmsQueueEntry | None:
        """
        Convert one MA URI to an LMS queue entry.

        :param uri: MA queue URI.
        """
        # A direct stream URL is already playable by LMS.
        if self._is_direct_stream_url(uri):
            return LmsQueueEntry(kind="url", value=uri)

        # For MA/library/provider URIs we resolve a native LMS track_id
        # only when the item maps to a Lyrion music provider instance
        # on the same LMS endpoint.
        lms_track_id = await self._resolve_matching_lms_track_id(uri)
        if lms_track_id:
            return LmsQueueEntry(kind="track_id", value=lms_track_id)

        # URI-only mapping failed; caller can still try stream URL fallback.
        return None

    def lms_queue_entry_to_ma_uri(
        self,
        entry: LmsQueueEntry,
        music_provider_instance: str,
    ) -> str:
        """
        Convert one LMS queue entry to an MA URI.

        :param entry: LMS queue entry.
        :param music_provider_instance: Matching Lyrion music
            provider instance id.
        """
        if entry.kind == "track_id":
            return create_uri(
                MediaType.TRACK,
                music_provider_instance,
                entry.value,
            )
        if ma_uri := self._extract_ma_uri_from_stream_redirect_url(entry.value):
            return ma_uri
        return entry.value

    def resolve_matching_music_provider_instance(self) -> str | None:
        """Resolve Lyrion music provider instance running on same LMS endpoint."""
        endpoint = self._provider_lms_endpoint(self.player.provider)
        if endpoint is None:
            return None

        for provider in self.player.mass.providers:
            if provider.domain != self.LYRION_PROVIDER_DOMAIN:
                continue
            if self._provider_lms_endpoint(cast("_ConfigProvider", provider)) == endpoint:
                return provider.instance_id
        return None

    @staticmethod
    def extract_lms_entry_from_playlist_item(
        item: dict[str, object],
    ) -> LmsQueueEntry | None:
        """Extract track_id or URL from one LMS playlist_loop item."""
        track_id_value = item.get("track_id")
        if track_id_value is not None:
            return LmsQueueEntry(kind="track_id", value=str(track_id_value))

        for key in ("url", "uri", "play_url", "playurl", "content_url"):
            value = item.get(key)
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                return LmsQueueEntry(kind="url", value=value)

        # LMS may expose transient queue rows with only a title that still
        # carries the stream URL, while URL-specific keys are absent.
        title_value = item.get("title")
        if isinstance(title_value, str) and title_value.startswith(("http://", "https://")):
            return LmsQueueEntry(kind="url", value=title_value)
        return None

    @staticmethod
    def _is_direct_stream_url(uri: str) -> bool:
        """Return True when URI can be passed directly to LMS as stream URL."""
        return uri.startswith(("http://", "https://"))

    def _extract_ma_uri_from_stream_redirect_url(self, url: str) -> str | None:
        """Extract original MA URI from this provider's stream-redirect URL."""
        parsed = urlparse(url)
        expected_path = f"/{self.player.provider.instance_id}/get_stream_url"
        if parsed.path != expected_path:
            return None
        ma_uri_values = parse_qs(parsed.query).get("ma_uri")
        if not ma_uri_values:
            return None
        ma_uri = ma_uri_values[0]
        return ma_uri or None

    async def _resolve_matching_lms_track_id(self, uri: str) -> str | None:
        """
        Resolve MA/library URI to LMS track id for matching LMS endpoint.

        :param uri: MA URI to resolve.
        """
        try:
            media_type, provider_lookup, item_id = await parse_uri(uri)
        except MusicAssistantError:
            return None

        if media_type != MediaType.TRACK:
            return None

        if self._is_matching_music_provider(provider_lookup):
            return item_id

        if provider_lookup != "library":
            return None

        try:
            library_track = await self.player.mass.music.get_item_by_uri(
                uri,
                allow_update_metadata=False,
            )
        except MusicAssistantError:
            return None

        if not isinstance(library_track, Track):
            return None

        for mapping in library_track.provider_mappings:
            if mapping.provider_domain != self.LYRION_PROVIDER_DOMAIN:
                continue
            if self._is_matching_music_provider(mapping.provider_instance):
                return mapping.item_id
        return None

    def _is_matching_music_provider(self, provider_lookup: str) -> bool:
        """
        Return True when provider lookup resolves to same-endpoint Lyrion music provider.

        :param provider_lookup: Provider instance id or domain string.
        """
        music_provider = self.player.mass.get_provider(provider_lookup)
        if music_provider is None or music_provider.domain != self.LYRION_PROVIDER_DOMAIN:
            return False

        endpoint = self._provider_lms_endpoint(self.player.provider)
        if endpoint is None:
            return False
        return self._provider_lms_endpoint(cast("_ConfigProvider", music_provider)) == endpoint

    @staticmethod
    def _provider_lms_endpoint(provider: _ConfigProvider) -> tuple[str, int] | None:
        """
        Return normalized Lyrion endpoint tuple from provider setup values.

        :param provider: Provider exposing get_setup_value.
        """
        host_raw = provider.get_setup_value(CONF_LMS_HOST)
        if not isinstance(host_raw, str):
            return None
        host = host_raw.strip()
        if not host:
            return None

        port_raw = provider.get_setup_value(CONF_LMS_PORT)
        try:
            port = int(cast("str | int", port_raw))
        except TypeError, ValueError:
            return None
        if port < 1 or port > 65535:
            return None

        return host, port
